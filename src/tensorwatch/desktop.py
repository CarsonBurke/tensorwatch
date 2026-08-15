"""Desktop integration: an application launcher entry, an icon, and `tensorwatch` on PATH.

The launcher runs ``tensorwatch open``, which starts the manager if it is not running
and then opens the dashboard in its own window, so the entry behaves like an
ordinary desktop app even though the boards live in a systemd user service.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "tensorwatch"
APP_NAME = "TensorWatch"
ICON_SOURCE = Path(__file__).parent / "web" / "icon.svg"

ENTRY_TEMPLATE = """\
[Desktop Entry]
# Written by `tensorwatch install`. Edit freely; re-running overwrites it.
Type=Application
Version=1.5
Name={name}
GenericName=TensorBoard dashboard
Comment=Every registered TensorBoard in one window
Exec={exec_open}
TryExec={try_exec}
Icon={icon}
Terminal=false
Categories=Development;
Keywords=tensorboard;tensorflow;pytorch;jax;training;metrics;ml;
StartupNotify=true
# chromium is launched with --class=tensorwatch, so the window groups under this entry.
StartupWMClass={app_id}
Actions=Restart;Registry;

[Desktop Action Restart]
Name=Restart manager
Exec={exec_restart}

[Desktop Action Registry]
Name=Edit registry
Exec={exec_registry}
"""


def _data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".local" / "share"


def applications_dir() -> Path:
    return _data_home() / "applications"


def icon_dir() -> Path:
    return _data_home() / "icons" / "hicolor" / "scalable" / "apps"


def bin_dir() -> Path:
    raw = os.environ.get("XDG_BIN_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".local" / "bin"


def entry_path() -> Path:
    return applications_dir() / f"{APP_ID}.desktop"


def icon_path() -> Path:
    return icon_dir() / f"{APP_ID}.svg"


def launcher_path() -> Path:
    return bin_dir() / APP_ID


def checkout_launcher() -> Path | None:
    """``bin/tensorwatch`` from this checkout, when running from a source tree."""
    candidate = Path(__file__).resolve().parents[2] / "bin" / APP_ID
    return candidate if candidate.is_file() else None


def command() -> str:
    """Shell-quoted command that runs tensorwatch, preferring the checkout launcher."""
    launcher = checkout_launcher()
    if launcher is not None:
        return _quote(str(launcher))
    installed = shutil.which(APP_ID)
    if installed:
        return _quote(installed)
    return f"{_quote(sys.executable)} -m tensorwatch"


#: Reserved in an Exec value by the Desktop Entry spec; a path containing any of
#: them must be quoted or launchers mis-split it.
_RESERVED = ' \t"\'\\><~|&;$*?#()`'


def _quote(value: str) -> str:
    """Desktop-entry quoting, including ``%%`` so a literal % is not a field code."""
    escaped = value.replace("%", "%%")
    if any(char in escaped for char in _RESERVED):
        escaped = escaped.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return escaped


def render_entry() -> str:
    from .config import config_path

    base = command()
    return ENTRY_TEMPLATE.format(
        name=APP_NAME,
        app_id=APP_ID,
        icon=APP_ID,
        exec_open=f"{base} open",
        try_exec=str(checkout_launcher() or shutil.which(APP_ID) or sys.executable),
        exec_restart="systemctl --user restart tensorwatch.service",
        exec_registry=f"xdg-open {_quote(str(config_path()))}",
    )


def _refresh_caches() -> None:
    if shutil.which("update-desktop-database"):
        subprocess.run(
            ["update-desktop-database", str(applications_dir())],
            capture_output=True,
            check=False,
        )
    if shutil.which("gtk-update-icon-cache"):
        subprocess.run(
            ["gtk-update-icon-cache", "-t", "-f", str(_data_home() / "icons" / "hicolor")],
            capture_output=True,
            check=False,
        )


def link_launcher() -> tuple[bool, str]:
    """Put ``tensorwatch`` on PATH as a symlink to this checkout's launcher."""
    launcher = checkout_launcher()
    target = launcher_path()
    if launcher is None:
        return False, f"{APP_ID} is not a source checkout; skipping the {target} symlink"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_symlink() and target.readlink() == launcher:
            return True, f"{target} already points at {launcher}"
        if not target.is_symlink():
            return False, f"{target} exists and is not a symlink; left untouched"
        target.unlink()
    target.symlink_to(launcher)
    on_path = str(target.parent) in os.environ.get("PATH", "").split(os.pathsep)
    note = "" if on_path else f" (note: {target.parent} is not on PATH)"
    return True, f"linked {target} -> {launcher}{note}"


def install() -> list[str]:
    """Install the launcher entry, icon and PATH symlink. Returns log lines."""
    notes: list[str] = []
    icon_dir().mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON_SOURCE, icon_path())
    notes.append(f"installed icon {icon_path()}")

    applications_dir().mkdir(parents=True, exist_ok=True)
    entry_path().write_text(render_entry(), encoding="utf-8")
    entry_path().chmod(0o755)
    notes.append(f"installed launcher {entry_path()}")

    _ok, message = link_launcher()
    notes.append(message)
    _refresh_caches()
    return notes


def uninstall() -> list[str]:
    notes: list[str] = []
    for path in (entry_path(), icon_path()):
        if path.exists():
            path.unlink()
            notes.append(f"removed {path}")
        else:
            notes.append(f"{path} not present")
    target = launcher_path()
    if target.is_symlink() and target.readlink() == checkout_launcher():
        target.unlink()
        notes.append(f"removed {target}")
    _refresh_caches()
    return notes


def installed() -> bool:
    return entry_path().exists()
