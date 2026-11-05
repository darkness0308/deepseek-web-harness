"""Metadata discovery and explicit skill activation; no script execution."""
from __future__ import annotations

from pathlib import Path
import yaml


class SkillLibrary:
    def __init__(self, roots=None):
        from . import config
        self.roots = [Path(p).resolve() for p in (config.SKILL_PATHS if roots is None else roots)]
        self.entries = {}
        self.available_servers = set()
        self.errors = []
        self.refresh()

    def refresh(self):
        self.entries, self.errors = {}, []
        for root in self.roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("SKILL.md")):
                if not path.resolve().is_relative_to(root):
                    continue
                try:
                    text = self._text(path, 100000)
                    if not text.startswith("---\n"):
                        raise ValueError("Missing frontmatter")
                    head, body = text[4:].split("\n---", 1)
                    meta = yaml.safe_load(head)
                    if not isinstance(meta, dict) or not isinstance(meta.get("name"), str) or not isinstance(meta.get("description"), str):
                        raise ValueError("Invalid metadata")
                    name = meta["name"]
                    if not name or len(name) > 100 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in name):
                        raise ValueError("Invalid skill name")
                    if name in self.entries:
                        raise ValueError("Duplicate skill name")
                    # Shared Harness references use paths relative to package root.
                    package = path.parent
                    for ancestor in (path.parent, *path.parents):
                        if not ancestor.is_relative_to(root):
                            break
                        if (ancestor / "PROVENANCE.json").is_file():
                            package = ancestor
                            break
                    if not isinstance(meta.get("metadata", {}), dict):
                        raise ValueError("Invalid skill metadata")
                    self.entries[name] = {"name": name, "description": meta["description"],
                        "compatibility": str(meta.get("compatibility", "")),
                        "license": str(meta.get("license", "")),
                        "mcp_server": (meta.get("metadata") or {}).get("mcp-server", ""),
                        "path": path, "package": package, "body": body.strip()}
                except (ValueError, OSError, yaml.YAMLError):
                    self.errors.append(str(path.relative_to(root)))

    @staticmethod
    def _text(path, limit):
        with path.open(encoding="utf-8") as fh:
            text = fh.read(limit + 1)
        if len(text) > limit:
            raise ValueError("Skill resource exceeds size limit")
        return text

    def catalog(self):
        return [{k: entry[k] for k in ("name", "description", "compatibility", "license", "mcp_server")}
                for entry in self.entries.values()]

    def entry(self, name):
        if name not in self.entries:
            raise ValueError("Unknown skill")
        return self.entries[name]

    def activate(self, names):
        if not isinstance(names, list) or len(names) > 8 or any(not isinstance(n, str) for n in names):
            raise ValueError("skills must be an array of at most eight names")
        result = []
        for name in dict.fromkeys(names):
            entry = self.entry(name)
            if entry["mcp_server"] and entry["mcp_server"] not in self.available_servers:
                raise ValueError(f"Skill {name} requires configured MCP server {entry['mcp_server']}")
            result.append(name)
        return result

    def resource(self, name, resource="SKILL.md"):
        entry = self.entry(name)
        path = (entry["path"].parent / resource).resolve()
        if not path.is_relative_to(entry["package"]) or not path.is_file():
            raise ValueError("Skill resource is outside its package or does not exist")
        return self._text(path, 50000)

    def instructions(self, names):
        return "\n\n".join(f"Activated skill: {name}\n{self.entry(name)['body']}" for name in names)
