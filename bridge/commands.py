"""Prompt-style slash commands: built-ins plus ``<workspace>/.harness/commands/*.md``.

A command is a prompt template sent to the agent; ``$ARGUMENTS`` is replaced by the
text after the command name. Custom commands are plain text: they cannot register
tools or widen permissions, so a file in the workspace never gains authority.
"""
from __future__ import annotations

from pathlib import Path
import re

BUILTIN = {
    "init": ("Create or update AGENTS.md for this repository",
             "Inspect this repository (manifests, entry points, tests, scripts, existing docs and instruction files) and create "
             "or update AGENTS.md at the workspace root with concise, accurate guidance for future agents: what the project is, "
             "how to set up, build, run and test it (exact commands you verified), code conventions you observed, and "
             "pitfalls. Do not invent commands; omit anything you cannot confirm. $ARGUMENTS"),
    "review": ("Review the current uncommitted changes",
               "Review the current changes in this workspace (use git_status and git_diff when it is a repository, otherwise "
               "the session's checkpoints). Report concrete defects first, ordered by severity, each with file and line and the "
               "failing scenario; then risks and missing tests. Do not edit files. $ARGUMENTS"),
    "explain": ("Explain how a part of the codebase works",
                "Investigate the code and explain how this works, citing files and functions you actually read: $ARGUMENTS"),
    "fix": ("Reproduce and fix a reported problem",
            "Reproduce this problem first, find the root cause, make the smallest correct fix, add a regression test and "
            "run the relevant checks: $ARGUMENTS"),
}


def _parse(text):
    description = ""
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
    if match:
        for line in match.group(1).splitlines():
            if line.lower().startswith("description:"):
                description = line.split(":", 1)[1].strip()
        text = text[match.end():]
    return description, text.strip()


def load(root):
    """Return ``{name: (description, template)}``; custom files override built-ins."""
    commands = dict(BUILTIN)
    directory = Path(root) / ".harness" / "commands"
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            name = path.stem.lower()
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", name) or path.stat().st_size > 20000:
                continue
            try:
                description, body = _parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            if body:
                commands[name] = (description or "Custom command", body)
    return commands


def expand(template, argument):
    return template.replace("$ARGUMENTS", argument.strip()).strip()
