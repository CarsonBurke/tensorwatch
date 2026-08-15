from __future__ import annotations

import os

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
    # Chromium is launched with --class=tensorwatch; the entry must match so the
    # window groups under this app instead of a generic browser icon.
    assert "StartupWMClass=tensorwatch" in entry
    assert "Icon=tensorwatch" in entry
    assert "[Desktop Action Restart]" in entry and "[Desktop Action Registry]" in entry
    assert "Categories=Development;" in entry


def test_install_places_icon_entry_and_symlink(xdg):
    notes = desktop.install()

    assert desktop.entry_path().is_file()
    assert desktop.icon_path().read_text().startswith("<svg")
    launcher = desktop.launcher_path()
    assert launcher.is_symlink()
    assert launcher.readlink() == desktop.checkout_launcher()
    assert os.access(launcher.resolve(), os.X_OK)
    assert desktop.installed() is True
    assert any("launcher" in note for note in notes)

    desktop.install()  # idempotent
    assert desktop.installed() is True


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
    assert not desktop.launcher_path().exists()
    assert desktop.installed() is False


def test_cli_install_without_service(xdg, capsys, monkeypatch):
    monkeypatch.setattr(cli.service, "install", lambda **kwargs: pytest.fail("service touched"))
    assert cli.main(["install", "--no-service"]) == 0
    assert "TensorWatch installed" in capsys.readouterr().out
    assert desktop.entry_path().is_file()

    assert cli.main(["uninstall", "--keep-service"]) == 0
    assert not desktop.entry_path().exists()


def test_quoting_only_when_needed():
    assert desktop._quote("/usr/bin/tensorwatch") == "/usr/bin/tensorwatch"
    assert desktop._quote("/opt/my apps/tensorwatch") == '"/opt/my apps/tensorwatch"'


def test_interface_has_no_orange_accent():
    """The UI is monochrome plus status colours; no accent chrome."""
    css = (desktop.ICON_SOURCE.parent / "style.css").read_text()
    assert "--accent" not in css
    assert "ff8a3d" not in css.lower()
    assert "ff8a3d" not in desktop.ICON_SOURCE.read_text().lower()
