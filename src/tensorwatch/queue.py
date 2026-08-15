"""mlq queue integration over the mlqd socket.

TensorWatch subscribes to `mlqd` directly - it opens the Unix socket, sends one
``subscribe`` op, and then reads pushed snapshots. There is no polling and no
`mlq` subprocess in the loop: the daemon sends a frame when, and only when, the
queue changes.

Wire format (mlqueue/src/protocol.rs): big-endian u32 length prefix followed by
JSON. The reply payload of a subscription frame is the same ``status`` view that
``mlq status --json`` prints, so one parser serves both.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

#: Must match ``PROTOCOL_VERSION`` in mlqueue/src/protocol.rs.
PROTOCOL_VERSION = 8
#: Mirrors the daemon's ``DEFAULT_MAX_FRAME_BYTES``; a bigger prefix is a desync.
MAX_FRAME_BYTES = 1 << 20
#: States mlq reports for work that has not finished yet.
LIVE_STATES = ("running", "queued", "held", "ready", "pending")
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)
#: A subscription that lasted this long was healthy: the next failure restarts the
#: backoff ladder instead of inheriting an old outage's delay.
STABLE_SESSION = 60.0
READ_TIMEOUT = 5.0
#: A frame that stops halfway means a half-open socket; reconnect instead of hanging.
MID_FRAME_TIMEOUT = 30.0
#: A job is attributed to a board when the board's logdir sits at most this many
#: levels below the job's working directory...
MAX_BOARD_DEPTH = 3
#: ...when falling back to the working directory, and then only if it leaves a
#: single candidate: a repo with several watched logdirs cannot say which is moving.


def socket_path() -> Path:
    """Resolve mlqd's socket exactly like mlqueue's ``Paths``."""
    override = os.environ.get("MLQUEUE_RUNTIME_DIR")
    if override:
        return Path(override) / "mlqd.sock"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "mlqueue" / "mlqd.sock"
    state = os.environ.get("MLQUEUE_STATE_DIR") or os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    if not os.environ.get("MLQUEUE_STATE_DIR"):
        base = base / "mlqueue"
    return base / "runtime" / "mlqd.sock"


@dataclass(frozen=True, slots=True)
class QueueJob:
    """One mlq job, reduced to what the sidebar shows."""

    id: int
    name: str
    state: str
    priority: int
    #: mlq's admission reason for a job that is not running ("protected_drain", ...).
    reason: str
    cwd: str
    project: str
    #: Boards watching this job's output, matched by logdir containment.
    boards: tuple[str, ...]
    #: The single board to open for this job, when it is unambiguous.
    board: str | None
    #: Epoch seconds of the last transition (start time for a running job).
    since: float | None
    queued_at: float | None
    time_limit: float | None
    attempts: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """Everything the dashboard needs about the queue."""

    connected: bool = False
    error: str | None = None
    running: tuple[QueueJob, ...] = ()
    queued: tuple[QueueJob, ...] = ()
    active_leases: int = 0
    effective_limit: int | None = None
    admission_blocked: bool = False
    protected_job: int | None = None
    updated_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "error": self.error,
            "running": [job.to_json() for job in self.running],
            "queued": [job.to_json() for job in self.queued],
            "active_leases": self.active_leases,
            "effective_limit": self.effective_limit,
            "admission_blocked": self.admission_blocked,
            "protected_job": self.protected_job,
            "updated_at": self.updated_at,
        }

    @property
    def signature(self) -> tuple[Any, ...]:
        """What a viewer must be told about (elapsed time is derived client-side)."""
        return (
            self.connected,
            self.error,
            self.active_leases,
            self.effective_limit,
            self.admission_blocked,
            tuple((job.id, job.state, job.reason, job.boards) for job in self.running),
            tuple((job.id, job.state, job.reason, job.boards) for job in self.queued),
        )


def _ms(value: Any) -> float | None:
    return value / 1000.0 if isinstance(value, (int, float)) and value else None


def _boards_for(
    cwd: str, args: Sequence[str], board_dirs: Mapping[str, Path]
) -> tuple[str, ...]:
    """Boards this job actually writes to.

    The command line is the strong signal: a run that passes ``--run-dir
    runs/vapo`` or ``--output samples/rl/runs/x`` names its own logdir, so only
    the board watching that path is marked.  A job's cwd is merely where ``mlq
    submit`` ran - a repository holding several watched logdirs (``tb_logs`` and
    ``pretraining/runs`` and ``postraining/runs``) says nothing about which one is
    moving - so cwd containment is used only when it leaves exactly one candidate.
    """
    if not cwd:
        return ()
    root = _resolved(Path(cwd))
    resolved = {name: _resolved(logdir) for name, logdir in board_dirs.items()}

    referenced = _referenced_paths(root, args)
    named = [
        name
        for name, logdir in resolved.items()
        if any(_touches(candidate, logdir) for candidate in referenced)
    ]
    if named:
        return tuple(named)

    inside = [
        name
        for name, logdir in resolved.items()
        if _within(logdir, root, MAX_BOARD_DEPTH)
    ]
    # Ambiguous (or absurdly broad, e.g. $HOME) means "no board", not "the first".
    return tuple(inside) if len(inside) == 1 else ()


def _referenced_paths(root: Path, args: Sequence[str]) -> list[Path]:
    """Path-looking arguments, resolved against the job's working directory."""
    found: list[Path] = []
    for token in args:
        value = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
        if value.startswith("-") or "/" not in value:
            continue
        candidate = Path(value)
        found.append(_resolved(candidate if candidate.is_absolute() else root / candidate))
    return found


def _touches(candidate: Path, logdir: Path) -> bool:
    """True when a referenced path is the logdir, inside it, or contains it."""
    try:
        return candidate == logdir or candidate.is_relative_to(logdir) or logdir.is_relative_to(candidate)
    except (ValueError, OSError):
        return False


def _within(logdir: Path, root: Path, max_depth: int) -> bool:
    try:
        if logdir != root and not logdir.is_relative_to(root):
            return False
    except (ValueError, OSError):
        return False
    return len(logdir.parts) - len(root.parts) <= max_depth


def _resolved(path: Path) -> Path:
    """Resolve symlinks so containment is not merely lexical."""
    try:
        return path.resolve()
    except OSError:
        return path


def parse(view: Mapping[str, Any], board_dirs: Mapping[str, Path]) -> QueueSnapshot:
    """Turn an mlq ``status`` view into a snapshot."""
    running: list[QueueJob] = []
    queued: list[QueueJob] = []
    for raw in view.get("jobs") or ():
        state = str(raw.get("state") or "")
        if state not in LIVE_STATES:
            continue
        cwd = str(raw.get("cwd") or "")
        attempt_count = raw.get("attemptCount")
        max_attempts = raw.get("maxAttempts")
        args = [str(token) for token in (raw.get("args") or ())]
        boards = _boards_for(cwd, args, board_dirs)
        job = QueueJob(
            id=int(raw.get("id") or 0),
            name=str(raw.get("name") or "?"),
            state=state,
            priority=int(raw.get("priority") or 0),
            reason=str(raw.get("eligibility") or ""),
            cwd=cwd,
            project=Path(cwd).name if cwd else "",
            boards=boards,
            board=boards[0] if boards else None,
            since=_ms(raw.get("updatedAt")),
            queued_at=_ms(raw.get("createdAt")),
            time_limit=(raw["timeLimitMs"] / 1000.0) if raw.get("timeLimitMs") else None,
            attempts=(
                f"{attempt_count}/{max_attempts}"
                if isinstance(attempt_count, int) and isinstance(max_attempts, int)
                else ""
            ),
        )
        (running if state == "running" else queued).append(job)

    queued.sort(key=lambda job: (-job.priority, job.id))
    running.sort(key=lambda job: job.since or 0.0)
    reservation = view.get("reservation") or {}
    limit = view.get("effectiveLimit")
    return QueueSnapshot(
        connected=True,
        error=None,
        running=tuple(running),
        queued=tuple(queued),
        active_leases=int(view.get("activeLeases") or 0),
        effective_limit=int(limit) if isinstance(limit, int) else None,
        admission_blocked=bool(view.get("admissionBlocked")),
        protected_job=reservation.get("protectedJob"),
        updated_at=time.time(),
    )


def subscribe_request() -> bytes:
    """The single framed request that turns a connection into a subscription."""
    body = json.dumps(
        {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": uuid.uuid4().hex,
            "op": {"type": "subscribe"},
        },
        separators=(",", ":"),
    ).encode()
    return struct.pack(">I", len(body)) + body


def read_frame(sock: socket.socket) -> bytes | None:
    """Read one length-prefixed frame.

    Returns ``None`` when mlqd closed the connection and ``b""`` when the socket
    was simply idle - an idle queue pushes nothing for hours, so that is the
    normal case, not an error.
    """
    header = _read_exactly(sock, 4, idle_ok=True)
    if header is None:
        return None
    if not header:
        return b""
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > MAX_FRAME_BYTES:
        raise ConnectionError(f"mlqd sent a {length} byte frame; connection out of sync")
    body = _read_exactly(sock, length)
    if body is None:
        return None
    if len(body) != length:
        raise ConnectionError("mlqd truncated a frame; connection out of sync")
    return body


def _read_exactly(sock: socket.socket, count: int, idle_ok: bool = False) -> bytes | None:
    """Read ``count`` bytes; ``None`` on close, ``b""`` when nothing was in flight.

    ``idle_ok`` applies to the frame header only: a body that starts late is still
    a body, and waiting for it (up to ``MID_FRAME_TIMEOUT``) beats tearing down a
    healthy subscription.
    """
    chunks: list[bytes] = []
    remaining = count
    deadline = time.monotonic() + MID_FRAME_TIMEOUT
    while remaining:
        try:
            chunk = sock.recv(remaining)
        except TimeoutError:
            if idle_ok and not chunks:
                return b""  # idle: no frame in flight
            if time.monotonic() > deadline:
                # A half-open socket can stop mid-frame; do not wait forever.
                raise ConnectionError("mlqd stopped mid-frame") from None
            continue
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def status_view(frame: bytes) -> Mapping[str, Any]:
    """Extract the ``status`` view from a response frame, or raise ConnectionError."""
    payload = json.loads(frame)
    error = payload.get("error")
    if error:
        raise ConnectionError(f"{error.get('code', 'error')}: {error.get('message', '')}")
    reply = payload.get("reply") or {}
    if reply.get("type") != "status":
        raise ConnectionError(f"unexpected reply {reply.get('type')!r} on the subscription")
    return reply


class QueueSubscriber(threading.Thread):
    """Keeps a live mlqd subscription and exposes the latest snapshot."""

    def __init__(
        self,
        path: Path,
        board_dirs: Callable[[], Mapping[str, Path]],
        on_change: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(name="tensorwatch-queue", daemon=True)
        self._path = path
        self._board_dirs = board_dirs
        self._on_change = on_change
        self._snapshot = QueueSnapshot()
        #: Last view received, kept so a registry reload can re-match boards
        #: without waiting for the queue to change.
        self._view: Mapping[str, Any] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    # ------------------------------------------------------------------ access

    def snapshot(self) -> QueueSnapshot:
        with self._lock:
            return self._snapshot

    def refresh(self) -> None:
        """Re-parse the last view, e.g. after the registry gained a board."""
        with self._lock:
            view = self._view
        if view is not None:
            self._absorb(view)

    def shutdown(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # ------------------------------------------------------------------- loop

    def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._session()
                attempt = 0
            except Exception as exc:  # a bad frame must degrade, not kill the thread
                self._degrade(str(exc) or exc.__class__.__name__)
                # A session that ran for a while was healthy; do not inherit the
                # delay from an older outage.
                attempt = 0 if time.monotonic() - started >= STABLE_SESSION else attempt + 1
            if self._stop.is_set():
                return
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            self._stop.wait(delay)

    def _session(self) -> None:
        if not self._path.exists():
            raise ConnectionError(f"mlqd socket not found at {self._path}")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            self._sock = sock
            sock.settimeout(READ_TIMEOUT)
            sock.connect(str(self._path))
            sock.sendall(subscribe_request())
            while not self._stop.is_set():
                frame = read_frame(sock)
                if frame is None:
                    raise ConnectionError("mlqd closed the subscription")
                if not frame:
                    continue  # read timeout: an idle queue simply sends nothing
                self._absorb(status_view(frame))
        self._sock = None

    def _absorb(self, view: Mapping[str, Any]) -> None:
        fresh = parse(view, self._board_dirs())
        with self._lock:
            changed = fresh.signature != self._snapshot.signature
            self._snapshot = fresh
            self._view = view
        if changed and self._on_change is not None:
            self._on_change()

    def _degrade(self, message: str) -> None:
        """Mark the queue offline but keep the last jobs, labelled as stale."""
        with self._lock:
            previous = self._snapshot
            self._snapshot = replace(
                previous, connected=False, error=message, updated_at=time.time()
            )
            changed = previous.signature != self._snapshot.signature
        if changed and self._on_change is not None:
            self._on_change()


def start(
    path: Path | None,
    board_dirs: Callable[[], Mapping[str, Path]],
    on_change: Callable[[], None] | None = None,
) -> QueueSubscriber:
    """Start a subscriber thread; it reconnects on its own if mlqd is absent."""
    subscriber = QueueSubscriber(path or socket_path(), board_dirs, on_change)
    subscriber.start()
    return subscriber


def one_shot(
    path: Path | None = None,
    board_dirs: Mapping[str, Path] | None = None,
    timeout: float = 5.0,
) -> QueueSnapshot:
    """Single snapshot for CLI use, without a background thread."""
    target = path or socket_path()
    if not target.exists():
        return QueueSnapshot(error=f"mlqd socket not found at {target}", updated_at=time.time())
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(target))
            sock.sendall(subscribe_request())
            frame = read_frame(sock)
            if not frame:
                raise ConnectionError("mlqd sent no snapshot")
            return parse(status_view(frame), board_dirs or {})
    except (OSError, ConnectionError, json.JSONDecodeError, struct.error) as exc:
        return QueueSnapshot(error=str(exc), updated_at=time.time())


__all__: Sequence[str] = (
    "QueueJob",
    "QueueSnapshot",
    "QueueSubscriber",
    "one_shot",
    "parse",
    "socket_path",
    "start",
    "status_view",
    "subscribe_request",
)
