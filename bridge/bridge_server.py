"""
DeepSeek Web Bridge Server
--------------------------
Exposes an OpenAI-compatible REST API on localhost so that any chatbot
that supports the OpenAI format can use DeepSeek's web interface for free.

Compatible endpoints:
  GET  /v1/models                        → list available models
  POST /v1/chat/completions              → chat (streaming or non-streaming)
  POST /v1/responses                     → OpenAI Responses API (Codex-style clients)
  POST /v1/messages                      → Anthropic Messages API (Claude Code)

Session management endpoints:
  GET  /v1/sessions                      → list all chat sessions
  POST /v1/sessions                      → create a new session, returns session_id
  GET  /v1/sessions/<id>                 → get session history (all messages)
  PATCH /v1/sessions/<id>               → rename or pin/unpin a session
  DELETE /v1/sessions/<id>              → delete a session
  DELETE /v1/sessions                   → delete ALL sessions (dangerous!)

Chat operations:
  POST /v1/sessions/<id>/stop           → stop an in-progress stream
  POST /v1/sessions/<id>/regenerate     → regenerate last assistant response
  POST /v1/sessions/<id>/continue       → continue a truncated response
  POST /v1/sessions/<id>/feedback       → submit like/dislike on a message
  POST /v1/sessions/<id>/edit           → edit a user message + regenerate

Share management:
  GET  /v1/shares                        → list all shares
  POST /v1/shares                        → create a share link
  DELETE /v1/shares/<share_id>          → delete a share link

User & export:
  GET  /v1/user                          → get current user profile
  GET  /v1/user/settings                 → get user settings (training_allowed)
  POST /v1/export                        → trigger full chat history export
  GET  /v1/export/download               → poll export download URL

Usage:
  python bridge_server.py

The server defaults to port 11434 (Ollama-compatible) but can be changed
via the BRIDGE_PORT environment variable.
"""

import json
import secrets
import time
import uuid
from typing import Generator

from flask import Flask, request, Response, jsonify
try:
    from flask_cors import CORS
except ImportError:  # CORS is a convenience, not a requirement
    CORS = None

from . import config
from .deepseek_client import DeepSeekClient, DeepSeekUpstreamError
from .formatter import extract_full_from_sse
from .sse_parser import DeepSeekStreamParser
from . import api_keys, security
from .agent import AgentRuntime, HarnessError
from .providers import configured_provider
from .diagnostics import safe_message, configure_logging

# ── Configuration (from bridge/config.py + .env) ──────────────────────────────

EMAIL    = config.DS_EMAIL
PASSWORD = config.DS_PASSWORD
PORT     = config.BRIDGE_PORT

# Single-model truth (verified 2026-10-01 via GET /api/v0/client/settings
# scope=model + Playwright): the server enables ONLY model_type "default"
# ("Instant", is_default, enabled, switchable).  "expert" and "vision" are
# present in model_configs but enabled:false / switchable:false, and the web
# UI no longer offers a model picker — search ("search_enabled") and
# reasoning ("thinking_enabled") are independent per-message toggles on the
# single Instant model (plus an "Expert Mode" upgrade banner that is still
# disabled).  Every OpenAI-style model name therefore resolves to "default"
# (the provider always sends model_type "default"), and search, thinking and
# file upload combine freely (HAR: think T/F × search T/F × files all observed;
# file_feature.conflict_with_search=false).

# ── App & client ──────────────────────────────────────────────────────────────

app    = Flask(__name__)
configure_logging()

# Scope CORS to an explicit allow-list instead of reflecting whatever Origin the
# request carries.  A bare CORS(app) reflected ANY origin while allowing the
# Authorization header, so any web page the user visited could read /v1/models
# and — given a key — spend the account's DeepSeek quota.  Set
# BRIDGE_ALLOWED_ORIGINS in .env to a comma-separated list to widen it.
_ALLOWED_ORIGINS = config.BRIDGE_ALLOWED_ORIGINS
if CORS is not None:
    CORS(app,
         resources={r"/v1/*": {"origins": _ALLOWED_ORIGINS}},
         supports_credentials=False)
else:
    print("[API] flask_cors not installed — browser clients cannot call /v1 "
          "cross-origin. Native/server clients are unaffected. "
          "Install it with: pip install flask-cors")

app.config["MAX_CONTENT_LENGTH"] = config.BRIDGE_MAX_BODY_MB * 1024 * 1024
_auth_throttle = security.FailureThrottle(limit=config.BRIDGE_AUTH_FAILURE_LIMIT)

client = DeepSeekClient(EMAIL, PASSWORD)
from .state import SessionStore
runtime = AgentRuntime(configured_provider(client),
                       store=SessionStore(config.STATE_PATH) if config.PERSISTENCE else None)


# ── Unhandled exceptions → OpenAI-shaped JSON ─────────────────────────────────
#
# Flask's default is an HTML traceback with a bare 500, which every OpenAI
# client fails to parse — the caller sees "unexpected response" instead of the
# reason.  Any exception that escapes a route (an upstream HTTP error, a bad
# session id, a bug) is converted here instead.  A DeepSeekUpstreamError keeps
# its real status: a 422 from the upstream is the caller's problem (bad
# session_id), not a bridge fault, and telling them 500 sends them chasing the
# wrong thing.

@app.errorhandler(Exception)
def _unhandled(exc):
    from werkzeug.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        # Real HTTP errors Flask raised itself (404 for an unknown route, 405 …)
        return _openai_error(exc.description or exc.name,
                             "invalid_request_error", exc.code or 500)
    if isinstance(exc, HarnessError):
        return _openai_error(safe_message(exc), "invalid_request_error" if exc.status < 500 else "server_error",
                             exc.status, code=exc.code)
    if isinstance(exc, DeepSeekUpstreamError):
        app.logger.warning("Upstream request failed (%s)", type(exc).__name__)
        # 4xx means the caller's request was bad (a nonexistent session_id, a
        # stale parent); 5xx means DeepSeek's side failed.
        kind = "invalid_request_error" if exc.status_code < 500 else "server_error"
        return _openai_error(safe_message(exc), kind, exc.status_code,
                             code="upstream_error")
    app.logger.error("Unhandled request failure (%s)", type(exc).__name__)
    return _openai_error("Internal request failure",
                         "server_error", 500, code="internal_error")


# ── Optional API key authentication ───────────────────────────────────────────

@app.before_request
def _check_api_key():
    """Enforce API key authentication. Health endpoints are always public.

    Priority:
      1. If named API keys exist (api_keys.json), validate against them.
      2. Else if BRIDGE_API_KEY is set, validate against it.
      3. Else allow unauthenticated access.

    CORS preflight (OPTIONS) is exempt — browsers never attach an
    Authorization header to a preflight, so requiring it breaks every
    browser-based client outright.
    """
    # DNS rebinding: while bound to loopback, only loopback names may address the server.
    if not security.host_allowed(request.host, config.BRIDGE_HOST, config.BRIDGE_ALLOWED_HOSTS):
        return _openai_error("Host header not allowed for this server.", "invalid_request_error", 403,
                             code="host_not_allowed")

    # Health endpoints are always public
    if request.path in ("/", "/health", "/v1/health"):
        return None

    # Other websites: a browser attaches Origin to cross-site requests; native clients send none.
    if not security.origin_allowed(request.headers.get("Origin", ""), request.host, _ALLOWED_ORIGINS):
        return _openai_error("Cross-origin requests from this origin are not allowed. Add it to "
                             "BRIDGE_ALLOWED_ORIGINS if it is your own front-end.",
                             "invalid_request_error", 403, code="origin_not_allowed")

    # CORS preflight must not require auth
    if request.method == "OPTIONS":
        return None

    who = request.remote_addr or "?"
    locked = _auth_throttle.blocked(who)
    if locked:
        response, status = _openai_error("Too many wrong API keys; try again shortly.", "invalid_request_error",
                                         429, code="auth_throttled")
        response.headers["Retry-After"] = str(int(locked) + 1)
        return response, status

    # Check if we have named API keys
    has_named_keys = api_keys.key_exists()

    if has_named_keys:
        provided = _presented_key()
        if not provided or not api_keys.validate_key(provided):
            _auth_throttle.fail(who)
            return _openai_error(
                "Provide a valid API key via the Authorization: Bearer header "
                "or the X-API-Key header. Use /api-show in the terminal to see "
                "your keys.",
                "invalid_request_error", 401, code="invalid_api_key")
        _auth_throttle.succeed(who)
        return None

    # Legacy single-key auth
    if config.BRIDGE_API_KEY:
        provided = _presented_key()
        if not provided or not secrets.compare_digest(provided.encode(), config.BRIDGE_API_KEY.encode()):
            _auth_throttle.fail(who)
            return _openai_error(
                "Provide a valid API key via the Authorization: Bearer header "
                "or the X-API-Key header.",
                "invalid_request_error", 401, code="invalid_api_key")
        _auth_throttle.succeed(who)
        return None

    # REQUIRE_API_KEY is a hard startup gate: the operator asked for auth, but
    # no key exists to validate against.  Serving open would silently ignore
    # that setting — refuse instead, so the misconfiguration is visible.
    if config.REQUIRE_API_KEY:
        return _openai_error(
            "Server is configured to require an API key, but none are "
            "configured. Create one with /api in the terminal, or set "
            "BRIDGE_API_KEY.",
            "invalid_request_error", 503, code="api_key_required")

    # No key anywhere. Fine on loopback; on a network-facing bind it would hand a file-editing,
    # command-running agent to anyone who can reach the port, so refuse unless explicitly allowed.
    if not security.is_loopback(config.BRIDGE_HOST) and not config.BRIDGE_ALLOW_OPEN_NETWORK:
        return _openai_error(
            "This server is reachable from the network but has no API key, so it refuses requests. Create "
            "a key (/api in the terminal), set BRIDGE_API_KEY, bind BRIDGE_HOST=127.0.0.1, or set "
            "BRIDGE_ALLOW_OPEN_NETWORK=true if you really want it open.",
            "invalid_request_error", 503, code="open_network_refused")

    return None  # auth disabled


@app.after_request
def _security_headers(response):
    for name, value in security.SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if request.path.startswith("/v1/") or request.path in ("/health", "/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _presented_key() -> str:
    """Extract the caller-supplied key from either supported header."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def _openai_error(message: str, err_type: str = "invalid_request_error",
                  status: int = 400, code: str | None = None) -> tuple:
    """Build an OpenAI-shaped error response.

    OpenAI SDKs parse `error.message`; a bare {"error": "..."} string (what this
    server used to return) surfaces as an unhelpful parse failure client-side.
    """
    if request.path.startswith("/v1/messages"):
        from .anthropic_api import error_body, error_kind
        return jsonify(error_body(message, error_kind(status))), status
    body = {"error": {"message": message, "type": err_type,
                      "param": None, "code": code or err_type}}
    return jsonify(body), status


def _sse_to_openai_stream(sse_iter, model: str,
                          include_usage: bool = False,
                          completion_id: str | None = None,
                          on_finish=None) -> Generator:
    """
    Convert a raw DeepSeek SSE iterator into an OpenAI-compatible SSE stream.

    Uses bridge.sse_parser so THINK (reasoning) and SEARCH fragments never leak
    into `content`.  This matters: DeepSeek streams the answer as a separate
    `response/fragments` APPEND once it stops reasoning, and a naive parser
    returns the reasoning while dropping the answer.

    OpenAI clients read reasoning from `delta.reasoning_content`, so we route
    THINK fragments there.

    The completion `id` is generated ONCE per response, not per chunk —
    clients that key state off `id` require it to be stable across the stream.
    """
    completion_id = completion_id or f"chatcmpl-{uuid.uuid4().hex}"
    parser = DeepSeekStreamParser()
    created = int(time.time())

    # Let the caller record which message was just created, so a caller-owned
    # conversation can chain its next turn without touching shared state.
    def _report() -> None:
        if on_finish is not None:
            try:
                on_finish(parser)
            except Exception as exc:
                app.logger.warning("Completion callback failed (%s)", type(exc).__name__)

    def chunk(delta: dict, finish: str | None = None) -> str:
        return "data: " + json.dumps({
            "id":      completion_id,
            "object":  "chat.completion.chunk",
            "created": created,
            "model":   model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }) + "\n\n"

    sent_role     = False
    sent_reasoning = 0   # chars of reasoning already emitted
    sent_answer   = 0   # chars of answer already emitted
    stream_error: str | None = None

    # The generator is fed to Flask's Response, so by the time anything here
    # runs the 200 status and headers are already on the wire — an exception
    # escaping this frame cannot become a status code, it just tears the
    # connection mid-stream and the client sees a truncated body with no
    # explanation.  Catch everything and end with an error chunk instead.
    try:
        for line in sse_iter:
            parser.feed_line(line)
            if parser.upstream_error:
                stream_error = "DeepSeek refused the completion"
                break

            if not sent_role and (parser.has_content or parser.reasoning):
                yield chunk({"role": "assistant", "content": ""})
                sent_role = True

            reasoning = parser.reasoning
            if len(reasoning) > sent_reasoning:
                if sent_reasoning == 0:
                    yield chunk({"reasoning_content": ""})
                yield chunk({"reasoning_content": reasoning[sent_reasoning:]})
                sent_reasoning = len(reasoning)

            answer = parser.answer
            if len(answer) > sent_answer:
                yield chunk({"content": answer[sent_answer:]})
                sent_answer = len(answer)

            if parser.status == "FINISHED" and parser.finished:
                break
    except Exception as exc:
        stream_error = safe_message(exc)
        app.logger.warning("Legacy stream failed (%s)", type(exc).__name__)

    if not sent_role:
        yield chunk({"role": "assistant", "content": ""})

    # The upstream died part-way through.  Say so, rather than emitting a
    # "stop" that tells the client the (truncated) answer is complete.
    if stream_error is not None:
        _report()
        yield "data: " + json.dumps({
            "id":      completion_id,
            "object":  "error",
            "created": created,
            "model":   model,
            "error": {
                "message": stream_error,
                "type":    "server_error",
                "code":    "upstream_error",
            },
        }) + "\n\n"
        yield "data: [DONE]\n\n"
        return

    # A stream that ended without DeepSeek reporting FINISHED was cut short —
    # by its 60s server timeout, a dropped connection, or an error frame.
    # Reporting finish_reason "stop" there tells the client the answer is
    # complete when it is silently truncated, so surface "length" instead.
    complete = parser.finished or parser.status == "FINISHED"

    # An empty stream is not a valid completion. The HTTP status was already
    # committed to 200 by the time we can see this, so the only signal left is
    # a terminal error chunk — a truncated-content masquerading as an empty
    # answer is the one failure clients handle worst.
    if not sent_answer and not sent_reasoning:
        err = parser.upstream_error
        detail = f": {err}" if err else ""
        _report()
        yield "data: " + json.dumps({
            "id":      completion_id,
            "object":  "error",
            "created": created,
            "model":   model,
            "error": {
                "message": f"DeepSeek returned no content{detail}",
                "type":    "server_error",
                "code":    "upstream_empty_response",
            },
        }) + "\n\n"
        yield "data: [DONE]\n\n"
        return

    _report()

    if include_usage:
        yield "data: " + json.dumps({
            "id":      completion_id,
            "object":  "chat.completion.chunk",
            "created": created,
            "model":   model,
            "choices": [],
            "usage": {
                "prompt_tokens":     0,
                "completion_tokens": parser.token_usage or sent_answer,
                "total_tokens":      parser.token_usage or sent_answer,
            },
        }) + "\n\n"

    yield chunk({}, finish="stop" if complete else "length")
    yield "data: [DONE]\n\n"


def _completion_json(model_name: str, parsed: dict) -> dict:
    """Build an OpenAI non-streaming completion object from a parsed stream."""
    completion_tokens = parsed.get("usage") or 0
    msg = {"role": "assistant", "content": parsed.get("answer", "")}
    if parsed.get("reasoning"):
        msg["reasoning_content"] = parsed["reasoning"]
    # "length" when DeepSeek never reported FINISHED — the answer above is
    # truncated, and the caller deserves to know rather than assume it isn't.
    finish = "stop" if parsed.get("finished") else "length"
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": finish,
            }
        ],
        "usage": {
            "prompt_tokens":     0,
            "completion_tokens": completion_tokens,
            "total_tokens":      completion_tokens,
        },
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/v1/models")
def list_models():
    # Only one model is enabled server-side (see the single-model note above).  Listing
    # just deepseek-chat reflects the actual web UI (no model picker).
    # One list that both OpenAI clients (object/owned_by) and Anthropic clients
    # (type/display_name/has_more) can read.
    if config.PROVIDER == "openai":
        models = [{"id": config.MODEL, "object": "model", "owned_by": "configured-provider"}]
    else:
        models = [
            {
                "id": "deepseek-chat",
                "object": "model",
                "created": 1700000000,
                "owned_by": "deepseek-web-bridge",
                "description": "Instant — search, file upload and deep think are independent toggles",
            },
        ]
    for model in models:
        model.setdefault("type", "model")
        model.setdefault("display_name", model["id"])
        model.setdefault("created_at", "2023-11-14T22:13:20Z")
    return jsonify({"object": "list", "data": models, "has_more": False,
                    "first_id": models[0]["id"], "last_id": models[-1]["id"]})


def _json_object(default=None):
    """Request body as a JSON object; anything else is the caller's error (400)."""
    body = request.get_json(force=True, silent=True)
    if body is None:
        if default is not None and not request.get_data():
            return default  # an empty body means "no fields"; malformed JSON still fails
        raise HarnessError("Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HarnessError("Request body must be a JSON object")
    return body


def _integer(value, name, minimum=None):
    """Strict integer input; bool and non-numeric strings are rejected, not coerced."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise HarnessError(f"{name} must be an integer")
    try:
        number = int(value)
    except ValueError:
        raise HarnessError(f"{name} must be an integer") from None
    if minimum is not None and number < minimum:
        raise HarnessError(f"{name} must be at least {minimum}")
    return number


def _owner():
    import hashlib
    # Namespace sessions by authenticated caller; terminal has its own owner.
    return "api:" + hashlib.sha256(_presented_key().encode()).hexdigest()


def _runtime():
    return runtime


from .anthropic_api import register as _register_anthropic
_register_anthropic(app, _runtime, _owner, safe_message, HarnessError)

from .responses_api import register as _register_responses
_register_responses(app, _runtime, _owner, safe_message, HarnessError)


def _native_response(execute=False):
    body = request.get_json(force=True)
    if isinstance(body, dict) and config.PROVIDER == "deepseek-web" and not execute:
        body.setdefault("search_enabled", True)
        body.setdefault("thinking_enabled", body.get("reasoning_effort") in ("medium", "high"))
    session, control, events = _runtime().prepare(body, _owner(), execute=execute)
    completion_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    model = body.get("model", config.MODEL)

    def chunk(delta=None, finish=None, usage=None):
        obj = {"id": completion_id, "object": "chat.completion.chunk", "created": created,
               "model": model, "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
        if usage is not None:
            obj.update(choices=[], usage=usage)
        return "data: " + json.dumps(obj) + "\n\n"

    def stream():
        try:
            yield chunk({"role": "assistant", "content": ""})
            for event in events:
                kind = event["type"]
                if kind == "delta":
                    yield chunk(event["delta"])
                elif kind in ("tool_start", "tool_result", "verification_required", "continuation_required", "completion_check",
                              "provider_retry", "protocol_correction", "auto_compact"):
                    # Agent-only events are separate from OpenAI content deltas.
                    yield "event: " + kind + "\ndata: " + json.dumps(event) + "\n\n"
                elif kind == "response":
                    if execute and event.get("task_status"):
                        yield "event: task_status\ndata: " + json.dumps({"status": event["task_status"]}) + "\n\n"
                    calls = event["message"].get("tool_calls", [])
                    if calls:
                        yield chunk({"tool_calls": [dict(c, index=i) for i, c in enumerate(calls)]})
                    yield chunk(finish=event["finish_reason"])
                    if (body.get("stream_options") or {}).get("include_usage"):
                        yield chunk(usage=event.get("usage") or {})
            yield "data: [DONE]\n\n"
        except Exception as exc:
            if isinstance(exc, HarnessError):
                error = {"message": safe_message(exc), "code": exc.code, "type": "server_error"}
            else:
                error = {"message": "Completion failed", "code": "internal_error", "type": "server_error"}
            yield "data: " + json.dumps({"object": "error", "error": error}) + "\n\n"
            yield "data: [DONE]\n\n"
        finally:
            events.close()
            _runtime().release(session, control)

    if body.get("stream", False):
        response = Response(stream(), content_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Session-ID": session.id})
        # Also release if the WSGI server closes before starting iteration.
        response.call_on_close(lambda: _runtime().release(session, control))
        return response
    final, tool_events = None, []
    try:
        for event in events:
            if event["type"] == "response":
                final = event
            elif event["type"] in ("tool_start", "tool_result"):
                tool_events.append(event)
    finally:
        events.close()
        _runtime().release(session, control)
    if final is None:
        raise HarnessError("No completion produced", "provider_error", 502)
    result = {"id": completion_id, "object": "chat.completion", "created": created, "model": model,
              "session_id": session.id,
              "choices": [{"index": 0, "message": final["message"], "finish_reason": final["finish_reason"]}],
              "usage": final.get("usage") or {}}
    if execute:
        result["tool_events"] = tool_events
        result["task_status"] = final.get("task_status", "completed")
    return jsonify(result)


@app.post("/v1/chat/completions")
def chat_completions():
    return _native_response(execute=False)


@app.post("/v1/agent/runs")
def agent_runs():
    return _native_response(execute=True)


@app.get("/v1/tools")
def native_tools():
    return jsonify({"tools": _runtime().registry.definitions})


@app.get("/v1/skills")
def native_skills():
    library = _runtime().registry.skills
    return jsonify({"skills": library.catalog(), "invalid": library.errors})


@app.get("/v1/mcp")
def native_mcp_status():
    hub = _runtime().registry.mcp
    return jsonify({"transport": "stdio", "servers": list(hub.clients), "tools": [
        {"name": name, "server": server, "remote_name": tool, "read_only": name in hub.read_only}
        for name, (server, tool, _) in hub.bindings.items()]})


@app.get("/v1/skills/<name>")
def native_skill_resource(name):
    try:
        content = _runtime().registry.skills.resource(name, request.args.get("resource", "SKILL.md"))
    except ValueError as exc:
        raise HarnessError(str(exc)) from exc
    return jsonify({"name": name, "content": content})


def _session_action(session_id, action):
    session = _runtime().get(session_id, _owner())
    if not session.lock.acquire(blocking=False):
        raise HarnessError("Session is busy", "session_busy", 409)
    try:
        return jsonify(action(session))
    finally:
        session.lock.release()


@app.post("/v1/sessions/<session_id>/fork")
def fork_native_session(session_id):
    return _session_action(session_id, lambda s: {"session_id": _runtime().fork(s).id})


@app.get("/v1/sessions/<session_id>/export")
def export_native_session(session_id):
    def snapshot(session):
        data = _runtime().snapshot(session, private=False)
        data.pop("owner", None)
        data["format"] = "native-harness-session-v1"
        return data
    return _session_action(session_id, snapshot)


@app.post("/v1/sessions/<session_id>/compact")
def compact_native_session(session_id):
    """Body (optional): {"focus": "what the summary must preserve"}. Returns what was folded and how."""
    body = request.get_json(silent=True)
    if body is not None and not isinstance(body, dict):
        raise HarnessError("Request body must be an object")
    focus = (body or {}).get("focus", "")
    if not isinstance(focus, str):
        raise HarnessError("focus must be a string")
    return _session_action(session_id, lambda session: _runtime().compact(session, focus[:500]))


@app.get("/v1/sessions/<session_id>/context")
def native_context(session_id):
    """Where this session's context budget goes (characters) and the auto-compact threshold."""
    return jsonify(_runtime().context_report(_runtime().get(session_id, _owner())))


@app.get("/v1/sessions/<session_id>/checkpoints")
def native_checkpoints(session_id):
    return _session_action(session_id, lambda s: {"checkpoints": [
        {"index": i, "path": c["path"]} for i, c in enumerate(s.checkpoints)]})


@app.post("/v1/sessions/<session_id>/undo")
def undo_native_checkpoint(session_id):
    return _session_action(session_id, _runtime().undo)


@app.get("/v1/sessions/<session_id>/review")
def review_native_changes(session_id):
    return _session_action(session_id, _runtime().review)


@app.get("/v1/sessions/<session_id>/verification")
def native_verification(session_id):
    return _session_action(session_id, lambda s: {"verification": _runtime().verification_status(s)})


# Health-check
@app.get("/")
@app.get("/health")
@app.get("/v1/health")
def health():
    """Detailed health check — also returns version and auth status."""
    auth_ok = client._token is not None
    has_named_keys = api_keys.key_exists()
    return jsonify({
        "status":      "ok",
        "service":     "deepseek-web-bridge",
        "version":     config.VERSION,
        "port":        config.BRIDGE_PORT,
        "auth":        "authenticated" if auth_ok else "unauthenticated",
        "api_key_req": has_named_keys or bool(config.BRIDGE_API_KEY),
        "auth_method": "named_keys" if has_named_keys else ("single_key" if config.BRIDGE_API_KEY else "none"),
        # REQUIRE_API_KEY=true means: refuse to serve until a key exists.
        "api_key_required": config.REQUIRE_API_KEY,
        "api_key_ok": (not config.REQUIRE_API_KEY) or has_named_keys or bool(config.BRIDGE_API_KEY),
        "models": [config.MODEL],
        "provider": config.PROVIDER,
        "native_agent": True,
        "thinking": "reasoning_content",
    })


# ══════════════════════════════════════════════════════════════════════════════
# Session Management Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/v1/sessions")
def list_sessions():
    """List all chat sessions (paginated)."""
    if request.args.get("backend") != "web":
        data = [dict(s, backend="native") for s in runtime.list(_owner())]
        return jsonify({"object": "list", "data": data})
    count  = _integer(request.args.get("count", 50), "count", 1)
    pinned = request.args.get("pinned", "false").lower() == "true"
    try:
        sessions = client.list_sessions(count=count, pinned=pinned)
        return jsonify({"object": "list", "data": sessions})
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.post("/v1/sessions")
def create_session():
    """Create a new empty chat session."""
    if request.args.get("backend") != "web":
        body = request.get_json(silent=True)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise HarnessError("Session configuration must be an object")
        session = runtime.create(_owner(), body)
        return jsonify({"session_id": session.id, "backend": "native"}), 201
    try:
        client.ensure_authenticated()
        sid = client.create_session()
        return jsonify({"session_id": sid}), 201
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.get("/v1/sessions/<session_id>")
def get_session_history(session_id: str):
    """Return full message history for a session including search results and thinking."""
    is_native = runtime.contains(session_id)
    if is_native:
        session = runtime.get(session_id, _owner())
        if not session.lock.acquire(blocking=False):
            raise HarnessError("Session is busy", "session_busy", 409)
        try:
            return jsonify({"session_id": session.id, "messages": session.messages, "mode": session.mode,
                            "skills": session.skills, "plan": session.plan, "context": session.context, "cwd": session.cwd,
                            "verification": runtime.verification_status(session)})
        finally:
            session.lock.release()
    try:
        data = client.get_history(session_id)
        return jsonify(data)
    except DeepSeekUpstreamError as exc:
        if exc.status_code < 500:
            # Neither a native session nor a remote chat: say so plainly.
            return _openai_error("Session not found", "invalid_request_error", 404, code="session_not_found")
        return _openai_error(safe_message(exc), "server_error", 502, code="upstream_error")
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.patch("/v1/sessions/<session_id>")
def update_session(session_id: str):
    """Rename or pin/unpin a session.
    Body: {"title": "...", "pinned": true/false}
    """
    body = _json_object({})
    is_native = runtime.contains(session_id)
    if is_native:
        session = runtime.get(session_id, _owner())
        if not isinstance(body, dict) or ("title" in body and not isinstance(body["title"], str)) or ("pinned" in body and not isinstance(body["pinned"], bool)):
            raise HarnessError("Invalid session metadata")
        if not session.lock.acquire(blocking=False):
            raise HarnessError("Session is busy", "session_busy", 409)
        try:
            runtime.configure(session, body)
            session.title = body.get("title", session.title)
            session.pinned = body.get("pinned", session.pinned)
            runtime.save(session)
            return jsonify({"status": "updated"})
        finally:
            session.lock.release()
    try:
        if "title" in body:
            client.rename_session(session_id, body["title"])
        if "pinned" in body:
            client.pin_session(session_id, bool(body["pinned"]))
        return jsonify({"status": "updated"})
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.delete("/v1/sessions/<session_id>")
def delete_session(session_id: str):
    """Permanently delete a single chat session."""
    is_native = runtime.contains(session_id)
    if is_native:
        session = runtime.get(session_id, _owner())
        if not session.lock.acquire(blocking=False):
            raise HarnessError("Cancel the active run before deleting", "session_busy", 409)
        try:
            runtime.delete(session)
            return jsonify({"status": "deleted"})
        finally:
            session.lock.release()
    try:
        client.delete_session(session_id)
        return jsonify({"status": "deleted"})
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.delete("/v1/sessions")
def delete_all_sessions():
    """Permanently delete ALL chat sessions. Use with caution."""
    if request.args.get("backend") != "web":
        entries = runtime.list(_owner())
        acquired = []
        try:
            for entry in entries:
                session = runtime.get(entry["id"], _owner())
                if not session.lock.acquire(blocking=False):
                    raise HarnessError("Cancel active runs before deleting", "session_busy", 409)
                acquired.append(session)
            for session in acquired:
                runtime.delete(session)
        finally:
            for session in acquired:
                session.lock.release()
        return jsonify({"status": "all_deleted"})
    try:
        client.delete_all_sessions()
        return jsonify({"status": "all_deleted"})
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


# ══════════════════════════════════════════════════════════════════════════════
# Chat Operation Routes (stop / regenerate / continue / feedback)
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/v1/sessions/<session_id>/stop")
def stop_stream_route(session_id: str):
    """Stop an in-progress streaming completion.
    Body: {"message_id": <int>}
    """
    is_native = runtime.contains(session_id)
    if is_native:
        return jsonify({"status": "cancel_requested" if runtime.cancel(session_id, _owner()) else "idle"})
    body = _json_object({})
    message_id = _integer(body.get("message_id", 0), "message_id")
    try:
        client.stop_stream(session_id, message_id)
        return jsonify({"status": "stopped"})
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.post("/v1/sessions/<session_id>/regenerate")
def regenerate_route(session_id: str):
    """
    Regenerate the last assistant message. Returns SSE stream.
    Body: {"child_message_id": <int>, "thinking_enabled": bool, "search_enabled": bool,
           "user_options": "verbosity_high"|"verbosity_low"|"search"|null, "stream": bool}
    """
    body          = _json_object({})
    child_id      = body.get("child_message_id")
    thinking      = bool(body.get("thinking_enabled", False))
    search        = bool(body.get("search_enabled", True))
    user_options  = body.get("user_options")
    streaming     = bool(body.get("stream", True))

    if child_id is None:
        return _openai_error("child_message_id is required", "invalid_request_error", 400)

    model_name = body.get("model", "deepseek-chat")

    sse_iter = client.regenerate_stream(
        session_id, _integer(child_id, "child_message_id"), thinking, search, user_options
    )
    if streaming:
        return Response(_sse_to_openai_stream(sse_iter, model_name),
                        content_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    lines = list(sse_iter)
    parsed = extract_full_from_sse(lines)
    return jsonify(_completion_json(model_name, parsed))


@app.post("/v1/sessions/<session_id>/continue")
def continue_route(session_id: str):
    """
    Continue a truncated assistant response. Returns SSE stream.
    Body: {"message_id": <int>, "stream": bool}
    """
    body       = _json_object({})
    message_id = body.get("message_id")
    streaming  = bool(body.get("stream", True))
    model_name = body.get("model", "deepseek-chat")

    if message_id is None:
        return _openai_error("message_id is required", "invalid_request_error", 400)

    sse_iter = client.continue_stream(session_id, _integer(message_id, "message_id"))
    if streaming:
        return Response(_sse_to_openai_stream(sse_iter, model_name),
                        content_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    lines = list(sse_iter)
    parsed = extract_full_from_sse(lines)
    return jsonify(_completion_json(model_name, parsed))


@app.post("/v1/sessions/<session_id>/feedback")
def feedback_route(session_id: str):
    """
    Submit feedback on an assistant message.
    Body: {"message_id": <int>, "feedback_type": "LIKE"|"DISLIKE", "description": str}
    """
    body          = _json_object({})
    message_id    = body.get("message_id")
    feedback_type = body.get("feedback_type", "LIKE").upper()
    description   = body.get("description")

    if message_id is None:
        return _openai_error("message_id is required", "invalid_request_error", 400)
    if feedback_type not in ("LIKE", "DISLIKE"):
        return _openai_error("feedback_type must be LIKE or DISLIKE", "invalid_request_error", 400)

    message_id = _integer(message_id, "message_id")
    try:
        client.message_feedback(session_id, message_id, feedback_type, description)
        return jsonify({"status": "ok"})
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.post("/v1/sessions/<session_id>/edit")
def edit_route(session_id: str):
    """
    Edit a previous user message and re-generate the assistant response.
    Body: {"message_id": <int>, "prompt": str, "thinking_enabled": bool,
           "search_enabled": bool, "stream": bool, "model": str}
    Upstream: POST /api/v0/chat/edit_message — client method existed but had
    no bridge route (dead code). Not exercised in HAR 2026-10-01 (no edit in
    the 6-min window) so treated as unverified-but-likely-valid.
    """
    body      = _json_object({})
    message_id = body.get("message_id")
    prompt     = body.get("prompt", "")
    thinking   = bool(body.get("thinking_enabled", False))
    search     = bool(body.get("search_enabled", True))
    streaming  = bool(body.get("stream", True))
    model_name = body.get("model", "deepseek-chat")

    if message_id is None:
        return _openai_error("message_id is required", "invalid_request_error", 400)
    if not prompt:
        return _openai_error("prompt is required", "invalid_request_error", 400)

    sse_iter = client.edit_message_stream(
        session_id, _integer(message_id, "message_id"), prompt, thinking, search
    )
    if streaming:
        return Response(_sse_to_openai_stream(sse_iter, model_name),
                        content_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    lines = list(sse_iter)
    parsed = extract_full_from_sse(lines)
    return jsonify(_completion_json(model_name, parsed))


# ══════════════════════════════════════════════════════════════════════════════
# Share Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/v1/shares")
def list_shares_route():
    """List all shared conversations."""
    count = _integer(request.args.get("count", 10), "count", 1)
    try:
        shares = client.list_shares(count=count)
        return jsonify({"object": "list", "data": shares})
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.post("/v1/shares")
def create_share_route():
    """Create a share link.
    Body: {"session_id": "...", "message_ids": [1, 2, ...]}
    Returns: {"share_id": "...", "url": "https://chat.deepseek.com/a/chat/s/..."}
    """
    body        = _json_object({})
    session_id  = body.get("session_id")
    message_ids = body.get("message_ids", [])

    if not session_id:
        return _openai_error("session_id is required", "invalid_request_error", 400)

    try:
        share_id = client.create_share(session_id, message_ids)
        return jsonify({
            "share_id": share_id,
            "url":      f"https://chat.deepseek.com/a/chat/s/{share_id}",
        }), 201
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.delete("/v1/shares/<share_id>")
def delete_share_route(share_id: str):
    """Delete a share link."""
    try:
        client.delete_share(share_id)
        return jsonify({"status": "deleted"})
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


# ══════════════════════════════════════════════════════════════════════════════
# User & Export Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/v1/user")
def get_user_route():
    """Return current user profile information."""
    try:
        user = client.get_current_user()
        return jsonify(user)
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.get("/v1/user/settings")
def get_user_settings_route():
    """Return user account settings (e.g. training_allowed).

    Upstream: GET /api/v0/users/settings — seen in HAR 2026-10-01 (3 calls).
    Previously the client method existed but had no bridge route (dead code).
    """
    try:
        settings = client.get_user_settings()
        return jsonify(settings)
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.post("/v1/export")
def export_route():
    """Trigger a full chat history export. Returns export task info."""
    try:
        info = client.trigger_export()
        return jsonify({"status": "triggered", "data": info})
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.get("/v1/export/download")
def export_download_route():
    """Poll the export download URL (None while not ready).

    Upstream: GET /api/v0/download_export_history — seen in HAR returning
    biz_code=2 TASK_EXPIRED when no export is pending.  Previously the
    client method existed but had no bridge route.
    """
    try:
        url = client.get_export_download()
        return jsonify({"download_url": url, "ready": url is not None})
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


# ══════════════════════════════════════════════════════════════════════════════
# New Endpoints (from HAR analysis)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/v1/tts/voices")
def tts_voices_route():
    """Get available TTS voices from DeepSeek."""
    try:
        voices = client.get_tts_voices()
        return jsonify({"object": "list", "data": voices})
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.get("/v1/client/settings")
def client_settings_route():
    """Get client settings from DeepSeek.
    Query params: did (device_id), scope (main, model, web_upgrade, provider, banner)
    """
    did = request.args.get("did", "")
    scope = request.args.get("scope", "main")
    try:
        settings = client.get_client_settings(did=did, scope=scope)
        return jsonify(settings)
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.post("/v1/device/check")
def device_check_route():
    """Check device status with DeepSeek.
    Body: {"device_id": "...", "device_model": "..."}
    """
    body = _json_object({})
    device_id = body.get("device_id", "")
    device_model = body.get("device_model", "")
    try:
        result = client.check_device(device_id=device_id, device_model=device_model)
        return jsonify(result)
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


@app.post("/v1/client/settings/report")
def client_settings_report_route():
    """Report client settings to DeepSeek.
    Body: {"settings_ids": [...], "did": "...", "sso_id": "..."}
    """
    body = _json_object({})
    try:
        result = client.report_client_settings(
            settings_ids=body.get("settings_ids", []),
            did=body.get("did", ""),
            sso_id=body.get("sso_id", ""),
        )
        return jsonify({"status": "reported", "data": result})
    except Exception as exc:
        return _openai_error(safe_message(exc), "server_error", 500, code="upstream_error")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  DeepSeek Web Bridge")
    print(f"  Listening on http://{config.BRIDGE_HOST}:{PORT}")
    print("  Compatible with OpenAI API clients")
    print("  Model: deepseek-chat (single Instant model; expert/vision aliases map to it)")
    print("=" * 60)
    app.run(host=config.BRIDGE_HOST, port=PORT, threaded=True, debug=False)
