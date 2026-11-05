"""Session-owned model/tool loop shared by the native CLI and HTTP API."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
import logging
import re
import threading
import time
import uuid

from . import config
from .providers import ProviderError, ToolProtocolError, normalize_calls
from .tools import ToolRegistry, definitions_valid, parse_arguments
from .context import model_view, size, conversation_units, call_index
from . import compaction
from .editing import undo_checkpoint
from .prompts import coding_prompt, PROMPT_VERSION

log = logging.getLogger("harness")
log.addHandler(logging.NullHandler())


class HarnessError(RuntimeError):
    def __init__(self, message, code="invalid_request", status=400):
        super().__init__(message)
        self.code, self.status = code, status


class Control:
    def __init__(self, timeout=None):
        self.cancelled = threading.Event()
        self.deadline = time.monotonic() + (config.AGENT_TIMEOUT if timeout is None else timeout)
        # Interactive hooks. A client that can talk to a person (the terminal)
        # supplies them; API runs leave them unset and such actions are refused.
        self.approve = None
        self.ask_user = None
        self.approval_rules = None
        self.authorized = set()
        self.session_id = None
        self.depth = 0
        self.max_turns = None
        self.waited = 0.0

    def remaining(self):
        return max(0.001, self.deadline - time.monotonic())

    def check(self):
        if self.cancelled.is_set():
            raise InterruptedError("Run cancelled")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Run deadline exceeded")


@dataclass
class Session:
    id: str
    owner: str
    messages: list = field(default_factory=list)
    calls: dict = field(default_factory=dict)
    title: str = ""
    pinned: bool = False
    mode: str = "build"
    skills: list = field(default_factory=list)
    plan: list = field(default_factory=list)
    checkpoints: list = field(default_factory=list)
    compact_before: int = 0
    summary: str = ""          # model-written handoff for the first `summary_groups` steps
    summary_groups: int = 0
    compactions: int = 0
    context: dict = field(default_factory=dict)
    verification: list = field(default_factory=list)
    require_verification: bool = False
    cwd: str = "."
    provider_state: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    control: Control | None = None
    touched: float = field(default_factory=time.monotonic)


def validate_messages(messages, check_results=True):
    if not isinstance(messages, list) or not messages:
        raise HarnessError("messages must be a nonempty array")
    pending = set()
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") not in ("system", "developer", "user", "assistant", "tool", "function"):
            raise HarnessError("Invalid message role")
        content = msg.get("content")
        if check_results and pending and msg["role"] != "tool":
            raise HarnessError("Tool results must immediately follow the assistant calls")
        if content is None and (msg["role"] != "assistant" or not msg.get("tool_calls")):
            raise HarnessError("Message content is required")
        if content is not None and not isinstance(content, (str, list)):
            raise HarnessError("Invalid message content")
        if isinstance(content, list) and any(not isinstance(b, dict) or b.get("type") not in ("text", "image_url")
                                            for b in content):
            raise HarnessError("Unsupported content block")
        if isinstance(content, list):
            for block in content:
                if block["type"] == "text" and not isinstance(block.get("text"), str):
                    raise HarnessError("Text blocks require string text")
                if block["type"] == "image_url" and (not isinstance(block.get("image_url"), dict) or
                        not isinstance(block["image_url"].get("url"), str)):
                    raise HarnessError("Image blocks require a URL")
        if msg.get("tool_calls"):
            try:
                pending.update(c["id"] for c in normalize_calls(msg["tool_calls"]))
            except ProviderError as exc:
                raise HarnessError(str(exc)) from exc
        if msg["role"] == "tool":
            if not isinstance(msg.get("tool_call_id"), str) or (check_results and msg["tool_call_id"] not in pending):
                raise HarnessError("Tool result must match a preceding assistant call")
            pending.discard(msg["tool_call_id"])
    if pending and check_results:
        raise HarnessError("Missing tool results")
    return copy.deepcopy(messages)


def session_title(messages):
    """Short title from the first user message: its first non-empty line, 60 characters at most."""
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        for line in str(content or "").splitlines():
            line = " ".join(line.split())
            if line:
                return line if len(line) <= 60 else line[:59].rstrip() + "…"
    return ""


def protocol_correction(error):
    """The system note sent after a reply that could not be decoded. It names the problem when
    known: resending the same oversized reply was the most common way a run used to fail."""
    diagnosis = getattr(error, "diagnosis", None)
    if diagnosis and "empty envelope" in diagnosis:
        return ("Your previous response was empty: it had no content and no tool calls. If the work is finished, put your final "
                'report in "content" (what you did, what the tools showed, what remains unverified); otherwise make the next tool '
                'call. Return ONLY {"content": "answer or null", "tool_calls": []}.')
    text = ("Your previous response was rejected because it was not valid tool JSON. No calls from that response ran. ")
    if diagnosis:
        text += f"Problem: {diagnosis}. "
    if getattr(error, "cut_off", False):
        text += ("Replies are limited to about 20,000 characters in total and yours was cut off. Do NOT resend it: make the reply "
                 "much shorter. Write a large file in parts of at most 8,000 characters, one part per reply: write_file for the "
                 "first part, then write_file with append=true for each following part. ")
    return text + ('Return ONLY {"content": "answer or null", "tool_calls": []}, with function calls in tool_calls when needed. '
                   'Follow the original tool choice. Use existing tool results; do not repeat completed writes.')


def tool_summary(call):
    """Short, human-readable description of a call's target for UIs (not logged)."""
    try:
        args = json.loads(call["function"]["arguments"])
    except ValueError:
        return ""
    if not isinstance(args, dict):
        return ""
    for key in ("path", "command", "url", "query", "pattern", "description", "question", "id"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return " ".join(value.split())[:120]
    if isinstance(args.get("argv"), list):
        return " ".join(map(str, args["argv"]))[:120]
    if isinstance(args.get("checks"), list):
        return ", ".join(str(c.get("name", "")) for c in args["checks"] if isinstance(c, dict))[:120]
    return ""


def call_signature(call):
    try:
        arguments = json.loads(call["function"]["arguments"])
    except ValueError:
        arguments = call["function"]["arguments"]
    return json.dumps({"name": call["function"]["name"], "arguments": arguments}, sort_keys=True)


class AgentRuntime:
    def __init__(self, provider, registry=None, store=None):
        self.provider = provider
        self.registry = registry or ToolRegistry()
        self.sessions = {}
        self.guard = threading.Lock()
        self.store = store
        self.approval_rules = {}

    @staticmethod
    def snapshot(session, private=True):
        keys = ["id", "owner", "messages", "title", "pinned", "mode", "skills", "plan", "compact_before", "summary", "summary_groups", "compactions", "context", "verification", "require_verification", "cwd"]
        if private:
            keys += ["calls", "checkpoints", "provider_state"]
        return copy.deepcopy({k: getattr(session, k) for k in keys})

    def save(self, session):
        if self.store:
            self.store.save(self.snapshot(session))

    def _evict(self):
        if len(self.sessions) >= config.MAX_SESSIONS:
            idle = [s for s in self.sessions.values() if not s.lock.locked()]
            if not idle:
                raise HarnessError("Session capacity reached", "session_capacity", 503)
            del self.sessions[min(idle, key=lambda s: s.touched).id]

    def list(self, owner):
        with self.guard:
            entries = {s["id"]: s for s in self.store.list(owner)} if self.store else {}
            entries.update({s.id: self.snapshot(s) for s in self.sessions.values() if s.owner == owner})
            return [dict({k: s[k] for k in ("id", "title", "pinned", "mode", "skills")}, message_count=len(s["messages"]))
                    for s in entries.values()]

    def latest(self, owner):
        """Id of the owner's most recently saved session, or ``None``."""
        entries = self.list(owner)
        return entries[0]["id"] if entries else None

    def contains(self, sid):
        with self.guard:
            return sid in self.sessions or bool(self.store and self.store.load(sid))

    def delete(self, session):
        with self.guard:
            if self.store:
                self.store.delete(session.id)
            self.sessions.pop(session.id, None)
            self.approval_rules.pop(session.id, None)
        self.registry.processes.stop_owner(session.id)
        forget = getattr(self.provider, "forget", None)
        if forget is not None:
            forget(copy.deepcopy(session.provider_state))

    def settings(self, session, body):
        mode = body.get("mode", session.mode)
        if mode not in ("plan", "build"):
            raise HarnessError("mode must be plan or build")
        try:
            skills = self.registry.skills.activate(body.get("skills", session.skills))
        except ValueError as exc:
            raise HarnessError(str(exc)) from exc
        required = body.get("require_verification", session.require_verification)
        if not isinstance(required, bool):
            raise HarnessError("require_verification must be boolean")
        if required and not self.verification_tools(self.registry.definitions):
            raise HarnessError("require_verification needs run_checks or validate_files enabled")
        cwd = body.get("cwd", session.cwd)
        try:
            directory = self.registry.directory(cwd)
        except (ValueError, OSError) as exc:
            raise HarnessError("cwd must be an existing workspace directory") from exc
        return mode, skills, required, str(directory.relative_to(self.registry.root))

    @staticmethod
    def verification_tools(tools):
        return {t["function"]["name"] for t in tools} & {"run_checks", "validate_files"}

    def change_directory(self, session, value):
        if not isinstance(value, str) or not value.strip():
            raise HarnessError("Directory path is required")
        from pathlib import Path
        try:
            directory = self.registry.directory(str(Path(session.cwd) / value))
        except (ValueError, OSError) as exc:
            raise HarnessError("Directory must exist inside the configured workspace") from exc
        self.configure(session, {"cwd": str(directory.relative_to(self.registry.root))})
        return {"cwd": session.cwd, "directory": str(directory), "workspace": str(self.registry.root)}

    def configure(self, session, body):
        session.mode, session.skills, session.require_verification, session.cwd = self.settings(session, body)
        self.save(session)

    def fork(self, session):
        clone = self.create(session.owner)
        for key in ("messages", "calls", "mode", "skills", "plan", "compact_before", "summary", "summary_groups", "compactions", "verification", "require_verification", "cwd"):
            setattr(clone, key, copy.deepcopy(getattr(session, key)))
        clone.title = session.title + " (fork)"
        self.save(clone)
        return clone

    def context_report(self, session):
        """Where this session's context goes (characters); see ``compaction.usage_report``."""
        return compaction.usage_report(self, session)

    def _fold(self, session, units, upto, summary_text):
        """Record that the oldest ``upto`` steps are folded (into ``summary_text`` when there is one)."""
        session.compact_before = upto
        if summary_text:
            session.summary, session.summary_groups = summary_text, upto
        session.compactions += 1

    def _summarize_new(self, session, units, upto, control, focus=""):
        """Summary covering steps 0..upto: the previous summary plus the steps it does not yet cover."""
        names = call_index(session.messages)
        covered = min(session.summary_groups, upto) if session.summary else 0
        return compaction.summarize(self.provider, config.MODEL, control, units[covered:upto],
                                    previous=session.summary if covered else "", focus=focus, names=names)

    def compact(self, session, focus="", keep=4):
        """Fold all but the last ``keep`` steps into a model-written summary (``/compact [focus]``).

        The caller holds the session lock. Never raises for a model failure: it falls back to the extractive
        handoff and says so. The transcript is untouched."""
        units = conversation_units(session.messages)
        before = self.context_report(session).get("in_view")
        if len(units) <= keep + 1:
            return {"compacted": False, "reason": "history is still short; nothing worth compacting",
                    "steps": len(units), "transcript_preserved": True}
        upto = len(units) - keep
        control = Control()
        control.provider_state = copy.deepcopy(session.provider_state)
        control.session_id = session.id
        method, warning = "summary", None
        try:
            text = self._summarize_new(session, units, upto, control, focus)
        except compaction.CompactionError as exc:
            text, method, warning = "", "extractive", str(exc)
        finally:
            session.provider_state = copy.deepcopy(control.provider_state)  # a remote chat created for this call is tracked
        self._fold(session, units, upto, text)
        self.save(session)
        after = self.context_report(session).get("in_view")
        result = {"compacted": True, "method": method, "compact_before_group": session.compact_before,
                  "steps_folded": upto, "steps_kept": len(units) - upto, "summary_chars": len(session.summary) if text else 0,
                  "characters_before": before, "characters_after": after, "transcript_preserved": True}
        if warning:
            result["warning"] = warning + "; used the extractive handoff instead"
        return result

    def _auto_compact(self, session, control, messages, tools, model, guard, turn):
        """Before a model turn: fold older steps into a model-written summary when the context needs it.

        Micro-compaction (clearing old bulky tool results) already happens inside ``model_view``. This adds the summary
        in two situations:

        * mandatory - steps are already being dropped from the view and no summary covers them. Dropping them silently
          would lose facts, so this is never capped (a failed attempt only backs off for a few turns);
        * proactive - the view passed ``AUTOCOMPACT_PERCENT`` of the budget. Capped per run, never on consecutive turns,
          and skipped when folding would save less than it costs.

        Yields one ``auto_compact`` event."""
        percent = config.AUTOCOMPACT_PERCENT
        if percent <= 0:
            return
        budget, tool_chars = config.CONTEXT_CHARS, size(tools)
        threshold = int(budget * percent / 100)
        instructions = self.instructions(session, control)

        def build():
            return model_view(messages, instructions, budget - tool_chars, session.compact_before, session.summary,
                              session.summary_groups, soft=threshold - tool_chars, keep_groups=config.COMPACT_KEEP_GROUPS,
                              min_clear=config.MICROCOMPACT_MIN_CHARS)
        try:
            view, report = build()
        except ValueError:
            return  # the turn itself reports context_limit with a precise message
        used = size(view) + tool_chars
        dropped = report.get("omitted_groups", 0) if report.get("compacted") else 0
        mandatory = dropped > (session.summary_groups if session.summary else 0)
        if mandatory:
            if guard["mandatory"] >= 40 or turn - guard["failed"] < 3:
                return
        elif used <= threshold or guard["count"] >= config.AUTOCOMPACT_MAX_PER_RUN or turn - guard["turn"] < 2:
            return
        units = conversation_units(messages)
        overhead = len(instructions) + tool_chars + config.SUMMARY_CHARS + 1500
        tail_chars = max(int(budget * 0.15), int(budget * 0.5) - overhead)
        upto = compaction.plan_fold(units, tail_chars)
        if mandatory:
            upto = min(max(upto, dropped), max(0, len(units) - 1))
        covered = min(session.summary_groups, upto) if session.summary else 0
        if upto <= covered:
            return
        if not mandatory and sum(size(u) for u in units[covered:upto]) < 3000:
            return  # a summary costs a model call and several thousand characters; not worth it for this little
        guard["mandatory" if mandatory else "count"] += 1
        guard["turn"] = turn
        started = time.monotonic()
        method, warning = "summary", None
        try:
            text = self._summarize_new(session, units, upto, control)
        except compaction.CompactionError as exc:
            text, method, warning = "", "extractive", str(exc)
            guard["failed"] = turn
            log.warning("auto_compact summary failed (%s)", exc)
        self._fold(session, units, upto, text)
        self.save(session)
        after, _ = build()
        log.info("auto_compact method=%s mandatory=%s folded=%d before=%d after=%d", method, mandatory, upto, used,
                 size(after) + tool_chars)
        event = {"type": "auto_compact", "method": method, "steps_folded": upto, "characters_before": used,
                 "characters_after": size(after) + tool_chars, "seconds": round(time.monotonic() - started, 1),
                 "mandatory": mandatory}
        if warning:
            event["warning"] = warning
        yield event

    def undo(self, session):
        if session.messages:
            validate_messages(session.messages)
        if not session.checkpoints:
            raise HarnessError("No native file checkpoint available")
        with self.registry.workspace_lock:
            item = session.checkpoints[-1]
            try:
                undo_checkpoint(item, self.registry)
            except ValueError as exc:
                raise HarnessError(str(exc), "checkpoint_conflict", 409) from exc
            session.checkpoints.pop()
            self.registry.mark_changed()
            # Prior cached results no longer describe this file. Preserve the
            # ID ledger to prevent replay, and make the changed state explicit.
            session.messages.append({"role": "system", "content": f"User restored native file checkpoint for {item['path']}. Prior tool results may be stale; inspect the file."})
            self.save(session)
            return {"restored": item["path"]}

    def review(self, session):
        import base64
        import difflib
        first = {}
        for checkpoint in session.checkpoints:
            for item in checkpoint.get("changes", [checkpoint]):
                first.setdefault(item["path"], item)
        output = []
        with self.registry.workspace_lock:
            for path, item in first.items():
                target = self.registry.path(path)
                if target.exists() and target.stat().st_size > config.MAX_FILE_BYTES:
                    raise HarnessError("Review file exceeds HARNESS_MAX_FILE_BYTES")
                old = base64.b64decode(item["before"]).decode("utf-8") if item["before"] is not None else ""
                new = target.read_bytes().decode("utf-8") if target.exists() else ""
                output.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile=path, tofile=path))
        text = "".join(output)
        return {"diff": text[:config.MAX_TOOL_OUTPUT], "truncated": len(text) > config.MAX_TOOL_OUTPUT}

    def instructions(self, session, control=None):
        sections = [coding_prompt(self.registry, session)]
        if control is not None:
            self.registry._scoped_instructions(self.registry.directory(session.cwd), control)
        for filename in ("AGENTS.md", "CLAUDE.md"):
            path = self.registry.root / filename
            if path.is_file() and path.resolve().is_relative_to(self.registry.root):
                with path.open(encoding="utf-8") as fh:
                    text = fh.read(20001)
                if len(text) > 20000:
                    raise HarnessError(f"{filename} exceeds the 20000 character instruction limit")
                sections.append(f"Workspace instructions ({filename}):\n{text}")
        for path, text in getattr(control, "scoped_instructions", {}).items():
            sections.append(f"Scoped instructions ({path}; applies to that directory and descendants):\n{text}")
        if session.mode == "plan":
            sections.append("PLAN MODE: inspect and plan only. File mutations and command execution are denied by the runtime. Ask the user to switch to build to implement.")
        if session.plan:
            sections.append("Current session plan (recorded state; completion evidence is not independently verified):\n" + json.dumps(session.plan))
        if session.verification:
            sections.append("Observed verification (older evidence may be stale):\n" + json.dumps(self.verification_status(session)))
        if session.skills:
            sections.append(self.registry.skills.instructions(session.skills))
        if self.registry.mcp.bindings:
            sections.append("MCP tool mapping (use registered names; schemas are authoritative):\n" + "\n".join(
                f"{server}/{tool} -> {name}" for name, (server, tool, _) in self.registry.mcp.bindings.items()))
        return "\n\n".join(sections)

    def verification_status(self, session):
        output, current = [], {}
        for record in session.verification:
            stale = record.get("epoch") != self.registry.epoch or record.get("revision") != self.registry.activity_revision
            if not stale:
                scope = tuple(record["paths"]) if record.get("paths") else None
                if scope not in current:
                    current[scope] = self.registry.fingerprint(list(scope) if scope else None)
                stale = record.get("fingerprint") is None or current[scope] != record["fingerprint"]
            output.append(dict(record, stale=stale))
        return output

    def create(self, owner, body=None):
        session = Session(str(uuid.uuid4()), owner)
        session.mode, session.skills, session.require_verification, session.cwd = self.settings(session, body or {})
        with self.guard:
            self._evict()
            self.save(session)
            self.sessions[session.id] = session
            return session

    def get(self, sid, owner):
        with self.guard:
            session = self.sessions.get(sid)
            if session is None and self.store:
                saved = self.store.load(sid)
                if saved and saved["owner"] == owner:
                    self._evict()
                    known = Session.__dataclass_fields__
                    session = Session(**{k: v for k, v in saved.items() if k in known})
                    self.sessions[sid] = session
            if session is None or session.owner != owner:
                raise HarnessError("Session not found", "session_not_found", 404)
            return session

    def cancel(self, sid, owner):
        session = self.get(sid, owner)
        with self.guard:
            if session.control:
                session.control.cancelled.set()
                return True
            return False

    def prepare(self, body, owner, execute=False, approver=None, asker=None, parent=None):
        if not isinstance(body, dict):
            raise HarnessError("Request body must be an object")
        incoming = validate_messages(body.get("messages"), check_results=False)
        for key in ("stream", "search_enabled", "thinking_enabled", "require_verification"):
            if key in body and not isinstance(body[key], bool):
                raise HarnessError(f"{key} must be boolean")
        if "stream_options" in body and not isinstance(body["stream_options"], dict):
            raise HarnessError("stream_options must be an object")
        model = body.get("model", config.MODEL)
        if not isinstance(model, str) or not model:
            raise HarnessError("model must be a nonempty string")
        if body.get("n", 1) != 1:
            raise HarnessError("Only n=1 is supported")
        if "file_ids" in body and (not isinstance(body["file_ids"], list) or
                                   any(not isinstance(f, str) for f in body["file_ids"])):
            raise HarnessError("file_ids must be an array of strings")
        if "functions" in body:
            raise HarnessError("Use tools and tool_choice instead of legacy functions")
        tools = body.get("tools", self.registry.definitions if execute else [])
        try:
            definitions_valid(tools)
        except Exception as exc:
            raise HarnessError("Invalid tool definitions") from exc
        if execute:
            allowed = {t["function"]["name"]: t for t in self.registry.definitions}
            if any(allowed.get(t["function"]["name"]) != t for t in tools):
                raise HarnessError("Agent tools must match the configured native definitions")
        if body.get("require_verification"):
            if not execute or not self.verification_tools(tools):
                raise HarnessError("require_verification needs a native run with run_checks or validate_files enabled")
        choice = body.get("tool_choice", "auto")
        if isinstance(choice, dict):
            if choice.get("type") != "function" or not isinstance(choice.get("function"), dict) or \
                choice["function"].get("name") not in {t["function"]["name"] for t in tools}:
                raise HarnessError("Invalid tool_choice")
        elif choice not in ("auto", "none", "required"):
            raise HarnessError("Invalid tool_choice")
        if not tools and choice not in ("auto", "none"):
            raise HarnessError("tool_choice requires tools")
        sid = body.get("session_id")
        if sid is not None and (not isinstance(sid, str) or not sid):
            raise HarnessError("session_id must be a nonempty string")
        session = self.get(sid, owner) if sid else self.create(owner, body if execute else None)
        if not session.lock.acquire(blocking=False):
            raise HarnessError("Session already has an active run", "session_busy", 409)
        control = Control()
        try:
            with self.guard:
                if self.sessions.get(session.id) is not session:
                    session.lock.release()
                    raise HarnessError("Session was evicted; create a new one", "session_not_found", 404)
                session.control = control
            if session.messages and incoming[:len(session.messages)] == session.messages:
                messages = incoming
            else:
                messages = copy.deepcopy(session.messages) + incoming
            validate_messages(messages)
            if not session.title:
                session.title = session_title(incoming)
            control.provider_state = copy.deepcopy(session.provider_state)
            control.session_id = session.id
            if execute:
                control.approve = approver
                control.approval_rules = self.approval_rules.setdefault(session.id, set())
                if asker is not None:
                    def ask_user(question, options):
                        started = time.monotonic()
                        try:
                            return asker(question, options)
                        finally:
                            control.deadline += time.monotonic() - started
                    control.ask_user = ask_user
                if parent is not None:
                    control.depth = parent.depth + 1
                    control.max_turns = config.SUBAGENT_MAX_TURNS
                    control.deadline = min(parent.deadline, time.monotonic() + config.SUBAGENT_TIMEOUT)
            def save_provider_state():
                session.provider_state = copy.deepcopy(control.provider_state)
                self.save(session)
            control.save_provider_state = save_provider_state
            if execute:
                self.configure(session, body)
                control.mode = session.mode
                control.require_verification = session.require_verification
                if control.require_verification and not self.verification_tools(tools):
                    raise HarnessError("Selected tools omit verification tools required by this session")
                control.cwd = session.cwd
                control.plan_updated = False
                def change_directory(value):
                    try:
                        result = self.change_directory(session, value)
                    except HarnessError as exc:
                        # Tool errors are shown to the model; a HarnessError would be hidden
                        # behind the generic failure text. The path is relative to the cwd.
                        raise ValueError(f"{exc}. The path is relative to the current directory ({session.cwd}); "
                                         "create the folder first if it does not exist.") from exc
                    control.cwd = session.cwd
                    return result
                control.change_directory = change_directory
                control.changed_files = False

                def update_plan(steps):
                    session.plan = copy.deepcopy(steps)
                    control.plan_updated = True
                    self.save(session)

                control.changed_paths = []

                def add_checkpoint(item):
                    if item["path"] not in control.changed_paths:
                        control.changed_paths.append(item["path"])
                    session.checkpoints.append(item)
                    session.checkpoints = session.checkpoints[-20:]
                    self.save(session)

                control.update_plan, control.checkpoint = update_plan, add_checkpoint
                def record_verification(record):
                    session.verification.append(record)
                    session.verification = session.verification[-20:]
                    self.save(session)
                control.record_verification = record_verification
            options = {k: v for k, v in body.items() if k in (
                "temperature", "max_tokens", "top_p", "stop", "search_enabled", "thinking_enabled", "file_ids")}
            return session, control, self._run(session, control, messages, tools, choice, model, execute, options)
        except BaseException:
            self.release(session, control)
            raise

    def _delegate(self, session, control, args):
        """Run a focused sub-agent in an isolated, short-lived child session.

        The child shares the workspace, permissions and approver of this run but
        none of its history. It cannot delegate again. Its final report is the
        tool result; its file checkpoints move to the parent so /undo still works.
        """
        if control.depth >= 1:
            return json.dumps({"error": "nested_delegation", "message": "Sub-agents cannot delegate further"})
        mode = "plan" if getattr(control, "mode", "build") == "plan" else args.get("mode", "plan")
        keep = (self.registry.read_only | {"apply_patch"}) if mode == "plan" else None
        child_tools = [t for t in self.registry.definitions if t["function"]["name"] != "delegate_task"
                       and (keep is None or t["function"]["name"] in keep)]
        brief = ("You are a delegated sub-agent working for a lead agent. Complete exactly this task, then reply with a "
                 "concise, self-contained report (findings with file paths and line numbers, or what you changed and "
                 "verified). You cannot ask the lead questions.\n\nTask: " + args["description"] + "\n\n" + args["prompt"])
        try:
            child = self.create(session.owner, {"mode": mode, "cwd": session.cwd})
        except HarnessError as exc:
            return json.dumps({"error": "subagent_failed", "message": str(exc)})
        child.title = "[sub-agent] " + args["description"][:80]
        body = {"session_id": child.id, "mode": mode, "cwd": session.cwd, "tools": child_tools,
                "messages": [{"role": "user", "content": brief}]}
        done = threading.Event()

        def relay_cancel():
            while not done.is_set():
                if control.cancelled.wait(0.1):
                    child_control.cancelled.set()
                    return

        events = None
        try:
            _, child_control, events = self.prepare(body, session.owner, execute=True, approver=control.approve,
                                                    asker=None, parent=control)
            child_control.approval_rules = control.approval_rules
            threading.Thread(target=relay_cancel, daemon=True).start()
            final, calls_made = None, 0
            for event in events:
                if event["type"] == "tool_start":
                    calls_made += 1
                    yield {"type": "subagent_tool", "description": args["description"], "name": event["name"]}
                elif event["type"] == "response":
                    final = event
            if final is None:
                return json.dumps({"error": "subagent_failed", "message": "Sub-agent produced no report"})
            for item in child.checkpoints:
                control.checkpoint(item)
            if getattr(child_control, "changed_files", False):
                control.changed_files = True
            control.deadline += child_control.waited
            from .tool_output import encode_result
            return encode_result({"report": final["message"].get("content") or "", "mode": mode,
                                  "tool_calls": calls_made}, config.MAX_TOOL_OUTPUT)
        except HarnessError as exc:
            control.check()
            return json.dumps({"error": "subagent_failed", "message": str(exc)})
        finally:
            done.set()
            if events is not None:
                events.close()
            self.delete(child)

    # A request that asks for no change and no sequence of steps ("ls the files") cannot be
    # "completed early", so auditing it only costs a turn and invites an embellished report.
    ACTION = re.compile(r"\b(?:create|make|write|build|implement|add|fix|change|edit|modify|update|delete|remove|rename|"
                        r"move|install|run|test|refactor|generate|configure|deploy|commit|push|then|after|and also)\b", re.IGNORECASE)

    NEGATION = re.compile(r"(?:\bnot\b|n't\b|\bnever\b|\bwithout\b|\bavoid\b|\bno\b)[^.;\n]{0,40}$", re.IGNORECASE)

    @staticmethod
    def _latest_request(messages):
        for message in reversed(messages):
            if message["role"] == "user":
                content = message.get("content")
                if isinstance(content, list):
                    content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
                return str(content or "")
        return ""

    def _named_uncalled(self, request, called):
        """Registered tools the user's request names explicitly but this run never
        called. A mention shortly after a negation ("don't use run_shell") is not a
        request to call it."""
        names = []
        for tool in self.registry.definitions:
            name = tool["function"]["name"]
            if name in called:
                continue
            for match in re.finditer(r"(?<![\w])" + re.escape(name) + r"(?![\w])", request):
                if not self.NEGATION.search(request[max(0, match.start() - 50):match.start()]):
                    names.append(name)
                    break
        return names

    def _completion_audit(self, control, messages, tools_run, done, called):
        """Self-audit text to send before accepting a tool-using build run's final
        answer, or ``None``.

        Some models stop after the first steps of a multi-step request and then
        describe the rest as done. The audit re-presents the user's request. It
        costs one extra model turn (more only while explicitly named tools remain
        uncalled), so it is on by default only for providers that declare
        ``completion_check`` (the text-protocol web backend).
        """
        mode = config.COMPLETION_CHECK
        enabled = mode == "true" or (mode == "auto" and getattr(self.provider, "completion_check", False))
        if not (enabled and tools_run > 0 and getattr(control, "mode", "build") == "build" and control.depth == 0):
            return None
        request = self._latest_request(messages)
        named = self._named_uncalled(request, called)
        if not named and called <= self.registry.read_only and not self.ACTION.search(request):
            return None
        if done >= 3 or (done >= 1 and not named):
            return None
        text = ("Before you finish, check completeness. The user's request was:\n<<<\n" + request[:2000] + "\n>>>\n"
                "Compare it with the tool results above. If any requested action has not been performed with a tool and "
                "observed in a result, perform it now with tool calls. If everything is done, reply with your final report "
                "(what you did, what the tools actually showed, what remains unverified) without repeating completed actions. "
                "Do not claim results you have not received.")
        if named:
            text += ("\nThe request names these tools, which you have not called in this run: " + ", ".join(named) +
                     ". Call them now, unless the user told you not to use them.")
        return text

    def release(self, session, control):
        with self.guard:
            if session.control is control:
                control.cancelled.set()
                session.control = None
                session.touched = time.monotonic()
                session.lock.release()

    def _run(self, session, control, messages, tools, choice, model, execute, options):
        run_id = uuid.uuid4().hex
        signatures = {}
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        protocol_failures = 0
        verification_corrections = 0
        continuation_corrections = 0
        completion_checks = 0
        tools_run = 0
        called_tools = set()
        rate_retries = 0
        auto_guard = {"count": 0, "mandatory": 0, "turn": -10, "failed": -10}
        log.info("run=%s session=%s start provider=%s", run_id, session.id, type(self.provider).__name__)
        try:
            session.messages = copy.deepcopy(messages)
            self.save(session)
            max_turns = (control.max_turns or config.MAX_AGENT_TURNS) if execute else 1 + config.PROTOCOL_RETRIES
            for turn in range(max_turns + config.RATE_LIMIT_RETRIES):
                if turn - rate_retries >= max_turns:
                    raise HarnessError("Maximum agent turns reached", "turn_limit", 508)
                control.check()
                response = None
                pending_deltas = []
                buffer_completion = execute and ((control.plan_updated and control.mode == "build") or
                                                   (control.require_verification and control.changed_files) or
                                                   self._completion_audit(control, messages, tools_run, completion_checks, called_tools) is not None)
                try:
                    view = messages
                    if execute:
                        yield from self._auto_compact(session, control, messages, tools, model, auto_guard, turn)
                        try:
                            tool_chars = size(tools)
                            threshold = int(config.CONTEXT_CHARS * config.AUTOCOMPACT_PERCENT / 100) - tool_chars \
                                if config.AUTOCOMPACT_PERCENT else None
                            view, session.context = model_view(messages, self.instructions(session, control),
                                                               config.CONTEXT_CHARS - tool_chars, session.compact_before,
                                                               session.summary, session.summary_groups, soft=threshold,
                                                               keep_groups=config.COMPACT_KEEP_GROUPS,
                                                               min_clear=config.MICROCOMPACT_MIN_CHARS)
                            session.context.update(tool_characters=tool_chars,
                                total_input_characters=size(view) + tool_chars, budget=config.CONTEXT_CHARS,
                                prompt_version=PROMPT_VERSION)
                        except ValueError as exc:
                            raise HarnessError(str(exc), "context_limit", 413) from exc
                    for event in self.provider.stream(view, tools, choice, model, control, **options):
                        control.check()
                        if event["type"] == "response":
                            response = event
                        elif buffer_completion and event["type"] == "delta" and "content" in event["delta"]:
                            pending_deltas.append(event)
                        else:
                            yield event
                except ToolProtocolError as exc:
                    protocol_failures += 1
                    if protocol_failures > config.PROTOCOL_RETRIES:
                        prose = getattr(exc, "fallback_text", None)
                        if not (prose and choice in ("auto", "none")):
                            raise ProviderError("Model tool JSON remained malformed after bounded corrections")
                        # The model answered in plain prose without any call or
                        # envelope. Deliver it rather than failing a finished task.
                        log.info("run=%s tool_protocol_prose_fallback", run_id)
                        delta = {"type": "delta", "delta": {"content": prose}}
                        if buffer_completion:
                            pending_deltas.append(delta)
                        else:
                            yield delta
                        response = {"type": "response", "finish_reason": "stop", "usage": {},
                                    "message": {"role": "assistant", "content": prose}}
                    else:
                        log.info("run=%s tool_protocol_correction=%d", run_id, protocol_failures)
                        messages.append({"role": "system", "content": protocol_correction(exc)})
                        # Text of the rejected reply may already have been streamed; say so.
                        yield {"type": "protocol_correction", "attempt": protocol_failures}
                        continue
                except ProviderError as exc:
                    if exc.reason != "rate_limit_reached" or rate_retries >= config.RATE_LIMIT_RETRIES:
                        raise
                    rate_retries += 1
                    delay = min(60, config.RATE_LIMIT_DELAY * rate_retries)
                    log.info("run=%s rate_limit_retry=%d delay_seconds=%s", run_id, rate_retries, delay)
                    yield {"type": "provider_retry", "reason": "rate_limit_reached", "attempt": rate_retries, "delay_seconds": delay}
                    until = time.monotonic() + delay
                    while time.monotonic() < until:
                        control.check()
                        control.cancelled.wait(min(0.1, max(0, until - time.monotonic())))
                    control.check()
                    continue
                protocol_failures = 0
                if response is None:
                    raise ProviderError("Provider omitted its final response")
                msg = response["message"]
                for key in total_usage:
                    value = (response.get("usage") or {}).get(key, 0)
                    if isinstance(value, int) and value >= 0:
                        total_usage[key] += value
                if execute:
                    response["usage"] = dict(total_usage)
                calls = normalize_calls(msg.get("tool_calls", []))
                if choice == "none" and calls:
                    raise ProviderError("Provider called tools despite tool_choice=none")
                if (choice == "required" or isinstance(choice, dict)) and not calls:
                    raise ProviderError("Provider did not call the required tool")
                for call in calls:
                    if isinstance(choice, dict) and call["function"]["name"] != choice["function"]["name"]:
                        raise ProviderError("Provider called the wrong forced tool")
                    if not execute:
                        try:
                            parse_arguments(call, tools)
                        except (ValueError, TypeError) as exc:
                            raise ProviderError("Invalid model function arguments") from exc
                if calls:
                    msg["tool_calls"] = calls
                messages.append(msg)
                if not execute or not calls:
                    blocked = execute and control.plan_updated and any(step["status"] == "blocked" for step in session.plan)
                    unfinished = execute and not blocked and control.mode == "build" and control.plan_updated and any(
                        step["status"] in ("pending", "in_progress") for step in session.plan)
                    if unfinished:
                        if continuation_corrections >= 2:
                            raise HarnessError("Agent stopped with an unfinished plan; inspect the preserved session and continue",
                                               "unfinished_plan", 409)
                        continuation_corrections += 1
                        messages.append({"role": "system", "content":
                            "Your current implementation plan is unfinished. Continue the authorized task using tools now. "
                            "For low-risk choices use a sensible default (a simple portfolio is static HTML/CSS/JS). "
                            "An optional preference is not a blocker. If required information or permission truly prevents "
                            "progress, mark the blocked plan step with status=blocked and a concrete reason, then ask the "
                            "necessary question. Complete steps only with actual evidence."})
                        session.messages = copy.deepcopy(messages)
                        self.save(session)
                        yield {"type": "continuation_required", "attempt": continuation_corrections}
                        continue
                    if execute and not blocked and control.require_verification and control.changed_files and not any(
                            r["passed"] and not r["stale"] for r in self.verification_status(session)):
                        if verification_corrections >= 2:
                            raise HarnessError("Files changed but no fresh passing verification evidence was produced",
                                               "verification_failed", 409)
                        verification_corrections += 1
                        records = self.verification_status(session)
                        stale = any(r["passed"] and r["stale"] for r in records)
                        changed = getattr(control, "changed_paths", [])[:10]
                        detail = (" Files you changed in this run: " + ", ".join(changed) + "." if changed else "")
                        if stale:
                            detail += (" An earlier check passed but is now stale because files or commands changed after it; "
                                       "run it again as the last step.")
                        messages.append({"role": "system", "content":
                            "The runtime requires verification before completing this run because you changed files. "
                            "Use run_checks for relevant project tests, or validate_files (a quick static check; pass the "
                            "changed paths) when commands are not suitable; "
                            "inspect failures, fix your changes, and rerun. "
                            "No fresh passing check evidence exists. Do not claim verification from proposed commands." + detail})
                        session.messages = copy.deepcopy(messages)
                        self.save(session)
                        yield {"type": "verification_required", "attempt": verification_corrections}
                        continue
                    audit = None if (not execute or blocked) else self._completion_audit(
                        control, messages, tools_run, completion_checks, called_tools)
                    if audit:
                        completion_checks += 1
                        messages.append({"role": "system", "content": audit})
                        session.messages = copy.deepcopy(messages)
                        self.save(session)
                        log.info("run=%s completion_check", run_id)
                        yield {"type": "completion_check"}
                        continue
                    session.messages = copy.deepcopy(messages)
                    self.save(session)
                    for delta in pending_deltas:
                        yield delta
                    metadata = {"task_status": "blocked" if blocked else ("planned" if control.mode == "plan" else "completed")} if execute else {}
                    yield dict(response, session_id=session.id, **metadata)
                    return
                for delta in pending_deltas:
                    yield delta
                tools_run += len(calls)
                called_tools.update(c["function"]["name"] for c in calls)
                # Every call gets a result, including malformed or unknown calls.
                completed = set()
                try:
                    for call in calls:
                        control.check()
                        signature = call_signature(call)
                        previous = session.calls.get(call["id"])
                        if previous and previous[0] != signature:
                            result = json.dumps({"error": "Tool call id reused with different arguments"})
                        elif previous:
                            result = previous[1]
                        elif call["function"]["name"] not in self.registry.read_only and signature in signatures and \
                                signatures[signature][0] == self.registry.activity_revision and (
                                    signatures[signature][2] is None or
                                    signatures[signature][2] == self.registry.fingerprint()):
                            result = json.dumps({"error": "Repeated tool call suppressed; inspect state or make a relevant change before retrying",
                                                 "previous_result": signatures[signature][1]})
                        else:
                            yield {"type": "tool_start", "name": call["function"]["name"], "id": call["id"], "summary": tool_summary(call)}
                            log.info("run=%s tool=%s call=%s", run_id, call["function"]["name"], call["id"])
                            try:
                                arguments = parse_arguments(call, tools)
                            except (ValueError, TypeError) as exc:
                                result = json.dumps({"error": "invalid_arguments", "message": str(exc)})
                            else:
                                # Approval comes first: a declined action is not an
                                # effect, and waiting on a person must not hold the
                                # workspace lock or be journaled as in flight.
                                denial = (None if call["function"]["name"] == "delegate_task"
                                          else self.registry.authorize(call, arguments, control))
                                if denial:
                                    result = json.dumps({"error": "permission_denied", "message": denial})
                                else:
                                    # Persist intent and a fully paired recovery
                                    # transcript before any workspace side effect.
                                    uncertain = '{"error":"Tool outcome uncertain after interruption; inspect workspace before retrying"}'
                                    session.calls[call["id"]] = (signature, uncertain)
                                    recovery = copy.deepcopy(messages)
                                    for pending in calls:
                                        if pending["id"] not in completed:
                                            recovery.append({"role": "tool", "tool_call_id": pending["id"], "content": uncertain})
                                    session.messages = recovery
                                    self.save(session)
                                    if call["function"]["name"] == "delegate_task":
                                        result = yield from self._delegate(session, control, arguments)
                                    else:
                                        result = self.registry.execute(call, control)
                            session.calls[call["id"]] = (signature, result)
                            outcome = json.loads(result)
                            if call["function"]["name"] not in self.registry.read_only and "error" not in outcome and not outcome.get("isError"):
                                signatures[signature] = (self.registry.activity_revision, result, self.registry.fingerprint())
                        # Suppressed calls have results too. Retain their IDs so
                        # a later turn cannot reuse one with different arguments.
                        session.calls.setdefault(call["id"], (signature, result))
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                        completed.add(call["id"])
                        recovery = copy.deepcopy(messages)
                        for pending in calls:
                            if pending["id"] not in completed:
                                recovery.append({"role": "tool", "tool_call_id": pending["id"], "content": '{"error":"Execution interrupted before result; inspect workspace"}'})
                        session.messages = recovery
                        self.save(session)
                        yield {"type": "tool_result", "name": call["function"]["name"], "id": call["id"], "result": result}
                finally:
                    # Preserve side effects and a well-formed transcript even if
                    # cancellation/disconnect interrupts a batch between calls.
                    for call in calls:
                        if call["id"] not in completed:
                            result = '{"error":"Execution interrupted; inspect the workspace before retrying"}'
                            messages.append({"role": "tool", "tool_call_id": call["id"],
                                             "content": result})
                            session.calls.setdefault(call["id"], (call_signature(call), result))
                    session.messages = copy.deepcopy(messages)
                    self.save(session)
                # A forced first call must not force the same tool forever.
                choice = "auto"
            raise HarnessError("Maximum agent turns reached", "turn_limit", 508)
        except ProviderError as exc:
            log.warning("run=%s failure=provider_error", run_id)
            limited = exc.reason == "rate_limit_reached"
            auth = exc.reason == "authentication_error"
            muted = exc.reason == "account_muted"
            code = ("provider_rate_limited" if limited else "provider_authentication" if auth
                    else "provider_account_muted" if muted else "provider_error")
            raise HarnessError(str(exc), code, 429 if limited else 403 if muted else 502) from exc
        except InterruptedError as exc:
            log.info("run=%s cancelled", run_id)
            raise HarnessError("Run cancelled", "cancelled", 409) from exc
        except TimeoutError as exc:
            log.warning("run=%s failure=timeout", run_id)
            raise HarnessError("Run timed out", "timeout", 504) from exc
        except HarnessError:
            raise
        except Exception as exc:
            log.error("run=%s failure=%s", run_id, type(exc).__name__)
            raise
        finally:
            log.info("run=%s session=%s end", run_id, session.id)
            self.release(session, control)
