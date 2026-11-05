"""
bridge/api_keys.py — Named API key management for DeepSeek Bridge.

Stores multiple named API keys in a JSON file (api_keys.json).
Each key has: name, key value, created_at, last_used.

Terminal commands:
  /api <name>     — create a new API key with the given name
  /api-show       — list all API keys with names and full key values
  /api-delete <name> — delete an API key by name
  /api-revoke <name> — revoke (same as delete) an API key by name

The bridge server validates incoming requests against all stored keys.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

# ── Storage ───────────────────────────────────────────────────────────────────

API_KEYS_FILE = Path(__file__).parent.parent / "api_keys.json"

_lock = threading.Lock()

# In-memory cache.  `validate_key` used to re-read AND rewrite the whole file on
# every HTTP request (to stamp `last_used`), which serialised all concurrent
# requests behind a global lock and turned a few keys into a real bottleneck.
# We now cache the keys, validate against memory, and flush `last_used`
# lazily at most once per FLUSH_INTERVAL seconds.
_cache: dict | None = None
_cache_mtime: float = 0.0
_dirty: dict[str, int] = {}          # name -> last_used epoch
_last_flush: float = 0.0
FLUSH_INTERVAL = 30.0


def _mtime() -> float:
    try:
        return API_KEYS_FILE.stat().st_mtime
    except OSError:
        return 0.0


# True while api_keys.json exists but cannot be read as a key table.  Treating
# that as "no keys" would silently turn authentication OFF for a server whose
# operator created keys, so the file is reported as present and every request
# is rejected until it is repaired.
_unreadable: bool = False


def _load_keys() -> dict:
    """Load all API keys from disk. Returns {name: {key, created_at, last_used}}."""
    global _unreadable
    _unreadable = False
    if not API_KEYS_FILE.exists():
        return {}
    try:
        with open(API_KEYS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        data = None
    if not isinstance(data, dict) or any(
            not isinstance(entry, dict) or not isinstance(entry.get("key"), str) for entry in data.values()):
        _unreadable = True
        return {}
    return data


def _get_keys() -> dict:
    """Return the key table, reloading only when the file changed on disk.

    Callers that mutate must hold `_lock`.
    """
    global _cache, _cache_mtime
    m = _mtime()
    if _cache is None or m != _cache_mtime:
        _cache = _load_keys()
        _cache_mtime = m
    return _cache


def _save_keys(keys: dict) -> None:
    """Save all API keys to disk atomically."""
    global _cache, _cache_mtime
    tmp = API_KEYS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2)
    os.replace(tmp, API_KEYS_FILE)
    _cache = keys
    _cache_mtime = _mtime()
    _restrict_permissions()


def _restrict_permissions() -> None:
    """Best-effort: make the key file owner-readable only (POSIX)."""
    try:
        os.chmod(API_KEYS_FILE, 0o600)
    except (OSError, AttributeError, NotImplementedError):
        pass  # Windows / unsupported filesystem — nothing to do


def _flush_last_used(force: bool = False) -> None:
    """Persist buffered `last_used` timestamps, at most once per interval."""
    global _last_flush
    now = time.time()
    with _lock:
        if not _dirty:
            return
        if not force and (now - _last_flush) < FLUSH_INTERVAL:
            return
        keys = _get_keys()
        changed = False
        for name, ts in _dirty.items():
            if name in keys:
                keys[name]["last_used"] = ts
                changed = True
        if changed:
            _save_keys(keys)
        _dirty.clear()
        _last_flush = now


# ── Public API ────────────────────────────────────────────────────────────────

def create_key(name: str) -> str:
    """
    Create a new named API key.
    Returns the generated key value (shown once to the user).
    Raises ValueError if name is empty or already exists.
    """
    name = name.strip()
    if not name:
        raise ValueError("Key name cannot be empty")

    with _lock:
        keys = _get_keys()
        if _unreadable:
            raise ValueError("api_keys.json is unreadable; repair or delete it before creating keys")
        if name in keys:
            raise ValueError(f"API key '{name}' already exists")

        # Generate a secure random key: dsb_<48 hex chars>
        key_value = "dsb_" + secrets.token_hex(24)
        keys[name] = {
            "key": key_value,
            "created_at": int(time.time()),
            "last_used": None,
        }
        _save_keys(keys)
        return key_value


def list_keys() -> list[dict]:
    """Return all API keys as a list of dicts with name, key, created_at, last_used."""
    _flush_last_used()
    with _lock:
        keys = dict(_get_keys())
    result = []
    for name, data in keys.items():
        result.append({
            "name": name,
            "key": data["key"],
            "created_at": data.get("created_at"),
            "last_used": data.get("last_used"),
        })
    # Sort by creation time
    result.sort(key=lambda x: x["created_at"] or 0)
    return result


def delete_key(name: str) -> bool:
    """Delete an API key by name. Returns True if deleted, False if not found."""
    with _lock:
        keys = _get_keys()
        if name not in keys:
            return False
        del keys[name]
        _dirty.pop(name, None)
        _save_keys(keys)
        return True


def validate_key(key_value: str) -> bool:
    """Check if a given key value matches any stored API key.

    Records `last_used` in memory; the write is flushed lazily by
    _flush_last_used(), so a burst of requests does not hammer the disk.
    Comparison uses secrets.compare_digest to avoid leaking the key by timing.
    """
    if not key_value:
        return False
    matched = False
    with _lock:
        keys = _get_keys()
        for name, data in keys.items():
            stored = data.get("key", "")
            # Compare every key so the response time does not reveal which one matched.
            if stored and secrets.compare_digest(stored.encode(), key_value.encode()):
                _dirty[name] = int(time.time())
                matched = True
    if matched:
        _flush_last_used()
    return matched


def key_exists() -> bool:
    """Return True if at least one API key exists."""
    with _lock:
        return bool(_get_keys()) or _unreadable

