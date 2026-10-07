"""Control-plane HTTP server: dashboard assets plus a small JSON/SSE API.

Board traffic is deliberately *not* proxied through this server.  TensorBoard
ships multi-megabyte JSON payloads; putting Python in that path would add a copy
and a bottleneck for every scalar fetch.  Instead the dashboard iframes each
board on its own loopback port and this server only carries status.
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

from .config import AUTOSTART_MODES
from .queue import QueueError
from .supervisor import Supervisor

WEB_ROOT = Path(__file__).parent / "web"
#: SSE keep-alive comment cadence; also bounds how fast a closed tab is noticed.
SSE_HEARTBEAT = 20.0
_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
#: The control API is unauthenticated, so the dashboard must not be framable:
#: otherwise a page elsewhere can overlay it and harvest clicks on start/stop,
#: which arrive with a legitimate same-origin Origin.
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


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
    ) -> None:
        self.supervisor = supervisor
        self.on_reload = on_reload
        self.on_cancel = on_cancel
        self.on_add = on_add
        self.on_autostart = on_autostart
        self.on_remove = on_remove
        self.on_restore = on_restore
        self.on_complete = on_complete
        self.on_suggest = on_suggest
        self.started_at = time.time()
        super().__init__(address, ControlHandler)

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
        waved through - the check must fail closed.
        """
        raw_host = (self.headers.get("Host") or "").strip()
        # `[::1]:6005` -> `[::1]`, `127.0.0.1:6005` -> `127.0.0.1`.
        host = raw_host.split("]")[0] + "]" if raw_host.startswith("[") else raw_host.rsplit(":", 1)[0]
        if host not in _ALLOWED_HOSTS:
            return False
        # A page elsewhere can still fire GETs without an Origin (an <img>, a
        # <script>); browsers label those, and nothing here is meant for them.
        # Following a link to the dashboard from elsewhere stays allowed.
        if self.path.startswith("/api/") and self.headers.get(
            "Sec-Fetch-Site", "same-origin"
        ) not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in _ALLOWED_HOSTS:
                return False
            if parsed.port != self.server.server_address[1]:
                return False
        return True

    # ------------------------------------------------------------------- routing

    def do_GET(self) -> None:
        if not self._local_only():
            return self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            return self._static("index.html")
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

    do_HEAD = do_GET

    def _lookup(self, hook_name: str, key: str, *args: str) -> None:
        """Answer a read-only question about the filesystem through a hook."""
        hook = getattr(self.server, hook_name)
        if hook is None:
            return self._error(HTTPStatus.NOT_IMPLEMENTED, f"{key} unavailable")
        try:
            return self._json({key: hook(*args)})
        except Exception as exc:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_DELETE(self) -> None:
        # Drain any body, as do_POST does, so it is not read as the next request.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if not self._local_only():
            return self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
        parts = urlparse(self.path).path.strip("/").split("/")
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

    def do_POST(self) -> None:
        # Drain the body before any early return, or the unread bytes are parsed
        # as the next request on this keep-alive connection.
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not self._local_only():
            return self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
        parts = urlparse(self.path).path.strip("/").split("/")

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
            if action not in ("start", "stop", "restart", "demand"):
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
        channel = self.supervisor.subscribe()
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
) -> ControlServer:
    """Start the control server on a background thread and return it."""
    server = ControlServer(
        (host, port), supervisor, on_reload, on_cancel, on_add, on_autostart,
        on_remove, on_restore, on_complete, on_suggest,
    )
    thread = threading.Thread(target=server.serve_forever, name="tensorwatch-httpd", daemon=True)
    thread.start()
    return server
