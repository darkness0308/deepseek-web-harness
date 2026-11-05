"""Private SQLite session snapshots with exclusive process ownership.

The database and its lock belong to one harness process. Terminal and API use
that process together; a second process fails closed instead of racing writes.
"""
from __future__ import annotations

import atexit
import json
from pathlib import Path
import sqlite3
import threading


class SessionStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.guard = threading.RLock()
        self.connection = None
        self.lock_file = None
        atexit.register(self.close)

    def _open(self):
        if self.connection is not None:
            return self.connection
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0"); handle.flush()
            handle.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("Session database is in use by another harness process") from exc
        self.lock_file = handle
        try:
            self.connection = sqlite3.connect(str(self.path), check_same_thread=False)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL)")
            self.connection.commit()
            return self.connection
        except BaseException:
            self.close()
            raise

    def save(self, data):
        with self.guard:
            db = self._open()
            with db:
                db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)",
                           (data["id"], data["owner"], json.dumps(data, ensure_ascii=False)))

    def load(self, sid):
        with self.guard:
            row = self._open().execute("SELECT payload FROM sessions WHERE id=?", (sid,)).fetchone()
            return json.loads(row[0]) if row else None

    def list(self, owner):
        """Sessions of one owner, most recently saved first (REPLACE re-inserts a row, so
        the implicit rowid grows with every save)."""
        with self.guard:
            return [json.loads(row[0]) for row in self._open().execute(
                "SELECT payload FROM sessions WHERE owner=? ORDER BY rowid DESC", (owner,))]

    def delete(self, sid):
        with self.guard:
            with self._open() as db:
                db.execute("DELETE FROM sessions WHERE id=?", (sid,))

    def close(self):
        with self.guard:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            if self.lock_file is not None:
                handle, self.lock_file = self.lock_file, None
                handle.seek(0)
                if __import__("os").name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
