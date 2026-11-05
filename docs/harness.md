# Native coding harness

`run.py` starts a Python-owned agent loop and the Flask API. It does not
launch or require Claude Code, Codex, or another harness. A DeepSeek web login
(`session.json`, created with `/connect`) is required only by the `deepseek-web`
provider; the `openai` provider needs just an API key. The project overview, quick
start and screenshots are in the [README](../README.md).

## Start

```powershell
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe run.py
# API without terminal input:
.venv/Scripts/python.exe run.py --api-only
# Original web chatbot, uploads, shares, and account commands:
.venv/Scripts/python.exe run.py --legacy-chat
```

The native terminal supports `/new`, `/clear`, `/sessions`, `/resume <id>`, `/history`,
`/model <name>`, `/think`, `/tools`, `/cd <path>`, `/pwd`, `/status`, `/cost`,
`/permissions`, `/compact [focus]`, `/context`, `/doctor`, `/connect [code]` (sign in
through the browser extension), `/refresh` (re-import `cookies/` exports or run
`refresh_session.py`, then reload the login in place), `/config`, `/help`, and `/quit`,
plus the session, plan, skill and prompt commands in
[Advanced native runtime](advanced-harness.md). The named-key commands `/api`,
`/api-show` and `/api-delete` exist only in `--legacy-chat` ([commands.md](commands.md)).
Bare `cd <path>` and `pwd` are local commands; `/` shows help. Ctrl+C cancels a run.
Pasted multi-line text is one message; `"""` opens a block and a trailing `\`
continues a line. Its model calls and tools run in a worker; only the terminal
thread reads input and writes output, which is also how approval prompts and
`ask_user` questions reach you. API diagnostics go to `logs/harness.log`.

Non-interactive use and diagnostics:

```powershell
# One prompt, then exit (exit code 0 success, 1 failure). Output: text | json | stream-json.
.venv/Scripts/python.exe run.py --no-api -p "Fix the failing test and run the suite" --auto-approve
.venv/Scripts/python.exe run.py --no-api -p "Review the open changes" --mode plan --output-format json
# Environment, dependency and provider check (add --live for one real provider request):
.venv/Scripts/python.exe run.py --doctor --live
```

`--auto-approve` skips approval prompts for command and web tools; without it a
one-shot run has nobody to ask and those tools are refused (see Permissions).

## Provider and tool configuration

Copy `.env.example` to `.env`. Existing shell variables take precedence.
Use `HARNESS_PROVIDER=deepseek-web` with a `session.json` login. DeepSeek's login page rejects automated browsers, so the
harness never logs in by itself: it imports the session of your own browser. Type `/connect` and click **Connect** in
`extension/deepseek-session-exporter` (README, "Sign in"), or use `python import_session.py`, the `cookies/` folder or
`python refresh_session.py`. Or set:

```dotenv
HARNESS_PROVIDER=openai
HARNESS_BASE_URL=https://your-provider.example/v1
HARNESS_MODEL=your-model-id
HARNESS_API_KEY=your-provider-key
HARNESS_WORKSPACE=C:/path/to/project
HARNESS_ALLOW_COMMANDS=ask
```

`HARNESS_API_KEY` authenticates upstream model requests. `BRIDGE_API_KEY` and
the existing named API keys authenticate incoming requests. They serve different
purposes. `BRIDGE_HOST` defaults to `127.0.0.1` (this machine only). Binding `0.0.0.0` exposes the
agent API to your network; configure `BRIDGE_API_KEY` or a named key first. An unreadable or
malformed `api_keys.json` keeps authentication enabled and rejects every request until it is
repaired, instead of silently serving without keys.

| Setting | Default | Behavior |
|---|---|---|
| `HARNESS_PROVIDER` | `deepseek-web` | `deepseek-web` or `openai` |
| `HARNESS_MODEL` | `deepseek-chat` | Default model; request `model` overrides it |
| `HARNESS_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI-compatible provider base URL |
| `HARNESS_API_KEY` | empty | Upstream Bearer credential |
| `HARNESS_WORKSPACE` | repository directory | Root for native file tools |
| `HARNESS_TOOLS` | all native built-ins | Comma-separated enabled tool names |
| `HARNESS_ALLOW_COMMANDS` | `ask` | `ask`: the terminal approves each command, API runs are refused; `true`: run without asking; `false`: command tools not offered |
| `HARNESS_ALLOW_WEB` | `ask` | Same three values for `web_fetch` / `web_search` |
| `HARNESS_COMMAND_DENY` | empty | Comma-separated command globs that are blocked under every policy (see "Command rules") |
| `HARNESS_COMMAND_ALLOW` | empty | Comma-separated command globs approved without asking under `ask` |
| `HARNESS_WEB_ALLOW_PRIVATE` | `false` | Allow `web_fetch` to reach private, loopback and link-local addresses |
| `HARNESS_SEARCH_ENGINE` | `auto` | `auto` (SearXNG if set, then DuckDuckGo, then Bing), `duckduckgo`, `bing` or `searxng` |
| `HARNESS_SEARXNG_URL` | empty | Self-hosted SearXNG search URL (JSON output enabled) |
| `HARNESS_MAX_TURNS` | `40` | Maximum model turns per native agent run |
| `HARNESS_PROTOCOL_RETRIES` | `2` | Corrections for malformed web tool JSON |
| `HARNESS_COMPLETION_CHECK` | `auto` | One self-check before a tool-using run finishes: `auto` (DeepSeek web only), `true`, `false` |
| `HARNESS_RATE_LIMIT_RETRIES` | `4` | Retries of a confirmed rate-limit refusal only (waits of 15, 30, 45 and 60 seconds by default) |
| `HARNESS_RATE_LIMIT_DELAY` | `15` | Backoff seconds, multiplied by attempt and capped at 60 |
| `HARNESS_PROVIDER_RETRIES` | `2` | Repeats of a DeepSeek web request that failed before any reply (connection error, timeout, 5xx) |
| `HARNESS_PROVIDER_RETRY_DELAY` | `1.5` | Backoff seconds, multiplied by attempt |
| `HARNESS_PROVIDER_CONCURRENCY` | `2` | Simultaneous completions sent to the DeepSeek web account |
| `HARNESS_TIMEOUT` | `600` | Cooperative run deadline, seconds; approval waits do not count |
| `HARNESS_TOOL_TIMEOUT` | `120` | Default per-command deadline, seconds |
| `HARNESS_MAX_TOOL_TIMEOUT` | `900` | Largest per-command `timeout` a call may request |
| `HARNESS_MAX_PROCESSES` | `8` | Background processes per session |
| `HARNESS_SUBAGENT_TIMEOUT` / `HARNESS_SUBAGENT_MAX_TURNS` | `240` / `20` | Limits for `delegate_task` sub-agents |
| `HARNESS_MAX_TOOL_OUTPUT` | `32000` | Tool-result text limit |
| `HARNESS_MAX_SESSIONS` | `256` | In-memory session limit; oldest idle session evicted |
| `HARNESS_PERSISTENCE` / `HARNESS_STATE_PATH` | `true` / `<workspace>/.harness/state.sqlite3` | Durable sessions and their database |
| `HARNESS_CONTEXT_CHARS` | `120000` | Characters the model may see (instructions, tools and conversation) |
| `HARNESS_AUTOCOMPACT` | `80` | Percent of that budget at which older steps are compacted before a turn (`0` = off) |
| `HARNESS_AUTOCOMPACT_MAX` / `HARNESS_COMPACT_KEEP` | `6` / `6` | Proactive compactions per run / recent steps whose tool results are never cleared |
| `HARNESS_MICROCOMPACT_MIN` / `HARNESS_SUMMARY_CHARS` | `1200` / `9000` | Smallest tool result that gets cleared / size cap of a summary |
| `HARNESS_CONNECT_PORT` / `HARNESS_CONNECT_SECONDS` | `47123` / `120` | `/connect` listener port and lifetime (the extension expects 47123) |
| `HARNESS_COOKIE_AUTO_IMPORT` / `HARNESS_COOKIE_DIR` | `1` / `cookies/` | Import browser exports from this folder at startup and when a token is rejected |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `11434` | API bind address and port |
| `BRIDGE_ALLOWED_ORIGINS` / `BRIDGE_ALLOWED_HOSTS` | local front-ends / empty | Browser origins allowed to call `/v1/*`; extra `Host` names accepted on a loopback bind |
| `BRIDGE_MAX_BODY_MB` / `BRIDGE_AUTH_FAILURE_LIMIT` | `64` / `20` | Largest request body; wrong keys per minute per address before a lockout (`0` = off) |
| `BRIDGE_ALLOW_OPEN_NETWORK` | `false` | Allow a network-facing bind with no API key (otherwise it refuses requests) |

Built-in tools: `working_directory`, `change_directory`, `list_files`, `read_file`,
`write_file`, `replace_text`, `edit_file`, `apply_patch`, `move_file`, `delete_file`,
`glob_files`, `search_files` (literal or regex, with context lines), `validate_files`,
`update_plan`, `list_skills`, `read_skill`, `git_status`, `git_diff`, `git_log`,
`run_command` (argv), `run_shell` (PowerShell on Windows, bash/sh elsewhere),
`run_checks`, `start_process` / `read_process` / `stop_process` / `list_processes`
(session-owned background processes), `web_fetch`, `web_search`, `ask_user`
(terminal only) and `delegate_task` (sub-agents), plus configured MCP tools. Details
are in [Advanced native runtime](advanced-harness.md). `read_file` accepts `start_line` and `max_lines` for paging.
`replace_text` requires an exact, unique match. Writes use atomic replacement.
Explicit file paths resolve inside the workspace, including symlink resolution.
Each native session has a durable `cwd`, initially `.`. Relative paths in every file
tool, search, `validate_files`, `run_checks` and a command's `cwd` start from it, as in
a shell: after `change_directory` into `site`, `write_file index.html` creates
`site/index.html`. `..` may leave the folder while the result stays inside the
workspace, absolute paths must resolve inside it, and results report paths relative to
the session folder so a path the model is shown can be passed straight back. (Before
this contract, explicit file paths stayed workspace-relative, so a file written right
after "go to the folder" landed in the workspace root; a live session showed that.)
Checkpoints, `/diff` and verification records stay workspace-relative internally.
Changing a session directory never changes the process directory or another session.
API sessions/runs accept `cwd` too.
`run_command` uses an argv array without a shell (bare names such as `npm` resolve
through `PATH`/`PATHEXT`); `run_shell` runs a shell string with the host shell.
Output is bounded and keeps both its head and its tail, because failures print last.
A command that exceeds its `timeout` is killed with its process tree and returns
partial output with `timed_out: true`; the run continues so the model can react. The
run deadline (`HARNESS_TIMEOUT`) still ends the run. Subprocess environments omit
`DS_EMAIL`, `DS_PASSWORD`, `HARNESS_API_KEY` and `BRIDGE_API_KEY`. The agent cannot
read, search, patch (including `apply_patch` previews), edit, move, delete or diff the harness's own
credential files (`.env`, `session.json`, `api_keys.json`, cookie files, `credentials.txt`, `.har`
captures, the state database).
Commands can access anything your OS account can; this is not a sandbox.

### Local API protections

The local API listens on loopback and is guarded against the things a browser or a guesser can do to it (`bridge/security.py`).
SDKs, curl and scripts are unaffected.

- **DNS rebinding.** While `BRIDGE_HOST` is loopback, the `Host` header must be `localhost`, `127.0.0.1` or `::1` (or listed in
  `BRIDGE_ALLOWED_HOSTS`); anything else gets `403 host_not_allowed`.
- **Other websites.** A request that carries a browser `Origin` must come from `BRIDGE_ALLOWED_ORIGINS` or the server's own origin
  (`403 origin_not_allowed`). Requests with no `Origin` are not browser requests and pass.
- **Brute force and abuse.** Bodies above `BRIDGE_MAX_BODY_MB` get `413`. `BRIDGE_AUTH_FAILURE_LIMIT` wrong keys per minute from
  one address lock that address out for a minute (`429 auth_throttled`, with `Retry-After`); a correct key resets the count.
- **Open network binds.** A non-loopback `BRIDGE_HOST` with no API key refuses requests (`503 open_network_refused`) unless
  `BRIDGE_ALLOW_OPEN_NETWORK=true`. Key comparison is constant-time.
- **Headers.** Responses carry `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and a
  restrictive `Content-Security-Policy`; API routes add `Cache-Control: no-store`.
- **`/connect`.** A separate listener on `127.0.0.1:47123`, open only while `/connect` waits. It serves a loopback peer with a
  loopback `Host` and only the pinned extension origin, accepts one connection, caps bodies at 512 KB and closes after
  `HARNESS_CONNECT_SECONDS` or three failed attempts; `/connect code` adds a 6-digit pairing code. Tokens are never printed or logged.

## Permissions

`HARNESS_ALLOW_COMMANDS` and `HARNESS_ALLOW_WEB` take `ask`, `true` or `false`.
With `ask` (default) the terminal shows what the agent wants to do and you answer
`y` (once), `a` (always for that program/host/command this session) or `n`.
Approval is requested before a call is journaled and never while holding the
workspace lock; time spent waiting does not count against the run deadline. API
runs and one-shot runs without `--auto-approve` have nobody to ask, so those
actions return a `permission_denied` tool result and the model must take another
route. Set `true` to permit them for API runs. `/permissions auto` approves
commands for the current terminal session only. Plan mode denies command tools
without prompting, and web tools still need approval in plan mode. A declined
action has no effect. Rules keyed to a program name (`python.exe`) cover any
arguments to that program; shell strings are remembered exactly.

## API contracts

Authentication, CORS and the protections under "Local API protections" apply to every endpoint.

| Endpoint | Behavior |
|---|---|
| `POST /v1/messages` | Anthropic Messages facade; client executes `tool_use` blocks (see below) |
| `POST /v1/responses` | OpenAI Responses facade (Codex-style clients); client executes `function_call` items (see below) |
| `POST /v1/messages/count_tokens` | Estimated input tokens for a Messages request |
| `POST /v1/chat/completions` | One model turn; client executes supplied function calls |
| `POST /v1/agent/runs` | Harness executes native tools and continues model turns |
| `GET /v1/tools` | Configured native function definitions |
| `POST /v1/sessions` | Create a native session; returns `session_id` |
| `GET /v1/sessions` | List this caller's native sessions |
| `GET /v1/sessions/<id>` | Native message history |
| `PATCH /v1/sessions/<id>` | Set `title` and/or `pinned` |
| `DELETE /v1/sessions/<id>` | Delete idle native session |
| `DELETE /v1/sessions` | Delete this caller's idle native sessions |
| `POST /v1/sessions/<id>/stop` | Cancel a native run; no message ID needed |
| `POST /v1/sessions/<id>/compact` | Summarize earlier steps now; optional `{"focus": "..."}` |
| `GET /v1/sessions/<id>/context` | Where the session's context budget goes and when auto-compact runs |

Use `?backend=web` for listing, creating, or deleting all remote DeepSeek
sessions. Existing remote session IDs continue to work with web history, metadata,
delete, stop, regenerate, continue, edit, feedback, and share routes. Remote IDs
are not native conversation IDs and cannot be used for native completions.

Without `session_id`, every completion starts an isolated local conversation.
Supply full `messages` for stateless use. For server memory, create a session or
reuse the returned `session_id`, then send the next messages. Non-streaming
responses include `session_id`; streaming responses include `X-Session-ID`.
Sessions belong to the incoming API credential. The terminal uses a separate
namespace. With auth disabled, possession of an explicit API session ID allows
continuation; unrelated implicit requests still never share history.

Two simultaneous runs on one session return `409 session_busy`; other sessions
can progress concurrently. State changes and deletion cannot race an active run.

### Client-executed function calling

Send `tools` in the standard function shape and optional `tool_choice` (`auto`,
`none`, `required`, or a named function). The bridge returns
`choices[0].message.tool_calls` and `finish_reason: "tool_calls"`. Execute those
functions in your client and send each result as a `role: "tool"` message with
the matching `tool_call_id`, preserving the assistant call message in stateless
requests. When using a native `session_id`, the assistant message is already
stored, so sending only the new tool result is sufficient. Then the model continues.

This flow follows the public
[function-calling contract](https://developers.openai.com/api/docs/guides/function-calling).
Legacy `functions`/`function_call` request fields are rejected with a clear error;
use `tools`/`tool_choice`.

### Command rules

`HARNESS_COMMAND_DENY` and `HARNESS_COMMAND_ALLOW` hold comma-separated glob patterns matched
case-insensitively against the command line (`run_command` argv joined with the program reduced to
its bare name, the `run_shell` string, each `run_checks` check and `start_process`). A **deny** rule
refuses the command under every policy, including `HARNESS_ALLOW_COMMANDS=true`, and is enforced
again by the executor; for shell strings it also matches each `&&`, `||`, `;` and `|` segment, so
`git push*` catches `cd app && git push`. An **allow** rule approves a command without asking when
the policy is `ask`, which lets an unattended API run use e.g. `pytest*`; every segment must match,
and a command containing `$(...)`, backticks or process substitution is never auto-approved. Rules
come from the operator's environment only, so a repository cannot grant itself permissions. Deny
rules are shown to the model in its runtime environment. This narrows what the agent may run; it is
not an OS sandbox.

### OpenAI Responses facade (Codex-style clients)

`POST /v1/responses` accepts the Responses request shape: `input` (a string, or message,
`function_call` and `function_call_output` items with text and image parts), `instructions`,
function `tools`, `tool_choice`, `max_output_tokens`, `temperature`, `top_p`,
`reasoning.effort` and `stream`. Replies are Responses objects (`message` and `function_call`
output items, `usage`, `incomplete` when truncated) or the standard SSE events
(`response.created`, `response.output_text.delta`, `response.function_call_arguments.*`,
`response.completed`, ...). Tool use is client-executed and requests are stateless, exactly like
the Anthropic facade. Not implemented: `previous_response_id` (resend the full input; it returns
`400 unsupported_parameter`), built-in and custom/freeform tool types (skipped), stored
responses and the retrieve/delete routes. It was verified with scripted protocol tests, not with
the real Codex CLI.

### Anthropic Messages facade (Claude Code and Anthropic SDK clients)

`POST /v1/messages` accepts the Anthropic Messages request shape (`system`,
`messages` with `text`, `image`, `tool_use` and `tool_result` blocks, `tools`,
`tool_choice`, `max_tokens`, `temperature`, `stop_sequences`, `stream`) and returns
Anthropic-shaped messages or SSE (`message_start`, `content_block_*`, `message_delta`,
`message_stop`). Tool use is client-executed: the model's `tool_use` blocks are
returned, and your client sends `tool_result` blocks back. Requests are stateless
and run in a short-lived session that is deleted afterwards. `x-api-key` and
`Authorization: Bearer` both authenticate; errors use the Anthropic error shape.
`claude-*` model names are mapped to the configured provider model.

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:11434"
$env:ANTHROPIC_API_KEY  = "<your BRIDGE_API_KEY or a named key>"
claude -p "Read hello.txt and summarize it" --allowedTools Read
```

Verified: the real Claude Code CLI (2.1.287) completed plain chat and a full
`Read` tool round trip through this route against a scripted OpenAI-compatible
backend. Not verified: Claude Code against the live
DeepSeek web backend (its login token had expired when this was tested), long
sessions, or any server-side Anthropic tool. Claude Code sends a very large system
prompt and tool list; the DeepSeek web backend must accept prompts that size and
follow its text tool protocol, which is best effort. Extended-thinking blocks, prompt
caching, batches, files and server-side tools are not implemented; `count_tokens`
and usage figures are estimates when the provider reports none. This is a
compatibility surface for other clients: the harness's own terminal and
`/v1/agent/runs` never depend on Claude Code.

### Harness-executed coding run

```python
import requests

result = requests.post("http://localhost:11434/v1/agent/runs", json={
    "messages": [{"role": "user", "content":
        "Read src/main.py, fix the reported bug, then explain the change."}],
    "stream": False,
}, headers={"Authorization": "Bearer YOUR_BRIDGE_KEY"}, timeout=360)
result.raise_for_status()
print(result.json()["choices"][0]["message"]["content"])
```

Omit `tools` to use configured native tools, or select an unchanged subset from
`GET /v1/tools`. API definitions cannot install executables or replace built-ins.
Argument schemas validate before execution. Tool errors become tool results the
model can correct. Repeated identical calls within a run are suppressed; repeated
call IDs return cached results, and an ID reused with changed arguments errors.
Non-streaming agent responses also include `tool_events`.

Streaming chat uses OpenAI-shaped SSE deltas, a stable completion ID, optional
usage, and `[DONE]`. Streaming agent runs add named `tool_start` and `tool_result`
events. Native runs also emit `continuation_required`, `verification_required`
and `provider_retry` when applicable. Content after a build plan or native edits
requiring checks is buffered until the model turn passes the completion gates;
rejected early answers are suppressed. The final native response includes
`task_status` (`completed`, `planned` or `blocked`). Other content deltas can
include intermediate assistant text; the final
completion chunk signals the end of the run. Errors after headers are sent use
an `object: "error"` frame followed by `[DONE]`, without a success finish chunk.

## Architecture and failure handling

`bridge/agent.py` owns sessions, locks, the model/tool loop, cancellation, limits,
and tool result history. `bridge/providers.py` adapts model transports;
`bridge/tools.py` owns built-in execution; `bridge/terminal.py` is the native CLI.
`bridge/bridge_server.py` adapts the same loop to HTTP. Logs contain run IDs,
session IDs, provider types, and lifecycle metadata, without prompts, model
responses, tool arguments, results, or credentials.

The original failure points were missing tool definitions and execution,
discarded tool-result messages, one implicit API conversation shared across
clients, state updates based on response IDs rather than valid answers, and
shared terminal/API diagnostics. The checkout had no `data` directory; its
diagnostic material was the 372-entry HAR, 23 endpoint analysis records, logs,
and probe scripts. All 15 captured completion/regenerate streams were replayed.

OpenAI-compatible providers stream content and assemble function-name and
argument fragments before returning complete calls. Malformed or unfinished calls
are never executed. DeepSeek's web transport has no structured tool interface:
the harness supplies a JSON response protocol with definitions in the prompt and
decodes the complete reply before any call runs (`bridge/toolprotocol.py`). The
decoder accepts the JSON envelope even when it is fenced, wrapped in prose, or has
raw newlines, unescaped inner quotes, invalid escapes or trailing commas (these are
repaired and the result still passes schema validation), single bare calls, and
DeepSeek's native `<｜｜DSML｜｜ invoke ...>` call markup, whose string parameters
need no escaping. Truncated or ambiguous replies are never executed. The
envelope's `content` text streams to the user while the model is still writing it.

Findings from live DeepSeek sessions are built into the decoder (each is covered by a regression
test in the development test suite, using raw replies recorded from the live model):

- **Bracket slips.** Deeply nested replies often drop or misplace a closer (`]}}}` where
  `]}}}]}` was meant). For a reply the model *finished*, missing closers are inserted and a
  closer with nothing to close is dropped. A reply cut off inside a string is never
  completed, even when it happens to end in `}`.
- **Answer-only envelopes.** `{"content": "..."}` without `tool_calls` is a final answer.
- **Invented continuation.** After a complete reply the model often keeps writing as if it
  were the transcript: a made-up `Tool result ...` line and even the next `Assistant:` turn.
  The reply is cut at the first such line outside a JSON string, nothing after it is ever
  decoded or executed, and the provider stops reading and asks DeepSeek to stop generating.
- **History in the reply format.** With tools active, earlier assistant turns are shown to
  the model as the same `{"content": ..., "tool_calls": [...]}` object it must produce,
  results name their tool, and tool output cannot forge a conversation turn.
- **Reply size cap.** One web reply is limited to roughly 23,000 characters in total. A larger
  `write_file` arrives cut off, so the model is told up front to write files in parts of at
  most 8,000 characters (`write_file` with `append=true`), and a rejected reply is sent back
  with a diagnosis ("the reply ended inside a text value after N characters", "the
  tool_calls array was closed early", "empty envelope", ...) built from the structure only.

A reply that still cannot be decoded receives at most `HARNESS_PROTOCOL_RETRIES` (two)
format-correction requests, without executing its calls or repeating previously completed
writes; the terminal and the streaming API announce each discarded reply
(`protocol_correction`). If the model then answers in plain prose with no call at all, that
prose is delivered as the answer. Corrections consume the agent's turn budget. Plain web chat still
streams text. Because this backend sometimes ends multi-step work early and then
describes the remaining steps as done, a tool-using build run gets one completion
self-check before it may finish (`HARNESS_COMPLETION_CHECK`), unless the run only read
things and the request asked for no change or sequence of steps ("ls the files"): the
runtime re-presents the user's request, and tools the request names explicitly but the run never called
are listed (up to three audits; negated mentions such as "don't use run_shell" are
ignored). This costs one extra model turn and reduces, but does not eliminate,
premature or unsupported completion claims. Web completions are limited to
`HARNESS_PROVIDER_CONCURRENCY` at a time because the account rate-limits bursts.
Complete local history is authoritative and is sent with no remote
parent, avoiding duplicate replay and stale upstream parent IDs.
The remote transport session is reused per native/local session, saved privately,
and not inherited by forks or included in transcript exports. It appears as a chat in
the DeepSeek website's history; it is deleted together with its local session (`/delete`
in the terminal, `DELETE /v1/sessions/<id>`, and automatically for the stateless
`/v1/messages` and `/v1/responses` calls), and `--doctor --live` deletes its probe chat.
If the chat is deleted on the website, the next request creates a new one. A web request
that fails before any reply arrives (connection error, timeout, HTTP 5xx) is repeated up to
`HARNESS_PROVIDER_RETRIES` times; a reply that already started is never repeated.

Cancellation and run deadlines are cooperative. Commands are polled and killed
with their process tree on timeout/cancellation. An HTTP operation already waiting
for data can take up to its transport timeout to exit; its session remains busy
until cleanup completes. Disconnects release the session and close the active
provider iterator. Tool batches retain completed results and interruption results
for unfinished calls, so later turns do not lose recorded side effects.

There are no automatic network retries of a possibly accepted model completion
or local tool side effect. Format corrections are new model turns with preserved
tool results. Retry transport failures after inspecting the error/history.
Existing web metadata network retry behavior remains separate.
Confirmed rate-limit refusals alone receive bounded backoff using
`HARNESS_RATE_LIMIT_RETRIES` and `HARNESS_RATE_LIMIT_DELAY`; the wait honors
cancellation and the run deadline. These rejected requests do not consume model
turns or replay tools. Set retries to zero to disable this behavior. A rejected
DeepSeek token is reported as `provider_authentication` with a hint to type `/connect`, even when
upstream returned HTTP 200 with an application error; before reporting it the harness reloads
`session.json` and retries once, in case a newer login was imported meanwhile. If DeepSeek has muted
the account's chat, the reply is `provider_account_muted` (HTTP 403 over the API) with the date shown
by DeepSeek, and it is never retried. A reused chat that was deleted on the website returns an empty
stream; the harness then starts a fresh chat and retries once.

## Verification and limits

The development test suite (583 tests as of 1.0.0) runs offline with temporary workspaces and fake
providers. It is kept with the source and is not part of the published repository. Credentials, cookies,
HAR captures, uploaded files and logs are never published.

Native sessions persist by default, and native runs use a bounded context: old tool results are
cleared and older steps are folded into a model-written summary (`/compact`, `/context`, automatic;
see "Context and instructions" in the advanced guide). See [Advanced native runtime](advanced-harness.md) for
resume/fork/export, plans, checkpoints, skills and optional MCP stdio tools.
The web backend remains subject to WAF/cookie expiry and model compliance
with its JSON tool protocol. Usage from that backend reports only the token count
it supplies, without fabricating prompt-token estimates. This harness exposes
OpenAI Chat Completions and an Anthropic Messages facade (see above) and does not
claim feature parity with Claude Code or Codex. Provider APIs that lack streaming
Chat Completions need a separate adapter.
