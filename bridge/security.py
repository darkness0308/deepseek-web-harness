"""Request-level protections for the local HTTP API.

The API normally listens on loopback with no key. That is safe from the network but not from the
user's own browser: any web page can make the browser send requests to ``127.0.0.1`` (cross-site
requests) and, through DNS rebinding, even read the answers. Native clients (the OpenAI and Anthropic
SDKs, curl, scripts) never send an ``Origin`` header and always address the server by its own name, so
two cheap checks stop browser-borne attacks without touching them:

* **Host check.** While the server is bound to loopback, the ``Host`` header must be a loopback name.
  A rebinding page reaches the server under its own domain name and is refused.
* **Origin check.** A request that carries an ``Origin`` must come from an allowed origin
  (``BRIDGE_ALLOWED_ORIGINS``) or from the server's own origin. Other web pages are refused.

It also provides response headers that stop browsers from sniffing or framing API output, and a
small throttle for repeated wrong API keys.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from urllib.parse import urlsplit

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
WILDCARD_HOSTS = {"", "0.0.0.0", "::", "*"}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Resource-Policy": "same-site",
}


def split_host(value: str) -> str:
    """``Host`` header value -> bare lower-case host name without port or brackets."""
    value = (value or "").strip().lower()
    if value.startswith("["):  # [::1]:8080
        end = value.find("]")
        return value[1:end] if end > 0 else value
    if value.count(":") == 1:  # name:port (a bare IPv6 address has several colons)
        return value.rsplit(":", 1)[0]
    return value


def is_loopback(host: str) -> bool:
    host = split_host(host)
    if host in LOOPBACK_NAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def host_allowed(host_header: str, bound_host: str, extra_hosts=()) -> bool:
    """Is this ``Host`` header acceptable for a server bound to ``bound_host``?

    A server reachable from the network (wildcard or non-loopback bind) serves whatever name the
    operator exposed it under, so only loopback binds are restricted.
    """
    if (bound_host or "").strip().lower() in WILDCARD_HOSTS or not is_loopback(bound_host):
        return True
    host = split_host(host_header)
    if not host:
        return False
    return is_loopback(host) or host in {split_host(h) for h in extra_hosts}


def origin_allowed(origin: str, host_header: str, allowed_origins) -> bool:
    """Is a request with this ``Origin`` header acceptable? No ``Origin`` (native clients) always is."""
    if not origin:
        return True
    origin = origin.strip().rstrip("/").lower()
    if origin == "null":
        return False
    allowed = {o.strip().rstrip("/").lower() for o in allowed_origins}
    if "*" in allowed or origin in allowed:
        return True
    parts = urlsplit(origin)
    # The server's own pages (same scheme-less host:port) may call it.
    return bool(parts.netloc) and parts.netloc == (host_header or "").strip().lower()


class FailureThrottle:
    """Sliding-window limit on wrong API keys per client address.

    ``limit`` failures inside ``window`` seconds lock that address out for ``window`` seconds. A
    correct key clears the record, so a legitimate client is never affected by someone else's guesses
    unless they share an address (loopback), and then only after ``limit`` consecutive bad attempts.
    """

    def __init__(self, limit: int = 20, window: float = 60.0, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _recent(self, who: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(who, []) if now - t < self.window]
        if recent:
            self._failures[who] = recent
        else:
            self._failures.pop(who, None)
        return recent

    def blocked(self, who: str) -> float:
        """Seconds left on the lockout, or 0."""
        if self.limit <= 0:
            return 0.0
        with self._lock:
            now = self.clock()
            recent = self._recent(who, now)
            if len(recent) >= self.limit:
                return max(0.0, self.window - (now - recent[0]))
            return 0.0

    def fail(self, who: str) -> None:
        if self.limit <= 0:
            return
        with self._lock:
            now = self.clock()
            self._recent(who, now)
            self._failures.setdefault(who, []).append(now)
            if len(self._failures) > 1024:  # bound memory under address churn
                for key in sorted(self._failures, key=lambda k: self._failures[k][-1])[:256]:
                    self._failures.pop(key, None)

    def succeed(self, who: str) -> None:
        with self._lock:
            self._failures.pop(who, None)
