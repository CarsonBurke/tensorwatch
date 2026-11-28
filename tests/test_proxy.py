"""Board origins: Host routing, the cross-origin rules, and streaming relays."""

from __future__ import annotations

import gzip
import http.client
import http.server
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tensorwatch import httpd, proxy
from tensorwatch.config import BoardSpec, Config, RemoteSpec, ServerSpec
from tensorwatch.supervisor import Supervisor

# ------------------------------------------------------------------ host names


@pytest.mark.parametrize(
    ("host", "route"),
    [
        ("localhost:6005", proxy.DASHBOARD),
        ("127.0.0.1:6005", proxy.DASHBOARD),
        ("[::1]:6005", proxy.DASHBOARD),
        ("LOCALHOST", proxy.DASHBOARD),
        ("cleanrl.localhost:6005", proxy.Route("cleanrl")),
        ("CleanRL.localhost:6005", proxy.Route("cleanrl")),
        ("cleanrl.localhost.:6005", proxy.Route("cleanrl")),
        ("clean-rl.gpu.localhost:7005", proxy.Route("clean-rl", "gpu")),
    ],
)
def test_loopback_names_route_to_the_dashboard_or_a_board(host, route):
    assert proxy.classify(host) == route


@pytest.mark.parametrize(
    "host",
    [
        None, "", "evil.example:6005",  # a DNS-rebound name carries its own domain
        "localhost.evil.example", "cleanrl.localhost.evil.example", "10.0.0.5:6005",
        "a.b.c.localhost",  # deeper than board.remote
        "-bad.localhost", "bad-.localhost", "under_score.localhost", "x" * 64 + ".localhost",
        "localhost:0", "localhost:99999", "localhost:", "localhost:12ab", "::1:6005", "[::1",
        "[::1]x", "..localhost",
    ],
)
def test_anything_else_is_refused(host):
    assert proxy.classify(host) is None


def test_same_origin_compares_scheme_host_and_port():
    assert proxy.same_origin("http://cleanrl.localhost:6005", "cleanrl.localhost:6005")
    assert proxy.same_origin("http://localhost", "localhost:80")
    assert proxy.same_origin("http://[::1]:6005", "[::1]:6005")
    assert not proxy.same_origin("http://cleanrl.localhost:6006", "cleanrl.localhost:6005")
    assert not proxy.same_origin("http://other.localhost:6005", "cleanrl.localhost:6005")
    assert not proxy.same_origin("https://cleanrl.localhost:6005", "cleanrl.localhost:6005")
    assert not proxy.same_origin("null", "cleanrl.localhost:6005")
    assert not proxy.same_origin("http://x:bad", "localhost:6005")


@pytest.mark.parametrize(
    ("method", "headers", "allowed"),
    [
        # The board's own page, and tools that are not browsers.
        ("GET", {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}, True),
        ("POST", {"Origin": "http://b.localhost:6005", "Sec-Fetch-Site": "same-origin"}, True),
        ("GET", {}, True),
        ("GET", {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"}, True),
        # The dashboard framing it, or a link opening it, is a navigation.
        ("GET", {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}, True),
        # Another page, including another board, may not read or drive it.
        ("GET", {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"}, False),
        ("GET", {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors"}, False),
        ("POST", {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}, False),
        ("POST", {"Origin": "http://evil.example"}, False),
        ("GET", {"Origin": "http://other.localhost:6005", "Sec-Fetch-Site": "same-site"}, False),
    ],
)
def test_board_requests_from_other_pages_are_refused(method, headers, allowed):
    assert proxy.board_request_allowed(method, headers, "b.localhost:6005") is allowed


def test_board_names_become_unique_dns_labels():
    labels = proxy.assign_labels(["cleanrl", "CleanRL", "a_b.c", "a-b-c", "--", "x" * 80])
    assert labels == {
        "cleanrl": "cleanrl",
        "CleanRL": "cleanrl-2",
        "a_b.c": "a-b-c",
        "a-b-c": "a-b-c-2",
        "--": "board",
        "x" * 80: "x" * 63,
    }
    assert all(proxy.LABEL_RE.match(label) for label in labels.values())


# ------------------------------------------------------------- upstream boards


@dataclass
class FakeBoard:
    """A stand-in TensorBoard that records what reached it."""

    server: http.server.ThreadingHTTPServer
    seen: list[dict] = field(default_factory=list)
    connections: int = 0
    #: Set by a test to let a streaming response finish.
    release: threading.Event = field(default_factory=threading.Event)

    @property
    def port(self) -> int:
        return self.server.server_address[1]


BIG = bytes(range(256)) * (4 * 1024 * 1024 // 256)  # 4 MiB, incompressible enough


def _fake_board() -> FakeBoard:
    board: FakeBoard

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            # Like the manager, so a test of the relay does not time this server's Nagle.
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            board.connections += 1

        def log_message(self, *args):
            pass

        def _record(self, body: bytes = b"") -> None:
            board.seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                               "body": body})

        def _send(self, status, body: bytes, headers=(), close=False):
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            self._record()
            if self.path == "/ok":
                return self._send(200, b"ok", [("X-Board", "yes"), ("Keep-Alive", "timeout=5")])
            if self.path == "/werkzeug":
                # TensorBoard's server closes every connection.
                return self._send(200, b"closing", close=True)
            if self.path == "/gzip":
                payload = gzip.compress(b"scalars " * 1000)
                return self._send(200, payload, [("Content-Encoding", "gzip"), ("Content-Type", "application/json")])
            if self.path == "/big":
                return self._send(200, BIG, [("Content-Type", "application/octet-stream")])
            if self.path == "/redirect":
                host = self.headers["Host"]
                return self._send(302, b"", [("Location", f"http://{host}/ok")])
            if self.path == "/bad-redirect":
                return self._send(302, b"", [("Location", "http://[x/ok")])
            if self.path == "/early-hints":
                self.wfile.write(b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>\r\n\r\n")
                return self._send(200, b"final")
            if self.path == "/plus-length":
                self.send_response(200)
                self.send_header("Content-Length", "+3")
                self.end_headers()
                self.wfile.write(b"abc")
                return
            if self.path == "/stream":
                # A body that ends with the connection, written in two parts.
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                self.wfile.write(b"first;")
                self.wfile.flush()
                board.release.wait(10)
                self.wfile.write(b"second")
                return
            if self.path == "/chunked":
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for part in (b"one;", b"two;", b"three"):
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                    time.sleep(0.05)
                self.wfile.write(b"0\r\n\r\n")
                return
            if self.path == "/longpoll":
                time.sleep(1.0)
                return self._send(200, b"late")
            return self._send(404, b"no such page")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            self._record(body)
            reply = json.dumps({"got": body.decode(), "type": self.headers.get("Content-Type")}).encode()
            return self._send(200, reply, [("Content-Type", "application/json")])

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    board = FakeBoard(server)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    return board


@dataclass
class StubLink:
    """What httpd needs of a remote link (:class:`tensorwatch.remote.RemoteLink`)."""

    spec: RemoteSpec
    upstream: proxy.Upstream | None
    retried: int = 0

    def unavailable(self) -> str:
        return f"{self.spec.title} is not connected."

    def retry(self) -> None:
        self.retried += 1


class StubFederation:
    def __init__(self) -> None:
        self.links: dict[str, StubLink] = {}

    def link(self, name: str):
        return self.links.get(name)


def _manager(tmp_path: Path, boards: tuple[BoardSpec, ...], remotes=(), federation=None, name="here"):
    cfg = Config(server=ServerSpec(port=0), boards=boards, remotes=tuple(remotes),
                 path=tmp_path / f"{name}.toml")
    supervisor = Supervisor(cfg)  # not started: nothing spawns, routing still works
    server = httpd.serve(supervisor, "127.0.0.1", 0, federation=federation)
    return supervisor, server


@pytest.fixture
def boards(home, tmp_path):
    """A manager on this machine with two boards, and one remote manager behind a link."""
    clean, other = _fake_board(), _fake_board()
    gpu_board = _fake_board()
    local_specs = (
        BoardSpec(name="CleanRL", port=clean.port, logdir=tmp_path),
        BoardSpec(name="other_board", port=other.port, logdir=tmp_path),
    )
    remote_sup, remote_server = _manager(
        tmp_path, (BoardSpec(name="gpu-runs", port=gpu_board.port, logdir=tmp_path),), name="there")
    federation = StubFederation()
    spec = RemoteSpec(name="gpu", ssh="gpu-host", label="GPU box")
    federation.links["gpu"] = StubLink(
        spec, proxy.Upstream(address=("127.0.0.1", remote_server.server_address[1])))
    federation.links["down"] = StubLink(RemoteSpec(name="down", ssh="nowhere"), None)
    sup, server = _manager(tmp_path, local_specs, remotes=[spec], federation=federation)
    port = server.server_address[1]
    try:
        yield {
            "port": port, "clean": clean, "other": other, "gpu": gpu_board, "federation": federation,
            "supervisor": sup, "remote_supervisor": remote_sup,
        }
    finally:
        for item in (server, remote_server):
            item.shutdown()
            item.server_close()
        for board in (clean, other, gpu_board):
            board.release.set()
            board.server.shutdown()
            board.server.server_close()


def request(port, host, path="/ok", method="GET", headers=None, body=None, conn=None):
    """One request as a browser on this machine would send it, by Host."""
    own = conn is None
    conn = conn or http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    conn.putheader("Host", f"{host}:{port}")
    for key, value in (headers or {}).items():
        conn.putheader(key, value)
    if body is not None:
        conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body)
    response = conn.getresponse()
    data = response.read()
    if own:
        conn.close()
    return response, data


def test_each_board_answers_at_its_own_origin(boards):
    port = boards["port"]
    response, body = request(port, "cleanrl.localhost")
    assert (response.status, body) == (200, b"ok")
    assert response.getheader("X-Board") == "yes"
    # Hop-by-hop fields describe one connection, so they stop at the relay.
    assert response.getheader("Keep-Alive") is None
    assert len(boards["clean"].seen) == 1 and not boards["other"].seen

    response, body = request(port, "other-board.localhost")
    assert (response.status, body) == (200, b"ok")
    assert len(boards["other"].seen) == 1
    # The board sees the name it is reached by, as TensorBoard would directly.
    assert boards["other"].seen[0]["headers"]["Host"] == f"other-board.localhost:{port}"


def test_an_unknown_board_and_a_foreign_host_are_refused(boards):
    port = boards["port"]
    response, body = request(port, "nothing.localhost")
    assert response.status == 404 and b"No board is registered" in body
    # DNS rebinding: the attacker's name reaches this port, but nothing answers it.
    for host in ("evil.example", "cleanrl.localhost.evil.example"):
        response, _ = request(port, host)
        assert response.status == 403
        response, _ = request(port, host, path="/api/state")
        assert response.status == 403
    assert not boards["clean"].seen


def test_a_board_origin_never_reaches_the_control_api(boards):
    port = boards["port"]
    # On a board's origin, /api/... is the board's own path, not TensorWatch's.
    response, body = request(port, "cleanrl.localhost", path="/api/boards/CleanRL/stop", method="POST", body=b"")
    assert response.status == 200 and boards["clean"].seen[-1]["path"] == "/api/boards/CleanRL/stop"
    # And from a board's page, the dashboard's API refuses the cross-origin call.
    response, _ = request(port, "127.0.0.1", path="/api/boards/CleanRL/stop", method="POST", body=b"",
                          headers={"Origin": f"http://cleanrl.localhost:{port}"})
    assert response.status == 403
    response, _ = request(port, "localhost", path="/api/state",
                          headers={"Origin": f"http://cleanrl.localhost:{port}", "Sec-Fetch-Site": "cross-site"})
    assert response.status == 403


def test_the_dashboard_api_accepts_its_own_origin_on_any_port(boards):
    """A tunnel may put the dashboard on another port: its Origin follows the Host."""
    port = boards["port"]
    response, _ = request(port, "localhost", path="/api/state",
                          headers={"Origin": f"http://localhost:{port}", "Sec-Fetch-Site": "same-origin"})
    assert response.status == 200
    payload = json.loads(_)
    assert payload["api"] >= 2
    assert {b["name"]: b["host"] for b in payload["boards"]} == {"CleanRL": "cleanrl", "other_board": "other-board"}


def test_cross_site_reads_of_a_board_are_refused(boards):
    port = boards["port"]
    response, _ = request(port, "cleanrl.localhost", path="/ok",
                          headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors",
                                   "Origin": "http://evil.example"})
    assert response.status == 403
    # Framing it from the dashboard is a navigation, and allowed.
    response, body = request(port, "cleanrl.localhost", path="/ok",
                             headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                                      "Sec-Fetch-Dest": "iframe"})
    assert (response.status, body) == (200, b"ok")
    # Only the dashboard on this port may frame it; CSP has no IPv6 literals.
    policy = response.getheader("Content-Security-Policy")
    assert policy == f"frame-ancestors 'self' http://localhost:{port} http://127.0.0.1:{port}"


def test_post_bodies_and_compressed_responses_pass_through_untouched(boards):
    port = boards["port"]
    form = b"requests=" + b"%5B%7B%22plugin%22%3A%22scalars%22%7D%5D" * 50
    response, body = request(port, "cleanrl.localhost", path="/data", method="POST", body=form,
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status == 200
    assert json.loads(body) == {"got": form.decode(), "type": "application/x-www-form-urlencoded"}

    response, body = request(port, "cleanrl.localhost", path="/gzip", headers={"Accept-Encoding": "gzip"})
    assert response.getheader("Content-Encoding") == "gzip"
    assert int(response.getheader("Content-Length")) == len(body)
    assert gzip.decompress(body) == b"scalars " * 1000
    assert boards["clean"].seen[-1]["headers"]["Accept-Encoding"] == "gzip"

    response, body = request(port, "cleanrl.localhost", path="/big")
    assert body == BIG

    response, body = request(port, "cleanrl.localhost", path="/big", method="HEAD")
    assert response.status == 200 and body == b""
    assert response.getheader("Content-Length") == str(len(BIG))


def _raw(port: int, data: bytes) -> bytes:
    """Send raw bytes; everything the server answers until it closes."""
    with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
        sock.sendall(data)
        received = b""
        try:
            while chunk := sock.recv(65536):
                received += chunk
        except socket.timeout:
            pass
    return received


@pytest.mark.parametrize("host", ["nothing.localhost", "x.down.localhost", "x.unknown.localhost"])
def test_a_body_is_never_read_as_the_next_request(boards, host):
    """An error answered before the body was read would leave the body to be
    parsed as a request of its own, which no check has seen."""
    port = boards["port"]
    smuggled = (b"POST /api/reload HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n"
                b"GET /api/state HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    request_ = (b"POST /data HTTP/1.1\r\nHost: %s:%d\r\nContent-Length: %d\r\n\r\n"
                % (host.encode(), port, len(smuggled)) + smuggled)
    received = _raw(port, request_)
    assert received.count(b"HTTP/1.1 ") == 1, received[:400]


def test_cross_origin_api_requests_are_refused_unread(boards):
    """A page elsewhere cannot make the dashboard buffer a large body."""
    port = boards["port"]
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"POST /api/reload HTTP/1.1\r\nHost: localhost:%d\r\nOrigin: http://evil.example\r\n"
                     b"Content-Length: 67108864\r\n\r\n" % port)
        assert sock.recv(4096).startswith(b"HTTP/1.1 403")
    # And the dashboard's own requests are small: refused from the header alone.
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"POST /api/reload HTTP/1.1\r\nHost: localhost:%d\r\nOrigin: http://localhost:%d\r\n"
                     b"Content-Length: %d\r\n\r\n" % (port, port, 2 << 20))
        assert sock.recv(4096).startswith(b"HTTP/1.1 413")


def test_a_get_with_a_body_closes_the_connection(boards):
    port = boards["port"]
    received = _raw(port, b"GET /healthz HTTP/1.1\r\nHost: localhost:%d\r\nContent-Length: 50\r\n\r\n"
                    b"GET /api/state HTTP/1.1\r\nHost: localhost\r\n\r\n" % port)
    assert received.count(b"HTTP/1.1 ") == 1


def test_a_chunked_request_body_is_refused_rather_than_misread(boards):
    port = boards["port"]
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"POST /data HTTP/1.1\r\nHost: cleanrl.localhost:%d\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n" % port)
        assert sock.recv(4096).startswith(b"HTTP/1.1 411")


def test_responses_stream_as_the_board_writes_them(boards):
    port = boards["port"]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("GET", "/stream", skip_host=True)
    conn.putheader("Host", f"cleanrl.localhost:{port}")
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 200
    # The first part arrives while the board is still holding the second.
    assert response.read1(64) == b"first;"
    boards["clean"].release.set()
    assert response.read() == b"second"
    conn.close()

    response, body = request(port, "cleanrl.localhost", path="/chunked")
    assert body == b"one;two;three"


def test_long_polls_and_parallel_requests_do_not_block_each_other(boards):
    port = boards["port"]
    results = {}

    def slow():
        results["slow"] = request(port, "cleanrl.localhost", path="/longpoll")[1]

    thread = threading.Thread(target=slow)
    started = time.monotonic()
    thread.start()
    for _ in range(5):
        assert request(port, "cleanrl.localhost")[1] == b"ok"
    fast = time.monotonic() - started
    thread.join(10)
    assert results["slow"] == b"late"
    assert fast < 0.9, f"quick requests waited for the long poll ({fast:.2f}s)"


def test_browser_keep_alive_and_upstream_reuse(boards):
    port = boards["port"]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    for _ in range(4):
        response, body = request(port, "cleanrl.localhost", conn=conn)
        assert body == b"ok"
    conn.close()
    # Kept-alive upstream connections are reused; a board that closes each
    # one (TensorBoard does) gets a fresh one per request instead. The first
    # of these still rides the pooled connection, which then closes.
    assert boards["clean"].connections == 1
    for _ in range(3):
        assert request(port, "cleanrl.localhost", path="/werkzeug")[1] == b"closing"
    assert boards["clean"].connections == 3


def test_small_requests_are_not_held_back_by_nagle(boards):
    """Header and body leave as separate writes; without NODELAY each one waits ~40 ms."""
    port = boards["port"]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    request(port, "cleanrl.localhost", conn=conn)
    started = time.monotonic()
    for _ in range(20):
        request(port, "cleanrl.localhost", path="/data", method="POST", body=b"x=1", conn=conn)
    per_request = (time.monotonic() - started) / 20
    conn.close()
    assert per_request < 0.02, f"{per_request * 1000:.1f} ms per request"


def test_a_board_that_is_down_gets_an_explanation(boards):
    port = boards["port"]
    boards["clean"].server.shutdown()
    boards["clean"].server.server_close()
    response, body = request(port, "cleanrl.localhost")
    assert response.status == 502
    assert b"CleanRL is not answering" in body
    assert response.getheader("Content-Type").startswith("text/html")


def test_board_traffic_does_not_count_as_a_window(boards):
    """A raw board tab must not keep TensorWatch alive; the dashboard does that."""
    supervisor = boards["supervisor"]
    supervisor._last_seen = supervisor._last_window = time.monotonic() - 50
    request(boards["port"], "cleanrl.localhost")
    assert supervisor.idle_for() >= 50


# -------------------------------------------------------------------- remotes


def test_a_remote_board_is_relayed_through_its_link(boards):
    port = boards["port"]
    response, body = request(port, "gpu-runs.gpu.localhost", headers={
        "Origin": f"http://gpu-runs.gpu.localhost:{port}", "Sec-Fetch-Site": "same-origin",
        "Referer": f"http://gpu-runs.gpu.localhost:{port}/", "Cookie": "kept=1",
    })
    assert (response.status, body) == (200, b"ok")
    # One framing policy, for the browser's dashboard: the remote adds none of
    # its own, which would name a port the browser never used and block it.
    policies = [v for k, v in response.getheaders() if k.lower() == "content-security-policy"]
    assert policies == [f"frame-ancestors 'self' http://localhost:{port} http://127.0.0.1:{port}"]
    seen = boards["gpu"].seen[-1]["headers"]
    # Over there it is that machine's own board origin, asked by a peer.
    assert seen["Host"] == "gpu-runs.localhost"
    assert "Origin" not in seen and "Referer" not in seen and "Sec-Fetch-Site" not in seen
    assert httpd.PEER_HEADER not in seen  # the remote manager does not pass its own marker on
    # Redirects come back under the name the browser used.
    response, _ = request(port, "gpu-runs.gpu.localhost", path="/redirect")
    assert response.status == 302
    assert response.getheader("Location") == f"http://gpu-runs.gpu.localhost:{port}/ok"


def test_odd_upstream_responses_are_not_passed_on_as_is(boards):
    port = boards["port"]
    # An interim response is not the answer, nor is the connection reusable.
    response, _ = request(port, "cleanrl.localhost", path="/early-hints")
    assert response.status == 502
    assert request(port, "cleanrl.localhost")[1] == b"ok"
    # A length only a lenient parser accepts is not forwarded verbatim.
    response, body = request(port, "cleanrl.localhost", path="/plus-length")
    assert body == b"abc" and response.getheader("Content-Length") in (None, "3")
    # A malformed redirect from a remote is passed on, not a crash.
    response, _ = request(port, "gpu-runs.gpu.localhost", path="/bad-redirect")
    assert response.status == 302 and response.getheader("Location") == "http://[x/ok"


def test_remote_board_traffic_is_presence_over_there_but_not_a_window(boards):
    remote = boards["remote_supervisor"]
    remote._last_seen = remote._last_window = time.monotonic() - 50
    request(boards["port"], "localhost", path="/api/remotes/gpu/api/state")
    assert remote.idle_for() < 5  # the link keeps the remote up...
    assert remote.window_idle_for() >= 50  # ...without it wanting its own remotes


def test_the_remote_api_is_reachable_but_not_its_stream(boards):
    port = boards["port"]
    response, body = request(port, "127.0.0.1", path="/api/remotes/gpu/api/state",
                             headers={"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"})
    assert response.status == 200
    assert [b["name"] for b in json.loads(body)["boards"]] == ["gpu-runs"]
    response, _ = request(port, "127.0.0.1", path="/api/remotes/gpu/api/events")
    assert response.status == 404
    response, _ = request(port, "127.0.0.1", path="/api/remotes/nope/api/state")
    assert response.status == 404
    # A board page cannot use the remote API either.
    response, _ = request(port, "127.0.0.1", path="/api/remotes/gpu/api/boards/gpu-runs/stop", method="POST",
                          body=b"", headers={"Origin": f"http://gpu-runs.gpu.localhost:{port}"})
    assert response.status == 403


def test_a_disconnected_remote_explains_itself_and_can_be_retried(boards):
    port = boards["port"]
    response, body = request(port, "anything.down.localhost")
    assert response.status == 503 and b"down is not connected" in body
    response, body = request(port, "x.unknown.localhost")
    assert response.status == 404
    response, body = request(port, "localhost", path="/api/remotes/down/api/state")
    assert response.status == 503
    response, body = request(port, "localhost", path="/api/remotes/down/retry", method="POST", body=b"")
    assert response.status == 200
    assert boards["federation"].links["down"].retried == 1
