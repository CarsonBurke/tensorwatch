"""Remote machines: the SSH link's lifecycle, presence, and how it fails."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import time
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tensorwatch import cli, config, httpd, remote
from tensorwatch.config import BoardSpec, Config, RemoteSpec, ServerSpec
from tensorwatch.supervisor import API_VERSION, Supervisor

#: Stands in for OpenSSH. ``-N`` runs a master that listens on its ControlPath
#: and serves ``-O forward``/``-O cancel`` the way OpenSSH's multiplexer does
#: (including answering a repeated forward "ok" without binding again); any
#: other invocation runs a "remote command" whose outcome the test's plan says.
FAKE_SSH = r'''
import json, os, socket, sys, threading, time

argv = sys.argv[1:]
with open(os.environ["FAKE_SSH_LOG"], "a") as log:
    log.write(json.dumps({"argv": argv, "pid": os.getpid()}) + "\n")
with open(os.environ["FAKE_SSH_PLAN"]) as handle:
    plan = json.load(handle)

options, rest, i = {}, [], 0
while i < len(argv):
    if argv[i] == "-o":
        key, _, value = argv[i + 1].partition("=")
        options[key] = value
        i += 2
    else:
        rest.append(argv[i])
        i += 1
assert options.get("BatchMode") == "yes", "the link must never prompt"
control = options["ControlPath"]


def finish(outcome):
    sys.stderr.write(outcome.get("stderr", ""))
    sys.stdout.write(outcome.get("stdout", ""))
    sys.exit(outcome.get("code", 0))


def relay(a, b):
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def serve_forward(listener, host, port):
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        upstream = socket.create_connection((host, port))
        threading.Thread(target=relay, args=(client, upstream), daemon=True).start()
        threading.Thread(target=relay, args=(upstream, client), daemon=True).start()


if "-N" in rest:
    master = plan.get("master", {})
    time.sleep(master.get("delay", 0))
    if master.get("code"):
        finish(master)
    forwards = {}
    mux = socket.socket(socket.AF_UNIX)
    mux.bind(control)
    mux.listen()
    while True:
        conn, _ = mux.accept()
        op, path, host, port = conn.makefile().readline().split()
        key = (path, host, int(port))
        if op == "forward" and key not in forwards:
            if os.path.exists(path):
                os.unlink(path)  # StreamLocalBindUnlink=yes
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(path)
            listener.listen()
            forwards[key] = listener
            threading.Thread(target=serve_forward, args=(listener, host, int(port)), daemon=True).start()
        elif op == "cancel" and key in forwards:
            forwards.pop(key).close()
        conn.sendall(b"ok\n")
        conn.close()

if "-O" in rest:
    op = rest[rest.index("-O") + 1]
    spec = rest[rest.index("-L") + 1]
    path, host, port = spec.rsplit(":", 2)
    host = host.strip("[]")
    try:
        conn = socket.socket(socket.AF_UNIX)
        conn.connect(control)
    except OSError as exc:
        finish({"code": 255, "stderr": f"Control socket connect({control}): {exc}\n"})
    conn.sendall(f"{op} {path} {host} {port}\n".encode())
    conn.recv(16)
    sys.exit(0)

command = rest[-1]
if not os.path.exists(control):
    finish({"code": 255, "stderr": "ssh: connect to host nowhere port 22: Connection refused\n"})
outcome = plan.get("commands", {}).get(command)
if outcome is None:
    finish({"code": 127, "stderr": f"bash: line 1: {command.split()[0]}: command not found\n"})
time.sleep(outcome.get("delay", 0))
finish(outcome)
'''


def wait_until(predicate, timeout=10.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


@dataclass
class RemoteMachine:
    """A remote TensorWatch: a real control server, and a plan for its fake ssh."""

    supervisor: Supervisor
    server: httpd.ControlServer
    plan_path: Path
    log_path: Path
    ssh: str
    plan: dict = field(default_factory=dict)
    #: Every connection its servers accepted, so a test can drop them all.
    accepted: list = field(default_factory=list)

    def restart_drops_connections(self) -> None:
        for conn in self.accepted:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def hello(self, **changes) -> dict:
        answer = {"app": "tensorwatch", "version": "9.9", "api": API_VERSION, "host": "127.0.0.1",
                  "port": self.port, "hostname": "gpu-host", "running": True, "error": None}
        return {"stdout": "Welcome to the GPU box\n" + json.dumps({**answer, **changes}) + "\n",
                "stderr": "starting TensorWatch...\n"}

    def set_plan(self, **plan) -> None:
        self.plan = plan
        self.plan_path.write_text(json.dumps(plan))

    def answer_hello(self, command="tensorwatch", **outcome) -> None:
        self.set_plan(**{**self.plan, "commands": {f"{command} remote hello": outcome or self.hello()}})

    def calls(self, flag: str | None = None) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        lines = [json.loads(line)["argv"] for line in self.log_path.read_text().splitlines()]
        return [argv for argv in lines if flag is None or flag in argv]


@pytest.fixture
def runtime(monkeypatch):
    # AF_UNIX paths are short (108 bytes), so not pytest's long tmp_path.
    root = tempfile.mkdtemp(prefix="tw-")
    monkeypatch.setenv("XDG_RUNTIME_DIR", root)
    yield Path(root) / "tensorwatch"
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def machine(home, tmp_path, monkeypatch, runtime):
    script = tmp_path / "fake_ssh"
    script.write_text(f"#!{sys.executable}\n{FAKE_SSH}")
    script.chmod(0o755)
    plan_path, log_path = tmp_path / "plan.json", tmp_path / "ssh.log"
    monkeypatch.setenv("FAKE_SSH_PLAN", str(plan_path))
    monkeypatch.setenv("FAKE_SSH_LOG", str(log_path))
    cfg = Config(
        server=ServerSpec(port=0, idle_exit=4.0),
        boards=(BoardSpec(name="gpu-runs", port=1, logdir=tmp_path),),
        path=tmp_path / "remote.toml",
    )
    accepted: list = []
    original = httpd.ControlServer.get_request

    def get_request(self):
        conn, address = original(self)
        accepted.append(conn)
        return conn, address

    monkeypatch.setattr(httpd.ControlServer, "get_request", get_request)
    supervisor = Supervisor(cfg)
    server = httpd.serve(supervisor, "127.0.0.1", 0)
    item = RemoteMachine(supervisor, server, plan_path, log_path, str(script), accepted=accepted)
    item.answer_hello()
    try:
        yield item
    finally:
        server.shutdown()
        server.server_close()


@dataclass
class Window:
    """Whether a TensorWatch window is open on this machine."""

    open: bool = True
    published: int = 0

    def present(self) -> bool:
        return self.open

    def publish(self) -> None:
        self.published += 1


@pytest.fixture
def link_for(machine):
    links: list[remote.RemoteLink] = []

    def make(spec=None, window=None) -> tuple[remote.RemoteLink, Window]:
        window = window or Window()
        link = remote.RemoteLink(spec or RemoteSpec(name="gpu", ssh="gpu-host", label="GPU box"),
                                 window.publish, window.present, machine.ssh)
        links.append(link)
        link.start()
        return link, window

    yield make
    for link in links:
        link.shutdown()


def connected(link):
    return lambda: link.state == "connected"


# ------------------------------------------------------------------ lifecycle


def test_connects_while_a_window_is_open_and_closes_after(machine, link_for, runtime):
    link, window = link_for()
    wait_until(connected(link), what="connected")
    state = link.to_json()
    assert state["hostname"] == "gpu-host" and state["version"] == "9.9"
    assert [board["name"] for board in state["boards"]] == ["gpu-runs"]
    assert state["error"] is None and state["enabled"] is True
    assert isinstance(state["latency_ms"], float)
    # One master, one hello, one forward to a private Unix socket.
    assert len(machine.calls("-N")) == 1
    assert len(machine.calls("forward")) == 1
    sockets = sorted(path.name for path in runtime.iterdir())
    assert sockets == ["gpu.sock", "gpu.ssh"]
    assert oct(runtime.stat().st_mode & 0o777) == "0o700"
    master_pid = _master_pid(machine)

    # Requests to the remote go through the link.
    status, body = remote.request(link.upstream, "GET", "/api/state")
    assert status == 200 and json.loads(body)["boards"][0]["name"] == "gpu-runs"

    window.open = False
    link.nudge()
    wait_until(lambda: link.state == "idle", what="idle")
    assert link.upstream is None and link.to_json()["boards"] == []
    wait_until(lambda: not _alive(master_pid), what="the master to exit")
    assert list(runtime.iterdir()) == []

    # And it comes back when a window does.
    window.open = True
    link.nudge()
    wait_until(connected(link), what="reconnected")
    assert len(machine.calls("-N")) == 2


def test_nothing_connects_without_a_window_or_while_disabled(machine, link_for):
    link, _ = link_for(window=Window(open=False))
    disabled, _ = link_for(RemoteSpec(name="off", ssh="gpu-host", enabled=False))
    time.sleep(0.5)
    assert link.state == "idle" and disabled.state == "idle"
    assert disabled.to_json()["enabled"] is False
    assert machine.calls() == []


def test_the_remote_state_stream_reaches_this_dashboard(machine, link_for):
    link, window = link_for()
    wait_until(connected(link))
    before = window.published
    machine.supervisor.reload(Config(
        server=ServerSpec(port=0, idle_exit=4.0),
        boards=(BoardSpec(name="gpu-runs", port=1, logdir=Path("/")),
                BoardSpec(name="new-board", port=2, logdir=Path("/"))),
        path=machine.supervisor.config.path,
    ))
    machine.supervisor._absorb_pending()
    machine.supervisor._refresh_snapshot(force=True)
    machine.supervisor.publish()
    wait_until(lambda: len(link.to_json()["boards"]) == 2, what="the new board")
    assert window.published > before


def test_a_watched_remote_is_kept_up_but_not_treated_as_a_window(machine, link_for):
    """Presence: pings every idle_exit / 4, as a peer."""
    remote_sup = machine.supervisor
    link, _ = link_for()
    wait_until(connected(link))
    remote_sup._last_seen = remote_sup._last_window = time.monotonic() - 100
    # idle_exit is 4 s there, so the link pings every second.
    wait_until(lambda: remote_sup.idle_for() < 2, timeout=4, what="a ping")
    assert remote_sup.window_idle_for() > 90
    # Its stream is a peer's too: it neither counts as a window nor pins the
    # remote up on its own, should this machine's network vanish.
    assert remote_sup.watchers == 0


def test_clock_skew_is_measured_once_and_corrected(machine, link_for, monkeypatch):
    real = time.time
    # This machine's clock is an hour ahead of the remote's (the same process here).
    monkeypatch.setattr(remote, "time", types.SimpleNamespace(
        time=lambda: real() + 3600, monotonic=time.monotonic, sleep=time.sleep))
    link, _ = link_for()
    wait_until(connected(link))
    assert link._offset == pytest.approx(3600, abs=1)
    board = {"name": "b", "host": "b", "since": 1000.0, "last_event": 2000.0}
    job = {"id": 1, "since": 500.0, "queued_at": 400.0}
    link._absorb({"now": real() + 7, "boards": [board],
                  "queue": {"running": [job], "queued": [], "updated_at": 100.0}}, publish=False)
    shifted = link.to_json()
    # The frame's own "now" (delivery delay) does not move it.
    assert shifted["boards"][0]["since"] == pytest.approx(4600.0, abs=1)
    assert shifted["queue"]["running"][0]["queued_at"] == pytest.approx(4000.0, abs=1)
    assert shifted["queue"]["updated_at"] == pytest.approx(3700.0, abs=1)


def test_unchanged_frames_are_not_passed_on(machine, link_for):
    """Two machines that list each other must not echo frames back and forth."""
    link, window = link_for()
    wait_until(connected(link))
    before = window.published
    for _ in range(5):
        machine.supervisor.publish()  # same boards, new "now"
    time.sleep(0.5)
    assert window.published == before


def test_only_dns_labels_from_a_remote_become_host_names(machine, link_for):
    link, _ = link_for()
    wait_until(connected(link))
    link._absorb({"now": time.time(), "boards": [
        {"name": "ok", "host": "ok"}, {"name": "evil", "host": "evil.example/"}, {"name": "none"},
    ]}, publish=False)
    assert [board["name"] for board in link.to_json()["boards"]] == ["ok"]


def test_a_ping_leaves_pooled_connections_with_the_relay_timeout(machine, link_for):
    from tensorwatch import proxy

    link, _ = link_for()
    wait_until(connected(link))
    remote.request(link.upstream, "GET", "/healthz", timeout=0.3)
    conn, reused = link.upstream.acquire()
    assert reused and conn.sock.gettimeout() == proxy.READ_TIMEOUT
    link.upstream.release(conn)


# -------------------------------------------------------------- failure modes


@pytest.mark.parametrize(
    ("master", "kind"),
    [
        ({"code": 255, "stderr": "marvin@gpu-host: Permission denied (publickey).\n"}, remote.AUTH),
        ({"code": 255, "stderr": "Host key verification failed.\n"}, remote.HOST_KEY),
        ({"code": 255, "stderr": "ssh: Could not resolve hostname gpu-host: Name or service not known\n"},
         remote.UNREACHABLE),
        ({"code": 255, "stderr": "ssh: connect to host 10.0.0.9 port 22: No route to host\n"},
         remote.UNREACHABLE),
    ],
)
def test_ssh_failures_are_told_apart(machine, link_for, master, kind):
    machine.set_plan(**{**machine.plan, "master": master})
    link, _ = link_for()
    wait_until(lambda: link.state == "error")
    state = link.to_json()
    assert state["error"]["kind"] == kind
    assert state["error"]["message"] == master["stderr"].strip()
    retry_in = state["next_attempt"] - time.time()
    if kind in remote.PERSISTENT_KINDS:
        assert retry_in > 50  # the user has to act; no point hammering it
    else:
        assert retry_in < 2


def test_a_missing_tensorwatch_is_looked_for_where_install_puts_it(machine, link_for):
    machine.answer_hello(command=remote.FALLBACK_COMMAND)
    link, _ = link_for()
    wait_until(connected(link))
    commands = [argv[-1] for argv in machine.calls() if "-N" not in argv and "-O" not in argv]
    assert commands == ["tensorwatch remote hello", f"{remote.FALLBACK_COMMAND} remote hello"]


@pytest.mark.parametrize(
    ("outcome", "kind", "words"),
    [
        ({"code": 127, "stderr": "zsh:1: command not found: tensorwatch\n"}, remote.MISSING, "not found"),
        ({"code": 2, "stderr": "tensorwatch: error: argument command: invalid choice: 'remote'\n"},
         remote.TOO_OLD, "predates remotes"),
        ("old-api", remote.TOO_OLD, "API 1"),
        ("not-running", remote.FAILED, "did not start"),
        ({"code": 0, "stdout": "a login banner only\n"}, remote.FAILED, "unexpected answer"),
    ],
)
def test_remote_tensorwatch_problems_are_explained(machine, link_for, outcome, kind, words):
    if outcome == "old-api":
        outcome = machine.hello(api=1)
    elif outcome == "not-running":
        outcome = {**machine.hello(running=False, error="TensorWatch did not start"), "code": 1}
    machine.set_plan(commands={"tensorwatch remote hello": outcome,
                               f"{remote.FALLBACK_COMMAND} remote hello": outcome})
    link, _ = link_for()
    wait_until(lambda: link.state == "error")
    error = link.to_json()["error"]
    assert error["kind"] == kind and words in error["message"]
    assert link.upstream is None


def test_a_hello_that_never_comes_back_is_unreachable(machine, link_for, monkeypatch):
    monkeypatch.setattr(remote, "HELLO_TIMEOUT", 0.5)
    machine.answer_hello(**machine.hello(), delay=5)
    link, _ = link_for()
    wait_until(lambda: link.state == "error")
    assert link.to_json()["error"]["kind"] == remote.UNREACHABLE


def test_retry_skips_the_backoff(machine, link_for):
    machine.set_plan(**{**machine.plan, "master": {"code": 255, "stderr": "Permission denied (publickey).\n"}})
    link, window = link_for()
    wait_until(lambda: link.state == "error")
    machine.set_plan(commands=machine.plan["commands"])  # the user fixed their key
    time.sleep(0.3)
    assert link.state == "error"  # sixty seconds of backoff...
    link.retry()
    wait_until(connected(link), timeout=5)  # ...unless asked


def test_a_restarted_remote_is_followed_through_the_same_master(machine, link_for):
    link, _ = link_for()
    wait_until(connected(link))
    master_pid = _master_pid(machine)
    # The remote's TensorWatch restarts on its configured port, while the
    # SSH master (and its forward to that port) stays up.
    port = machine.port
    machine.server.shutdown()
    machine.server.server_close()
    machine.restart_drops_connections()
    wait_until(lambda: link.state == "error", what="the drop to be noticed")
    machine.server = httpd.serve(machine.supervisor, "127.0.0.1", port)
    wait_until(connected(link), timeout=10, what="reconnected")
    # Asking the master again for the same forward would bind nothing: the old
    # one is cancelled first, and the new socket really reaches the remote.
    assert _master_pid(machine) == master_pid and len(machine.calls("-N")) == 1
    assert len(machine.calls("cancel")) == 1
    assert remote.request(link.upstream, "GET", "/healthz")[0] == 200


def test_a_dead_master_is_noticed_and_replaced(machine, link_for):
    link, _ = link_for()
    wait_until(connected(link))
    os.kill(_master_pid(machine), signal.SIGKILL)
    wait_until(lambda: link.state != "connected", what="the loss")
    wait_until(connected(link), timeout=10, what="a new master")
    assert len(machine.calls("-N")) == 2


def test_requests_while_disconnected_say_why(machine, link_for):
    machine.set_plan(**{**machine.plan, "master": {"code": 255, "stderr": "Permission denied (publickey).\n"}})
    link, _ = link_for()
    wait_until(lambda: link.state == "error")
    assert link.unavailable() == "Can't reach GPU box: Permission denied (publickey)."


# ------------------------------------------------------------------ federation


def test_federation_follows_the_registry(machine, runtime):
    window = Window()
    specs = [RemoteSpec(name="gpu", ssh="gpu-host"), RemoteSpec(name="spare", ssh="gpu-host", enabled=False)]
    federation = remote.Federation(specs, window.publish, window.present, ssh=machine.ssh)
    try:
        gpu = federation.link("gpu")
        wait_until(connected(gpu))
        assert [link["name"] for link in federation.snapshot()] == ["gpu", "spare"]

        # An unchanged spec keeps its link; a changed one is restarted; a removed one closes.
        federation.update([RemoteSpec(name="gpu", ssh="gpu-host"),
                           RemoteSpec(name="spare", ssh="gpu-host", label="Spare")])
        assert federation.link("gpu") is gpu
        spare = federation.link("spare")
        wait_until(connected(spare))
        federation.update([RemoteSpec(name="spare", ssh="gpu-host", label="Spare")])
        assert federation.link("gpu") is None and gpu.state == "connected" or gpu.upstream is None
        wait_until(lambda: sorted(p.name for p in runtime.iterdir()) == ["spare.sock", "spare.ssh"])
    finally:
        federation.shutdown()
    assert list(runtime.iterdir()) == []
    assert all(not _alive(pid) for pid in _master_pids(machine))


def test_check_reports_without_disturbing_the_managers_link(machine, link_for, runtime):
    link, _ = link_for()
    wait_until(connected(link))
    result = remote.check(RemoteSpec(name="gpu", ssh="gpu-host"), ssh=machine.ssh, timeout=10)
    assert result["state"] == "connected" and result["boards"][0]["name"] == "gpu-runs"
    assert remote.summarize(result).startswith("connected to TensorWatch 9.9 on gpu-host: 1 board(s)")
    # Its own sockets, gone again; the manager's link is untouched.
    assert sorted(p.name for p in runtime.iterdir()) == ["gpu.sock", "gpu.ssh"]
    assert link.state == "connected"
    assert remote.request(link.upstream, "GET", "/healthz")[0] == 200


# ---------------------------------------------------------------- classifiers


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        ("Warning: Permanently added 'x' (ED25519) to the list of known hosts.\n"
         "marvin@x: Permission denied (publickey,password).", remote.AUTH),
        ("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", remote.HOST_KEY),
        ("kex_exchange_identification: read: Connection reset by peer", remote.UNREACHABLE),
        ("Timeout, server gpu-host not responding.", remote.UNREACHABLE),
        ("something else entirely", remote.FAILED),
    ],
)
def test_classify_ssh(stderr, kind):
    error = remote.classify_ssh(255, stderr)
    assert error.kind == kind
    assert "Permanently added" not in error.message


def test_ssh_command_lines():
    ssh = remote.SSH("desktop", Path("/run/tw/x.ssh"))
    master = ssh.master()
    assert master[-3:] == ["-T", "--", "desktop"] and "-N" in master
    for option in ("BatchMode=yes", "ControlMaster=yes", "ControlPersist=no", "ExitOnForwardFailure=yes",
                   "ControlPath=/run/tw/x.ssh", "ServerAliveInterval=15"):
        assert option in master
    # A destination that looks like an option is still a destination.
    assert remote.SSH("-oProxyCommand=evil", Path("/c")).run("tensorwatch remote hello")[-3:] == [
        "--", "-oProxyCommand=evil", "tensorwatch remote hello"]
    assert ssh.forward(Path("/s"), "::1", 6005)[-4:] == ["-L", "/s:[::1]:6005", "--", "desktop"]


# ------------------------------------------------------------------------ cli


@pytest.fixture
def offline(home, monkeypatch):
    monkeypatch.setattr(cli, "_notify_reload", lambda cfg: False)
    monkeypatch.setattr(cli, "_live_state", lambda cfg: None)
    return home


def test_remote_add_set_list_rm(offline, capsys):
    assert cli.main(["remote", "add", "nvidia-desktop", "--ssh", "desktop", "--label", "NVIDIA desktop",
                     "--no-check"]) == 0
    spec = config.load().remote("nvidia-desktop")
    assert (spec.name, spec.ssh, spec.title, spec.enabled) == ("nvidia-desktop", "desktop", "NVIDIA desktop", True)

    assert cli.main(["remote", "add", "Bad_Name", "--ssh", "x", "--no-check"]) != 0
    assert cli.main(["remote", "add", "nvidia-desktop", "--ssh", "x", "--no-check"]) != 0
    capsys.readouterr()

    assert cli.main(["remote", "set", "nvidia-desktop", "--disable", "--command", "/opt/tw/bin/tensorwatch"]) == 0
    spec = config.load().remote("nvidia-desktop")
    assert not spec.enabled and spec.command == "/opt/tw/bin/tensorwatch"
    assert cli.main(["remote", "set", "nvidia-desktop", "--enable", "--label", "", "--command", ""]) == 0
    spec = config.load().remote("nvidia-desktop")
    assert spec.enabled and spec.label == "" and spec.title == "nvidia-desktop"
    assert spec.command == config.DEFAULT_REMOTE_COMMAND

    capsys.readouterr()
    assert cli.main(["remote", "list", "--json"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    assert listed["name"] == "nvidia-desktop" and listed["live"] is None

    assert cli.main(["remote", "rm", "nvidia-desktop"]) == 0
    assert config.load().remotes == ()


def test_remote_add_checks_the_connection(offline, machine, monkeypatch, capsys):
    monkeypatch.setenv("TENSORWATCH_SSH", machine.ssh)
    assert cli.main(["remote", "add", "gpu", "--ssh", "gpu-host"]) == 0
    out = capsys.readouterr().out
    assert "connected to TensorWatch 9.9 on gpu-host" in out and "gpu-runs" in out

    machine.set_plan(master={"code": 255, "stderr": "Permission denied (publickey).\n"})
    assert cli.main(["remote", "check", "gpu"]) == 1
    out = capsys.readouterr().out
    assert "SSH authentication failed" in out and "without a password prompt" in out


def test_remote_hello_prints_only_json(offline, monkeypatch, capsys):
    def ensure(cfg, timeout=None):
        print("starting TensorWatch...")  # must not reach stdout
        return True

    monkeypatch.setattr(cli, "_ensure_running", ensure)
    assert cli.main(["remote", "hello"]) == 0
    captured = capsys.readouterr()
    hello = json.loads(captured.out)
    assert hello["app"] == "tensorwatch" and hello["api"] == API_VERSION and hello["running"]
    assert hello["host"] == "127.0.0.1" and "starting" in captured.err
    assert remote.classify_hello(0, captured.out, captured.err) == hello


def test_cli_calls_keep_the_manager_up_but_are_not_windows(monkeypatch, home):
    """A status bar polling `tensorwatch status` must not hold SSH links open."""
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    def urlopen(request, timeout):
        seen.update(request.header_items())
        return Response()

    monkeypatch.setattr(cli.urllib.request, "urlopen", urlopen)
    cli._api(config.load(), "/api/state")
    assert seen.get(httpd.PEER_HEADER.capitalize()) == "1"


def test_a_new_manager_has_had_no_window(tmp_path):
    """Started by another machine's hello, it must not connect to its own remotes."""
    supervisor = Supervisor(Config(server=ServerSpec(port=0), boards=(), path=tmp_path / "c.toml"))
    assert supervisor.window_idle_for() == float("inf")
    supervisor.touch(peer=True)
    assert supervisor.idle_for() < 1 and supervisor.window_idle_for() == float("inf")
    supervisor.touch()
    assert supervisor.window_idle_for() < 1


def test_open_names_a_remote_board(offline, monkeypatch):
    assert cli.main(["remote", "add", "gpu", "--ssh", "gpu-host", "--no-check"]) == 0
    cfg = config.load()
    assert cli.board_page_url(cfg, "runs", "gpu").endswith("/boards/runs@gpu")


# -------------------------------------------------------------------- helpers


def _master_pids(machine) -> list[int]:
    lines = [json.loads(line) for line in machine.log_path.read_text().splitlines()]
    return [entry["pid"] for entry in lines if "-N" in entry["argv"]]


def _master_pid(machine) -> int:
    return _master_pids(machine)[-1]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is gone for our purposes; the link reaps its own children.
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return False
