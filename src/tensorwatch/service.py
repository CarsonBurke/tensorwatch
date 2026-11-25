"""systemd --user integration: start TensorWatch when something connects to it.

A socket unit listens on the dashboard port and starts the manager on the first
connection, from the launcher, a bookmark or the CLI.  The manager exits once no
window has been connected for ``[server] idle_exit`` seconds, and its boards
stop with it, so nothing runs while TensorWatch is closed.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from .config import Config

UNIT_NAME = "tensorwatch.service"
SOCKET_NAME = "tensorwatch.socket"
#: First descriptor systemd passes to an activated service (``SD_LISTEN_FDS_START``).
LISTEN_FDS_START = 3

SOCKET_TEMPLATE = """\
# Written by `tensorwatch install-service`. Edit freely; re-running overwrites it.
[Unit]
Description=TensorWatch dashboard socket (starts TensorWatch on first use)

[Socket]
ListenStream={listen}
# The port is taken from [server] in the registry when this file is written;
# re-run `tensorwatch install-service` after changing it.
NoDelay=true

[Install]
WantedBy=sockets.target
"""

UNIT_TEMPLATE = """\
# Written by `tensorwatch install-service`. Edit freely; re-running overwrites it.
# Started by tensorwatch.socket; it exits by itself once no window is open.
[Unit]
Description=TensorWatch (registry, supervisor, dashboard)
Requires=tensorwatch.socket
After=tensorwatch.socket

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
Environment="PYTHONPATH={pythonpath}"
# systemd --user starts with a minimal PATH; tensorboard usually lives in ~/.local/bin.
Environment=PATH=%h/.local/bin:%h/bin:/usr/local/bin:/usr/bin:/bin
ExecStart="{python}" -m tensorwatch serve --exit-when-idle
ExecReload=/bin/kill -HUP $MAINPID
# An idle exit is a clean exit; the socket starts the next instance on demand.
Restart=on-failure
RestartSec=5
# TensorBoard's data server holds one descriptor per event file, and a large
# logdir has thousands; systemd's default soft limit of 1024 makes it load
# nothing at all ("No dashboards are active for the current data set").
LimitNOFILE=65536
# The manager stops its own boards on SIGTERM; systemd cleans up whatever is left.
KillMode=mixed
TimeoutStopSec=45
"""


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "systemd" / "user"


def unit_path() -> Path:
    return unit_dir() / UNIT_NAME


def socket_path() -> Path:
    return unit_dir() / SOCKET_NAME


def listen_address(cfg: Config) -> str:
    """``ListenStream=`` value for the dashboard address in the registry."""
    host = cfg.server.host
    if host == "localhost":
        # ListenStream= takes an address, not a name to resolve.
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host.strip('[]')}]"
    return f"{host}:{cfg.server.port}"


def render_socket(cfg: Config) -> str:
    return SOCKET_TEMPLATE.format(listen=listen_address(cfg))


def render_unit() -> str:
    package_root = Path(__file__).resolve().parent.parent
    for value in (str(package_root), sys.executable):
        # systemd splits directives on whitespace and newlines; a quote or newline
        # in either path would let the value spill into further unit directives.
        if any(char in value for char in '"\n\\'):
            raise ValueError(f"refusing to write a unit for path {value!r}")
    return UNIT_TEMPLATE.format(pythonpath=package_root, python=sys.executable)


def installed() -> bool:
    """True when TensorWatch starts through an enabled socket unit.

    Asks systemd rather than trusting the file: a socket written with
    ``--no-enable``, or a session without a user bus (``systemctl --user``
    fails over SSH), must fall back to starting a manager directly.
    """
    if not socket_path().exists() or shutil.which("systemctl") is None:
        return False
    return _query("is-enabled", SOCKET_NAME) in ("enabled", "enabled-runtime")


def socket_active() -> bool:
    """Whether systemd listens on the dashboard port right now."""
    return _query("is-active", SOCKET_NAME) == "active"


def listen_stream(text: str) -> str | None:
    """The ``ListenStream=`` value in socket unit text."""
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "ListenStream":
            return value.strip()
    return None


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False
    )


def _query(verb: str, unit: str) -> str:
    return _systemctl(verb, unit).stdout.strip() or "unknown"


def manager_active() -> bool:
    """Whether the manager runs now, asked without connecting to its port.

    A connection to the socket would start it, which a read-only command such
    as ``status`` must not do.
    """
    return _query("is-active", UNIT_NAME) in ("active", "reloading", "activating")


def listen_socket() -> socket.socket | None:
    """The listening socket systemd passed to this process, if any.

    Mirrors ``sd_listen_fds(unset_environment=1)``: the variables are removed so
    the boards this process spawns do not think they were activated too.
    """
    pid = os.environ.pop("LISTEN_PID", None)
    count = os.environ.pop("LISTEN_FDS", None)
    os.environ.pop("LISTEN_FDNAMES", None)
    if pid is None or count is None:
        return None
    try:
        if int(pid) != os.getpid() or int(count) < 1:
            return None
    except ValueError:
        return None
    os.set_inheritable(LISTEN_FDS_START, False)
    return socket.socket(fileno=LISTEN_FDS_START)


def unit_state() -> str:
    if shutil.which("systemctl") is None:
        return "systemctl not available"
    socket_state = f"{_query('is-enabled', SOCKET_NAME)}, {_query('is-active', SOCKET_NAME)}"
    return f"socket {socket_state}; manager {_query('is-active', UNIT_NAME)}"


def _run(*args: str) -> bool:
    result = _systemctl(*args)
    if result.returncode != 0:
        print(f"systemctl --user {' '.join(args)} failed: {result.stderr.strip()}")
        return False
    print(f"systemctl --user {' '.join(args)}: ok")
    return True


def install(cfg: Config, enable: bool = True) -> int:
    have_systemctl = shutil.which("systemctl") is not None
    # Earlier versions enabled the service itself so it ran from login (or boot,
    # with linger) on.  Drop that link while the old unit still says where it
    # lives, even with --no-enable: otherwise every login still starts it.
    if have_systemctl and _query("is-enabled", UNIT_NAME) in ("enabled", "enabled-runtime"):
        if not _run("disable", UNIT_NAME):
            return 1
    previous = socket_path().read_text(encoding="utf-8") if socket_path().exists() else None
    unit_dir().mkdir(parents=True, exist_ok=True)
    unit_path().write_text(render_unit(), encoding="utf-8")
    print(f"wrote {unit_path()}")
    socket_path().write_text(render_socket(cfg), encoding="utf-8")
    print(f"wrote {socket_path()}")
    moved = previous is not None and listen_stream(previous) != listen_address(cfg)

    if not have_systemctl:
        print("systemctl not found; units written but not activated")
        return 0
    if not _run("daemon-reload"):
        return 1
    if not enable:
        return 0
    # A manager that was not started by the socket holds the port the socket
    # needs.  Restart it under the socket; its boards come back when a window opens.
    if socket_active():
        if moved:
            # A listening socket keeps its old address across daemon-reload; it
            # only binds the new one when restarted, which also stops the manager.
            print(f"moving the socket to {listen_address(cfg)}; open windows reconnect")
            if not _run("restart", SOCKET_NAME):
                return 1
    elif manager_active():
        print("stopping the running manager so the socket can take its port")
        if not _run("stop", UNIT_NAME):
            return 1
    if not _run("enable", "--now", SOCKET_NAME):
        return 1
    print(f"state: {unit_state()}")
    return 0


def uninstall() -> int:
    if shutil.which("systemctl") is not None:
        for args in (("disable", "--now", SOCKET_NAME), ("disable", "--now", UNIT_NAME)):
            result = _systemctl(*args)
            if result.returncode != 0 and "not loaded" not in result.stderr.lower():
                print(f"systemctl --user {' '.join(args)}: {result.stderr.strip()}")
    removed = False
    for path in (socket_path(), unit_path()):
        if path.exists():
            path.unlink()
            removed = True
            print(f"removed {path}")
        else:
            print(f"{path} not present")
    if removed and shutil.which("systemctl") is not None:
        _systemctl("daemon-reload")
    return 0
