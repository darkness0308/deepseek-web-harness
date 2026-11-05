"""Session-owned background processes (dev servers, watchers, long builds).

Output is spooled to a temporary file so a chatty process cannot exhaust memory.
Every process belongs to the session that started it; other sessions cannot see,
read or stop it. All processes are killed with their tree when the harness exits.
"""
from __future__ import annotations

import atexit
import os
import subprocess
import tempfile
import threading
import time
import uuid

from . import config
from .permissions import child_environment

_SPOOL_LIMIT = 16_000_000
_KEEP_FINISHED = 16


def kill_tree(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class _Entry:
    def __init__(self, pid_label, owner, argv, shell_text, cwd, process, spool):
        self.id, self.owner, self.argv, self.command = pid_label, owner, argv, shell_text
        self.cwd, self.process, self.spool = cwd, process, spool
        self.started = time.time()
        self.read_offset = 0


class ProcessManager:
    def __init__(self):
        self.entries = {}
        self.guard = threading.Lock()
        atexit.register(self.shutdown)

    def _owned(self, owner, pid_label):
        entry = self.entries.get(pid_label)
        if entry is None or entry.owner != owner:
            raise ValueError("No such background process in this session")
        return entry

    def start(self, owner, argv, cwd, command_text):
        with self.guard:
            running = [e for e in self.entries.values() if e.owner == owner and e.process.poll() is None]
            if len(running) >= config.MAX_PROCESSES:
                raise ValueError(f"At most {config.MAX_PROCESSES} background processes per session; stop one first")
            # Finished processes keep their output readable, but not forever: drop the
            # oldest ones so their temporary spool files cannot accumulate.
            finished = sorted((e for e in self.entries.values() if e.owner == owner and e.process.poll() is not None),
                              key=lambda e: e.started)
            for old in finished[:max(0, len(finished) - _KEEP_FINISHED)]:
                old.spool.close()
                self.entries.pop(old.id, None)
            spool = tempfile.TemporaryFile()
            try:
                process = subprocess.Popen(argv, cwd=cwd, shell=False, stdin=subprocess.DEVNULL,
                                           stdout=spool, stderr=subprocess.STDOUT, env=child_environment(),
                                           start_new_session=(os.name != "nt"))
            except BaseException:
                spool.close()
                raise
            label = "proc_" + uuid.uuid4().hex[:8]
            self.entries[label] = _Entry(label, owner, argv, command_text, str(cwd), process, spool)
            return label

    def _snapshot(self, entry):
        size = os.fstat(entry.spool.fileno()).st_size
        return {"id": entry.id, "running": entry.process.poll() is None, "exit_code": entry.process.poll(),
                "pid": entry.process.pid, "command": entry.command[:300],
                "seconds": round(time.time() - entry.started, 1), "output_bytes": size}

    def read(self, owner, pid_label, wait_seconds=0.0, max_chars=None, control=None):
        with self.guard:
            entry = self._owned(owner, pid_label)
        limit = max_chars or max(1000, config.MAX_TOOL_OUTPUT // 2)
        deadline = time.monotonic() + max(0.0, min(wait_seconds, 60.0))
        while time.monotonic() < deadline and entry.process.poll() is None:
            if os.fstat(entry.spool.fileno()).st_size > entry.read_offset:
                break
            if control is not None:
                control.check()
            time.sleep(0.1)
        if os.fstat(entry.spool.fileno()).st_size > _SPOOL_LIMIT and entry.process.poll() is None:
            kill_tree(entry.process)
        entry.spool.seek(entry.read_offset)
        data = entry.spool.read(limit)
        clipped, used = "", len(data)
        for trim in range(4):  # do not split a multibyte character at the tail
            try:
                clipped, used = data[:len(data) - trim].decode("utf-8"), len(data) - trim
                break
            except UnicodeDecodeError:
                continue
        else:
            clipped = data.decode("utf-8", "replace")
        # Advance by the bytes actually returned so a later read continues exactly.
        entry.read_offset += used
        result = self._snapshot(entry)
        remaining = result["output_bytes"] - entry.read_offset
        result.update(output=clipped, more_output=remaining > 0)
        return result

    def stop(self, owner, pid_label):
        with self.guard:
            entry = self._owned(owner, pid_label)
        kill_tree(entry.process)
        return self._snapshot(entry)

    def list(self, owner):
        with self.guard:
            return [self._snapshot(e) for e in self.entries.values() if e.owner == owner]

    def stop_owner(self, owner):
        with self.guard:
            doomed = [e for e in self.entries.values() if e.owner == owner]
            for entry in doomed:
                kill_tree(entry.process)
                entry.spool.close()
                self.entries.pop(entry.id, None)

    def shutdown(self):
        with self.guard:
            for entry in list(self.entries.values()):
                kill_tree(entry.process)
                try:
                    entry.spool.close()
                except OSError:
                    pass
            self.entries.clear()
