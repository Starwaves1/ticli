"""The background player process (ADR-0008): `python -m ticli.playerd`.

One `HeadlessTidalPlayer` runs headless as the core: audio, TIDAL session,
queue, saved state, downloads, caches and the command layer. Clients reach it
over the socket in `ticli.ipc`. One selectors loop wakes only on the socket,
on the core's self-pipe (background threads, the monitor's existing tick) and
on signals; no timers of its own (ADR-0003). It leaves when the last client has
gone, nothing is playing and no download runs.
"""

import argparse
import collections
import json
import logging
import os
import queue
import selectors
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Optional

from ticli import ipc
from ticli.commands import AGENT, COMMANDS, HUMAN
from ticli.utils.config import PROTECTED_KEYS, UNREADABLE, load_config

logger = logging.getLogger(__name__)

# Below this the TUI's own clock is as good as the backend's; resending would be noise.
CLOCK_TOLERANCE = 0.25
OUTBOX_MAX = 8 << 20
IDENTITY_KEYS = ("queue", "editable_playlists")


class _Client:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.inbuf = b""
        self.outbuf = bytearray()
        self.subscribed = False
        self.writing = False
        self.jobs: collections.deque = collections.deque()
        self.working = False


class PlayerServer:
    def __init__(self, core, path: Optional[Path] = None):
        self.core = core
        self.path = Path(path or ipc.socket_path())
        self.sel = selectors.DefaultSelector()
        self.clients: dict = {}
        self.had_client = False
        self.listener: Optional[socket.socket] = None
        self.sent: dict = {}
        self._clock = None
        self._identities: dict = {}
        self._posted = collections.deque()
        self._agent_jobs: "queue.Queue" = queue.Queue()
        self._agent_worker: Optional[threading.Thread] = None
        self._jobs_lock = threading.Lock()
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        core._wake_r, core._wake_w = self.wake_r, self.wake_w
        core._tick_hook = self._tick
        core._list_hook = self._list_changed
        self._default = ipc.encoder(remember=lambda kind, obj: core._remember(kind, [obj]))

    # ── lifecycle ──

    def listen(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Safe: only the holder of the instance lock gets here, so a file left is a dead player's.
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            sock.bind(str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        sock.listen(16)
        sock.setblocking(False)
        self.listener = sock
        self.sel.register(sock, selectors.EVENT_READ, "accept")
        self.sel.register(self.wake_r, selectors.EVENT_READ, "wake")

    def serve(self) -> None:
        while self.core.running and not self.should_exit():
            self.step()

    def step(self, timeout: Optional[float] = None) -> None:
        for key, mask in self.sel.select(timeout):
            if key.data == "accept":
                self._accept()
            elif key.data == "wake":
                try:
                    os.read(self.wake_r, 4096)
                except OSError:
                    pass
            else:
                client = key.data
                if mask & selectors.EVENT_READ:
                    self._read(client)
                if mask & selectors.EVENT_WRITE and client.sock.fileno() in self.clients:
                    self._flush(client)
        self._deliver_posted()
        self.broadcast()

    def should_exit(self) -> bool:
        core = self.core
        busy = ((core._download_job or {}).get("state") == "running"
                or core._download_run is not None
                or (core._refetch_job or {}).get("state") == "running")
        return self.had_client and not self.clients and not core._playing and not busy

    def wake(self) -> None:
        try:
            os.write(self.wake_w, b"\x01")
        except OSError:
            pass

    def _tick(self) -> None:
        if not self.clients or any(c.subscribed for c in self.clients.values()):
            self.wake()

    def close(self) -> None:
        for client in list(self.clients.values()):
            self._drop(client)
        if self.listener is not None:
            try:
                self.sel.unregister(self.listener)
            except (KeyError, ValueError):
                pass
            self.listener.close()
            self.listener = None
            try:
                self.path.unlink()
            except OSError:
                pass
        self._agent_jobs.put(None)
        for fd in (self.wake_r, self.wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
        self.core._wake_r = self.core._wake_w = None
        self.sel.close()

    # ── connections ──

    def _accept(self) -> None:
        try:
            sock, _ = self.listener.accept()
        except (BlockingIOError, OSError):
            return
        sock.setblocking(False)
        client = _Client(sock)
        self.clients[sock.fileno()] = client
        self.had_client = True
        self.sel.register(sock, selectors.EVENT_READ, client)

    def _drop(self, client: _Client) -> None:
        fd = client.sock.fileno()
        if self.clients.pop(fd, None) is None:
            return
        try:
            self.sel.unregister(client.sock)
        except (KeyError, ValueError):
            pass
        client.sock.close()

    def _read(self, client: _Client) -> None:
        try:
            data = client.sock.recv(1 << 16)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._drop(client)
            return
        client.inbuf += data
        *lines, client.inbuf = client.inbuf.split(b"\n")
        for line in lines:
            if not line.strip():
                continue
            try:
                message = ipc.loads(line)
            except ValueError:
                self._reply(client, None, {"ok": False, "code": "bad_request", "reason": "Not JSON."})
                continue
            if isinstance(message, dict):
                self._handle(client, message)

    def _send(self, client: _Client, data: bytes) -> None:
        if client.sock.fileno() not in self.clients:
            return
        client.outbuf += data
        if len(client.outbuf) > OUTBOX_MAX:
            self._drop(client)
            return
        self._flush(client)

    def _flush(self, client: _Client) -> None:
        try:
            sent = client.sock.send(client.outbuf)
            del client.outbuf[:sent]
        except BlockingIOError:
            pass
        except OSError:
            self._drop(client)
            return
        want = bool(client.outbuf)
        if want != client.writing:
            client.writing = want
            events = selectors.EVENT_READ | (selectors.EVENT_WRITE if want else 0)
            self.sel.modify(client.sock, events, client)

    def _encode(self, message: dict) -> bytes:
        return ipc.dumps(message, default=self._default)

    def _reply(self, client: _Client, rid, response: dict) -> None:
        self._send(client, self._encode({"id": rid, **response}))

    # ── requests ──

    def _handle(self, client: _Client, message: dict) -> None:
        rid, cmd = message.get("id"), message.get("cmd")
        args = message.get("args") or {}
        if not isinstance(cmd, str) or not isinstance(args, dict):
            self._reply(client, rid, {"ok": False, "code": "bad_request",
                                      "reason": "A request is {id, cmd, args, caller}."})
            return
        if cmd == "subscribe":
            self.broadcast(force=True)
            client.subscribed = True
            self._send(client, self._state_message(self.sent, full=True))
            self._reply(client, rid, {"ok": True, "result": {"subscribed": True}})
            return
        if cmd == "reload_switches":
            fresh = load_config()
            for key in (*PROTECTED_KEYS, UNREADABLE):
                self.core.config[key] = fresh.get(key)
            self.broadcast()
            self._reply(client, rid, {"ok": True, "result": {}})
            return
        caller = HUMAN if message.get("caller") in ("tui", "human") else AGENT
        key = message.get("key")
        spec = COMMANDS.get(cmd)
        if caller == AGENT:
            # One at a time, in order, off the loop: a wrong key costs a second (ADR-0007).
            self._start_agent_worker()
            self._agent_jobs.put((client, rid, cmd, args, key))
        elif spec is not None and spec.read and spec.tidal:
            threading.Thread(target=self._run_posted, args=(client, rid, cmd, args, caller, key),
                             daemon=True).start()
        elif not self._queue_job(client, (rid, cmd, args, caller, key),
                                 spec is not None and spec.tidal):
            self._deliver_posted()
            response = self._execute(cmd, args, caller, key)
            self.broadcast()
            self._reply(client, rid, response)

    def _queue_job(self, client: _Client, job: tuple, tidal: bool) -> bool:
        """A TIDAL call must not freeze the loop for every client: it runs on this
        client's worker, and the client's later requests queue behind it in order."""
        with self._jobs_lock:
            if not (tidal or client.working):
                return False
            client.jobs.append(job)
            if client.working:
                return True
            client.working = True
        threading.Thread(target=self._work, args=(client,), daemon=True).start()
        return True

    def _work(self, client: _Client) -> None:
        while True:
            with self._jobs_lock:
                if not client.jobs:
                    client.working = False
                    return
                rid, cmd, args, caller, key = client.jobs.popleft()
            self._run_posted(client, rid, cmd, args, caller, key)

    def _execute(self, cmd, args, caller, key) -> dict:
        try:
            return self.core.commands.execute(cmd, args, caller=caller, key=key)
        except Exception as e:
            logger.warning("Command %s failed: %r", cmd, e)
            return {"ok": False, "code": "failed", "reason": str(e) or type(e).__name__}

    def _run_posted(self, client, rid, cmd, args, caller, key) -> None:
        response = self._execute(cmd, args, caller, key)
        self._posted.append((client, {"id": rid, **response}))
        self.wake()

    def _start_agent_worker(self) -> None:
        if self._agent_worker is not None:
            return

        def _work():
            while True:
                job = self._agent_jobs.get()
                if job is None:
                    return
                client, rid, cmd, args, key = job
                self._run_posted(client, rid, cmd, args, AGENT, key)

        self._agent_worker = threading.Thread(target=_work, daemon=True)
        self._agent_worker.start()

    def _deliver_posted(self) -> None:
        if not self._posted:
            return
        # State first, so a client acting on the answer already holds what it changed.
        self.broadcast()
        while self._posted:
            client, message = self._posted.popleft()
            if client is None:
                for sub in self._subscribers():
                    self._send(sub, self._encode(message))
            else:
                self._send(client, self._encode(message))

    def _list_changed(self, source, tracks) -> None:
        self._posted.append((None, {"event": "list", "source": list(source), "tracks": list(tracks)}))
        self.wake()

    # ── pushed state ──

    def _subscribers(self) -> list:
        return [c for c in self.clients.values() if c.subscribed]

    def broadcast(self, force: bool = False) -> None:
        subscribers = self._subscribers()
        if not subscribers and not force:
            return
        delta = self.diff()
        if delta and subscribers:
            message = self._state_message(delta, full=False)
            for client in subscribers:
                self._send(client, message)

    def diff(self) -> dict:
        changed = {}
        for key, value in self.core.snapshot().items():
            if key == "clock":
                if not self._clock_moved(value) and key in self.sent:
                    continue
                self._clock = value
            elif key in IDENTITY_KEYS:
                identity = tuple(map(id, value))
                if self._identities.get(key) == identity and key in self.sent:
                    continue
                self._identities[key] = identity
            wire = json.dumps(value, default=self._default, separators=(",", ":"))
            if self.sent.get(key) != wire:
                self.sent[key] = wire
                changed[key] = wire
        return changed

    def _clock_moved(self, clock) -> bool:
        if self._clock is None:
            return True
        playing, offset, start = clock
        was_playing, was_offset, was_start = self._clock
        if playing != was_playing or (start is None) != (was_start is None):
            return True
        if start is None:
            return abs(offset - was_offset) > 0.05
        return abs((start - offset) - (was_start - was_offset)) > CLOCK_TOLERANCE

    @staticmethod
    def _state_message(wires: dict, full: bool) -> bytes:
        body = ",".join(f"{json.dumps(k)}:{v}" for k, v in wires.items())
        return (f'{{"event":"state","full":{"true" if full else "false"},"state":{{{body}}}}}\n'
                .encode())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ticli.playerd")
    parser.add_argument("--ready-fd", type=int)
    parser.add_argument("--quality")
    parser.add_argument("--login-flow")
    opts = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    def ready(line: str) -> None:
        if opts.ready_fd is None:
            print(line, file=sys.stderr)
            return
        try:
            os.write(opts.ready_fd, (line + "\n").encode())
            os.close(opts.ready_fd)
        except OSError:
            pass

    from rich.console import Console
    from ticli.player import HeadlessTidalPlayer

    core = HeadlessTidalPlayer(quality=opts.quality, login_flow=opts.login_flow)
    core.console = Console(file=open(os.devnull, "w"))
    if not core.start(interactive=False):
        ready(core.start_failure or "error: the player could not start")
        return 1
    server = PlayerServer(core)
    try:
        server.listen()
    except OSError as e:
        ready(f"error: could not open {server.path}: {e}")
        core.running = False
        core._shutdown()
        return 1

    def _on_signal(signum, frame):
        core.running = False
        server.wake()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _on_signal)
    os.set_blocking(server.wake_w, False)
    # A signal can land on a worker thread, which never interrupts the loop's select.
    signal.set_wakeup_fd(server.wake_w)

    ready("ready")
    try:
        server.serve()
    finally:
        core.running = False
        core._shutdown()
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
