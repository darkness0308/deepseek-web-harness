"""
DeepSeek Web API client — talks directly to chat.deepseek.com
using the reverse-engineered web endpoints (no official API key needed).

Authentication strategy:
  1. Load the session token + WAF cookies from session.json (built by
     import_session.py from a browser export in cookies/, by the browser
     extension, or by refresh_session.py; see bridge/session_import.py).
  2. If the token is expired or missing, fall back to credential-based login
     (only works when the AWS WAF token is still valid).
  3. If the WAF token is expired too, raise a clear error telling the user
     to run refresh_session.py.
"""

import functools
import hashlib
import json
import logging
import mimetypes
import os
import threading
import time
import uuid

import requests

from . import config
from .sse_parser import DeepSeekStreamParser
from .wasm_pow_solver import solve_wasm, build_pow_response as _build_pow_response

_dlog = logging.getLogger("deepseek")
_dlog.addHandler(logging.NullHandler())

_BASE = "https://chat.deepseek.com"
_SESSION_FILE = os.path.join(os.path.dirname(__file__), "..", "session.json")


def _retry_network(fn):
    """
    Decorator: retry on transient network errors (ConnectionError / Timeout)
    and on HTTP 429 / 5xx, which DeepSeek returns for rate limiting and brief
    backend errors.  Those are exactly the transient failures worth retrying.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        last_exc: Exception | None = None
        attempts = max(1, int(config.MAX_RETRIES))
        for attempt in range(attempts):
            try:
                resp = fn(*args, **kwargs)
                # Retry a rate-limited / server-error response, but let the
                # caller's own raise_for_status() report the final failure.
                status = getattr(resp, "status_code", None)
                if status in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                    delay = config.RETRY_DELAY * (attempt + 1)
                    _dlog.warning(
                        f"[RETRY] {fn.__name__} attempt {attempt + 1}/{attempts}: "
                        f"HTTP {status}. Retrying in {delay:.1f}s…"
                    )
                    time.sleep(delay)
                    continue
                return resp
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    delay = config.RETRY_DELAY * (attempt + 1)
                    _dlog.warning(
                        f"[RETRY] {fn.__name__} attempt {attempt + 1}/"
                        f"{attempts}: {exc}. "
                        f"Retrying in {delay:.1f}s…"
                    )
                    time.sleep(delay)
        # `raise None` is a TypeError that would mask the real network error.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(
            f"{fn.__name__}: exhausted {attempts} attempts without a response"
        )
    return wrapper

# Client fingerprint — matched against the real chat.deepseek.com web client
# as captured in a captured browser session (HAR) (2026-10-01, Chrome/154, client 2.5.0).
# Drift here risks WAF challenges.  Verified fields:
#   x-client-bundle-id/com.deepseek.chat, locale en_IN, platform web,
#   version 2.5.0, timezone-offset 19800, x-device-id UUID, x-device-model "".
#
# NOTE: the real client also sends `x-hif-dliq` / `x-hif-leim` on protected
# calls (GET hif-{leim,dliq}.deepseek.com/query → value.sig).  Those are
# server-issued anti-automation tokens that we do not have a generator for;
# live testing confirms the API works without them, so we omit them rather
# than send fabricated values.  Likewise `x-settings-token` (sent by the
# browser on 4/5 client/settings + report calls) is bound to the browser
# session that minted it — a foreign one returns biz_code=3 INVALID_TOKEN,
# and 1/5 browser calls succeed without it, so it is optional and omitted.
_COMMON_HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://chat.deepseek.com",
    "referer": "https://chat.deepseek.com/",
    "x-client-bundle-id": "com.deepseek.chat",
    "x-client-locale": "en_IN",
    "x-client-version": "2.5.0",
    "x-client-platform": "web",
    "x-client-timezone-offset": "19800",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
}

# Stable per-install device id.  DeepSeek expects a lowercase UUID here
# (704043a1-e853-4119-92a8-67ad391d3c89), not an arbitrary opaque string, so
# derive one from the MAC address and persist it in session.json so it stays
# stable across restarts.  Defined after _load_session/_save_session below.


def _load_session() -> dict:
    path = os.path.abspath(_SESSION_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        # A half-written or hand-edited file must not stop the whole harness from
        # importing; it behaves as "not logged in" and refresh_session.py repairs it.
        _dlog.warning("[AUTH] session.json is unreadable; treating it as empty")
        return {}
    return data if isinstance(data, dict) else {}


def _save_session(data: dict) -> None:
    path = os.path.abspath(_SESSION_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(tmp, 0o600)  # tokens and cookies: owner-only where the OS supports it
    except OSError:
        pass
    os.replace(tmp, path)


def _stable_device_id() -> str:
    """Return a persistent UUID-format device id, creating it on first use."""
    try:
        data = _load_session()
        existing = data.get("deviceId")
        if existing and isinstance(existing, str) and len(existing) >= 32:
            return existing
    except Exception:
        pass
    new_id = str(uuid.UUID(bytes=hashlib.sha256(
        uuid.getnode().to_bytes(8, "big")).digest()[:16]))
    try:
        data = _load_session()
        data["deviceId"] = new_id
        _save_session(data)
    except Exception:
        pass
    return new_id


_DEVICE_ID = _stable_device_id()


class DeepSeekUpstreamError(RuntimeError):
    """DeepSeek refused the request with a non-2xx HTTP status."""

    def __init__(self, message: str, status_code: int, retryable: bool = False, application_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.application_code = application_code


def _upstream_http_error(resp) -> DeepSeekUpstreamError:
    """
    Build a readable error from a non-2xx response.

    raise_for_status() alone discards the JSON body, which is where the actual
    reason lives — e.g. an unknown chat_session_id comes back as 422 with a
    biz_msg explaining it, and all the caller would otherwise see is
    "422 Client Error: Unprocessable Entity for url: …".
    """
    status = resp.status_code
    detail = ""
    try:
        # Streamed response: the body is still unread.  Bounded read so a
        # server that keeps the connection open cannot stall us here.
        text = resp.raw.read(2048).decode("utf-8", "replace")
        try:
            obj = json.loads(text)
            data = obj.get("data") or {}
            biz = data.get("biz_msg") or obj.get("msg") or obj.get("message")
            code = data.get("biz_code", obj.get("code"))
            detail = f"{biz} (biz_code={code})" if biz else text[:300]
        except Exception:
            detail = text[:300]
    except Exception:
        pass

    hint = ""
    if status == 422:
        hint = (" — the chat_session_id does not exist, or parent_message_id "
                "is stale")
    elif status == 429:
        hint = " — DeepSeek rate limit; retry after a short delay"
    elif status in (502, 503, 504):
        hint = " — DeepSeek is briefly unavailable; retry"

    return DeepSeekUpstreamError(
        f"DeepSeek rejected the request: HTTP {status}"
        + (f": {detail}" if detail else "")
        + hint,
        status,
        retryable=status in (429, 500, 502, 503, 504),
    )


class DeepSeekClient:
    """Stateful client that handles auth, sessions and POW."""

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self._token: str | None = None
        self._session = requests.Session()
        self._session.headers.update(_COMMON_HEADERS)
        # The real web client sends these on every request.
        self._session.headers["x-device-id"] = _DEVICE_ID
        self._session.headers["x-device-model"] = ""
        # Guards persistent-session state shared by the terminal chatbot and
        # concurrent HTTP API threads.
        self._state_lock = threading.RLock()
        # Guards the login handshake.  Separate from _state_lock so a slow
        # network login never blocks session bookkeeping.
        self._auth_lock = threading.Lock()
        # Persistent chat session (used by terminal chatbot)
        self._chat_session_id: str | None = None
        self._last_parent_id: int | None = None
        self._last_upload_statuses: dict[str, str] = {}
        self._apply_saved_session()

    # ── Session management ────────────────────────────────────────────────────

    def _apply_saved_session(self) -> None:
        """Load token + WAF cookies from session.json if available."""
        data = _load_session()
        if not data:
            return

        # Apply cookies
        cookies = data.get("cookies", {})
        for name, value in cookies.items():
            self._session.cookies.set(name, value, domain=".deepseek.com")
            self._session.cookies.set(name, value, domain="chat.deepseek.com")

        # Apply Bearer token
        token = data.get("userToken")
        if token:
            self._token = token
            self._session.headers["authorization"] = f"Bearer {token}"
        _dlog.debug(
            f"[AUTH] session.json: {len(cookies)} cookie(s), "
            f"token={'present' if token else 'missing'}"
        )

    def reload_session(self) -> bool:
        """Re-read session.json (for example after refresh_session.py ran while this
        process was running). True when the login token changed."""
        with self._auth_lock:
            before = self._token
            self._import_dropped_exports()
            token = _load_session().get("userToken")
            if not token:
                return False
            self._session.cookies.clear()  # cookies the new export no longer has must not linger
            self._apply_saved_session()
            return token != before

    def _import_dropped_exports(self) -> None:
        """Import browser exports newer than session.json from the ``cookies/`` folder (see
        ``bridge/session_import.py``). Never raises: a bad file must not break a request."""
        try:
            from . import session_import
            report = session_import.auto_import(os.path.abspath(_SESSION_FILE))
            if report is not None:
                _dlog.info("[AUTH] cookie folder import: %d cookie(s), token %s, check %s",
                           report["cookies"], report["token"], report["verified"])
        except Exception as exc:  # noqa: BLE001 - report the class only; files hold secrets
            _dlog.warning("[AUTH] cookie folder import failed (%s)", type(exc).__name__)

    def _check_waf_valid(self) -> bool:
        """Return True if the WAF token in session.json hasn't expired."""
        data = _load_session()
        expires = data.get("waf_expires", 0)
        return time.time() < expires - 60  # 60s margin

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _login(self) -> None:
        """Credential-based login (requires a valid WAF cookie already set)."""
        if not self.email or not self.password:
            raise RuntimeError(
                "\n\n  ⚠  No credentials configured.\n"
                "  Add DS_EMAIL and DS_PASSWORD to your .env file or environment.\n"
                "  See .env.example for details.\n"
            )
        _dlog.info("[AUTH] Credential login")
        resp = self._session.post(
            f"{_BASE}/api/v0/users/login",
            json={
                "email":     self.email,
                "mobile":    "",
                "password":  self.password,
                "area_code": "",
                "device_id": _DEVICE_ID,
                "os":        "web",
            },
            timeout=(config.CONNECT_TIMEOUT, config.STREAM_TIMEOUT),
        )

        _dlog.debug(f"[AUTH] POST /api/v0/users/login \u2192 {resp.status_code}")
        # WAF challenge block
        if resp.status_code == 202 and not resp.text.strip():
            _dlog.error("[AUTH] WAF token expired or missing \u2014 run refresh_session.py")
            raise RuntimeError(
                "\n\n  ⚠  AWS WAF token expired or missing!\n"
                "  Run:  python refresh_session.py\n"
                "  Then restart the bridge.\n"
            )

        resp.raise_for_status()
        data = resp.json()
        code = data.get("data", {}).get("biz_code", -1)
        if code != 0:
            raise RuntimeError(
                f"Login failed: biz_code={code}, "
                f"msg={data.get('data', {}).get('biz_msg')}"
            )
        self._token = data["data"]["biz_data"]["user"]["token"]
        self._session.headers["authorization"] = f"Bearer {self._token}"

        # Persist new token
        session_data = _load_session()
        session_data["userToken"] = self._token
        _save_session(session_data)
        _dlog.info("[AUTH] Login successful, token saved")

    def ensure_authenticated(self) -> None:
        if self._token:
            return
        # Double-checked locking.  This was a bare check-then-act: with N
        # waitress threads, N concurrent requests that all found the token
        # empty each ran the full login handshake, so N logins raced to
        # rewrite session.json and each burned a captcha attempt.  Re-check
        # inside the lock so only the first thread logs in.
        with self._auth_lock:
            if self._token:
                return
            if not self._check_waf_valid():
                raise RuntimeError(
                    "\n\n  ⚠  No usable DeepSeek login (session.json has no token).\n"
                    "  Run:  python refresh_session.py   (signs in through your own browser)\n"
                    "  or:   type /connect in the terminal, then press Connect in the browser extension\n"
                    "  Then restart the bridge, or type /refresh in the terminal.\n"
                )
            self._login()

    # ── Chat session ────────────────────────────────────────────────────

    @_retry_network
    def create_session(self) -> str:
        resp = self._session.post(
            f"{_BASE}/api/v0/chat_session/create", json={},
            timeout=config.CONNECT_TIMEOUT,
        )
        _dlog.debug(f"[SESSION] POST /api/v0/chat_session/create \u2192 {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        payload = data.get("data") if isinstance(data, dict) else None
        code = data.get("code") if isinstance(data, dict) else None
        if code == 40003:
            raise DeepSeekUpstreamError("DeepSeek authentication token was rejected; sign in again, then run python "
                                        "refresh_session.py (or /connect in the terminal and press Connect in the "
                                        "browser extension)", 401, application_code=code)
        biz = payload.get("biz_data") if isinstance(payload, dict) else None
        chat = biz.get("chat_session") if isinstance(biz, dict) else None
        session_id = chat.get("id") if isinstance(chat, dict) else None
        if not isinstance(session_id, str) or not session_id:
            numeric = code if isinstance(code, int) else None
            suffix = f" (application code {numeric})" if numeric is not None else ""
            raise DeepSeekUpstreamError("DeepSeek returned no valid session" + suffix, 502, application_code=numeric)
        _dlog.info(f"[SESSION] Created: {session_id}")
        return session_id

    def new_chat(self) -> str:
        """
        Start a fresh persistent chat session.
        All subsequent chat_stream() calls (without force_new=True) will reuse
        this session with proper parent_message_id chaining so DeepSeek keeps
        the full conversation context on its side.

        Call this once at program startup (terminal chatbot) or whenever the
        user wants to clear history.
        """
        self.ensure_authenticated()
        self._chat_session_id = self.create_session()
        self._last_parent_id = None
        return self._chat_session_id

    # ── File upload ───────────────────────────────────────────────────────────

    def upload_file(self, file_path: str, model_type: str = "default",
                    thinking: bool = False) -> str:
        """
        Upload a file and return its DeepSeek file ID.
        Single-model backend: everything uploads as 'default' (Instant).
        'vision'/'expert' are accepted as deprecated aliases and normalized
        (the server disables those model_types; HAR uploads use
        x-model-type: default).
        """
        if model_type in ("vision", "expert"):
            _dlog.debug(f"[UPLOAD] Normalizing deprecated model_type "
                        f"{model_type!r} → 'default'")
            model_type = "default"
        if model_type != "default":
            raise RuntimeError(f"Unknown model_type {model_type!r} (only 'default' exists)")

        self.ensure_authenticated()

        file_path = os.path.abspath(file_path)
        file_size = os.path.getsize(file_path)
        file_name = os.path.basename(file_path)
        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

        _dlog.info(
            f"[UPLOAD] Starting: {file_name}  ({file_size:,} bytes, {mime_type})  "
            f"model={model_type}, thinking={thinking}"
        )
        pow_header = self._solve_and_encode_pow("/api/v0/file/upload_file")

        extra_headers = {
            # Setting content-type to None removes the session-level
            # "application/json" header so requests can set the correct
            # "multipart/form-data; boundary=..." header for the file upload.
            "content-type":       None,
            "x-file-size":        str(file_size),
            "x-ds-pow-response":  pow_header,
            "x-thinking-enabled": "1" if thinking else "0",
            "x-model-type":       model_type,
        }

        with open(file_path, "rb") as fh:
            resp = self._session.post(
                f"{_BASE}/api/v0/file/upload_file",
                files={"file": (file_name, fh, mime_type)},
                headers=extra_headers,
                timeout=(config.CONNECT_TIMEOUT, config.UPLOAD_TIMEOUT),
            )
        _dlog.debug(f"[UPLOAD] POST /api/v0/file/upload_file \u2192 {resp.status_code}")
        resp.raise_for_status()
        biz_data = resp.json()["data"]["biz_data"]
        file_id = biz_data["id"]
        status  = biz_data.get("status", "UNKNOWN")
        _dlog.debug(f"[UPLOAD] Server response: id={file_id}, initial_status={status}")

        # Poll until the file is processed if not immediately ready
        if status != "SUCCESS":
            file_id = self._wait_for_file(file_id, model_type=model_type)
            status = self._last_upload_statuses.get(file_id, "SUCCESS")

        _dlog.info(f"[UPLOAD] Complete: {file_name} \u2192 {file_id} (final_status={status})")
        return file_id

    def _wait_for_file(self, file_id: str, max_wait: int = 60,
                       model_type: str = "default") -> str:
        """
        Poll /api/v0/file/fetch_files until the file reaches any terminal state.

        DeepSeek status lifecycle:
          PENDING / PROCESSING / UPLOADING / PARSING  →  still running
          SUCCESS                                      →  all good
          FAILED / FAIL / ERROR / SKIP                 →  text extraction failed
                                                          (images without text in chat mode)

        We return the file_id as soon as *any* terminal state is reached
        (not just SUCCESS) because the file is already on DeepSeek's servers
        and can still be referenced in a message — the model will handle it.
        On timeout we also return the file_id with a warning rather than
        raising, to avoid silently dropping the attachment.
        """
        # PARSING must be treated as still-running: DeepSeek returns an empty
        # response if we attach a file that is still in the PARSING state.
        _PENDING = {"PENDING", "PROCESSING", "UPLOADING", "PARSING"}
        poll = 0

        deadline = time.time() + max_wait
        while time.time() < deadline:
            time.sleep(2)
            poll += 1
            try:
                resp = self._session.get(
                    f"{_BASE}/api/v0/file/fetch_files",
                    params={"file_ids": file_id},
                    timeout=10,
                )
                resp.raise_for_status()
                files = (resp.json()
                         .get("data", {})
                         .get("biz_data", {})
                         .get("files", []))
                for f in files:
                    if f.get("id") == file_id:
                        status = f.get("status", "UNKNOWN")
                        _dlog.debug(
                            f"[UPLOAD-POLL] #{poll} {file_id[:8]}\u2026 "
                            f"status={status} "
                            f"({'pending' if status in _PENDING else 'terminal'})"
                        )
                        if status not in _PENDING:
                            if status != "SUCCESS":
                                _dlog.warning(
                                    f"[UPLOAD-POLL] Terminal non-success: "
                                    f"{file_id} status={status}"
                                )
                            self._last_upload_statuses[file_id] = status
                            return file_id
            except Exception as _poll_exc:
                _dlog.debug(f"[UPLOAD-POLL] #{poll} network error: {_poll_exc}")

        # Timed out: the file is still on DeepSeek's servers, send it anyway
        _dlog.warning(
            f"[UPLOAD-POLL] Timed out after {max_wait}s / {poll} polls: "
            f"{file_id} still processing \u2014 attaching anyway"
        )
        self._last_upload_statuses[file_id] = "TIMEOUT"
        return file_id

    # ── Proof-of-Work ─────────────────────────────────────────────────────────

    def _get_pow_challenge(self, target_path: str) -> dict:
        resp = self._session.post(
            f"{_BASE}/api/v0/chat/create_pow_challenge",
            json={"target_path": target_path},
            timeout=(config.CONNECT_TIMEOUT, config.STREAM_TIMEOUT),
        )
        _dlog.debug(f"[POW] POST create_pow_challenge({target_path}) \u2192 {resp.status_code}")
        resp.raise_for_status()
        # The server answers HTTP 200 with biz_data: null for a target_path it
        # does not issue challenges for (biz_code 1).  Indexing straight into
        # it raised "TypeError: 'NoneType' object is not subscriptable", which
        # says nothing about the actual problem.
        envelope = resp.json().get("data") or {}
        biz = envelope.get("biz_data")
        if not biz or "challenge" not in biz:
            raise RuntimeError(
                f"DeepSeek issued no PoW challenge for {target_path!r} "
                f"(biz_code={envelope.get('biz_code')}, "
                f"biz_msg={envelope.get('biz_msg')!r}). That target_path is "
                f"not one the server issues challenges for."
            )
        challenge = biz["challenge"]
        challenge["target_path"] = target_path
        _dlog.debug(
            f"[POW] Challenge: difficulty={challenge.get('difficulty')}, "
            f"expire_at={challenge.get('expire_at')}, "
            f"salt={str(challenge.get('salt', ''))[:16]}\u2026"
        )
        return challenge

    def _solve_and_encode_pow(self, target_path: str) -> str:
        """Fetch a POW challenge, solve it, return base64-encoded JSON header."""
        challenge = self._get_pow_challenge(target_path)
        t0 = time.time()
        nonce = solve_wasm(
            challenge["challenge"],
            challenge["salt"],
            challenge["expire_at"],
            challenge["difficulty"],
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        _dlog.info(
            f"[POW] {target_path}: nonce={nonce}, "
            f"difficulty={challenge['difficulty']}, elapsed={elapsed_ms}ms"
        )
        return _build_pow_response(challenge, nonce)

    # ── Streaming completion ──────────────────────────────────────────────────

    def chat_stream(
        self,
        prompt: str,
        model_type: str = "default",
        thinking: bool = False,
        search: bool = True,
        force_new: bool = False,
        file_ids: list[str] | None = None,
        session_id: str | None = None,
        parent_id: int | None = None,
    ):
        """
        Yields raw SSE lines from DeepSeek.

        force_new=False (default):
          If new_chat() has been called, reuses the persistent session and
          chains parent_message_id so DeepSeek sees the full conversation.
          If new_chat() was never called, behaves the same as force_new=True.

        force_new=True:
          Always creates a brand-new session.

        session_id / parent_id:
          An explicit conversation owned by the CALLER.  When session_id is
          given, this call neither reads nor writes the terminal's shared
          session state — so an HTTP request cannot splice itself onto the
          interactive chatbot's message chain (and vice versa).  The caller
          tracks the next parent itself; the response_message_id is on the
          `ready` SSE frame.

        file_ids:
          List of pre-uploaded file IDs to attach to this message.
          Single Instant model — files combine with search + thinking.
        """
        self.ensure_authenticated()

        # Persistent-session bookkeeping is guarded by a lock: the terminal
        # chatbot and concurrent HTTP API requests share one client instance,
        # and an interleaved read-modify-write of _last_parent_id would splice
        # two conversations onto the same message chain.
        if session_id is not None:
            # Caller-owned conversation — leave the shared state untouched.
            parent_id   = parent_id
            _persistent = False
        elif force_new:
            session_id  = self.create_session()
            parent_id   = None
            _persistent = False
        else:
            with self._state_lock:
                session_id  = self._chat_session_id
                if session_id is None:
                    session_id = self.create_session()
                    self._chat_session_id = session_id
                parent_id   = self._last_parent_id
            _persistent = True

        _dlog.info(
            f"[CHAT] Request: model={model_type}, search={search}, "
            f"thinking={thinking}, file_ids={file_ids or []}, "
            f"session={session_id}, parent_id={parent_id}"
        )
        _dlog.debug(
            f"[CHAT] Prompt length: {len(prompt)} chars"
        )
        pow_header = self._solve_and_encode_pow("/api/v0/chat/completion")

        payload = {
            "chat_session_id":   session_id,
            "parent_message_id": parent_id,
            "model_type":        model_type,
            "prompt":            prompt,
            "ref_file_ids":      list(file_ids) if file_ids else [],
            "thinking_enabled":  thinking,
            "search_enabled":    search,
            # Fingerprint parity with the web client (HAR 2026-10-01): every
            # completion carries "action":null.  The server accepts the
            # request without it, but sending it keeps us byte-identical to
            # the browser and avoids tripping future validation.
            # model_type:null is also accepted (web auto-mode) but we always
            # send an explicit default|expert|vision for determinism.
            "action":            None,
            "preempt":           False,
        }

        # Parse the same typed content as the API; title/search patches do
        # not prove that an assistant message was committed upstream.
        _tracked = DeepSeekStreamParser()

        def _iter_and_track(iterator):
            count = 0
            for line in iterator:
                count += 1
                _tracked.feed_line(line)
                yield line
            _dlog.debug("[SSE] Stream complete: %d lines", count)

        try:
            headers = {"x-ds-pow-response": pow_header}
            with self._session.post(
                f"{_BASE}/api/v0/chat/completion",
                json=payload,
                headers=headers,
                stream=True,
                timeout=(config.CONNECT_TIMEOUT, config.STREAM_TIMEOUT),
            ) as resp:
                if resp.status_code == 401:
                    _dlog.info("[AUTH] Token expired; refreshing")
                    with self._auth_lock:
                        self._token = None
                        self._login()
                    with self._session.post(
                        f"{_BASE}/api/v0/chat/completion",
                        json=payload,
                        headers=headers,
                        stream=True,
                        timeout=(config.CONNECT_TIMEOUT, config.STREAM_TIMEOUT),
                    ) as resp2:
                        if resp2.status_code >= 400:
                            raise _upstream_http_error(resp2)
                        yield from _iter_and_track(self._iter_sse(resp2))
                else:
                    if resp.status_code >= 400:
                        raise _upstream_http_error(resp)
                    yield from _iter_and_track(self._iter_sse(resp))
        finally:
            # Update parent for the next turn (runs whether caller exhausts
            # the generator normally or breaks out of it early).
            #
            # Only when the stream actually produced a response: a rate-limited
            # or errored stream still reports a response_message_id, and
            # committing it poisons every subsequent turn with
            # "invalid message id".  On failure we keep the previous parent, so
            # the next request retries this same turn against a valid chain.
            if _persistent and _tracked.response_message_id and _tracked.answer and not _tracked.upstream_error:
                with self._state_lock:
                    if self._chat_session_id == session_id:
                        self._last_parent_id = _tracked.response_message_id

    @staticmethod
    def _iter_sse(response):
        """Yield raw SSE lines from a streaming response."""
        response.encoding = "utf-8"
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            # Skip SSE keepalive heartbeats (bare ":" lines the server emits
            # every ~8s per x-ds-sse-heartbeat-timeout-secs).
            if raw_line == ":" or raw_line.startswith(":"):
                continue
            yield raw_line

    # ── Session management ────────────────────────────────────────────────────

    def list_sessions(self, count: int = 50, pinned: bool = False) -> list[dict]:
        """
        Return a list of chat sessions from the server.
        Set pinned=True to retrieve only pinned sessions.
        """
        self.ensure_authenticated()
        params = f"lte_cursor.pinned={'true' if pinned else 'false'}&count={count}"
        resp = self._session.get(
            f"{_BASE}/api/v0/chat_session/fetch_page?{params}",
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["data"]["biz_data"]["chat_sessions"]

    def get_history(self, session_id: str) -> dict:
        """
        Return the full chat history for a session including all messages,
        search_results, and thinking_content.

        The web client sometimes appends cache-busting params
        (cache_version, cache_reset_at) on repeat polls — the server ignores
        unknown query keys, so a plain chat_session_id request is sufficient.
        """
        self.ensure_authenticated()
        resp = self._session.get(
            f"{_BASE}/api/v0/chat/history_messages",
            params={"chat_session_id": session_id},
            timeout=20,
        )
        resp.raise_for_status()
        # A well-formed UUID that names no session comes back as HTTP 200 with
        # biz_data: null — index straight into it and you get
        # "AttributeError: 'NoneType' object has no attribute 'get'", which
        # reads as a bridge bug rather than a bad session id.
        envelope = resp.json().get("data") or {}
        biz = envelope.get("biz_data")
        if not biz:
            raise DeepSeekUpstreamError(
                f"DeepSeek has no chat session {session_id!r} "
                f"(biz_code={envelope.get('biz_code')}, "
                f"biz_msg={envelope.get('biz_msg')!r})",
                404,
            )
        return biz  # {"chat_session": {...}, "chat_messages": [...]}

    def delete_session(self, session_id: str) -> None:
        """Delete a single chat session permanently."""
        self.ensure_authenticated()
        resp = self._session.post(
            f"{_BASE}/api/v0/chat_session/delete",
            json={"chat_session_id": session_id},
            timeout=15,
        )
        resp.raise_for_status()
        _dlog.info(f"[SESSION] Deleted: {session_id}")

    def delete_all_sessions(self) -> None:
        """Delete ALL chat sessions permanently."""
        self.ensure_authenticated()
        resp = self._session.post(
            f"{_BASE}/api/v0/chat_session/delete_all",
            json={},
            timeout=30,
        )
        resp.raise_for_status()
        _dlog.info("[SESSION] All sessions deleted")

    def rename_session(self, session_id: str, title: str) -> None:
        """Rename a chat session."""
        self.ensure_authenticated()
        resp = self._session.post(
            f"{_BASE}/api/v0/chat_session/update_title",
            json={
                "chat_session_id": session_id,
                "title": title,
                "title_type": "MANUAL",
            },
            timeout=10,
        )
        resp.raise_for_status()
        _dlog.info(f"[SESSION] Renamed {session_id} → '{title}'")

    def pin_session(self, session_id: str, pinned: bool = True) -> None:
        """Pin or unpin a chat session."""
        self.ensure_authenticated()
        resp = self._session.post(
            f"{_BASE}/api/v0/chat_session/update_pinned",
            json={"chat_session_id": session_id, "pinned": pinned},
            timeout=10,
        )
        resp.raise_for_status()
        action = "Pinned" if pinned else "Unpinned"
        _dlog.info(f"[SESSION] {action}: {session_id}")

    # ── Stream control ─────────────────────────────────────────────────────────

    def stop_stream(self, session_id: str, message_id: int) -> None:
        """Stop an in-progress streaming completion."""
        self.ensure_authenticated()
        resp = self._session.post(
            f"{_BASE}/api/v0/chat/stop_stream",
            json={"chat_session_id": session_id, "message_id": message_id},
            timeout=10,
        )
        resp.raise_for_status()
        _dlog.info(f"[STREAM] Stopped: session={session_id}, msg={message_id}")

    # ── Regenerate / continue / edit streams ──────────────────────────────────

    def regenerate_stream(
        self,
        session_id: str,
        child_message_id: int,
        thinking: bool = False,
        search: bool = True,
        user_options: str | None = None,
    ):
        """
        Yields raw SSE lines from DeepSeek for a regeneration request.
        child_message_id is the message_id of the ASSISTANT message to regenerate.
        user_options: 'verbosity_high' | 'verbosity_low' | 'search' | None
        """
        self.ensure_authenticated()
        pow_header = self._solve_and_encode_pow("/api/v0/chat/regenerate")
        payload: dict = {
            "chat_session_id":  session_id,
            "child_message_id": child_message_id,
            "search_enabled":   search,
            "thinking_enabled": thinking,
        }
        if user_options:
            payload["user_options"] = [user_options]

        with self._session.post(
            f"{_BASE}/api/v0/chat/regenerate",
            json=payload,
            headers={"x-ds-pow-response": pow_header},
            stream=True,
            timeout=(config.CONNECT_TIMEOUT, config.STREAM_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            yield from self._iter_sse(resp)

    def continue_stream(self, session_id: str, message_id: int):
        """
        Yields raw SSE lines to continue a truncated/stopped assistant response.
        message_id is the ASSISTANT message_id to continue.
        """
        self.ensure_authenticated()
        # The server only issues PoW challenges for /api/v0/chat/completion and
        # /api/v0/file/upload_file — asking for "/api/v0/chat/continue" returns
        # biz_data: null, which used to blow up as a TypeError.  `/continue`
        # itself is accepted without a challenge, so reuse the completion
        # target rather than sending a header the server never validated.
        pow_header = self._solve_and_encode_pow("/api/v0/chat/completion")
        payload = {
            "chat_session_id":  session_id,
            "message_id":       message_id,
            "fallback_to_resume": True,
        }
        with self._session.post(
            f"{_BASE}/api/v0/chat/continue",
            json=payload,
            headers={"x-ds-pow-response": pow_header},
            stream=True,
            timeout=(config.CONNECT_TIMEOUT, config.STREAM_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            yield from self._iter_sse(resp)

    def edit_message_stream(
        self,
        session_id: str,
        message_id: int,
        prompt: str,
        thinking: bool = False,
        search: bool = True,
    ):
        """
        Yields raw SSE lines for editing a previous user message and
        re-generating the assistant response.
        message_id is the USER message_id to edit.
        """
        self.ensure_authenticated()
        pow_header = self._solve_and_encode_pow("/api/v0/chat/edit_message")
        payload = {
            "chat_session_id": session_id,
            "message_id":      message_id,
            "prompt":          prompt,
            "search_enabled":  search,
            "thinking_enabled": thinking,
        }
        with self._session.post(
            f"{_BASE}/api/v0/chat/edit_message",
            json=payload,
            headers={"x-ds-pow-response": pow_header},
            stream=True,
            timeout=(config.CONNECT_TIMEOUT, config.STREAM_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            yield from self._iter_sse(resp)

    # ── Feedback ──────────────────────────────────────────────────────────────

    def message_feedback(
        self,
        session_id: str,
        message_id: int,
        feedback_type: str,
        description: str | None = None,
    ) -> None:
        """
        Submit feedback on an assistant message.
        feedback_type: 'LIKE' | 'DISLIKE'
        """
        self.ensure_authenticated()
        body: dict = {
            "chat_session_id": session_id,
            "message_id":      message_id,
            "feedback_type":   feedback_type.upper(),
        }
        if description:
            body["description"] = description
        resp = self._session.post(
            f"{_BASE}/api/v0/chat/message_feedback",
            json=body,
            timeout=10,
        )
        resp.raise_for_status()
        _dlog.info(f"[FEEDBACK] {feedback_type} on msg {message_id} in {session_id}")

    # ── Share ─────────────────────────────────────────────────────────────────

    def create_share(self, session_id: str, message_ids: list[int]) -> str:
        """Create a shareable link and return the share_id."""
        self.ensure_authenticated()
        resp = self._session.post(
            f"{_BASE}/api/v0/share/create",
            json={"chat_session_id": session_id, "message_ids": message_ids},
            timeout=10,
        )
        resp.raise_for_status()
        share_id = resp.json()["data"]["biz_data"]["share_id"]
        _dlog.info(f"[SHARE] Created: {share_id}")
        return share_id

    def delete_share(self, share_id: str) -> None:
        """Delete a previously created share link."""
        self.ensure_authenticated()
        resp = self._session.post(
            f"{_BASE}/api/v0/share/delete",
            json={"share_id": share_id},
            timeout=10,
        )
        resp.raise_for_status()

    def list_shares(self, count: int = 10) -> list[dict]:
        """Return list of shared conversations."""
        self.ensure_authenticated()
        resp = self._session.get(
            f"{_BASE}/api/v0/share/list",
            params={"count": count},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["data"]["biz_data"]["shares"]

    # ── Export ────────────────────────────────────────────────────────────────

    def trigger_export(self) -> dict:
        """
        Trigger a full chat history export.
        Returns the export task info (exported_at, expires_at, etc.).
        Poll get_export_status() afterwards to get the download URL.
        """
        self.ensure_authenticated()
        resp = self._session.get(
            f"{_BASE}/api/v0/export_all",
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("biz_data") or {}

    def get_export_download(self) -> str | None:
        """
        Return the download URL for a completed export, or None if not ready.
        """
        self.ensure_authenticated()
        resp = self._session.get(
            f"{_BASE}/api/v0/download_export_history",
            timeout=15,
        )
        resp.raise_for_status()
        biz = resp.json().get("data", {}).get("biz_data") or {}
        return biz.get("download_url")

    # ── User info ─────────────────────────────────────────────────────────────

    def get_current_user(self) -> dict:
        """Return the current user's profile information."""
        self.ensure_authenticated()
        resp = self._session.get(
            f"{_BASE}/api/v0/users/current",
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["data"]["biz_data"]

    def get_user_settings(self) -> dict:
        """Return user account settings (e.g. training_allowed)."""
        self.ensure_authenticated()
        resp = self._session.get(
            f"{_BASE}/api/v0/users/settings",
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["data"]["biz_data"]

    # ── Load session ──────────────────────────────────────────────────────────

    def last_parent_of(self, session_id: str) -> int | None:
        """
        Return the message_id of the newest finished assistant message in
        `session_id`, or None for an empty session.

        Read-only: unlike load_and_resume_session(), this does NOT touch
        _chat_session_id / _last_parent_id.  Those are the *terminal*
        chatbot's live conversation, and the HTTP API needs to inspect a
        caller-pinned session without hijacking the interactive one.
        """
        self.ensure_authenticated()
        data = self.get_history(session_id)
        for msg in reversed(data.get("chat_messages", [])):
            if msg.get("role") == "ASSISTANT" and msg.get("status") == "FINISHED":
                return msg["message_id"]
        return None

    def load_and_resume_session(self, session_id: str) -> str:
        """
        Load an existing session by ID so all subsequent chat_stream() calls
        (with force_new=False) continue that conversation.
        Returns the session_id for confirmation.
        """
        last_id = self.last_parent_of(session_id)
        # Only mutate the shared conversation once the lookup has succeeded, so
        # a bad session_id cannot leave this client pointed at a session that
        # may not exist.
        self._chat_session_id = session_id
        self._last_parent_id = last_id
        _dlog.info(
            f"[SESSION] Loaded existing session: {session_id}, "
            f"last_parent_id={last_id}"
        )
        return session_id

    # ── TTS Voices ────────────────────────────────────────────────────────────

    def get_tts_voices(self) -> list[dict]:
        """Get available TTS voices from DeepSeek."""
        self.ensure_authenticated()
        resp = self._session.get(
            f"{_BASE}/api/v0/chat/tts/voices",
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("biz_data", {}).get("voices", [])

    # ── Client Settings ────────────────────────────────────────────────────────

    def get_client_settings(self, did: str = "", scope: str = "main") -> dict:
        """Get client settings from DeepSeek.

        scope: main, model, web_upgrade, provider, banner

        NOTE: the endpoint REQUIRES a UUID-format `did` query param — omitting
        it returns biz_code=2 INVALID_PARAM.  It must NOT be accompanied by an
        `x-settings-token` header: that token is bound to the browser session
        that minted it and a foreign one returns biz_code=3 INVALID_TOKEN.
        """
        self.ensure_authenticated()
        resp = self._session.get(
            f"{_BASE}/api/v0/client/settings",
            params={"did": did or _DEVICE_ID, "scope": scope},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        if data.get("biz_code") not in (0, None):
            _dlog.warning("[SETTINGS] biz_code=%s biz_msg=%s",
                          data.get("biz_code"), data.get("biz_msg"))
        return data.get("biz_data") or {}

    def report_client_settings(self, settings_ids: list[int], did: str = "",
                               sso_id: str = "") -> dict:
        """Report client settings to DeepSeek."""
        self.ensure_authenticated()
        body = {
            "settings_ids": settings_ids,
            "did": did,
            "sso_id": sso_id,
        }
        resp = self._session.post(
            f"{_BASE}/api/v0/client/settings/report",
            json=body,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("biz_data", {})

    # ── Device Check ───────────────────────────────────────────────────────────

    def check_device(self, device_id: str = "", device_model: str = "") -> dict:
        """Check device status with DeepSeek."""
        self.ensure_authenticated()
        body = {
            "device_id": device_id,
            "device_model": device_model,
        }
        resp = self._session.post(
            f"{_BASE}/api/v0/users/auth_token/check_device",
            json=body,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("biz_data", {})
