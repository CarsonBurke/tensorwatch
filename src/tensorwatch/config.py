"""Configuration model for tensorwatch.

The registry is a hand-editable TOML file (default ``~/.config/tensorwatch/config.toml``):

    [server]
    port = 6005
    keep_warm = 2

    [defaults]
    reload_interval = 60

    [[board]]
    name = "cleanrl"
    logdir = "~/Documents/repositories/cleanrl/runs"
    port = 6100

Every board is one long-lived ``tensorboard`` process bound to its own loopback
port.  The dashboard never proxies board traffic, so board ports are part of the
public contract: keep them stable so browser caches and bookmarks stay valid.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

APP_NAME: Final = "tensorwatch"

#: Board names double as URL path segments, systemd-ish log file names and CLI
#: arguments, so keep them boring.
NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

DEFAULT_SERVER_HOST: Final = "127.0.0.1"
DEFAULT_SERVER_PORT: Final = 6005
#: Board ports start well above 6006-6010, where hand-started TensorBoards land,
#: so managed boards never fight a `tensorboard --logdir runs` in a terminal.
DEFAULT_PORT_BASE: Final = 6100
DEFAULT_COMMAND: Final = ("tensorboard",)

#: ``always``    - start with the manager, restart forever (the reboot case).
#: ``on_demand`` - start when the dashboard opens the board, stop when idle.
#: ``manual``    - only ever started by an explicit request.
AUTOSTART_MODES: Final = ("always", "on_demand", "manual")

#: TensorBoard has no authentication and serves whole logdir trees, so anything
#: other than these needs an explicit ``[server] allow_remote = true``.
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})

#: Chrome refuses to connect to these (X11, IRC, NFS...); reject them early
#: instead of shipping a board nobody can open.
UNSAFE_PORTS: Final = frozenset(
    {1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79,
     87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137,
     139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530, 531, 532,
     540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995, 1719, 1720, 1723,
     2049, 3659, 4045, 4400, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669,
     6679, 6697, 10080}
)


class ConfigError(Exception):
    """Raised when the registry file is malformed."""


def _xdg(var: str, fallback: str) -> Path:
    raw = os.environ.get(var)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / fallback


def config_path() -> Path:
    """Registry path; ``TENSORWATCH_CONFIG`` wins so tests and one-offs stay isolated."""
    override = os.environ.get("TENSORWATCH_CONFIG")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME / "config.toml"


def state_dir() -> Path:
    override = os.environ.get("TENSORWATCH_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return _xdg("XDG_STATE_HOME", ".local/state") / APP_NAME


def log_dir() -> Path:
    return state_dir() / "logs"


@dataclass(frozen=True, slots=True)
class BoardSpec:
    """One registered TensorBoard instance."""

    name: str
    port: int
    logdir: Path | None = None
    logdir_spec: str | None = None
    enabled: bool = True
    autostart: str = "always"
    command: tuple[str, ...] = DEFAULT_COMMAND
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    host: str = "127.0.0.1"
    #: TensorBoard defaults to 5s, which means a permanent rescan of every event
    #: file in the tree.  With thousands of runs that is pure background I/O.
    reload_interval: float = 60.0
    samples_per_plugin: str | None = None
    #: ``on_demand`` boards stop after this many seconds without a viewer.
    idle_timeout: float = 900.0
    #: How long a cold start may take before it is declared failed.  Large
    #: logdirs bind the port quickly but this is the ceiling for slow disks.
    start_timeout: float = 180.0
    description: str = ""

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}/"

    @property
    def exposed(self) -> bool:
        """True when this board can be reached from outside the machine."""
        return self.host not in LOOPBACK_HOSTS or any(
            arg == "--bind_all" or arg.startswith("--bind_all=") for arg in self.args
        )

    @property
    def log_path(self) -> Path:
        return log_dir() / f"{self.name}.log"

    @property
    def target(self) -> str:
        """Human description of what this board serves."""
        return str(self.logdir) if self.logdir is not None else (self.logdir_spec or "")

    def argv(self) -> list[str]:
        argv = [*self.command]
        if self.logdir is not None:
            argv += ["--logdir", str(self.logdir)]
        else:
            argv += ["--logdir_spec", str(self.logdir_spec)]
        argv += [
            "--host", self.host,
            "--port", str(self.port),
            "--reload_interval", _fmt_number(self.reload_interval),
            "--window_title", f"TB: {self.name}",
        ]
        if self.samples_per_plugin:
            argv += ["--samples_per_plugin", self.samples_per_plugin]
        argv += list(self.args)
        return argv

    def process_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in self.env.items()})
        return env


@dataclass(frozen=True, slots=True)
class ServerSpec:
    """Dashboard/control-plane settings."""

    host: str = DEFAULT_SERVER_HOST
    port: int = DEFAULT_SERVER_PORT
    port_base: int = DEFAULT_PORT_BASE
    #: Live board iframes kept mounted in the dashboard.  Each mounted iframe is
    #: a full TensorBoard SPA (tens of MB of JS heap plus its own polling), so
    #: the dashboard evicts least-recently-used panes past this count.
    keep_warm: int = 2
    #: Seconds between spawns during a cold start.  Starting every board at once
    #: turns boot into an I/O storm because each one walks its whole logdir.
    start_stagger: float = 3.0
    #: Boards allowed to be warming up (spawned, port not yet accepting) at once.
    max_warming: int = 2
    #: Interval of the supervisor tick: liveness probe + resource sampling.
    poll_interval: float = 2.0
    #: Opt-in required before the dashboard or any board may bind a non-loopback
    #: address; without it such a config is rejected instead of silently exposing
    #: unauthenticated TensorBoards on the network.
    allow_remote: bool = False


@dataclass(frozen=True, slots=True)
class QueueSpec:
    """mlq integration: the queue panel in the dashboard sidebar.

    TensorWatch subscribes to the mlqd socket, so there is no poll interval to
    tune: the daemon pushes a snapshot whenever the queue changes.
    """

    enabled: bool = True
    #: Override for mlqd's socket; empty means the standard mlqueue location.
    socket: str = ""
    #: Queued jobs shown before the panel collapses the rest behind "+N more".
    visible: int = 5


@dataclass(frozen=True, slots=True)
class Config:
    server: ServerSpec
    boards: tuple[BoardSpec, ...]
    path: Path
    queue: QueueSpec = field(default_factory=QueueSpec)
    #: Boards whose port was auto-assigned during load; the CLI persists these so
    #: ports never move on the next start.
    assigned_ports: Mapping[str, int] = field(default_factory=dict)

    @property
    def board_dirs(self) -> dict[str, Path]:
        """Board name -> logdir, for matching queue jobs to boards."""
        return {spec.name: spec.logdir for spec in self.boards if spec.logdir is not None}

    def board(self, name: str) -> BoardSpec | None:
        for spec in self.boards:
            if spec.name == name:
                return spec
        return None

    @property
    def dashboard_url(self) -> str:
        host = "127.0.0.1" if self.server.host in ("0.0.0.0", "::") else self.server.host
        return f"http://{host}:{self.server.port}/"


def _fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


class _Reader:
    """Small typed accessor that reports the offending TOML key on error."""

    def __init__(self, data: Mapping[str, Any], where: str) -> None:
        self.data = data
        self.where = where

    def unknown(self, allowed: Sequence[str]) -> None:
        extra = sorted(set(self.data) - set(allowed))
        if extra:
            raise ConfigError(f"{self.where}: unknown key(s) {', '.join(extra)}")

    def _get(self, key: str, default: Any) -> Any:
        return self.data.get(key, default)

    def str_(self, key: str, default: str | None = None) -> str | None:
        value = self._get(key, default)
        if value is None or isinstance(value, str):
            return value
        raise ConfigError(f"{self.where}.{key}: expected string, got {type(value).__name__}")

    def bool_(self, key: str, default: bool) -> bool:
        value = self._get(key, default)
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{self.where}.{key}: expected boolean")

    def int_(self, key: str, default: int | None) -> int | None:
        value = self._get(key, default)
        if value is None or (isinstance(value, int) and not isinstance(value, bool)):
            return value
        raise ConfigError(f"{self.where}.{key}: expected integer")

    def float_(self, key: str, default: float) -> float:
        value = self._get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{self.where}.{key}: expected number")
        return float(value)

    def strlist(self, key: str, default: Sequence[str]) -> tuple[str, ...]:
        value = self._get(key, None)
        if value is None:
            return tuple(default)
        if isinstance(value, str):
            raise ConfigError(f"{self.where}.{key}: expected a list of strings, got a string")
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ConfigError(f"{self.where}.{key}: expected a list of strings")
        return tuple(value)

    def table(self, key: str) -> dict[str, str]:
        value = self._get(key, None)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigError(f"{self.where}.{key}: expected a table")
        return {str(k): str(v) for k, v in value.items()}


_BOARD_KEYS: Final = (
    "name", "logdir", "logdir_spec", "port", "enabled", "autostart", "command",
    "args", "env", "cwd", "host", "reload_interval", "samples_per_plugin",
    "idle_timeout", "start_timeout", "description",
)
_DEFAULT_KEYS: Final = tuple(k for k in _BOARD_KEYS if k not in ("name", "logdir", "logdir_spec", "port"))


def _expand(raw: str) -> Path:
    return Path(os.path.expandvars(raw)).expanduser()


def _board_from(
    raw: Mapping[str, Any], defaults: Mapping[str, Any], index: int, allow_remote: bool = False
) -> BoardSpec:
    merged: dict[str, Any] = {**defaults, **raw}
    reader = _Reader(merged, f"board[{index}]")
    reader.unknown(_BOARD_KEYS)

    name = reader.str_("name")
    logdir_raw = reader.str_("logdir")
    logdir_spec = reader.str_("logdir_spec")
    if not name:
        if not logdir_raw:
            raise ConfigError(f"board[{index}]: needs a name")
        name = _expand(logdir_raw).name or "board"
    if not NAME_RE.match(name):
        raise ConfigError(f"board[{index}]: invalid name {name!r} (allowed: letters, digits, . _ -)")
    if bool(logdir_raw) == bool(logdir_spec):
        raise ConfigError(f"board {name!r}: set exactly one of logdir / logdir_spec")

    autostart = reader.str_("autostart", "always")
    if autostart not in AUTOSTART_MODES:
        raise ConfigError(
            f"board {name!r}: autostart must be one of {', '.join(AUTOSTART_MODES)}"
        )

    command = reader.strlist("command", DEFAULT_COMMAND)
    if not command:
        raise ConfigError(f"board {name!r}: command must not be empty")

    cwd_raw = reader.str_("cwd")
    port = reader.int_("port", None)
    if port is not None and not (1 <= port <= 65535):
        raise ConfigError(f"board {name!r}: port {port} out of range")
    if port in UNSAFE_PORTS:
        raise ConfigError(f"board {name!r}: port {port} is blocked by browsers")

    spec = BoardSpec(
        name=name,
        port=port or 0,
        logdir=_expand(logdir_raw) if logdir_raw else None,
        logdir_spec=logdir_spec,
        enabled=reader.bool_("enabled", True),
        autostart=autostart,
        command=command,
        args=reader.strlist("args", ()),
        env=reader.table("env"),
        cwd=_expand(cwd_raw) if cwd_raw else None,
        host=reader.str_("host", "127.0.0.1") or "127.0.0.1",
        reload_interval=reader.float_("reload_interval", 60.0),
        samples_per_plugin=reader.str_("samples_per_plugin"),
        idle_timeout=reader.float_("idle_timeout", 900.0),
        start_timeout=reader.float_("start_timeout", 180.0),
        description=reader.str_("description", "") or "",
    )
    if spec.exposed and not allow_remote:
        raise ConfigError(
            f"board {name!r} would listen outside this machine (host {spec.host!r}"
            f"{' plus --bind_all' if '--bind_all' in spec.args else ''}). TensorBoard has no "
            "authentication; set [server] allow_remote = true if that is really intended"
        )
    return spec


def _queue_from(raw: Mapping[str, Any]) -> QueueSpec:
    reader = _Reader(raw, "queue")
    reader.unknown(("enabled", "socket", "visible"))
    return QueueSpec(
        enabled=reader.bool_("enabled", True),
        socket=reader.str_("socket", "") or "",
        visible=max(1, reader.int_("visible", 5) or 5),
    )


def _server_from(raw: Mapping[str, Any]) -> ServerSpec:
    reader = _Reader(raw, "server")
    reader.unknown(("host", "port", "port_base", "keep_warm", "start_stagger",
                    "max_warming", "poll_interval", "allow_remote"))
    port = reader.int_("port", DEFAULT_SERVER_PORT) or DEFAULT_SERVER_PORT
    if port in UNSAFE_PORTS:
        raise ConfigError(f"server.port {port} is blocked by browsers")
    allow_remote = reader.bool_("allow_remote", False)
    host = reader.str_("host", DEFAULT_SERVER_HOST) or DEFAULT_SERVER_HOST
    if host not in LOOPBACK_HOSTS and not allow_remote:
        raise ConfigError(
            f"server.host {host!r} exposes the unauthenticated dashboard beyond this "
            "machine; set [server] allow_remote = true if that is really intended"
        )
    return ServerSpec(
        host=host,
        port=port,
        port_base=reader.int_("port_base", DEFAULT_PORT_BASE) or DEFAULT_PORT_BASE,
        keep_warm=max(1, reader.int_("keep_warm", 2) or 2),
        start_stagger=max(0.0, reader.float_("start_stagger", 3.0)),
        max_warming=max(1, reader.int_("max_warming", 2) or 2),
        poll_interval=max(0.25, reader.float_("poll_interval", 2.0)),
        allow_remote=allow_remote,
    )


def next_free_port(base: int, taken: set[int]) -> int:
    port = base
    while port in taken or port in UNSAFE_PORTS:
        port += 1
        if port > 65535:
            raise ConfigError("ran out of ports")
    return port


def parse(text: str, path: Path | None = None) -> Config:
    """Parse registry text into a validated :class:`Config`."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path or '<config>'}: {exc}") from exc

    unknown = sorted(set(data) - {"server", "defaults", "board", "queue"})
    if unknown:
        raise ConfigError(f"unknown top-level table(s): {', '.join(unknown)}")

    server = _server_from(data.get("server") or {})
    queue = _queue_from(data.get("queue") or {})
    defaults = data.get("defaults") or {}
    _Reader(defaults, "defaults").unknown(_DEFAULT_KEYS)

    raw_boards = data.get("board") or []
    if not isinstance(raw_boards, list):
        raise ConfigError("board must be an array of tables ([[board]])")

    boards = [
        _board_from(raw, defaults, i, server.allow_remote) for i, raw in enumerate(raw_boards)
    ]

    seen: dict[str, int] = {}
    for spec in boards:
        if spec.name in seen:
            raise ConfigError(f"duplicate board name {spec.name!r}")
        seen[spec.name] = 1

    taken = {spec.port for spec in boards if spec.port} | {server.port}
    if len(taken) != len([s for s in boards if s.port]) + 1:
        ports = [s.port for s in boards if s.port] + [server.port]
        dupes = sorted({p for p in ports if ports.count(p) > 1})
        raise ConfigError(f"port(s) used by more than one board: {dupes}")

    assigned: dict[str, int] = {}
    resolved: list[BoardSpec] = []
    for spec in boards:
        if spec.port:
            resolved.append(spec)
            continue
        port = next_free_port(server.port_base, taken)
        taken.add(port)
        assigned[spec.name] = port
        resolved.append(BoardSpec(**{**_as_dict(spec), "port": port}))

    return Config(
        server=server,
        boards=tuple(resolved),
        path=path or config_path(),
        queue=queue,
        assigned_ports=assigned,
    )


def _as_dict(spec: BoardSpec) -> dict[str, Any]:
    return {slot: getattr(spec, slot) for slot in BoardSpec.__slots__}


def load(path: Path | None = None) -> Config:
    """Load the registry; a missing file yields an empty registry."""
    target = path or config_path()
    if not target.exists():
        return Config(server=ServerSpec(), boards=(), path=target)
    return parse(target.read_text(encoding="utf-8"), target)
