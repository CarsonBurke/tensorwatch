"""systemd --user integration so the boards come back after a reboot."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "tensorwatch.service"

UNIT_TEMPLATE = """\
# Written by `tensorwatch install-service`. Edit freely; re-running overwrites it.
[Unit]
Description=TensorWatch (registry, supervisor, dashboard)
After=network.target

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
Environment="PYTHONPATH={pythonpath}"
# systemd --user starts with a minimal PATH; tensorboard usually lives in ~/.local/bin.
Environment=PATH=%h/.local/bin:%h/bin:/usr/local/bin:/usr/bin:/bin
ExecStart="{python}" -m tensorwatch serve
ExecReload=/bin/kill -HUP $MAINPID
Restart=always
RestartSec=5
# TensorBoard's data server holds one descriptor per event file, and a large
# logdir has thousands; systemd's default soft limit of 1024 makes it load
# nothing at all ("No dashboards are active for the current data set").
LimitNOFILE=65536
# The manager stops its own boards on SIGTERM; systemd cleans up whatever is left.
KillMode=mixed
TimeoutStopSec=45

[Install]
WantedBy=default.target
"""


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "systemd" / "user"


def unit_path() -> Path:
    return unit_dir() / UNIT_NAME


def render_unit() -> str:
    package_root = Path(__file__).resolve().parent.parent
    for value in (str(package_root), sys.executable):
        # systemd splits directives on whitespace and newlines; a quote or newline
        # in either path would let the value spill into further unit directives.
        if any(char in value for char in '"\n\\'):
            raise ValueError(f"refusing to write a unit for path {value!r}")
    return UNIT_TEMPLATE.format(pythonpath=package_root, python=sys.executable)


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False
    )


def unit_state() -> str:
    if shutil.which("systemctl") is None:
        return "systemctl not available"
    enabled = _systemctl("is-enabled", UNIT_NAME).stdout.strip() or "unknown"
    active = _systemctl("is-active", UNIT_NAME).stdout.strip() or "unknown"
    return f"{enabled}, {active}"


def install(enable: bool = True, linger: bool = False) -> int:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_unit(), encoding="utf-8")
    print(f"wrote {path}")

    if shutil.which("systemctl") is None:
        print("systemctl not found; unit written but not activated")
        return 0

    for args in (("daemon-reload",), ("enable", "--now", UNIT_NAME) if enable else ()):
        if not args:
            continue
        result = _systemctl(*args)
        if result.returncode != 0:
            print(f"systemctl --user {' '.join(args)} failed: {result.stderr.strip()}")
            return 1
        print(f"systemctl --user {' '.join(args)}: ok")

    if linger:
        user = os.environ.get("USER") or Path.home().name
        result = subprocess.run(
            ["loginctl", "enable-linger", user], capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            print(f"loginctl enable-linger {user}: ok (boards start at boot without a login)")
        else:
            print(f"loginctl enable-linger failed: {result.stderr.strip()}")
            return 1

    print(f"state: {unit_state()}")
    return 0


def uninstall() -> int:
    path = unit_path()
    if shutil.which("systemctl") is not None:
        for args in (("disable", "--now", UNIT_NAME),):
            result = _systemctl(*args)
            if result.returncode != 0 and "not loaded" not in result.stderr.lower():
                print(f"systemctl --user {' '.join(args)}: {result.stderr.strip()}")
    if path.exists():
        path.unlink()
        print(f"removed {path}")
        if shutil.which("systemctl") is not None:
            _systemctl("daemon-reload")
    else:
        print(f"{path} not present")
    return 0
