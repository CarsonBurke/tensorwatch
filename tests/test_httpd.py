from __future__ import annotations

import json
import time
import http.client
import urllib.error
import urllib.request

import pytest

from conftest import wait_until
from tensorwatch import httpd
from tensorwatch.config import BoardSpec, Config, ServerSpec
from tensorwatch.supervisor import Supervisor


@pytest.fixture
def stack(home, tmp_path, fake_tb, free_port):
    logdir = tmp_path / "runs"
    logdir.mkdir()
    board = BoardSpec(
        name="fake",
        port=free_port(),
        logdir=logdir,
        command=tuple(fake_tb),
        autostart="manual",  # keep the test hermetic: nothing spawns unasked
    )
    cfg = Config(
        server=ServerSpec(port=0, poll_interval=0.1, start_stagger=0.0),
        boards=(board,),
        path=tmp_path / "config.toml",
    )
    supervisor = Supervisor(cfg)
    supervisor.start()
    reloads: list[int] = []
    server = httpd.serve(supervisor, "127.0.0.1", 0, lambda: reloads.append(1))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, supervisor, reloads
    finally:
        server.shutdown()
        server.server_close()
        supervisor.shutdown()


def fetch(url: str, method: str = "GET", headers: dict[str, str] = {}):
    request = urllib.request.Request(url, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.headers, response.read()


def test_serves_dashboard_and_assets(stack):
    base, _, _ = stack
    status, headers, body = fetch(f"{base}/")
    assert status == 200
    assert b"<title>TensorWatch</title>" in body
    assert b'href="/static/icon-32.png"' in body
    assert headers["Content-Type"].startswith("text/html")

    status, headers, body = fetch(f"{base}/static/app.js")
    assert status == 200 and b"keep_warm" in body
    etag = headers["ETag"]

    status, headers, body = fetch(f"{base}/static/icon-32.png")
    assert status == 200
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    assert "png" in headers["Content-Type"]

    # Unchanged assets are not re-sent.
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        fetch(f"{base}/static/app.js", headers={"If-None-Match": etag})
    assert excinfo.value.code == 304

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        fetch(f"{base}/static/../config.py")
    assert excinfo.value.code == 404


def test_state_endpoint_lists_boards(stack):
    base, _, _ = stack
    _, _, body = fetch(f"{base}/api/state")
    payload = json.loads(body)
    assert [board["name"] for board in payload["boards"]] == ["fake"]
    assert payload["server"]["keep_warm"] == 2
    assert payload["boards"][0]["url"].startswith("http://127.0.0.1:")


def test_actions_drive_the_supervisor(stack):
    base, supervisor, _ = stack
    fetch(f"{base}/api/boards/fake/start", method="POST")
    assert wait_until(lambda: supervisor.snapshot()[0].state == "running"), supervisor.snapshot()

    _, _, body = fetch(f"{base}/api/boards/fake/logs?lines=5")
    assert b"start:" in body

    fetch(f"{base}/api/boards/fake/stop", method="POST")
    assert wait_until(lambda: supervisor.snapshot()[0].state == "stopped")

    # `demand` only keeps on_demand boards alive; it must not resurrect a manual
    # board the user just stopped.
    fetch(f"{base}/api/boards/fake/demand", method="POST")
    time.sleep(0.5)
    assert supervisor.snapshot()[0].state == "stopped"

    fetch(f"{base}/api/boards/fake/restart", method="POST")
    assert wait_until(lambda: supervisor.snapshot()[0].state == "running")


def test_unknown_board_and_action(stack):
    base, _, _ = stack
    for url in (f"{base}/api/boards/nope/start", f"{base}/api/boards/fake/explode"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            fetch(url, method="POST")
        assert excinfo.value.code == 404


def test_reload_hook(stack):
    base, _, reloads = stack
    fetch(f"{base}/api/reload", method="POST")
    assert reloads == [1]


def test_queue_cancel_unavailable_without_a_hook(stack):
    base, _, _ = stack
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        fetch(f"{base}/api/queue/7/cancel", method="POST")
    assert excinfo.value.code == 501


def test_queue_cancel_rejects_a_bad_id(stack):
    base, _, _ = stack
    for url in (f"{base}/api/queue/nope/cancel", f"{base}/api/queue/0/cancel"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            fetch(url, method="POST")
        assert excinfo.value.code == 400


def test_queue_cancel_drives_the_hook(home, tmp_path, fake_tb, free_port):
    from tensorwatch.queue import QueueError

    logdir = tmp_path / "runs"
    logdir.mkdir()
    board = BoardSpec(
        name="fake",
        port=free_port(),
        logdir=logdir,
        command=tuple(fake_tb),
        autostart="manual",
    )
    cfg = Config(
        server=ServerSpec(port=0, poll_interval=0.1, start_stagger=0.0),
        boards=(board,),
        path=tmp_path / "config.toml",
    )
    supervisor = Supervisor(cfg)
    supervisor.start()
    cancelled: list[int] = []

    def on_cancel(job: int):
        cancelled.append(job)
        if job == 99:
            raise QueueError("not_found: job 99 not found", "not_found")
        return {"id": job, "state": "cancelled"}

    server = httpd.serve(supervisor, "127.0.0.1", 0, None, on_cancel)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        _, _, body = fetch(f"{base}/api/queue/7/cancel", method="POST")
        assert json.loads(body) == {"ok": True, "job": 7, "action": "cancel"}
        assert cancelled == [7]

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            fetch(f"{base}/api/queue/99/cancel", method="POST")
        assert excinfo.value.code == 404
        assert json.loads(excinfo.value.read())["error"] == "not_found: job 99 not found"
        assert cancelled == [7, 99]
    finally:
        server.shutdown()
        server.server_close()
        supervisor.shutdown()



def test_cross_origin_requests_are_refused(stack):
    base, _, _ = stack
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        fetch(f"{base}/api/boards/fake/start", method="POST",
              headers={"Origin": "http://evil.example"})
    assert excinfo.value.code == 403


def test_event_stream_pushes_state(stack):
    base, supervisor, _ = stack
    request = urllib.request.Request(f"{base}/api/events")
    with urllib.request.urlopen(request, timeout=15) as response:
        assert response.headers["Content-Type"] == "text/event-stream"
        line = response.readline().decode()
        assert line.startswith("data: ")
        payload = json.loads(line[len("data: ") :])
        assert payload["boards"][0]["name"] == "fake"

        supervisor.request("start", "fake")
        frames = []
        while len(frames) < 1:
            raw = response.readline().decode()
            if raw.startswith("data: "):
                frames.append(json.loads(raw[len("data: ") :]))
        assert frames[0]["boards"][0]["state"] in ("starting", "running")


def test_responses_carry_anti_framing_headers(stack):
    base, _, _ = stack
    for url in (f"{base}/", f"{base}/api/state"):
        _, headers, _ = fetch(url)
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        assert headers["X-Content-Type-Options"] == "nosniff"


def test_bad_lines_parameter_is_a_400(stack):
    base, _, _ = stack
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        fetch(f"{base}/api/boards/fake/logs?lines=abc")
    assert excinfo.value.code == 400


def test_missing_host_header_is_refused(stack):
    base, _, _ = stack
    host, port = base.removeprefix("http://").split(":")


    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.putrequest("POST", "/api/boards/fake/stop", skip_host=True, skip_accept_encoding=True)
    conn.putheader("Content-Length", "0")
    conn.endheaders()
    assert conn.getresponse().status == 403
    conn.close()


def test_rejected_post_keeps_the_connection_usable(stack):
    """A 403 must consume the body, or the next request on the socket desyncs."""
    base, _, _ = stack
    host, port = base.removeprefix("http://").split(":")


    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    conn.request("POST", "/api/boards/fake/stop", body=b'{"x":1}',
                 headers={"Origin": "http://evil.example", "Content-Type": "application/json"})
    first = conn.getresponse()
    assert first.status == 403
    first.read()
    conn.request("GET", "/healthz")
    second = conn.getresponse()
    assert second.status == 200 and json.loads(second.read())["ok"] is True
    conn.close()


def test_healthz(stack):
    base, _, _ = stack
    _, _, body = fetch(f"{base}/healthz")
    assert json.loads(body)["ok"] is True


def test_state_carries_the_queue_snapshot(stack):
    """The queue rides in the same payload, so the dashboard needs one stream."""
    base, supervisor, _ = stack
    _, _, body = fetch(f"{base}/api/state")
    assert json.loads(body)["queue"] is None  # no subscriber wired into this stack

    from tensorwatch.queue import QueueJob, QueueSnapshot

    job = QueueJob(
        id=7, name="job", state="running", priority=0, reason="", cwd="/repos/x",
        project="x", boards=("fake",), board="fake", since=1.0, queued_at=1.0,
        time_limit=None, attempts="1/1",
    )
    supervisor.set_queue_source(
        lambda: QueueSnapshot(connected=True, running=(job,), active_leases=1, effective_limit=1)
    )
    _, _, body = fetch(f"{base}/api/state")
    payload = json.loads(body)
    assert payload["queue"]["connected"] is True
    assert payload["queue"]["running"][0]["board"] == "fake"
    assert payload["server"]["queue_visible"] == 5
