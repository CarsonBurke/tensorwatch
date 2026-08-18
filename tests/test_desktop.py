from __future__ import annotations

import os
import subprocess

import pytest

from tensorwatch import cli, desktop


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    """Isolate the launcher entry, icon and PATH symlink."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("XDG_BIN_HOME", str(tmp_path / "bin"))
    monkeypatch.setenv("TENSORWATCH_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)  # no cache refresh
    return tmp_path


def test_entry_describes_the_app(xdg):
    entry = desktop.render_entry()
    assert "Name=TensorWatch" in entry
    assert "Type=Application" in entry
    assert entry.count("[Desktop Entry]") == 1
    # The launcher opens the dashboard window and starts the service if needed.
    exec_line = next(line for line in entry.splitlines() if line.startswith("Exec="))
    assert exec_line.endswith(" open")
    assert "StartupWMClass=chrome-127.0.0.1__-Default" in entry
    assert "Icon=tensorwatch" in entry
    assert "[Desktop Action Restart]" in entry and "[Desktop Action Registry]" in entry
    assert "Categories=Development;" in entry


def test_chromium_app_id_matches_wayland():
    assert desktop.chrome_app_id("http://127.0.0.1:6005/") == "chrome-127.0.0.1__-Default"
    assert desktop.chrome_app_id("http://127.0.0.1:6100/") == "chrome-127.0.0.1__-Default"

def test_install_places_icon_entry_and_symlink(xdg):
    notes = desktop.install()

    assert desktop.entry_path().is_file()
    assert desktop.icon_path().read_text().startswith("<svg")
    for size in desktop.ICON_PNG_SIZES:
        png = desktop.icon_png_path(size)
        assert png.is_file()
        assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    alias = desktop.chrome_app_entry_path("http://127.0.0.1:6005/")
    assert alias.is_file()
    text = alias.read_text()
    assert "NoDisplay=" not in text
    assert f"Icon={desktop.icon_path()}" in text or f"Icon={desktop.ICON_SOURCE}" in text
    named = desktop.icon_dir() / f"{desktop.chrome_app_id('http://127.0.0.1:6005/')}.svg"
    assert named.is_file() and named.read_text().startswith("<svg")
    launcher = desktop.launcher_path()
    assert launcher.is_symlink()
    assert launcher.readlink() == desktop.checkout_launcher()
    assert os.access(launcher.resolve(), os.X_OK)
    assert desktop.installed() is True
    assert any("launcher" in note for note in notes)

    desktop.install()  # idempotent
    assert desktop.installed() is True


def test_path_symlink_can_import_the_package(xdg):
    desktop.install()
    launcher = desktop.launcher_path()
    result = subprocess.run(
        [str(launcher), "-h"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert "No module named tensorwatch" not in result.stderr


def test_install_leaves_a_foreign_launcher_alone(xdg):
    target = desktop.launcher_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\necho mine\n")

    notes = desktop.install()
    assert target.read_text() == "#!/bin/sh\necho mine\n"
    assert any("not a symlink" in note for note in notes)


def test_uninstall_removes_what_install_added(xdg):
    desktop.install()
    desktop.uninstall()

    assert not desktop.entry_path().exists()
    assert not desktop.icon_path().exists()
    for size in desktop.ICON_PNG_SIZES:
        assert not desktop.icon_png_path(size).exists()
    assert not list(desktop._owned_chrome_entries())
    assert not desktop.launcher_path().exists()
    assert desktop.installed() is False


def test_cli_install_without_service(xdg, capsys, monkeypatch):
    monkeypatch.setattr(cli.service, "install", lambda **kwargs: pytest.fail("service touched"))
    assert cli.main(["install", "--no-service"]) == 0
    assert "TensorWatch installed" in capsys.readouterr().out
    assert desktop.entry_path().is_file()

    assert cli.main(["uninstall", "--keep-service"]) == 0
    assert not desktop.entry_path().exists()


def test_open_window_uses_a_private_chromium_profile(xdg, monkeypatch):
    launched = {}

    def fake_which(name):
        return "/usr/bin/chromium" if name == "chromium" else None

    def fake_popen(argv, **kwargs):
        launched["argv"] = argv
        launched["env"] = kwargs.get("env")
        return None

    monkeypatch.setattr(cli.shutil, "which", fake_which)
    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    assert cli.open_window("http://127.0.0.1:6005/") == "chromium"
    argv = launched["argv"]
    assert argv[0] == "/usr/bin/chromium"
    assert argv[1] == "--app=http://127.0.0.1:6005/"
    assert "--class=tensorwatch" in argv
    assert "--name=TensorWatch" in argv
    assert any(arg.startswith("--user-data-dir=") and "chromium-app" in arg for arg in argv)
    assert launched["env"]["CHROME_DESKTOP"] == "tensorwatch.desktop"


def test_quoting_only_when_needed():
    assert desktop._quote("/usr/bin/tensorwatch") == "/usr/bin/tensorwatch"
    assert desktop._quote("/opt/my apps/tensorwatch") == '"/opt/my apps/tensorwatch"'


def test_interface_has_no_orange_accent():
    """The UI is monochrome plus status colours; no accent chrome."""
    css = (desktop.ICON_SOURCE.parent / "style.css").read_text()
    assert "--accent" not in css
    assert "ff8a3d" not in css.lower()
    assert "ff8a3d" not in desktop.ICON_SOURCE.read_text().lower()
