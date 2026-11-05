"""``/connect``: a short-lived, single-purpose listener that lets the browser extension hand over
a signed-in DeepSeek session with one click.

Security model (this listener is deliberately NOT part of the main API on port 11434):

* It exists only while the person has asked for it (``/connect``) and closes by itself after
  ``seconds``, after the first successful connection, after too many failures, or on ``/connect stop``.
* It binds to 127.0.0.1 only, and exclusively (on Windows no other program can share the port).
* A request is served only when ALL hold: the peer is loopback, the ``Host`` header names the exact
  loopback address and port (DNS rebinding), and the ``Origin`` is the pinned extension
  (``chrome-extension://<EXTENSION_ID>``). Web pages and other extensions are refused.
* Optional pairing code (``/connect code``): a random 6-digit code printed in the terminal that the
  person types into the extension; compared in constant time, five misses close the listener.
* Bodies are capped, must be JSON, and are never logged. Replies carry counts and verdicts, never secrets.

``EXTENSION_ID`` is derived from the public key stored in ``extension/deepseek-session-exporter/manifest.json``
(a test keeps the two in sync), so a re-installed extension keeps the same ID.
"""
from __future__ import annotations

import http.server
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

from . import session_import

DEFAULT_PORT = 47123
DEFAULT_SECONDS = 120
EXTENSION_ID = "fkllneljkdkoghfelacdfnglaoblmnhp"
MAX_FAILED_IMPORTS = 3
MAX_WRONG_CODES = 5
_ID_RE = re.compile(r"^[a-p]{32}$")


class ConnectError(RuntimeError):
    """The listener could not start (for example the port is taken)."""


def allowed_origins(extra_ids=None) -> set:
    ids = {EXTENSION_ID}
    raw = extra_ids if extra_ids is not None else os.getenv("HARNESS_CONNECT_EXTENSION_IDS", "")
    for item in (raw.split(",") if isinstance(raw, str) else raw):
        item = item.strip().lower()
        if _ID_RE.match(item):
            ids.add(item)
    return {"chrome-extension://" + i for i in ids}


def check_request(peer: str, host: str, origin: str, port: int, origins: set, allow_no_origin: bool = False):
    """Return ``None`` when the request may proceed, else ``(status, code)``. Pure, so it is unit-tested directly.

    ``allow_no_origin`` is only for the read-only status ping: Chrome sends no ``Origin`` on a simple GET from an
    extension page. A page that sends one must still be the pinned extension, and a reply carries CORS headers
    only for the pinned origin, so a web page can never read it."""
    try:
        if not ipaddress.ip_address(str(peer).split("%")[0]).is_loopback:
            return 403, "local_only"
    except ValueError:
        return 403, "local_only"
    if str(host).lower() not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return 403, "bad_host"
    if origin not in origins and not (allow_no_origin and origin == ""):
        return 403, "origin_refused"
    return None


class _Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = False   # SO_REUSEADDR would let another process share the port on Windows
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class ConnectListener:
    """Run it with :meth:`start`; progress is reported through ``on_event(kind, info)`` where kind is
    ``refused`` | ``connected`` | ``failed`` | ``expired`` | ``stopped``. The callback must not raise (it is guarded)."""

    def __init__(self, session_path, on_event=None, *, port=None, seconds=None, code: bool = False,
                 extension_ids=None, verify: bool = True, http=None):
        self.session_path = Path(session_path)
        self.on_event = on_event or (lambda kind, info: None)
        self.requested_port = DEFAULT_PORT if port is None else port
        self.seconds = DEFAULT_SECONDS if seconds is None else seconds
        self.code = f"{secrets.randbelow(10**6):06d}" if code else None
        self.origins = allowed_origins(extension_ids)
        self.verify, self.http = verify, http
        self.state = "idle"       # idle | waiting | connected | expired | stopped
        self.port = None
        self._server = None
        self._timer = None
        self._lock = threading.Lock()
        self._failed = self._wrong = 0
        self.finished = threading.Event()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> int:
        owner = self

        class Handler(_Handler):
            listener = owner

        try:
            self._server = _Server(("127.0.0.1", self.requested_port), Handler)
        except OSError as exc:
            raise ConnectError(f"port {self.requested_port} is not available ({exc.strerror or type(exc).__name__}); "
                               "another /connect window or program is using it") from None
        self.port = self._server.server_address[1]
        self.state = "waiting"
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        self._timer = threading.Timer(self.seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()
        return self.port

    def _emit(self, kind, info=None):
        try:
            self.on_event(kind, info or {})
        except Exception:  # noqa: BLE001 - a broken callback must never leave the port open
            pass

    def _close(self, state: str) -> bool:
        with self._lock:
            if self.state != "waiting":
                return False
            self.state = state
        if self._timer is not None:
            self._timer.cancel()
        server = self._server
        if server is not None:
            threading.Thread(target=lambda: (server.shutdown(), server.server_close()), daemon=True).start()
        self.finished.set()
        return True

    def _expire(self):
        if self._close("expired"):
            self._emit("expired")

    def stop(self, reason: str = "stopped"):
        if self._close("stopped"):
            self._emit("stopped", {"reason": reason})

    @property
    def active(self) -> bool:
        return self.state == "waiting"

    # ── request handling (called from handler threads) ───────────────────────

    def _wrong_code(self):
        self._wrong += 1
        if self._wrong >= MAX_WRONG_CODES:
            self.stop("too many wrong pairing codes")

    def handle_import(self, payload, presented_code):
        """Returns ``(status, body)``."""
        if self.code is not None and not secrets.compare_digest(str(presented_code or ""), self.code):
            self._wrong_code()
            return 403, {"ok": False, "error": "wrong pairing code", "code": "wrong_code"}
        with self._lock:
            if self.state != "waiting":
                return 410, {"ok": False, "error": "the harness is no longer listening", "code": "closed"}
        try:
            report = session_import.import_payload(payload, session_path=self.session_path,
                                                   verify=self.verify, http=self.http)
        except session_import.ImportProblem as exc:
            return 400, {"ok": False, "error": str(exc), "code": "unreadable_export"}
        report.pop("cookie_names", None)
        if report["ok"]:
            self._emit("connected", report)
            self._close("connected")
            return 200, report
        self._failed += 1
        self._emit("failed", report)
        if self._failed >= MAX_FAILED_IMPORTS:
            self.stop("too many failed attempts")
        return 422, report


class _Handler(http.server.BaseHTTPRequestHandler):
    listener: ConnectListener
    protocol_version = "HTTP/1.1"
    timeout = 10                     # a stalled client cannot hold a thread

    def log_message(self, *args):    # request lines are never written anywhere
        pass

    def _send(self, status, body=None, origin=None):
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Connect-Code")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")
        self.send_header("Connection", "close")
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)
        self.close_connection = True

    def _drain(self):
        """Read and discard (a bounded amount of) the request body. Replying to a request whose body is
        still unread makes Windows reset the connection, so the sender would never see the refusal."""
        try:
            remaining = min(int(self.headers.get("Content-Length", "0") or 0), 4 * session_import.MAX_PAYLOAD_BYTES)
        except ValueError:
            return
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def _guard(self, allow_no_origin=False):
        listener = self.listener
        origin = self.headers.get("Origin", "")
        refusal = check_request(self.client_address[0], self.headers.get("Host", ""), origin,
                                listener.port, listener.origins, allow_no_origin)
        if refusal is None:
            return origin
        status, code = refusal
        self._drain()
        self._send(status, {"ok": False, "error": _REFUSALS[code], "code": code})
        listener._emit("refused", {"code": code, "origin": re.sub(r"[^\x21-\x7e]", "?", origin)[:80]})
        return None

    def do_OPTIONS(self):
        origin = self._guard()
        if origin is not None:
            self._send(204, None, origin)

    def do_GET(self):
        origin = self._guard(allow_no_origin=True)
        if origin is None:
            return
        if self.path.split("?")[0] == "/connect/ping" and self.listener.active:
            self._send(200, {"harness": "deepseek-bridge", "waiting": True,
                             "needs_code": self.listener.code is not None}, origin or None)
        else:
            self._send(404, {"ok": False, "error": "not found", "code": "not_found"}, origin)

    def do_POST(self):
        origin = self._guard()
        if origin is None:
            return
        if self.path.split("?")[0] != "/connect/session":
            return self._send(404, {"ok": False, "error": "not found", "code": "not_found"}, origin)
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            return self._send(415, {"ok": False, "error": "send JSON", "code": "bad_type"}, origin)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send(411, {"ok": False, "error": "Content-Length required", "code": "no_length"}, origin)
        if length < 0 or length > session_import.MAX_PAYLOAD_BYTES:
            self._drain()
            return self._send(413, {"ok": False, "error": "export too large", "code": "too_large"}, origin)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, OSError):
            return self._send(400, {"ok": False, "error": "body is not valid JSON", "code": "bad_json"}, origin)
        if not isinstance(payload, (dict, list)):
            return self._send(400, {"ok": False, "error": "body must be a JSON object", "code": "bad_json"}, origin)
        status, body = self.listener.handle_import(payload, self.headers.get("X-Connect-Code"))
        self._send(status, body, origin)


_REFUSALS = {
    "local_only": "this listener answers only the computer it runs on",
    "bad_host": "unexpected Host header",
    "origin_refused": "only the DeepSeek Session Connector extension may connect",
}


OUTPUT_LOCK = threading.Lock()   # the listener thread and the waiting status line share the console


class EscapeKey:
    """Non-blocking "was Esc pressed?" on the console. Inert (``enabled`` False) when stdin is not a terminal."""

    def __init__(self):
        self.enabled = False
        self._fd = self._old = self._msvcrt = None

    def __enter__(self):
        try:
            if not sys.stdin or not sys.stdin.isatty():
                return self
            if os.name == "nt":
                import msvcrt
                self._msvcrt = msvcrt
                while msvcrt.kbhit():          # drop type-ahead so an old Esc does not cancel at once
                    msvcrt.getwch()
            else:
                import termios
                import tty
                self._fd = sys.stdin.fileno()
                self._old = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
            self.enabled = True
        except Exception:  # noqa: BLE001 - no key support: Ctrl+C still cancels
            self.enabled = False
        return self

    def pressed(self) -> bool:
        if not self.enabled:
            return False
        try:
            if self._msvcrt is not None:
                while self._msvcrt.kbhit():
                    key = self._msvcrt.getwch()
                    if key in ("\x00", "\xe0"):       # arrow/function key: a second code follows
                        self._msvcrt.getwch()
                    elif key == "\x1b":
                        return True
                return False
            import select
            while select.select([self._fd], [], [], 0)[0]:
                key = os.read(self._fd, 1)
                if key == b"\x1b":
                    if select.select([self._fd], [], [], 0.05)[0]:
                        os.read(self._fd, 8)    # an escape sequence (arrow key), not the Esc key
                        continue
                    return True
        except Exception:  # noqa: BLE001
            self.enabled = False
        return False

    def __exit__(self, *exc):
        if self._old is not None:
            try:
                import termios
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            except Exception:  # noqa: BLE001
                pass
        return False


def wait_for_connection(listener: ConnectListener) -> str:
    """Hold the terminal until the extension connected, the window ended, or the person pressed Esc / Ctrl+C.

    Returns the listener's final state (``connected``, ``expired``, ``stopped``). While waiting there is no input
    prompt: a single status line counts down on a real terminal."""
    started = time.monotonic()
    live = bool(getattr(sys.stdout, "isatty", lambda: False)())
    shown = 0
    with EscapeKey() as keys:
        hint = "Esc" if keys.enabled else "Ctrl+C"
        if not live:
            with OUTPUT_LOCK:
                print(f"Waiting for the extension ({hint} cancels) ...")
        try:
            next_tick = 0.0
            while not listener.finished.wait(0.1):
                if keys.pressed():
                    listener.stop("cancelled")
                    break
                now = time.monotonic()
                if live and now >= next_tick:
                    next_tick = now + 1.0
                    left = max(0, int(listener.seconds - (now - started) + 0.99))
                    line = f"Waiting for the extension to connect ({left} s left) - press {hint} to cancel"
                    with OUTPUT_LOCK:
                        sys.stdout.write("\r" + line.ljust(shown))
                        sys.stdout.flush()
                    shown = len(line)
        except KeyboardInterrupt:
            listener.stop("cancelled")
        finally:
            if live and shown:
                with OUTPUT_LOCK:
                    sys.stdout.write("\r" + " " * shown + "\r")
                    sys.stdout.flush()
    time.sleep(0.05)   # let the listener thread finish printing its own message first
    return listener.state


def run_blocking(session_path, say=print, *, code: bool = False, seconds=None, port=None) -> int:
    """For ``python import_session.py --connect``: listen until connected or expired. Exit status 0 on success."""
    result = {"ok": False}

    def on_event(kind, info):
        if kind == "connected":
            result["ok"] = True
        say(describe_event(kind, info))

    listener = ConnectListener(session_path, on_event, code=code, seconds=seconds, port=port)
    try:
        listener.start()
    except ConnectError as exc:
        say("Cannot listen: " + str(exc))
        return 1
    say(start_message(listener))
    wait_for_connection(listener)
    return 0 if result["ok"] else 1


def start_message(listener: ConnectListener) -> str:
    lines = [f"Listening on 127.0.0.1:{listener.port} (closes after {int(listener.seconds)} s or the first connection).",
             "Open chat.deepseek.com (signed in), click the extension and press Connect."]
    if listener.code:
        lines.append(f"Pairing code: {listener.code}   (type it into the extension)")
    return "\n".join(lines)


def describe_event(kind: str, info: dict) -> str:
    if kind == "connected":
        return "Connected: " + "\n".join(line.strip() for line in session_import.describe(info)[-3:] if line.strip()) + \
               "\nThe listener is closed."
    if kind == "failed":
        return "Connection received but the login was not accepted: " + str(info.get("detail", "")) + \
               "\n" + "\n".join(info.get("messages", [])[:2])
    if kind == "refused":
        extra = f" (origin {info.get('origin')})" if info.get("origin") else ""
        return f"Refused a request: {_REFUSALS.get(info.get('code'), 'not allowed')}{extra}."
    if kind == "expired":
        return "The connect window expired; type /connect to open it again."
    return f"Connect listener closed ({info.get('reason', 'stopped')})."
