# Advanced native runtime

The native runtime implements these capabilities in Python, without another
coding harness. See `harness.md` for provider setup and the existing API contract.

## Start and resume

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe run.py
# Server only:
.venv/Scripts/python.exe run.py --api-only
# Native terminal without a server or API port:
.venv/Scripts/python.exe run.py --no-api
.venv/Scripts/python.exe run.py --no-api --resume YOUR_TERMINAL_SESSION_ID
.venv/Scripts/python.exe run.py --no-api -c   # the most recent terminal session
```

Use `.venv/bin/python` on POSIX systems.

Terminal commands:

| Command | Behavior |
|---|---|
| `!<command>` | Run a shell command yourself in the session folder (no approval: you typed it; the operator's deny rules and `HARNESS_ALLOW_COMMANDS=false` still apply) |
| `/delete [id]` | Delete this (or another) saved session and the chat it created on the DeepSeek website, then continue in a fresh session |
| `/sessions`, `/resume <id>` | List your durable sessions (newest first, titled from the first request, `*` marks the current one) and load one |
| `/fork` | Copy history, plan, skills and call ledger into an isolated session |
| `/cd <path>`, `/pwd` | Change/show this session's directory; bare `cd`/`pwd` also work |
| `/plan`, `/build` | Restrict agent actions to inspection/planning, or enable configured edits |
| `/skills`, `/skill <name>`, `/unskill <name>` | Discover and explicitly activate workflow skills |
| `/skills reload` | Rediscover installed skill metadata |
| `/mcp` | Show configured stdio servers and tool-name mappings |
| `/compact [focus]` | Summarize older steps now (the optional focus tells the summary what must survive); the transcript is never shortened |
| `/diff`, `/undo` | Review native file changes; restore the latest checkpoint if unchanged since the edit |
| `/export <path>` | Create a transcript JSON file in the workspace; refuses to overwrite an existing file |
| `/status` | Show mode, context character usage, skills and checkpoint count |
| `/checks` | Show observed check results and whether the evidence is stale |
| `/prompt` | Inspect the bundled core system prompt and factual runtime environment |

Old `/new`, `/load`, `/history`, `/model`, `/tools`, `/help` and `/quit` remain.
A lone `/` also shows help.
Ctrl+C requests cancellation. Terminal output stays owned by the terminal thread.

## Durable state and recovery

`HARNESS_PERSISTENCE=true` saves native sessions in
`HARNESS_WORKSPACE/.harness/state.sqlite3`. Override with `HARNESS_STATE_PATH` or
disable persistence for ephemeral sessions. State includes transcripts, titles,
skills, plans, current directory, private provider transport state, call results
and up to 20 file checkpoints per session. It is
private local data, excluded from the public exporter; it is not encrypted.
Transcript exports can contain sensitive conversation data and are explicit user
actions. Changing an API credential changes its session namespace.

SQLite commits use WAL and full synchronization. A process lock prevents two
harness processes from owning the same database. Use the terminal and API in
one process (`python run.py`), or different database paths for separate processes.
`HARNESS_MAX_SESSIONS` caps loaded sessions; idle sessions can be evicted from RAM
and loaded from disk again. Stored sessions remain until explicitly deleted.

Before an effect, the harness commits the call ID and a fully paired recovery
transcript. Results are committed before being emitted to clients. If the process
crashes after an effect but before recording its result, the ID remains uncertain
and is never automatically replayed. Inspect the workspace before deliberately
retrying with a new call ID. This is recovery journaling, not an atomic transaction
across the model, filesystem and external services.

## Context and instructions

`HARNESS_CONTEXT_CHARS=120000` limits approximate serialized input characters,
including tools and instructions, on native runs. This is a deterministic budget,
not a tokenizer or a promise about a provider's token limit. When the model's view
grows, three tiers apply to a *copy* sent to the model (the saved transcript is never
shortened): old bulky tool results become one-line stubs; older steps are folded into
a structured, model-written summary (merged with the previous summary on later
compactions; a deterministic digest of each step's call and the start of its result
is the fallback and is announced); recent inputs and complete assistant/tool-result
groups stay verbatim. `HARNESS_AUTOCOMPACT` (default 80, `0` = off) is the percentage
of the budget at which this runs before a turn; if steps would be dropped without a
summary covering them, a summary is always produced first. `/compact [focus]` does it
on demand, `/context` (and `GET /v1/sessions/<id>/context`) shows where the budget
goes, and `POST /v1/sessions/<id>/compact` accepts `{"focus": "..."}`. Tunables:
`HARNESS_AUTOCOMPACT_MAX`, `HARNESS_COMPACT_KEEP`, `HARNESS_MICROCOMPACT_MIN`,
`HARNESS_SUMMARY_CHARS`. Full canonical history stays available through `/history`
and API export. Oversized current turns or
mandatory instructions fail clearly instead of silently truncating input.
Client-owned `/v1/chat/completions` keeps the supplied transcript semantics.

Every native turn includes `prompts/coding-agent.md` (`coding-agent-v5`), including
in workspaces without instruction files. It specifies investigation, focused
changes, tool schemas, host-aware argv commands, verification, recovery and honest
completion. The runtime appends actual workspace, OS, Python executable and tool
availability, mode and session directory. Simple implementation requests use
reasonable defaults and proceed with edits and checks; optional framework/style
preferences do not require another user turn. The current user task survives
compaction through long tool chains.
This prompt is authored here; it is not copied from another coding agent.

Root `AGENTS.md` and `CLAUDE.md` load on every native model turn, each capped at
20,000 characters. Nested instruction files load when file tools visit their
directory. Instructions for the current session directory also load before its
first model turn. An edit encountering fresh scoped instructions returns them for review
before allowing a new call to apply the edit. Only instructions within the
configured workspace are loaded. Skills use metadata discovery, explicit body
activation and on-demand reference loading. See `skills/README.md` for provenance.

## Coding tools and plans

`working_directory` reports the workspace and this session's directory.
`change_directory` changes only session state, validates an existing directory
inside the workspace, and persists across resume/fork. Root confinement includes
symlink resolution. It does not call process-global `os.chdir`. Relative paths for
file tools, search/glob, `validate_files`, `run_checks` and command directories all start
from the session directory. For example, from `testing`, write `portfolio/index.html`,
not `testing/portfolio/index.html`; `..` may go up while the result stays inside the
workspace, and results use the same session-relative spelling. Git inspection always
runs at the workspace root (a `git_diff` path is resolved from the session directory).

`search_files` searches literal text with path/line results; `glob_files` lists
matching paths. Both skip common generated/private directories, symlinks and known
credential files, stop at 20,000 examined files or 200 results, and indicate
truncation. Glob matching uses Python `fnmatch` semantics. Search is literal unless `regex: true` is set (see below).
`HARNESS_MAX_FILE_BYTES=2000000` caps native text-file reads/edits and search files.

`apply_patch` accepts one workspace path and a standard unified diff. Hunk line
counts, order and exact context must match before the atomic file replacement.
`preview: true` returns a diff without writing. It handles empty-file insertion
and missing final newlines; it does not support renames, binary or multi-file
patches. Existing `write_file` and `replace_text` remain available.
LF patches can match CRLF files while preserving their existing newline style.
`read_file` preserves bytes when decoding UTF-8 and supplies a SHA-256 and
`next_line`/`next_column` pagination, including for a long single line.

`edit_file` applies an array of exact, unique replacements to one file atomically;
all replacements must match before writing. A replacement that matches several times is
rejected with the match count unless it sets `replace_all=true` (for renames), and a
replacement that matches nothing is always rejected. `replace_text` and `edit_file` accept
`expected_sha256` for optimistic conflict detection. `delete_file` removes one
regular file and requires a reason; `move_file` refuses an existing destination.
Both have checkpoints, and move undo checks both paths before restoring either.
Native tools refuse directory deletion, symlink move/delete, Git metadata and
the state database. Python edits clear matching generated bytecode caches so a
fast same-size change cannot silently run old code on the next check.

`git_status`, `git_diff` and `git_log` inspect a Git repository at the workspace
root, including in plan mode with arbitrary commands disabled. These use fixed
argv, disable external diff/text conversion and fsmonitor, and do not stage files.
Missing Git and a workspace without `.git` produce separate diagnostics. Native
editing, checkpoint review and validation work in a non-Git directory.

Native file edits save the original bytes and expected resulting hash. `/undo`
restores only if the current hash still matches, avoiding overwriting later manual
or other-session edits. Checkpoints survive restart. Forks inherit history/call
results but not ownership of earlier file checkpoints. Arbitrary subprocess or
MCP effects cannot be undone by file checkpoints. Native tools serialize against
one shared workspace; arbitrary external editors remain outside that lock.

Plan mode denies agent-issued file mutations, commands and MCP tools unless
explicitly classified read-only locally. Preview patches and `update_plan` remain
available. Switching to build enables the configured tools; command execution
is governed by `HARNESS_ALLOW_COMMANDS` (`ask` approvals in the terminal by default,
`true` to run unattended, `false` to remove the tools) and runs with OS permissions,
without a sandbox. `update_plan` permits at most one active step and requires a nonempty
evidence list for completed steps. Evidence is recorded text, not independent
proof that a test passed. Users can explicitly invoke checkpoint undo in either mode.

Plans may mark a step `blocked` with a nonempty reason for a real missing
requirement. In build mode, a plan updated during the current run with pending or
in-progress steps triggers up to two continuation turns instead of accepting an
early final answer. Repeated early answers end with `unfinished_plan` (409),
preserving edits/history. Plan mode may return a pending implementation plan.
An older plan does not force an unrelated new task to continue it. Native responses
report `task_status`: `completed`, `planned` or `blocked`. Plan completion text
is not independent evidence that an implementation satisfies every requirement.

## Shell, processes, web, questions and sub-agents

These tools use the permission policy in `harness.md`. API runs without an approver
get `permission_denied` for anything that needs approval.

- `run_shell {command, cwd?, timeout?}` runs a shell string with the host shell
  (PowerShell on Windows with UTF-8 output and a faithful exit status; `bash`/`sh`
  elsewhere). Prefer `run_command` (argv) for single programs. Both accept
  `timeout` (seconds, up to `HARNESS_MAX_TOOL_TIMEOUT`).
- `start_process {command|argv, cwd?}` starts a background process (dev server,
  watcher) and returns an id; `read_process {id, wait_seconds?, max_chars?}` returns
  new output and continues exactly where the last read stopped; `stop_process {id}`
  kills the process tree; `list_processes` lists them. Processes belong to the
  session that started them: other sessions cannot see, read or stop them, and they
  are killed when the session is deleted or the harness exits. Output is spooled to
  disk (16 MB cap).
- `web_fetch {url, max_chars?}` returns readable text for HTML/text/JSON pages (2 MB
  cap, up to 5 redirects, each hop checked). Private, loopback, link-local and
  metadata addresses are refused unless `HARNESS_WEB_ALLOW_PRIVATE=true`; name
  resolution is checked before connecting, which does not defend against a hostile DNS
  server that changes its answer. `web_search {query, max_results?}` uses the engine
  chain in `harness.md` and reports why each engine failed. Scraped search pages
  change without notice; a self-hosted SearXNG is the stable option. Page and result
  text is untrusted data.
- `ask_user {question, options?}` asks the person at the terminal (numbered options
  accepted). It is unavailable to API and one-shot runs, which return an error result
  so the model asks in its final answer instead. Waiting does not use the run deadline.
- `delegate_task {description, prompt, mode?}` runs a sub-agent in a fresh, isolated,
  short-lived session and returns its report. `plan` (default) is read-only; `build`
  may edit and is forced to `plan` when the lead is in plan mode. Sub-agents share the
  workspace, approver and "always" rules, cannot delegate again, have their own turn
  and time limits, and are cancelled with the lead. A build sub-agent's file
  checkpoints transfer to the lead so `/undo` covers them. Sub-agent tool names stream
  as `subagent_tool` events. They run one after another, not in parallel.
- `search_files` accepts `regex: true`, `context_lines` (0-5) and `max_results`.
  Patterns are Python regexes, limited to 500 characters; pathological patterns can
  still be slow.
- `validate_files` adds CSS structure (balanced blocks, closed comments/strings),
  JavaScript syntax via `node --check` when Node.js is installed (parse only; nothing
  executes), YAML, TOML and plain text (`.txt`, `.md`, `.csv`, ... : exists and is
  UTF-8, with that coverage stated). These remain static checks, not behavior tests.

## Terminal additions

| Command | Behavior |
|---|---|
| `/permissions [ask\|auto]` | Show policy and "always" rules; `auto` approves commands for this terminal session |
| `/think [on\|off]` | Toggle DeepSeek deep thinking for later turns |
| `/clear` | New session that keeps mode and directory (`/new` resets them) |
| `/cost` | Token usage seen this terminal session (DeepSeek web reports completion tokens only) |
| `/doctor [live]` | Environment/dependency/provider diagnostics; `live` makes one real request |
| `/compact [focus]`, `/context` | Summarize earlier steps (optional focus text) / show the context budget breakdown |
| `/connect [code]` | Sign in without the captcha: listen on `127.0.0.1:47123` until the bundled browser extension sends your signed-in session (Esc cancels; `code` adds a 6-digit pairing code) |
| `/refresh` | Re-import `cookies/` exports or run `refresh_session.py`, then reload the login |
| `/config` | Non-secret effective settings |
| `/init`, `/review`, `/explain <x>`, `/fix <x>` | Built-in prompt commands |
| `/<name> [args]` | Custom prompt command from `<workspace>/.harness/commands/<name>.md` (`$ARGUMENTS`, optional `description:` front matter). Plain text only; it cannot add tools or permissions |

Input: pasted multi-line text is sent as one message; `"""` starts a multi-line
block; a trailing `\` continues a line (not for `/` commands, `cd` or paths).
Output colours are used only on a real terminal (`NO_COLOR` disables them).
Provider failures print an actionable hint (for example `/connect` for a rejected
DeepSeek token). Stdout/stderr replace characters a legacy
console codepage cannot encode instead of crashing.

## Native tool-protocol notes

Providers return text; `bridge/toolprotocol.py` turns web-backend replies into
validated calls (JSON envelope, repaired JSON, DeepSeek DSML markup). Nothing runs
from an incomplete reply. Every decoded call still goes through schema validation,
plan-mode checks, approval and the crash journal described above.

## Observed verification and completion policy

`validate_files` is available with command execution disabled. It parses Python
with `ast` without imports/execution, strict JSON, and HTML document sections,
duplicate IDs, local fragments and referenced-file existence. It returns explicit
coverage, warnings and errors. HTML with a base URL requires a browser check;
unsupported extensions fail rather than imply coverage. CSS gets a structural
check and JavaScript a `node --check` parse when Node.js is installed; it does not
render pages or test application behavior. Records use the
same freshness rules as executed checks, with a whole-workspace fingerprint;
the tool currently has no explicit fingerprint scope for oversized workspaces.

With commands enabled, `run_checks` runs up to ten named argv checks, records exit
codes/durations and a workspace fingerprint, and returns bounded stdout/stderr.
It stops at the first failure by default (`fail_fast: false` runs every check).
The session keeps the last twenty records. A passing result means the supplied
commands exited zero; the runtime cannot determine whether those commands are
sufficient tests for the user's task. Review the check names and coverage.

Evidence becomes stale after a workspace mutation, a later arbitrary command or
remote mutation, or process restart. Fingerprints also detect manual edits. By
default hashing excludes generated/private directories and known credential,
HAR and log files; it covers at most 20,000 files and 32 MB. For larger projects,
pass `paths` with up to 200 relevant file paths (not directories). Explicit scopes
only establish freshness for those files. Unreadable/oversized inputs and inputs
changed during checks produce historical evidence, not a freshness claim.

Native API sessions/runs accept `require_verification: true`. After native file
changes, completion then requires a fresh passing `run_checks` or `validate_files`
record. A premature
answer triggers up to two correction turns (within `HARNESS_MAX_TURNS`), then a
`verification_failed` error with edits/transcript preserved. The policy requires
at least one verification tool in the selected tool set. New terminal sessions
enable it automatically when either tool is available; resumed sessions retain
their saved policy. This gate
covers native file edits, not arbitrary subprocess or remote service effects.
Streaming clients receive a `verification_required` event when correction begins.
Content that would ask an optional question or claim unverified completion after
a new build plan/required edits is buffered and discarded if the gate rejects it.
Tool progress remains visible. A blocked plan can explain the real blocker,
including after partial edits; it does not report successful task completion.

Completed call IDs always replay their saved result, including across restarts.
A fresh ID for a repeated effect is suppressed while activity and the bounded
workspace fingerprint remain unchanged; reads remain fresh. An intervening native
edit, command or detected manual change permits a deliberate new call. Uncertain
effects are never retried automatically. Schemas may use local references only;
validation never fetches remote schema resources.

Command output is disk-spooled and killed above
`HARNESS_MAX_COMMAND_OUTPUT_BYTES=8000000` (polling can overshoot briefly). Model
results are capped with truncation indicators while retaining structured exit
codes and error metadata. Configure `HARNESS_MAX_TOOL_OUTPUT` with enough room for
essential metadata; a tiny budget cannot shrink required IDs/status fields.

## Native MCP stdio integration

Set `HARNESS_MCP_CONFIG` to a local JSON file. Only this explicit configuration
starts server processes; request bodies and skills cannot install servers.

```json
{
  "servers": {
    "harness-mcp-v2": {
      "command": "C:/path/to/mcp-server.exe",
      "args": [],
      "env_keys": ["HARNESS_TOKEN"],
      "allow_tools": ["harness_list", "harness_get", "harness_describe"],
      "read_only_tools": ["harness_list", "harness_get", "harness_describe"],
      "timeout": 30
    }
  }
}
```

The executable is an illustrative path: install/configure your chosen server
separately and supply its actual documented command/args. `env_keys` passes named
environment variables, so credentials need not appear in config or prompts. The
client otherwise inherits only basic OS/runtime-discovery variables. Configured
server processes run with your OS permissions even when only read tools are exposed.
Store private config outside the public exporter (for example under `.harness/`).

The client negotiates MCP, sends `notifications/initialized`, discovers paginated
tool schemas and exposes only `allow_tools`. Tools receive stable names such as
`mcp_harness_mcp_v2__harness_list`. Locally configured `read_only_tools` governs
plan-mode access; untrusted server annotations never grant it. Missing allowed
tools or invalid schemas fail configuration. Server schema changes require restart.
Arguments validate before invocation. MCP results return to the model as tool data;
errors, deadline and cancellation participate in the native loop and crash journal.
Server stderr is suppressed to prevent logging credentials or corrupting terminal
output. Server-initiated capabilities such as sampling/elicitation are rejected.

This adapter implements the [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
and [tools lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).
HTTP/SSE transports, OAuth flows, resources/prompts, resumable remote jobs and
list-change rediscovery are not implemented. Live Harness.io account behavior
requires its own server/credentials and has not been verified here.

## API additions

Authentication and the request protections described in `harness.md` ("Local API protections") apply to every `/v1/*` route. Native sessions retain
API-key ownership, and terminal sessions are inaccessible to API callers.

| Route | Result |
|---|---|
| `GET /v1/skills` | Metadata and invalid-package diagnostics |
| `GET /v1/skills/<name>?resource=<relative path>` | Bounded skill resource |
| `GET /v1/mcp` | Server names and exposed tool mappings, without credentials |
| `POST /v1/sessions` | Create native session; optional `cwd`, `mode`, `skills`, `require_verification` |
| `PATCH /v1/sessions/<id>` | Title/pin plus `cwd`, `mode`, `skills`, `require_verification` |
| `GET /v1/sessions/<id>` | Canonical history, cwd, plan, skills, mode and context metrics |
| `POST /v1/sessions/<id>/fork` | New isolated session ID |
| `POST /v1/sessions/<id>/compact` | Summarize earlier steps now; optional `{"focus": "..."}`; returns what was folded and how |
| `GET /v1/sessions/<id>/context` | Context budget breakdown and the auto-compact threshold |
| `GET /v1/sessions/<id>/export` | Portable transcript JSON without internal checkpoints/call ledger |
| `GET /v1/sessions/<id>/review` | Native checkpoint diff |
| `GET /v1/sessions/<id>/checkpoints` | Checkpoint paths and indices |
| `POST /v1/sessions/<id>/undo` | Restore latest native file checkpoint or return 409 on conflict |
| `GET /v1/sessions/<id>/verification` | Observed check metadata with current freshness |

`POST /v1/agent/runs` also accepts `cwd`, `mode`, `skills` and `require_verification`; omitted values retain the
session's configuration. All session mutations reject an active run with 409.

```json
{
  "session_id": "your-session-id",
  "mode": "plan",
  "skills": ["harness-creator"],
  "messages": [{"role": "user", "content": "Inspect the project and propose a testable implementation plan"}],
  "stream": true
}
```

## Verification and boundaries

The development test suite (kept with the source, not part of the published repository)
exercises actual crash/restart recovery, two-process database exclusion, ownership,
plan restrictions, evidence rules, context/tool pairing, fresh reads after edits,
patch/checkpoint conflicts, native CLI/API actions and local MCP subprocess execution.
Credentials, cookies, the session database and logs are never published; bundled skills
keep their licenses and pinned provenance.

This runtime does not claim complete Claude Code/Codex parity. Provider
interfaces are DeepSeek web and streaming OpenAI-compatible Chat Completions; an
Anthropic Messages facade serves other clients (see `harness.md`) but there is no
Anthropic upstream provider. There is no parallel multi-agent scheduler, OS sandbox
or automatic account creation or login (signing in is a manual `/connect` handover of your own browser session).
