from __future__ import annotations

import json
import time
import http.client
import urllib.error
import urllib.request

import pytest

from conftest import wait_until
from tensorwatch import cli, httpd
from tensorwatch.config import BoardSpec, Config, ServerSpec, load
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


def fetch(url: str, method: str = "GET", headers: dict[str, str] = {}, data=None):
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data).encode()
        headers = {"Content-Type": "application/json", **headers}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.headers, response.read()


def test_serves_dashboard_and_assets(stack):
    base, _, _ = stack
    status, headers, body = fetch(f"{base}/")
    assert status == 200
    assert b"<title>TensorWatch</title>" in body
    assert b'href="/static/icon-32.png"' in body
    assert b'id="add-board"' in body
    assert b'id="add-board-dialog"' in body
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


@pytest.fixture
def autostart_stack(stack):
    """A second control server on the same supervisor, this one with the hook."""
    _, supervisor, _ = stack
    calls: list[tuple[str, str]] = []
    server = httpd.serve(
        supervisor, "127.0.0.1", 0, None, None, None,
        lambda name, mode: calls.append((name, mode)),
    )
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", calls
    finally:
        server.shutdown()
        server.server_close()


def test_autostart_endpoint_hands_the_mode_to_the_registry(autostart_stack):
    base, calls = autostart_stack
    status, _, body = fetch(f"{base}/api/boards/fake/autostart", method="POST",
                            data={"mode": "on_demand"})
    assert status == 200
    assert json.loads(body) == {"ok": True, "board": "fake", "autostart": "on_demand"}
    assert calls == [("fake", "on_demand")]


def test_autostart_endpoint_rejects_bad_modes_and_boards(autostart_stack):
    base, calls = autostart_stack
    for name, data, status in (
        ("fake", {"mode": "whenever"}, 400),
        ("fake", {}, 400),
        ("fake", b"not-json", 400),
        ("nope", {"mode": "always"}, 404),
    ):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            fetch(f"{base}/api/boards/{name}/autostart", method="POST", data=data)
        assert excinfo.value.code == status
    assert calls == []


def test_autostart_unavailable_without_a_hook(stack):
    base, _, _ = stack
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        fetch(f"{base}/api/boards/fake/autostart", method="POST", data={"mode": "always"})
    assert excinfo.value.code == 501


def test_add_board_unavailable_without_a_hook(stack):
    base, _, _ = stack
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        fetch(f"{base}/api/boards", method="POST", data={"path": "/tmp"})
    assert excinfo.value.code == 501


def test_add_board_rejects_bad_bodies(stack):
    base, _, _ = stack
    for data, status in (
        (b"not-json", 400),
        ({}, 400),
        ({"path": ""}, 400),
        ({"path": 12}, 400),
    ):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            fetch(f"{base}/api/boards", method="POST", data=data)
        assert excinfo.value.code == status


def test_add_board_registers_a_logdir(home, tmp_path):

    logdir = tmp_path / "proj" / "runs"
    logdir.mkdir(parents=True)
    cfg = Config(
        server=ServerSpec(port=0, poll_interval=0.1, start_stagger=0.0),
        boards=(),
        path=tmp_path / "config.toml",
    )
    supervisor = Supervisor(cfg)
    supervisor.start()

    def on_add(path: str):
        entry = cli.add_board(path)
        supervisor.reload(load())
        return {"ok": True, "board": entry["name"], "port": entry["port"]}

    server = httpd.serve(supervisor, "127.0.0.1", 0, on_add=on_add)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        _, _, body = fetch(f"{base}/api/boards", method="POST", data={"path": str(logdir)})
        payload = json.loads(body)
        assert payload["ok"] is True
        assert payload["board"] == "proj"
        assert load().board("proj").logdir == logdir.resolve()

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            fetch(f"{base}/api/boards", method="POST", data={"path": str(logdir)})
        assert excinfo.value.code == 400
        assert "already registered" in json.loads(excinfo.value.read())["error"]
    finally:
        server.shutdown()
        server.server_close()
        supervisor.shutdown()


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


@pytest.fixture
def registry_stack(home, tmp_path):
    """A manager whose registry lives on disk, wired the way `serve` wires it."""
    path = tmp_path / "config.toml"
    supervisor = Supervisor(
        Config(
            server=ServerSpec(port=0, poll_interval=0.1, start_stagger=0.0),
            boards=(),
            path=path,
        )
    )
    supervisor.start()
    ops = cli.RegistryOps(path, lambda: supervisor.reload(load(path)), lambda: load(path))
    server = httpd.serve(
        supervisor,
        "127.0.0.1",
        0,
        on_add=lambda logdir, **options: ops.add(logdir, autostart="manual", **options),
        on_remove=ops.remove,
        on_restore=ops.restore,
        on_complete=lambda prefix: [{"path": prefix + "x", "events": False}],
        on_suggest=lambda: [{"path": "/s", "name": "s", "events": 1}],
    )
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, tmp_path
    finally:
        server.shutdown()
        server.server_close()
        supervisor.shutdown()


def call(url: str, method: str = "GET", data=None, headers: dict[str, str] = {}):
    """Like fetch, but an HTTP error is an answer rather than an exception."""
    try:
        status, _, body = fetch(url, method=method, data=data, headers=headers)
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    return status, json.loads(body)


def add_three(base: str, tmp_path) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name / "runs").mkdir(parents=True)
        assert call(f"{base}/api/boards", "POST", {"path": str(tmp_path / name / "runs")})[0] == 200


def test_add_takes_a_name_and_rejects_bad_fields(registry_stack):
    base, tmp_path = registry_stack
    (tmp_path / "proj" / "runs").mkdir(parents=True)
    status, payload = call(
        f"{base}/api/boards", "POST", {"path": str(tmp_path / "proj" / "runs"), "name": "mine"}
    )
    assert (status, payload["board"], payload["events"]) == (200, "mine", False)

    # Blank optional fields are what an untouched form sends.
    (tmp_path / "other" / "runs").mkdir(parents=True)
    status, payload = call(
        f"{base}/api/boards", "POST",
        {"path": str(tmp_path / "other" / "runs"), "name": "", "autostart": ""},
    )
    assert (status, payload["board"]) == (200, "other")

    for bad in ({"path": "/x", "autostart": "sometimes"}, {"path": "/x", "name": 7}, {"name": "x"}):
        assert call(f"{base}/api/boards", "POST", bad)[0] == 400


def test_remove_then_undo_round_trips_the_registry(registry_stack):
    base, tmp_path = registry_stack
    add_three(base, tmp_path)
    before = (tmp_path / "config.toml").read_text()

    status, removed = call(f"{base}/api/boards/b", "DELETE")
    assert status == 200 and removed["board"] == "b"
    # The answer carries a token and nothing of the registry.
    assert set(removed) == {"ok", "board", "undo"}
    assert [board.name for board in load().boards] == ["a", "c"]
    # The supervisor picks the new registry up on its own thread.
    assert wait_until(
        lambda: [b["name"] for b in call(f"{base}/api/state")[1]["boards"]] == ["a", "c"]
    )

    status, restored = call(f"{base}/api/boards", "POST", {"undo": removed["undo"]})
    assert (status, restored["board"]) == (200, "b")
    assert (tmp_path / "config.toml").read_text() == before

    # Undoing twice must not register the board twice.
    status, again = call(f"{base}/api/boards", "POST", {"undo": removed["undo"]})
    assert status == 400 and "no longer be undone" in again["error"]


def test_the_api_cannot_be_handed_registry_text(registry_stack):
    base, tmp_path = registry_stack
    add_three(base, tmp_path)
    before = (tmp_path / "config.toml").read_text()
    block = '[[board]]\nname = "evil"\nlogdir = "/x"\ncommand = ["sh", "-c", "id"]\n'
    for payload in (
        {"restore": {"block": block, "line": 0}},  # the shape an earlier design accepted
        {"undo": block},
        {"undo": {"block": block}},
        {"undo": ""},
    ):
        assert call(f"{base}/api/boards", "POST", payload)[0] == 400
    assert (tmp_path / "config.toml").read_text() == before

    # Keys other than the ones the form sends are ignored, not written.
    (tmp_path / "d" / "runs").mkdir(parents=True)
    status, _ = call(
        f"{base}/api/boards", "POST",
        {"path": str(tmp_path / "d" / "runs"), "command": ["sh", "-c", "id"], "env": {"X": "1"}},
    )
    assert status == 200
    assert load().board("d").command == ("tensorboard",)
    assert load().board("d").env == {}


def test_remove_rejects_what_it_cannot_do(registry_stack):
    base, _ = registry_stack
    assert call(f"{base}/api/boards/ghost", "DELETE")[0] == 404
    assert call(f"{base}/api/boards", "DELETE")[0] == 404


def test_a_delete_body_is_not_read_as_the_next_request(registry_stack):
    base, tmp_path = registry_stack
    add_three(base, tmp_path)
    host, port = base.removeprefix("http://").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        conn.request("DELETE", "/api/boards/a", body=b"GET /api/boards/b HTTP/1.1\r\n\r\n")
        assert conn.getresponse().read() and True
        conn.request("GET", "/healthz")
        response = conn.getresponse()
        assert (response.status, json.loads(response.read())["ok"]) == (200, True)
    finally:
        conn.close()
    assert [board.name for board in load().boards] == ["b", "c"]


def test_registry_edits_are_refused_from_other_sites(registry_stack):
    base, tmp_path = registry_stack
    add_three(base, tmp_path)
    for headers in ({"Origin": "http://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
        assert call(f"{base}/api/boards/a", "DELETE", headers=headers)[0] == 403
    assert load().board("a") is not None


def test_filesystem_lookups_are_refused_to_other_sites_but_pages_still_load(registry_stack):
    base, _ = registry_stack
    # An <img> or <script> elsewhere sends no Origin, but the browser labels it.
    for site in ("cross-site", "same-site"):
        for path in ("/api/paths?prefix=/", "/api/suggestions", "/api/state"):
            assert call(f"{base}{path}", headers={"Sec-Fetch-Site": site})[0] == 403
    for site in ("same-origin", "none"):
        assert call(f"{base}/api/suggestions", headers={"Sec-Fetch-Site": site})[0] == 200
    # Following a link to the dashboard from another site is ordinary navigation.
    status, _, body = fetch(f"{base}/", headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 200 and b"TensorWatch" in body


def test_path_completion_and_suggestions_are_served(registry_stack):
    base, _ = registry_stack
    assert call(f"{base}/api/paths?prefix=%7E%2Fwork%2F")[1] == {
        "paths": [{"path": "~/work/x", "events": False}]
    }
    assert call(f"{base}/api/paths")[1] == {"paths": [{"path": "x", "events": False}]}
    assert call(f"{base}/api/suggestions")[1] == {
        "suggestions": [{"path": "/s", "name": "s", "events": 1}]
    }


def test_registry_endpoints_are_unavailable_without_hooks(stack):
    base, _, _ = stack
    assert call(f"{base}/api/boards/fake", "DELETE")[0] == 501
    assert call(f"{base}/api/boards", "POST", {"undo": "token"})[0] == 501
    assert call(f"{base}/api/paths?prefix=/")[0] == 501
    assert call(f"{base}/api/suggestions")[0] == 501
