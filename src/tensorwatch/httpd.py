"""The dashboard port: dashboard assets, a small JSON/SSE API, and the boards.

What a request is for depends on its Host header. The loopback names
(``127.0.0.1``, ``localhost``, ``[::1]``) reach the dashboard and its API; a
board's own name (``cleanrl.localhost``, or ``cleanrl.nvidia-desktop.localhost``
for a remote one) reaches that board, relayed by :mod:`tensorwatch.proxy`. The
two never mix: a board's page, whatever it contains, cannot call the API.
"""

from __future__ import annotations

import json
import mimetypes
import queue
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import proxy
from .config import AUTOSTART_MODES, BoardSpec
from .queue import QueueError
from .supervisor import Supervisor

WEB_ROOT = Path(__file__).parent / "web"
#: SSE keep-alive comment cadence; also bounds how fast a closed tab is noticed.
SSE_HEARTBEAT = 20.0
#: Marks a request that keeps this manager up but is not a window here: from
#: another TensorWatch that shows this one's boards, or from the CLI (see
#: ``Supervisor.touch``). Only windows make a manager connect to its remotes.
PEER_HEADER = "X-TensorWatch-Peer"
#: The control API is unauthenticated, so the dashboard must not be framable:
#: otherwise a page elsewhere can overlay it and harvest clicks on start/stop,
#: which arrive with a legitimate same-origin Origin.
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

#: Dashboard requests carry small JSON; anything bigger is refused unread.
API_BODY_LIMIT = 1024 * 1024


def board_framing(port: int | None) -> tuple[str, str]:
    """The policy added to every board response: only the dashboard on this
    port (or the board itself, for TensorBoard's plugin frames) may frame it.

    The port is the one the browser used, which a tunnel may have changed.
    Sent as a second policy, so the board's own still applies in full. CSP
    cannot name IPv6 literals, so a dashboard opened at ``[::1]`` cannot frame
    boards; the CLI and launcher open ``127.0.0.1``.
    """
    where = f":{port}" if port else ""
    return (
        "Content-Security-Policy",
        f"frame-ancestors 'self' http://localhost{where} http://127.0.0.1{where}",
    )


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        supervisor: Supervisor,
        on_reload: Callable[[], None] | None = None,
        on_cancel: Callable[[int], Any] | None = None,
        on_add: Callable[..., Any] | None = None,
        on_autostart: Callable[[str, str], Any] | None = None,
        on_remove: Callable[[str], Any] | None = None,
        on_restore: Callable[[str], Any] | None = None,
        on_complete: Callable[[str], Any] | None = None,
        on_suggest: Callable[[], Any] | None = None,
        sock: socket.socket | None = None,
        federation: Any = None,
    ) -> None:
        self.supervisor = supervisor
        #: The remote links (:class:`tensorwatch.remote.Federation`), if any.
        self.federation = federation
        self._upstreams: dict[tuple[str, int], proxy.Upstream] = {}
        self._upstreams_lock = threading.Lock()
        self.on_reload = on_reload
        self.on_cancel = on_cancel
        self.on_add = on_add
        self.on_autostart = on_autostart
        self.on_remove = on_remove
        self.on_restore = on_restore
        self.on_complete = on_complete
        self.on_suggest = on_suggest
        self.started_at = time.time()
        if sock is None:
            super().__init__(address, ControlHandler)
            return
        # Socket activation: systemd already bound and is listening on the
        # address, and keeps doing so after this process exits.
        super().__init__(sock.getsockname()[:2], ControlHandler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        self.server_address = sock.getsockname()

    def get_request(self):
        conn, address = super().get_request()
        # Responses go out as a header write and then body writes. Without
        # NODELAY, Nagle holds back the second small write until the browser's
        # delayed ACK (~40 ms) arrives, on every request. systemd's NoDelay=
        # covers socket activation; this covers a server that binds its own.
        if conn.family in (socket.AF_INET, socket.AF_INET6):
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return conn, address

    def board_upstream(self, spec: BoardSpec) -> proxy.Upstream:
        key = (spec.host, spec.port)
        with self._upstreams_lock:
            upstream = self._upstreams.get(key)
            if upstream is None:
                upstream = self._upstreams[key] = proxy.Upstream(address=key)
            return upstream

    def remote_link(self, name: str):
        return self.federation.link(name) if self.federation is not None else None

    def handle_error(self, request, client_address) -> None:  # noqa: D102 - quiet resets
        import sys
        import traceback

        exc = sys.exception()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        traceback.print_exc()


class ControlHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tensorwatch"
    sys_version = ""
    #: Where the Host header points; set once the request is parsed.
    route: proxy.Route | None = None

    # ------------------------------------------------------------------ helpers

    @property
    def supervisor(self) -> Supervisor:
        return self.server.supervisor  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # keep the journal readable
        return

    def _send(self, status: HTTPStatus, body: bytes, ctype: str, extra: dict[str, str] = {}) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self._send(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message}, status)

    def _queue_error(self, exc: QueueError) -> None:
        status = {
            "not_found": HTTPStatus.NOT_FOUND,
            "invalid_state": HTTPStatus.CONFLICT,
            "invalid_argument": HTTPStatus.BAD_REQUEST,
            "unsafe_resolution": HTTPStatus.CONFLICT,
            "missing_idempotency_key": HTTPStatus.BAD_REQUEST,
            "idempotency_conflict": HTTPStatus.CONFLICT,
        }.get(exc.code, HTTPStatus.BAD_GATEWAY)
        self._error(status, str(exc))

    def _local_only(self) -> bool:
        """Reject cross-origin and non-loopback-addressed requests.

        The dashboard is unauthenticated, so a page in another tab must not be
        able to drive it: any browser-attached request carries Origin, and only
        our own origin is accepted.  A missing Host header is refused rather than
        waved through - the check must fail closed.  The Origin is compared with
        the Host header rather than this server's port, so the dashboard also
        works through ``ssh -L 7005:127.0.0.1:6005``.

        A board origin is someone else's page as far as the API is concerned,
        though it is on loopback too.
        """
        if self.route is None or not self.route.dashboard:
            return False
        # A page elsewhere can still fire GETs without an Origin (an <img>, a
        # <script>); browsers label those, and nothing here is meant for them.
        # Following a link to the dashboard from elsewhere stays allowed.
        if self.path.startswith("/api/") and self.headers.get(
            "Sec-Fetch-Site", "same-origin"
        ) not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin and not proxy.same_origin(origin, self.headers.get("Host") or ""):
            return False
        self.supervisor.touch(peer=self._peer)
        return True

    @property
    def _peer(self) -> bool:
        return bool(self.headers.get(PEER_HEADER))

    # ------------------------------------------------------------------- routing

    def parse_request(self) -> bool:
        ok = super().parse_request()
        if ok:
            self.route = proxy.classify(self.headers.get("Host"))
        return ok

    def _dispatch(self) -> None:
        if self.route is not None and not self.route.dashboard:
            return self._board()
        if self.command in ("GET", "HEAD"):
            # Nothing here reads a GET body; one that came anyway must not be
            # parsed as the next request on this connection.
            if proxy.has_body(self):
                self.close_connection = True
            return self._get()
        if self.command == "POST":
            return self._post()
        if self.command == "DELETE":
            return self._delete()
        self.close_connection = True
        return self._error(HTTPStatus.METHOD_NOT_ALLOWED, f"{self.command} not allowed")

    def _api_body(self) -> bytes | None:
        """The body of a request to the dashboard's API, checked before it is read.

        Refusing first means a page elsewhere cannot make this server buffer
        anything; the connection then closes with the body unread.
        """
        if not self._local_only():
            self.close_connection = True
            self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
            return None
        return proxy.read_body(self, API_BODY_LIMIT)

    do_GET = do_HEAD = do_POST = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = _dispatch

    def _get(self) -> None:
        if not self._local_only():
            return self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            return self._static("index.html")
        if path.startswith("/boards/") and path.count("/") == 2:
            # A board in its own window. The page lives on this origin so that
            # it holds a dashboard stream: the manager must not exit under it.
            return self._static("board.html")
        if path == "/healthz":
            return self._json({"ok": True, "uptime": time.time() - self.server.started_at})
        if path.startswith("/static/"):
            return self._static(path[len("/static/") :])
        if path == "/api/state":
            return self._json(self.supervisor.state_json())
        if path == "/api/events":
            return self._events()
        if path == "/api/paths":
            prefix = (parse_qs(parsed.query, keep_blank_values=True).get("prefix") or [""])[0]
            return self._lookup("on_complete", "paths", prefix)
        if path == "/api/suggestions":
            return self._lookup("on_suggest", "suggestions")

        parts = path.strip("/").split("/")
        if parts[:2] == ["api", "remotes"]:
            return self._remote_api(parts, b"")
        if len(parts) == 4 and parts[:2] == ["api", "boards"] and parts[3] == "logs":
            try:
                lines = int((parse_qs(parsed.query).get("lines") or ["200"])[0])
            except ValueError:
                return self._error(HTTPStatus.BAD_REQUEST, "lines must be an integer")
            try:
                text = self.supervisor.log_tail(parts[2], max(1, min(lines, 5000)))
            except KeyError:
                return self._error(HTTPStatus.NOT_FOUND, f"no board {parts[2]!r}")
            except OSError as exc:
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return self._send(
                HTTPStatus.OK,
                text.encode(),
                "text/plain; charset=utf-8",
                {"Cache-Control": "no-store"},
            )
        return self._error(HTTPStatus.NOT_FOUND, "not found")

    def _lookup(self, hook_name: str, key: str, *args: str) -> None:
        """Answer a read-only question about the filesystem through a hook."""
        hook = getattr(self.server, hook_name)
        if hook is None:
            return self._error(HTTPStatus.NOT_IMPLEMENTED, f"{key} unavailable")
        try:
            return self._json({key: hook(*args)})
        except Exception as exc:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _delete(self) -> None:
        raw = self._api_body()
        if raw is None:
            return
        parts = urlparse(self.path).path.strip("/").split("/")
        if parts[:2] == ["api", "remotes"]:
            return self._remote_api(parts, raw)
        if len(parts) != 3 or parts[:2] != ["api", "boards"]:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        name = unquote(parts[2])
        hook = self.server.on_remove  # type: ignore[attr-defined]
        if hook is None:
            return self._error(HTTPStatus.NOT_IMPLEMENTED, "remove unavailable")
        if not any(board.name == name for board in self.supervisor.snapshot()):
            return self._error(HTTPStatus.NOT_FOUND, f"no board {name!r}")
        try:
            result = hook(name)
        except Exception as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        return self._json({"ok": True, "board": name, **result})

    def _post(self) -> None:
        raw = self._api_body()
        if raw is None:
            return
        parts = urlparse(self.path).path.strip("/").split("/")

        if parts[:2] == ["api", "remotes"]:
            return self._remote_api(parts, raw)

        if parts == ["api", "reload"]:
            reload_hook = self.server.on_reload  # type: ignore[attr-defined]
            if reload_hook is None:
                return self._error(HTTPStatus.NOT_IMPLEMENTED, "reload unavailable")
            try:
                reload_hook()
            except Exception as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return self._json(self.supervisor.state_json())

        if parts == ["api", "boards"]:
            return self._add_board(raw)

        if len(parts) == 4 and parts[:2] == ["api", "queue"] and parts[3] == "cancel":
            try:
                job = int(parts[2])
            except ValueError:
                return self._error(HTTPStatus.BAD_REQUEST, "job must be an integer")
            if job < 1:
                return self._error(HTTPStatus.BAD_REQUEST, "job must be a positive integer")
            cancel_hook = self.server.on_cancel  # type: ignore[attr-defined]
            if cancel_hook is None:
                return self._error(HTTPStatus.NOT_IMPLEMENTED, "queue cancel unavailable")
            try:
                cancel_hook(job)
            except QueueError as exc:
                return self._queue_error(exc)
            except Exception as exc:
                return self._error(HTTPStatus.BAD_GATEWAY, str(exc))
            return self._json({"ok": True, "job": job, "action": "cancel"})

        if len(parts) == 4 and parts[:2] == ["api", "boards"]:
            name, action = unquote(parts[2]), parts[3]
            if action == "autostart":
                return self._set_autostart(name, raw)
            if action not in ("start", "stop", "restart", "demand", "renew"):
                return self._error(HTTPStatus.NOT_FOUND, f"unknown action {action!r}")
            try:
                self.supervisor.request(action, name)  # type: ignore[arg-type]
            except KeyError:
                return self._error(HTTPStatus.NOT_FOUND, f"no board {name!r}")
            return self._json({"ok": True, "board": name, "action": action})
        return self._error(HTTPStatus.NOT_FOUND, "not found")

    def _set_autostart(self, name: str, raw: bytes) -> None:
        """Persist a board's autostart policy; the registry is the source of truth."""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return self._error(HTTPStatus.BAD_REQUEST, "invalid JSON")
        if not isinstance(payload, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "expected a JSON object")
        mode = payload.get("mode")
        if mode not in AUTOSTART_MODES:
            return self._error(
                HTTPStatus.BAD_REQUEST, f"mode must be one of {', '.join(AUTOSTART_MODES)}"
            )
        if not any(board.name == name for board in self.supervisor.snapshot()):
            return self._error(HTTPStatus.NOT_FOUND, f"no board {name!r}")
        hook = self.server.on_autostart  # type: ignore[attr-defined]
        if hook is None:
            return self._error(HTTPStatus.NOT_IMPLEMENTED, "autostart unavailable")
        try:
            hook(name, mode)
        except Exception as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        return self._json({"ok": True, "board": name, "autostart": mode})

    def _add_board(self, raw: bytes) -> None:
        """Register a logdir, or put back a board that was just removed."""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return self._error(HTTPStatus.BAD_REQUEST, "invalid JSON")
        if not isinstance(payload, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "expected a JSON object")

        undo = payload.get("undo")
        if undo is not None:
            # Only a token: the removed block itself never leaves the manager, so
            # this endpoint cannot be used to write registry text.
            hook = self.server.on_restore  # type: ignore[attr-defined]
            if hook is None:
                return self._error(HTTPStatus.NOT_IMPLEMENTED, "undo unavailable")
            if not isinstance(undo, str) or not undo:
                return self._error(HTTPStatus.BAD_REQUEST, "undo must be a token")
            call = lambda: hook(undo)
        else:
            path = payload.get("path") or payload.get("logdir")
            if not isinstance(path, str) or not path.strip():
                return self._error(HTTPStatus.BAD_REQUEST, "path is required")
            options = {}
            for key in ("name", "autostart"):
                value = payload.get(key)
                if value is None or value == "":
                    continue
                if not isinstance(value, str):
                    return self._error(HTTPStatus.BAD_REQUEST, f"{key} must be a string")
                options[key] = value.strip()
            if options.get("autostart", AUTOSTART_MODES[0]) not in AUTOSTART_MODES:
                return self._error(
                    HTTPStatus.BAD_REQUEST,
                    f"autostart must be one of {', '.join(AUTOSTART_MODES)}",
                )
            hook = self.server.on_add  # type: ignore[attr-defined]
            if hook is None:
                return self._error(HTTPStatus.NOT_IMPLEMENTED, "add unavailable")
            call = lambda: hook(path.strip(), **options)
        try:
            result = call()
        except Exception as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        return self._json(result)

    # ------------------------------------------------------------------- boards

    def _board(self) -> None:
        """Relay a request on a board's origin to that board."""
        raw_host = self.headers.get("Host") or ""
        if not proxy.board_request_allowed(self.command, self.headers, raw_host):
            self.close_connection = True
            return proxy.send_text(self, HTTPStatus.FORBIDDEN, "cross-origin request refused")
        if not self.path.startswith("/"):
            self.close_connection = True
            return proxy.send_text(self, HTTPStatus.BAD_REQUEST, "expected a path")
        # Read before anything else can answer: an unread body would be parsed
        # as the next request on this connection, a request no check has seen.
        body = proxy.read_body(self)
        if body is None:
            return
        route = self.route
        assert route is not None
        rewrite = None
        if route.remote is None:
            spec = self.supervisor.config.by_label.get(route.label)
            if spec is None:
                return self._board_error(HTTPStatus.NOT_FOUND, f"No board is registered as {raw_host}.")
            upstream: proxy.Upstream | None = self.server.board_upstream(spec)  # type: ignore[attr-defined]
            # The peer marker is for this manager; the board never sees it.
            headers = proxy.forward_headers(self.headers.items(), drop=frozenset({PEER_HEADER.lower()}))
            where = f"{spec.name} is not answering on port {spec.port}"
        else:
            link = self.server.remote_link(route.remote)  # type: ignore[attr-defined]
            if link is None:
                return self._board_error(HTTPStatus.NOT_FOUND, f"No remote is registered as {route.remote!r}.")
            upstream = link.upstream
            if upstream is None:
                return self._board_error(HTTPStatus.SERVICE_UNAVAILABLE, link.unavailable())
            inner = f"{route.label}.localhost"
            headers = proxy.forward_headers(
                self.headers.items(),
                drop=proxy.BROWSER_CONTEXT,
                set_={"Host": inner, PEER_HEADER: "1"},
            )
            rewrite = _location_rewriter(inner, raw_host)
            where = f"{link.spec.title} did not answer"
        # The manager the browser talks to says who may frame the board. One
        # relaying for a peer leaves that to the peer: it knows the browser's
        # port, and browsers enforce every policy, so a second would block it.
        # A navigation cannot carry the peer header, so no page can drop it.
        host = proxy.split_host(raw_host)
        append = () if self._peer else (board_framing(host and host[1]),)
        try:
            proxy.relay(self, upstream, path=self.path, headers=headers, body=body,
                        rewrite_location=rewrite, append=append)
        except proxy.UpstreamError as exc:
            return self._board_error(HTTPStatus.BAD_GATEWAY, f"{where} ({exc}).")

    def _board_error(self, status: HTTPStatus, message: str) -> None:
        return proxy.send_text(self, status, message, html=True)

    def _remote_api(self, parts: list[str], body: bytes) -> None:
        """``/api/remotes/<name>/api/...`` is that remote's own API, through its link."""
        if len(parts) < 3:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        name = unquote(parts[2])
        link = self.server.remote_link(name)  # type: ignore[attr-defined]
        if link is None:
            return self._error(HTTPStatus.NOT_FOUND, f"no remote {name!r}")
        if parts[3:] == ["retry"] and self.command == "POST":
            link.retry()
            return self._json({"ok": True, "remote": name})
        # Board origins, not this API, carry board traffic; and the remote's
        # stream reaches the dashboard merged into this manager's own.
        if parts[3:4] != ["api"] or parts[4:5] == ["events"] or len(parts) < 5:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        upstream = link.upstream
        if upstream is None:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, link.unavailable())
        query = urlparse(self.path).query
        path = "/" + "/".join(parts[3:]) + (f"?{query}" if query else "")
        headers = proxy.forward_headers(
            self.headers.items(),
            drop=proxy.BROWSER_CONTEXT | {"cookie"},
            set_={"Host": "localhost", PEER_HEADER: "1"},
        )
        try:
            proxy.relay(self, upstream, path=path, headers=headers, body=body)
        except proxy.UpstreamError as exc:
            return self._error(HTTPStatus.BAD_GATEWAY, f"{link.spec.title} did not answer ({exc})")

    # -------------------------------------------------------------------- static

    def _static(self, relative: str) -> None:
        target = (WEB_ROOT / relative).resolve()
        if not target.is_file() or WEB_ROOT.resolve() not in target.parents:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        stat = target.stat()
        etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(
            HTTPStatus.OK,
            target.read_bytes(),
            ctype,
            {"ETag": etag, "Cache-Control": "no-cache"},
        )

    # ----------------------------------------------------------------------- sse

    def _events(self) -> None:
        channel = self.supervisor.subscribe(peer=self._peer)
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            # No Content-Length is possible for a stream, so this connection ends
            # with the stream; EventSource reconnects on its own.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            while True:
                try:
                    payload = channel.get(timeout=SSE_HEARTBEAT)
                    frame = f"data: {payload}\n\n"
                except queue.Empty:
                    frame = ": keepalive\n\n"
                self.wfile.write(frame.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            # Writing the headers can already fail if the tab navigated away; the
            # subscription must still be released, hence try from the subscribe on.
            pass
        finally:
            self.supervisor.unsubscribe(channel)


def serve(
    supervisor: Supervisor,
    host: str,
    port: int,
    on_reload: Callable[[], None] | None = None,
    on_cancel: Callable[[int], Any] | None = None,
    on_add: Callable[..., Any] | None = None,
    on_autostart: Callable[[str, str], Any] | None = None,
    on_remove: Callable[[str], Any] | None = None,
    on_restore: Callable[[str], Any] | None = None,
    on_complete: Callable[[str], Any] | None = None,
    on_suggest: Callable[[], Any] | None = None,
    sock: socket.socket | None = None,
    federation: Any = None,
) -> ControlServer:
    """Start the control server on a background thread and return it.

    ``sock`` is an already-listening socket handed over by systemd; without
    one the server binds ``host:port`` itself.
    """
    server = ControlServer(
        (host, port), supervisor, on_reload, on_cancel, on_add, on_autostart,
        on_remove, on_restore, on_complete, on_suggest, sock, federation,
    )
    thread = threading.Thread(target=server.serve_forever, name="tensorwatch-httpd", daemon=True)
    thread.start()
    return server


def _location_rewriter(inner: str, outer: str) -> Callable[[str], str]:
    """Map a remote board's redirects from its own host name to the one used here."""

    def rewrite(location: str) -> str:
        try:
            parsed = urlparse(location)
            hostname = parsed.hostname
        except ValueError:  # a malformed URL is the browser's problem, as it was sent
            return location
        if hostname and hostname.lower() == inner:
            return parsed._replace(netloc=outer).geturl()
        return location

    return rewrite
