# Terminal modes

`python run.py` starts the **native coding harness**. Its commands (`/plan`, `/diff`, `/undo`, `/compact`, `/context`,
`/connect`, `/doctor`, ...) are listed by `/help` and documented in [harness.md](harness.md) and
[advanced-harness.md](advanced-harness.md).

`python run.py --legacy-chat` starts the **original DeepSeek web chat UI**: a plain chatbot on one DeepSeek chat session, with no
agent tools. The reference below is for that mode only, and the two modes have different command sets:

| You want to | Native (`python run.py`) | Legacy (`--legacy-chat`) |
|---|---|---|
| Sign in | `/connect`, `/refresh` | no sign-in command: sign in first (native `/connect` or `python import_session.py`), then start legacy chat |
| Manage sessions | `/sessions`, `/resume`, `/fork`, `/delete` | `/sessions`, `/load`, `/rename`, `/pin`, `/delete` |
| Manage context | `/compact`, `/context` (and automatic) | not available |
| Create API keys | set `BRIDGE_API_KEY` | `/api`, `/api-show`, `/api-delete` (keys are stored in `api_keys.json` and protect the API in either mode) |
| Think harder | `/think` | `/deepthink on\|off` |
| Search, upload, share, voices, limits | agent tools such as `web_search` and `web_fetch` | `/search`, `/upload`, `/share`, `/voices`, `/limits` |

Typing a legacy command such as `/api-show` in the native terminal prints "Unknown command"; that is expected.

> The web route is reverse-engineered, unofficial and may violate DeepSeek's Terms of Service; it can get an account blocked or
> suspended. See the [README](../README.md).

# Legacy web chat: command reference

All commands are typed in the **terminal chatbot** (the window where you ran `python run.py`).
Every command starts with `/`.  Commands are **case-insensitive**.
Anything you type that does **not** start with `/` is sent as a chat message.

---

## Quick reference

| Command | Purpose |
|---|---|
| `/new` | Start a fresh conversation |
| `/sessions` | List recent sessions (numbered) |
| `/load <id\|num>` | Resume a session by full ID or list number |
| `/rename <title>` | Rename the current session |
| `/pin` / `/unpin` | Pin or unpin the current session |
| `/delete` | Delete the current session (asks for confirmation) |
| `/model` | Show the current (single) model; old names are deprecated aliases |
| `/search on \| off` | Toggle web search |
| `/deepthink on \| off` | Toggle extended reasoning |
| `/upload` | Open file picker |
| `/share` | Create a public share link |
| `/api` | Create a new API key (prompts for name) |
| `/api-show` | List all API keys with names & values |
| `/api-delete <name>` | Delete an API key by name |
| `/history` | Show this session’s message history |
| `/retry` | Resend the last message |
| `/edit <id> <text>` | Edit a message + regenerate |
| `/export` | Trigger history export + show URL |
| `/status` | Show current state |
| `/whoami` | Show the signed-in DeepSeek account |
| `/limits` | Show server-side client settings & timeouts |
| `/voices` | List available TTS voices |
| `/stop` | Ask DeepSeek to stop the current reply |
| `/clear` | Clear the terminal screen |
| `/help` | Show the command box |
| `/q` `/quit` `/exit` | Quit |

**Added later in development:** `/edit <id> <text>`, `/export` (plus `GET /v1/user/settings`,
`GET /v1/export/download`, `POST /v1/sessions/<id>/edit` on the API side,
and a full HAR re-verification: `action:null`, `update_file` events,
`conversation_mode`, keepalive filtering, Chrome/154 unification).

**Added earlier in development:** `/whoami`, `/limits`, `/voices`, `/stop`, plus streamed
Deep Think reasoning rendered live in its own framed block.

---

## Conversation

---

### `/new`

**Purpose:** Start a brand-new conversation.  All previous context is cleared and a fresh DeepSeek session is created.  The model lock is also removed so you can switch models.

**Syntax:**
```
/new
```

**Example:**
```
You › /new
  ●  New session  #a1b2c3d4
  ◆  deepseek-chat  |  search on  |  think off  |  files yes  |  …a1b2c3d4
```

**Output:** Prints the last 8 characters of the new session ID and a status line.

---

### `/sessions`

**Purpose:** List your 20 most recent chat sessions from DeepSeek.  Useful for finding a session ID to pass to `/load`.  Pinned sessions are marked with a diamond (◆).

**Syntax:**
```
/sessions
```

**Example:**
```
You › /sessions

  Recent sessions  (latest first)
  ──────────────────────────────────────────────────────
  ●  1.  …a1b2c3d4  Python debugging session  (active)
      2.  …e5f6g7h8  Recipe ideas  ◆
      3.  …i9j0k1l2  (untitled)

  Use  /load <number>  to continue a session
```

**Output:** Numbered list showing a short session ID, the session title (up to 44 characters), a diamond (◆) for pinned sessions, and `(active)` on the current session.

---

### `/load <id|num>`

**Purpose:** Load an existing session and continue from where it left off.  Accepts either a full session UUID or the **list number** from the most recent `/sessions` output.  The model lock is cleared so you can adjust settings.

**Syntax:**
```
/load <session_id>
/load <list number>
```

**Examples:**
```
You › /load 3
  ●  Loaded session  …i9j0k1l2
  ◆  deepseek-chat  |  search on  |  think off  |  files yes  |  …i9j0k1l2
```
```
You › /load a1b2c3d4-e5f6-7890-abcd-ef1234567890
  ●  Loaded session  …7890abcd
  ◆  deepseek-chat  |  search on  |  think off  |  files yes  |  …7890abcd
```

**Output:** Confirms the loaded session ID and shows the status line.

> Tip: Run `/sessions` first to get the numbered list, then use `/load 2` to load by number.

---

### `/rename <title>`

**Purpose:** Give the current session a custom title.  The new title will appear in DeepSeek's web interface and in `/sessions` output.

**Syntax:**
```
/rename <new title>
```

**Example:**
```
You › /rename My Python project
  ✓  Session renamed to  My Python project
```

**Output:** Confirmation with the new title.

> Note: Requires an active session — run `/new` or send at least one message first.

---

### `/pin` / `/unpin`

**Purpose:** Pin the current session so it always appears at the top of DeepSeek’s session list, or remove a pin.

**Syntax:**
```
/pin
/unpin
```

**Example:**
```
You › /pin
  ◆  Pinned  …a1b2c3d4

You › /unpin
  ◇  Unpinned  …a1b2c3d4
```

**Output:** Confirmation with a filled diamond (◆) for pinned, empty diamond (◇) for unpinned.

> Note: Requires an active session.

---

### `/delete`

**Purpose:** Permanently delete the current session from DeepSeek.  Asks for confirmation before proceeding.

**Syntax:**
```
/delete
```

**Example:**
```
You › /delete
  ⚠  Delete session  …a1b2c3d4?  Type  yes  to confirm:  yes
  ✓  Session deleted.  Use  /new  to start a fresh session.
```

**Output:** Prompts for `yes` before deleting.  Any other input (or pressing Ctrl+C) cancels the operation.

> After deleting, use `/new` to create a new session.

---

## Models

Single-model backend (verified via `GET /api/v0/client/settings`
`scope=model`): only `deepseek-chat` (`Instant`) is enabled. `expert` and
`vision` are `enabled:false / switchable:false` — the website has no model
picker anymore.

---

### `/model`

**Purpose:** Show the current model. `/model expert` and `/model vision`
are accepted as deprecated aliases (same Instant model, with a warning) so
old habits don't error out.

**Syntax:**
```
/model
/model expert
```

**Example:**
```
You › /model
  ●  Model: deepseek-chat  (single Instant model — no picker on the website)

You › /model expert
  ⚠  'expert' was removed from the website (server: enabled=false).
     Staying on deepseek-chat — search/deepthink are independent toggles now.
```

---

## Features

---

### `/search on` / `/search off`

**Purpose:** Enable or disable web search.  When on, DeepSeek searches the web before answering — useful for current events and factual questions.

Independent toggle on the single Instant model (server default: on). Combines freely with deep think and file attachments.

**Syntax:**
```
/search on
/search off
```

**Example:**
```
You › /search off
  ✓  Web search disabled
```

**Output:** Confirmation and updated status line.

---

### `/deepthink on` / `/deepthink off`

**Purpose:** Toggle Deep Think (extended chain-of-thought reasoning).  When on, the model reasons through the problem step by step before answering — best for hard math, logic, and coding problems.  Responses take longer.

Independent toggle on the single Instant model (defaults off). Combines freely with web search and file attachments.

**Syntax:**
```
/deepthink on
/deepthink off
```

**Example:**
```
You › /deepthink on
  ✓  Deep Think enabled
```

---

### `/upload`

**Purpose:** Open a native file picker dialog so you can choose files to attach to your **next** message.  Files are held in a queue until you send a message, then uploaded and cleared.

Works on the single Instant model, combined with search and deep think.

**Syntax:**
```
/upload
```

**Example:**
```
You › /upload
  ✓  2 file(s) queued — will upload with your next message
    ·  report.pdf
    ·  screenshot.png
```

**Output:** Lists the queued files.  If you cancel the dialog, prints "No files selected".

> Other ways to attach files:
> - Drop files into the `file_uploads/` folder — uploaded automatically on your next message.
> - Type `@path/to/file` or `@"path with spaces"` anywhere in your message.

---

### `/share`

**Purpose:** Create a public share link for the current conversation on DeepSeek's website.  Anyone with the link can read the conversation without an account.

**Syntax:**
```
/share
```

**Example:**
```
You › /share
  ✓  Share link created:
     https://chat.deepseek.com/a/chat/s/abc123xyz
```

**Output:** The full public URL you can copy and share.

> Note: Requires at least one message in the current session.

---

### `/history`

**Purpose:** Print a compact summary of every message in the current session — useful for reviewing what was discussed before continuing.

**Syntax:**
```
/history
```

**Example:**
```
You › /history

  Conversation history  (4 messages)
  ────────────────────────────────────────────────────────
  You:    Write me a quicksort in Python.
  AI:     Sure! Here’s a clean quicksort implementation…
  You:    Now add type hints.
  AI:     Here’s the same function with full type hints…
```

**Output:** Each message is shown with role label and up to 90 characters of content.  Longer messages are truncated with …

> Note: Requires an active session.

---

### `/retry`

**Purpose:** Resend the last message you typed without retyping it.  Useful when a response was cut off or you want a fresh answer.

**Syntax:**
```
/retry
```

**Example:**
```
You › /retry
  ⤵  Retrying:  Write me a quicksort in Python.
  AI  ‹ deepseek-chat  ›  17:42
  ...
```

**Output:** Prints the message being retried, then streams a new response exactly as if you had typed the message again.

> Note: Only available after at least one message has been sent in the current session.

---

### `/status`

**Purpose:** Show the current model, web search setting, Deep Think setting, file support, and short session ID — all in one line.

**Syntax:**
```
/status
```

**Example:**
```
You › /status
  ◆  deepseek-chat  |  search on  |  think off  |  files yes  |  …a1b2c3d4
```

**Output:** One-line summary:

| Part | Meaning |
|---|---|
| `◆ deepseek-chat` | Current model (colour-coded) |
| `search on / off` | Web search state |
| `think on / off` | Deep Think state |
| `files yes / no` | Whether this model supports file upload |
| `…a1b2c3d4` | Last 8 characters of the session ID |

Also shows any pending files in the upload queue.

---

### `/whoami`

**Purpose:** Show which DeepSeek account you are signed in as.

**Syntax:**
```
/whoami
```

**Example:**
```
You › /whoami
  Email    ped***********@example.com
  User ID  eb1bd380-9b56-4cdc-acf5-82bdcea0667d
  Region   not mainland China
```

The email is masked by DeepSeek itself.  Useful for confirming the bridge is
using the account you expect before you start spending quota.

---

### `/limits`

**Purpose:** Dump the account's server-side client settings — the timeouts and
feature flags DeepSeek applies to this account.  Useful for debugging why a
request was cut short, or which features the account has enabled.

**Syntax:**
```
/limits
```

**Example:**
```
You › /limits
  ────────────────────────────────────────────────────────
  auto_resume_request_timeout_ms      3000
  chat_hcaptcha                        True
  completion_request_timeout_ms       60000
  normal_history_and_file_token_limit 890880
  pow_prefetch                         True
  search_state_on_launch               on
  …
  ────────────────────────────────────────────────────────
```

The settings that matter most in practice:

| Setting | Meaning |
|---|---|
| `completion_request_timeout_ms` | How long the server lets a reply run before cutting it off. A reply that stops abruptly near this mark is being truncated upstream — check whether the text you received is complete. |
| `normal_history_and_file_token_limit` | Context ceiling for Instant chat (the same number for `r1_…` on R1). Exceeding it is another way a reply gets cut short. |
| `auto_resume_request_timeout_ms` / `sse_auto_resume_timeout` | Grace period for resuming an interrupted reply. |
| `chat_hcaptcha` | Whether this account is subject to captcha challenges — a common cause of sudden auth failures. |

> DeepSeek does **not** expose remaining-quota counters, so this command
> cannot show "how many messages are left". When you hit a limit, the bridge
> surfaces the real upstream refusal — `rate_limit_reached` — rather than an
> empty reply.

---

### `/voices`

**Purpose:** List the text-to-speech voices DeepSeek offers.

**Syntax:**
```
/voices
```

**Output:** The available voices with their names and languages, e.g.:

```
You › /voices
  TTS voices  (4)
  ────────────────────────────────────────────────────────
  mira  mira
  …
```

---

### `/stop`

**Purpose:** Signal DeepSeek to stop generating the current reply.

**Syntax:**
```
/stop
```

Requests a stop for the message ID of the most recent streamed response, and
reports which message it targeted:

```
You › /stop
  ✓  Stop requested for message 4.
```

Run it when a reply is going long or off track.  Text already printed is kept.

If no response has been streamed yet in this session, `/stop` says so and does
nothing:

```
You › /stop
  ⚠  No completed response to stop — DeepSeek streams are stopped from the web UI.
```

> **Stopping mid-stream:** pressing `Ctrl+C` interrupts the reply immediately
> and keeps you in the session — no command needed.  `Ctrl+C` a second time
> quits.  `/stop` is for signalling a response that is still being generated
> upstream, not for cancelling local output.

---

### `/clear`

**Purpose:** Clear the terminal screen without exiting.  The current session and all settings are preserved.

**Syntax:**
```
/clear
```

**Output:** The terminal is cleared.  Nothing else changes.

---

### `/help`

**Purpose:** Print the command reference box to the terminal at any time — same box shown on startup.

**Syntax:**
```
/help
```

**Example:**
```
You › /help

  ╭────────────────────────────────────────────────────────────────────────╮
  │  ◆  DeepSeek Bridge  v1.0.0                                          │
  │  API  ·  http://localhost:11434/v1  ·  OpenAI-compatible              │
  ├────────────────────────────────────────────────────────────────────────┤
  │  Session                                                               │
  ├────────────────────────────────────────────────────────────────────────┤
  │  /new                         start a new conversation                │
  │  /sessions                    list recent sessions (then /load 3)     │
  │  /load <id|num>               load & continue an existing session     │
  │  /rename <title>              rename the current session               │
  │  /pin  /unpin                 pin or unpin the current session         │
  │  /delete                      delete the current session               │
  │  /share                       create a share link                      │
  ├────────────────────────────────────────────────────────────────────────┤
  │  Model & Features                                                      │
  ├────────────────────────────────────────────────────────────────────────┤
   │  /model                      show model (single Instant — no picker)  │
   │  /search on|off               toggle web search (independent)         │
   │  /deepthink on|off            toggle deep think (independent)         │
  ├────────────────────────────────────────────────────────────────────────┤
  │  API Keys                                                              │
  ├────────────────────────────────────────────────────────────────────────┤
  │  /api                         create a new API key                    │
  │  /api-show                    list all API keys                       │
  │  /api-delete <name>           delete an API key by name               │
  ├────────────────────────────────────────────────────────────────────────┤
  │  Files & Tools                                                         │
  ├────────────────────────────────────────────────────────────────────────┤
  │  /upload                      open file picker GUI                    │
  │  /history                     show this session’s message history    │
  │  /retry                       resend the last message                 │
  │  /status                      show current status                     │
  │  /clear                       clear the terminal screen               │
  │  /help                        show this help                          │
  │  /q  /quit  /exit             quit                                    │
  ├────────────────────────────────────────────────────────────────────────┤
  │  Attach:  @"path/to/file"  or drop files into  file_uploads/          │
  ╰────────────────────────────────────────────────────────────────────────╯
```

---

## API Keys

---

### `/api`

**Purpose:** Create a new named API key.  The bridge will prompt you to enter a name, then generate and display the key.  The key is stored in `api_keys.json` and can be used to authenticate API requests.

**Syntax:**
```
/api
```

**Example:**
```
You › /api
  Enter a name for this API key: my-laptop

  ✓  API key created!
     Name:  my-laptop
     Key:   dsb_a1b2c3d4e5f6...
  ⚠  Save this key — it won't be shown again!
```

**Output:** Prompts for a name, then displays the generated key once.  The key is saved to `api_keys.json`.

> Once at least one named key exists, the bridge requires `Authorization: Bearer <key>` on all `/v1/*` requests.

---

### `/api-show`

**Purpose:** List all API keys with their names and full key values.  Useful if you forgot a key and need to look it up.

**Syntax:**
```
/api-show
```

**Example:**
```
You › /api-show

  API Keys  (2 total)
  ────────────────────────────────────────────────────────────
  1.  my-laptop
     Key:     dsb_a1b2c3d4e5f6...
     Created: 2025-01-15 10:30

  2.  phone-app
     Key:     dsb_f6e5d4c3b2a1...
     Created: 2025-01-16 14:22
     Last used: 2025-01-17 09:15

  ────────────────────────────────────────────────────────────
```

**Output:** Numbered list of all keys with name, full key value, creation time, and last-used time.

---

### `/api-delete <name>`

**Purpose:** Delete an API key by name.  Once deleted, the key can no longer be used to authenticate.

**Syntax:**
```
/api-delete <name>
```

**Example:**
```
You › /api-delete old-key
  ✓  API key 'old-key' deleted.
```

**Output:** Confirmation message, or a warning if no key with that name exists.

---

## Quit

---

### `/q` / `/quit` / `/exit`

**Purpose:** Exit the bridge cleanly.  The API server (background thread) also stops.

**Syntax:**
```
/q
/quit
/exit
```

**Example:**
```
You › /q
  Goodbye!
```

---

## Inline file references

You can attach files directly inside your message text using `@`:

```
You › explain this code @"C:\Projects\main.py"
You › what is in this image @screenshot.png
You › summarise @report.pdf and @notes.txt
```

- Paths with spaces must be wrapped in double or single quotes: `@"my file.pdf"` or `@'my file.pdf'`
- Relative and absolute paths both work
- Multiple files can be referenced in one message
- Works with search and deep think combined

---

## API server extra fields

When sending requests to `POST /v1/chat/completions` from an external app, you can include these extra fields in the JSON body:

| Field | Type | Default | What it does |
|---|---|---|---|
| `thinking_enabled` | bool | `false` | Enable Deep Think for this request |
| `search_enabled` | bool | `true` | Enable or disable web search |
| `file_ids` | list | `[]` | List of pre-uploaded DeepSeek file IDs to attach |
| `stream` | bool | `false` | Stream the response as SSE |

**Example request:**
```json
{
  "model": "deepseek-chat",
  "messages": [{"role": "user", "content": "Prove the Pythagorean theorem."}],
  "thinking_enabled": true,
  "search_enabled": false,
  "stream": true
}
```
