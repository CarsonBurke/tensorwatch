from __future__ import annotations

from pathlib import Path

import pytest

from tensorwatch import cli, config, discover, service


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


def test_set_autostart_persists_and_rejects_junk(tmp_path):
    logdir = tmp_path / "runs"
    logdir.mkdir()
    cli.main(["add", str(logdir), "--name", "b"])

    cli.set_autostart("b", "on_demand")
    assert config.load().board("b").autostart == "on_demand"
    cli.set_autostart("b", "always")
    assert config.load().board("b").autostart == "always"

    with pytest.raises(cli.registry.RegistryError):
        cli.set_autostart("b", "whenever")
    with pytest.raises(cli.registry.RegistryError):
        cli.set_autostart("nope", "on_demand")
    assert config.load().board("b").autostart == "always"


def test_list_reports_offline_manager(tmp_path, capsys):
    logdir = tmp_path / "runs"
    logdir.mkdir()
    cli.main(["add", str(logdir), "--name", "c"])
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "BOARD" in out and "c" in out
    assert "manager not running" in out


def test_names_are_qualified_by_their_repository(tmp_path):
    """A bare leaf like `postraining` says nothing; name it after its repo too."""
    repo = tmp_path / "parameter-golf"
    (repo / ".git").mkdir(parents=True)
    (repo / "tb_logs").mkdir()
    (repo / "postraining" / "runs").mkdir(parents=True)
    (repo / "samples" / "rl" / "runs").mkdir(parents=True)

    assert discover.derive_name(repo / "tb_logs") == "parameter-golf"
    assert discover.derive_name(repo / "postraining" / "runs") == "parameter-golf-postraining"
    assert discover.derive_name(repo / "samples" / "rl" / "runs") == "parameter-golf-rl"
    # Outside a repository there is nothing to qualify with.
    plain = tmp_path / "loose" / "experiment" / "runs"
    plain.mkdir(parents=True)
    assert discover.derive_name(plain) == "experiment"


def test_add_uses_the_qualified_name(tmp_path, capsys):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    logdir = repo / "stage2" / "runs"
    logdir.mkdir(parents=True)

    assert cli.main(["add", str(logdir)]) == 0
    assert config.load().board("proj-stage2") is not None


def test_scan_finds_run_roots(tmp_path):
    make_runs(
        tmp_path,
        "projA/runs/exp1/events.out.tfevents.1.host",
        "projA/runs/exp2/events.out.tfevents.2.host",
        "projB/tb_logs/run-1/events.out.tfevents.3.host",
        "projC/experiment/events.out.tfevents.4.host",
        "projD/README.md",
    )
    found = discover.discover(tmp_path, max_depth=4)
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
    assert "ExecStart=" in unit and "-m tensorwatch serve" in unit
    assert "PYTHONPATH=" in unit
    assert "%h/.local/bin" in unit  # systemd --user PATH does not include it by default
    assert "WantedBy=default.target" in unit
    assert "KillMode=mixed" in unit
    # A large logdir needs one descriptor per event file; systemd defaults to 1024.
    assert "LimitNOFILE=65536" in unit

    assert cli.main(["uninstall-service"]) == 0
    assert not service.unit_path().exists()


def test_add_refuses_a_logdir_that_is_already_a_board(tmp_path, capsys):
    logdir = tmp_path / "proj" / "runs"
    logdir.mkdir(parents=True)
    assert cli.main(["add", str(logdir)]) == 0
    capsys.readouterr()

    # Under another name it would still be a second TensorBoard on the same data.
    assert cli.main(["add", str(logdir), "--name", "again"]) == 1
    assert "already registered as 'proj'" in capsys.readouterr().err
    link = tmp_path / "link"
    link.symlink_to(logdir)
    assert cli.main(["add", str(link)]) == 1
    assert [board.name for board in config.load().boards] == ["proj"]


def test_add_names_a_second_logdir_in_the_same_repository(tmp_path, capsys):
    (tmp_path / "cleanrl" / ".git").mkdir(parents=True)
    for leaf in ("runs", "runs_old"):
        (tmp_path / "cleanrl" / leaf).mkdir()
        assert cli.main(["add", str(tmp_path / "cleanrl" / leaf)]) == 0
    assert [board.name for board in config.load().boards] == ["cleanrl", "cleanrl-runs_old"]


def test_add_takes_several_logdirs_and_reports_each_failure(tmp_path, capsys):
    make_runs(tmp_path, "a/runs/x/events.out.tfevents.1")
    (tmp_path / "b" / "runs").mkdir(parents=True)
    code = cli.main(["add", str(tmp_path / "a/runs"), str(tmp_path / "nope"), str(tmp_path / "b/runs")])
    captured = capsys.readouterr()
    assert code == 1
    assert [board.name for board in config.load().boards] == ["a", "b"]
    assert "nope does not exist" in captured.err
    # An empty logdir is allowed, and said out loud.
    assert captured.err.count("no event files there yet") == 1

    assert cli.main(["add", str(tmp_path / "a"), str(tmp_path / "b"), "--name", "x"]) == 1
    assert "one board" in capsys.readouterr().err


def test_add_rejects_a_file(tmp_path, capsys):
    target = tmp_path / "notes.txt"
    target.write_text("x")
    assert cli.main(["add", str(target)]) == 1
    assert "is not a directory" in capsys.readouterr().err


def test_rm_takes_names_and_logdirs(tmp_path, capsys):
    for name in ("a", "b", "c"):
        (tmp_path / name / "runs").mkdir(parents=True)
        assert cli.main(["add", str(tmp_path / name / "runs")]) == 0
    capsys.readouterr()

    assert cli.main(["rm", "a", str(tmp_path / "b" / "runs"), "ghost"]) == 1
    captured = capsys.readouterr()
    assert "removed a" in captured.out and "removed b" in captured.out
    assert "no board named 'ghost'" in captured.err
    assert [board.name for board in config.load().boards] == ["c"]


def three_boards(tmp_path) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name / "runs").mkdir(parents=True)
        assert cli.main(["add", str(tmp_path / name / "runs")]) == 0


def ops_for(reloads: list[int] | None = None) -> cli.RegistryOps:
    return cli.RegistryOps(
        config.config_path(),
        lambda: reloads.append(1) if reloads is not None else None,
        config.load,
    )


def test_remove_then_undo_keeps_the_board_as_it_was(tmp_path):
    three_boards(tmp_path)
    assert cli.main(["set", "b", "autostart=on_demand", "reload_interval=300"]) == 0
    before = config.config_path().read_text()
    reloads: list[int] = []
    ops = ops_for(reloads)

    removed = ops.remove("b")
    assert set(removed) == {"undo"}  # a token, never the registry text
    assert [board.name for board in config.load().boards] == ["a", "c"]
    assert ops.restore(removed["undo"]) == {"ok": True, "board": "b"}
    assert config.config_path().read_text() == before
    assert len(reloads) == 2

    # A token works once, and an invented one never.
    for token in (removed["undo"], "made-up"):
        with pytest.raises(cli.registry.RegistryError, match="no longer be undone"):
            ops.restore(token)


def test_undoing_several_removals_in_any_order_rebuilds_the_order(tmp_path):
    import itertools

    three_boards(tmp_path)
    before = config.config_path().read_text()
    for removal in itertools.permutations("abc"):
        for undo in itertools.permutations("abc"):
            ops = ops_for()
            tokens = {name: ops.remove(name)["undo"] for name in removal}
            assert config.load().boards == ()
            for name in undo:
                ops.restore(tokens[name])
            assert [board.name for board in config.load().boards] == ["a", "b", "c"], (removal, undo)
            config.config_path().write_text(before)


def test_an_undo_expires(tmp_path, monkeypatch):
    three_boards(tmp_path)
    ops = ops_for()
    token = ops.remove("a")["undo"]
    clock = cli.time.monotonic()
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock + cli.RegistryOps.UNDO_TTL + 1)
    with pytest.raises(cli.registry.RegistryError, match="no longer be undone"):
        ops.restore(token)
    assert [board.name for board in config.load().boards] == ["b", "c"]


def test_undo_refuses_a_port_that_was_taken_meanwhile(tmp_path):
    three_boards(tmp_path)
    ops = ops_for()
    token = ops.remove("b")["undo"]
    (tmp_path / "d" / "runs").mkdir(parents=True)
    port = config.DEFAULT_PORT_BASE + 1
    assert cli.main(["add", str(tmp_path / "d" / "runs"), "--port", str(port)]) == 0

    with pytest.raises(config.ConfigError, match="more than one board"):
        ops.restore(token)
    assert [board.name for board in config.load().boards] == ["a", "c", "d"]


def test_ops_add_writes_the_registry_it_was_given(tmp_path, monkeypatch):
    # `serve --config X`: edits must land in X, not in the default registry.
    other = tmp_path / "other.toml"
    (tmp_path / "proj" / "runs").mkdir(parents=True)
    ops = cli.RegistryOps(other, lambda: None, lambda: config.load(other))
    assert ops.add(str(tmp_path / "proj" / "runs"), autostart="manual")["board"] == "proj"
    assert config.load(other).board("proj").autostart == "manual"
    assert not config.config_path().exists()


def test_concurrent_adds_do_not_lose_a_board_or_share_a_port(tmp_path):
    import threading

    for index in range(8):
        (tmp_path / f"p{index}" / "runs").mkdir(parents=True)
    ops = ops_for()
    threads = [
        threading.Thread(target=ops.add, args=(str(tmp_path / f"p{index}" / "runs"),))
        for index in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    boards = config.load().boards
    assert sorted(board.name for board in boards) == [f"p{index}" for index in range(8)]
    assert len({board.port for board in boards}) == 8


def test_suggestions_are_cached_until_the_registry_changes(tmp_path, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(cli, "suggest_boards", lambda cfg: calls.append(1) or [{"n": len(calls)}])
    ops = ops_for()
    assert ops.suggest() == ops.suggest() == [{"n": 1}]
    ops.forget_suggestions()
    assert ops.suggest() == [{"n": 2}]

    # A change that lands while a walk is running must not be cached over.
    def slow(cfg):
        ops.forget_suggestions()
        return [{"stale": True}]

    monkeypatch.setattr(cli, "suggest_boards", slow)
    ops.forget_suggestions()
    assert ops.suggest() == [{"stale": True}]
    monkeypatch.setattr(cli, "suggest_boards", lambda cfg: [{"fresh": True}])
    assert ops.suggest() == [{"fresh": True}]


def test_scan_reports_a_registered_logdir_under_its_real_name(tmp_path, capsys):
    make_runs(tmp_path, "cleanrl/runs_old/a/events.out.tfevents.1")
    assert cli.main(["add", str(tmp_path / "cleanrl" / "runs_old"), "--name", "archive"]) == 0
    capsys.readouterr()
    assert cli.main(["scan", str(tmp_path)]) == 0
    row = next(line for line in capsys.readouterr().out.splitlines() if "runs_old" in line)
    assert row.split()[:2] == ["archive", "registered"]


def test_suggestions_skip_what_is_registered_and_name_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ws = tmp_path / "ws"
    for repo in ("one", "two"):
        (ws / repo / ".git").mkdir(parents=True)
    make_runs(ws, "one/runs/a/events.out.tfevents.1", "one/runs/b/events.out.tfevents.1",
              "one/runs_old/a/events.out.tfevents.1", "two/tb_logs/a/events.out.tfevents.1",
              "two/tb_logs/b/events.out.tfevents.1", "two/tb_logs/c/events.out.tfevents.1")
    assert cli.main(["add", str(ws / "one" / "runs")]) == 0

    assert cli.suggest_boards(config.load()) == [
        {"path": str(ws / "two" / "tb_logs"), "name": "two", "events": 3},
        {"path": str(ws / "one" / "runs_old"), "name": "one-runs_old", "events": 1},
    ]
