"""Strict single-file unified patches and conflict-aware file checkpoints."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as fh:
            temp = fh.name
            fh.write(data)
        if mode is not None:
            os.chmod(temp, mode)
        os.replace(temp, path)
        return invalidate_python_cache(path)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


def invalidate_python_cache(path):
    """Avoid stale timestamp/size pyc hits after fast, same-size source edits."""
    path = Path(path)
    if path.suffix != ".py":
        return True
    cache = path.parent / "__pycache__"
    if not cache.is_dir() or cache.is_symlink():
        return True
    succeeded = True
    for entry in cache.glob(path.stem + ".*.pyc"):
        if entry.is_file() and not entry.is_symlink():
            try:
                entry.unlink()
            except OSError:
                succeeded = False
    return succeeded


def fit_newlines(value, existing):
    """Give ``value`` the line-ending style of ``existing`` when that style is consistent.

    A model writes "\\n" even for a CRLF file, and a mixed result would show up as a whole-file diff. A file that
    is entirely CRLF gets CRLF, a file with no carriage returns gets LF, a file with mixed endings is left to the
    exact text the caller supplied."""
    crlf = existing.count("\r\n")
    if crlf and crlf == existing.count("\n"):
        return value.replace("\r\n", "\n").replace("\n", "\r\n")
    if "\r" not in existing:
        return value.replace("\r\n", "\n")
    return value


def unified_patch(original, patch):
    source = original.splitlines(keepends=True)
    lines = patch.splitlines(keepends=True)
    result, cursor, i, hunks = [], 0, 0, 0
    newline = "\r\n" if original.count("\r\n") and original.count("\r\n") == original.count("\n") else "\n"
    while i < len(lines) and lines[i].startswith(("--- ", "+++ ")):
        i += 1
    while i < len(lines):
        header = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?", lines[i])
        if not header:
            raise ValueError("Expected a unified diff hunk; one file per call")
        old_start, old_count, new_start, new_count = header.groups()
        old_count, new_count = int(old_count or 1), int(new_count or 1)
        start = int(old_start) - (1 if old_count else 0)
        if start < cursor or start > len(source):
            raise ValueError("Patch hunk position is invalid or overlaps")
        result.extend(source[cursor:start]); cursor = start
        if int(new_start) - (1 if new_count else 0) != len(result):
            raise ValueError("Patch new line position is inconsistent")
        removed, added = 0, 0
        i += 1
        while i < len(lines) and not lines[i].startswith("@@ "):
            line = lines[i]
            if not line or line[0] not in " +-":
                raise ValueError("Invalid patch line")
            marker, value = line[0], line[1:]
            if i + 1 < len(lines) and lines[i + 1].startswith("\\ No newline at end of file"):
                value = value.rstrip("\r\n"); i += 1
            if marker in " -":
                if cursor >= len(source) or source[cursor].replace("\r\n", "\n") != value.replace("\r\n", "\n"):
                    raise ValueError("Patch context does not match the current file")
                existing = source[cursor]
                cursor += 1; removed += 1
            if marker in " +":
                if marker == " ":
                    result.append(existing)
                else:
                    result.append(value.rstrip("\r\n") + newline if value.endswith(("\n", "\r")) else value)
                added += 1
            i += 1
        if (removed, added) != (old_count, new_count):
            raise ValueError("Patch line counts do not match")
        hunks += 1
    if not hunks:
        raise ValueError("Patch contains no hunks")
    return "".join(result + source[cursor:])


def checkpoint(path, root, new_data):
    import base64
    old = path.read_bytes() if path.exists() else None
    return {"path": str(path.relative_to(root)), "before": base64.b64encode(old).decode() if old is not None else None,
            "mode": stat.S_IMODE(path.stat().st_mode) if path.exists() else None,
            "after": digest(new_data)}


def undo_checkpoint(item, registry):
    import base64
    changes = item.get("changes", [item])
    targets = [(registry.path(change["path"]), change) for change in changes]
    # Validate every affected path before restoring any part of a grouped edit.
    for target, change in targets:
        actual = digest(target.read_bytes()) if target.is_file() else None
        if target.exists() and not target.is_file() or actual != change["after"]:
            raise ValueError("Checkpoint conflicts with current file; no change made")
    for target, change in targets:
        if change["before"] is None:
            if target.exists():
                target.unlink()
        else:
            atomic_write(target, base64.b64decode(change["before"]), change["mode"])


def deleted_checkpoint(path, root):
    item = checkpoint(path, root, b"")
    item["after"] = None
    return item
