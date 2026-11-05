"""Environment and provider diagnostics. Reports presence and status, never values."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sys
import time

from . import config

OK, WARN, FAIL = "ok", "warn", "fail"
ROOT = Path(__file__).resolve().parent.parent


def _module(name, dist=None):
    try:
        importlib.import_module(name)
    except Exception:
        return None
    try:
        from importlib import metadata
        return metadata.version(dist or name)
    except Exception:
        return "installed"


def _cookie_exports():
    """Note export files waiting in cookies/ (names only)."""
    from . import session_import
    folder = session_import.cookie_dir(ROOT / "session.json")
    files = session_import.discover(folder)
    if not files:
        return OK, f"no export waiting in {folder.name}/"
    session = ROOT / "session.json"
    mark = session.stat().st_mtime if session.is_file() else 0.0
    fresh = [p.name for p in files if p.stat().st_mtime > mark]
    if fresh:
        return WARN, f"{len(fresh)} export file(s) newer than session.json ({', '.join(fresh[:3])}); "                      "run python import_session.py or restart the harness to import them"
    return OK, f"{len(files)} export file(s) in {folder.name}/ already imported"


def _session_file():
    path = ROOT / "session.json"
    if not path.is_file():
        return FAIL, "session.json is missing; run python import_session.py (browser export) or python refresh_session.py"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return FAIL, "session.json is unreadable; run python refresh_session.py"
    if not data.get("userToken"):
        return FAIL, ("session.json holds no login token (cookie exports do not contain it); type /connect in the "
                      "harness and press Connect in the browser extension, or python refresh_session.py")
    expires = data.get("waf_expires") or 0
    left = expires - time.time()
    if expires and left <= 0:
        return FAIL, "browser/WAF cookies expired; run python refresh_session.py or import a fresh browser export"
    if expires and left < 86400:
        return WARN, f"cookies expire in {left / 3600:.1f} h; refresh soon with python refresh_session.py"
    return OK, "login token present" + (f"; cookies valid for {left / 86400:.1f} days" if expires else "")


def _loopback(host):
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


def exposure_warning():
    """Message when the API is reachable from the network without any API key, else ``None``."""
    from . import api_keys
    if _loopback(config.BRIDGE_HOST) or api_keys.key_exists() or config.BRIDGE_API_KEY:
        return None
    return (f"the API listens on {config.BRIDGE_HOST}:{config.BRIDGE_PORT} with no API key, so anyone on the network can "
            "use this agent; set BRIDGE_HOST=127.0.0.1 or configure BRIDGE_API_KEY")


def collect(runtime, live=False, check_port=False):
    """Return ``[(level, name, detail)]``. ``live`` makes one real provider request."""
    items = []
    add = lambda level, name, detail: items.append((level, name, detail))

    add(OK if sys.version_info >= (3, 10) else FAIL, "python", f"{platform.python_version()} at {sys.executable}")
    add(OK, "platform", f"{platform.system()} {platform.release()}")
    for name, dist in (("flask", "Flask"), ("requests", None), ("waitress", None), ("jsonschema", None), ("yaml", "PyYAML")):
        version = _module(name, dist)
        add(OK if version else FAIL, "dependency " + name, version or "missing; pip install -r requirements.txt")
    for name in ("wasmtime", "playwright"):
        version = _module(name)
        add(OK if version else WARN, "dependency " + name,
            version or "missing (needed for DeepSeek proof-of-work / login refresh)")

    registry = runtime.registry
    root = registry.root
    add(OK if root.is_dir() else FAIL, "workspace", str(root))
    try:
        probe = root / ".harness-write-probe"
        probe.write_text("x")
        probe.unlink()
        add(OK, "workspace writable", "yes")
    except OSError:
        add(FAIL, "workspace writable", "no; file tools cannot edit here")

    add(OK, "provider", f"{config.PROVIDER} / model {config.MODEL}")
    if config.PROVIDER == "deepseek-web":
        level, detail = _session_file()
        add(level, "deepseek session", detail)
        level, detail = _cookie_exports()
        add(level, "cookie exports", detail)
        add(OK, "deepseek credentials",
            "email and password present in .env (only used by refresh_session.py)" if config.DS_EMAIL and config.DS_PASSWORD
            else "not needed: the login comes from session.json (type /connect to renew it)")
    else:
        add(OK if config.PROVIDER_API_KEY else WARN, "provider api key", "set" if config.PROVIDER_API_KEY else "HARNESS_API_KEY is empty")
        add(OK, "provider base url", config.PROVIDER_BASE_URL)

    names = [t["function"]["name"] for t in registry.definitions]
    add(OK, "tools", f"{len(names)} enabled")
    add(OK if registry.command_policy != "deny" else WARN, "command policy",
        {"allow": "allow (commands run without asking)", "ask": "ask (terminal approves each; API runs refused)",
         "deny": "deny (command tools disabled)"}[registry.command_policy])
    add(OK, "web policy", registry.web_policy)
    if registry.command_deny or registry.command_allow:
        add(OK, "command rules", f"{len(registry.command_deny)} deny, {len(registry.command_allow)} allow "
                                 "(HARNESS_COMMAND_DENY / HARNESS_COMMAND_ALLOW)")
    shell = shutil.which("pwsh") or shutil.which("powershell") if os.name == "nt" else (shutil.which("bash") or shutil.which("sh"))
    add(OK if shell else WARN, "shell", shell or "no shell found; run_shell unavailable")
    add(OK if shutil.which("git") else WARN, "git", shutil.which("git") or "not installed; git tools unavailable")
    add(OK if (root / ".git").exists() else WARN, "git repository", "workspace is a repository" if (root / ".git").exists()
        else "workspace is not a repository; git_* tools report this")
    add(OK if shutil.which("node") else WARN, "node", shutil.which("node") or "not installed; JavaScript syntax validation unavailable")

    if config.PERSISTENCE:
        state = config.STATE_PATH
        add(OK if state.parent.exists() or state.parent.parent.exists() else WARN, "session database", str(state))
    else:
        add(WARN, "session database", "persistence disabled; sessions are lost on exit")
    logs = ROOT / "logs"
    add(OK if logs.is_dir() else WARN, "log directory", str(logs))

    catalog = registry.skills.catalog()
    add(OK, "skills", f"{len(catalog)} available" + (f", {len(registry.skills.errors)} invalid" if registry.skills.errors else ""))
    if registry.mcp.clients:
        add(OK, "mcp servers", ", ".join(registry.mcp.clients))

    warning = exposure_warning()
    add(WARN if warning else OK, "api exposure", warning or
        f"{config.BRIDGE_HOST}:{config.BRIDGE_PORT} " + ("(loopback only)" if _loopback(config.BRIDGE_HOST) else "(network-reachable, API key required)"))

    if check_port:
        with socket.socket() as probe:
            busy = probe.connect_ex(("127.0.0.1", config.BRIDGE_PORT)) == 0
        add(WARN if busy else OK, "api port", f"{config.BRIDGE_PORT} is " + ("already in use" if busy else "free"))

    if live:
        started = time.monotonic()
        try:
            client = getattr(runtime.provider, "client", None)
            if client is not None:
                client.ensure_authenticated()
                probe = client.create_session()
                # The probe chat would otherwise stay in the account's chat history forever.
                try:
                    client.delete_session(probe)
                except Exception:  # noqa: BLE001 - cleanup of a probe must never fail the check
                    pass
            else:
                import requests
                headers = {"Authorization": "Bearer " + config.PROVIDER_API_KEY} if config.PROVIDER_API_KEY else {}
                response = requests.get(config.PROVIDER_BASE_URL.rstrip("/") + "/models", headers=headers, timeout=15)
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}")
            add(OK, "live provider check", f"succeeded in {time.monotonic() - started:.1f}s")
        except Exception as exc:  # noqa: BLE001 - report the class only; bodies may hold secrets
            add(FAIL, "live provider check", f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:160] if str(exc).strip() else 'failed'}")
    return items


def format_report(items, color=False):
    marks = {OK: ("ok  ", "\033[32m"), WARN: ("warn", "\033[33m"), FAIL: ("FAIL", "\033[31m")}
    lines = []
    for level, name, detail in items:
        mark, code = marks[level]
        tag = f"{code}[{mark}]\033[0m" if color else f"[{mark}]"
        lines.append(f"{tag} {name}: {detail}")
    failures = sum(1 for level, _, _ in items if level == FAIL)
    warnings = sum(1 for level, _, _ in items if level == WARN)
    lines.append(f"{len(items)} checks: {failures} failed, {warnings} warnings")
    return "\n".join(lines)
