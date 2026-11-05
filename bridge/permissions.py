"""Approval policy for actions with effects beyond the workspace file tools.

Policies: ``allow`` runs without asking, ``ask`` needs a human decision from an
interactive client (the terminal), ``deny`` never offers the tool. API runs have
no human to ask, so under ``ask`` they are refused with a clear message instead
of silently running. Summaries are for the approving human only and are never
logged.
"""
from __future__ import annotations

import fnmatch
import ntpath
import os
import re
import time
from urllib.parse import urlsplit

COMMAND_TOOLS = {"run_command", "run_shell", "run_checks", "start_process"}
WEB_TOOLS = {"web_fetch", "web_search"}


def policy_for(name, command_policy, web_policy):
    if name in COMMAND_TOOLS:
        return command_policy
    if name in WEB_TOOLS:
        return web_policy
    return None


def _program(argv):
    return ntpath.basename(str(argv[0])).lower() if argv else ""


# ── Command rules ─────────────────────────────────────────────────────────────
# HARNESS_COMMAND_DENY / HARNESS_COMMAND_ALLOW hold glob patterns (comma separated) that are
# matched, case-insensitively, against the command line. A deny rule refuses a command under
# every policy; an allow rule approves it without asking when the policy is "ask" (so a
# trusted API run can use `pytest*` while everything else stays refused). Rules come from the
# operator's environment, never from the workspace, so a repository cannot grant itself rights.

_SEGMENTS = re.compile(r"&&|\|\||[;|\n]")
_SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")


def parse_rules(text):
    return tuple(rule.strip().lower() for rule in (text or "").split(",") if rule.strip())


def _normalized(argv):
    """Command line with the program reduced to its bare lower-case name."""
    program = re.sub(r"\.(exe|cmd|bat|com)$", "", _program(argv))
    return " ".join([program, *map(str, argv[1:])]).strip().lower()


def command_lines(name, args):
    """The command line(s) a tool call would run, for rule matching."""
    if name == "run_command":
        return [_normalized(args.get("argv", []))]
    if name == "run_shell":
        return [args.get("command", "").lower()]
    if name == "start_process":
        return [args["command"].lower()] if args.get("command") else [_normalized(args.get("argv", []))]
    if name == "run_checks":
        return [_normalized(check.get("argv", [])) for check in args.get("checks", [])]
    return []


def _pieces(line):
    """A shell line and each of its &&, ||, ; and | segments: a deny rule for `git push*`
    must also catch `cd app && git push`."""
    return [line] + [part.strip() for part in _SEGMENTS.split(line) if part.strip()]


def denied_by(name, args, deny):
    """The first deny rule matching the call, or ``None``."""
    if not deny or name not in COMMAND_TOOLS:
        return None
    for line in command_lines(name, args):
        for piece in _pieces(line):
            for rule in deny:
                if fnmatch.fnmatchcase(piece, rule):
                    return rule
    return None


def allowed_by(name, args, allow):
    """True when every command in the call matches an allow rule. Command substitution
    (`$(...)`, backticks) can hide a different command, so it is never auto-approved."""
    if not allow or name not in COMMAND_TOOLS:
        return False
    lines = command_lines(name, args)
    if not lines:
        return False
    for line in lines:
        if _SUBSTITUTION.search(line):
            return False
        segments = [part.strip() for part in _SEGMENTS.split(line) if part.strip()] or [line]
        if not all(any(fnmatch.fnmatchcase(segment, rule) for rule in allow) for segment in segments):
            return False
    return True


def summarize(name, args):
    if name == "run_command":
        return "run " + " ".join(map(str, args.get("argv", [])))
    if name == "run_shell":
        return "shell: " + args.get("command", "")
    if name == "start_process":
        return "start background process: " + (args.get("command") or " ".join(map(str, args.get("argv", []))))
    if name == "run_checks":
        return "run checks: " + "; ".join(f"{c['name']} ({' '.join(map(str, c['argv']))})" for c in args.get("checks", []))
    if name == "web_fetch":
        return "fetch " + args.get("url", "")
    if name == "web_search":
        return "web search: " + args.get("query", "")
    return name


def rule_key(name, args):
    """Stable key for 'always allow'. Shell strings and URLs are matched exactly
    enough that one approval never becomes a blanket grant."""
    if name == "run_command":
        return "run_command:" + _program(args.get("argv", []))
    if name == "run_checks":
        return "run_checks:" + ",".join(sorted({_program(c["argv"]) for c in args.get("checks", [])}))
    if name == "run_shell":
        return "run_shell:" + args.get("command", "")
    if name == "start_process":
        return "start_process:" + (args.get("command") or _program(args.get("argv", [])))
    if name == "web_fetch":
        return "web_fetch:" + (urlsplit(args.get("url", "")).hostname or "")
    if name == "web_search":
        return "web_search"
    return name


def authorize(name, args, call_id, control, policy, allow=(), deny=()):
    """Return ``None`` when the call may run, else a message for the model."""
    rule = denied_by(name, args, deny)
    if rule is not None:
        return (f"This command is blocked by the operator's deny rule '{rule}'. Do not retry it; "
                "choose another approach or explain what you need.")
    if policy in (None, "allow"):
        return None
    if name in COMMAND_TOOLS and getattr(control, "mode", "build") == "plan":
        return None  # plan mode denies these outright; do not bother the user
    if allowed_by(name, args, allow):
        control.authorized.add(call_id)
        return None
    approve = getattr(control, "approve", None)
    if approve is None:
        return ("This action needs interactive approval and no user is attached to this run. "
                "Set HARNESS_ALLOW_COMMANDS=true (or HARNESS_ALLOW_WEB=true) to permit it for API runs, "
                "or continue with tools that do not need approval.")
    rules = getattr(control, "approval_rules", None)
    key = rule_key(name, args)
    if rules is not None and key in rules:
        control.authorized.add(call_id)
        return None
    started = time.monotonic()
    try:
        decision = approve({"tool": name, "summary": summarize(name, args), "key": key})
    finally:
        # Waiting on a person must not consume the run's own deadline.
        waited = time.monotonic() - started
        if hasattr(control, "deadline"):
            control.deadline += waited
    if decision == "always" and rules is not None:
        rules.add(key)
    if decision in ("once", "always"):
        control.authorized.add(call_id)
        return None
    return "The user declined this action. Do not retry it; choose another approach or explain what you need."


def child_environment(extra=None):
    """Environment for subprocesses: harness credentials never reach the model."""
    hidden = {"DS_EMAIL", "DS_PASSWORD", "HARNESS_API_KEY", "BRIDGE_API_KEY"}
    env = {k: v for k, v in os.environ.items() if k.upper() not in hidden}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.update(extra or {})
    return env
