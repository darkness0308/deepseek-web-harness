# Current native API

See [harness.md](harness.md) for the current completions and tool-calling contract.
`POST /v1/agent/runs` executes native tools. Sessions created through
`POST /v1/sessions` are native and in memory; requests omitting a session ID
are isolated. Use `?backend=web` for remote session list/create/delete-all.
Remote regenerate/edit/share endpoints remain web-provider operations.

## Legacy web endpoint reference

The examples below describe the original web surface. The native session and
isolation rules above replace its shared-memory behavior.

# API Reference (DeepSeek web routes)

Test every endpoint directly from the terminal using `curl.exe`.

> **Windows note:** In PowerShell use `curl.exe` (not `curl`, which is an alias for `Invoke-WebRequest`).  
> In Command Prompt use `curl` normally.

> **Added during development (protocol re-verification):** `POST /v1/sessions/<id>/edit`,
> `GET /v1/user/settings`, `GET /v1/export/download`, terminal `/edit` +
> `/export`, and a full re-verification against HAR 2026-10-01
> (`action:null`, `update_file` events, `conversation_mode`, keepalive
> filtering, Chrome/154, `deviceId` preservation, auto-login fallback).
>
> **Also added:** conversations persist between requests
> (`session_id`), reasoning is returned separately as `reasoning_content`,
> truncated replies report `finish_reason: "length"`, empty upstream replies
> fail loudly instead of returning blank text, and CORS is scoped to an
> allow-list rather than reflecting any origin.

---

## Setup

### Base URL

```
http://localhost:11434
```

Start the bridge first:

```bat
.venv\Scripts\python run.py
```

### Reusable variables (PowerShell)

Paste these once and reuse `$BASE` and `$SID` throughout your session:

```powershell
$BASE = "http://localhost:11434"
$SID  = "paste-your-session-id-here"
```

### Common headers

All `POST` / `PATCH` requests that send JSON need:

```
Content-Type: application/json
```

### Authentication

By default the API needs no key, because it listens on loopback only (browser requests with a foreign `Origin` or a non-local `Host` are still refused; see [harness.md](harness.md), "Local API protections").  If you set `BRIDGE_API_KEY` in your `.env` file, every request to `/v1/*` must include:

```
Authorization: Bearer <your-key>
```

The health endpoints (`/`, `/health`, `/v1/health`) are always public.

### Named API keys

When `api_keys.json` exists and contains keys, the bridge validates against those keys **instead of** `BRIDGE_API_KEY`.  Create and manage keys from the terminal:

| Command | What it does |
|---|---|
| `/api <name>` | Create a new named API key (prompts for name) |
| `/api-show` | List all API keys with names and full values |
| `/api-delete <name>` | Delete an API key by name |

Each key is stored in `api_keys.json` at the project root with its name, value, creation time, and last-used time.  Set `REQUIRE_API_KEY=true` in `.env` to require at least one key before the server starts.

---

## Endpoint index

| # | Method | Path | What it does |
|---|---|---|---|
| 1 | GET | `/` or `/health` | Basic health check |
| 1b | GET | `/v1/health` | Detailed health — version, port, auth status |
| 2 | GET | `/v1/models` | List available models |
| 3 | POST | `/v1/chat/completions` | Send a chat message |
| 3m | POST | `/v1/messages` | Anthropic Messages facade (Claude Code / Anthropic SDK); client-executed tools |
| 3n | POST | `/v1/messages/count_tokens` | Estimated input tokens for a Messages request |
| 3a | POST | `/v1/agent/runs` | Harness-executed coding run (see `harness.md`) |
| 4 | GET | `/v1/sessions` | List recent sessions |
| 5 | POST | `/v1/sessions` | Create a new session |
| 6 | GET | `/v1/sessions/<id>` | Get full message history |
| 7 | PATCH | `/v1/sessions/<id>` | Rename or pin a session |
| 8 | DELETE | `/v1/sessions/<id>` | Delete one session |
| 9 | DELETE | `/v1/sessions` | Delete ALL sessions |
| 10 | POST | `/v1/sessions/<id>/stop` | Stop an in-progress stream |
| 11 | POST | `/v1/sessions/<id>/regenerate` | Regenerate last answer |
| 12 | POST | `/v1/sessions/<id>/continue` | Continue a cut-off answer |
| 13 | POST | `/v1/sessions/<id>/feedback` | Like or dislike a message |
| 13b | POST | `/v1/sessions/<id>/edit` | Edit a user message + regenerate |
| 14 | GET | `/v1/shares` | List shared conversations |
| 15 | POST | `/v1/shares` | Create a share link |
| 16 | DELETE | `/v1/shares/<id>` | Delete a share link |
| 17 | GET | `/v1/user` | Get current user profile |
| 17b | GET | `/v1/user/settings` | Get user settings |
| 18 | POST | `/v1/export` | Trigger chat history export |
| 18b | GET | `/v1/export/download` | Poll export download URL |
| 19 | GET | `/v1/tts/voices` | List available TTS voices |
| 20 | GET | `/v1/client/settings` | Get client settings |
| 21 | POST | `/v1/device/check` | Check device status |
| 22 | POST | `/v1/client/settings/report` | Report client settings |

---

## 1. Health check

**Purpose:** Confirm the bridge is running and ready.

**Method & URL:**
```
GET /
GET /health
```

**curl:**
```powershell
curl.exe -s http://localhost:11434/health
```

**Example response:**
```json
{
  "status": "ok",
  "service": "deepseek-web-bridge"
}
```

---

## 1b. Detailed health

**Purpose:** Get version, port, and authentication status in one call.  Useful for monitoring or verifying configuration.

**Method & URL:**
```
GET /v1/health
```

**curl:**
```powershell
curl.exe -s http://localhost:11434/v1/health
```

**Example response:**
```json
{
  "status": "ok",
  "service": "DeepSeek Bridge",
  "version": "1.0.0",
  "port": 11434,
  "auth": "named_keys",
  "api_key_req": true
}
```

> `api_key_req: true` means `BRIDGE_API_KEY` is set and all `/v1/*` calls require `Authorization: Bearer <key>`.

---

## 2. List models

**Purpose:** List all models the bridge supports.  Use the `id` value as the `model` field in chat requests.

**Method & URL:**
```
GET /v1/models
```

**curl:**
```powershell
curl.exe -s http://localhost:11434/v1/models
```

**Example response:**
```json
{
  "object": "list",
  "data": [
    {
      "id": "deepseek-chat",
      "object": "model",
      "created": 1700000000,
      "owned_by": "deepseek-web-bridge",
      "description": "Instant — search, file upload and deep think are independent toggles"
    }
  ]
}
```

> Single-model backend: `deepseek-expert` / `deepseek-vision` are disabled
> server-side (`enabled:false`). Old configs requesting them still work —
> the bridge maps every name to the same Instant model.

---

## 3. Chat completions

**Purpose:** Send a message and get a reply.  The main endpoint — compatible with any OpenAI client library.

**Method & URL:**
```
POST /v1/chat/completions
```

### Request body fields

| Field | Type | Default | Description |
|---|---|---|---|
| `model` | string | `deepseek-chat` | Model to use (`deepseek-chat`; `deepseek-expert`/`deepseek-vision` map to it) |
| `messages` | array | — | Array of `{"role": "...", "content": "..."}` objects |
| `stream` | bool | `false` | Stream tokens as SSE (`true`) or return full reply at once (`false`) |
| `thinking_enabled` | bool | `false` | Enable Deep Think (independent toggle, combines with search + files) |
| `search_enabled` | bool | `true` | Enable web search (independent toggle, combines with think + files) |
| `file_ids` | array | `[]` | Pre-uploaded DeepSeek file IDs to attach |
| `session_id` | string | *(none)* | Pin the turn to a specific conversation — see [Conversations](#conversations-and-memory) |
| `stream_options` | object | — | `{"include_usage": true}` adds a final usage-only chunk |

### Roles accepted in `messages`

| Role | Meaning |
|---|---|
| `system` | System prompt (instructions for the model) |
| `user` | Human message |
| `assistant` | Previous AI reply (for multi-turn context) |

---

### Conversations and memory

**This changed during development.** Originally every HTTP request created a brand-new
DeepSeek session, so the API had **no memory at all** — asking a follow-up
question in a second request started from nothing.

Now a conversation persists server-side. Two ways to use it:

| Behaviour | How |
|---|---|
| **Implicit** — omit `session_id`. Each request is isolated; include full message history. |
| **Explicit** — pass `session_id: "<uuid>"`. The turn is chained onto that exact conversation. This is what you want for a chatbot with several users, or anything that must not cross-contaminate. |

Get a UUID from `POST /v1/sessions` (or `GET /v1/sessions` to list existing
conversations with their titles and last-used times).

```jsonc
// Turn 1 — seed the conversation
{ "model": "deepseek-chat",
  "session_id": "9bca8a8c-b41c-425a-bb81-9cb48f463b38",
  "messages": [{"role": "user", "content": "My favourite number is 731."}] }

// Turn 2 — same session_id, and the model remembers
{ "model": "deepseek-chat",
  "session_id": "9bca8a8c-b41c-425a-bb81-9cb48f463b38",
  "messages": [{"role": "user", "content": "What number did I say?"}] }
// → "731"
```

Two different `session_id` values are genuinely isolated — each keeps its own
history, and neither can see the other's turns.

To start over, just send a **new** `session_id`, or call
[`DELETE /v1/sessions/{id}`](#8-delete-one-session).

> `messages` is flattened into a single prompt before it is sent, so the server
> replays the real conversation history from DeepSeek's side. You do not need
> to resend prior turns — but sending them is harmless.

---

### Reasoning output

With `thinking_enabled: true` the chain-of-thought is returned separately from
the answer, never mixed into it:

| Path | Field | Appears when |
|---|---|---|
| Non-streaming | `choices[0].message.reasoning_content` | `thinking_enabled: true` |
| Streaming | `choices[0].delta.reasoning_content` | `thinking_enabled: true` |

`content` holds only the final answer. This split is what lets reasoning-aware
UIs (Cursor, Continue, OpenWebUI…) show thinking separately instead of dumping it
into the reply.

---

### `finish_reason` and truncation

| Value | Meaning |
|---|---|
| `stop` | DeepSeek reported the answer complete |
| `length` | The stream ended early — 60s upstream timeout, dropped connection, or a server-side cut-off. **The answer above is partial.** |

An empty reply is *never* reported as success. If DeepSeek returns no content —
including the in-band rate-limit and content-filter refusements it sends
mid-stream — the bridge retries once and then fails loudly:

```json
{
  "error": {
    "message": "DeepSeek returned no content: Messages zyada aa rahe hain... (rate_limit_reached) — retry the request",
    "type": "server_error",
    "code": "upstream_empty_response"
  }
}
```

HTTP `502`. Streaming clients receive the same information as a terminal
`object: "error"` frame before `[DONE]`, since the `200` status is already sent
by then.

---

### 3a. Simple message (non-streaming)

**curl:**
```powershell
curl.exe -s -X POST http://localhost:11434/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"deepseek-chat\",\"messages\":[{\"role\":\"user\",\"content\":\"What is 2+2?\"}]}'
```

**CMD version:**
```cmd
curl -s -X POST http://localhost:11434/v1/chat/completions ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"deepseek-chat\",\"messages\":[{\"role\":\"user\",\"content\":\"What is 2+2?\"}]}"
```

**Example response:**
```json
{
  "id": "chatcmpl-a1b2c3d4e5f6",
  "object": "chat.completion",
  "created": 1716100000,
  "model": "deepseek-chat",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "2 + 2 = 4."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": -1,
    "completion_tokens": -1,
    "total_tokens": -1
  }
}
```

---

### 3b. Streaming response

**Purpose:** Receive the reply token by token as Server-Sent Events (SSE).  Each line is a JSON chunk.  The last line is `data: [DONE]`.

**curl:**
```powershell
curl.exe -s -N -X POST http://localhost:11434/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"deepseek-chat\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Tell me a short joke.\"}]}'
```

> `-N` disables output buffering so you see tokens as they arrive.

**Example output (SSE stream):**
```
data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1716100000,"model":"deepseek-chat","choices":[{"index":0,"delta":{},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1716100000,"model":"deepseek-chat","choices":[{"index":0,"delta":{"content":"Why"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1716100000,"model":"deepseek-chat","choices":[{"index":0,"delta":{"content":" don't"},"finish_reason":null}]}

...

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1716100000,"model":"deepseek-chat","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

---

### 3c. Multi-turn conversation

**Purpose:** Hold a real conversation across separate HTTP calls.

**Preferred — pin a `session_id`** (the server keeps the history on DeepSeek's
side, so you only send the newest turn):

```powershell
# Turn 1 — create a conversation and remember its id
$sid = (curl.exe -s -X POST http://localhost:11434/v1/sessions `
  -H "Content-Type: application/json" -d '{}' |
  ConvertFrom-Json).session_id

curl.exe -s -X POST http://localhost:11434/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d "{`"model`":`"deepseek-chat`",`"session_id`":`"$sid`",`"messages`":[{
      `"role`":`"user`",`"content`":`"My name is Alex.`"}]}"

# Turn 2 — same session_id; the model remembers without resending anything
curl.exe -s -X POST http://localhost:11434/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d "{`"model`":`"deepseek-chat`",`"session_id`":`"$sid`",`"messages`":[{
      `"role`":`"user`",`"content`":`"What is my name?`"}]}"
```

**Example response:**
```json
{
  "choices": [
    {
      "finish_reason": "stop",
      "message": {
        "role": "assistant",
        "content": "Your name is Alex!"
      }
    }
  ]
}
```

**Also accepted** — replaying the whole conversation in one request, which
OpenAI libraries do by default:

```powershell
curl.exe -s -X POST http://localhost:11434/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"deepseek-chat\",\"messages\":[{\"role\":\"user\",\"content\":\"My name is Alex.\"},{\"role\":\"assistant\",\"content\":\"Nice to meet you, Alex!\"},{\"role\":\"user\",\"content\":\"What is my name?\"}]}'
```

See [Conversations and memory](#conversations-and-memory) for when to use which.

---

### 3d. With Deep Think + search disabled

**Purpose:** Extended reasoning without web search, on the single Instant model.

**curl:**
```powershell
curl.exe -s -X POST http://localhost:11434/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"deepseek-chat\",\"thinking_enabled\":true,\"search_enabled\":false,\"messages\":[{\"role\":\"user\",\"content\":\"Prove that the square root of 2 is irrational.\"}]}'
```

---

### 3e. With system prompt

**curl:**
```powershell
curl.exe -s -X POST http://localhost:11434/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"deepseek-chat\",\"messages\":[{\"role\":\"system\",\"content\":\"You are a pirate. Speak only in pirate dialect.\"},{\"role\":\"user\",\"content\":\"What is the weather like today?\"}]}'
```

---

## 3m. Anthropic Messages (`/v1/messages`)

Accepts the Anthropic Messages request shape and returns Anthropic-shaped responses
(`stream: true` gives `message_start`, `content_block_*`, `message_delta` and
`message_stop` events). Authenticate with `x-api-key` or `Authorization: Bearer`.
Errors are `{"type":"error","error":{"type":...,"message":...}}`. Tool use is
client-executed (`tool_use` out, `tool_result` back); requests are stateless. See
`harness.md` for the supported subset and the Claude Code setup.

```powershell
curl http://localhost:11434/v1/messages -H "x-api-key: $KEY" -H "content-type: application/json" `
  -d '{"model":"claude-sonnet-4-5","max_tokens":200,"messages":[{"role":"user","content":"Say hi"}]}'
```

## 4. List sessions

**Purpose:** Retrieve a list of your recent chat sessions (newest first).  The `id` field from each session is the `SESSION_ID` used in other endpoints.

**Method & URL:**
```
GET /v1/sessions?count=<n>&pinned=<true|false>
```

**Query parameters:**

| Parameter | Default | Description |
|---|---|---|
| `count` | `50` | How many sessions to return |
| `pinned` | `false` | Set to `true` to return only pinned sessions |

**curl:**
```powershell
# Latest 10 sessions
curl.exe -s "http://localhost:11434/v1/sessions?count=10"
```

```powershell
# Pinned sessions only
curl.exe -s "http://localhost:11434/v1/sessions?pinned=true"
```

**Example response:**
```json
{
  "object": "list",
  "data": [
    {
      "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "title": "Python debugging session",
      "pinned": false,
      "created_at": 1716100000,
      "updated_at": 1716103600
    },
    {
      "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
      "title": "Recipe ideas",
      "pinned": true,
      "created_at": 1716090000,
      "updated_at": 1716092000
    }
  ]
}
```

---

## 5. Create session

**Purpose:** Create a new empty chat session.  Returns the `session_id` you can use with `/v1/chat/completions` or other session endpoints.

**Method & URL:**
```
POST /v1/sessions
```

**Body:** none required.

**curl:**
```powershell
curl.exe -s -X POST http://localhost:11434/v1/sessions `
  -H "Content-Type: application/json" `
  -d '{}'
```

**Example response:**
```json
{
  "session_id": "c3d4e5f6-a7b8-9012-cdef-123456789012"
}
```

> HTTP status 201 Created.

---

## 6. Get session history

**Purpose:** Return the full message history for a session — every user and assistant message, including search results and thinking content.

**Method & URL:**
```
GET /v1/sessions/<SESSION_ID>
```

**curl:**
```powershell
$SID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
curl.exe -s "http://localhost:11434/v1/sessions/$SID"
```

**Example response:**
```json
{
  "chat_session": {
    "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "title": "Python debugging session",
    "pinned": false,
    "created_at": 1716100000
  },
  "chat_messages": [
    {
      "message_id": 1,
      "role": "USER",
      "content": "Why does my code crash?",
      "created_at": 1716100050
    },
    {
      "message_id": 2,
      "role": "ASSISTANT",
      "content": "The crash is likely caused by...",
      "status": "FINISHED",
      "created_at": 1716100055
    }
  ]
}
```

> The `message_id` integers are needed for `stop`, `regenerate`, `continue`, and `feedback`.

---

## 7. Update session (rename / pin)

**Purpose:** Change the title of a session, or pin/unpin it.  You can send either or both fields in the same request.

**Method & URL:**
```
PATCH /v1/sessions/<SESSION_ID>
```

**Request body fields:**

| Field | Type | Description |
|---|---|---|
| `title` | string | New title for the session |
| `pinned` | bool | `true` to pin, `false` to unpin |

**curl — rename:**
```powershell
$SID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
curl.exe -s -X PATCH "http://localhost:11434/v1/sessions/$SID" `
  -H "Content-Type: application/json" `
  -d '{\"title\":\"My Python project\"}'
```

**curl — pin:**
```powershell
curl.exe -s -X PATCH "http://localhost:11434/v1/sessions/$SID" `
  -H "Content-Type: application/json" `
  -d '{\"pinned\":true}'
```

**curl — rename and pin in one call:**
```powershell
curl.exe -s -X PATCH "http://localhost:11434/v1/sessions/$SID" `
  -H "Content-Type: application/json" `
  -d '{\"title\":\"Important project\",\"pinned\":true}'
```

**Example response:**
```json
{
  "status": "updated"
}
```

---

## 8. Delete one session

**Purpose:** Permanently delete a single session and all its messages.  This cannot be undone.

**Method & URL:**
```
DELETE /v1/sessions/<SESSION_ID>
```

**curl:**
```powershell
$SID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
curl.exe -s -X DELETE "http://localhost:11434/v1/sessions/$SID"
```

**Example response:**
```json
{
  "status": "deleted"
}
```

---

## 9. Delete ALL sessions

**Purpose:** Permanently delete every chat session in your account.  **This cannot be undone — use with caution.**

**Method & URL:**
```
DELETE /v1/sessions
```

**curl:**
```powershell
curl.exe -s -X DELETE http://localhost:11434/v1/sessions
```

**Example response:**
```json
{
  "status": "all_deleted"
}
```

> This calls DeepSeek's `delete_all` endpoint on your account.

---

## 10. Stop a stream

**Purpose:** Abort an in-progress streaming response for a given message.  Useful when the model is generating a very long reply and you want to stop it early.

**Method & URL:**
```
POST /v1/sessions/<SESSION_ID>/stop
```

**Request body fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `message_id` | int | yes | The `message_id` of the assistant message currently streaming |

**curl:**
```powershell
$SID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
curl.exe -s -X POST "http://localhost:11434/v1/sessions/$SID/stop" `
  -H "Content-Type: application/json" `
  -d '{\"message_id\":2}'
```

**Example response:**
```json
{
  "status": "stopped"
}
```

> Get the `message_id` from `GET /v1/sessions/<id>`.

---

## 11. Regenerate last answer

**Purpose:** Discard the last assistant reply and generate a new one.  Optionally adjust tone with `user_options`.

**Method & URL:**
```
POST /v1/sessions/<SESSION_ID>/regenerate
```

**Request body fields:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `child_message_id` | int | yes | — | `message_id` of the assistant message to regenerate |
| `model` | string | no | `deepseek-chat` | Model name for the response |
| `thinking_enabled` | bool | no | `false` | Enable Deep Think |
| `search_enabled` | bool | no | `true` | Enable web search |
| `stream` | bool | no | `true` | Stream the new answer as SSE |
| `user_options` | string | no | `null` | `"verbosity_high"`, `"verbosity_low"`, or `"search"` |

**curl (non-streaming):**
```powershell
$SID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
curl.exe -s -X POST "http://localhost:11434/v1/sessions/$SID/regenerate" `
  -H "Content-Type: application/json" `
  -d '{\"child_message_id\":2,\"stream\":false}'
```

**curl (streaming + more verbose answer):**
```powershell
curl.exe -s -N -X POST "http://localhost:11434/v1/sessions/$SID/regenerate" `
  -H "Content-Type: application/json" `
  -d '{\"child_message_id\":2,\"stream\":true,\"user_options\":\"verbosity_high\"}'
```

**Example response (non-streaming):**
```json
{
  "id": "chatcmpl-xyz",
  "object": "chat.completion",
  "model": "deepseek-chat",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Here is a more detailed explanation..."
      },
      "finish_reason": "stop"
    }
  ]
}
```

---

## 12. Continue a cut-off response

**Purpose:** Resume an assistant reply that was stopped or truncated before it finished.

**Method & URL:**
```
POST /v1/sessions/<SESSION_ID>/continue
```

**Request body fields:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `message_id` | int | yes | — | `message_id` of the assistant message to continue |
| `model` | string | no | `deepseek-chat` | Model name for the response label |
| `stream` | bool | no | `true` | Stream the continuation as SSE |

**curl (non-streaming):**
```powershell
$SID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
curl.exe -s -X POST "http://localhost:11434/v1/sessions/$SID/continue" `
  -H "Content-Type: application/json" `
  -d '{\"message_id\":2,\"stream\":false}'
```

**curl (streaming):**
```powershell
curl.exe -s -N -X POST "http://localhost:11434/v1/sessions/$SID/continue" `
  -H "Content-Type: application/json" `
  -d '{\"message_id\":2,\"stream\":true}'
```

**Example response (non-streaming):**
```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "...continuing from where I left off. The final step is..."
      },
      "finish_reason": "stop"
    }
  ]
}
```

---

## 13. Submit feedback

**Purpose:** Like or dislike an assistant message.  Optionally include a description explaining the rating.

**Method & URL:**
```
POST /v1/sessions/<SESSION_ID>/feedback
```

**Request body fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `message_id` | int | yes | `message_id` of the assistant message to rate |
| `feedback_type` | string | yes | `"LIKE"` or `"DISLIKE"` |
| `description` | string | no | Optional reason for the rating |

**curl — like:**
```powershell
$SID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
curl.exe -s -X POST "http://localhost:11434/v1/sessions/$SID/feedback" `
  -H "Content-Type: application/json" `
  -d '{\"message_id\":2,\"feedback_type\":\"LIKE\"}'
```

**curl — dislike with reason:**
```powershell
curl.exe -s -X POST "http://localhost:11434/v1/sessions/$SID/feedback" `
  -H "Content-Type: application/json" `
  -d '{\"message_id\":2,\"feedback_type\":\"DISLIKE\",\"description\":\"Answer was incorrect\"}'
```

**Example response:**
```json
{
  "status": "ok"
}
```

---

## 14. List shares

**Purpose:** List all publicly shared conversations in your account.

**Method & URL:**
```
GET /v1/shares?count=<n>
```

**Query parameters:**

| Parameter | Default | Description |
|---|---|---|
| `count` | `10` | How many shares to return |

**curl:**
```powershell
curl.exe -s "http://localhost:11434/v1/shares?count=10"
```

**Example response:**
```json
{
  "object": "list",
  "data": [
    {
      "share_id": "xYzAbC123",
      "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "title": "Python debugging session",
      "created_at": 1716105000,
      "url": "https://chat.deepseek.com/a/chat/s/xYzAbC123"
    }
  ]
}
```

---

## 15. Create share link

**Purpose:** Generate a public URL for a conversation.  Anyone with the link can read the chat without a DeepSeek account.  Optionally restrict to specific messages by providing `message_ids`.

**Method & URL:**
```
POST /v1/shares
```

**Request body fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | yes | The session to share |
| `message_ids` | array | no | Share only these message IDs. Empty (`[]`) shares the whole session |

**curl — share whole session:**
```powershell
$SID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
curl.exe -s -X POST http://localhost:11434/v1/shares `
  -H "Content-Type: application/json" `
  -d "{\"session_id\":\"$SID\",\"message_ids\":[]}"
```

**curl — share specific messages:**
```powershell
curl.exe -s -X POST http://localhost:11434/v1/shares `
  -H "Content-Type: application/json" `
  -d "{\"session_id\":\"$SID\",\"message_ids\":[1,2]}"
```

**Example response:**
```json
{
  "share_id": "xYzAbC123",
  "url": "https://chat.deepseek.com/a/chat/s/xYzAbC123"
}
```

> HTTP status 201 Created.

---

## 16. Delete share link

**Purpose:** Remove a public share link so it can no longer be accessed.

**Method & URL:**
```
DELETE /v1/shares/<SHARE_ID>
```

**curl:**
```powershell
$SHARE = "xYzAbC123"
curl.exe -s -X DELETE "http://localhost:11434/v1/shares/$SHARE"
```

**Example response:**
```json
{
  "status": "deleted"
}
```

---

## 17. Get current user

**Purpose:** Retrieve your DeepSeek account profile — name, email, avatar, plan details, and settings.

**Method & URL:**
```
GET /v1/user
```

**curl:**
```powershell
curl.exe -s http://localhost:11434/v1/user
```

**Example response:**
```json
{
  "id": 12345678,
  "name": "Alex",
  "email": "alex@example.com",
  "avatar_url": "https://cdn.deepseek.com/avatars/default.png",
  "mobile": "",
  "is_email_verified": true,
  "is_mobile_verified": false,
  "created_at": 1700000000
}
```

---

## 18. Export chat history

**Purpose:** Trigger a full export of all your chat history.  DeepSeek generates a downloadable archive in the background.  Poll `GET /v1/user` or re-call this endpoint to check the `download_url` once it's ready.

**Method & URL:**
```
POST /v1/export
```

**Body:** none required.

**curl:**
```powershell
curl.exe -s -X POST http://localhost:11434/v1/export `
  -H "Content-Type: application/json" `
  -d '{}'
```

**Example response:**
```json
{
  "status": "triggered",
  "data": {
    "exported_at": 1716105000,
    "expires_at": 1716191400,
    "download_url": "https://cdn.deepseek.com/exports/export_abc123.zip"
  }
}
```

> If `download_url` is `null`, the export is still being prepared — call again in a few seconds.

---

## 19. List TTS voices

**Purpose:** Get available text-to-speech voices from DeepSeek.

**Method & URL:**
```
GET /v1/tts/voices
```

**curl:**
```powershell
curl.exe -s http://localhost:11434/v1/tts/voices
```

**Example response:**
```json
{
  "voices": [
    {"id": "voice-1", "name": "Default", "language": "en"},
    {"id": "voice-2", "name": "Warm", "language": "en"}
  ]
}
```

---

## 20. Get client settings

**Purpose:** Retrieve client-side settings from DeepSeek (feature flags, UI config, etc.).

**Method & URL:**
```
GET /v1/client/settings?did=<device_id>&scope=<scope>
```

**Query parameters:**

| Parameter | Default | Description |
|---|---|---|
| `did` | `""` | Device ID |
| `scope` | `main` | Settings scope: `main`, `model`, `web_upgrade`, `provider`, `banner` |

**curl:**
```powershell
curl.exe -s "http://localhost:11434/v1/client/settings?scope=main"
```

**Example response:**
```json
{
  "settings": [
    {"id": 1, "key": "feature_flag_name", "value": "on"}
  ]
}
```

---

## 21. Check device

**Purpose:** Check device status with DeepSeek (device registration/verification).

**Method & URL:**
```
POST /v1/device/check
```

**Request body fields:**

| Field | Type | Description |
|---|---|---|
| `device_id` | string | Device identifier |
| `device_model` | string | Device model name |

**curl:**
```powershell
curl.exe -s -X POST http://localhost:11434/v1/device/check `
  -H "Content-Type: application/json" `
  -d '{\"device_id\":\"my-device\",\"device_model\":\"\"}'
```

**Example response:**
```json
{
  "status": "ok"
}
```

---

## 22. Report client settings

**Purpose:** Report client settings state to DeepSeek (analytics/feature flag reporting).

**Method & URL:**
```
POST /v1/client/settings/report
```

**Request body fields:**

| Field | Type | Description |
|---|---|---|
| `settings_ids` | array | List of setting IDs to report |
| `did` | string | Device ID |
| `sso_id` | string | SSO session ID |

**curl:**
```powershell
curl.exe -s -X POST http://localhost:11434/v1/client/settings/report `
  -H "Content-Type: application/json" `
  -d '{\"settings_ids\":[1,2],\"did\":\"my-device\",\"sso_id\":\"\"}'
```

**Example response:**
```json
{
  "status": "ok"
}
```

---

## Practical workflows

### Workflow 1 — Quick chat test

```powershell
# 1. Check the bridge is running
curl.exe -s http://localhost:11434/health

# 2. Send a message
curl.exe -s -X POST http://localhost:11434/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"deepseek-chat\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello! What can you do?\"}]}'
```

---

### Workflow 2 — Create session → chat → share → clean up

```powershell
$BASE = "http://localhost:11434"

# Step 1 — Create a session
$resp = curl.exe -s -X POST "$BASE/v1/sessions" -H "Content-Type: application/json" -d '{}'
# Copy the session_id from the response
$SID = "paste-session-id-here"

# Step 2 — Chat in that session
curl.exe -s -X POST "$BASE/v1/chat/completions" `
  -H "Content-Type: application/json" `
  -d "{\"model\":\"deepseek-chat\",\"messages\":[{\"role\":\"user\",\"content\":\"Explain REST APIs in one paragraph.\"}]}"

# Step 3 — Rename the session
curl.exe -s -X PATCH "$BASE/v1/sessions/$SID" `
  -H "Content-Type: application/json" `
  -d '{\"title\":\"REST API explanation\"}'

# Step 4 — Create a share link
curl.exe -s -X POST "$BASE/v1/shares" `
  -H "Content-Type: application/json" `
  -d "{\"session_id\":\"$SID\",\"message_ids\":[]}"

# Step 5 — Delete the session when done
curl.exe -s -X DELETE "$BASE/v1/sessions/$SID"
```

---

### Workflow 3 — Get history, regenerate, continue

```powershell
$BASE = "http://localhost:11434"
$SID  = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

# Step 1 — See all messages and their IDs
curl.exe -s "$BASE/v1/sessions/$SID"

# Step 2 — Regenerate last answer (message_id 2) with more detail
curl.exe -s -X POST "$BASE/v1/sessions/$SID/regenerate" `
  -H "Content-Type: application/json" `
  -d '{\"child_message_id\":2,\"stream\":false,\"user_options\":\"verbosity_high\"}'

# Step 3 — If the new answer got cut off, continue it
curl.exe -s -X POST "$BASE/v1/sessions/$SID/continue" `
  -H "Content-Type: application/json" `
  -d '{\"message_id\":2,\"stream\":false}'

# Step 4 — Like the final answer
curl.exe -s -X POST "$BASE/v1/sessions/$SID/feedback" `
  -H "Content-Type: application/json" `
  -d '{\"message_id\":2,\"feedback_type\":\"LIKE\"}'
```

---

## Error responses

All endpoints return errors in the OpenAI shape:

```json
{
  "error": {
    "message": "human-readable description",
    "type":    "invalid_request_error",
    "param":   null,
    "code":    null
  }
}
```

| HTTP status | `type` | Meaning |
|---|---|---|
| `400` | `invalid_request_error` | Bad request — missing or invalid field in the body |
| `401` | `authentication_error` | Missing or unknown API key |
| `502` | `server_error` | Upstream failure — `upstream_error` or `upstream_empty_response` |
| `503` | `server_error` | `api_key_required` — `REQUIRE_API_KEY=true` but no key exists yet |
| `500` | `server_error` | Anything else, including upstream error passthrough |

**Common errors and fixes:**

| Error message | Code | Fix |
|---|---|---|
| `messages field is required` | `invalid_request_error` | Include `"messages"` in the POST body |
| `child_message_id is required` | `invalid_request_error` | Include `"child_message_id"` when calling `/regenerate` |
| `message_id is required` | `invalid_request_error` | Include `"message_id"` when calling `/continue` or `/feedback` |
| `feedback_type must be LIKE or DISLIKE` | `invalid_request_error` | Use uppercase `"LIKE"` or `"DISLIKE"` |
| `DeepSeek returned no content: …` | `upstream_empty_response` | DeepSeek refused or rate-limited in-band. Wait and retry — the bridge already retried once for you. |
| `API key required` | `api_key_required` | Create one in the terminal with `/api <name>`, or set `REQUIRE_API_KEY=false` |
| `Session expired! Run: python refresh_session.py` | `authentication_error` | Type `/connect` in the terminal and click Connect in the browser extension (or run `refresh_session.py`) |

**Streaming errors.** Once a stream has started the HTTP status is already
`200`, so failures arrive as a final in-band frame instead:

```jsonc
data: {"id":"chatcmpl-…","object":"error","created":1716100000,"model":"deepseek-chat",
       "error":{"message":"DeepSeek returned no content: … (rate_limit_reached)",
                "type":"server_error","code":"upstream_empty_response"}}

data: [DONE]
```

A `finish_reason` of `length` (rather than `stop`) means the same thing for a
partial answer: the reply was cut short and the text above it is incomplete.
