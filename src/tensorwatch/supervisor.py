"""Process supervision for registered TensorBoard boards.

One background thread owns every child process.  All mutation goes through a
queue so the HTTP handlers never touch process state directly, and every tick
publishes a status snapshot to subscribers (the dashboard's SSE stream), which
keeps the browser from polling.

Cold-start behaviour is deliberate: TensorBoard walks its whole logdir on start,
so spawning every board at once turns login into an I/O storm.  Starts are
staggered and the number of simultaneously warming boards is capped.
"""

from __future__ import annotations

import errno
import json
import os
import queue
import resource
import signal
import socket
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

from .config import BoardSpec, Config, log_dir
from . import activity, procstats

#: Log files are truncated-by-rotation at this size; TensorBoard is chatty on
#: reload errors and these logs live forever under a systemd unit.
LOG_ROTATE_BYTES = 8 * 1024 * 1024
#: Consecutive failed connect() probes tolerated on a live process before restart.
UNHEALTHY_STRIKES = 3
#: Bytes of a rotated log kept in ``<board>.log.1``.
LOG_KEEP_BYTES = 512 * 1024
#: Resource sampling is cheaper than a tick but not free; sample at this period.
STATS_INTERVAL = 5.0
MAX_BACKOFF = 60.0
#: A board that stayed up this long is considered healthy again (backoff reset).
STABLE_AFTER = 300.0
PROBE_TIMEOUT = 0.25
#: Descriptors a board may need: one per event file, plus headroom.
WANTED_NOFILE = 65536
TERM_GRACE = 10.0

Action = Literal["start", "stop", "restart", "demand"]


class State(str, Enum):
    disabled = "disabled"
    stopped = "stopped"
    starting = "starting"
    running = "running"
    backoff = "backoff"
    failed = "failed"


@dataclass(frozen=True, slots=True)
class BoardStatus:
    """Serializable snapshot of one board."""

    name: str
    state: str
    port: int
    url: str
    target: str
    description: str
    autostart: str
    enabled: bool
    pid: int | None
    since: float | None
    restarts: int
    last_exit: int | None
    message: str
    log_path: str
    rss_bytes: int | None
    cpu_percent: float | None
    idle_timeout: float
    demanded_ago: float | None
    #: Newest event-file write under the logdir, and whether that counts as live.
    last_event: float | None
    writing: bool

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _Runtime:
    """Mutable per-board bookkeeping owned by the supervisor thread."""

    spec: BoardSpec
    state: State = State.stopped
    proc: subprocess.Popen[bytes] | None = None
    log: Any = None
    since: float | None = None
    running_since: float | None = None
    restarts: int = 0
    last_exit: int | None = None
    message: str = ""
    next_attempt: float = 0.0
    strikes: int = 0
    #: ``None`` follows the autostart policy; ``True``/``False`` are explicit
    #: user overrides from the dashboard or CLI.
    override: bool | None = None
    last_demand: float = 0.0
    rss_bytes: int | None = None
    cpu_percent: float | None = None
    _cpu_prev: tuple[float, float] | None = None

    @property
    def pgid(self) -> int | None:
        return self.proc.pid if self.proc and self.proc.poll() is None else None


def raise_file_limit(target: int = WANTED_NOFILE) -> tuple[int, int]:
    """Lift this process's file-descriptor soft limit; children inherit it.

    TensorBoard's data server keeps one descriptor per event file, so a logdir
    with a few thousand runs exhausts the usual soft limit of 1024 and the board
    silently serves nothing ("No dashboards are active for the current data set").
    Returns the (soft, hard) limit in force afterwards.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = min(target, hard) if hard != resource.RLIM_INFINITY else target
    if soft != resource.RLIM_INFINITY and soft < wanted:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
        except (ValueError, OSError):
            return soft, hard
        return wanted, hard
    return soft, hard


def probe(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """True when something accepts TCP on the board's port."""
    target = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        with socket.create_connection((target, port), timeout):
            return True
    except OSError:
        return False


class Supervisor:
    """Owns the child processes for every enabled board."""

    def __init__(
        self,
        config: Config,
        queue_source: Callable[[], Any] | None = None,
        activity_source: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._config = config
        #: Optional callable returning per-board logdir activity; this is what makes
        #: a row say "data is arriving", independent of who launched the run.
        self._activity_source = activity_source
        #: Optional callable returning the current mlq queue snapshot; it rides
        #: along in the same state payload so the dashboard needs one stream.
        self._queue_source = queue_source
        self._boards: dict[str, _Runtime] = {}
        self._order: list[str] = []
        self._requests: queue.Queue[tuple[Action, str]] = queue.Queue()
        self._subscribers: set[queue.Queue[str]] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="tensorwatch-supervisor", daemon=True
        )
        self._last_spawn = 0.0
        self._last_stats = 0.0
        self._last_publish = 0.0
        self._pending_config: Config | None = None
        self._snapshot: tuple[BoardStatus, ...] = ()
        self._activity: Mapping[str, Any] = {}
        self._apply_config(config)

    def set_queue_source(self, source: Callable[[], Any] | None) -> None:
        self._queue_source = source

    def set_activity_source(self, source: Callable[[], Mapping[str, Any]] | None) -> None:
        self._activity_source = source

    @property
    def watchers(self) -> int:
        """Number of connected dashboards (SSE subscribers)."""
        with self._lock:
            return len(self._subscribers)

    def publish(self) -> None:
        """Push a state frame without touching child state.

        Called from the queue poller thread, so it only re-encodes the snapshot
        the supervisor thread already built.
        """
        with self._lock:
            payload = self._encode(self._snapshot)
            subscribers = list(self._subscribers)
            self._last_publish = time.time()
        self._broadcast(payload, subscribers)

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._thread.start()

    def shutdown(self, timeout: float = TERM_GRACE + 5.0) -> None:
        self._stopping.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout)
        for runtime in self._boards.values():
            self._terminate(runtime, "manager shutting down")

    # ------------------------------------------------------------------ queries

    @property
    def config(self) -> Config:
        with self._lock:
            return self._config

    def snapshot(self) -> tuple[BoardStatus, ...]:
        with self._lock:
            return self._snapshot

    def log_tail(self, name: str, lines: int = 200) -> str:
        spec = self.config.board(name)
        if spec is None:
            raise KeyError(name)
        path = spec.log_path
        if not path.exists():
            return ""
        # Read only the tail: these files can be megabytes.
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            block = min(size, max(4096, lines * 400))
            handle.seek(size - block)
            data = handle.read()
        text = data.decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])

    # ----------------------------------------------------------------- commands

    def request(self, action: Action, name: str) -> None:
        if name not in self._boards:
            raise KeyError(name)
        self._requests.put((action, name))
        self._wake.set()

    def reload(self, config: Config) -> None:
        """Queue a registry reload; applied by the supervisor thread."""
        with self._lock:
            self._pending_config = config
        self._wake.set()
        if not self._thread.is_alive():
            self._absorb_pending()

    # ------------------------------------------------------------------- events

    def subscribe(self) -> queue.Queue[str]:
        channel: queue.Queue[str] = queue.Queue(maxsize=32)
        with self._lock:
            self._subscribers.add(channel)
            # Enqueue the first frame while holding the lock: a publish that
            # starts right after the add must not overtake this snapshot.
            channel.put_nowait(self._encode(self._snapshot))
        return channel

    def unsubscribe(self, channel: queue.Queue[str]) -> None:
        with self._lock:
            self._subscribers.discard(channel)

    # -------------------------------------------------------------- internals

    def _apply_config(self, config: Config) -> None:
        """Reconcile runtimes with the (possibly reloaded) registry."""
        specs = {spec.name: spec for spec in config.boards}
        for name in list(self._boards):
            if name not in specs:
                self._terminate(self._boards.pop(name), "board removed from registry")
        for name, spec in specs.items():
            runtime = self._boards.get(name)
            if runtime is None:
                self._boards[name] = _Runtime(spec=spec)
                continue
            if runtime.spec != spec:
                restart_needed = (
                    runtime.spec.argv() != spec.argv()
                    or runtime.spec.cwd != spec.cwd
                    or dict(runtime.spec.env) != dict(spec.env)
                )
                runtime.spec = spec
                if restart_needed and runtime.proc is not None:
                    self._terminate(runtime, "restarting: configuration changed")
                    runtime.state = State.stopped
        self._order = [spec.name for spec in config.boards]
        self._refresh_snapshot(force=True)

    def _absorb_pending(self) -> None:
        with self._lock:
            pending, self._pending_config = self._pending_config, None
            if pending is not None:
                self._config = pending
        if pending is not None:
            self._apply_config(pending)

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self._absorb_pending()
                self._drain_requests()
                self._tick()
            except Exception as exc:  # never let the supervisor thread die
                self._note_error(exc)
            self._wake.wait(self.config.server.poll_interval)
            self._wake.clear()
        for runtime in self._boards.values():
            self._terminate(runtime, "manager shutting down")
        self._refresh_snapshot(force=True)

    def _note_error(self, exc: Exception) -> None:
        path = log_dir() / "supervisor.log"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%F %T')} supervisor error: {exc!r}\n")

    def _drain_requests(self) -> None:
        while True:
            try:
                action, name = self._requests.get_nowait()
            except queue.Empty:
                return
            runtime = self._boards.get(name)
            if runtime is None:
                continue
            now = time.time()
            if action == "demand":
                runtime.last_demand = now
                if runtime.override is False:
                    runtime.override = None
            elif action == "start":
                runtime.override = True
                runtime.next_attempt = 0.0
                runtime.restarts = 0
                runtime.last_demand = now
            elif action == "stop":
                runtime.override = False
                runtime.last_demand = 0.0
                self._terminate(runtime, "stopped on request")
                runtime.state = State.stopped
            elif action == "restart":
                # `last_demand = now` below is enough to bring an on_demand board
                # back, so only a manual board needs to be pinned running.
                if runtime.override is None and runtime.spec.autostart == "manual":
                    runtime.override = True
                runtime.next_attempt = 0.0
                runtime.restarts = 0
                runtime.last_demand = now
                self._terminate(runtime, "restarting on request")
                runtime.state = State.stopped

    def _desired(self, runtime: _Runtime, now: float) -> bool:
        spec = runtime.spec
        if not spec.enabled:
            return False
        if runtime.override is not None:
            return runtime.override
        if spec.autostart == "always":
            return True
        if spec.autostart == "on_demand":
            if runtime.state is State.starting:
                # A board that is still scanning its logdir must not be killed
                # because the demand aged out while it warmed up.
                return True
            return bool(runtime.last_demand) and (now - runtime.last_demand) < spec.idle_timeout
        return False

    def _tick(self) -> None:
        now = time.time()
        server = self.config.server
        warming = sum(1 for r in self._boards.values() if r.state is State.starting)

        for name in self._order:
            runtime = self._boards.get(name)
            if runtime is None:
                continue
            desired = self._desired(runtime, now)
            alive = runtime.proc is not None and runtime.proc.poll() is None

            if not desired:
                if runtime.proc is not None:
                    reason = (
                        "stopped: idle" if runtime.spec.autostart == "on_demand" else "stopped"
                    )
                    self._terminate(runtime, reason)
                runtime.state = State.disabled if not runtime.spec.enabled else State.stopped
                continue

            if alive:
                self._check_health(runtime, now)
                continue

            if runtime.proc is not None:  # exited on its own
                self._handle_exit(runtime, now)
                continue

            if now < runtime.next_attempt:
                runtime.state = State.backoff if runtime.restarts else runtime.state
                continue
            if warming >= server.max_warming:
                continue
            if now - self._last_spawn < server.start_stagger:
                continue
            if self._spawn(runtime, now):
                warming += 1

        self._sample_resources(now)
        self._refresh_snapshot()

    def _check_health(self, runtime: _Runtime, now: float) -> None:
        spec = runtime.spec
        listening = probe(spec.host, spec.port)
        if runtime.state is State.starting:
            if listening:
                runtime.state = State.running
                runtime.running_since = now
                runtime.strikes = 0
                runtime.message = "serving"
            elif runtime.since is not None and now - runtime.since > spec.start_timeout:
                self._terminate(runtime, f"start timed out after {spec.start_timeout:.0f}s")
                runtime.state = State.failed
                runtime.restarts += 1
                runtime.next_attempt = now + self._backoff(runtime.restarts)
            return

        if listening:
            runtime.strikes = 0
            runtime.state = State.running
            if runtime.running_since and now - runtime.running_since > STABLE_AFTER:
                runtime.restarts = 0
            return

        runtime.strikes += 1
        if runtime.strikes >= UNHEALTHY_STRIKES:
            self._terminate(runtime, "restarting: port stopped accepting connections")
            runtime.state = State.stopped
            runtime.strikes = 0
            # Same accounting as a crash: a board that keeps wedging must back off
            # instead of cold-restarting (and rescanning its logdir) every tick.
            runtime.restarts += 1
            runtime.next_attempt = now + self._backoff(runtime.restarts)

    def _handle_exit(self, runtime: _Runtime, now: float) -> None:
        assert runtime.proc is not None
        code = runtime.proc.returncode
        runtime.last_exit = code
        self._close_log(runtime)
        runtime.proc = None
        runtime.restarts += 1
        delay = self._backoff(runtime.restarts)
        runtime.next_attempt = now + delay
        runtime.state = State.backoff
        tail = self._first_error_line(runtime.spec)
        runtime.message = (
            f"exited with code {code}; retrying in {delay:.0f}s" + (f" - {tail}" if tail else "")
        )

    def _first_error_line(self, spec: BoardSpec) -> str:
        try:
            lines = [line for line in self.log_tail(spec.name, 40).splitlines() if line.strip()]
        except (KeyError, OSError):
            return ""
        for line in reversed(lines):
            if not line.startswith("--- tensorwatch"):
                return line[:200]
        return ""

    @staticmethod
    def _backoff(restarts: int) -> float:
        return min(MAX_BACKOFF, 2.0 ** min(restarts, 6))

    def _spawn(self, runtime: _Runtime, now: float) -> bool:
        spec = runtime.spec
        if spec.logdir is not None and not spec.logdir.exists():
            runtime.state = State.failed
            runtime.message = f"logdir does not exist: {spec.logdir}"
            runtime.next_attempt = now + 30.0
            return False
        if probe(spec.host, spec.port):
            runtime.state = State.failed
            runtime.message = f"port {spec.port} is already in use by another process"
            runtime.next_attempt = now + 30.0
            return False

        log = self._open_log(spec)
        argv = spec.argv()
        log.write(
            f"\n--- tensorwatch {time.strftime('%F %T')} start: {' '.join(argv)}\n".encode()
        )
        log.flush()
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=str(spec.cwd) if spec.cwd else None,
                env=spec.process_env(),
                start_new_session=True,
            )
        except OSError as exc:
            log.write(f"--- tensorwatch spawn failed: {exc}\n".encode())
            log.close()
            runtime.state = State.failed
            runtime.message = f"cannot start {spec.command[0]!r}: {exc.strerror or exc}"
            runtime.next_attempt = now + 30.0
            return False

        runtime.proc = proc
        runtime.log = log
        runtime.state = State.starting
        runtime.since = now
        runtime.running_since = None
        runtime.message = "starting: scanning logdir"
        runtime._cpu_prev = None
        self._last_spawn = now
        return True

    def _open_log(self, spec: BoardSpec):
        path = spec.log_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            path.replace(path.with_suffix(".log.1"))
        # These logs carry the full child command line and every logdir path, so
        # tighten both the file and a directory created by an older version.
        os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "wb", buffering=0)

    def _rotate_live_logs(self) -> None:
        """Trim the log of a long-running board without disturbing its writer.

        Boards under systemd run for weeks, so rotation cannot only happen at
        spawn.  The handle is ``O_APPEND`` in both the manager and the child, so
        keeping the tail and truncating in place is safe for both writers.
        """
        for runtime in self._boards.values():
            if runtime.log is None:
                continue
            path = runtime.spec.log_path
            try:
                if path.stat().st_size <= LOG_ROTATE_BYTES:
                    continue
                with path.open("rb") as handle:
                    handle.seek(-LOG_KEEP_BYTES, os.SEEK_END)
                    tail = handle.read()
                previous = path.with_suffix(".log.1")
                fd = os.open(previous, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as archive:
                    archive.write(tail)
                os.truncate(runtime.log.fileno(), 0)
                runtime.log.write(
                    f"--- tensorwatch {time.strftime('%F %T')} rotated; tail kept in {previous}\n".encode()
                )
            except OSError:
                continue

    def _close_log(self, runtime: _Runtime) -> None:
        if runtime.log is not None:
            try:
                runtime.log.close()
            except OSError:
                pass
            runtime.log = None

    def _terminate(self, runtime: _Runtime, reason: str) -> None:
        proc = runtime.proc
        runtime.message = reason
        if proc is None:
            self._close_log(runtime)
            return
        if proc.poll() is None:
            self._signal_group(proc, signal.SIGTERM)
            try:
                proc.wait(TERM_GRACE)
            except subprocess.TimeoutExpired:
                self._signal_group(proc, signal.SIGKILL)
                try:
                    proc.wait(5.0)
                except subprocess.TimeoutExpired:
                    pass
        runtime.last_exit = proc.returncode
        runtime.proc = None
        runtime.since = None
        runtime.running_since = None
        runtime.rss_bytes = None
        runtime.cpu_percent = None
        runtime._cpu_prev = None
        self._close_log(runtime)

    @staticmethod
    def _signal_group(proc: subprocess.Popen[bytes], sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except OSError as exc:
            if exc.errno not in (errno.ESRCH, errno.EPERM):
                raise
            try:
                proc.send_signal(sig)
            except OSError:
                pass

    def _sample_resources(self, now: float) -> None:
        if now - self._last_stats < STATS_INTERVAL:
            return
        self._last_stats = now
        self._rotate_live_logs()
        groups = {r.pgid: r for r in self._boards.values() if r.pgid}
        samples = procstats.sample(groups.keys())
        for pgid, runtime in groups.items():
            entry = samples.get(pgid)
            if entry is None:
                runtime.rss_bytes = None
                runtime.cpu_percent = None
                continue
            runtime.rss_bytes = entry.rss_bytes
            previous = runtime._cpu_prev
            runtime._cpu_prev = (now, entry.cpu_seconds)
            if previous is not None and now > previous[0]:
                delta = (entry.cpu_seconds - previous[1]) / (now - previous[0])
                runtime.cpu_percent = max(0.0, round(delta * 100.0, 1))

    # ---------------------------------------------------------------- snapshots

    def _status(self, runtime: _Runtime, now: float) -> BoardStatus:
        spec = runtime.spec
        sample = self._activity.get(spec.name)
        return BoardStatus(
            name=spec.name,
            state=runtime.state.value,
            port=spec.port,
            url=spec.url,
            target=spec.target,
            description=spec.description,
            autostart=spec.autostart,
            enabled=spec.enabled,
            pid=runtime.pgid,
            since=runtime.since,
            restarts=runtime.restarts,
            last_exit=runtime.last_exit,
            message=runtime.message,
            log_path=str(spec.log_path),
            rss_bytes=runtime.rss_bytes,
            cpu_percent=runtime.cpu_percent,
            idle_timeout=spec.idle_timeout,
            demanded_ago=(now - runtime.last_demand) if runtime.last_demand else None,
            last_event=sample.mtime if sample is not None else None,
            writing=activity.writing(sample, now),
        )

    def _refresh_snapshot(self, force: bool = False) -> None:
        """Rebuild the board snapshot (supervisor thread only) and publish it."""
        now = time.time()
        self._activity = self._activity_source() if self._activity_source is not None else {}
        fresh = tuple(
            self._status(self._boards[name], now) for name in self._order if name in self._boards
        )
        with self._lock:
            changed = (
                force
                or self._significant(self._snapshot) != self._significant(fresh)
                # resource counters change every tick; refresh viewers on a slower
                # cadence instead of pushing a frame for every byte of RSS.
                or now - self._last_publish >= STATS_INTERVAL
            )
            self._snapshot = fresh
            if not changed:
                return
            self._last_publish = now
            payload = self._encode(fresh)
            subscribers = list(self._subscribers)
        self._broadcast(payload, subscribers)

    @staticmethod
    def _broadcast(payload: str, subscribers: Iterable[queue.Queue[str]]) -> None:
        for channel in subscribers:
            try:
                channel.put_nowait(payload)
            except queue.Full:
                pass

    @staticmethod
    def _significant(statuses: Iterable[BoardStatus]) -> tuple[Any, ...]:
        """Fields worth waking the browser for (resource counters are not)."""
        return tuple(
            (s.name, s.state, s.pid, s.message, s.restarts, s.enabled, s.port, s.writing)
            for s in statuses
        )

    def _encode(self, statuses: tuple[BoardStatus, ...]) -> str:
        server = self._config.server
        snapshot = self._queue_source() if self._queue_source is not None else None
        return json.dumps(
            {
                "boards": [status.to_json() for status in statuses],
                "queue": snapshot.to_json() if snapshot is not None else None,
                "server": {
                    "keep_warm": server.keep_warm,
                    "port": server.port,
                    "config_path": str(self._config.path),
                    "queue_visible": self._config.queue.visible,
                },
                "now": time.time(),
            },
            separators=(",", ":"),
        )

    def state_json(self) -> str:
        with self._lock:
            return self._encode(self._snapshot)
