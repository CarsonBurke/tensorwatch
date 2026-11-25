"""TensorWatch runs while a window is open, and only then."""

from __future__ import annotations

import http.client
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import wait_until
from tensorwatch import cli, config, service
from tensorwatch.config import Config, ServerSpec
from tensorwatch.supervisor import Supervisor, probe

SRC = Path(__file__).resolve().parents[1] / "src"
#: test_cli's autouse fixture replaces this; keep the real one for these tests.
LIVE_STATE = cli._live_state


# ------------------------------------------------------------------ units


def test_units_start_on_first_use_and_never_at_login(home, monkeypatch):
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    assert cli.main(["install-service", "--no-enable"]) == 0

    unit = service.unit_path().read_text()
    assert "-m tensorwatch serve --exit-when-idle" in unit
    assert "PYTHONPATH=" in unit
    assert "%h/.local/bin" in unit  # systemd --user PATH does not include it by default
    assert "KillMode=mixed" in unit
    # A large logdir needs one descriptor per event file; systemd defaults to 1024.
    assert "LimitNOFILE=65536" in unit
    # Only the socket starts the manager, and an idle exit must not be undone.
    assert "[Install]" not in unit
    assert "Requires=tensorwatch.socket" in unit
    assert "Restart=on-failure" in unit and "Restart=always" not in unit

    sock = service.socket_path().read_text()
    assert "ListenStream=127.0.0.1:6005" in sock
    assert "WantedBy=sockets.target" in sock

    assert cli.main(["uninstall-service"]) == 0
    assert not service.unit_path().exists() and not service.socket_path().exists()


@pytest.mark.parametrize(
    ("host", "listen"),
    [("127.0.0.1", "127.0.0.1:7000"), ("localhost", "127.0.0.1:7000"), ("::1", "[::1]:7000")],
)
def test_socket_listens_where_the_registry_says(tmp_path, host, listen):
    cfg = Config(server=ServerSpec(host=host, port=7000), boards=(), path=tmp_path / "c.toml")
    assert service.listen_address(cfg) == listen


class FakeSystemctl:
    """Records `systemctl --user` calls and answers from a table of unit states."""

    def __init__(self, states: dict[tuple[str, str], str]) -> None:
        self.states = states
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[0] in ("is-enabled", "is-active"):
            return subprocess.CompletedProcess(args, 0, self.states.get(args[:2], "inactive"), "")
        if args[:2] == ("stop", service.UNIT_NAME):
            self.states[("is-active", service.UNIT_NAME)] = "inactive"
        return subprocess.CompletedProcess(args, 0, "", "")


def test_install_moves_a_login_service_onto_the_socket(home, monkeypatch, capsys):
    # The layout an earlier install left: the service enabled at login and running.
    fake = FakeSystemctl({
        ("is-enabled", service.UNIT_NAME): "enabled",
        ("is-active", service.UNIT_NAME): "active",
    })
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(service, "_systemctl", fake)

    assert service.install(config.load()) == 0

    changes = [call for call in fake.calls if call[0] not in ("is-enabled", "is-active")]
    assert changes == [
        # Disabled before the old unit file is replaced, while it still has [Install].
        ("disable", service.UNIT_NAME),
        ("daemon-reload",),
        # The running manager holds the port the socket needs.
        ("stop", service.UNIT_NAME),
        ("enable", "--now", service.SOCKET_NAME),
    ]


def test_reinstalling_leaves_a_socket_started_manager_running(home, monkeypatch):
    fake = FakeSystemctl({
        ("is-enabled", service.UNIT_NAME): "static",
        ("is-active", service.UNIT_NAME): "active",
        ("is-active", service.SOCKET_NAME): "active",
    })
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(service, "_systemctl", fake)

    assert service.install(config.load()) == 0
    assert ("stop", service.UNIT_NAME) not in fake.calls
    assert ("disable", service.UNIT_NAME) not in fake.calls


def test_changing_the_port_moves_a_listening_socket(home, monkeypatch, tmp_path):
    fake = FakeSystemctl({("is-active", service.SOCKET_NAME): "active"})
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(service, "_systemctl", fake)
    assert service.install(config.load()) == 0
    # Same address: daemon-reload is enough, and the running manager stays up.
    assert ("restart", service.SOCKET_NAME) not in fake.calls

    moved = Config(server=ServerSpec(port=7007), boards=(), path=tmp_path / "c.toml")
    assert service.install(moved) == 0
    # A listening socket keeps its old address until it is restarted.
    assert ("restart", service.SOCKET_NAME) in fake.calls
    assert "ListenStream=127.0.0.1:7007" in service.socket_path().read_text()


def test_no_enable_still_stops_the_login_start(home, monkeypatch):
    fake = FakeSystemctl({("is-enabled", service.UNIT_NAME): "enabled"})
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(service, "_systemctl", fake)
    assert service.install(config.load(), enable=False) == 0
    assert ("disable", service.UNIT_NAME) in fake.calls
    assert ("enable", "--now", service.SOCKET_NAME) not in fake.calls


def test_a_socket_systemd_does_not_run_is_not_installed(home, monkeypatch):
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/systemctl")
    service.unit_dir().mkdir(parents=True)
    service.socket_path().write_text(service.render_socket(config.load()))
    # Written with --no-enable, or no user bus to ask (an SSH session).
    for answer in ("disabled", "unknown"):
        monkeypatch.setattr(service, "_query", lambda verb, unit, answer=answer: answer)
        assert not service.installed()
    monkeypatch.setattr(service, "_query", lambda verb, unit: "enabled")
    assert service.installed()


# ------------------------------------------------------- socket activation


def test_listen_socket_takes_the_descriptor_systemd_passed(monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    monkeypatch.setattr(service, "LISTEN_FDS_START", listener.fileno())
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setenv("LISTEN_FDNAMES", "tensorwatch.socket")

    sock = service.listen_socket()
    assert sock is not None
    assert sock.getsockname() == listener.getsockname()
    # Boards are spawned from this process and must not believe they were activated.
    assert not {"LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"} & set(os.environ)
    listener.detach()  # `sock` owns the descriptor now
    sock.close()


def test_listen_socket_ignores_variables_meant_for_another_process(monkeypatch):
    monkeypatch.setenv("LISTEN_PID", str(os.getpid() + 1))
    monkeypatch.setenv("LISTEN_FDS", "1")
    assert service.listen_socket() is None
    assert "LISTEN_FDS" not in os.environ
    monkeypatch.setenv("LISTEN_FDS", "1")  # without LISTEN_PID it is not addressed to anyone
    assert service.listen_socket() is None


# -------------------------------------------------------------- idleness


def test_idle_time_counts_from_the_last_window_or_request(tmp_path):
    cfg = Config(server=ServerSpec(port=0), boards=(), path=tmp_path / "c.toml")
    supervisor = Supervisor(cfg)
    time.sleep(0.05)
    assert supervisor.idle_for() >= 0.05

    channel = supervisor.subscribe()
    time.sleep(0.05)
    assert supervisor.idle_for() == 0.0  # an open window, however long, is not idle
    supervisor.unsubscribe(channel)
    assert supervisor.idle_for() < 0.05

    time.sleep(0.05)
    supervisor.touch()
    assert supervisor.idle_for() < 0.05


# ---------------------------------------------------------------- the CLI


def test_read_only_commands_do_not_start_a_socket_activated_manager(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_socket_owns_port", lambda: True)
    monkeypatch.setattr(service, "manager_active", lambda: False)
    monkeypatch.setattr(cli, "probe", lambda *a: pytest.fail("connecting starts the manager"))
    monkeypatch.setattr(cli, "_api", lambda *a, **k: pytest.fail("connecting starts the manager"))
    monkeypatch.setattr(cli, "_live_state", LIVE_STATE)

    assert cli.main(["status"]) == 1
    assert "not running" in capsys.readouterr().err
    assert cli._notify_reload(config.load()) is False


def test_a_foreground_manager_is_found_while_the_socket_is_stopped(home, monkeypatch):
    service.unit_dir().mkdir(parents=True)
    service.socket_path().write_text("")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(service, "socket_active", lambda: False)
    monkeypatch.setattr(service, "manager_active", lambda: pytest.fail("systemd knows nothing"))
    monkeypatch.setattr(cli, "probe", lambda *a: True)
    assert cli._manager_up(config.load())


def test_serve_leaves_a_listening_socket_alone(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_socket_owns_port", lambda: True)
    monkeypatch.setattr(cli, "probe", lambda *a: pytest.fail("connecting starts the manager"))
    assert cli.main(["serve"]) == 1
    assert "tensorwatch.socket owns the dashboard port" in capsys.readouterr().err


def test_opening_a_board_goes_through_the_dashboard(home, tmp_path, monkeypatch, capsys):
    logdir = tmp_path / "runs"
    logdir.mkdir()
    assert cli.main(["add", str(logdir), "--name", "exp.1"]) == 0
    opened: list[str] = []
    monkeypatch.setattr(cli, "_ensure_running", lambda cfg: True)
    monkeypatch.setattr(cli, "open_window", lambda url, app_mode=True: opened.append(url) or "test")

    assert cli.main(["open", "exp.1"]) == 0
    assert opened == ["http://127.0.0.1:6005/boards/exp.1"]


# ------------------------------------------------------------ end to end


def _registry(home: Path, port: int, board_port: int, fake_tb, idle: float) -> Path:
    logdir = home / "runs"
    logdir.mkdir(exist_ok=True)
    command = ", ".join(f'"{part}"' for part in fake_tb)
    path = home / "config.toml"
    path.write_text(
        f"[server]\nport = {port}\nidle_exit = {idle}\nstart_stagger = 0\npoll_interval = 0.2\n"
        f"[queue]\nenabled = false\n"
        f'[[board]]\nname = "fake"\nlogdir = "{logdir}"\nport = {board_port}\n'
        f"command = [{command}]\n",
        encoding="utf-8",
    )
    return path


def _env(home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "TENSORWATCH_CONFIG": str(home / "config.toml"),
        "TENSORWATCH_STATE_DIR": str(home / "state"),
        "XDG_CONFIG_HOME": str(home / "xdg-config"),
    }


def _open_window(port: int) -> http.client.HTTPResponse:
    """Hold a dashboard stream open, as an open window does.

    The stream is `Connection: close`, so the response owns the socket: keep it
    referenced for as long as the window should count as open.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/api/events")
    response = conn.getresponse()
    assert response.status == 200
    response.readline()  # the first frame has arrived: the window counts
    return response


def test_manager_outlives_no_window_and_exits_after_the_last(home, fake_tb, free_port):
    port, board_port = free_port(), free_port()
    _registry(home, port, board_port, fake_tb, idle=1.5)
    proc = subprocess.Popen(
        [sys.executable, "-m", "tensorwatch", "serve", "--exit-when-idle"],
        env=_env(home), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        assert wait_until(lambda: probe("127.0.0.1", port))
        window = _open_window(port)
        assert wait_until(lambda: probe("127.0.0.1", board_port))
        time.sleep(3.0)  # twice the grace, with a window open
        assert proc.poll() is None

        window.close()
        assert proc.wait(timeout=20) == 0
        assert not probe("127.0.0.1", board_port)  # the board went with it
        assert "no TensorWatch window" in proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(shutil.which("systemd-socket-activate") is None, reason="needs systemd")
def test_socket_activation_starts_the_manager_on_first_connection(home, fake_tb, free_port):
    port, board_port = free_port(), free_port()
    _registry(home, port, board_port, fake_tb, idle=1.0)
    # Like tensorwatch.socket: something else listens, and the first connection
    # hands that socket to a freshly started manager.
    env = _env(home)
    passed = [f"--setenv={key}" for key in (
        "PATH", "PYTHONPATH", "TENSORWATCH_CONFIG", "TENSORWATCH_STATE_DIR", "XDG_CONFIG_HOME",
    )]
    proc = subprocess.Popen(
        ["systemd-socket-activate", "-l", f"127.0.0.1:{port}", *passed,
         sys.executable, "-m", "tensorwatch", "serve", "--exit-when-idle"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        assert wait_until(lambda: probe("127.0.0.1", port))
        window = _open_window(port)
        assert wait_until(lambda: probe("127.0.0.1", board_port))
        window.close()
        assert proc.wait(timeout=20) == 0
        output = proc.stdout.read()
        assert "already listens" not in output
        assert "no TensorWatch window" in output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
