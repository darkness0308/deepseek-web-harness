"""
Message formatting helpers.

DeepSeek's web API only accepts a single `prompt` string, so when a chatbot
sends a multi-turn `messages` array we flatten it into a readable transcript.
"""

import base64
import json
import re
from typing import List, Dict


def _content_to_text(content) -> str:
    """
    Normalise an OpenAI 'content' value to a plain string.
    content can be:
      - a string
      - a list of content blocks {"type": "text"/"image_url", ...}
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", "").strip())
                elif block.get("type") == "image_url":
                    texts.append("[image attached]")
        return " ".join(texts).strip()
    return "" if content is None else str(content).strip()


def extract_image_files(messages: List[Dict]) -> list[tuple[str, bytes, str]]:
    """
    Extract base64-encoded images from the *last user* message's content blocks.
    Returns a list of (filename, raw_bytes, mime_type) tuples, one per image.
    Only looks at the last user message (the one being sent now).
    """
    results = []
    # Walk from the end to find the last user message
    for msg in reversed(messages):
        if msg.get("role", "").lower() != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            break
        for i, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "image_url":
                continue
            url = block.get("image_url", {}).get("url", "")
            if not url.startswith("data:"):
                continue
            # data:<mime>;base64,<data>
            try:
                header, b64data = url.split(",", 1)
                mime = header.split(";")[0].split(":")[1]  # e.g. image/jpeg
                ext = mime.split("/")[-1]
                raw = base64.b64decode(b64data)
                results.append((f"image_{i}.{ext}", raw, mime))
            except Exception:
                pass
        break  # only process the last user message
    return results


_TURN_MARKER = re.compile(r"(?m)^(?=(?:Human|Assistant|Tool result)\b|\[System\])")


def _call_objects(calls) -> list:
    """Calls as the model is told to write them: ``arguments`` is an object, not an
    escaped JSON string."""
    rendered = []
    for call in calls:
        function = call.get("function", {})
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass  # keep malformed arguments verbatim rather than hide them
        rendered.append({"id": call.get("id"), "type": "function",
                         "function": {"name": function.get("name"), "arguments": arguments}})
    return rendered


def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def format_messages(messages: List[Dict], envelope: bool = False) -> str:
    """
    Convert OpenAI-style messages list → single prompt string.
    Supports roles: system / user / assistant.
    content may be a string or a list of content blocks.

    ``envelope=True`` is for tool-using runs: each assistant turn is written as the
    ``{"content": ..., "tool_calls": [...]}`` object the model must answer with.
    """
    parts = []
    names = {}  # tool_call_id -> function name, so a result says which tool produced it

    for msg in messages:
        role = msg.get("role", "user").lower()
        content = _content_to_text(msg.get("content", ""))

        if role in ("system", "developer"):
            parts.append(f"[System]: {content}")
        elif role == "user":
            parts.append(f"Human: {content}")
        elif role == "assistant":
            calls = msg.get("tool_calls") or []
            names.update({c.get("id"): c.get("function", {}).get("name", "") for c in calls})
            if envelope:
                parts.append("Assistant: " + _compact({"content": content or None, "tool_calls": _call_objects(calls)}))
            else:
                parts.append(f"Assistant: {content}")
                if calls:
                    parts.append("Assistant function calls: " + _compact(_call_objects(calls)))
        elif role in ("tool", "function"):
            call_id = str(msg.get("tool_call_id", msg.get("name", "")))
            tool = names.get(call_id)
            # Tool output is untrusted: a line that looks like a turn marker must not be
            # able to forge a conversation turn in this flat transcript.
            parts.append("Tool result " + (f"({tool}) " if tool else "") + call_id + ": " + _TURN_MARKER.sub("| ", content))

    # Append a blank "Assistant:" cue so the model knows it must continue
    parts.append("Assistant:")
    return "\n\n".join(parts)


def extract_full_from_sse(lines: list[str]) -> dict:
    """
    Parse SSE lines and return answer text plus reasoning, citations and usage.

    Returns a dict with keys: answer, reasoning, citations, searches, usage,
    title, status, finished, upstream_error, files, conversation_mode,
    model_type.
    """
    from .sse_parser import parse_lines

    p = parse_lines(lines)
    return {
        "answer":     p.answer,
        "reasoning":  p.reasoning,
        "citations":  p.citations(),
        "searches":   p.searches,
        "usage":      p.token_usage,
        "title":      p.title,
        "status":     p.status,
        "finished":   p.finished,
        # Set when DeepSeek refused in-band (rate limit, filter) — an empty
        # answer with this set is a failure, not a blank reply.
        "upstream_error": p.upstream_error,
        # id of the assistant message just created; becomes the next turn's
        # parent_message_id for a caller-owned conversation.
        "response_message_id": p.response_message_id,
        # New in HAR 2026-10-01: file attachment status (update_file events),
        # SEARCH vs DEFAULT conversation mode, and the model that served us.
        "files": p.files,
        "conversation_mode": p.conversation_mode,
        "model_type": p.model_type,
    }
