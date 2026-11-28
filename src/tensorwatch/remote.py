"""Remote machines: another TensorWatch's boards and queue, in this dashboard.

Each ``[[remote]]`` in the registry gets one SSH connection, opened while a
TensorWatch window is open here and closed after the manager's ``idle_exit``
without one. Connecting takes three steps over a single SSH master connection:

1. ``ssh -M -N <dest>`` authenticates, with ``BatchMode`` so it never prompts.
   Everything else (user, key, port, jump hosts) comes from ``~/.ssh/config``.
2. ``tensorwatch remote hello`` on the remote starts its TensorWatch if needed
   and says which API version it speaks and where its dashboard listens.
3. The master forwards a Unix socket under ``$XDG_RUNTIME_DIR`` to that
   dashboard port, so links to several machines never compete for ports.

The link then follows the remote's state stream and merges it into this
manager's, and pings it so the remote stays up while it is being watched.
Browsers reach a remote board at ``<board>.<remote>.localhost:<port>``; the
local manager relays those requests through the socket, and the remote
manager relays them to its TensorBoard (see :mod:`tensorwatch.proxy`).
"""

from __future__ import annotations

import collections
import itertools
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import proxy
from .config import DEFAULT_REMOTE_COMMAND, RemoteSpec

#: Oldest remote API this manager can show (``supervisor.API_VERSION``).
MIN_API = 2
#: Marks requests from this manager to a remote; must match ``httpd.PEER_HEADER``.
PEER_HEADER = "X-TensorWatch-Peer"
#: Where ``tensorwatch install`` puts the command, for a login shell whose
#: PATH does not include it when SSH runs a command without a terminal.
FALLBACK_COMMAND = "~/.local/bin/tensorwatch"
#: Bounds on each connection step.
MASTER_TIMEOUT = 30.0
HELLO_TIMEOUT = 75.0
FORWARD_TIMEOUT = 10.0
#: How often a watched remote is told so. Its idle exit is two minutes by
#: default; the ping interval also bounds how quickly a dead link is noticed.
PING_INTERVAL = 30.0
#: The remote's stream sends a keep-alive every 20 s (``httpd.SSE_HEARTBEAT``);
#: silence for this long means the link is gone even if the socket looks open.
STREAM_SILENCE = 50.0
#: Retry delays after a failure that may clear up by itself (a network blip,
#: the remote restarting), and after one that needs the user (a key, an install).
RETRY_TRANSIENT = (1.0, 3.0, 10.0, 30.0, 60.0)
RETRY_PERSISTENT = 60.0
#: Clocks this far apart are corrected for in the remote's timestamps.
SKEW_TOLERANCE = 1.0
#: SSH options the link relies on, given on the command line so they apply
#: whatever ``~/.ssh/config`` says. Failing fast and noticing a dead network
#: are what make the dashboard's error states truthful.
SSH_OPTIONS = (
    "BatchMode=yes",
    "ConnectTimeout=10",
    "ServerAliveInterval=15",
    "ServerAliveCountMax=3",
)

#: Kinds of failure the dashboard explains differently.
UNREACHABLE = "unreachable"
AUTH = "auth"
HOST_KEY = "host_key"
MISSING = "missing"
TOO_OLD = "too_old"
FAILED = "failed"
PERSISTENT_KINDS = frozenset({AUTH, HOST_KEY, MISSING, TOO_OLD})

_UNREACHABLE_MARKERS = (
    "Could not resolve hostname", "Connection timed out", "Connection refused",
    "No route to host", "Network is unreachable", "Operation timed out",
    "Connection closed by", "Connection reset", "Timeout, server",
    "kex_exchange_identification", "Broken pipe", "not responding",
)
_AUTH_MARKERS = ("Permission denied", "Too many authentication failures", "no supported authentication")
_HOST_KEY_MARKERS = (
    "Host key verification failed", "REMOTE HOST IDENTIFICATION HAS CHANGED",
    "host key is known", "No matching host key",
)


class LinkError(Exception):
    """A link step failed; ``kind`` says how to explain it."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def runtime_dir() -> Path:
    """Private directory for the SSH control and forward sockets."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    root = (
        Path(base) / "tensorwatch"
        if base
        else Path(tempfile.gettempdir()) / f"tensorwatch-{os.getuid()}"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # ssh prints a banner-like "Warning: Permanently added ..." before the real error.
    meaningful = [line for line in lines if not line.startswith("Warning: Permanently added")]
    return (meaningful or lines or [""])[-1][:300]


def classify_ssh(code: int | None, stderr: str) -> LinkError:
    """Turn a failed ``ssh`` run into a :class:`LinkError`."""
    line = _last_line(stderr)
    if any(marker in stderr for marker in _HOST_KEY_MARKERS):
        return LinkError(HOST_KEY, line or "the host key could not be verified")
    if any(marker in stderr for marker in _AUTH_MARKERS):
        return LinkError(AUTH, line or "SSH authentication failed")
    if any(marker in stderr for marker in _UNREACHABLE_MARKERS):
        return LinkError(UNREACHABLE, line)
    return LinkError(FAILED, line or f"ssh exited with code {code}")


def classify_hello(code: int, stdout: str, stderr: str) -> dict[str, Any] | LinkError:
    """The remote's hello, or why there is none."""
    if code == 255:
        return classify_ssh(code, stderr)
    if code == 127 or "command not found" in stderr or "Unknown command" in stderr:
        return LinkError(MISSING, _last_line(stderr) or "tensorwatch is not installed there")
    if code == 2 and "invalid choice" in stderr:
        return LinkError(TOO_OLD, "its TensorWatch predates remotes; update it")
    hello = None
    for line in reversed(stdout.splitlines()):
        try:
            hello = json.loads(line)
        except ValueError:
            continue
        break
    if not isinstance(hello, dict) or hello.get("app") != "tensorwatch":
        detail = _last_line(stderr) or _last_line(stdout) or f"exit code {code}"
        return LinkError(FAILED, f"unexpected answer from tensorwatch remote hello: {detail}")
    api = hello.get("api")
    if not isinstance(api, int) or api < MIN_API:
        return LinkError(TOO_OLD, f"its TensorWatch speaks API {api}, this one needs {MIN_API}; update it")
    if not hello.get("running"):
        return LinkError(FAILED, hello.get("error") or _last_line(stderr) or "TensorWatch did not start there")
    if not isinstance(hello.get("port"), int):
        return LinkError(FAILED, "the remote did not say where its dashboard listens")
    return hello


class SSH:
    """Command lines for one remote, all sharing one master connection."""

    def __init__(self, destination: str, control: Path, binary: str = "ssh") -> None:
        self.destination = destination
        self.control = control
        self.binary = binary

    def _base(self, *extra: str) -> list[str]:
        argv = [self.binary]
        for option in (*SSH_OPTIONS, f"ControlPath={self.control}", *extra):
            argv += ["-o", option]
        return argv

    def master(self) -> list[str]:
        return [
            *self._base("ControlMaster=yes", "ControlPersist=no", "ExitOnForwardFailure=yes",
                        "StreamLocalBindUnlink=yes"),
            "-N", "-T", "--", self.destination,
        ]

    def run(self, command: str) -> list[str]:
        return [*self._base("ControlMaster=no"), "-T", "--", self.destination, command]

    def forward(self, path: Path, host: str, port: int) -> list[str]:
        return self._mux("forward", path, host, port)

    def cancel(self, path: Path, host: str, port: int) -> list[str]:
        return self._mux("cancel", path, host, port)

    def _mux(self, operation: str, path: Path, host: str, port: int) -> list[str]:
        target = f"[{host}]" if ":" in host else host
        return [*self._base("ControlMaster=no"), "-O", operation, "-L", f"{path}:{target}:{port}",
                "--", self.destination]


def request(
    upstream: proxy.Upstream, method: str, path: str, timeout: float = 10.0
) -> tuple[int, bytes]:
    """One request to a remote TensorWatch through its link."""
    conn, reused = upstream.acquire()
    try:
        conn.timeout = timeout
        if conn.sock is not None:
            conn.sock.settimeout(timeout)
        conn.request(method, path, headers={"Host": "localhost", PEER_HEADER: "1"})
        response = conn.getresponse()
        body = response.read()
    except Exception:
        conn.close()
        if reused:  # an idle connection the remote closed; try a fresh one
            return request(upstream, method, path, timeout)
        raise
    if response.will_close:
        conn.close()
    else:
        # The pool is shared with board relays, which need the long timeout.
        conn.timeout = proxy.READ_TIMEOUT
        if conn.sock is not None:
            conn.sock.settimeout(proxy.READ_TIMEOUT)
        upstream.release(conn)
    return response.status, body


#: Remote timestamps (epoch seconds) that a clock difference would skew.
_BOARD_TIMES = ("since", "next_attempt", "last_event")
_JOB_TIMES = ("since", "queued_at")


def _shift(payload: dict[str, Any], offset: float) -> None:
    for board in payload.get("boards") or ():
        for key in _BOARD_TIMES:
            if isinstance(board.get(key), (int, float)):
                board[key] += offset
    snapshot = payload.get("queue")
    if isinstance(snapshot, dict):
        if isinstance(snapshot.get("updated_at"), (int, float)):
            snapshot["updated_at"] += offset
        for job in (*(snapshot.get("running") or ()), *(snapshot.get("queued") or ())):
            for key in _JOB_TIMES:
                if isinstance(job.get(key), (int, float)):
                    job[key] += offset


class RemoteLink:
    """The connection to one remote, run by a thread of its own."""

    def __init__(
        self,
        spec: RemoteSpec,
        publish: Callable[[], None],
        present: Callable[[], bool],
        ssh: str = "ssh",
        tag: str = "",
    ) -> None:
        self.spec = spec
        #: Distinguishes this link's sockets from another one to the same remote
        #: (``remote check`` runs beside the manager's link).
        self._stem = spec.name + tag
        self._publish = publish
        self._present = present
        self._ssh_binary = ssh
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._retry = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"tensorwatch-remote-{spec.name}", daemon=True
        )
        self._master: subprocess.Popen[bytes] | None = None
        #: The remote's last state as it sent it, to tell real changes apart.
        self._received: dict[str, Any] | None = None
        #: Seconds to add to the remote's timestamps to read them on this clock.
        self._offset = 0.0
        #: The master's current forward, as (host, port).
        self._forward: tuple[str, int] | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=40)
        self._socket: socket.socket | None = None
        #: Set while connected; requests to the remote go through it.
        self.upstream: proxy.Upstream | None = None
        self.state = "idle"
        self.error: LinkError | None = None
        self.next_attempt: float | None = None
        self.since: float | None = None
        self.hello: dict[str, Any] = {}
        self.latency: float | None = None
        #: The remote's last state, kept while reconnecting.
        self.remote: dict[str, Any] | None = None
        self._failures = 0
        self._ping_interval = PING_INTERVAL

    # ------------------------------------------------------------ interface

    def start(self) -> None:
        self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        self.stop()
        if self._thread.is_alive():
            self._thread.join(timeout)
        self._teardown()

    def stop(self) -> None:
        """Begin shutting down without waiting: end the stream and the master,
        which also ends any SSH command the link thread is waiting on."""
        self._stopping.set()
        self._wake.set()
        self._close_stream()
        master = self._master
        if master is not None and master.poll() is None:
            master.terminate()

    def retry(self) -> None:
        """Try again now instead of after the backoff."""
        self._retry.set()
        self._wake.set()

    def nudge(self) -> None:
        """Re-check whether the link is wanted (a window just opened)."""
        self._wake.set()

    def unavailable(self) -> str:
        """Why requests to this remote cannot be served right now."""
        with self._lock:
            error, state = self.error, self.state
        title = self.spec.title
        if state == "connecting":
            return f"Connecting to {title}…"
        if error is not None:
            return f"Can't reach {title}: {error.message}"
        return f"{title} is not connected."

    def to_json(self) -> dict[str, Any]:
        with self._lock:
            remote = self.remote or {}
            error = self.error
            return {
                "name": self.spec.name,
                "label": self.spec.title,
                "ssh": self.spec.ssh,
                "enabled": self.spec.enabled,
                "state": self.state,
                "error": {"kind": error.kind, "message": error.message} if error else None,
                "next_attempt": self.next_attempt,
                "since": self.since,
                "hostname": self.hello.get("hostname"),
                "version": self.hello.get("version"),
                "latency_ms": round(self.latency * 1000, 1) if self.latency is not None else None,
                "boards": remote.get("boards") or [],
                "queue": remote.get("queue"),
                "server": remote.get("server"),
            }

    # --------------------------------------------------------------- thread

    def _set(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)
        self._publish()

    def _run(self) -> None:
        while not self._stopping.is_set():
            wanted = self.spec.enabled and self._present()
            if not wanted:
                if self.state != "idle" or self._master is not None:
                    self._teardown()
                    self._failures = 0
                    self._set(state="idle", error=None, next_attempt=None, remote=None, since=None)
                self._sleep(1.0)
                continue
            if self._retry.is_set():
                self._retry.clear()
                self._set(next_attempt=None)
            if self.next_attempt is not None and time.time() < self.next_attempt:
                self._sleep(min(1.0, self.next_attempt - time.time()))
                continue
            try:
                self._connect()
                self._follow()
            except LinkError as exc:
                self._fail(exc)
            except Exception as exc:  # never let the link thread die
                self._fail(LinkError(FAILED, f"{type(exc).__name__}: {exc}"))

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(max(0.05, seconds))
        self._wake.clear()

    def _fail(self, exc: LinkError) -> None:
        self._close_stream()
        if self.upstream is not None:
            self.upstream.close()
        self._failures += 1
        if exc.kind in PERSISTENT_KINDS:
            delay = RETRY_PERSISTENT
            self._teardown()
        else:
            delay = RETRY_TRANSIENT[min(self._failures, len(RETRY_TRANSIENT)) - 1]
            if self._master is not None and self._master.poll() is not None:
                self._teardown()
        self._set(state="error", error=exc, upstream=None, next_attempt=time.time() + delay)

    # ------------------------------------------------------------- connect

    def _paths(self) -> tuple[Path, Path]:
        root = runtime_dir()
        return root / f"{self._stem}.ssh", root / f"{self._stem}.sock"

    def _connect(self) -> None:
        # A retry asked for while connecting is this attempt; and whatever
        # served the last connection is no longer to be used.
        self._retry.clear()
        previous = self.upstream
        self._set(state="connecting", upstream=None)
        if previous is not None:
            previous.close()
        control, forwarded = self._paths()
        ssh = SSH(self.spec.ssh, control, self._ssh_binary)
        if self._master is None or self._master.poll() is not None:
            self._teardown()
            self._start_master(ssh, control)
        hello = self._hello(ssh)
        target = (hello.get("host") or "127.0.0.1", hello["port"])
        if self._forward is not None:
            # A master asked again for a forward it has answers "ok" without
            # binding anew, so the old one goes first, even if it looks the same.
            self._ssh(ssh.cancel(forwarded, *self._forward), FORWARD_TIMEOUT)
            self._forward = None
        forwarded.unlink(missing_ok=True)
        result = self._ssh(ssh.forward(forwarded, *target), FORWARD_TIMEOUT)
        if result.returncode != 0 or not _wait_for(forwarded, FORWARD_TIMEOUT, self._master):
            raise classify_ssh(result.returncode, result.stderr or self._stderr_text())
        self._forward = target
        upstream = proxy.Upstream(path=str(forwarded))
        started, sent = time.monotonic(), time.time()
        try:
            status, body = request(upstream, "GET", "/api/state")
        except OSError as exc:
            upstream.close()
            raise LinkError(FAILED, f"its dashboard did not answer: {exc}") from exc
        if status != 200:
            upstream.close()
            raise LinkError(FAILED, f"its dashboard answered {status}")
        payload = json.loads(body)
        if not isinstance(payload.get("api"), int) or payload["api"] < MIN_API:
            upstream.close()
            raise LinkError(TOO_OLD, "its TensorWatch is older than its hello said; restart it")
        latency = time.monotonic() - started
        idle_exit = (payload.get("server") or {}).get("idle_exit") or 120.0
        self._ping_interval = max(1.0, min(PING_INTERVAL, float(idle_exit) / 4))
        self._failures = 0
        # The remote said "now" about halfway through the round trip. Measured
        # once per connection: per frame, delivery delays would make it jitter.
        offset = sent + latency / 2 - float(payload.get("now") or sent + latency / 2)
        self._offset = offset if abs(offset) > SKEW_TOLERANCE else 0.0
        self._received = None
        with self._lock:
            self.hello = hello
            self.latency = latency
        self._absorb(payload, publish=False)
        self._set(state="connected", error=None, next_attempt=None, upstream=upstream,
                  since=time.time())

    def _ssh(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LinkError(UNREACHABLE, f"no answer within {timeout:.0f}s") from exc
        except FileNotFoundError as exc:
            raise LinkError(FAILED, f"{self._ssh_binary!r} is not installed here") from exc

    def _start_master(self, ssh: SSH, control: Path) -> None:
        control.unlink(missing_ok=True)
        self._stderr.clear()
        argv = ssh.master()
        if _SETPRIV:
            # Should this manager die without tearing down, the master goes too,
            # instead of holding a connection open with nobody to use it.
            argv = [_SETPRIV, "--pdeathsig", "TERM", "--", *argv]
        try:
            master = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise LinkError(FAILED, f"{self._ssh_binary!r} is not installed here") from exc
        self._master = master
        threading.Thread(
            target=self._drain_stderr, args=(master,), name=f"{self._thread.name}-stderr",
            daemon=True,
        ).start()
        if not _wait_for(control, MASTER_TIMEOUT, master, self._stopping):
            code = master.poll()
            if code is None and not self._stopping.is_set():
                self._teardown()
                raise LinkError(UNREACHABLE, f"no answer within {MASTER_TIMEOUT:.0f}s")
            try:
                master.wait(2.0)
            except subprocess.TimeoutExpired:
                pass
            time.sleep(0.05)  # let the stderr reader catch the last line
            error = classify_ssh(master.returncode, self._stderr_text())
            self._teardown()
            raise error

    def _drain_stderr(self, master: subprocess.Popen[bytes]) -> None:
        assert master.stderr is not None
        for raw in master.stderr:
            self._stderr.append(raw.decode("utf-8", "replace").rstrip())

    def _stderr_text(self) -> str:
        return "\n".join(self._stderr)

    def _hello(self, ssh: SSH) -> dict[str, Any]:
        commands = [self.spec.command]
        if self.spec.command == DEFAULT_REMOTE_COMMAND:
            commands.append(FALLBACK_COMMAND)
        outcome: dict[str, Any] | LinkError = LinkError(FAILED, "no hello")
        for command in commands:
            result = self._ssh(ssh.run(f"{command} remote hello"), HELLO_TIMEOUT)
            outcome = classify_hello(result.returncode, result.stdout, result.stderr)
            if not (isinstance(outcome, LinkError) and outcome.kind == MISSING):
                break
        if isinstance(outcome, LinkError):
            raise outcome
        return outcome

    # -------------------------------------------------------------- follow

    def _follow(self) -> None:
        """Read the remote's stream until it ends or the link is not wanted."""
        _, forwarded = self._paths()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket = sock
        try:
            sock.settimeout(proxy.CONNECT_TIMEOUT)
            sock.connect(str(forwarded))
            sock.sendall(
                b"GET /api/events HTTP/1.1\r\nHost: localhost\r\n"
                + f"{PEER_HEADER}: 1\r\n".encode()
                + b"Accept: text/event-stream\r\n\r\n"
            )
            sock.settimeout(1.0)
            buffer = b""
            head = True
            last_data = last_ping = time.monotonic()
            while not self._stopping.is_set():
                try:
                    chunk = sock.recv(256 * 1024)
                    if not chunk:
                        raise LinkError(FAILED, "its TensorWatch closed the connection")
                    buffer += chunk
                    last_data = time.monotonic()
                except socket.timeout:
                    chunk = b""
                except OSError as exc:
                    raise LinkError(UNREACHABLE, f"the connection dropped ({exc})") from exc
                if head and b"\r\n\r\n" in buffer:
                    status_line, _, buffer = buffer.partition(b"\r\n\r\n")
                    if b" 200 " not in status_line.split(b"\r\n", 1)[0] + b" ":
                        raise LinkError(FAILED, f"its stream answered {status_line[:60]!r}")
                    head = False
                if not head:
                    buffer = self._events(buffer)
                now = time.monotonic()
                if not (self.spec.enabled and self._present()):
                    return
                if self._retry.is_set():
                    self._retry.clear()
                    return  # reconnect: the user asked for a fresh start
                if self._master is not None and self._master.poll() is not None:
                    raise classify_ssh(self._master.returncode, self._stderr_text())
                if now - last_data > STREAM_SILENCE:
                    raise LinkError(UNREACHABLE, f"no word from it for {STREAM_SILENCE:.0f}s")
                if now - last_ping >= self._ping_interval:
                    last_ping = now
                    self._ping()
        finally:
            self._close_stream()

    def _events(self, buffer: bytes) -> bytes:
        """Absorb each complete event in ``buffer``; return the incomplete rest."""
        while b"\n\n" in buffer:
            event, _, buffer = buffer.partition(b"\n\n")
            data = [line[5:].lstrip() for line in event.split(b"\n") if line.startswith(b"data:")]
            if data:
                try:
                    payload = json.loads(b"\n".join(data))
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    self._absorb(payload)
        return buffer

    def _ping(self) -> None:
        """Tell the remote it is still being watched, and time the round trip."""
        upstream = self.upstream
        if upstream is None:
            return
        started = time.monotonic()
        try:
            status, _ = request(upstream, "GET", "/healthz", timeout=10.0)
        except OSError as exc:
            raise LinkError(UNREACHABLE, f"it stopped answering ({exc})") from exc
        if status == 200:
            with self._lock:
                self.latency = time.monotonic() - started

    def _absorb(self, payload: dict[str, Any], publish: bool = True) -> None:
        received = {key: payload.get(key) for key in ("boards", "queue", "server")}
        if received == self._received:
            # Nothing new: the remote republished for its own reasons, such as
            # its remotes changing. Passing it on would let two machines that
            # list each other echo frames back and forth without end.
            return
        self._received = received
        kept = json.loads(json.dumps(received))
        # Each board's address comes from the remote; only a DNS label may
        # become part of a host name here.
        kept["boards"] = [
            board for board in kept.get("boards") or ()
            if isinstance(board, dict) and isinstance(board.get("host"), str)
            and proxy.LABEL_RE.match(board["host"])
        ]
        if self._offset:
            _shift(kept, self._offset)
        with self._lock:
            self.remote = kept
        if publish:
            self._publish()

    # ------------------------------------------------------------ teardown

    def _close_stream(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _teardown(self) -> None:
        """Close everything this link holds open."""
        upstream, self.upstream = self.upstream, None
        if upstream is not None:
            upstream.close()
        master, self._master = self._master, None
        self._forward = None
        if master is not None and master.poll() is None:
            master.terminate()
            try:
                master.wait(3.0)
            except subprocess.TimeoutExpired:
                master.kill()
                master.wait(2.0)
        try:
            for path in self._paths():
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _wait_for(
    path: Path,
    timeout: float,
    process: subprocess.Popen[bytes] | None = None,
    stop: threading.Event | None = None,
) -> bool:
    """Wait for ``path`` to appear; False if ``process`` exits or time runs out first."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        if process is not None and process.poll() is not None:
            return False
        if stop is not None and stop.is_set():
            return False
        time.sleep(0.02)
    return path.exists()


_SETPRIV = shutil.which("setpriv")


def default_ssh() -> str:
    """The ssh client; ``TENSORWATCH_SSH`` overrides it (the tests use a fake)."""
    return os.environ.get("TENSORWATCH_SSH") or shutil.which("ssh") or "ssh"


class Federation:
    """Every remote link of this manager."""

    def __init__(
        self,
        specs: Iterable[RemoteSpec],
        publish: Callable[[], None],
        present: Callable[[], bool],
        ssh: str | None = None,
    ) -> None:
        self._publish = publish
        self._present = present
        self._ssh = ssh or default_ssh()
        self._links: dict[str, RemoteLink] = {}
        #: Links removed by a reload that are still shutting down.
        self._stopping: list[RemoteLink] = []
        self._lock = threading.Lock()
        self.update(specs)

    def link(self, name: str) -> RemoteLink | None:
        with self._lock:
            return self._links.get(name)

    @property
    def links(self) -> list[RemoteLink]:
        with self._lock:
            return list(self._links.values())

    def update(self, specs: Iterable[RemoteSpec]) -> None:
        """Follow a registry reload: start, stop or restart links as needed.

        Returns at once: a link can take seconds to stop (an SSH command in
        flight), and a reload must not wait on it. A replacement for a link
        being stopped starts once the old one is gone, since both use the
        same socket paths.
        """
        wanted = {spec.name: spec for spec in specs}
        stale: list[RemoteLink] = []
        fresh: list[RemoteLink] = []
        with self._lock:
            for name, link in list(self._links.items()):
                if wanted.get(name) != link.spec:
                    stale.append(self._links.pop(name))
            ordered: dict[str, RemoteLink] = {}
            for name, spec in wanted.items():
                link = self._links.get(name)
                if link is None:
                    link = RemoteLink(spec, self._publish, self._present, self._ssh)
                    fresh.append(link)
                ordered[name] = link
            self._links = ordered
            self._stopping.extend(stale)
        for link in stale:
            link.stop()
        replacing = {link.spec.name for link in stale}
        for link in fresh:
            if link.spec.name not in replacing:
                link.start()
        if stale:
            later = [link for link in fresh if link.spec.name in replacing]
            threading.Thread(
                target=self._replace, args=(stale, later), name="tensorwatch-remote-reload",
                daemon=True,
            ).start()
        if stale or fresh:
            self._publish()

    def _replace(self, stale: list[RemoteLink], fresh: list[RemoteLink]) -> None:
        for link in stale:
            link.shutdown()
        with self._lock:
            for link in stale:
                if link in self._stopping:
                    self._stopping.remove(link)
            current = [link for link in fresh if self._links.get(link.spec.name) is link]
        for link in current:
            link.start()

    def nudge(self) -> None:
        for link in self.links:
            link.nudge()

    def snapshot(self) -> list[dict[str, Any]]:
        return [link.to_json() for link in self.links]

    def shutdown(self) -> None:
        with self._lock:
            links = [*self._links.values(), *self._stopping]
            self._links = {}
        # All at once: each may be waiting on an SSH command.
        for link in links:
            link.stop()
        for link in links:
            link.shutdown()


def check(spec: RemoteSpec, ssh: str | None = None, timeout: float = 120.0) -> dict[str, Any]:
    """Connect once, as the manager would, and report what happened."""
    seen: dict[str, Any] = {}
    done = threading.Event()
    link: RemoteLink

    def publish() -> None:
        state = link.to_json()
        if state["state"] in ("connected", "error"):
            seen.update(state)
            done.set()

    link = RemoteLink(spec, publish, lambda: not done.is_set(), ssh or default_ssh(),
                      tag=f".check{os.getpid()}")
    link.start()
    done.wait(timeout)
    link.shutdown()
    return seen or {**link.to_json(), "state": "error",
                    "error": {"kind": UNREACHABLE, "message": f"no result within {timeout:.0f}s"}}


def summarize(remote: Mapping[str, Any]) -> str:
    """One line about a remote's state, for the CLI."""
    state = remote.get("state")
    if state == "idle":
        return "not connected (connects while a TensorWatch window is open)"
    if state == "connected":
        boards = remote.get("boards") or []
        running = sum(1 for board in boards if board.get("state") == "running")
        queue = remote.get("queue")
        mlq = "" if queue is None else (", mlq connected" if queue.get("connected") else ", mlq offline")
        version = remote.get("version") or "?"
        where = remote.get("hostname") or remote.get("ssh")
        latency = remote.get("latency_ms")
        rtt = f", {latency:.0f} ms" if isinstance(latency, (int, float)) else ""
        return (f"connected to TensorWatch {version} on {where}: {len(boards)} board(s), "
                f"{running} running{mlq}{rtt}")
    error = remote.get("error")
    if state == "error" and error:
        return f"{ERROR_WORDS.get(error['kind'], 'error')}: {error['message']}"
    return str(state or "idle")


#: How each failure kind reads, here and in the dashboard.
ERROR_WORDS = {
    UNREACHABLE: "unreachable",
    AUTH: "SSH authentication failed",
    HOST_KEY: "host key not trusted",
    MISSING: "TensorWatch is not installed there",
    TOO_OLD: "its TensorWatch is too old",
    FAILED: "failed",
}
