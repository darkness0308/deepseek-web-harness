"""Standalone native terminal agent. No HTTP client or external harness required.

Model and tool work runs in a worker thread; only this module's calling thread
reads input and writes output, so approvals, questions and cancellation never
interleave with streamed text. API clients have their own session namespace.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from . import commands as prompt_commands, config, permissions
from .agent import Control, HarnessError

HELP = [
    ("/help", "Show this help"),
    ("!<command>", "Run a shell command yourself, e.g. !git status (the agent is not involved)"),
    ("/new, /clear", "Start a fresh session (keeps mode and directory for /clear)"),
    ("/delete [id]", "Delete this (or another) saved session and its chat on DeepSeek"),
    ("/sessions, /resume <id>", "List durable sessions / continue one"),
    ("/fork", "Copy this session into an isolated new one"),
    ("/cd <path>, /pwd", "Change / show this session's directory (bare cd, pwd work too)"),
    ("/plan, /build", "Read-only planning mode / mode that can edit files"),
    ("/permissions [ask|auto]", "Show the approval policy; auto approves commands this session"),
    ("/think [on|off]", "Toggle deep thinking (slower, better for hard problems)"),
    ("/init, /review, /explain, /fix", "Built-in prompt commands (add your own in .harness/commands/)"),
    ("/diff, /undo", "Review file changes / restore the latest checkpoint"),
    ("/checks, /status, /cost", "Verification evidence, session status, token usage"),
    ("/doctor [live]", "Diagnose configuration, dependencies and the provider"),
    ("/connect [code]", "Wait for the browser extension's Connect click (Esc cancels)"),
    ("/refresh", "Re-run the browser login and reload the session"),
    ("/tools, /mcp, /skills", "Inspect tools, MCP servers and skills"),
    ("/skill <name>, /unskill <name>", "Activate / deactivate a skill"),
    ("/compact [focus]", "Summarize earlier steps to free context (focus text optional)"),
    ("/context", "Show where the context budget goes and when auto-compact runs"),
    ("/history, /export <path>", "Show the transcript / save it to a file"),
    ("/prompt, /config, /model <name>", "Inspect the prompt and settings, switch model"),
    ("/quit", "Exit (Ctrl+C cancels the running task)"),
]
ERROR_HINTS = {
    "provider_authentication": "Type /connect and press Connect in the browser extension (or run: python refresh_session.py), then retry.",
    "provider_account_muted": "DeepSeek muted this account's chat temporarily. Retrying will not help: wait until the time shown.",
    "provider_rate_limited": "The provider is rate limiting this account. Wait a minute and send 'continue'.",
    "timeout": "The run hit HARNESS_TIMEOUT. Send 'continue' to resume, or raise HARNESS_TIMEOUT.",
    "turn_limit": "The agent used its turn budget. Send 'continue' to keep going, or raise HARNESS_MAX_TURNS.",
    "context_limit": "The conversation no longer fits the context budget. Try /compact or /new.",
    "unfinished_plan": "Work was kept. Send 'continue' to resume the plan.",
    "verification_failed": "Edits were kept but no passing check exists yet. Ask it to run the tests, or use /diff.",
    "provider_error": "Provider failure; run /doctor live to test connectivity. Details: logs/harness.log.",
}


def clean(text, keep_newlines=True):
    """Make model- or tool-supplied text safe to print.

    Control characters (ESC sequences, carriage returns, backspaces...) could
    redraw the screen and disguise what an approval prompt really says, so they
    are shown as visible escapes instead.
    """
    allowed = "\n\t" if keep_newlines else ""
    return "".join(ch if (ch.isprintable() or ch in allowed) else f"\\x{ord(ch):02x}" for ch in str(text))


class Ui:
    """ANSI colour only on a real terminal, decided per call so redirected output stays plain."""
    CODES = {"dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33", "blue": "34",
             "magenta": "35", "cyan": "36"}
    _vt_ready = False

    @classmethod
    def enabled(cls):
        stream = sys.stdout
        if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb" or not getattr(stream, "isatty", lambda: False)():
            return False
        if os.name == "nt" and not cls._vt_ready:
            try:
                import ctypes
                kernel = ctypes.windll.kernel32
                handle = kernel.GetStdHandle(-11)
                mode = ctypes.c_uint32()
                if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                    kernel.SetConsoleMode(handle, mode.value | 0x0004)
                cls._vt_ready = True
            except Exception:  # noqa: BLE001 - colour is cosmetic
                return False
        return True

    @classmethod
    def paint(cls, name, text):
        return f"\033[{cls.CODES[name]}m{text}\033[0m" if cls.enabled() else text


def _stdin_pending():
    """True when more pasted input is already waiting in the terminal buffer."""
    try:
        if not sys.stdin.isatty():
            return False
        if os.name == "nt":
            import msvcrt
            return msvcrt.kbhit()
        import select
        return bool(select.select([sys.stdin], [], [], 0.03)[0])
    except (OSError, ValueError, ImportError):
        return False


def read_input(prompt):
    """Read one message. Supports pasted multi-line text, ``\"\"\"`` blocks and a
    trailing ``\\`` continuation, so a pasted brief is one request, not many."""
    first = input(prompt)
    stripped = first.strip()
    if stripped in ('"""', "'''"):
        lines = []
        while True:
            line = input("... ")
            if line.strip() == stripped:
                return "\n".join(lines)
            lines.append(line)
    lines = [first]
    plain = not stripped.startswith("/") and stripped.split(" ")[0].lower() not in ("cd", "pwd")
    while plain and lines[-1].endswith("\\") and not lines[-1].endswith("\\\\"):
        lines[-1] = lines[-1][:-1]
        lines.append(input("... "))
    while _stdin_pending():
        lines.append(input(""))
    return "\n".join(lines).strip()


def _ask_choice(question, options):
    print(Ui.paint("cyan", "\n? " + clean(question)))
    for index, option in enumerate(options, 1):
        print(f"  {index}. {clean(option, False)}")
    try:
        answer = input("answer > ").strip()
    except EOFError:  # input closed (piped stdin, Ctrl+Z): the model must not hang waiting
        return "(no answer: input was closed)"
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1]
    return answer


def describe_compaction(info):
    """One readable line (or two) for a compaction result or an auto_compact event."""
    if info.get("compacted") is False:
        return "Nothing to compact: " + info.get("reason", "history is short") + "."
    how = "model-written summary" if info.get("method") == "summary" else "extractive handoff (no summary)"
    before, after = info.get("characters_before"), info.get("characters_after")
    sizes = f" {before:,} -> {after:,} characters" if before is not None and after is not None else ""
    text = f"[context compacted: {info.get('steps_folded', '?')} earlier step(s) folded via {how};{sizes}]"
    if info.get("warning"):
        text += "\n  note: " + clean(info["warning"], False)
    return text


class Terminal:
    def __init__(self, runtime, session_id=None, auto_approve=False, mode=None):
        self.runtime = runtime
        self.owner = "terminal"
        self.model = config.MODEL
        self.thinking = False
        self.auto_approve = auto_approve
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.runs = 0
        self.defaults = {"require_verification": bool(runtime.verification_tools(runtime.registry.definitions))}
        if mode:
            self.defaults["mode"] = mode
        self.session = runtime.get(session_id, self.owner) if session_id else runtime.create(self.owner, self.defaults)

    # ── output ────────────────────────────────────────────────────────────────

    def banner(self):
        registry = self.runtime.registry
        print(Ui.paint("bold", f"Native coding harness | {config.PROVIDER} | {self.model}"))
        print(f"Workspace: {registry.root}")
        print(f"Commands: {registry.command_policy} | web: {registry.web_policy} | mode: {self.session.mode}"
              f" | tools: {len(registry.definitions)}")
        print(Ui.paint("dim", "Type a task, or /help. Paste multi-line text freely. Ctrl+C cancels a run."))

    def help(self):
        for name, text in HELP:
            print(f"  {Ui.paint('cyan', name.ljust(34))} {text}")
        custom = prompt_commands.load(self.runtime.registry.root)
        print(Ui.paint("dim", "  prompt commands: " + ", ".join("/" + n for n in sorted(custom))))

    # ── approvals and questions (worker -> terminal thread) ───────────────────

    def make_approver(self, output, cancelled):
        def approver(request):
            if self.auto_approve:
                return "once"
            reply = queue.Queue(maxsize=1)
            output.put({"type": "approval_request", "reply": reply, **request})
            while not cancelled.is_set():
                try:
                    return reply.get(timeout=0.1)
                except queue.Empty:
                    continue
            return "deny"
        return approver

    def make_asker(self, output, cancelled):
        def asker(question, options):
            reply = queue.Queue(maxsize=1)
            output.put({"type": "ask_request", "question": question, "options": options, "reply": reply})
            while not cancelled.is_set():
                try:
                    return reply.get(timeout=0.1)
                except queue.Empty:
                    continue
            return "(the user cancelled)"
        return asker

    def answer_approval(self, event):
        print(Ui.paint("yellow", "\n! Permission requested: ") + clean(event["summary"]))
        try:
            choice = input("  Allow? [y] once  [a] always (this session)  [n] no  > ").strip().lower()
        except EOFError:
            choice = "n"
        decision = {"y": "once", "yes": "once", "a": "always", "always": "always"}.get(choice, "deny")
        event["reply"].put(decision)
        if decision == "deny":
            print(Ui.paint("dim", "  declined"))

    # ── running a task ────────────────────────────────────────────────────────

    def run(self, prompt):
        runtime, output, cancelled = self.runtime, queue.Queue(), threading.Event()
        body = {"session_id": self.session.id, "model": self.model,
                "messages": [{"role": "user", "content": prompt}]}
        if self.thinking:
            body["thinking_enabled"] = True
        try:
            active, control, events = runtime.prepare(
                body, self.owner, execute=True, approver=self.make_approver(output, cancelled),
                asker=self.make_asker(output, cancelled))
        except HarnessError as exc:
            print("Error:", exc)
            return

        def worker():
            try:
                for event in events:
                    output.put(event)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user, details only in logs
                known = isinstance(exc, HarnessError)
                output.put({"type": "error", "code": getattr(exc, "code", "internal_error"),
                            "message": str(exc) if known else "Run failed; see logs/harness.log"})
            finally:
                events.close()
                runtime.release(active, control)
                output.put(None)

        threading.Thread(target=worker, daemon=True).start()
        started, tool_count, mid_line = time.monotonic(), 0, False
        print(Ui.paint("green", "Assistant > "), end="", flush=True)
        try:
            while True:
                try:
                    event = output.get(timeout=0.1)
                except queue.Empty:
                    continue
                if event is None:
                    break
                kind = event["type"]
                if kind == "delta":
                    text = clean(event["delta"].get("content", ""))
                    if text:
                        print(text, end="", flush=True)
                        mid_line = not text.endswith("\n")
                elif kind == "approval_request":
                    self.answer_approval(event)
                elif kind == "ask_request":
                    event["reply"].put(_ask_choice(event["question"], event["options"]))
                elif kind == "tool_start":
                    tool_count += 1
                    detail = (" " + Ui.paint("dim", clean(event["summary"], False))) if event.get("summary") else ""
                    print(f"\n{Ui.paint('cyan', '[tool: ' + event['name'] + ']')}{detail}", flush=True)
                    mid_line = False
                elif kind == "subagent_tool":
                    print(Ui.paint("dim", f"\n  [sub-agent: {clean(event['description'], False)}] {event['name']}"), flush=True)
                elif kind == "verification_required":
                    print("\n[verification required before completion]", flush=True)
                elif kind == "completion_check":
                    print(Ui.paint("dim", "\n[checking the request is fully done]"), flush=True)
                elif kind == "auto_compact":
                    print(Ui.paint("dim", "\n" + describe_compaction(event)), flush=True)
                    mid_line = False
                elif kind == "protocol_correction":
                    print(Ui.paint("dim", "\n[the model's reply was not valid tool JSON and was discarded; asking it to retry]"), flush=True)
                    mid_line = False
                elif kind == "continuation_required":
                    print("\n[continuing unfinished task]", flush=True)
                elif kind == "provider_retry":
                    print(f"\n[provider rate limited; retry {event['attempt']} after {event['delay_seconds']:g}s; Ctrl+C cancels]", flush=True)
                elif kind == "tool_result":
                    self.show_result(event)
                elif kind == "error":
                    print(Ui.paint("red", "\nError: " + event["message"]))
                    hint = ERROR_HINTS.get(event.get("code"))
                    if hint:
                        print(Ui.paint("yellow", hint))
                elif kind == "response":
                    self.session = active
                    for key in self.usage:
                        value = (event.get("usage") or {}).get(key, 0)
                        if isinstance(value, int):
                            self.usage[key] += value
                    if event["finish_reason"] == "length":
                        print("\n[Response truncated]")
                    if event.get("task_status") == "blocked":
                        print("\n[task blocked; required input is recorded in the plan]", flush=True)
        except KeyboardInterrupt:
            cancelled.set()
            runtime.cancel(active.id, self.owner)
            print("\nCancellation requested ...", flush=True)
            if self.await_release(output):
                print("Cancelled.")
            else:
                print("The active operation has not exited yet; the session stays busy until it does.")
        self.runs += 1
        elapsed = time.monotonic() - started
        tail = f"{elapsed:.1f}s" + (f" | {tool_count} tool call{'s' if tool_count != 1 else ''}" if tool_count else "")
        print(("\n" if mid_line else "") + Ui.paint("dim", tail))

    @staticmethod
    def await_release(output, seconds=15.0):
        """After a cancel, wait for the worker to finish so the next prompt does not hit
        a still-busy session. Late events are discarded; a second Ctrl+C stops waiting."""
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                try:
                    if output.get(timeout=0.1) is None:
                        return True
                except queue.Empty:
                    continue
        except KeyboardInterrupt:
            pass
        return False

    @staticmethod
    def show_result(event):
        try:
            result = json.loads(event["result"])
        except ValueError:
            return
        if not isinstance(result, dict):
            return
        if result.get("error"):
            print(f"\n[tool error: {clean(result.get('message', result['error']), False)}]", flush=True)
        elif result.get("timed_out"):
            print(f"\n[command timed out; exit: {result.get('exit_code')}]", flush=True)
        elif "exit_code" in result and "checks" not in result:
            print(f"\n[exit: {result['exit_code']}]", flush=True)
        elif event["name"] in ("run_checks", "validate_files"):
            print(f"\n[checks: {'passed' if result['passed'] else 'failed'} | {result['id']}]", flush=True)

    # ── commands ──────────────────────────────────────────────────────────────

    def locked(self, action):
        held = self.session  # an action such as /fork may replace self.session
        if not held.lock.acquire(blocking=False):
            print("Session is busy; wait for cancellation to finish.")
            return None
        try:
            return action()
        except (HarnessError, ValueError, OSError) as exc:
            print("Error:", exc)
            return None
        finally:
            held.lock.release()

    def command(self, prompt):
        """Handle a local command. Returns a prompt to send to the agent, False to
        quit, or None when the input was handled locally."""
        runtime, session = self.runtime, self.session
        name, _, argument = prompt.partition(" ")
        name, argument = name.lower(), argument.strip()
        if name in ("pwd", "/pwd") and not argument:
            try:
                print(runtime.registry.directory(session.cwd))
            except ValueError:
                print("Working directory no longer exists. Use /cd with an existing workspace directory.")
        elif name in ("cd", "/cd"):
            self.locked(lambda: print(runtime.change_directory(session, argument)["directory"]))
        elif name in ("/q", "/quit", "/exit"):
            return False
        elif name in ("/new", "/clear"):
            settings = dict(self.defaults)
            if name == "/clear":
                settings.update(mode=session.mode, cwd=session.cwd)
            self.session = runtime.create(self.owner, settings)
            print("Session:", self.session.id)
        elif name == "/delete":
            self.delete_session(argument)
        elif name == "/sessions":
            listed = runtime.list(self.owner)
            for entry in listed[:30]:
                marker = "*" if entry["id"] == session.id else " "
                print(f"{marker} {entry['id']}  {entry['mode']:<5} {entry['message_count']:>3} msgs  {clean(entry['title'], False) or '(untitled)'}")
            if len(listed) > 30:
                print(f"  ... {len(listed) - 30} older sessions not shown")
            if not listed:
                print("No saved sessions.")
        elif name in ("/load", "/resume"):
            try:
                self.session = runtime.get(argument, self.owner)
                print("Session:", self.session.id)
            except HarnessError as exc:
                print(exc)
        elif name == "/history":
            for message in session.messages:
                print(message["role"] + ":", message.get("content") or "[function calls]")
        elif name == "/model":
            self.model = argument or self.model
            print("Model:", self.model)
            if config.PROVIDER == "deepseek-web":
                print(Ui.paint("dim", "The DeepSeek web backend serves one model; use /think for deep thinking."))
        elif name == "/think":
            self.thinking = {"on": True, "off": False}.get(argument.lower(), not self.thinking)
            print("Deep thinking:", "on" if self.thinking else "off")
        elif name == "/tools":
            print(", ".join(t["function"]["name"] for t in runtime.registry.definitions))
        elif name == "/mcp":
            print("MCP stdio servers:", ", ".join(runtime.registry.mcp.clients) or "none configured")
            for tool, (server, remote, _) in runtime.registry.mcp.bindings.items():
                print(f"{server}/{remote}: {tool}")
        elif name == "/status":
            registry = runtime.registry
            print(f"Session: {session.id} | {len(session.messages)} messages | {config.PROVIDER} | {self.model} | {session.mode}")
            print(f"Skills: {', '.join(session.skills) or 'none'} | context: {session.context} | checkpoints: {len(session.checkpoints)}")
            print("Working directory:", registry.path(session.cwd))
            print(f"Commands: {registry.command_policy}{' (auto-approved this session)' if self.auto_approve else ''}"
                  f" | web: {registry.web_policy} | thinking: {'on' if self.thinking else 'off'}")
        elif name == "/cost":
            print(f"{self.runs} runs | prompt tokens {self.usage['prompt_tokens']} | completion tokens "
                  f"{self.usage['completion_tokens']} | total {self.usage['total_tokens']}")
            if config.PROVIDER == "deepseek-web":
                print(Ui.paint("dim", "The web backend reports one total per request; the prompt/completion split is an "
                                      "estimate (about 4 characters per token)."))
        elif name == "/checks":
            print(json.dumps(runtime.verification_status(session), indent=2))
        elif name == "/prompt":
            from .prompts import coding_prompt
            print(coding_prompt(runtime.registry, session))
        elif name == "/config":
            for key, value in self.config_view():
                print(f"{key}: {value}")
        elif name == "/context":
            self.show_context()
        elif name == "/doctor":
            from .doctor import collect, format_report
            print(format_report(collect(runtime, live=argument.lower() == "live"), color=Ui.enabled()))
        elif name == "/permissions":
            self.permissions(argument.lower())
        elif name == "/refresh":
            self.refresh_login()
        elif name == "/connect":
            listener = self.connect(argument.strip().lower())
            if listener is not None:
                from .connect import wait_for_connection
                wait_for_connection(listener)   # no "You >" prompt until connected, cancelled (Esc) or expired
        elif name == "/skills":
            if argument == "reload":
                runtime.registry.skills.refresh()
            for entry in runtime.registry.skills.catalog():
                prerequisite = f" [requires {entry['mcp_server']}]" if entry["mcp_server"] else ""
                print(entry["name"] + prerequisite + ": " + entry["description"].replace("\n", " "))
        elif name in ("/fork", "/compact", "/undo", "/export", "/plan", "/build", "/skill", "/unskill", "/diff"):
            self.session_action(name, argument)
        elif name == "/help":
            self.help()
        elif prompt == "/":
            self.help()
        else:
            custom = prompt_commands.load(runtime.registry.root).get(name[1:]) if name.startswith("/") else None
            if custom is None:
                print("Unknown command. Use /help.")
            else:
                return prompt_commands.expand(custom[1], argument)
        return None

    def delete_session(self, target):
        """``/delete [id]``: remove a saved session and the remote chat that backed it. Deleting the
        current session continues in a fresh one with the same mode and folder."""
        runtime = self.runtime
        try:
            doomed = runtime.get(target, self.owner) if target else self.session
        except HarnessError as exc:
            print(exc)
            return
        held = doomed.lock.acquire(blocking=False)
        if not held:
            print("Session is busy; wait for cancellation to finish.")
            return
        try:
            runtime.delete(doomed)
        finally:
            doomed.lock.release()
        drain = getattr(runtime.provider, "drain", None)
        if drain is not None:
            drain(8.0)  # the chat on the DeepSeek site is deleted in the background; wait for it
        print("Deleted session:", doomed.id)
        if doomed.id == self.session.id:
            settings = dict(self.defaults, mode=doomed.mode, cwd=doomed.cwd)
            self.session = runtime.create(self.owner, settings)
            print("New session:", self.session.id)

    def shell_escape(self, command):
        """``!command``: the person typed it, so no approval is asked, but the operator's
        policy still applies (commands disabled, deny rules) and the output is sanitised."""
        registry = self.runtime.registry
        if not command:
            print("Usage: !<shell command>   (runs in the session folder)")
            return
        if registry.command_policy == "deny":
            print("Commands are disabled by HARNESS_ALLOW_COMMANDS=false.")
            return
        rule = permissions.denied_by("run_shell", {"command": command}, registry.command_deny)
        if rule is not None:
            print(f"Blocked by the operator's deny rule '{rule}'.")
            return
        control = Control()
        control.cwd = self.session.cwd
        control.authorized.add("local-shell")
        call = {"id": "local-shell", "type": "function",
                "function": {"name": "run_shell", "arguments": json.dumps({"command": command})}}
        try:
            result = json.loads(registry.execute(call, control))
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return
        if result.get("error"):
            print(Ui.paint("red", clean(result.get("message", result["error"]))))
            return
        for stream, color in (("stdout", None), ("stderr", "yellow")):
            text = result.get(stream, "")
            if text:
                print(Ui.paint(color, clean(text.rstrip())) if color else clean(text.rstrip()))
        if result.get("timed_out"):
            print(Ui.paint("yellow", clean(result.get("message", "Command timed out."))))
        elif result.get("exit_code"):
            print(Ui.paint("dim", f"[exit: {result['exit_code']}]"))

    def show_context(self):
        report = self.runtime.context_report(self.session)
        budget, total = report["budget"], report["used"]
        sent = report["in_view"] if report["in_view"] is not None else total   # what the model actually receives
        width = 30
        filled = min(width, round(width * sent / budget)) if budget else 0
        bar = "#" * filled + "." * (width - filled)
        roles = report["by_role"]
        print(f"Context  {sent:,} / {budget:,} characters sent to the model ({round(100 * sent / budget, 1) if budget else 0}%)  [{bar}]")
        print(f"  instructions {report['instructions']:,} | tool definitions {report['tools']:,} | saved conversation {report['conversation']:,}")
        print(f"  conversation: user {roles['user']:,}, assistant {roles['assistant']:,}, tool results {roles['tool']:,}, notes {roles['system']:,}")
        view = report.get("view") or {}
        how = []
        if view.get("microcompacted"):
            how.append(f"{view['microcompacted']} old tool result(s) cleared")
        if view.get("compacted"):
            how.append(f"{report['compact_before']} earlier step(s) folded" + (" into a summary" if view.get("summary") else ""))
        if how or sent < total:
            print(f"  {', '.join(how) or 'trimmed'} for the model; the full transcript ({total:,} characters in all) stays saved")
        if report["summary_chars"]:
            print(f"  summary: {report['summary_chars']:,} characters covering {report['summary_groups']} step(s) | compactions: {report['compactions']}")
        if report["autocompact_at"]:
            print(f"  auto-compact at {report['autocompact_at']:,} characters ({report['autocompact_percent']}%) | "
                  f"{report['clearable_results']} old tool result(s) clearable ({report['clearable_characters']:,} characters)")
        else:
            print("  auto-compact is off (HARNESS_AUTOCOMPACT=0); use /compact")
        for item in report["largest_tool_results"][:3]:
            print(Ui.paint("dim", f"  largest tool result: {item['tool']} {clean(item['arguments'], False)} - {item['characters']:,} characters"))

    def connect(self, argument=""):
        """``/connect`` opens the one-click pairing listener for the browser extension (bridge/connect.py)."""
        from .connect import OUTPUT_LOCK, ConnectError, ConnectListener, describe_event, start_message
        current = getattr(self, "_connect", None)
        if argument == "stop":
            if current is not None and current.active:
                current.stop("stopped by you")
            else:
                print("Not listening.")
            return None
        if argument not in ("", "code"):
            print("Usage: /connect [code|stop]")
            return None
        client = getattr(self.runtime.provider, "client", None)
        if config.PROVIDER != "deepseek-web" or client is None or not hasattr(client, "reload_session"):
            print("Nothing to connect: this provider does not use the DeepSeek web login.")
            return None
        if current is not None and current.active:
            print(start_message(current))
            return current

        def on_event(kind, info):
            if kind == "connected":
                try:
                    client.reload_session()
                except Exception:  # noqa: BLE001 - the session file is already written; /refresh can retry
                    pass
            # Runs on the listener's thread; the lock keeps it from tearing the waiting status line.
            with OUTPUT_LOCK:
                print(Ui.paint("green" if kind == "connected" else "yellow", "\r[connect] " + clean(describe_event(kind, info))))

        from . import deepseek_client
        listener = ConnectListener(deepseek_client._SESSION_FILE, on_event, port=config.CONNECT_PORT,
                                   seconds=config.CONNECT_SECONDS, code=argument == "code")
        try:
            listener.start()
        except ConnectError as exc:
            print("Cannot listen: " + str(exc))
            return None
        self._connect = listener
        print(start_message(listener))
        return listener

    def refresh_login(self):
        """Run the browser login helper, then reload the session in this process."""
        client = getattr(self.runtime.provider, "client", None)
        if config.PROVIDER != "deepseek-web" or client is None or not hasattr(client, "reload_session"):
            print("Nothing to refresh: this provider does not use the DeepSeek web login.")
            return
        script = Path(__file__).resolve().parent.parent / "refresh_session.py"
        print("Opening the login helper; sign in when the browser appears (this can take a minute) ...")
        try:
            code = subprocess.run([sys.executable, str(script)], cwd=str(script.parent)).returncode
        except OSError as exc:
            print("Could not start the login helper:", type(exc).__name__)
            return
        if code != 0:
            print("Login helper did not finish; the session was not changed. Try again or run it from a shell.")
            return
        print("Session reloaded." if client.reload_session() else "Session file unchanged; check refresh_session.py output.")

    def config_view(self):
        registry = self.runtime.registry
        return [("provider", config.PROVIDER), ("model", self.model), ("workspace", registry.root),
                ("commands", registry.command_policy), ("web", registry.web_policy),
                ("command deny/allow rules", f"{len(registry.command_deny)}/{len(registry.command_allow)}"),
                ("max turns", config.MAX_AGENT_TURNS), ("run timeout (s)", config.AGENT_TIMEOUT),
                ("tool timeout (s)", config.TOOL_TIMEOUT), ("context chars", config.CONTEXT_CHARS),
                ("persistence", config.PERSISTENCE), ("tools", len(registry.definitions))]

    def permissions(self, argument):
        registry = self.runtime.registry
        if argument in ("auto", "ask"):
            if argument == "auto" and registry.command_policy == "deny":
                print("Commands are disabled by HARNESS_ALLOW_COMMANDS=false; nothing to approve.")
                return
            self.auto_approve = argument == "auto"
        print(f"Commands: {registry.command_policy} | web: {registry.web_policy} | "
              f"auto-approve this terminal session: {'on' if self.auto_approve else 'off'}")
        rules = sorted(self.runtime.approval_rules.get(self.session.id, ()))
        print("Always-allowed this session:", ", ".join(rules) or "none")
        print("Operator deny rules:", ", ".join(registry.command_deny) or "none", "| allow rules:", ", ".join(registry.command_allow) or "none")
        print(Ui.paint("dim", "Policy changes: HARNESS_ALLOW_COMMANDS / HARNESS_ALLOW_WEB = true | ask | false"))

    def session_action(self, name, argument):
        runtime, session = self.runtime, self.session

        def act():
            if name == "/fork":
                self.session = runtime.fork(session)
                print("Forked session:", self.session.id)
            elif name == "/compact":
                print(Ui.paint("dim", "Compacting earlier steps into a summary (Ctrl+C cancels) ..."), flush=True)
                try:
                    result = runtime.compact(session, argument)
                except KeyboardInterrupt:
                    print("Compaction cancelled; nothing changed.")
                    return
                print(describe_compaction(result))
            elif name == "/undo":
                print(runtime.undo(session))
            elif name == "/diff":
                print(runtime.review(session)["diff"] or "No native file changes.")
            elif name in ("/plan", "/build"):
                runtime.configure(session, {"mode": name[1:]})
                print("Mode:", session.mode)
                print(json.dumps(session.plan, indent=2))
            elif name in ("/skill", "/unskill"):
                names = session.skills + [argument] if name == "/skill" else [n for n in session.skills if n != argument]
                runtime.configure(session, {"skills": names})
                print("Active skills:", ", ".join(session.skills) or "none")
            else:
                target = runtime.registry.path(argument or f"session-{session.id}.json")
                data = runtime.snapshot(session, private=False)
                data.pop("owner", None)
                data["format"] = "native-harness-session-v1"
                # Exclusive creation avoids accidental transcript overwrite.
                with target.open("x", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                print("Exported:", target)
        self.locked(act)

    # ── main loop ─────────────────────────────────────────────────────────────

    def loop(self):
        runtime = self.runtime
        while True:
            try:
                prompt = read_input("\nYou > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                return
            if not prompt:
                continue
            # Idle API requests may evict this object from the in-memory cache.
            # Refresh the canonical session before reading or changing its state.
            try:
                self.session = runtime.get(self.session.id, self.owner)
            except HarnessError:
                self.session = runtime.create(self.owner, self.defaults)
                print("Previous ephemeral session unavailable. New session:", self.session.id)
            if prompt.startswith("!"):
                self.shell_escape(prompt[1:].strip())
                continue
            if prompt.startswith("/") or prompt.split(" ")[0].lower() in ("cd", "pwd"):
                outcome = self.command(prompt)
                if outcome is False:
                    return
                if outcome is None:
                    continue
                prompt = outcome
            self.run(prompt)


def _tolerant_streams():
    """Model text may contain characters a legacy console codepage cannot encode;
    replace them instead of crashing the terminal mid-answer."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def run_terminal(runtime, session_id=None, auto_approve=False, mode=None):
    _tolerant_streams()
    try:
        import readline  # noqa: F401 - line editing and history where available
    except ImportError:
        pass
    terminal = Terminal(runtime, session_id, auto_approve, mode)
    terminal.banner()
    terminal.loop()


def run_once(runtime, prompt, session_id=None, output_format="text", auto_approve=False, mode=None, stdout=None):
    """Run one prompt non-interactively (scripts, CI, quick checks). Returns an exit code.

    Actions needing approval are refused unless ``auto_approve`` is set, because
    nobody is present to answer.
    """
    _tolerant_streams()
    out = stdout or sys.stdout
    owner = "terminal"
    settings = {"require_verification": bool(runtime.verification_tools(runtime.registry.definitions))}
    if mode:
        settings["mode"] = mode
    try:
        session = runtime.get(session_id, owner) if session_id else runtime.create(owner, settings)
    except HarnessError as exc:
        print("Error:", exc, file=sys.stderr)
        return 1
    body = {"session_id": session.id, "model": config.MODEL, "messages": [{"role": "user", "content": prompt}]}
    if mode:
        body["mode"] = mode
    approver = (lambda request: "once") if auto_approve else None
    final, tool_events, error, open_line = None, [], None, False
    try:
        active, control, events = runtime.prepare(body, owner, execute=True, approver=approver)
    except HarnessError as exc:
        print("Error:", exc, file=sys.stderr)
        return 1
    try:
        for event in events:
            if output_format == "stream-json":
                out.write(json.dumps({k: v for k, v in event.items() if k != "reply"}, ensure_ascii=False) + "\n")
                out.flush()
            elif event["type"] == "delta" and output_format == "text":
                piece = event["delta"].get("content", "")
                out.write(piece)
                out.flush()
                open_line = bool(piece) and not piece.endswith("\n")
            elif event["type"] == "tool_start":
                if open_line and output_format == "text":
                    out.write("\n")       # narration of the previous turn must not run into the next turn's text
                    open_line = False
                tool_events.append({"name": event["name"], "summary": event.get("summary", "")})
                if output_format == "text":
                    print(f"\n[tool: {event['name']}] {event.get('summary', '')}", file=sys.stderr)
            elif event["type"] == "auto_compact" and output_format == "text":
                print("\n" + describe_compaction(event), file=sys.stderr)
            elif event["type"] == "response":
                final = event
    except HarnessError as exc:
        error = exc
    except Exception as exc:  # noqa: BLE001 - scripts get a structured failure, not a traceback
        error = HarnessError("Run failed; see logs/harness.log", "internal_error", 500)
        logging.getLogger("harness").error("one-shot run failed (%s)", type(exc).__name__)
    finally:
        events.close()
        runtime.release(active, control)
    if output_format == "json":
        out.write(json.dumps({
            "is_error": error is not None, "session_id": session.id,
            "result": (final or {}).get("message", {}).get("content") if error is None else None,
            "error": {"code": error.code, "message": str(error)} if error else None,
            "task_status": (final or {}).get("task_status"), "tool_calls": tool_events,
            "usage": (final or {}).get("usage")}, ensure_ascii=False) + "\n")
    elif error is not None:
        print("\nError:", error, file=sys.stderr)
        hint = ERROR_HINTS.get(error.code)
        if hint:
            print(hint, file=sys.stderr)
    elif output_format == "text":
        out.write("\n")
    print("Session:", session.id, file=sys.stderr)
    return 1 if error is not None else 0
