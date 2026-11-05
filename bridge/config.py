"""
bridge/config.py — Centralized configuration for DeepSeek Bridge.

Reads from environment variables.  If a  .env  file exists at the project
root it is loaded automatically (no third-party library required).

Priority (highest → lowest):
  1. Environment variable already set in the shell
  2. .env file in the project root
  3. Built-in default value

Usage
-----
    from . import config

    client = DeepSeekClient(config.DS_EMAIL, config.DS_PASSWORD)
    serve(app, host=config.BRIDGE_HOST, port=config.BRIDGE_PORT,
          threads=config.BRIDGE_THREADS)
"""

from __future__ import annotations

import os
from pathlib import Path

# ── .env loader (zero external dependencies) ──────────────────────────────────


def _load_dotenv() -> None:
    """Load variables from  .env  at the project root into os.environ.

    Existing environment variables are *never* overwritten.
    Handles quoted values (single or double) and inline comments.
    """
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if val.startswith(('"', "'")):
                quote = val[0]
                end = val.find(quote, 1)
                val = val[1:end] if end >= 1 else val[1:]
            else:
                val = val.split(" #", 1)[0].strip()
            if key and key not in os.environ:
                os.environ[key] = val


_load_dotenv()

# ── Metadata ──────────────────────────────────────────────────────────────────

VERSION  = "1.0.0"

# ── DeepSeek credentials ──────────────────────────────────────────────────────
# Set these in your  .env  file:
#   DS_EMAIL=you@example.com
#   DS_PASSWORD=your_password

DS_EMAIL    = os.getenv("DS_EMAIL",    "")
DS_PASSWORD = os.getenv("DS_PASSWORD", "")

# ── Server ────────────────────────────────────────────────────────────────────

BRIDGE_PORT    = int(os.getenv("BRIDGE_PORT",    "11434"))
# Loopback by default: the agent can edit files and (when permitted) run commands, so
# the API must not be reachable from the network unless the operator opts in AND
# sets an API key.  Use BRIDGE_HOST=0.0.0.0 to serve other machines.
BRIDGE_HOST    = os.getenv("BRIDGE_HOST",         "127.0.0.1")
BRIDGE_THREADS = int(os.getenv("BRIDGE_THREADS", "8"))
# A network-facing bind (anything but loopback) with no API key refuses requests unless this is true.
BRIDGE_ALLOW_OPEN_NETWORK = os.getenv("BRIDGE_ALLOW_OPEN_NETWORK", "false").lower() == "true"

# ── API key authentication ───────────────────────────────────────────────────
# Legacy single-key auth (still supported for backwards compatibility).
# If set, every request to /v1/* must include:
#   Authorization: Bearer <BRIDGE_API_KEY>
# Leave empty (default) to allow unauthenticated local access.
BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY", "")

# Named API key system.
# When api_keys.json exists and contains keys, the bridge validates against
# those keys INSTEAD of BRIDGE_API_KEY.  Use the terminal commands:
#   /api <name>       — create a new named key
#   /api-show         — list all keys
#   /api-delete <name> — delete a key
# Set to "true" to require at least one API key to exist before the server starts.
REQUIRE_API_KEY = os.getenv("REQUIRE_API_KEY", "false").lower() == "true"

# Origins allowed to call /v1/* from a browser (CORS).  Only needed for web
# front-ends — native and server-side clients are unaffected by CORS entirely.
# Scoped deliberately: reflecting any Origin while allowing the Authorization
# header would let any page the user visits spend this account's quota.
BRIDGE_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "BRIDGE_ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173",
    ).split(",") if o.strip()
]

# Extra Host header names accepted while the server is bound to loopback (comma-separated). Loopback
# names always work; add a name only if you reach the server through a local reverse proxy.
BRIDGE_ALLOWED_HOSTS = [h.strip() for h in os.getenv("BRIDGE_ALLOWED_HOSTS", "").split(",") if h.strip()]
# Largest request body the API accepts, in megabytes (413 above it).
BRIDGE_MAX_BODY_MB = max(1, int(os.getenv("BRIDGE_MAX_BODY_MB", "64")))
# Wrong API keys per client address per minute before that address is locked out for a minute
# (0 turns the throttle off).
BRIDGE_AUTH_FAILURE_LIMIT = max(0, int(os.getenv("BRIDGE_AUTH_FAILURE_LIMIT", "20")))

# ── Browser connect (/connect) ────────────────────────────────────────────────
# The terminal command /connect opens a short-lived listener on this loopback port so the browser
# extension can hand over a signed-in DeepSeek session. It is separate from the API port above and
# is not running unless /connect was typed. The extension expects 47123.
CONNECT_PORT = int(os.getenv("HARNESS_CONNECT_PORT", "47123"))
CONNECT_SECONDS = int(os.getenv("HARNESS_CONNECT_SECONDS", "120"))

# ── Timeouts (seconds) ────────────────────────────────────────────────────────

CONNECT_TIMEOUT = int(os.getenv("CONNECT_TIMEOUT", "10"))
STREAM_TIMEOUT  = int(os.getenv("STREAM_TIMEOUT",  "120"))
UPLOAD_TIMEOUT  = int(os.getenv("UPLOAD_TIMEOUT",   "60"))

# ── Retry ─────────────────────────────────────────────────────────────────────

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", "1.5"))

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Native harness. Credentials are read from the environment, never request bodies.
PROVIDER = os.getenv("HARNESS_PROVIDER", "deepseek-web")
MODEL = os.getenv("HARNESS_MODEL", "deepseek-chat")
PROVIDER_BASE_URL = os.getenv("HARNESS_BASE_URL", "https://api.deepseek.com/v1")
PROVIDER_API_KEY = os.getenv("HARNESS_API_KEY", "")
WORKSPACE = Path(os.getenv("HARNESS_WORKSPACE", str(Path(__file__).parent.parent))).resolve()
DEFAULT_TOOLS = ("working_directory,change_directory,validate_files,list_files,read_file,write_file,replace_text,"
                 "edit_file,delete_file,move_file,apply_patch,search_files,glob_files,update_plan,list_skills,"
                 "read_skill,git_status,git_diff,git_log,run_command,run_shell,run_checks,start_process,"
                 "read_process,stop_process,list_processes,web_fetch,web_search,ask_user,delegate_task")
ENABLED_TOOLS = [s.strip() for s in os.getenv("HARNESS_TOOLS", DEFAULT_TOOLS).split(",") if s.strip()]


def _policy(name, default):
    """Parse an allow/ask/deny setting; legacy true/false remain accepted."""
    aliases = {"true": "allow", "1": "allow", "yes": "allow", "on": "allow", "allow": "allow",
               "false": "deny", "0": "deny", "no": "deny", "off": "deny", "deny": "deny",
               "ask": "ask", "prompt": "ask"}
    raw = os.getenv(name, default).strip().lower()
    if raw not in aliases:
        raise ValueError(f"{name} must be true, false or ask")
    return aliases[raw]


# allow: run without asking | ask: the terminal user approves each action (API
# runs have nobody to ask and are refused) | deny: the tools are not offered.
COMMAND_POLICY = _policy("HARNESS_ALLOW_COMMANDS", "ask")
WEB_POLICY = _policy("HARNESS_ALLOW_WEB", "ask")
# Comma-separated glob rules matched against command lines (see bridge/permissions.py):
# deny refuses a command under every policy; allow approves it without asking under "ask".
COMMAND_DENY = os.getenv("HARNESS_COMMAND_DENY", "")
COMMAND_ALLOW = os.getenv("HARNESS_COMMAND_ALLOW", "")
WEB_ALLOW_PRIVATE = os.getenv("HARNESS_WEB_ALLOW_PRIVATE", "false").lower() == "true"
SEARCH_URL = os.getenv("HARNESS_SEARCH_URL", "https://html.duckduckgo.com/html/")
SEARCH_ENGINE = os.getenv("HARNESS_SEARCH_ENGINE", "auto").strip().lower()  # auto | duckduckgo | bing | searxng
BING_URL = os.getenv("HARNESS_BING_URL", "https://www.bing.com/search")
SEARXNG_URL = os.getenv("HARNESS_SEARXNG_URL", "")  # e.g. http://localhost:8080/search (format=json must be enabled)
MAX_AGENT_TURNS = int(os.getenv("HARNESS_MAX_TURNS", "40"))
PROTOCOL_RETRIES = int(os.getenv("HARNESS_PROTOCOL_RETRIES", "2"))
RATE_LIMIT_RETRIES = int(os.getenv("HARNESS_RATE_LIMIT_RETRIES", "4"))
RATE_LIMIT_DELAY = float(os.getenv("HARNESS_RATE_LIMIT_DELAY", "15"))
# Repeat a web completion that failed before any reply arrived (connection errors, timeouts, 5xx).
PROVIDER_RETRIES = int(os.getenv("HARNESS_PROVIDER_RETRIES", "2"))
PROVIDER_RETRY_DELAY = float(os.getenv("HARNESS_PROVIDER_RETRY_DELAY", "1.5"))
AGENT_TIMEOUT = float(os.getenv("HARNESS_TIMEOUT", "600"))
TOOL_TIMEOUT = float(os.getenv("HARNESS_TOOL_TIMEOUT", "120"))
MAX_TOOL_TIMEOUT = float(os.getenv("HARNESS_MAX_TOOL_TIMEOUT", "900"))
MAX_PROCESSES = int(os.getenv("HARNESS_MAX_PROCESSES", "8"))
COMPLETION_CHECK = os.getenv("HARNESS_COMPLETION_CHECK", "auto").strip().lower()  # auto | true | false
PROVIDER_CONCURRENCY = int(os.getenv("HARNESS_PROVIDER_CONCURRENCY", "2"))
SUBAGENT_TIMEOUT = float(os.getenv("HARNESS_SUBAGENT_TIMEOUT", "240"))
SUBAGENT_MAX_TURNS = int(os.getenv("HARNESS_SUBAGENT_MAX_TURNS", "20"))
MAX_TOOL_OUTPUT = int(os.getenv("HARNESS_MAX_TOOL_OUTPUT", "32000"))
MAX_SESSIONS = int(os.getenv("HARNESS_MAX_SESSIONS", "256"))

for _setting in ("MAX_AGENT_TURNS", "AGENT_TIMEOUT", "TOOL_TIMEOUT", "MAX_TOOL_TIMEOUT", "MAX_TOOL_OUTPUT",
                 "MAX_SESSIONS", "MAX_PROCESSES", "SUBAGENT_TIMEOUT", "SUBAGENT_MAX_TURNS", "PROVIDER_CONCURRENCY"):
    if globals()[_setting] <= 0:
        raise ValueError(f"Native harness setting {_setting} must be positive")
if COMPLETION_CHECK not in ("auto", "true", "false"):
    raise ValueError("HARNESS_COMPLETION_CHECK must be auto, true or false")
if PROTOCOL_RETRIES < 0:
    raise ValueError("HARNESS_PROTOCOL_RETRIES must be zero or positive")
if PROVIDER_RETRIES < 0 or PROVIDER_RETRY_DELAY < 0:
    raise ValueError("Provider retries and delay must be zero or positive")
if RATE_LIMIT_RETRIES < 0 or RATE_LIMIT_DELAY < 0:
    raise ValueError("Rate-limit retries and delay must be zero or positive")

PERSISTENCE = os.getenv("HARNESS_PERSISTENCE", "true").lower() == "true"
STATE_PATH = Path(os.getenv("HARNESS_STATE_PATH", str(WORKSPACE / ".harness" / "state.sqlite3")))
MCP_CONFIG = os.getenv("HARNESS_MCP_CONFIG", "")
CONTEXT_CHARS = int(os.getenv("HARNESS_CONTEXT_CHARS", "120000"))
# Context management (see bridge/context.py and bridge/compaction.py).
# AUTOCOMPACT_PERCENT: when the model's view passes this share of CONTEXT_CHARS, old tool results are cleared
# and, if that is not enough, earlier history is folded into a model-written summary. 0 turns it off.
AUTOCOMPACT_PERCENT = int(os.getenv("HARNESS_AUTOCOMPACT", "80"))
AUTOCOMPACT_MAX_PER_RUN = int(os.getenv("HARNESS_AUTOCOMPACT_MAX", "6"))   # proactive ones; mandatory ones are uncapped
COMPACT_KEEP_GROUPS = int(os.getenv("HARNESS_COMPACT_KEEP", "6"))      # recent steps whose tool results are never cleared
MICROCOMPACT_MIN_CHARS = int(os.getenv("HARNESS_MICROCOMPACT_MIN", "1200"))
SUMMARY_CHARS = int(os.getenv("HARNESS_SUMMARY_CHARS", "9000"))
MAX_FILE_BYTES = int(os.getenv("HARNESS_MAX_FILE_BYTES", "2000000"))
MAX_COMMAND_OUTPUT_BYTES = int(os.getenv("HARNESS_MAX_COMMAND_OUTPUT_BYTES", "8000000"))
SKILL_PATHS = [WORKSPACE / ".harness" / "skills", Path(__file__).parent.parent / "skills" / "vendor"]
if os.getenv("HARNESS_SKILL_PATHS"):
    SKILL_PATHS = [Path(p) for p in os.environ["HARNESS_SKILL_PATHS"].split(os.pathsep) if p]
if not 0 <= AUTOCOMPACT_PERCENT <= 95 or AUTOCOMPACT_MAX_PER_RUN < 0 or COMPACT_KEEP_GROUPS < 1 or \
        MICROCOMPACT_MIN_CHARS < 100 or not 1000 <= SUMMARY_CHARS <= 40000:
    raise ValueError("HARNESS_AUTOCOMPACT must be 0-95; HARNESS_COMPACT_KEEP >= 1; HARNESS_MICROCOMPACT_MIN >= 100; "
                     "HARNESS_SUMMARY_CHARS 1000-40000")
if CONTEXT_CHARS < 2000 or MAX_FILE_BYTES <= 0 or MAX_COMMAND_OUTPUT_BYTES <= 0:
    raise ValueError("HARNESS_CONTEXT_CHARS must be >=2000 and HARNESS_MAX_FILE_BYTES positive")
