"""Board origins: every board is served through the dashboard port.

A board is reachable at ``http://<label>.localhost:<dashboard port>/``, and a
board on a remote machine at ``http://<label>.<remote>.localhost:<port>/``. The
manager reads the Host header, picks the board, and relays the request to its
TensorBoard (or, for a remote board, through the SSH link to the remote
TensorWatch, which does the same on its side).

Why subdomains of ``localhost``:

- Each board keeps an origin of its own. TensorBoard keeps pins, smoothing and
  other settings in per-origin ``localStorage``, and browsers allow only six
  HTTP/1.1 connections per host, so boards sharing one origin would share both.
- Browsers resolve every ``*.localhost`` name to loopback without asking DNS,
  so these names cannot be rebound to another address, and they need no
  ``/etc/hosts`` entries.
- The whole dashboard, boards included, needs a single port: one ``ssh -L``
  to it is enough to use TensorWatch from another machine.

The relay streams in both framings TensorBoard and TensorWatch use (a length,
or a body that ends when the connection does) and passes compressed bodies
through untouched, so a long response reaches the browser as it is produced.
"""

from __future__ import annotations

import http.client
import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

#: One DNS label, as browsers lowercase it.
LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
#: Names that reach the dashboard itself rather than a board.
DASHBOARD_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
#: RFC 9110 hop-by-hop fields, plus the legacy proxy ones. They describe one
#: connection, so they never cross the relay.
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
})
#: Fields about the browser's view of the request. They are checked here, and
#: a remote TensorWatch would read them as a cross-origin request from a page
#: on its own machine, so they are not forwarded to a remote.
BROWSER_CONTEXT = frozenset({
    "origin", "referer", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest",
    "sec-fetch-user", "sec-fetch-storage-access",
})
#: Request bodies are read in full before relaying, which keeps a retry on a
#: fresh connection possible. TensorBoard's POSTs are a few kilobytes.
MAX_REQUEST_BODY = 64 * 1024 * 1024
#: Methods a stale keep-alive connection may be retried with: a request that
#: may already have reached the upstream must not be applied twice.
IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS"})
_LENGTH_RE = re.compile(r"[0-9]{1,18}")
#: Bytes relayed per write; large enough that a multi-megabyte scalar or image
#: response costs a handful of system calls, small enough to stream promptly.
CHUNK = 256 * 1024
#: Idle keep-alive connections kept per upstream, and for how long. A remote
#: TensorWatch keeps connections open; each new one is an SSH channel, which
#: costs a round trip. TensorBoard closes every connection, so it never pools.
POOL_SIZE = 8
POOL_IDLE = 30.0
CONNECT_TIMEOUT = 5.0
#: A response may take this long to start or to send its next byte. TensorBoard
#: can spend tens of seconds on a first scalar query over thousands of runs.
READ_TIMEOUT = 300.0


@dataclass(frozen=True, slots=True)
class Route:
    """What a Host header names: the dashboard, or one board."""

    #: The board's host label; empty for the dashboard itself.
    label: str = ""
    #: The remote machine the board lives on, or None for this machine.
    remote: str | None = None

    @property
    def dashboard(self) -> bool:
        return not self.label


DASHBOARD = Route()


def split_host(raw: str | None) -> tuple[str, int | None] | None:
    """``Host`` header -> (lowercased name, port), or None when malformed."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0:
            return None
        name, rest = raw[1:end], raw[end + 1 :]
        if rest and not rest.startswith(":"):
            return None
        port_text = rest[1:]
    else:
        name, sep, port_text = raw.rpartition(":")
        if not sep:
            name, port_text = raw, ""
        elif ":" in name:
            return None  # a bare IPv6 address must be bracketed
    if port_text:
        if not (port_text.isascii() and port_text.isdigit()) or not 0 < int(port_text) < 65536:
            return None
        port: int | None = int(port_text)
    elif raw.endswith(":"):
        return None
    else:
        port = None
    name = name.lower()
    # "cleanrl.localhost." is the same name written as a fully qualified one.
    if name.endswith(".") and not name.endswith(".."):
        name = name[:-1]
    return (name, port) if name else None


def classify(raw_host: str | None) -> Route | None:
    """Where a request is going, by its Host header; None for anywhere else.

    Anything that is not a loopback name is refused: a DNS-rebound name carries
    the attacker's domain here, and the check must fail closed.
    """
    parts = split_host(raw_host)
    if parts is None:
        return None
    name = parts[0]
    if name in DASHBOARD_HOSTS:
        return DASHBOARD
    labels = name.split(".")
    if labels[-1] != "localhost" or not all(LABEL_RE.match(label) for label in labels[:-1]):
        return None
    if len(labels) == 2:
        return Route(label=labels[0])
    if len(labels) == 3:
        return Route(label=labels[0], remote=labels[1])
    return None


def same_origin(origin: str, raw_host: str) -> bool:
    """Whether an ``Origin`` header names the very host:port being requested."""
    host = split_host(raw_host)
    try:
        parsed = urlsplit(origin)
        origin_port = parsed.port
    except ValueError:
        return False
    if host is None or parsed.scheme != "http" or not parsed.hostname:
        return False
    origin_host = parsed.hostname.lower().rstrip(".")
    return (origin_host, origin_port or 80) == (host[0], host[1] or 80)


def board_request_allowed(method: str, headers: Mapping[str, str], raw_host: str) -> bool:
    """Whether a request to a board origin may reach the board.

    A page elsewhere must not be able to drive a board, nor read one through a
    cross-origin request. A board's own page is same-origin, and opening it is
    a navigation (the dashboard frames it, or it opens in its own tab), which
    browsers label so. Clients that send neither field (curl, scripts) are not
    browsers and cannot be made to act on another page's behalf.
    """
    origin = headers.get("Origin")
    if origin and not same_origin(origin, raw_host):
        return False
    site = headers.get("Sec-Fetch-Site")
    if site is None or site in ("same-origin", "none"):
        return True
    return headers.get("Sec-Fetch-Mode") == "navigate" and method in ("GET", "HEAD")


def host_label(name: str) -> str:
    """The DNS label a board name becomes: lowercase letters, digits and dashes."""
    label = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return label[:63].rstrip("-") or "board"


def assign_labels(names: Iterable[str]) -> dict[str, str]:
    """Board name -> host label, unique in registry order.

    Hosts are case-insensitive and cannot hold ``_`` or ``.``, so two names
    can map to one label; the later board gets a numbered one.
    """
    taken: set[str] = set()
    labels: dict[str, str] = {}
    for name in names:
        base = host_label(name)
        label, number = base, 1
        while label in taken:
            number += 1
            suffix = f"-{number}"
            label = base[: 63 - len(suffix)].rstrip("-") + suffix
        taken.add(label)
        labels[name] = label
    return labels


# ----------------------------------------------------------------- upstreams


class _TCPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int) -> None:
        super().__init__(host, port, timeout=CONNECT_TIMEOUT)

    def connect(self) -> None:
        super().connect()
        # http.client sends a body separately from its headers; see
        # ControlServer.get_request for why small writes need NODELAY.
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(READ_TIMEOUT)


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost", timeout=CONNECT_TIMEOUT)
        self.path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect(self.path)
            sock.settimeout(READ_TIMEOUT)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class Upstream:
    """Where a route's requests go: a TCP address or a Unix socket.

    Keeps a few idle keep-alive connections. A connection that the far side
    closed while idle is found out on reuse; :func:`relay` then retries once on
    a fresh one.
    """

    def __init__(self, *, address: tuple[str, int] | None = None, path: str | None = None) -> None:
        if (address is None) == (path is None):
            raise ValueError("give exactly one of address or path")
        self.address = address
        self.path = path
        self._idle: deque[tuple[float, http.client.HTTPConnection]] = deque()
        self._lock = threading.Lock()
        self._closed = False

    def __repr__(self) -> str:
        return f"Upstream({self.path or self.address})"

    def _new(self) -> http.client.HTTPConnection:
        if self.path is not None:
            return _UnixConnection(self.path)
        host, port = self.address  # type: ignore[misc]
        return _TCPConnection("127.0.0.1" if host in ("0.0.0.0", "::") else host, port)

    def acquire(self) -> tuple[http.client.HTTPConnection, bool]:
        """A connection, and whether it was used before."""
        now = time.monotonic()
        with self._lock:
            while self._idle:
                since, conn = self._idle.pop()
                if now - since < POOL_IDLE:
                    return conn, True
                conn.close()
        return self._new(), False

    def release(self, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            if not self._closed and len(self._idle) < POOL_SIZE:
                self._idle.append((time.monotonic(), conn))
                return
        conn.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            idle, self._idle = list(self._idle), deque()
        for _, conn in idle:
            conn.close()


class UpstreamError(Exception):
    """The upstream could not be reached or sent no valid response."""


def _connection_tokens(headers: Iterable[tuple[str, str]]) -> set[str]:
    """Field names listed in Connection, which are hop-by-hop too."""
    tokens: set[str] = set()
    for key, value in headers:
        if key.lower() == "connection":
            tokens.update(token.strip().lower() for token in value.split(","))
    return tokens


def forward_headers(
    headers: Iterable[tuple[str, str]],
    *,
    drop: frozenset[str] = frozenset(),
    set_: Mapping[str, str] = {},
) -> list[tuple[str, str]]:
    """Request fields to send upstream: end-to-end ones, with ``set_`` replacing."""
    items = list(headers)
    skip = HOP_BY_HOP | drop | _connection_tokens(items) | {"content-length"}
    skip |= {key.lower() for key in set_}
    kept = [(key, value) for key, value in items if key.lower() not in skip]
    return kept + list(set_.items())


def content_length(value: str | None) -> int | None:
    """A Content-Length value, strictly: digits only (``int`` also takes ``+3``, ``5_0``)."""
    if value is None:
        return None
    value = value.strip()
    return int(value) if _LENGTH_RE.fullmatch(value) else None


def has_body(handler: BaseHTTPRequestHandler) -> bool:
    """Whether the request carries a body (or claims one, however malformed)."""
    headers = handler.headers
    return bool(headers.get("Transfer-Encoding")) or (headers.get("Content-Length") or "0").strip() != "0"


def read_body(handler: BaseHTTPRequestHandler, limit: int = MAX_REQUEST_BODY) -> bytes | None:
    """The request body, or None after answering a request that cannot be relayed.

    Every path that answers without reading the body must close the connection,
    or the unread bytes are parsed as the next request on it.
    """
    if handler.headers.get("Transfer-Encoding"):
        # Browsers send lengths; a chunked upload would need streaming support.
        handler.close_connection = True
        send_text(handler, HTTPStatus.LENGTH_REQUIRED, "a Content-Length is required")
        return None
    raw = handler.headers.get("Content-Length")
    length = 0 if raw is None else content_length(raw)
    if length is None:
        handler.close_connection = True
        send_text(handler, HTTPStatus.BAD_REQUEST, "invalid Content-Length")
        return None
    if length > limit:
        handler.close_connection = True
        send_text(handler, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        return None
    return handler.rfile.read(length) if length else b""


def send_text(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    message: str,
    *,
    html: bool = False,
) -> None:
    """A small error page; ``html`` for what lands in a board's frame."""
    if html:
        from html import escape

        body = (
            '<!DOCTYPE html><meta charset="utf-8"><meta name="color-scheme" content="light dark">'
            f"<title>{status.value} {escape(status.phrase)}</title>"
            '<body style="font:14px system-ui,sans-serif;margin:2rem">'
            f"<p>{escape(message)}</p>"
        ).encode()
        ctype = "text/html; charset=utf-8"
    else:
        body = (message + "\n").encode()
        ctype = "text/plain; charset=utf-8"
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def _send(
    upstream: Upstream, method: str, path: str, headers: list[tuple[str, str]], body: bytes
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    """Send the request and read the response head, retrying a stale connection once."""
    for attempt in (1, 2):
        conn, reused = upstream.acquire()
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in headers:
                conn.putheader(key, value)
            if body or method in ("POST", "PUT", "PATCH"):
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body or None)
            response = conn.getresponse()
        except (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected) as exc:
            conn.close()
            # Most likely a keep-alive connection the far side closed while it
            # sat idle, so try a new one; but only if a second copy of the
            # request could do no harm, since it may have arrived after all.
            if reused and attempt == 1 and method in IDEMPOTENT:
                continue
            raise UpstreamError(str(exc) or type(exc).__name__) from exc
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise UpstreamError(str(exc) or type(exc).__name__) from exc
        if 100 <= response.status < 200:
            # http.client skips 100 Continue itself. Another interim response
            # (103 Early Hints) would be relayed as if final, with the real one
            # left unread on the connection; neither board nor peer sends one.
            conn.close()
            raise UpstreamError(f"unexpected interim response {response.status}")
        return conn, response
    raise AssertionError("unreachable")


def relay(
    handler: BaseHTTPRequestHandler,
    upstream: Upstream,
    *,
    path: str,
    headers: list[tuple[str, str]],
    body: bytes,
    rewrite_location: Callable[[str], str] | None = None,
    append: Sequence[tuple[str, str]] = (),
) -> None:
    """Send the handler's request to ``upstream`` and stream the answer back.

    ``append`` headers are sent alongside the upstream's own, never instead of
    them: a second ``Content-Security-Policy`` tightens the board's, and both hold.

    Raises :class:`UpstreamError` before anything was written to the client,
    so the caller can still answer with an error page of its own.
    """
    method = handler.command
    conn, response = _send(upstream, method, path, headers, body)
    reusable = False
    try:
        reusable = _stream(handler, response, rewrite_location, append)
    finally:
        if reusable and not response.will_close:
            upstream.release(conn)
        else:
            conn.close()


def _stream(
    handler: BaseHTTPRequestHandler,
    response: http.client.HTTPResponse,
    rewrite_location: Callable[[str], str] | None,
    append: Sequence[tuple[str, str]],
) -> bool:
    """Write the response to the client; True if the upstream connection is reusable."""
    upstream_headers = response.getheaders()
    skip = HOP_BY_HOP | _connection_tokens(upstream_headers) | {"content-length"}
    no_body = handler.command == "HEAD" or response.status in (204, 304)
    # Re-serialised, so a lenient parse upstream cannot reach the browser as is.
    length_value = content_length(response.getheader("Content-Length"))
    length = None if length_value is None else str(length_value)

    handler.send_response_only(response.status, response.reason)
    for key, value in upstream_headers:
        if key.lower() in skip:
            continue
        if rewrite_location is not None and key.lower() == "location":
            value = rewrite_location(value)
        handler.send_header(key, value)
    for key, value in append:
        handler.send_header(key, value)

    chunked = False
    if no_body:
        if length is not None:
            handler.send_header("Content-Length", length)
    elif length is not None and not response.chunked:
        handler.send_header("Content-Length", length)
    elif handler.request_version >= "HTTP/1.1":
        handler.send_header("Transfer-Encoding", "chunked")
        chunked = True
    else:
        handler.send_header("Connection", "close")
        handler.close_connection = True
    handler.end_headers()
    if no_body:
        return True

    writer = handler.wfile
    try:
        while True:
            data = response.read1(CHUNK)
            if not data:
                break
            if chunked:
                writer.write(b"".join((b"%X\r\n" % len(data), data, b"\r\n")))
            else:
                writer.write(data)
        if chunked:
            writer.write(b"0\r\n\r\n")
    except (http.client.IncompleteRead, OSError):
        # The response is cut short on one side or the other; the framing can
        # no longer be finished, so neither connection is reusable.
        handler.close_connection = True
        return False
    return True
