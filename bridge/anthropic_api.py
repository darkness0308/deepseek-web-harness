"""Anthropic Messages API facade over the native runtime.

Lets Anthropic SDK clients (including Claude Code pointed at this server through
``ANTHROPIC_BASE_URL``) use the configured provider as a model backend. Tool use
is client-executed exactly like Chat Completions: the model's ``tool_use`` blocks
are returned and the client sends ``tool_result`` blocks back. Requests are
stateless, so each one runs in a short-lived session that is deleted afterwards.

Not implemented: extended-thinking blocks, prompt caching, the batches/files APIs
and server-side tools. Token counts are estimates when the provider reports none.
"""
from __future__ import annotations

import json
import uuid

from flask import Response, jsonify, request

from . import config

STOP_REASONS = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


class AnthropicError(ValueError):
    def __init__(self, message, status=400, kind="invalid_request_error"):
        super().__init__(message)
        self.status, self.kind = status, kind


def error_body(message, kind):
    return {"type": "error", "error": {"type": kind, "message": message}}


def error_kind(status):
    return {401: "authentication_error", 403: "permission_error", 404: "not_found_error",
            413: "request_too_large", 429: "rate_limit_error", 529: "overloaded_error"}.get(
                status, "invalid_request_error" if status < 500 else "api_error")


def _text(content):
    """Flatten a string or list of text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return "" if content is None else str(content)


def _image(block):
    source = block.get("source") or {}
    if source.get("type") == "base64" and source.get("data"):
        return {"type": "image_url", "image_url": {"url": f"data:{source.get('media_type', 'image/png')};base64,{source['data']}"}}
    if source.get("type") == "url" and source.get("url"):
        return {"type": "image_url", "image_url": {"url": source["url"]}}
    raise AnthropicError("Unsupported image source")


def convert_messages(body):
    """Anthropic ``system`` + ``messages`` -> OpenAI-style message list."""
    out = []
    system = _text(body.get("system"))
    if system.strip():
        out.append({"role": "system", "content": system})
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise AnthropicError("messages must be a nonempty array")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            raise AnthropicError("Each message needs a role of user or assistant")
        content = message.get("content")
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
            continue
        if not isinstance(content, list):
            raise AnthropicError("Message content must be a string or an array of blocks")
        if message["role"] == "assistant":
            text, calls = [], []
            for block in content:
                kind = block.get("type") if isinstance(block, dict) else None
                if kind == "text":
                    text.append(block.get("text", ""))
                elif kind == "tool_use":
                    if not isinstance(block.get("id"), str) or not isinstance(block.get("name"), str):
                        raise AnthropicError("tool_use blocks need id and name")
                    calls.append({"id": block["id"], "type": "function", "function": {
                        "name": block["name"], "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)}})
                elif kind in ("thinking", "redacted_thinking"):
                    continue
                else:
                    raise AnthropicError(f"Unsupported assistant content block: {kind}")
            item = {"role": "assistant", "content": "\n".join(text) if text else (None if calls else "")}
            if calls:
                item["tool_calls"] = calls
            out.append(item)
            continue
        parts, results = [], []
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text":
                if block.get("text"):
                    parts.append({"type": "text", "text": block["text"]})
            elif kind == "image":
                parts.append(_image(block))
            elif kind == "tool_result":
                if not isinstance(block.get("tool_use_id"), str):
                    raise AnthropicError("tool_result blocks need tool_use_id")
                body_text = _text(block.get("content"))
                if block.get("is_error"):
                    body_text = "Error: " + body_text
                results.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": body_text})
            else:
                raise AnthropicError(f"Unsupported user content block: {kind}")
        out.extend(results)  # results must directly follow the assistant's tool calls
        if parts:
            if all(p["type"] == "text" for p in parts):
                out.append({"role": "user", "content": "\n".join(p["text"] for p in parts)})
            else:
                out.append({"role": "user", "content": parts})
    return out


def convert_tools(body):
    tools = body.get("tools") or []
    if not isinstance(tools, list):
        raise AnthropicError("tools must be an array")
    converted = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise AnthropicError("Each tool needs a name")
        if tool.get("type") not in (None, "custom"):
            continue  # server-side tools (web_search, computer use...) are not implemented
        schema = tool.get("input_schema") or {"type": "object", "properties": {}}
        converted.append({"type": "function", "function": {
            "name": tool["name"], "description": tool.get("description", ""), "parameters": schema}})
    return converted


def convert_choice(body, tools):
    choice = body.get("tool_choice")
    if not tools or not isinstance(choice, dict):
        return "auto"
    kind = choice.get("type")
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool" and isinstance(choice.get("name"), str):
        return {"type": "function", "function": {"name": choice["name"]}}
    return "auto"


def to_chat_body(body):
    if not isinstance(body, dict):
        raise AnthropicError("Request body must be an object")
    tools = convert_tools(body)
    chat = {"messages": convert_messages(body), "stream": False}
    if tools:
        chat["tools"] = tools
        chat["tool_choice"] = convert_choice(body, tools)
    if isinstance(body.get("max_tokens"), int) and body["max_tokens"] > 0:
        chat["max_tokens"] = body["max_tokens"]
    for key in ("temperature", "top_p"):
        if isinstance(body.get(key), (int, float)):
            chat[key] = body[key]
    if isinstance(body.get("stop_sequences"), list) and body["stop_sequences"]:
        chat["stop"] = [s for s in body["stop_sequences"] if isinstance(s, str)]
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        chat["thinking_enabled"] = True
    requested = body.get("model") if isinstance(body.get("model"), str) else config.MODEL
    # Anthropic model names mean nothing to the configured provider.
    chat["model"] = config.MODEL if requested.lower().startswith("claude") else requested
    return chat, requested


def estimate_tokens(chat):
    return max(1, len(json.dumps([chat.get("messages"), chat.get("tools")], ensure_ascii=False)) // 4)


def _input_json(call):
    try:
        value = json.loads(call["function"]["arguments"])
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def build_message(final, requested, estimate):
    message = final["message"]
    blocks = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": _input_json(call)})
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    usage = final.get("usage") or {}
    return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": requested,
            "content": blocks, "stop_reason": STOP_REASONS.get(final.get("finish_reason"), "end_turn"),
            "stop_sequence": None,
            "usage": {"input_tokens": usage.get("prompt_tokens") or estimate,
                      "output_tokens": usage.get("completion_tokens") or max(1, sum(
                          len(json.dumps(b, ensure_ascii=False)) for b in blocks) // 4)}}


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def stream_events(events, requested, estimate):
    """Translate runtime events into Anthropic SSE frames."""
    yield _sse("message_start", {"type": "message_start", "message": {
        "id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": requested,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": estimate, "output_tokens": 1}}})
    yield _sse("ping", {"type": "ping"})
    index, text_open, final = -1, False, None
    for event in events:
        if event["type"] == "delta" and event["delta"].get("content"):
            if not text_open:
                index += 1
                text_open = True
                yield _sse("content_block_start", {"type": "content_block_start", "index": index,
                                                   "content_block": {"type": "text", "text": ""}})
            yield _sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                               "delta": {"type": "text_delta", "text": event["delta"]["content"]}})
        elif event["type"] == "response":
            final = event
    if text_open:
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})
    if final is None:
        raise AnthropicError("No completion produced", 502, "api_error")
    for call in final["message"].get("tool_calls") or []:
        index += 1
        yield _sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": {
            "type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": {}}})
        yield _sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {
            "type": "input_json_delta", "partial_json": json.dumps(_input_json(call), ensure_ascii=False)}})
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})
    usage = final.get("usage") or {}
    yield _sse("message_delta", {"type": "message_delta", "delta": {
        "stop_reason": STOP_REASONS.get(final.get("finish_reason"), "end_turn"), "stop_sequence": None},
        "usage": {"output_tokens": usage.get("completion_tokens") or 1}})
    yield _sse("message_stop", {"type": "message_stop"})


def register(app, get_runtime, owner, safe_message, harness_error):
    @app.post("/v1/messages")
    def anthropic_messages():
        body = request.get_json(force=True, silent=True)
        try:
            chat, requested = to_chat_body(body)
        except AnthropicError as exc:
            return jsonify(error_body(str(exc), exc.kind)), exc.status
        runtime = get_runtime()
        estimate = estimate_tokens(chat)
        streaming = bool(body.get("stream"))
        if config.PROVIDER == "deepseek-web":
            chat.setdefault("search_enabled", False)
        session, control, events = runtime.prepare(chat, owner(), execute=False)

        finished = []

        def finish():
            # Idempotent: a stream the server never started is closed through
            # call_on_close, and one that ran is closed by generate()'s finally.
            if finished:
                return
            finished.append(True)
            events.close()
            runtime.release(session, control)
            runtime.delete(session)  # stateless protocol: no server-side memory

        if streaming:
            def generate():
                try:
                    yield from stream_events(events, requested, estimate)
                except Exception as exc:  # noqa: BLE001 - surfaced as an SSE error frame
                    if isinstance(exc, harness_error):
                        status, message = exc.status, safe_message(exc)
                    elif isinstance(exc, AnthropicError):
                        status, message = exc.status, str(exc)
                    else:
                        status, message = 500, "Completion failed"
                    yield _sse("error", error_body(message, error_kind(status)))
                finally:
                    finish()
            response = Response(generate(), content_type="text/event-stream", headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            response.call_on_close(finish)
            return response
        final = None
        try:
            for event in events:
                if event["type"] == "response":
                    final = event
        finally:
            finish()
        if final is None:
            raise harness_error("No completion produced", "provider_error", 502)
        return jsonify(build_message(final, requested, estimate))

    @app.post("/v1/messages/count_tokens")
    def anthropic_count_tokens():
        body = request.get_json(force=True, silent=True)
        try:
            chat, _ = to_chat_body(body)
        except AnthropicError as exc:
            return jsonify(error_body(str(exc), exc.kind)), exc.status
        return jsonify({"input_tokens": estimate_tokens(chat)})
