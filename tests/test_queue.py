from __future__ import annotations

import json
import os
import socket
import time
import struct
import threading
from pathlib import Path

import pytest

from conftest import wait_until
from tensorwatch import queue

STATUS_VIEW = {
    "jobs": [
        {
            "id": 2759,
            "name": "screeps-outpost-actor",
            "state": "running",
            "priority": 1000,
            "cwd": "/repos/xxscreeps",
            "args": ["python", "-m", "agent", "--output", "samples/rl/runs/corpora"],
            "maxAttempts": 1,
            "attemptCount": 1,
            "timeLimitMs": 2_700_000,
            "createdAt": 1_700_000_000_000,
            "updatedAt": 1_700_000_060_000,
        },
        {
            "id": 2758,
            "name": "kagg-vapo",
            "state": "queued",
            "eligibility": "protected_drain",
            "priority": 0,
            "cwd": "/repos/kraggiculture",
            "args": ["python", "launch.py", "--run-dir", "runs/vapo"],
            "createdAt": 1_700_000_000_000,
            "updatedAt": 1_700_000_000_000,
        },
        {
            "id": 2761,
            "name": "byte-duo",
            "state": "queued",
            "priority": 5,
            "cwd": "/repos/bitlearn",
            "createdAt": 1_700_000_010_000,
            "updatedAt": 1_700_000_010_000,
        },
        {"id": 1, "name": "old", "state": "succeeded", "priority": 0, "cwd": "/repos/x"},
    ],
    "activeLeases": 1,
    "effectiveLimit": 1,
    "admissionBlocked": False,
    "reservation": {"protectedJob": 2758},
}

BOARD_DIRS = {
    "xxscreeps": Path("/repos/xxscreeps/samples/rl/runs"),
    "kraggiculture": Path("/repos/kraggiculture/runs"),
    "unrelated": Path("/repos/elsewhere/runs"),
}


def frame(payload: dict) -> bytes:
    body = json.dumps(payload).encode()
    return struct.pack(">I", len(body)) + body


def status_frame() -> bytes:
    return frame({"request_id": "x", "reply": {"type": "status", **STATUS_VIEW}})


def test_parse_splits_live_jobs_and_matches_boards():
    snapshot = queue.parse(STATUS_VIEW, BOARD_DIRS)

    assert snapshot.connected is True
    assert [job.id for job in snapshot.running] == [2759]
    # Queued jobs are ordered by priority, then by id (the scheduler's order).
    assert [job.id for job in snapshot.queued] == [2761, 2758]
    assert snapshot.active_leases == 1 and snapshot.effective_limit == 1
    assert snapshot.protected_job == 2758

    running = snapshot.running[0]
    assert running.board == "xxscreeps"  # logdir lives under the job's cwd
    assert running.project == "xxscreeps"
    assert running.attempts == "1/1"
    assert running.time_limit == 2700.0
    assert snapshot.queued[1].reason == "protected_drain"
    assert snapshot.queued[0].board is None  # bitlearn has no board here


def test_finished_jobs_are_dropped():
    snapshot = queue.parse(STATUS_VIEW, {})
    assert all(job.state in queue.LIVE_STATES for job in (*snapshot.running, *snapshot.queued))


def test_signature_ignores_elapsed_time():
    first = queue.parse(STATUS_VIEW, BOARD_DIRS)
    view = json.loads(json.dumps(STATUS_VIEW))
    view["jobs"][0]["updatedAt"] += 30_000
    assert queue.parse(view, BOARD_DIRS).signature == first.signature

    view["jobs"][0]["state"] = "queued"
    assert queue.parse(view, BOARD_DIRS).signature != first.signature


def test_subscribe_request_is_the_documented_frame():
    payload = queue.subscribe_request()
    (length,) = struct.unpack(">I", payload[:4])
    body = json.loads(payload[4:])
    assert length == len(payload) - 4
    assert body["protocol_version"] == queue.PROTOCOL_VERSION
    assert body["op"] == {"type": "subscribe"}
    assert body["request_id"]


def test_cancel_request_is_the_documented_frame():
    payload = queue.encode_request(
        {"type": "cancel", "job": 7, "force": False}, idempotency_key="k"
    )
    (length,) = struct.unpack(">I", payload[:4])
    body = json.loads(payload[4:])
    assert length == len(payload) - 4
    assert body["protocol_version"] == queue.PROTOCOL_VERSION
    assert body["op"] == {"type": "cancel", "job": 7, "force": False}
    assert body["idempotency_key"] == "k"
    assert body["request_id"]


def test_job_view_surfaces_daemon_errors():
    with pytest.raises(queue.QueueError, match="not_found") as excinfo:
        queue.job_view(
            json.dumps({"request_id": "x", "error": {"code": "not_found",
                                                     "message": "job 7 not found"}}).encode()
        )
    assert excinfo.value.code == "not_found"
    with pytest.raises(queue.QueueError, match="unexpected reply"):
        queue.job_view(json.dumps({"request_id": "x", "reply": {"type": "status"}}).encode())


def test_cancel_without_a_daemon(tmp_path):
    with pytest.raises(queue.QueueError, match="not found"):
        queue.cancel(7, tmp_path / "absent.sock")
    with pytest.raises(queue.QueueError, match="invalid job id"):
        queue.cancel(0, tmp_path / "absent.sock")


def test_cancel_sends_a_mutation_and_returns_the_job(tmp_path):
    path = tmp_path / "mlqd.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    requests: list[dict] = []

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            header = conn.recv(4)
            (length,) = struct.unpack(">I", header)
            requests.append(json.loads(conn.recv(length)))
            conn.sendall(frame({
                "request_id": "x",
                "reply": {"type": "job", "job": {"id": 7, "name": "run", "state": "cancelled"}},
            }))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        job = queue.cancel(7, path)
    finally:
        listener.close()
        thread.join(timeout=2)

    assert job["id"] == 7 and job["state"] == "cancelled"
    assert requests[0]["op"] == {"type": "cancel", "job": 7, "force": False}
    assert requests[0]["idempotency_key"]


def test_cancel_surfaces_a_daemon_error(tmp_path):
    path = tmp_path / "mlqd.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            header = conn.recv(4)
            (length,) = struct.unpack(">I", header)
            conn.recv(length)
            conn.sendall(frame({
                "request_id": "x",
                "error": {"code": "not_found", "message": "job 9 not found"},
            }))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with pytest.raises(queue.QueueError, match="not_found") as excinfo:
            queue.cancel(9, path)
        assert excinfo.value.code == "not_found"
    finally:
        listener.close()
        thread.join(timeout=2)


def test_status_view_surfaces_daemon_errors():
    with pytest.raises(ConnectionError, match="unsupported_protocol"):
        queue.status_view(
            json.dumps({"request_id": "x", "error": {"code": "unsupported_protocol",
                                                     "message": "expected 9"}}).encode()
        )
    with pytest.raises(ConnectionError, match="unexpected reply"):
        queue.status_view(json.dumps({"request_id": "x", "reply": {"type": "job"}}).encode())


def test_oversized_frame_is_rejected(tmp_path):
    """A bogus length prefix means the stream is desynced, not that we allocate."""
    server, client = socket.socketpair()
    with server, client:
        server.sendall(struct.pack(">I", queue.MAX_FRAME_BYTES + 1))
        client.settimeout(2)
        with pytest.raises(ConnectionError, match="out of sync"):
            queue.read_frame(client)


@pytest.fixture
def fake_mlqd(tmp_path):
    """A stand-in mlqd that accepts one subscription and pushes frames on demand."""
    path = tmp_path / "mlqd.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    pushes: list[bytes] = []
    requests: list[dict] = []
    ready = threading.Event()
    push_now = threading.Event()
    stop = threading.Event()

    def serve() -> None:
        conn, _ = listener.accept()
        with conn:
            header = conn.recv(4)
            (length,) = struct.unpack(">I", header)
            requests.append(json.loads(conn.recv(length)))
            conn.sendall(status_frame())
            ready.set()
            while not stop.is_set():
                if push_now.wait(0.05):
                    push_now.clear()
                    if pushes:
                        conn.sendall(pushes.pop(0))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield {
            "path": path,
            "requests": requests,
            "ready": ready,
            "push": lambda payload: (pushes.append(payload), push_now.set()),
            "stop": stop,
        }
    finally:
        stop.set()
        push_now.set()
        listener.close()
        thread.join(timeout=2)


def test_subscriber_consumes_pushed_snapshots(fake_mlqd):
    changes: list[int] = []
    subscriber = queue.start(
        fake_mlqd["path"], lambda: BOARD_DIRS, lambda: changes.append(1)
    )
    try:
        assert fake_mlqd["ready"].wait(5)
        snapshot = wait_until(lambda: subscriber.snapshot().connected and subscriber.snapshot())
        assert snapshot is not None
        assert [job.id for job in snapshot.running] == [2759]
        assert fake_mlqd["requests"][0]["op"] == {"type": "subscribe"}
        assert changes  # the first snapshot is a change

        # A pushed frame with the running job gone must land without polling.
        view = json.loads(json.dumps(STATUS_VIEW))
        view["jobs"] = [job for job in view["jobs"] if job["id"] != 2759]
        view["activeLeases"] = 0
        fake_mlqd["push"](frame({"request_id": "x", "reply": {"type": "status", **view}}))
        assert wait_until(lambda: not subscriber.snapshot().running), subscriber.snapshot()
        assert subscriber.snapshot().active_leases == 0
    finally:
        subscriber.shutdown()


def test_subscriber_reports_a_missing_daemon(tmp_path):
    subscriber = queue.start(tmp_path / "absent.sock", lambda: {}, None)
    try:
        snapshot = wait_until(lambda: subscriber.snapshot().error and subscriber.snapshot())
        assert snapshot is not None and snapshot.connected is False
        assert "not found" in snapshot.error
    finally:
        subscriber.shutdown()


def test_one_shot_without_a_daemon(tmp_path):
    snapshot = queue.one_shot(tmp_path / "absent.sock")
    assert snapshot.connected is False and "not found" in snapshot.error



def test_idle_subscription_stays_connected(fake_mlqd, monkeypatch):
    """An idle queue pushes nothing; that must not look like a broken stream.

    Regression: read timeouts were parsed as a frame header, so every idle period
    tore the subscription down and the panel flapped between connected and
    "unpack requires a buffer of 4 bytes".
    """
    monkeypatch.setattr(queue, "READ_TIMEOUT", 0.15)
    subscriber = queue.start(fake_mlqd["path"], lambda: BOARD_DIRS, None)
    try:
        assert wait_until(lambda: subscriber.snapshot().connected)
        # Several read timeouts must pass without a reconnect or an error.
        for _ in range(6):
            assert subscriber.snapshot().connected is True
            assert subscriber.snapshot().error is None
            time.sleep(0.12)
        assert len(fake_mlqd["requests"]) == 1  # never resubscribed

        # And a push after the idle period still lands.
        view = json.loads(json.dumps(STATUS_VIEW))
        view["activeLeases"] = 0
        view["jobs"] = [job for job in view["jobs"] if job["state"] != "running"]
        fake_mlqd["push"](frame({"request_id": "x", "reply": {"type": "status", **view}}))
        assert wait_until(lambda: subscriber.snapshot().active_leases == 0)
    finally:
        subscriber.shutdown()


def test_read_frame_distinguishes_idle_close_and_data():
    server, client = socket.socketpair()
    with server, client:
        client.settimeout(0.1)
        assert queue.read_frame(client) == b""  # idle

        server.sendall(status_frame())
        payload = queue.read_frame(client)
        assert payload and json.loads(payload)["reply"]["type"] == "status"

        server.close()
        assert queue.read_frame(client) is None  # closed

def test_one_shot_reads_the_first_frame(fake_mlqd):
    snapshot = queue.one_shot(fake_mlqd["path"])
    assert snapshot.connected is True
    assert [job.id for job in snapshot.queued] == [2761, 2758]


DIRS = {
    "cleanrl": Path("/repos/cleanrl/runs"),
    "cleanrl-archive": Path("/repos/cleanrl/runs_old"),
    "golf": Path("/repos/parameter-golf/tb_logs"),
    "golf-post": Path("/repos/parameter-golf/postraining/runs"),
    "screeps": Path("/repos/xxscreeps/samples/rl/runs"),
}


def test_output_paths_in_the_command_line_decide_the_board():
    """The run names its own logdir; that beats guessing from the cwd."""
    # A repo with several watched logdirs: only the one the job writes to is marked.
    assert queue._boards_for(
        "/repos/parameter-golf", ["python", "train.py", "--logdir", "tb_logs/run7"], DIRS
    ) == ("golf",)
    assert queue._boards_for(
        "/repos/parameter-golf", ["python", "post.py", "--run-dir=postraining/runs/x"], DIRS
    ) == ("golf-post",)
    # A run writing somewhere nobody watches marks nothing at all.
    assert queue._boards_for(
        "/repos/parameter-golf", ["python", "pre.py", "--out", "pretraining/runs/exp"], DIRS
    ) == ()
    # Absolute paths and deeper logdirs work the same way.
    assert queue._boards_for(
        "/repos/xxscreeps",
        ["uv", "run", "--project", "samples/rl", "--output", "samples/rl/runs/corpora"],
        DIRS,
    ) == ("screeps",)
    assert queue._boards_for(
        "/repos/cleanrl", ["python", "x.py", "--dir", "/repos/cleanrl/runs_old/exp"], DIRS
    ) == ("cleanrl-archive",)


def test_activity_breaks_an_ambiguous_cwd():
    """CleanRL-style runs name no logdir; the one receiving data is theirs."""
    args = [".venv/bin/python", "cleanrl/td_jepa.py", "--exp-name", "td_jepa_v1"]
    # cleanrl and cleanrl-archive both live under the repo: undecidable on paths.
    assert queue._boards_for("/repos/cleanrl", args, DIRS) == ()
    # ...but only one of them is being written to right now.
    assert queue._boards_for("/repos/cleanrl", args, DIRS, {"cleanrl"}) == ("cleanrl",)
    # Both writing is still ambiguous, and an unrelated board does not help.
    assert queue._boards_for("/repos/cleanrl", args, DIRS, {"cleanrl", "cleanrl-archive"}) == ()
    assert queue._boards_for("/repos/cleanrl", args, DIRS, {"golf"}) == ()


def test_cwd_is_only_a_fallback_and_only_when_unambiguous():
    # One watched logdir in the repo: attribute it even without an explicit path.
    assert queue._boards_for("/repos/xxscreeps", ["python", "train.py"], DIRS) == ("screeps",)
    # Two candidates in the same repo: cannot tell, so say nothing.
    assert queue._boards_for("/repos/cleanrl", ["python", "train.py"], DIRS) == ()
    assert queue._boards_for("/repos/parameter-golf", ["python", "train.py"], DIRS) == ()
    # A directory full of projects, or none at all.
    assert queue._boards_for("/repos", ["python", "x.py"], DIRS) == ()
    assert queue._boards_for("/", [], DIRS) == ()
    assert queue._boards_for("", [], DIRS) == ()
    assert queue._boards_for("/repos/unrelated", ["python", "x.py"], DIRS) == ()


def test_a_job_writing_to_two_watched_logdirs_marks_both():
    dirs = {"a": Path("/repos/proj/runs"), "b": Path("/repos/proj/runs_old")}
    view = {
        "jobs": [{
            "id": 1, "name": "j", "state": "running", "cwd": "/repos/proj",
            "args": ["python", "x.py", "--new", "runs/v2", "--compare", "runs_old/v1"],
        }]
    }
    job = queue.parse(view, dirs).running[0]
    assert job.boards == ("a", "b")
    assert job.board == "a"  # registry order decides what a click opens


def test_symlinked_cwd_still_matches(tmp_path):
    real = tmp_path / "project"
    (real / "runs").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    assert queue._boards_for(str(link), ["python", "t.py"], {"b": real / "runs"}) == ("b",)


def test_offline_keeps_the_last_jobs(fake_mlqd, monkeypatch):
    """A daemon restart must not blank the panel: label the rows stale instead."""
    monkeypatch.setattr(queue, "READ_TIMEOUT", 0.1)
    monkeypatch.setattr(queue, "RECONNECT_BACKOFF", (0.05,))
    subscriber = queue.start(fake_mlqd["path"], lambda: BOARD_DIRS, None)
    try:
        assert wait_until(lambda: subscriber.snapshot().connected)
        fake_mlqd["stop"].set()  # the fake daemon goes away
        os.unlink(fake_mlqd["path"])

        degraded = wait_until(lambda: not subscriber.snapshot().connected and subscriber.snapshot())
        assert degraded is not None
        assert degraded.error
        assert [job.id for job in degraded.running] == [2759]  # last known, not blank
    finally:
        subscriber.shutdown()


def test_backoff_resets_after_a_healthy_session(monkeypatch):
    """A long-lived session must not inherit an old outage's 30 s delay."""
    monkeypatch.setattr(queue, "STABLE_SESSION", 0.0)
    delays: list[float] = []
    subscriber = queue.QueueSubscriber(Path("/nonexistent.sock"), lambda: {}, None)

    sessions = [ConnectionError("boom"), ConnectionError("boom"), None]

    def fake_session() -> None:
        outcome = sessions.pop(0) if sessions else None
        if outcome is not None:
            raise outcome
        subscriber._stop.set()

    monkeypatch.setattr(subscriber, "_session", fake_session)
    monkeypatch.setattr(subscriber._stop, "wait", lambda delay: delays.append(delay) or False)
    subscriber.run()
    # STABLE_SESSION=0 means every failure counts as healthy: always the first rung.
    assert delays == [queue.RECONNECT_BACKOFF[0], queue.RECONNECT_BACKOFF[0]]


def test_refresh_rematches_boards_without_a_new_frame(fake_mlqd):
    """Adding a board must light up its activity now, not at the next queue change."""
    dirs: dict = {}
    subscriber = queue.start(fake_mlqd["path"], lambda: dirs, None)
    try:
        assert wait_until(lambda: subscriber.snapshot().connected)
        assert subscriber.snapshot().running[0].boards == ()

        dirs["screeps"] = Path("/repos/xxscreeps/samples/rl/runs")
        subscriber.refresh()
        assert subscriber.snapshot().running[0].boards == ("screeps",)
    finally:
        subscriber.shutdown()


def test_a_bad_frame_degrades_instead_of_killing_the_thread(fake_mlqd, monkeypatch):
    monkeypatch.setattr(queue, "READ_TIMEOUT", 0.1)
    monkeypatch.setattr(queue, "RECONNECT_BACKOFF", (0.05,))
    subscriber = queue.start(fake_mlqd["path"], lambda: BOARD_DIRS, None)
    try:
        assert wait_until(lambda: subscriber.snapshot().connected)
        fake_mlqd["push"](struct.pack(">I", 5) + b"\xff\xfe\xfd\xfc\xfb")  # not UTF-8 JSON
        assert wait_until(lambda: subscriber.snapshot().error), subscriber.snapshot()
        assert subscriber.is_alive()  # the reconnect loop survived
    finally:
        subscriber.shutdown()


def test_socket_path_follows_mlqueue_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("MLQUEUE_RUNTIME_DIR", str(tmp_path / "rt"))
    assert queue.socket_path() == tmp_path / "rt" / "mlqd.sock"

    monkeypatch.delenv("MLQUEUE_RUNTIME_DIR")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    assert queue.socket_path() == tmp_path / "xdg" / "mlqueue" / "mlqd.sock"
