"""Turn a browser export of a signed-in DeepSeek session into ``session.json``.

The web login endpoint sits behind an AWS WAF challenge (a captcha for automated
browsers), so the harness never has to log in itself: a person signs in once in a
normal browser and this module imports the result.  Accepted inputs:

* the bundled browser extension's export (``extension/deepseek-session-exporter``)
* Cookie-Editor / EditThisCookie / ``chrome.cookies`` JSON lists
* Playwright ``storage_state`` files (these also carry localStorage, so the token)
* ``session.json`` itself, Netscape ``cookies.txt``, a ``Cookie:`` header string
* DevTools "Copy as cURL" text and HAR files (they carry the Authorization header)

A cookie export alone cannot log in: the bearer token (``userToken``) lives in the
page's localStorage, not in a cookie.  ``import_files`` reports that explicitly.

Secrets are never logged or returned: reports carry counts, names and verdicts only.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

DOMAIN = "deepseek.com"
MAX_FILE_BYTES = 40 * 1024 * 1024    # a HAR capture is large; cookie exports are tiny
MAX_PAYLOAD_BYTES = 512 * 1024       # what the local HTTP import route accepts
EXPORT_SUFFIXES = {".json", ".txt", ".curl", ".har", ".cookies", ".sh", ".cmd"}
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=\-]{16,4096}$")
_WAF_COOKIE = "aws-waf-token"
_DEFAULT_WAF_LIFETIME = 4 * 86400    # the lifetime refresh_session.py always assumed


class ImportProblem(ValueError):
    """The input cannot be understood. The message never contains secret values."""


@dataclass
class Parsed:
    cookies: dict = field(default_factory=dict)      # name -> value (deepseek.com only)
    expiries: dict = field(default_factory=dict)     # name -> epoch seconds
    token: str | None = None
    device_id: str | None = None
    fmt: str = "unknown"
    expired: int = 0                                 # cookies skipped because they had expired
    foreign: int = 0                                 # cookies skipped for another website


# ── token / cookie helpers ────────────────────────────────────────────────────

def clean_token(raw) -> str | None:
    """Accept the raw localStorage value (``{"value": "...", "__version": "0"}``), a bare token
    or ``Bearer <token>``; return the token or ``None`` when it is not token-shaped."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = raw.get("value")
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if raw[:1] in "{\"":
        try:
            decoded = json.loads(raw)
        except ValueError:
            decoded = None
        if isinstance(decoded, (dict, str)):
            return clean_token(decoded)
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw if _TOKEN_RE.match(raw) else None


def _is_site(domain) -> bool:
    if not domain:
        return True  # header strings and some exports carry no domain at all
    domain = str(domain).strip().lower().lstrip(".")
    return domain == DOMAIN or domain.endswith("." + DOMAIN)


def _expiry(entry: dict):
    for key in ("expirationDate", "expires", "expiry", "expire", "expiration"):
        value = entry.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > 0:                       # -1 / 0 mean "session cookie"
            return float(value) / 1000 if value > 1e11 else float(value)
    return None


def _add_cookie(parsed: Parsed, entry: dict, now: float, priority: dict) -> None:
    name, value = entry.get("name"), entry.get("value")
    if not isinstance(name, str) or not name or not isinstance(value, str):
        return
    if not _is_site(entry.get("domain")):
        parsed.foreign += 1
        return
    expires = _expiry(entry)
    if expires is not None and expires < now:
        parsed.expired += 1
        return
    domain = str(entry.get("domain") or "").lstrip(".").lower()
    score = 2 if domain.startswith("chat.") else 1   # the host-specific cookie wins
    if name in parsed.cookies and priority.get(name, 0) > score:
        return
    priority[name] = score
    parsed.cookies[name] = value
    if expires is not None:
        parsed.expiries[name] = expires
    else:
        parsed.expiries.pop(name, None)


def _local_storage_token(origins) -> str | None:
    for origin in origins if isinstance(origins, list) else []:
        if not isinstance(origin, dict) or not _is_site(re.sub(r"^https?://", "", str(origin.get("origin", ""))).split("/")[0]):
            continue
        for item in origin.get("localStorage") or []:
            if isinstance(item, dict) and item.get("name") == "userToken":
                return clean_token(item.get("value"))
    return None


# ── format parsers ────────────────────────────────────────────────────────────

def _from_cookie_list(items, parsed: Parsed, now: float) -> None:
    priority: dict = {}
    for entry in items:
        if isinstance(entry, dict):
            _add_cookie(parsed, entry, now, priority)


def _from_object(obj, now: float) -> Parsed:
    parsed = Parsed()
    if isinstance(obj, list):
        parsed.fmt = "cookie-list"
        _from_cookie_list(obj, parsed, now)
        if not parsed.cookies:
            raise ImportProblem("the list has no usable deepseek.com cookies")
        return parsed
    if not isinstance(obj, dict):
        raise ImportProblem("JSON must be an object or a list of cookies")

    if isinstance(obj.get("log"), dict) and isinstance(obj["log"].get("entries"), list):
        return _from_har(obj["log"]["entries"], now)

    cookies = obj.get("cookies")
    if isinstance(cookies, list):
        parsed.fmt = "storage-state" if "origins" in obj else "cookie-list"
        _from_cookie_list(cookies, parsed, now)
    elif isinstance(cookies, dict):
        parsed.fmt = "session-json"
        for name, value in cookies.items():
            if isinstance(name, str) and isinstance(value, str):
                parsed.cookies[name] = value
        waf = obj.get("waf_expires")
        if _WAF_COOKIE in parsed.cookies and isinstance(waf, (int, float)) and waf > 0:
            parsed.expiries[_WAF_COOKIE] = float(waf)
    else:
        parsed.fmt = "token-only"

    if obj.get("format") == "deepseek-harness-session":
        parsed.fmt = "extension-export"
    parsed.token = (clean_token(obj.get("userToken")) or clean_token(obj.get("token"))
                    or _local_storage_token(obj.get("origins")))
    storage = obj.get("localStorage")
    if parsed.token is None and isinstance(storage, dict):
        parsed.token = clean_token(storage.get("userToken"))
    device = obj.get("deviceId") or obj.get("device_id")
    if isinstance(device, str) and len(device) >= 32:
        parsed.device_id = device
    if not parsed.cookies and not parsed.token:
        raise ImportProblem("no DeepSeek cookies or login token found in this JSON")
    return parsed


def _from_har(entries, now: float) -> Parsed:
    parsed = Parsed(fmt="har")
    priority: dict = {}
    for entry in entries:
        request = entry.get("request") if isinstance(entry, dict) else None
        if not isinstance(request, dict) or not _is_site(re.sub(r"^https?://", "", str(request.get("url", ""))).split("/")[0]):
            continue
        if DOMAIN not in str(request.get("url", "")):
            continue
        for header in request.get("headers") or []:
            if isinstance(header, dict) and str(header.get("name", "")).lower() == "authorization":
                parsed.token = clean_token(header.get("value")) or parsed.token
        for cookie in request.get("cookies") or []:
            if isinstance(cookie, dict):
                _add_cookie(parsed, {**cookie, "domain": cookie.get("domain") or "chat.deepseek.com"}, now, priority)
        for header in request.get("headers") or []:
            if isinstance(header, dict) and str(header.get("name", "")).lower() == "x-device-id":
                value = header.get("value")
                if isinstance(value, str) and len(value) >= 32:
                    parsed.device_id = value
    if not parsed.cookies and not parsed.token:
        raise ImportProblem("this HAR has no cookies or Authorization header for chat.deepseek.com "
                            "(browsers strip them from exported HAR files unless sensitive data is included)")
    return parsed


def _from_netscape(text: str, now: float) -> Parsed:
    parsed = Parsed(fmt="netscape-cookies")
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        try:
            expires = int(float(parts[4]))
        except ValueError:
            expires = 0
        entries.append({"domain": parts[0], "name": parts[5], "value": "\t".join(parts[6:]), "expires": expires})
    if not entries:
        raise ImportProblem("no cookie lines found")
    _from_cookie_list(entries, parsed, now)
    if not parsed.cookies:
        raise ImportProblem("the file has no usable deepseek.com cookies")
    return parsed


_AUTH_RE = re.compile(r"authorization['\"]?\s*[:=]\s*['\"]?\s*bearer\s+([A-Za-z0-9._~+/=\-]{16,4096})", re.I)
_COOKIE_HDR_RE = re.compile(r"(?:^|[\s'\"])(?:-H\s+)?['\"]?cookie['\"]?\s*:\s*([^\r\n'\"]+)", re.I)
_CURL_B_RE = re.compile(r"(?:^|\s)(?:-b|--cookie)\s+(?:\$?'([^']*)'|\"([^\"]*)\"|(\S+))")
_DEVICE_RE = re.compile(r"x-device-id['\"]?\s*:\s*['\"]?\s*([0-9a-fA-F-]{32,40})", re.I)


def _cookie_pairs(blob: str) -> list:
    pairs = []
    for part in blob.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip():
            pairs.append({"name": name.strip(), "value": value.strip(), "domain": "chat." + DOMAIN})
    return pairs


def _from_text(text: str, now: float) -> Parsed:
    """cURL commands, raw header dumps and bare ``a=b; c=d`` cookie strings."""
    parsed = Parsed(fmt="http-text")
    match = _AUTH_RE.search(text)
    if match:
        parsed.token = clean_token(match.group(1))
    device = _DEVICE_RE.search(text)
    if device:
        parsed.device_id = device.group(1).lower()
    blob = None
    header = _COOKIE_HDR_RE.search(text)
    if header:
        blob = header.group(1)
    else:
        flag = _CURL_B_RE.search(text)
        if flag:
            blob = next(g for g in flag.groups() if g)
        elif "\n" not in text.strip() and "=" in text and not text.strip().lower().startswith(("curl", "fetch")):
            blob = text.strip()
    if blob:
        _from_cookie_list(_cookie_pairs(blob), parsed, now)
    if not parsed.cookies and not parsed.token:
        raise ImportProblem("could not find cookies or a login token in this text")
    return parsed


def parse_text(text: str, now: float | None = None) -> Parsed:
    now = time.time() if now is None else now
    stripped = text.lstrip("﻿ \t\r\n")
    if not stripped:
        raise ImportProblem("the file is empty")
    if stripped[0] in "{[":
        try:
            obj = json.loads(stripped)
        except ValueError as exc:
            raise ImportProblem(f"invalid JSON ({exc.__class__.__name__}) at character {getattr(exc, 'pos', '?')}") from None
        return _from_object(obj, now)
    if "# Netscape HTTP Cookie File" in stripped[:200] or re.search(r"^\S+\t(?:TRUE|FALSE)\t\S+\t(?:TRUE|FALSE)\t", stripped, re.M | re.I):
        return _from_netscape(stripped, now)
    return _from_text(stripped, now)


def parse_file(path, now: float | None = None) -> Parsed:
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ImportProblem(f"cannot read {path.name}: {exc.__class__.__name__}") from None
    if size > MAX_FILE_BYTES:
        raise ImportProblem(f"{path.name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise ImportProblem(f"cannot read {path.name}: {exc.__class__.__name__}") from None
    return parse_text(text, now)


def parse_payload(payload, now: float | None = None) -> Parsed:
    """The extension's JSON body (already decoded)."""
    return _from_object(payload, time.time() if now is None else now)


# ── session.json storage ──────────────────────────────────────────────────────

def default_session_path() -> Path:
    return Path(__file__).resolve().parent.parent / "session.json"


def load_session(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_session(path, data: dict) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def cookie_dir(session_path=None) -> Path:
    """Where exports are dropped: ``HARNESS_COOKIE_DIR`` or ``cookies/`` beside session.json."""
    base = Path(session_path).resolve().parent if session_path else default_session_path().parent
    override = os.getenv("HARNESS_COOKIE_DIR", "").strip()
    if override:
        folder = Path(override)
        return folder if folder.is_absolute() else base / folder
    return base / "cookies"


def discover(folder) -> list:
    """Candidate export files in ``folder``, oldest first (so the newest is applied last)."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir()
             if p.is_file() and not p.name.startswith((".", "~")) and p.suffix.lower() in EXPORT_SUFFIXES
             and p.name.lower() != "readme.txt"]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))


def merge(existing: dict, parsed_list: list, token: str | None = None, now: float | None = None) -> dict:
    """Return the new session dict. Newer exports win; the stable ``deviceId`` is preserved."""
    now = time.time() if now is None else now
    session = dict(existing)
    # An export that carries a token is a whole signed-in browser snapshot: it replaces the old cookie set
    # (cookies of the previous login would only confuse the server). A cookies-only export tops up the old one.
    snapshot = any(parsed.token for parsed in parsed_list)
    cookies = {} if snapshot else dict(existing.get("cookies") or {})
    waf_expiry = 0 if snapshot else (existing.get("waf_expires") or 0)
    if _WAF_COOKIE in cookies and waf_expiry and waf_expiry < now:
        cookies.pop(_WAF_COOKIE, None)       # a dead challenge cookie only causes confusion
        waf_expiry = 0
    for parsed in parsed_list:
        cookies.update(parsed.cookies)
        if _WAF_COOKIE in parsed.cookies:
            waf_expiry = parsed.expiries.get(_WAF_COOKIE) or (now + _DEFAULT_WAF_LIFETIME)
        if parsed.token:
            session["userToken"] = parsed.token
        if parsed.device_id and not session.get("deviceId"):
            session["deviceId"] = parsed.device_id
    if token:
        session["userToken"] = token
    session["cookies"] = cookies
    if waf_expiry:
        session["waf_expires"] = int(waf_expiry)
    else:
        session.pop("waf_expires", None)
    session["extracted_at"] = int(now * 1000)
    session["_note"] = "Imported from a browser export; see import_session.py. Re-import when the token is rejected."
    return session


# ── verification ──────────────────────────────────────────────────────────────

def verify_login(session: dict, timeout: float = 15, http=None) -> tuple:
    """Ask DeepSeek whether the token is accepted. Returns ``(verdict, detail)`` where verdict is
    ``valid``, ``rejected``, ``blocked`` (WAF challenge), ``missing`` or ``unknown``.
    ``GET /api/v0/users/current`` is the call the web app makes on load; it has no side effects."""
    token = session.get("userToken")
    if not token:
        return "missing", "no login token"
    import requests
    from .deepseek_client import _COMMON_HEADERS, _BASE
    http = http or requests.Session()
    headers = dict(_COMMON_HEADERS)
    headers["authorization"] = "Bearer " + token
    headers["x-device-id"] = session.get("deviceId") or ""
    headers["x-device-model"] = ""
    cookies = {k: v for k, v in (session.get("cookies") or {}).items() if isinstance(v, str)}
    try:
        response = http.get(_BASE + "/api/v0/users/current", headers=headers, cookies=cookies, timeout=timeout)
    except requests.RequestException as exc:
        return "unknown", f"network error ({exc.__class__.__name__})"
    if response.status_code == 202 and "challenge" in str(response.headers.get("x-amzn-waf-action", "")).lower():
        return "blocked", "AWS WAF challenge; import a fresh export that includes the aws-waf-token cookie"
    try:
        body = response.json()
    except ValueError:
        return "unknown", f"HTTP {response.status_code} without JSON"
    code = body.get("code") if isinstance(body, dict) else None
    data = body.get("data") if isinstance(body, dict) else None
    if code == 0 and isinstance(data, dict) and isinstance(data.get("biz_data"), dict):
        return "valid", "token accepted"
    if code in (40002, 40003):
        return "rejected", f"DeepSeek rejected the token (code {code}); sign in again and re-export"
    return "unknown", f"HTTP {response.status_code}, application code {code}"


# ── the import pipeline ───────────────────────────────────────────────────────

TOKEN_HELP = (
    "These cookies do not include the login token, which DeepSeek keeps in localStorage. Get it one of three ways:\n"
    "  1. Type /connect in the harness, then press Connect in the browser extension (extension/deepseek-session-exporter).\n"
    "  2. In chat.deepseek.com press F12 -> Console, run   copy(JSON.parse(localStorage.userToken).value)\n"
    "     then run   python import_session.py --token-prompt   and paste it.\n"
    "  3. F12 -> Network -> pick any /api/v0/ request -> Copy as cURL (bash) -> save it as cookies/request.txt."
)


def import_parsed(parsed_list: list, *, session_path=None, token: str | None = None, verify: bool = True,
                  dry_run: bool = False, http=None, sources: list | None = None) -> dict:
    """Merge parsed exports into session.json and return a secret-free report."""
    path = Path(session_path) if session_path else default_session_path()
    existing = load_session(path)
    candidate = merge(existing, parsed_list, token=clean_token(token) if token else None)
    if token and not clean_token(token):
        raise ImportProblem("that does not look like a DeepSeek login token")
    had_token = bool(existing.get("userToken"))
    new_token = candidate.get("userToken") != existing.get("userToken")
    report = {
        "ok": False, "wrote": False, "sources": sources or [], "cookies": len(candidate["cookies"]),
        "cookie_names": sorted(candidate["cookies"]),
        "token": "new" if new_token else ("kept" if had_token else "missing"),
        "verified": "skipped", "detail": "", "messages": [],
        "waf_expires": candidate.get("waf_expires"),
    }
    if not candidate.get("userToken"):
        report["detail"] = "no login token"
        report["messages"].append(TOKEN_HELP)
        # cookies are still worth keeping (a later token-only import completes the login)
        if parsed_list and not dry_run:
            save_session(path, candidate)
            report["wrote"] = True
        return report
    if verify:
        verdict, detail = verify_login(candidate, http=http)
        report["verified"], report["detail"] = verdict, detail
        if verdict == "rejected" and new_token and had_token:
            verdict_old, _ = verify_login(existing, http=http)
            if verdict_old == "valid":
                report["messages"].append("The imported token was rejected; the working login in session.json was kept.")
                return report
        if verdict == "rejected" and report["token"] == "kept":
            report["messages"].append("The token already in session.json has expired, and this export did not bring a new one.")
            report["messages"].append(TOKEN_HELP)
        elif verdict == "rejected":
            report["messages"].append(
                "DeepSeek rejects this token. It is expired or from a signed-out browser: sign in at "
                "chat.deepseek.com, then export again.")
        elif verdict == "blocked":
            report["messages"].append(
                "The WAF challenge is active for this network/session; export again right after loading "
                "chat.deepseek.com in your browser so the aws-waf-token cookie is included.")
    report["ok"] = report["verified"] in ("valid", "skipped", "unknown")
    if not dry_run and (report["ok"] or report["verified"] in ("rejected", "blocked")):
        save_session(path, candidate)   # keep the new cookies/token so `--doctor` shows the real state
        report["wrote"] = True
    return report


def import_files(paths, *, session_path=None, token: str | None = None, verify: bool = True,
                 dry_run: bool = False, http=None) -> dict:
    parsed_list, sources, problems = [], [], []
    for path in paths:
        path = Path(path)
        try:
            parsed = parse_file(path)
        except ImportProblem as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        parsed_list.append(parsed)
        sources.append({"file": path.name, "format": parsed.fmt, "cookies": len(parsed.cookies),
                        "token": bool(parsed.token), "expired_skipped": parsed.expired,
                        "other_sites_skipped": parsed.foreign})
    if not parsed_list and not token:
        return {"ok": False, "wrote": False, "sources": [], "cookies": 0, "cookie_names": [], "token": "missing",
                "verified": "skipped", "detail": "nothing could be imported", "messages": problems or ["no export files found"],
                "waf_expires": None}
    report = import_parsed(parsed_list, session_path=session_path, token=token, verify=verify, dry_run=dry_run,
                           http=http, sources=sources)
    report["messages"] = problems + report["messages"]
    return report


def import_payload(payload, *, session_path=None, verify: bool = True, http=None) -> dict:
    parsed = parse_payload(payload)
    return import_parsed([parsed], session_path=session_path, verify=verify, http=http,
                         sources=[{"file": "(extension)", "format": parsed.fmt, "cookies": len(parsed.cookies),
                                   "token": bool(parsed.token), "expired_skipped": parsed.expired,
                                   "other_sites_skipped": parsed.foreign}])


def auto_import(session_path=None, folder=None, *, verify: bool = True, http=None) -> dict | None:
    """Import export files in the cookie folder that are newer than session.json.

    Returns the report, or ``None`` when there was nothing new. Importing rewrites session.json,
    so the same files are not imported twice. ``HARNESS_COOKIE_AUTO_IMPORT=0`` disables this."""
    if os.getenv("HARNESS_COOKIE_AUTO_IMPORT", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    path = Path(session_path) if session_path else default_session_path()
    files = discover(folder or cookie_dir(path))
    try:
        mark = path.stat().st_mtime if path.is_file() else 0.0
    except OSError:
        mark = 0.0
    fresh = [p for p in files if p.stat().st_mtime > mark]
    if not fresh:
        return None
    # Apply every file in the folder (oldest first) so a newer token-only file combines with older cookies.
    return import_files(files, session_path=path, verify=verify, http=http)


def describe(report: dict) -> list:
    """Human-readable lines for the terminal and CLI (no secret values)."""
    lines = []
    for source in report.get("sources", []):
        extra = []
        if source.get("expired_skipped"):
            extra.append(f"{source['expired_skipped']} expired skipped")
        if source.get("other_sites_skipped"):
            extra.append(f"{source['other_sites_skipped']} other-site skipped")
        lines.append(f"  read {source['file']} [{source['format']}]: {source['cookies']} cookies, token "
                     f"{'found' if source['token'] else 'not in file'}" + (f" ({', '.join(extra)})" if extra else ""))
    lines.append(f"  session: {report['cookies']} cookies, token {report['token']}, login check: {report['verified']}"
                 + (f" - {report['detail']}" if report.get("detail") else ""))
    if report.get("wrote"):
        lines.append("  session.json updated")
    if report.get("waf_expires"):
        lines.append("  aws-waf-token valid until " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(report["waf_expires"])))
    lines.extend(report.get("messages", []))
    return lines
