"""OpenAI Responses API facade over the native runtime.

Lets Responses clients (the shape Codex-style agents speak) use the configured provider
as a model backend. Tool use is client-executed exactly like Chat Completions and the
Anthropic facade: the model's function calls are returned as ``function_call`` output
items and the client sends ``function_call_output`` items back in the next request.
Requests are stateless, so each runs in a short-lived session that is deleted afterwards.

Implemented: text and image input, ``instructions``, function tools and ``tool_choice``,
streaming events, usage, ``max_output_tokens`` and ``reasoning.effort``. Not implemented:
``previous_response_id`` (resend the full input), built-in and custom/freeform tool types
(they are skipped), stored responses and the retrieve/delete/input_items routes.
Token counts are estimates when the provider reports none.
"""
from __future__ import annotations

import json
import time
import uuid

from flask import Response, jsonify, request

from . import config


class ResponsesError(ValueError):
    def __init__(self, message, status=400, code="invalid_request_error"):
        super().__init__(message)
        self.status, self.code = status, code


def _content(value, role):
    """Responses content (string or typed blocks) -> Chat Completions content."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ResponsesError("Message content must be a string or an array of content parts")
    parts = []
    for block in value:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind in ("input_text", "output_text", "text", "refusal"):
            parts.append({"type": "text", "text": str(block.get("text") or block.get("refusal") or "")})
        elif kind == "input_image":
            url = block.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            if not isinstance(url, str) or not url:
                raise ResponsesError("input_image parts need an image_url")
            parts.append({"type": "image_url", "image_url": {"url": url}})
        else:
            raise ResponsesError(f"Unsupported content part type: {kind}")
    if all(part["type"] == "text" for part in parts):
        return "\n".join(part["text"] for part in parts)
    return parts


def convert_input(body):
    """``instructions`` + ``input`` -> Chat Completions message list."""
    out = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        out.append({"role": "system", "content": instructions})
    items = body.get("input")
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    if not isinstance(items, list) or not items:
        raise ResponsesError("input must be a nonempty string or array")
    calls = []

    def flush():
        # Consecutive function_call items are one assistant turn with several calls.
        if calls:
            out.append({"role": "assistant", "content": None, "tool_calls": list(calls)})
            calls.clear()

    for item in items:
        if not isinstance(item, dict):
            raise ResponsesError("Each input item must be an object")
        kind = item.get("type") or ("message" if "role" in item else None)
        if kind == "function_call":
            call_id = item.get("call_id") or item.get("id")
            if not isinstance(call_id, str) or not isinstance(item.get("name"), str):
                raise ResponsesError("function_call items need call_id and name")
            arguments = item.get("arguments", "{}")
            calls.append({"id": call_id, "type": "function", "function": {
                "name": item["name"], "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}})
            continue
        flush()
        if kind == "function_call_output":
            if not isinstance(item.get("call_id"), str):
                raise ResponsesError("function_call_output items need call_id")
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            out.append({"role": "tool", "tool_call_id": item["call_id"], "content": output})
        elif kind == "message":
            role = item.get("role")
            if role not in ("user", "assistant", "system", "developer"):
                raise ResponsesError("Message items need a role of user, assistant, system or developer")
            out.append({"role": role, "content": _content(item.get("content", ""), role)})
        elif kind in ("reasoning", "item_reference"):
            continue  # reasoning summaries are not fed back; references need stored responses
        else:
            raise ResponsesError(f"Unsupported input item type: {kind}")
    flush()
    return out


def convert_tools(body):
    tools = body.get("tools") or []
    if not isinstance(tools, list):
        raise ResponsesError("tools must be an array")
    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise ResponsesError("Each tool must be an object")
        if tool.get("type") != "function":
            continue  # built-in and custom tools are not implemented
        if not isinstance(tool.get("name"), str) or not tool["name"]:
            raise ResponsesError("Function tools need a name")
        converted.append({"type": "function", "function": {
            "name": tool["name"], "description": tool.get("description") or "",
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}}}})
    return converted


def convert_choice(body, tools):
    choice = body.get("tool_choice")
    if not tools:
        return "auto"
    if choice in ("none", "required"):
        return choice
    if isinstance(choice, dict) and choice.get("type") == "function" and isinstance(choice.get("name"), str):
        return {"type": "function", "function": {"name": choice["name"]}}
    return "auto"


def to_chat_body(body):
    if not isinstance(body, dict):
        raise ResponsesError("Request body must be a JSON object")
    if body.get("previous_response_id"):
        raise ResponsesError("previous_response_id is not supported; resend the full input", code="unsupported_parameter")
    tools = convert_tools(body)
    chat = {"messages": convert_input(body), "stream": False}
    if tools:
        chat["tools"] = tools
        chat["tool_choice"] = convert_choice(body, tools)
    if isinstance(body.get("max_output_tokens"), int) and body["max_output_tokens"] > 0:
        chat["max_tokens"] = body["max_output_tokens"]
    for key in ("temperature", "top_p"):
        if isinstance(body.get(key), (int, float)) and not isinstance(body.get(key), bool):
            chat[key] = body[key]
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort") in ("medium", "high"):
        chat["thinking_enabled"] = True
    requested = body.get("model") if isinstance(body.get("model"), str) and body["model"] else config.MODEL
    chat["model"] = config.MODEL if requested.lower().startswith(("gpt", "o1", "o3", "o4", "codex")) else requested
    return chat, requested


def estimate_tokens(chat):
    return max(1, len(json.dumps([chat.get("messages"), chat.get("tools")], ensure_ascii=False)) // 4)


def _usage(final, estimate, output_chars):
    usage = final.get("usage") or {}
    inputs = usage.get("prompt_tokens") or estimate
    outputs = usage.get("completion_tokens") or max(1, output_chars // 4)
    return {"input_tokens": inputs, "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": outputs, "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": inputs + outputs}


def _message_item(item_id, text, status="completed"):
    return {"type": "message", "id": item_id, "status": status, "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}] if text is not None else []}


def _call_item(item_id, call, status="completed"):
    return {"type": "function_call", "id": item_id, "call_id": call["id"], "name": call["function"]["name"],
            "arguments": call["function"]["arguments"], "status": status}


class _Response:
    """Builds the response object shown by every non-streaming reply and streaming event."""

    def __init__(self, requested, body):
        self.id = "resp_" + uuid.uuid4().hex
        self.created = int(time.time())
        self.model = requested
        self.body = body

    def render(self, status, output, usage=None, incomplete=None):
        return {"id": self.id, "object": "response", "created_at": self.created, "status": status,
                "error": None, "incomplete_details": {"reason": incomplete} if incomplete else None,
                "instructions": self.body.get("instructions"), "model": self.model, "output": output,
                "parallel_tool_calls": True, "tool_choice": self.body.get("tool_choice", "auto"),
                "tools": self.body.get("tools") or [], "temperature": self.body.get("temperature"),
                "top_p": self.body.get("top_p"), "max_output_tokens": self.body.get("max_output_tokens"),
                "previous_response_id": None, "store": False, "metadata": self.body.get("metadata") or {},
                "usage": usage}


def build_response(response, final, estimate):
    message = final["message"]
    output = []
    text = message.get("content") or ""
    if text:
        output.append(_message_item("msg_" + uuid.uuid4().hex, text))
    for call in message.get("tool_calls") or []:
        output.append(_call_item("fc_" + uuid.uuid4().hex, call))
    truncated = final.get("finish_reason") == "length"
    chars = sum(len(json.dumps(item, ensure_ascii=False)) for item in output)
    return response.render("incomplete" if truncated else "completed", output, _usage(final, estimate, chars),
                           "max_output_tokens" if truncated else None)


def stream_events(events, response, estimate):
    """Translate runtime events into Responses API server-sent events."""
    counter = iter(range(1_000_000))

    def sse(kind, **data):
        payload = dict(data, type=kind, sequence_number=next(counter))
        return f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield sse("response.created", response=response.render("in_progress", []))
    yield sse("response.in_progress", response=response.render("in_progress", []))
    output, message_id, text, index, final = [], None, "", -1, None

    def open_message():
        nonlocal message_id, index
        message_id, index = "msg_" + uuid.uuid4().hex, index + 1
        return [sse("response.output_item.added", output_index=index, item=_message_item(message_id, None, "in_progress")),
                sse("response.content_part.added", item_id=message_id, output_index=index, content_index=0,
                    part={"type": "output_text", "text": "", "annotations": []})]

    for event in events:
        if event["type"] == "delta" and event["delta"].get("content"):
            if message_id is None:
                yield from open_message()
            text += event["delta"]["content"]
            yield sse("response.output_text.delta", item_id=message_id, output_index=index, content_index=0,
                      delta=event["delta"]["content"])
        elif event["type"] == "response":
            final = event
    if final is None:
        raise ResponsesError("No completion produced", 502, "provider_error")
    if message_id is None and final["message"].get("content"):
        yield from open_message()
        text = final["message"]["content"]
        yield sse("response.output_text.delta", item_id=message_id, output_index=index, content_index=0, delta=text)
    if message_id is not None:
        yield sse("response.output_text.done", item_id=message_id, output_index=index, content_index=0, text=text)
        yield sse("response.content_part.done", item_id=message_id, output_index=index, content_index=0,
                  part={"type": "output_text", "text": text, "annotations": []})
        item = _message_item(message_id, text)
        output.append(item)
        yield sse("response.output_item.done", output_index=index, item=item)
    for call in final["message"].get("tool_calls") or []:
        index += 1
        item_id = "fc_" + uuid.uuid4().hex
        yield sse("response.output_item.added", output_index=index, item=dict(_call_item(item_id, call, "in_progress"), arguments=""))
        yield sse("response.function_call_arguments.delta", item_id=item_id, output_index=index,
                  delta=call["function"]["arguments"])
        yield sse("response.function_call_arguments.done", item_id=item_id, output_index=index,
                  arguments=call["function"]["arguments"])
        item = _call_item(item_id, call)
        output.append(item)
        yield sse("response.output_item.done", output_index=index, item=item)
    truncated = final.get("finish_reason") == "length"
    chars = sum(len(json.dumps(item, ensure_ascii=False)) for item in output)
    snapshot = response.render("incomplete" if truncated else "completed", output, _usage(final, estimate, chars),
                               "max_output_tokens" if truncated else None)
    yield sse("response.incomplete" if truncated else "response.completed", response=snapshot)


def _error_body(message, code, kind="invalid_request_error"):
    return {"error": {"message": message, "type": kind, "param": None, "code": code}}


def register(app, get_runtime, owner, safe_message, harness_error):
    @app.post("/v1/responses")
    def openai_responses():
        body = request.get_json(force=True, silent=True)
        try:
            chat, requested = to_chat_body(body)
        except ResponsesError as exc:
            return jsonify(_error_body(str(exc), exc.code)), exc.status
        runtime = get_runtime()
        estimate = estimate_tokens(chat)
        response = _Response(requested, body)
        if config.PROVIDER == "deepseek-web":
            chat.setdefault("search_enabled", False)
        session, control, events = runtime.prepare(chat, owner(), execute=False)
        finished = []

        def finish():
            # Idempotent: a stream the server never started is closed through call_on_close,
            # and one that ran is closed by generate()'s finally.
            if finished:
                return
            finished.append(True)
            events.close()
            runtime.release(session, control)
            runtime.delete(session)  # stateless protocol: no server-side memory

        if body.get("stream"):
            def generate():
                try:
                    yield from stream_events(events, response, estimate)
                except Exception as exc:  # noqa: BLE001 - surfaced as an SSE error frame
                    if isinstance(exc, harness_error):
                        message, code = safe_message(exc), exc.code
                    elif isinstance(exc, ResponsesError):
                        message, code = str(exc), exc.code
                    else:
                        message, code = "Completion failed", "internal_error"
                    yield ("event: error\ndata: " + json.dumps({"type": "error", "code": code, "message": message,
                                                                "param": None, "sequence_number": 999999}) + "\n\n")
                finally:
                    finish()
            reply = Response(generate(), content_type="text/event-stream", headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            reply.call_on_close(finish)
            return reply
        final = None
        try:
            for event in events:
                if event["type"] == "response":
                    final = event
        finally:
            finish()
        if final is None:
            raise harness_error("No completion produced", "provider_error", 502)
        return jsonify(build_response(response, final, estimate))
