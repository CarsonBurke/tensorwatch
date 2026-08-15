from __future__ import annotations

from pathlib import Path

import pytest

from tbmgr import cli, config, service


@pytest.fixture(autouse=True)
def offline(home, monkeypatch):
    """No CLI test may talk to (or wake) a real manager."""
    monkeypatch.setattr(cli, "_notify_reload", lambda cfg: False)
    monkeypatch.setattr(cli, "_live_state", lambda cfg: None)
    return home


def make_runs(root: Path, *relative: str) -> Path:
    for item in relative:
        target = root / item
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00")
    return root


def test_add_derives_name_and_port(tmp_path, capsys):
    logdir = tmp_path / "parameter-golf" / "tb_logs"
    logdir.mkdir(parents=True)

    assert cli.main(["add", str(logdir)]) == 0
    out = capsys.readouterr().out
    assert "registered parameter-golf" in out

    board = config.load().board("parameter-golf")
    assert board is not None
    assert board.logdir == logdir.resolve()
    assert board.port == config.DEFAULT_PORT_BASE
    assert board.autostart == "always"

    # A second board takes the next port.
    other = tmp_path / "cleanrl" / "runs"
    other.mkdir(parents=True)
    assert cli.main(["add", str(other), "--autostart", "on_demand"]) == 0
    assert config.load().board("cleanrl").port == config.DEFAULT_PORT_BASE + 1
    assert config.load().board("cleanrl").autostart == "on_demand"


def test_add_rejects_duplicates_and_missing_paths(tmp_path, capsys):
    logdir = tmp_path / "runs"
    logdir.mkdir()
    assert cli.main(["add", str(logdir), "--name", "a"]) == 0
    assert cli.main(["add", str(logdir), "--name", "a"]) == 1
    assert "already registered" in capsys.readouterr().err

    assert cli.main(["add", str(tmp_path / "nope")]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert cli.main(["add", str(tmp_path / "nope"), "--name", "forced", "--force"]) == 0


def test_add_passes_through_performance_flags(tmp_path):
    logdir = tmp_path / "runs"
    logdir.mkdir()
    assert cli.main([
        "add", str(logdir), "--name", "big",
        "--samples-per-plugin", "scalars=2000,images=0",
        "--reload-interval", "300",
        # argparse needs `=` here: the value itself starts with dashes.
        "--arg=--load_fast=true",
        "--description", "the huge one",
    ]) == 0
    board = config.load().board("big")
    assert board.samples_per_plugin == "scalars=2000,images=0"
    assert board.reload_interval == 300.0
    assert board.args == ("--load_fast=true",)
    assert board.description == "the huge one"
    assert "--samples_per_plugin" in board.argv()


def test_set_enable_disable_and_rm(tmp_path, capsys):
    logdir = tmp_path / "runs"
    logdir.mkdir()
    cli.main(["add", str(logdir), "--name", "b"])

    assert cli.main(["set", "b", "autostart=on_demand", "idle_timeout=120"]) == 0
    board = config.load().board("b")
    assert board.autostart == "on_demand" and board.idle_timeout == 120.0

    assert cli.main(["disable", "b"]) == 0
    assert config.load().board("b").enabled is False
    assert cli.main(["enable", "b"]) == 0
    assert config.load().board("b").enabled is True

    assert cli.main(["set", "b", "autostart=whenever"]) == 1
    assert "autostart must be" in capsys.readouterr().err
    # A rejected edit is never written: the registry stays loadable.
    assert config.load().board("b").autostart == "on_demand"

    assert cli.main(["rm", "b"]) == 0
    assert config.load().boards == ()
    assert cli.main(["rm", "b"]) == 1


def test_list_reports_offline_manager(tmp_path, capsys):
    logdir = tmp_path / "runs"
    logdir.mkdir()
    cli.main(["add", str(logdir), "--name", "c"])
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "BOARD" in out and "c" in out
    assert "manager not running" in out


def test_scan_finds_run_roots(tmp_path):
    make_runs(
        tmp_path,
        "projA/runs/exp1/events.out.tfevents.1.host",
        "projA/runs/exp2/events.out.tfevents.2.host",
        "projB/tb_logs/run-1/events.out.tfevents.3.host",
        "projC/experiment/events.out.tfevents.4.host",
        "projD/README.md",
    )
    found = cli._discover(tmp_path, max_depth=4)
    names = {path.relative_to(tmp_path).as_posix() for path in found}
    assert names == {"projA/runs", "projB/tb_logs", "projC/experiment"}


def test_scan_add_registers_new_boards(tmp_path, capsys):
    make_runs(
        tmp_path,
        "projA/runs/exp1/events.out.tfevents.1.host",
        "projB/tb_logs/run-1/events.out.tfevents.2.host",
    )
    assert cli.main(["scan", str(tmp_path), "--add"]) == 0
    cfg = config.load()
    assert sorted(board.name for board in cfg.boards) == ["projA", "projB"]
    assert {board.port for board in cfg.boards} == {
        config.DEFAULT_PORT_BASE,
        config.DEFAULT_PORT_BASE + 1,
    }

    # Re-scanning is idempotent: known logdirs are reported, not duplicated.
    assert cli.main(["scan", str(tmp_path), "--add"]) == 0
    assert "registered" in capsys.readouterr().out
    assert len(config.load().boards) == 2


def test_doctor_flags_missing_logdir(tmp_path, capsys):
    logdir = tmp_path / "runs"
    logdir.mkdir()
    cli.main(["add", str(logdir), "--name", "d"])
    logdir.rmdir()

    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "logdir missing" in out
    assert "problem(s) found" in out


def test_doctor_is_clean_for_a_real_logdir(tmp_path, capsys):
    make_runs(tmp_path, "proj/runs/exp/events.out.tfevents.1.host")
    cli.main(["add", str(tmp_path / "proj" / "runs"), "--name", "e", "--command", "python3"])
    assert cli.main(["doctor"]) == 0
    assert "no problems found" in capsys.readouterr().out


def test_service_unit_is_self_contained(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service.shutil, "which", lambda name: None)

    assert cli.main(["install-service", "--no-enable"]) == 0
    unit = service.unit_path().read_text()
    assert "ExecStart=" in unit and "-m tbmgr serve" in unit
    assert "PYTHONPATH=" in unit
    assert "%h/.local/bin" in unit  # systemd --user PATH does not include it by default
    assert "WantedBy=default.target" in unit
    assert "KillMode=mixed" in unit

    assert cli.main(["uninstall-service"]) == 0
    assert not service.unit_path().exists()
