"""Optional MCP stdio tools. JSON-RPC lifecycle, bounded I/O, no auto-retries.

Only locally configured servers and explicitly allowed tools are exposed.
Sampling, elicitation, resources and HTTP transports are not implemented.
"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

from .config import VERSION
from .schema import check_schema


class MCPError(ValueError):
    pass


class Deadline:
    def __init__(self, seconds=30):
        self.deadline = time.monotonic() + seconds

    def check(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("MCP operation timed out")


class StdioClient:
    VERSIONS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}

    def __init__(self, spec, root):
        self.spec, self.root = spec, root
        self.process = None
        self.guard = threading.RLock()
        self.write_lock = threading.Lock()
        self.sequence = 0
        self.tools = []
        atexit.register(self.close)

    def _write(self, value, control=None):
        data = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        if len(data) > 1000000:
            raise MCPError("MCP request exceeds size limit")
        stream = self.process.stdin
        finished, failures = threading.Event(), []

        def writer():
            try:
                with self.write_lock:
                    stream.write(data); stream.flush()
            except (OSError, ValueError) as exc:
                failures.append(exc)
            finally:
                finished.set()

        threading.Thread(target=writer, daemon=True).start()
        control = control or Deadline(self.spec.get("timeout", 30))
        deadline = time.monotonic() + self.spec.get("timeout", 30)
        while not finished.wait(0.05):
            control.check()
            if time.monotonic() >= deadline:
                raise TimeoutError("MCP write timed out")
        if failures:
            raise MCPError("MCP server disconnected")

    def _request(self, method, params, control):
        control.check()
        self.sequence += 1
        rid = self.sequence
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, control)
        deadline = time.monotonic() + self.spec.get("timeout", 30)
        try:
            while True:
                control.check()
                if time.monotonic() >= deadline:
                    raise TimeoutError("MCP operation timed out")
                try:
                    message = self.inbox.get(timeout=0.05)
                except queue.Empty:
                    if self.failed.is_set():
                        raise MCPError("MCP server disconnected or emitted invalid protocol data")
                    continue
                if message.get("jsonrpc") != "2.0":
                    raise MCPError("Invalid MCP response")
                if message.get("method"):
                    if "id" in message:
                        self._write({"jsonrpc": "2.0", "id": message["id"], "error": {
                            "code": -32601, "message": "Client capability is not supported"}})
                    elif message["method"] == "notifications/tools/list_changed":
                        # Definitions must stay stable during a run.
                        raise MCPError("MCP tool list changed; restart the harness to rediscover schemas")
                    continue
                if message.get("id") != rid:
                    raise MCPError("MCP response id mismatch")
                if "error" in message:
                    raise MCPError("MCP server rejected the request")
                if not isinstance(message.get("result"), dict):
                    raise MCPError("Invalid MCP result")
                return message["result"]
        except (InterruptedError, TimeoutError):
            try:
                self._write({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {
                    "requestId": rid, "reason": "Harness run cancelled or timed out"}}, Deadline(0.1))
            except (MCPError, TimeoutError):
                pass
            raise

    def connect(self, control):
        if self.process is not None and self.process.poll() is None:
            return
        self.close()
        # Inherit runtime discovery variables, not arbitrary provider secrets.
        environment = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA") if k in os.environ}
        for key in self.spec.get("env_keys", []):
            if key not in os.environ:
                raise MCPError("Configured MCP environment variable is missing")
            environment[key] = os.environ[key]
        try:
            self.process = subprocess.Popen([self.spec["command"], *self.spec.get("args", [])],
                cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=environment, shell=False, start_new_session=(os.name != "nt"))
        except OSError as exc:
            raise MCPError("Could not start configured MCP server") from exc
        self.inbox, self.failed = queue.Queue(maxsize=128), threading.Event()
        stream, inbox, failed = self.process.stdout, self.inbox, self.failed

        def reader():
            try:
                while True:
                    line = stream.readline(1000001)
                    if not line:
                        return
                    if len(line) > 1000000 or not line.endswith(b"\n"):
                        return
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        return
                    inbox.put_nowait(value)
            except (ValueError, OSError, queue.Full):
                pass
            finally:
                failed.set()

        threading.Thread(target=reader, daemon=True).start()
        try:
            init = self._request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "native-coding-harness", "version": VERSION}}, control)
            if init.get("protocolVersion") not in self.VERSIONS or "tools" not in init.get("capabilities", {}):
                raise MCPError("MCP server did not negotiate a supported tools capability")
            self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools, cursor, seen = [], None, set()
            for _ in range(20):
                page = self._request("tools/list", {"cursor": cursor} if cursor else {}, control)
                if not isinstance(page.get("tools"), list):
                    raise MCPError("MCP tools/list must return an array")
                tools.extend(page["tools"])
                cursor = page.get("nextCursor")
                if not cursor:
                    break
                if not isinstance(cursor, str) or cursor in seen:
                    raise MCPError("Invalid MCP pagination cursor")
                seen.add(cursor)
            else:
                raise MCPError("MCP tool pagination exceeded limit")
            self.tools = tools
        except BaseException:
            self.close()
            raise

    def call(self, tool, arguments, schema, control):
        while not self.guard.acquire(timeout=0.05):
            control.check()
        try:
            self.connect(control)
            current = next((t for t in self.tools if t.get("name") == tool), {})
            if current.get("inputSchema") != schema:
                raise MCPError("MCP schema changed; restart harness before invoking this tool")
            return self._request("tools/call", {"name": tool, "arguments": arguments}, control)
        except BaseException:
            # Outcome may be uncertain: never transparently retry an effect.
            self.close()
            raise
        finally:
            self.guard.release()

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=5)
                else:
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()


class MCPHub:
    def __init__(self, path=None, root=None):
        self.clients, self.bindings, self.definitions, self.read_only = {}, {}, [], set()
        if not path:
            return
        try:
            specs = json.loads(Path(path).read_text(encoding="utf-8"))["servers"]
            if not isinstance(specs, dict):
                raise ValueError()
            for server, spec in specs.items():
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", server) or not isinstance(spec, dict):
                    raise ValueError()
                if not isinstance(spec.get("command"), str) or not spec["command"]:
                    raise ValueError()
                for key in ("args", "env_keys", "allow_tools", "read_only_tools"):
                    if not isinstance(spec.get(key, []), list) or any(not isinstance(a, str) for a in spec.get(key, [])):
                        raise ValueError()
                if not isinstance(spec.get("timeout", 30), (int, float)) or not 0 < spec.get("timeout", 30) <= 300:
                    raise ValueError()
                client = StdioClient(spec, root)
                self.clients[server] = client
                client.connect(Deadline(spec.get("timeout", 30)))
                allow = set(spec.get("allow_tools", []))
                discovered = set()
                for tool in client.tools:
                    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or tool["name"] in discovered:
                        raise ValueError()
                    discovered.add(tool["name"])
                    if tool["name"] not in allow:
                        continue
                    # Names are stable and model-provider compatible.
                    name = "mcp_" + server.replace("-", "_") + "__" + tool["name"].replace(".", "_").replace("-", "_")
                    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name) or name in self.bindings:
                        raise ValueError()
                    schema = tool["inputSchema"]
                    check_schema(schema)
                    if not isinstance(schema, dict):
                        raise ValueError()
                    self.bindings[name] = (server, tool["name"], schema)
                    self.definitions.append({"type": "function", "function": {"name": name,
                        "description": str(tool.get("description", "MCP tool"))[:4000], "parameters": schema}})
                    if tool["name"] in spec.get("read_only_tools", []):
                        self.read_only.add(name)
                if allow - discovered:
                    raise ValueError()
        except Exception as exc:
            self.close()
            raise MCPError("Invalid MCP configuration, tools or unavailable server; check command and environment") from exc

    def call(self, name, arguments, control):
        server, tool, schema = self.bindings[name]
        return self.clients[server].call(tool, arguments, schema, control)

    def close(self):
        for client in self.clients.values():
            client.close()
