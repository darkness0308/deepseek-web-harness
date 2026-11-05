"""Versioned native-agent instructions and factual runtime context."""
from datetime import date
from pathlib import Path
import json
import os
import platform
import shutil
import sys

PROMPT_VERSION = "coding-agent-v5"


def _shell():
    if os.name == "nt":
        return "PowerShell" if (shutil.which("pwsh") or shutil.which("powershell")) else "cmd"
    return "bash" if shutil.which("bash") else "sh"


def project_python(root):
    """The interpreter a *project* should be run with: its own virtualenv, else ``python`` on PATH, and only as a last
    resort the harness's own interpreter (which carries the harness's dependencies, not the project's)."""
    root = Path(root)
    for relative in (".venv/Scripts/python.exe", "venv/Scripts/python.exe", ".venv/bin/python", "venv/bin/python"):
        candidate = root / relative
        if candidate.is_file():
            return str(candidate.resolve()), "project virtualenv"
    for name in ("python", "python3") if os.name != "nt" else ("python", "py"):
        found = shutil.which(name)
        if found and "WindowsApps" not in found:
            return found, "python on PATH"
    return sys.executable, "the harness's own interpreter (no project or PATH Python found)"


def coding_prompt(registry, session=None):
    text = (Path(__file__).resolve().parent.parent / "prompts" / "coding-agent.md").read_text(encoding="utf-8")
    environment = {"workspace": str(registry.root), "os": platform.system(),
        "python_executable": project_python(registry.root)[0], "python_source": project_python(registry.root)[1],
        "commands_enabled": registry.allow_commands,
        "command_policy": registry.command_policy, "web_policy": registry.web_policy,
        "command_deny_rules": list(registry.command_deny),
        "shell": _shell(), "path_separator": os.sep, "date": date.today().isoformat(),
        "git_repository": (registry.root / ".git").exists(),
        "node_available": bool(shutil.which("node")),
        "tools": [t["function"]["name"] for t in registry.definitions],
        "prompt_version": PROMPT_VERSION}
    environment["cwd"] = getattr(session, "cwd", ".")
    environment["working_directory"] = str(registry.path(environment["cwd"]))
    environment["mode"] = getattr(session, "mode", "build")
    return text.strip() + "\n\nFactual runtime environment:\n" + json.dumps(environment, ensure_ascii=False)
