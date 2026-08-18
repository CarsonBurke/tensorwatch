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
WEB_DIR = Path(__file__).parent / "web"
ICON_SOURCE = WEB_DIR / "icon.svg"
ICON_PNG_SIZES = (16, 32, 48, 128, 256)

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
# Wayland --app windows report chrome-<host>__-Default, not --class.
StartupWMClass={wm_class}
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


def icon_png_path(size: int) -> Path:
    return _data_home() / "icons" / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ID}.png"


def icon_png_source(size: int) -> Path:
    return WEB_DIR / f"icon-{size}.png"


def bin_dir() -> Path:
    raw = os.environ.get("XDG_BIN_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".local" / "bin"


def entry_path() -> Path:
    return applications_dir() / f"{APP_ID}.desktop"


def chrome_app_id(url: str) -> str:
    """Wayland app id Chromium assigns to ``--app=url`` windows."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    path = (parsed.path or "/").replace("/", "_")
    return f"chrome-{host}_{path}-Default"


def chrome_app_entry_path(url: str) -> Path:
    return applications_dir() / f"{chrome_app_id(url)}.desktop"


def icon_path() -> Path:
    return icon_dir() / f"{APP_ID}.svg"


def launcher_path() -> Path:
    return bin_dir() / APP_ID


def checkout_launcher() -> Path | None:
    """``bin/tensorwatch`` from this checkout, when running from a source tree."""
    candidate = Path(__file__).resolve().parents[2] / "bin" / APP_ID
    return candidate if candidate.is_file() else None


def chrome_profile() -> Path:
    """Private Chromium profile so --class is not eaten by a running browser."""
    from .config import state_dir

    path = state_dir() / "chromium-app"
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def render_entry(*, wm_class: str | None = None, icon: str | None = None) -> str:
    from .config import config_path, load

    base = command()
    class_name = wm_class or chrome_app_id(load().dashboard_url)
    return ENTRY_TEMPLATE.format(
        name=APP_NAME,
        wm_class=class_name,
        icon=icon or APP_ID,
        exec_open=f"{base} open",
        try_exec=str(checkout_launcher() or shutil.which(APP_ID) or sys.executable),
        exec_restart="systemctl --user restart tensorwatch.service",
        exec_registry=f"xdg-open {_quote(str(config_path()))}",
    )


def install_window_entry(url: str) -> Path:
    """Desktop file whose name matches Chromium's Wayland app id for ``url``.

    COSMIC skips NoDisplay entries and treats dots in Icon= names as reverse-DNS
    pieces, so this file stays visible and Icon= is an absolute path.
    """
    applications_dir().mkdir(parents=True, exist_ok=True)
    app_id = chrome_app_id(url)
    path = chrome_app_entry_path(url)
    icon = str(icon_path() if icon_path().is_file() else ICON_SOURCE)
    path.write_text(render_entry(wm_class=app_id, icon=icon), encoding="utf-8")
    path.chmod(0o755)
    _install_named_icons(app_id)
    return path


def _install_named_icons(name: str) -> list[Path]:
    """Theme icons keyed by ``name`` so a bar can look the id up like vesktop."""
    written: list[Path] = []
    scalable = icon_dir() / f"{name}.svg"
    icon_dir().mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON_SOURCE, scalable)
    written.append(scalable)
    for size in ICON_PNG_SIZES:
        source = icon_png_source(size)
        if not source.is_file():
            continue
        dest = _data_home() / "icons" / "hicolor" / f"{size}x{size}" / "apps" / f"{name}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        written.append(dest)
    return written


def _owned_chrome_entries() -> list[Path]:
    root = applications_dir()
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.glob("chrome-*-Default.desktop")
        if "Name=TensorWatch" in path.read_text(encoding="utf-8", errors="replace")
    )


def _owned_chrome_icons() -> list[Path]:
    root = _data_home() / "icons" / "hicolor"
    if not root.is_dir():
        return []
    return sorted(root.glob("*/apps/chrome-*-Default.*"))


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
    from .config import load

    notes: list[str] = []
    icon_dir().mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICON_SOURCE, icon_path())
    notes.append(f"installed icon {icon_path()}")
    for size in ICON_PNG_SIZES:
        source = icon_png_source(size)
        if not source.is_file():
            continue
        dest = icon_png_path(size)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        notes.append(f"installed icon {dest}")

    applications_dir().mkdir(parents=True, exist_ok=True)
    entry_path().write_text(render_entry(), encoding="utf-8")
    entry_path().chmod(0o755)
    notes.append(f"installed launcher {entry_path()}")
    alias = install_window_entry(load().dashboard_url)
    notes.append(f"installed launcher {alias}")

    _ok, message = link_launcher()
    notes.append(message)
    _refresh_caches()
    return notes


def uninstall() -> list[str]:
    notes: list[str] = []
    paths = [
        entry_path(),
        icon_path(),
        *(icon_png_path(size) for size in ICON_PNG_SIZES),
        *_owned_chrome_entries(),
        *_owned_chrome_icons(),
    ]
    for path in paths:
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
