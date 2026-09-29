"""The daemon's two Unix sockets, both in $XDG_RUNTIME_DIR/iris-dictation/.
The line protocols are documented in PROTOCOL.md; other programs depend on
them, so change them only by adding."""

from __future__ import annotations

import os
import queue
import re
import socket
import threading
from collections.abc import Callable

SOCKET_NAME = "iris-dictation.sock"
LEVELS_SOCKET = "levels.sock"   # live state + voice level for the waveform overlay


def listen(path: str, backlog: int) -> socket.socket:
    """A Unix socket at `path` that only this user can connect to."""
    if os.path.exists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.chmod(path, 0o600)
    srv.listen(backlog)
    return srv


class LevelServer(threading.Thread):
    """Broadcasts one line per event to every connected client (the Omarchy
    shell's waveform overlay): "recording", "level 0.42" about every 20 ms
    while recording (0..1, voice loudness), "transcribing", "text <result>",
    "typing <chars>" (0 = instant paste), "nothing", "idle"."""

    def __init__(self, path: str) -> None:
        super().__init__(daemon=True)
        self.path = path
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()

    def run(self) -> None:
        srv = listen(self.path, 4)
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.settimeout(0.05)
            with self._lock:
                self._clients.append(conn)

    def send(self, line: str) -> None:
        data = (line.replace("\n", " ") + "\n").encode()
        with self._lock:
            for c in list(self._clients):
                try:
                    c.sendall(data)
                except OSError:
                    self._clients.remove(c)
                    c.close()


class ControlServer(threading.Thread):
    """Commands from iris-dictation (the CLI) and other programs. Each one
    becomes an event on the daemon's queue, as a tuple (kind, *args)."""

    def __init__(self, path: str, events: queue.Queue,
                 status: Callable[[], str]) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.events = events
        self.status = status
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        srv = listen(self.path, 8)
        srv.settimeout(1)
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with conn:
                try:
                    cmd = conn.recv(65536).decode().strip()
                except Exception:
                    continue
                reply = self.handle(cmd)
                try:
                    conn.sendall(reply.encode())
                except Exception:
                    pass
        srv.close()

    def ask(self, kind: str, *args) -> str:
        """Queue an event and wait for the transcript it produces."""
        box: queue.Queue = queue.Queue()
        self.events.put((kind, *args, box))
        try:
            return box.get(timeout=60)
        except queue.Empty:
            return ""

    def handle(self, cmd: str) -> str:
        verb, _, arg = cmd.partition(" ")
        if verb == "start":
            # "start <tag>": a recording another program shows itself (the
            # levels socket says "recording <tag>"; the waveform pill stays out).
            tag = arg.strip()
            if tag and not re.fullmatch(r"[a-z0-9-]{1,32}", tag):
                return "bad tag: lowercase letters, digits and - only"
            self.events.put(("down", tag) if tag else ("down",))
            return "ok"
        if verb == "stop":
            self.events.put(("up",))
            return "ok"
        if verb == "stop-return":
            # Stop and hand the transcript back on this socket instead of typing
            # it (omarchy-controller sends it to Iris). Empty reply = nothing heard.
            return self.ask("up")
        if verb == "toggle":
            self.events.put(("up",) if self.status() == "recording" else ("down",))
            return "ok"
        if verb == "status":
            return self.status()
        if verb == "transcribe":
            # Replies with the text instead of typing it, so tests and scripts
            # can check what was heard.
            return self.ask("file", arg.strip())
        if verb == "ping":
            return "pong"
        if verb == "quit":
            self.events.put(("quit",))
            return "ok"
        return f"unknown command: {verb}"
