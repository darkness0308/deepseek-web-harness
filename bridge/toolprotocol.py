"""Tolerant decoding of the JSON tool envelope used by text-only web backends.

The DeepSeek web transport has no structured tool interface, so the model is
asked to reply with ``{"content": ..., "tool_calls": [...]}``. Models regularly
add prose around the object, put raw newlines or unescaped quotes inside file
contents, use ``\\d``-style escapes, or emit a single bare call. Rejecting all of
those cost whole turns, so this module recovers the intended envelope without
ever inventing a call: anything it cannot decode unambiguously still raises
``EnvelopeError`` and no tool runs.
"""
from __future__ import annotations

import json
import re

_FENCE = re.compile(r"```(?:json|JSON)?[ \t]*\r?\n(.*?)\r?\n?```", re.DOTALL)
_VALID_ESCAPES = '"\\/bfnrtu'
_CALL_KEYS = ("tool_calls", "tool_call", "function_call", "calls")


class EnvelopeError(ValueError):
    """The text held no decodable envelope. ``fallback_text`` is plain prose, if any."""

    def __init__(self, message, fallback_text=None):
        super().__init__(message)
        self.fallback_text = fallback_text


# ── lenient JSON ──────────────────────────────────────────────────────────────

def _closing_quote_ok(text, index, context):
    """Whether the quote at ``index`` plausibly ends a string in ``context``."""
    rest = text[index + 1:index + 200]
    if context == "key":
        return bool(re.match(r"\s*:", rest))
    if context == "object":
        return bool(re.match(r'\s*(?:}|$|,\s*"[^"\n]{1,80}"\s*:)', rest))
    if context == "array":
        return bool(re.match(r'\s*(?:]|$|,\s*(?:["{\[\-0-9]|true|false|null))', rest))
    return bool(re.match(r"\s*(?:$|[,}\]])", rest))


_CLOSER = {"{": "}", "[": "]"}


def repair_json(text, fix_structure=False):
    """Best-effort fix of common model JSON mistakes; the caller re-validates.

    Handles invalid backslash escapes, raw control characters, inner double
    quotes that were not escaped, and trailing commas. It never completes a
    truncated document.

    ``fix_structure`` also repairs bracket slips that deeply nested replies often
    contain, for a reply the model *finished* writing: a closer that arrives while
    inner brackets are still open (``]}}]}`` where ``]}}}]}`` was meant) gets the
    missing closers inserted, a closer matching nothing is dropped, and a document
    that ends right after a closer gets its remaining closers appended. No
    content is added, and a string, key or value cut off mid-way is never completed.
    """
    out, stack, i, n = [], [], 0, len(text)
    expecting_key = False
    unterminated = False  # the text ended inside a string: it was cut off, not merely sloppy
    while i < n:
        ch = text[i]
        if ch == '"':
            if stack and stack[-1] == "{":
                context = "key" if expecting_key else "object"
            elif stack and stack[-1] == "[":
                context = "array"
            else:
                context = "top"
            out.append('"')
            i += 1
            while i < n:
                c = text[i]
                if c == "\\":
                    nxt = text[i + 1] if i + 1 < n else ""
                    if nxt and nxt in _VALID_ESCAPES:
                        out.append(c + nxt)
                        i += 2
                    else:
                        out.append("\\\\")
                        i += 1
                    continue
                if c == '"':
                    if _closing_quote_ok(text, i, context):
                        break
                    out.append('\\"')
                    i += 1
                    continue
                if c == "\n":
                    out.append("\\n")
                elif c == "\r":
                    out.append("\\r")
                elif c == "\t":
                    out.append("\\t")
                elif ord(c) < 0x20:
                    out.append("\\u%04x" % ord(c))
                else:
                    out.append(c)
                i += 1
            else:
                unterminated = True
            out.append('"')
            i += 1
            if context == "key":
                expecting_key = False
            continue
        if ch in "{[":
            stack.append(ch)
            expecting_key = ch == "{"
        elif ch in "}]":
            # Drop a trailing comma left before the closing bracket.
            j = len(out) - 1
            while j >= 0 and out[j].isspace():
                j -= 1
            if j >= 0 and out[j] == ",":
                del out[j]
            if fix_structure and (not stack or stack[-1] != ("{" if ch == "}" else "[")):
                if ("{" if ch == "}" else "[") in stack:
                    while stack[-1] != ("{" if ch == "}" else "["):
                        out.append(_CLOSER[stack.pop()])  # the model forgot to close these
                else:
                    i += 1  # a closer with nothing to close
                    continue
            if stack:
                stack.pop()
            expecting_key = False
        elif ch == ",":
            expecting_key = bool(stack) and stack[-1] == "{"
        out.append(ch)
        i += 1
    if fix_structure and stack and not unterminated and text.rstrip()[-1:] in ("}", "]") and len(stack) <= 6:
        out.extend(_CLOSER[opener] for opener in reversed(stack))
    return "".join(out)


def loads_lenient(text, fix_structure=False):
    """Return ``(value, repaired)``; raises ValueError if nothing decodes."""
    reject = lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON"))
    try:
        return json.loads(text, strict=False, parse_constant=reject), False
    except ValueError:
        pass
    return json.loads(repair_json(text, fix_structure), strict=False, parse_constant=reject), True


# ── locating the envelope ─────────────────────────────────────────────────────

def _balanced_objects(text):
    """Yield ``(start, end)`` of top-level ``{...}`` spans, string aware."""
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, j, in_string, escaped = 0, i, False, False
        while j < n:
            c = text[j]
            if in_string:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_string = False
            elif c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield i, j + 1
                    break
            j += 1
        else:
            # Unbalanced: report the open tail so the repair path can try it.
            yield i, n
            return
        i = j + 1


# A final answer written as just {"content": "..."} (the model drops the empty tool_calls)
# is unambiguous; any other key besides these means it is something else.
_ANSWER_KEYS = {"content", "role", "type", "reasoning", "finish_reason"}


def _is_answer_only(value):
    return isinstance(value.get("content"), str) and set(value) <= _ANSWER_KEYS


def _looks_like_envelope(value):
    if not isinstance(value, dict):
        return False
    if any(key in value for key in _CALL_KEYS) or _is_answer_only(value):
        return True
    return isinstance(value.get("name"), str) and any(
        key in value for key in ("arguments", "parameters", "args", "input"))


def _normalize_call(raw):
    if not isinstance(raw, dict):
        raise EnvelopeError("Malformed function call")
    fn = raw.get("function") if isinstance(raw.get("function"), dict) else raw
    name = fn.get("name", fn.get("tool"))
    if not isinstance(name, str) or not name:
        raise EnvelopeError("Function call is missing its name")
    args = {}
    for key in ("arguments", "parameters", "args", "input"):
        if key in fn:
            args = fn[key]
            break
    if args is None:
        args = {}
    call = {"type": "function", "function": {"name": name, "arguments": args}}
    if isinstance(raw.get("id"), str) and raw["id"]:
        call["id"] = raw["id"]
    return call


def _envelope(value):
    """Map any accepted shape onto ``(content, [calls])``."""
    content = value.get("content")
    if content is not None and not isinstance(content, str):
        raise EnvelopeError("content must be a string or null")
    raw_calls = None
    for key in _CALL_KEYS:
        if key in value:
            raw_calls = value[key]
            break
    if raw_calls is None and _is_answer_only(value):
        return content, []
    if raw_calls is None:
        raw_calls = [value]  # a bare {"name": ..., "arguments": ...}
        content = None
    elif isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    if not isinstance(raw_calls, list):
        raise EnvelopeError("tool_calls must be an array")
    return content, [_normalize_call(call) for call in raw_calls]


# ── DeepSeek's native "DSML" call syntax ──────────────────────────────────────
#
# The web model is trained on its own function-call markup and often uses it
# instead of the requested JSON:
#   <｜｜DSML｜｜ calls>
#   <｜｜DSML｜｜ invoke name="write_file">
#   <｜｜DSML｜｜ parameter name="path" string="true">a.txt</｜｜DSML｜｜ parameter>
# String parameters are verbatim (no escaping), which makes it the most reliable
# way to carry file contents; ``string="false"`` parameters are JSON values.

_MARK = r"[｜|]{1,2}\s*DSML\s*[｜|]{1,2}\s*"
_INVOKE = re.compile(r"<" + _MARK + r"invoke\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</" + _MARK + r"invoke\s*>", re.DOTALL)
_PARAM = re.compile(r"<" + _MARK + r"parameter\s+name\s*=\s*[\"']([^\"']+)[\"'](?:\s+string\s*=\s*[\"'](true|false)[\"'])?\s*>"
                    r"(.*?)</" + _MARK + r"parameter\s*>", re.DOTALL)
_CALLS_TAG = re.compile(r"</?" + _MARK + r"(?:function_)?calls\s*>")
_ANY_DSML = re.compile(r"<" + _MARK + r"(?:(?:function_)?calls|invoke|parameter)\b")


def parse_dsml(raw):
    """Return ``(content, calls)`` for a DSML reply, ``None`` if there is none.

    Raises ``EnvelopeError`` for markup that never completes, so a truncated call
    is not executed.
    """
    invokes = list(_INVOKE.finditer(raw))
    if not invokes:
        if _ANY_DSML.search(raw):
            raise EnvelopeError("Incomplete DSML tool call")
        return None
    calls = []
    for invoke in invokes:
        args = {}
        for param in _PARAM.finditer(invoke.group(2)):
            name, flag, value = param.groups()
            if flag == "true":
                args[name] = value
                continue
            try:
                args[name] = loads_lenient(value.strip())[0]
            except (ValueError, RecursionError):
                args[name] = value if flag is None else value.strip()
        calls.append({"type": "function", "function": {"name": invoke.group(1), "arguments": args}})
    narration = _CALLS_TAG.sub("", raw[:invokes[0].start()]).strip()
    return (narration or None), calls


def parse_envelope(text, fix_structure=False):
    """Decode a model reply into ``(content, calls, repaired)``.

    ``fix_structure`` is for replies the model finished writing (see ``repair_json``).

    ``calls`` use the OpenAI shape with ``arguments`` as an object or string; the
    provider normalizes them. Prose around the JSON becomes content when the
    envelope has none. ``repaired`` is falsy for clean JSON, ``True`` when the JSON
    needed repair and ``"dsml"`` for DeepSeek's native call markup. Raises
    ``EnvelopeError`` otherwise.
    """
    raw = (text or "").strip()
    if not raw:
        raise EnvelopeError("Empty response")
    dsml = parse_dsml(raw)
    if dsml is not None:
        return dsml[0], dsml[1], "dsml"
    candidates = []
    fenced = _FENCE.findall(raw)
    if raw.startswith("{"):
        candidates.append((0, len(raw)))
    for start, end in _balanced_objects(raw):
        if (start, end) not in candidates:
            candidates.append((start, end))
    for start, end in candidates:
        snippet = raw[start:end]
        try:
            value, repaired = loads_lenient(snippet, fix_structure)
        except (ValueError, RecursionError):
            continue
        if not _looks_like_envelope(value):
            continue
        content, calls = _envelope(value)
        outside = (raw[:start] + "\n" + raw[end:]).strip()
        outside = _FENCE.sub("", outside).replace("```json", "").replace("```", "").strip()
        if outside and not re.search(r"\w", outside):
            outside = ""  # a stray closing brace or bracket is not prose
        if outside and not (content or "").strip():
            content = outside
        elif outside and calls and (content or "").strip():
            content = outside + "\n\n" + content
        if not calls and not (content or "").strip():
            raise EnvelopeError("Empty final tool envelope")
        return content, calls, repaired
    # Nothing structured: report prose that carries no JSON-looking envelope so
    # the caller may deliver it as the answer after bounded corrections.
    prose = None
    if not fenced and '"tool_calls"' not in raw and "tool_calls" not in raw[:400]:
        prose = raw
    raise EnvelopeError("No decodable tool envelope", fallback_text=prose)


# ── discarding a fabricated continuation ─────────────────────────────────────

# The flat transcript labels turns with these markers. After a complete reply the web model
# often keeps writing as if it were the transcript: an invented tool result, even an invented
# next assistant turn. None of it came from a tool or the user, so it is discarded.
_ECHO = re.compile(r"(?:Tool result\b|Assistant(?: function calls)?:|Human:|\[System\]:)")


def strip_transcript_echo(text):
    """``text`` cut at the first line (outside any JSON string) that starts with a transcript
    marker. A marker inside a string is file or answer content and is left alone."""
    in_string = escaped = False
    n = len(text)
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "\n":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if _ECHO.match(text, j):
                return text[:i].rstrip()
    return text


# ── incremental streaming of the "content" value ─────────────────────────────

class ContentStreamer:
    """Feed partial envelope text; ``feed`` returns newly decoded ``content``.

    Only the top-level ``"content"`` string is surfaced, so users see the answer
    while the model is still writing the envelope. State persists across calls,
    so total work is linear in the response size.
    """

    def __init__(self):
        self.depth = 0
        self.in_string = False
        self.escaped = False
        self.unicode = None          # pending \uXXXX digits
        self.surrogate = None
        self.key_buffer = None       # characters of the key being read (depth 1)
        self.current_key = None
        self.reading_content = False
        self.after_colon = False
        self.done_content = False
        self.started = False
        self.disabled = False        # reply is not a JSON envelope; never stream it
        self.emitted = ""

    _SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "/": "/", "\\": "\\", '"': '"'}

    def feed(self, chunk):
        out = []
        for ch in chunk:
            piece = self._step(ch)
            if piece:
                out.append(piece)
        text = "".join(out)
        self.emitted += text
        return text

    def _step(self, ch):
        if not self.started:
            if self.disabled:
                return ""
            if ch == "{":
                self.started = True
                self.depth = 1
            elif not (ch.isspace() or ch in "`jsonJSON"):
                # Prose, DSML or anything else first: its braces are not an
                # envelope, so do not mistake file contents for a "content" key.
                self.disabled = True
            return ""
        if self.in_string:
            return self._in_string(ch)
        if ch == '"':
            self.in_string = True
            self.escaped = False
            if self.depth == 1 and not self.after_colon:
                self.key_buffer = []
            elif self.depth == 1 and self.after_colon and self.current_key == "content" and not self.done_content:
                self.reading_content = True
            return ""
        if ch in "{[":
            self.depth += 1
            self.after_colon = False
        elif ch in "}]":
            self.depth -= 1
        elif ch == ":" and self.depth == 1:
            self.after_colon = True
        elif ch == "," and self.depth == 1:
            self.after_colon = False
            self.current_key = None
        elif self.depth == 1 and self.after_colon and not ch.isspace():
            self.after_colon = False  # non-string value (null, number...)
        return ""

    def _in_string(self, ch):
        capture = self.reading_content
        if self.unicode is not None:
            self.unicode += ch
            if len(self.unicode) < 4:
                return ""
            try:
                code = int(self.unicode, 16)
            except ValueError:
                code = 0xFFFD
            self.unicode = None
            return self._emit_code(code, capture)
        if self.escaped:
            self.escaped = False
            if ch == "u":
                self.unicode = ""
                return ""
            decoded = self._SIMPLE.get(ch, ch)
            if self.key_buffer is not None:
                self.key_buffer.append(decoded)
            return decoded if capture else ""
        if ch == "\\":
            self.escaped = True
            return ""
        if ch == '"':
            self.in_string = False
            if self.key_buffer is not None:
                self.current_key = "".join(self.key_buffer)
                self.key_buffer = None
            if self.reading_content:
                self.reading_content = False
                self.done_content = True
                self.after_colon = False
            return ""
        if self.key_buffer is not None:
            self.key_buffer.append(ch)
        return ch if capture else ""

    def _emit_code(self, code, capture):
        if 0xD800 <= code < 0xDC00:
            self.surrogate = code
            return ""
        if 0xDC00 <= code < 0xE000 and self.surrogate is not None:
            code = 0x10000 + ((self.surrogate - 0xD800) << 10) + (code - 0xDC00)
        self.surrogate = None
        piece = chr(code) if code <= 0x10FFFF else "�"
        if self.key_buffer is not None:
            self.key_buffer.append(piece)
        return piece if capture else ""


# ── explaining a rejected reply ───────────────────────────────────────────────

def diagnose(text):
    """``(reason, cut_off)`` for a reply that could not be decoded, so the correction sent
    back to the model says what to change. ``cut_off`` means the JSON ended before it was
    complete (the provider limits one reply to roughly 23,000 characters). Built from the
    structure only; none of the reply's text is included."""
    raw = (text or "").strip()
    in_string = escaped = False
    stack = []
    for ch in raw:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    if in_string:
        return f"the reply ended inside a text value after {len(raw)} characters, before the JSON was complete", True
    if stack:
        return f"the reply ended with {len(stack)} unclosed bracket(s) after {len(raw)} characters", True
    start = raw.find("{")
    if start < 0:
        return "the reply contained no JSON object", False
    try:
        value = json.loads(repair_json(raw[start:], fix_structure=True), strict=False)
    except json.JSONDecodeError as exc:
        if exc.msg.startswith("Extra data"):
            return ("extra text followed the end of the JSON object (was the tool_calls array closed before "
                    "its last call?)"), False
        return f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}", False
    if isinstance(value, dict) and not value.get("tool_calls") and not str(value.get("content") or "").strip():
        return "the reply was an empty envelope: no content and no tool calls", False
    return "the JSON had no content or tool_calls", False
