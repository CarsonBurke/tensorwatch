from __future__ import annotations

from pathlib import Path

import pytest

from tensorwatch import config


def test_parses_boards_and_applies_defaults(tmp_path):
    text = f"""
    [server]
    port = 6100
    port_base = 6200
    keep_warm = 3

    [defaults]
    reload_interval = 120
    autostart = "on_demand"

    [[board]]
    name = "cleanrl"
    logdir = "{tmp_path}/cleanrl/runs"
    port = 6201

    [[board]]
    logdir = "{tmp_path}/golf/tb_logs"
    autostart = "always"
    """
    cfg = config.parse(text)

    assert cfg.server.port == 6100
    assert cfg.server.keep_warm == 3
    first, second = cfg.boards
    assert (first.name, first.port, first.autostart) == ("cleanrl", 6201, "on_demand")
    assert first.reload_interval == 120.0
    # Name falls back to the logdir basename, port to the next free slot.
    assert (second.name, second.autostart) == ("tb_logs", "always")
    assert second.port == 6200
    assert cfg.assigned_ports == {"tb_logs": 6200}
    assert cfg.board("cleanrl") is first


def test_argv_carries_performance_flags(tmp_path):
    text = f"""
    [[board]]
    name = "golf"
    logdir = "{tmp_path}"
    port = 6010
    reload_interval = 90
    samples_per_plugin = "scalars=2000,images=0"
    args = ["--load_fast=true"]
    """
    board = config.parse(text).boards[0]
    argv = board.argv()

    assert argv[0] == "tensorboard"
    assert argv[argv.index("--logdir") + 1] == str(tmp_path)
    assert argv[argv.index("--port") + 1] == "6010"
    assert argv[argv.index("--reload_interval") + 1] == "90"
    assert argv[argv.index("--samples_per_plugin") + 1] == "scalars=2000,images=0"
    assert argv[argv.index("--window_title") + 1] == "TB: golf"
    assert argv[-1] == "--load_fast=true"
    assert board.url == "http://127.0.0.1:6010/"


def test_expands_user_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MYRUNS", str(tmp_path / "runs"))
    cfg = config.parse('[[board]]\nname = "a"\nlogdir = "$MYRUNS"\nport = 6010\n')
    assert cfg.boards[0].logdir == tmp_path / "runs"

    cfg = config.parse('[[board]]\nname = "b"\nlogdir = "~/somewhere"\nport = 6011\n')
    assert cfg.boards[0].logdir == Path.home() / "somewhere"


def test_auto_ports_skip_taken_and_unsafe(tmp_path):
    text = """
    [server]
    port = 6010
    port_base = 5999

    [[board]]
    name = "a"

    [[board]]
    name = "b"
    port = 6001

    [[board]]
    name = "c"
    """
    with pytest.raises(config.ConfigError):
        config.parse(text)  # boards a and c have no logdir

    text = text.replace('name = "a"', 'name = "a"\nlogdir = "/tmp"')
    text = text.replace('name = "b"', 'name = "b"\nlogdir = "/tmp"')
    text = text.replace('name = "c"', 'name = "c"\nlogdir = "/tmp"')
    cfg = config.parse(text)
    ports = [board.port for board in cfg.boards]
    # 5999 is free, 6000 is browser-blocked, 6001 is claimed, 6010 is the server.
    assert ports == [5999, 6001, 6002]


@pytest.mark.parametrize(
    "text, needle",
    [
        ('[[board]]\nname = "a"\n', "exactly one of logdir"),
        ('[[board]]\nname = "a"\nlogdir = "/tmp"\nlogdir_spec = "x:/tmp"\n', "exactly one of"),
        ('[[board]]\nname = "a b"\nlogdir = "/tmp"\n', "invalid name"),
        ('[[board]]\nname = "a"\nlogdir = "/tmp"\nautostart = "sometimes"\n', "autostart must be"),
        ('[[board]]\nname = "a"\nlogdir = "/tmp"\nport = 6000\n', "blocked by browsers"),
        ('[[board]]\nname = "a"\nlogdir = "/tmp"\nreload_interval = "fast"\n', "expected number"),
        ('[[board]]\nname = "a"\nlogdir = "/tmp"\nargs = "--x"\n', "list of strings"),
        ('[[board]]\nname = "a"\nlogdir = "/tmp"\nnope = 1\n', "unknown key"),
        ('[boards]\nname = "a"\n', "unknown top-level"),
        ('[[board]]\nname = "a"\nlogdir = "/a"\n[[board]]\nname = "a"\nlogdir = "/b"\n', "duplicate"),
    ],
)
def test_rejects_bad_config(text, needle):
    with pytest.raises(config.ConfigError) as excinfo:
        config.parse(text)
    assert needle in str(excinfo.value)


def test_duplicate_ports_are_reported():
    text = """
    [[board]]
    name = "a"
    logdir = "/tmp"
    port = 6010

    [[board]]
    name = "b"
    logdir = "/tmp"
    port = 6010
    """
    with pytest.raises(config.ConfigError, match="more than one board"):
        config.parse(text)


def test_paths_follow_environment(home):
    assert config.config_path() == home / "config.toml"
    assert config.state_dir() == home / "state"
    assert config.log_dir() == home / "state" / "logs"

    cfg = config.load()
    assert cfg.boards == ()  # missing file is an empty registry, not an error


def test_logdir_spec_board():
    cfg = config.parse('[[board]]\nname = "multi"\nlogdir_spec = "a:/x,b:/y"\nport = 6010\n')
    board = cfg.boards[0]
    assert board.argv()[1:3] == ["--logdir_spec", "a:/x,b:/y"]
    assert board.target == "a:/x,b:/y"



def test_non_loopback_binds_need_explicit_opt_in():
    board = '[[board]]\nname = "a"\nlogdir = "/tmp"\nport = 6100\nhost = "0.0.0.0"\n'
    with pytest.raises(config.ConfigError, match="would listen outside this machine"):
        config.parse(board)

    bind_all = '[[board]]\nname = "a"\nlogdir = "/tmp"\nport = 6100\nargs = ["--bind_all"]\n'
    with pytest.raises(config.ConfigError, match="--bind_all"):
        config.parse(bind_all)

    with pytest.raises(config.ConfigError, match="exposes the unauthenticated dashboard"):
        config.parse('[server]\nhost = "0.0.0.0"\n')

    allowed = config.parse("[server]\nallow_remote = true\nhost = \"0.0.0.0\"\n" + board)
    assert allowed.server.host == "0.0.0.0"
    assert allowed.boards[0].exposed is True
    # The link still points at loopback so it is clickable from this machine.
    assert allowed.boards[0].url == "http://127.0.0.1:6100/"
    assert config.parse('[[board]]\nname = "a"\nlogdir = "/tmp"\nport = 6100\n').boards[0].exposed is False