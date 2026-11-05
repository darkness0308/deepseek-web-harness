> **Educational use only.** The web route documented here reverse-engineers DeepSeek's private web protocol, is unofficial, and
> may violate DeepSeek's Terms of Service; it can get the account blocked or suspended. See the [README](../README.md) (the notice
> at the top and "Account risk and the official API").

# Archived original README (legacy web bridge)

This file keeps the detailed reference for the original **DeepSeek web bridge**: the `--legacy-chat` terminal UI, the web-session,
share, export and TTS endpoints, the HAR-derived protocol notes and the development history from before 1.0.0. The current native coding
harness is documented elsewhere:

| Topic | Where |
|---|---|
| Overview, quick start, sign-in, safety | [README](../README.md) |
| Native commands, configuration, API contracts, request protections | [harness.md](harness.md) |
| Sessions, plans, checkpoints, skills, MCP, verification, context management | [advanced-harness.md](advanced-harness.md) |
| Legacy chat commands | [commands.md](commands.md) |
| Web-route API reference | [api.md](api.md) |
| Bundled skills | [skills/README.md](../skills/README.md) |

The login, API, configuration and troubleshooting sections below were brought up to date for version 1.0.0; the "development history"
sections keep the original findings without release numbers. File paths in them are relative to the repository root.

## Legacy DeepSeek web chat reference

The following describes `python run.py --legacy-chat`. Use `?backend=web` for
remote session list/create/delete-all. The native guide describes the current API.

## DeepSeek Bridge (legacy reference)

Use your free DeepSeek web account as a local AI API — no API key or paid credits needed. This is the reverse-engineered route, so read the account-risk notice in the [README](../README.md) first; DeepSeek's official API is the supported alternative.

Runs a local server on port `11434` that speaks the OpenAI API format, so any chatbot app that supports OpenAI (Open WebUI, SillyTavern, Chatbox, etc.) works as a drop-in backend.  At the same time it gives you a full-featured terminal chatbot with markdown rendering, file upload, and session management.

---

## How it works

1. You sign in to [chat.deepseek.com](https://chat.deepseek.com) in your own browser (no automation, so no captcha).
2. The session (login token + cookies) is imported into `session.json` by the extension, `import_session.py`, the `cookies/` folder or `refresh_session.py`.
3. When your chatbot (or the built-in terminal) sends a message, the bridge forwards it to DeepSeek's website and streams the reply back in OpenAI SSE format.

---

## Requirements

- **Python 3.10 or newer** — [python.org/downloads](https://www.python.org/downloads/)
- **A free DeepSeek account** — [chat.deepseek.com](https://chat.deepseek.com)
- Windows (the `.bat` file is Windows-only; Python scripts work anywhere)

---

## One-time setup

### 1. Open a terminal in this folder

Right-click the folder → "Open in Terminal" (or `cd` to it).

### 2. Create a virtual environment and install packages

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

The sign-in script `refresh_session.py` drives your own installed Chrome or Edge, so no browser download is needed; only its
`--mode launch` with the bundled browser needs `.venv\Scripts\playwright install chromium`.

### 3. Create your configuration

```bat
copy .env.example .env
```

Every setting is documented in `.env.example`; the defaults work as they are. `DS_EMAIL` and `DS_PASSWORD` are optional and
normally unnecessary: the credential login endpoint sits behind the same captcha as the login page, so the login comes from step 4.
`.env` is private; never commit it.

### 4. Get a login into `session.json` (first time & whenever it expires)

DeepSeek's login endpoint is behind an AWS WAF challenge (the captcha) that rejects browsers started by
Playwright, so the harness no longer needs to log in by itself. Pick **one**:

| Way | Steps |
|---|---|
| **`/connect` + browser extension** (recommended) | Once: load `extension/deepseek-session-exporter` in Chrome/Edge (`chrome://extensions` > Developer mode > Load unpacked). Then, each time: sign in at chat.deepseek.com, type **`/connect`** in the harness, click the extension > **Connect**. While it waits the terminal shows a countdown instead of the `You >` prompt (**Esc** or Ctrl+C cancels); on success it prints `[connect] Connected` and the prompt returns |
| **Drop a file in `cookies/`** | Copy as cURL (bash) of any `chat.deepseek.com/api/v0/...` request (F12 > Network) saved as `cookies/request.txt`, or any export listed below; it is imported on start, on `/refresh` and when a token is rejected |
| **Cookie export + token** | Export cookies with Cookie-Editor into `cookies/`, then `python import_session.py --token-prompt` and paste the token from F12 > Console: `copy(JSON.parse(localStorage.userToken).value)` |
| **Guided browser sign-in** | `.venv\Scripts\python refresh_session.py` opens your own Chrome/Edge with a private profile; you sign in (and solve a captcha by hand if one shows); the script reads the session through the DevTools port |

A cookie export **alone cannot log in**: DeepSeek keeps the bearer token in `localStorage`, not in a cookie.
`import_session.py` tells you exactly that when the token is missing, and checks every import against
`GET /api/v0/users/current` before trusting it (it never replaces a working login with a rejected one).

---

## Logging in without the captcha

* **Formats understood** (`bridge/session_import.py`): the extension export, Cookie-Editor / EditThisCookie JSON,
  Playwright `storage_state`, Netscape `cookies.txt`, a `Cookie:` header, DevTools *Copy as cURL*, HAR files with
  headers, and `session.json` itself. Only `deepseek.com` cookies are kept; expired ones are skipped.
* **Commands**: `/connect [code]` in the harness (it waits, Esc cancels); `python import_session.py [files] [--token-prompt|--token-stdin] [--no-verify] [--dry-run] [--connect]`;
  `python refresh_session.py [--mode attach|launch] [--browser chrome|msedge] [--no-import]`; `/refresh` in the terminal.
* **`/connect` security**: it opens a listener on **127.0.0.1:47123** (not the API port 11434; `HARNESS_CONNECT_PORT`
  changes it, but the extension only talks to 47123) that exists only until you connect, type `/connect stop`, 120 s pass
  (`HARNESS_CONNECT_SECONDS`) or 3 attempts fail, or you press Esc. It serves only a loopback peer with a loopback `Host`, and only the
  extension with the pinned ID `fkllneljkdkoghfelacdfnglaoblmnhp` (derived from the public key in its manifest; web pages and
  other extensions are refused, `HARNESS_CONNECT_EXTENSION_IDS` adds IDs). The port is bound exclusively, bodies are
  capped at 512 KB, nothing is logged, replies carry counts and verdicts only. On a shared computer use `/connect code`:
  the terminal shows a 6-digit pairing code that you type into the extension (5 wrong codes close the listener).
* **Automatic**: files in `cookies/` newer than `session.json` are imported at startup and whenever DeepSeek
  rejects the token (`HARNESS_COOKIE_AUTO_IMPORT=0` turns this off, `HARNESS_COOKIE_DIR` moves the folder).
* **Privacy**: files in `cookies/` are live logins. They are git-ignored; delete them
  after importing. Nothing is logged or printed except cookie *names* and counts.
* **Limits**: the token lasts until you sign out or DeepSeek expires it; the harness cannot renew it by itself.
  If your network triggers the WAF on API calls too, re-export right after loading chat.deepseek.com so the
  `aws-waf-token` cookie is included.

## Context management (auto-compact, /compact, /context)

A long task fills the model's context. The harness manages it in three tiers, always on a copy that is sent to the model:
the saved transcript is never shortened, so `/export`, `/history` and resuming keep everything.

1. **Clear old tool results** (no model call). Bulky results from older steps (`HARNESS_MICROCOMPACT_MIN` characters or more,
   older than the last `HARNESS_COMPACT_KEEP` steps) become one-line stubs naming the tool, its arguments and the size. The
   model sees what it did and can re-run a tool if it needs the output again.
2. **Fold older steps into a model-written summary.** A structured handoff (requests and decisions, work completed with
   evidence, files, errors and fixes, facts learned, pending work and next step) replaces the oldest steps. Later compactions
   merge the previous summary with the new steps. If the model cannot produce a summary the harness falls back to a
   deterministic digest (each step's call and the start of its result) and says so.
3. **Automatic.** Before each model turn, when the view passes `HARNESS_AUTOCOMPACT` percent of `HARNESS_CONTEXT_CHARS`
   (default 80), tier 1 runs and, if that is not enough and folding saves a meaningful amount, tier 2. If steps are already
   being dropped without a summary covering them, the harness always summarizes them first (uncapped). You see
   `[context compacted: ...]` when it happens. `HARNESS_AUTOCOMPACT=0` turns it off.

Commands: `/context` (where the budget goes: instructions, tool definitions, conversation by role, summary, the largest tool
results, the auto-compact threshold); `/compact [focus]` (summarize now, keeping the last 4 steps; the optional focus text tells
the summarizer what must survive, e.g. `/compact keep the database schema and the failing test names`). API:
`GET /v1/sessions/<id>/context`, `POST /v1/sessions/<id>/compact` with an optional `{"focus": "..."}`.

Limits: the budget counts characters, not tokens; a summary costs one extra model request (a few seconds) and can omit a
detail, so important facts should also live in files or `update_plan`; the compactor treats the transcript as data, but a
summary of untrusted content is still derived from it.

## Running the bridge

### Double-click `start_bridge.bat`

Or from a terminal:

```bat
.venv\Scripts\python run.py
```

You will see the startup banner and command box, then the chat prompt. Type a message and press Enter — DeepSeek streams the reply token by token with full markdown rendering.

**The same chat session stays open for the entire run**, so the model remembers everything said earlier.  The **API server also runs in the background** at `http://localhost:11434/v1` the whole time.

---

## Terminal commands

Type `/help` at any time to see the full list. Quick reference:

| Command | What it does |
|---|---|
| `/new` | Start a fresh conversation (new session) |
| `/sessions` | List recent sessions (numbered for easy loading) |
| `/load <id\|num>` | Load a session by full ID or list number |
| `/rename <title>` | Rename the current session |
| `/pin` / `/unpin` | Pin or unpin the current session |
| `/delete` | Delete the current session (asks for confirmation) |
| `/model` | Show the current model (single Instant model — no picker) |
| `/search on\|off` | Toggle web search (independent toggle, default on) |
| `/deepthink on\|off` | Toggle Deep Think / extended reasoning (independent toggle) |
| `/upload` | Open GUI file picker |
| `/share` | Create a public share link for the current session |
| `/api` | Create a new named API key (prompts for name, or `/api my-laptop`) |
| `/api-show` | List all API keys with names & full values |
| `/api-delete <name>` | Delete an API key by name |
| `/history` | Show this session's message history |
| `/retry` | Resend the last message (with its attachments) |
| `/edit <id> <text>` | Edit a user message + regenerate |
| `/export` | Trigger history export + show download URL |
| `/status` | Show model, search, thinking, session ID |
| `/whoami` | Show the signed-in DeepSeek account |
| `/limits` | Show server-side client settings & timeouts |
| `/voices` | List available TTS voices |
| `/stop` | Ask DeepSeek to stop the current reply |
| `/clear` | Clear the terminal screen |
| `/help` | Show the full command box |
| `/q` `/quit` `/exit` | Quit |

Commands are case-insensitive. Anything that doesn't start with `/` is sent as a message.

With `/deepthink on`, the chain-of-thought streams live in its own dimmed,
italic block framed by rules, and closes just before the answer begins — so
reasoning never gets mistaken for the reply.

---

## File attachments

Three ways to attach files:

| Method | How |
|---|---|
| **GUI picker** | Type `/upload` — opens a native file dialog |
| **Drop folder** | Copy/move any file into `file_uploads/` — it uploads automatically on your next message |
| **Inline reference** | Type `@path/to/file` or `@"path with spaces"` anywhere in your message |

Uploaded files are moved to `file_uploads/Done/` after sending.

Files combine freely with search and deep think on the single model
(server `conflict_with_search=false`).

---

## Models

Single-model backend (verified via `GET /api/v0/client/settings`
`scope=model`): the server enables **only** `deepseek-chat`
(`Instant`, `is_default`, `enabled`, `switchable`). `expert` and `vision`
exist in the config but are `enabled:false / switchable:false`, and the web
UI no longer shows a model picker — search (`search_enabled`) and reasoning
(`thinking_enabled`) are independent per-message toggles. The bridge maps
the old `deepseek-expert` / `deepseek-vision` names to the same Instant
model so existing configs keep working; `GET /v1/models` lists only
`deepseek-chat`.

| Model | What it is | Search | Files | Think |
|---|---|---|---|---|
| `deepseek-chat` | DeepSeek Instant — the only enabled model | ✓ | ✓ | ✓ |

> The website advertises a "new Expert Mode" upgrade banner, but it is still
> disabled server-side. When it launches, a new `model_type` will appear in
> `scope=model` and the bridge will adopt it.

---

## API server (OpenAI-compatible)

Connect any OpenAI-compatible app while the bridge is running:

| Setting | Value |
|---|---|
| **Base URL** | `http://localhost:11434/v1` |
| **API Key** | Anything (e.g. `none`) — unless you set `BRIDGE_API_KEY` in `.env` |
| **Model** | `deepseek-chat` (single model; `deepseek-expert`/`deepseek-vision` map to it) |

### Optional API key protection

If you want to protect your bridge (e.g. when exposed on a LAN), set `BRIDGE_API_KEY` in `.env`:

```env
BRIDGE_API_KEY=my-secret-key
```

All requests to `/v1/*` must then include the header:

```
Authorization: Bearer my-secret-key
```

The health endpoints (`/`, `/health`, `/v1/health`) are always unauthenticated.

### Named API keys

For more flexible key management, use the built-in named key system.  Create keys from the terminal:

```
You › /api
  Enter a name for this API key: my-laptop
  ✓  API key created!
     Key: dsb_a1b2c3d4e5f6...
```

| Command | What it does |
|---|---|
| `/api` | Create a new named API key (prompts for name) |
| `/api-show` | List all keys with names and full values |
| `/api-delete <name>` | Delete a key by name |

Keys are stored in `api_keys.json` at the project root.  Once at least one named key exists, the bridge validates against those keys **instead of** `BRIDGE_API_KEY`.  Set `REQUIRE_API_KEY=true` in `.env` to require at least one key before the server starts.

You can also name the key inline instead of being prompted:

```
You › /api my-laptop
  ✓  API key created!
     Key: dsb_a1b2c3d4e5f6…
  Delete with  /api-delete my-laptop
```

### Conversations and memory

DeepSeek keeps conversation history on its own servers, keyed by session.  Originally
the bridge created a brand-new session on every HTTP request, so **the API
had no memory at all** — a follow-up question started from nothing.

Now you can hold a real conversation across calls:

```jsonc
// Turn 1
{ "model": "deepseek-chat",
  "session_id": "9bca8a8c-b41c-425a-bb81-9cb48f463b38",
  "messages": [{"role": "user", "content": "My favourite number is 731."}] }

// Turn 2 — same session_id, and the model remembers
{ "model": "deepseek-chat",
  "session_id": "9bca8a8c-b41c-425a-bb81-9cb48f463b38",
  "messages": [{"role": "user", "content": "What number did I say?"}] }
// → "731"
```

| Style | How | Use for |
|---|---|---|
| **Implicit** | Omit `session_id` — each request is isolated; include full message history | Stateless clients |
| **Explicit** | Pass `session_id: "<uuid>"` from `POST /v1/sessions` | Multi-user apps, anything that must not cross-contaminate |

Two different `session_id` values are fully isolated from each other.

### Development history: protocol re-verification (HAR 2026-10-01)

Re-verified every upstream endpoint against a fresh 372-request capture:

| Change | Detail |
|---|---|
| `action: null` in chat payload | Browser sends it on all 14 completions — now byte-identical |
| `update_file` SSE events parsed | File `SUCCESS` + `token_usage` + `signed_path` surfaced via `files` |
| `conversation_mode` tracked | `SEARCH` vs `DEFAULT` switch patch no longer ignored |
| `:` keepalive heartbeats skipped | Server emits them every ~8s (`x-ds-sse-heartbeat-timeout-secs`) |
| `Chrome/154` everywhere | `refresh_session.py` was on 148, client on 154 — unified |
| `deviceId` preserved on refresh | Old script dropped it, rotating `x-device-id` every refresh |
| Auto-login + manual fallback | `.env` credentials tried first, then manual (historical: replaced by browser-session import, see "Logging in without the captcha") |
| 3 missing bridge routes added | `GET /v1/user/settings`, `GET /v1/export/download`, `POST /v1/sessions/<id>/edit` |
| 2 terminal commands added | `/edit <id> <text>`, `/export` |
| `parse_har.py` rewritten | Base64 SSE decode, 16-endpoint surface, hif/files/gator/turnstile coverage (the tool is no longer part of the repository) |

Unchanged (verified still valid): PoW `DeepSeekHashV1` difficulty 144000,
`x-client-version 2.5.0`, `x-hif-*`/`x-settings-token` correctly omitted
(optional server-issued tokens), `model_type` explicit `default`
(`null` auto-mode accepted but not used), all session/share/feedback flows.

### Development history: single-model UI and toggle fixes

Verified via `GET /api/v0/client/settings` `scope=model` + Playwright
(`testing-cookies.json` login analysed — saved cookies are expired, so the
live check covered the logged-out flow + server config as source of truth):

| Change | Detail |
|---|---|
| Single Instant model | Only `default` is `enabled`/`switchable`; `expert`/`vision` are `enabled:false`. `/model` is now informational, old names are deprecated aliases |
| `/search` fixed | Was gated behind `vision`-exclusion logic — now a plain independent toggle (server default on) |
| `/deepthink` fixed | Independent toggle combining freely with search + files (`conflict_with_search=false`) |
| Uploads unified | Single file picker for docs + images; misleading "switch to Vision" warnings removed |
| `GET /v1/models` | Lists only `deepseek-chat`; `deepseek-expert`/`deepseek-vision` requests still map to Instant |

### Reasoning, truncation and empty replies

| Behaviour | What you'll see |
|---|---|
| `thinking_enabled: true` | Reasoning comes back separately as `reasoning_content` (both streaming and non-streaming) — never mixed into `content` |
| Reply cut short upstream | `finish_reason: "length"` instead of `"stop"`. **The text is partial** — the server truncated it. |
| DeepSeek refuses or rate-limits in-band | HTTP `502` with `code: "upstream_empty_response"` and the real reason. The bridge retries once, then fails loudly rather than returning a blank `200`. |

Previously an empty upstream reply produced `200` with empty `content`, which
clients could not distinguish from a real refusal.

### Browser access (CORS)

If you call the bridge from a web page, the origin must be allow-listed:

```env
BRIDGE_ALLOWED_ORIGINS=http://localhost:3000,http://localhost:5173
```

Only `/v1/*` is CORS-enabled, and credentials are not allowed.  An earlier
version reflected *any* `Origin` while permitting the `Authorization` header,
which let any page you visited spend your DeepSeek quota.

Beyond CORS, the server itself refuses a request whose `Origin` is not allow-listed (`403 origin_not_allowed`) and, while it is
bound to loopback, a request whose `Host` is not a loopback name (`403 host_not_allowed`, the DNS-rebinding defence). Clients
that send no `Origin` (SDKs, curl, scripts) are unaffected. The other protections (body size cap, wrong-key throttle, response
headers, refusal of an open network bind) are described in [harness.md](harness.md), "Local API protections".

### Supported endpoints

| Endpoint | Description |
|---|---|
| `GET /v1/models` | List available models |
| `POST /v1/chat/completions` | Chat — streaming and non-streaming |
| `GET /v1/sessions` | List chat sessions |
| `POST /v1/sessions` | Create a new session |
| `GET /v1/sessions/<id>` | Get full message history |
| `PATCH /v1/sessions/<id>` | Rename or pin/unpin a session |
| `DELETE /v1/sessions/<id>` | Delete a session |
| `DELETE /v1/sessions` | Delete all sessions |
| `POST /v1/sessions/<id>/stop` | Stop an in-progress stream |
| `POST /v1/sessions/<id>/regenerate` | Regenerate the last answer |
| `POST /v1/sessions/<id>/continue` | Continue a truncated response |
| `POST /v1/sessions/<id>/edit` | Edit a user message + regenerate |
| `POST /v1/sessions/<id>/feedback` | Like / dislike a message |
| `GET /v1/shares` | List shared conversations |
| `POST /v1/shares` | Create a share link |
| `DELETE /v1/shares/<id>` | Delete a share link |
| `GET /v1/user` | Get current user profile |
| `GET /v1/user/settings` | Get user settings |
| `POST /v1/export` | Trigger full chat history export |
| `GET /v1/export/download` | Poll export download URL |
| `GET /v1/tts/voices` | List available TTS voices |
| `GET /v1/client/settings` | Get client settings |
| `POST /v1/device/check` | Check device status |
| `POST /v1/client/settings/report` | Report client settings |

### Extra request body fields for `/v1/chat/completions`

| Field | Type | Default | Description |
|---|---|---|---|
| `thinking_enabled` | bool | `false` | Enable Deep Think mode |
| `search_enabled` | bool | `true` | Enable web search |
| `file_ids` | list | `[]` | Pre-uploaded DeepSeek file IDs to attach |

### Connect popular apps

**Open WebUI** — Settings → Connections → Add OpenAI-compatible → paste Base URL.

**SillyTavern** — API → OpenAI → Reverse Proxy → `http://localhost:11434/v1`

**Chatbox / any other app** — Look for "Custom / OpenAI-compatible" and enter `http://localhost:11434/v1`.

---

## Configuration reference

All settings live in the `.env` file at the project root.  Copy `.env.example` to get started.

| Variable | Default | Description |
|---|---|---|
| `DS_EMAIL` / `DS_PASSWORD` | — | Optional and normally unused (the credential login is behind the captcha); sign in with `/connect` instead |
| `BRIDGE_PORT` | `11434` | Port the API server listens on |
| `BRIDGE_HOST` | `127.0.0.1` | Host to bind. Loopback only by default; `0.0.0.0` serves your network, so set an API key first (the harness warns at startup and in `--doctor` when it is exposed without one) |
| `BRIDGE_THREADS` | `8` | Waitress worker thread count |
| `BRIDGE_API_KEY` | *(empty)* | Optional secret key for API authentication (legacy single-key mode) |
| `REQUIRE_API_KEY` | `false` | Set to `true` to require at least one named API key before server starts |
| `CONNECT_TIMEOUT` | `10` | Seconds to wait for TCP connection to DeepSeek |
| `STREAM_TIMEOUT` | `120` | Seconds to wait for the full SSE stream |
| `UPLOAD_TIMEOUT` | `60` | Seconds to wait for a file upload |
| `MAX_RETRIES` | `3` | Network retry attempts on connection errors |
| `RETRY_DELAY` | `1.5` | Base delay between retries (scales with attempt) |
| `LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `BRIDGE_ALLOWED_ORIGINS`, `BRIDGE_ALLOWED_HOSTS`, `BRIDGE_MAX_BODY_MB`, `BRIDGE_AUTH_FAILURE_LIMIT`, `BRIDGE_ALLOW_OPEN_NETWORK` | see `.env.example` | Request protections for the local API |
| `HARNESS_CONNECT_PORT`, `HARNESS_CONNECT_SECONDS`, `HARNESS_COOKIE_DIR`, `HARNESS_COOKIE_AUTO_IMPORT` | `47123`, `120`, `cookies/`, `1` | Sign-in handover settings |

The agent's own settings (`HARNESS_*`: provider, workspace, permissions, context budget, compaction) are listed in [harness.md](harness.md).

---

## Troubleshooting

### "AWS WAF token expired" or requests silently fail

Import a fresh browser session (extension, `cookies/` + `python import_session.py`) or run
`.venv\Scripts\python refresh_session.py` - see [Logging in without the captcha](#logging-in-without-the-captcha).

### Browser doesn't open / Playwright error

`refresh_session.py` uses your installed Chrome or Edge (set `DS_BROWSER_PATH` if it is in an unusual place); no
download is needed. For `--mode launch` with the bundled browser: `.venv\Scripts\playwright install chromium`.

### Port already in use

Open `.env` and change the port:

```env
BRIDGE_PORT=8080
```

Then update your chatbot's Base URL to `http://localhost:8080/v1`.

### `authentication token was rejected` / authentication fails

Sign in again at chat.deepseek.com, type `/connect` in the harness and click **Connect** in the browser extension. The harness
switches to the new login at once; no restart and no `.env` credentials are involved.

### `DeepSeek has muted this account's chat until ...`

DeepSeek restricted the account (often after heavy automated use). Nothing in the harness can lift it and it is never retried:
wait until the date shown. See "Account risk" in the [README](../README.md).

### Responses are empty or cut off

Your login has expired — export a fresh session (see above) or run `refresh_session.py` again. `python run.py --doctor --live` shows which part is rejected.

### `/model expert` says "removed from the website"

Correct — the server disabled `expert`/`vision` (`enabled:false`). The
bridge serves everything through the single Instant model. Your old config
still works: the names map to Instant automatically.

---

## File overview

```
run.py                  <- entry point: native terminal, API, --legacy-chat
start_bridge.bat        <- double-click shortcut (Windows)
refresh_session.py      <- sign in through your own browser, or import cookies/, then save the session
import_session.py       <- import a browser export (cookies/, extension file, cURL, token) into session.json
extension/              <- browser extension that sends your signed-in session to /connect
cookies/                <- drop browser exports here (private, git-ignored)
session.json            <- your session cookies and token (auto-generated, private)
api_keys.json           <- named API keys (auto-generated, private)
.env / .env.example     <- your private configuration / the documented template
prompts/, skills/       <- coding-agent system prompt / bundled skill packages
docs/                   <- guides, API reference, this archived README, screenshots (docs/images/)

bridge/
  agent.py              <- sessions and the model/tool loop        tools.py, processes.py, web.py <- tools
  providers.py          <- DeepSeek web and OpenAI-compatible      context.py, compaction.py      <- context management
  terminal.py           <- native terminal UI and commands         connect.py, session_import.py  <- sign-in handover
  bridge_server.py      <- Flask server: OpenAI/Anthropic APIs     security.py, api_keys.py       <- API protections and keys
  deepseek_client.py    <- reverse-engineered DeepSeek web client  config.py, doctor.py           <- settings, diagnostics
  sse_parser.py         <- parses DeepSeek's JSON-Patch SSE stream (shared by server and terminal)
  wasm_pow_solver.py    <- proof-of-work solver (WASM; sha3_wasm_bg.wasm is its module)

file_uploads/           <- drop files here to auto-attach on the next message (legacy chat)
file_uploads/Done/      <- files moved here after a successful upload
logs/                   <- harness.log and per-session log files
```
