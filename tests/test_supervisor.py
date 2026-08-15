from __future__ import annotations

import socket
from pathlib import Path
import time

import pytest

from conftest import wait_until
from tensorwatch import procstats
from tensorwatch.config import BoardSpec, Config, ServerSpec
from tensorwatch.supervisor import Supervisor, probe


def build(tmp_path, fake_tb, free_port, **board_kwargs) -> Config:
    logdir = tmp_path / "runs"
    logdir.mkdir(exist_ok=True)
    board = BoardSpec(
        name=board_kwargs.pop("name", "fake"),
        port=board_kwargs.pop("port", free_port()),
        logdir=logdir,
        command=tuple(fake_tb),
        start_timeout=board_kwargs.pop("start_timeout", 10.0),
        **board_kwargs,
    )
    server = ServerSpec(port=free_port(), poll_interval=0.1, start_stagger=0.0, max_warming=4)
    return Config(server=server, boards=(board,), path=tmp_path / "config.toml")


@pytest.fixture
def supervisor(home):
    created: list[Supervisor] = []

    def make(config: Config) -> Supervisor:
        sup = Supervisor(config)
        created.append(sup)
        sup.start()
        return sup

    yield make
    for sup in created:
        sup.shutdown()


def state_of(sup: Supervisor, name: str = "fake") -> str:
    for status in sup.snapshot():
        if status.name == name:
            return status.state
    return "missing"


def test_always_board_starts_and_stops_on_request(tmp_path, fake_tb, free_port, supervisor):
    cfg = build(tmp_path, fake_tb, free_port)
    sup = supervisor(cfg)
    board = cfg.boards[0]

    assert wait_until(lambda: state_of(sup) == "running"), sup.snapshot()
    status = sup.snapshot()[0]
    assert status.pid and status.port == board.port
    assert probe("127.0.0.1", board.port)
    assert "start:" in sup.log_tail("fake")

    sup.request("stop", "fake")
    assert wait_until(lambda: state_of(sup) == "stopped"), sup.snapshot()
    assert wait_until(lambda: not probe("127.0.0.1", board.port))
    assert sup.snapshot()[0].pid is None



def test_logs_are_private(tmp_path, fake_tb, free_port, supervisor):
    sup = supervisor(build(tmp_path, fake_tb, free_port))
    assert wait_until(lambda: state_of(sup) == "running")
    log = Path(sup.snapshot()[0].log_path)
    assert log.stat().st_mode & 0o777 == 0o600
    assert log.parent.stat().st_mode & 0o777 == 0o700

def test_resource_sampling_reports_memory(tmp_path, fake_tb, free_port, supervisor):
    sup = supervisor(build(tmp_path, fake_tb, free_port))
    assert wait_until(lambda: state_of(sup) == "running")
    status = wait_until(lambda: sup.snapshot()[0].rss_bytes and sup.snapshot()[0], timeout=20)
    assert status and status.rss_bytes > 1_000_000  # a python process is at least this big


def test_crash_lands_in_backoff_with_exit_code(tmp_path, fake_tb, free_port, supervisor):
    cfg = build(tmp_path, fake_tb, free_port, env={"FAKE_TB_EXIT": "3"})
    sup = supervisor(cfg)

    assert wait_until(lambda: state_of(sup) == "backoff"), sup.snapshot()
    status = sup.snapshot()[0]
    assert status.last_exit == 3
    assert status.restarts >= 1
    assert "exited with code 3" in status.message
    # The failure reason from the child's stderr is surfaced, not swallowed.
    assert "failing on purpose" in status.message
    assert "failing on purpose" in sup.log_tail("fake")


def test_start_timeout_kills_a_hung_board(tmp_path, fake_tb, free_port, supervisor):
    cfg = build(
        tmp_path, fake_tb, free_port, env={"FAKE_TB_DELAY": "60"}, start_timeout=1.0
    )
    sup = supervisor(cfg)

    assert wait_until(lambda: state_of(sup) in ("failed", "backoff"), timeout=20), sup.snapshot()
    assert "timed out" in sup.snapshot()[0].message


def test_on_demand_board_starts_on_demand_and_stops_when_idle(
    tmp_path, fake_tb, free_port, supervisor
):
    cfg = build(tmp_path, fake_tb, free_port, autostart="on_demand", idle_timeout=3.0)
    sup = supervisor(cfg)
    time.sleep(0.4)
    assert state_of(sup) == "stopped"

    sup.request("demand", "fake")
    assert wait_until(lambda: state_of(sup) == "running"), sup.snapshot()
    # No further demand arrives, so the idle timeout must reclaim it.
    assert wait_until(lambda: state_of(sup) == "stopped", timeout=20), sup.snapshot()
    assert "idle" in sup.snapshot()[0].message


def test_manual_board_only_starts_when_asked(tmp_path, fake_tb, free_port, supervisor):
    sup = supervisor(build(tmp_path, fake_tb, free_port, autostart="manual"))
    time.sleep(0.4)
    assert state_of(sup) == "stopped"

    sup.request("start", "fake")
    assert wait_until(lambda: state_of(sup) == "running"), sup.snapshot()



def test_restart_keeps_on_demand_policy(tmp_path, fake_tb, free_port, supervisor):
    cfg = build(tmp_path, fake_tb, free_port, autostart="on_demand", idle_timeout=3.0)
    sup = supervisor(cfg)
    sup.request("demand", "fake")
    assert wait_until(lambda: state_of(sup) == "running"), sup.snapshot()

    sup.request("restart", "fake")
    assert wait_until(lambda: state_of(sup) == "running", timeout=20), sup.snapshot()
    # A restart must not pin the board running: idle stop still applies.
    assert wait_until(lambda: state_of(sup) == "stopped", timeout=25), sup.snapshot()


def test_unhealthy_board_backs_off_instead_of_looping(tmp_path, fake_tb, free_port, supervisor):
    """A process that holds the port open but stops accepting must not hot-loop."""
    cfg = build(tmp_path, fake_tb, free_port)
    sup = supervisor(cfg)
    assert wait_until(lambda: state_of(sup) == "running")

    # Make every probe fail while the child stays alive.
    import tensorwatch.supervisor as supervisor_module

    original = supervisor_module.probe
    supervisor_module.probe = lambda host, port, timeout=0.25: False
    try:
        status = wait_until(
            lambda: next((s for s in sup.snapshot() if s.restarts >= 1), None), timeout=20
        )
        assert status is not None, sup.snapshot()
        assert "stopped accepting" in status.message
    finally:
        supervisor_module.probe = original


def test_disabled_board_is_never_started(tmp_path, fake_tb, free_port, supervisor):
    cfg = build(tmp_path, fake_tb, free_port, enabled=False)
    sup = supervisor(cfg)
    time.sleep(0.4)
    assert state_of(sup) == "disabled"
    assert not probe("127.0.0.1", cfg.boards[0].port)


def test_occupied_port_fails_fast_instead_of_spinning(tmp_path, fake_tb, free_port, supervisor):
    port = free_port()
    with socket.socket() as squatter:
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("127.0.0.1", port))
        squatter.listen(1)

        sup = supervisor(build(tmp_path, fake_tb, free_port, port=port))
        assert wait_until(lambda: state_of(sup) == "failed"), sup.snapshot()
        status = sup.snapshot()[0]
        assert "already in use" in status.message
        assert status.restarts == 0  # no restart storm


def test_missing_logdir_is_reported(tmp_path, fake_tb, free_port, supervisor):
    cfg = build(tmp_path, fake_tb, free_port)
    (tmp_path / "runs").rmdir()
    sup = supervisor(cfg)
    assert wait_until(lambda: state_of(sup) == "failed"), sup.snapshot()
    assert "logdir does not exist" in sup.snapshot()[0].message


def test_reload_applies_registry_changes(tmp_path, fake_tb, free_port, supervisor):
    cfg = build(tmp_path, fake_tb, free_port)
    sup = supervisor(cfg)
    assert wait_until(lambda: state_of(sup) == "running")

    second = BoardSpec(
        name="second",
        port=free_port(),
        logdir=tmp_path / "runs",
        command=tuple(fake_tb),
    )
    sup.reload(Config(server=cfg.server, boards=(second,), path=cfg.path))

    assert wait_until(lambda: state_of(sup, "second") == "running", timeout=20), sup.snapshot()
    assert [status.name for status in sup.snapshot()] == ["second"]
    assert wait_until(lambda: not probe("127.0.0.1", cfg.boards[0].port))


def test_subscribers_receive_snapshots(tmp_path, fake_tb, free_port, supervisor):
    sup = supervisor(build(tmp_path, fake_tb, free_port))
    channel = sup.subscribe()
    payloads = [channel.get(timeout=15) for _ in range(2)]
    assert all('"boards"' in payload for payload in payloads)
    sup.unsubscribe(channel)


def test_procstats_sees_our_own_group():
    import os

    sample = procstats.sample([os.getpgrp()])
    assert sample[os.getpgrp()].rss_bytes > 0
    assert sample[os.getpgrp()].processes >= 1
    assert procstats.sample([]) == {}
