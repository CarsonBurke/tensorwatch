from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

#: Stand-in for the real `tensorboard` binary: parses the same flags, binds the
#: requested port, and can be made to stall or exit through env vars.
FAKE_TB = '''
import argparse, http.server, os, socketserver, sys, time

parser = argparse.ArgumentParser()
parser.add_argument("--logdir")
parser.add_argument("--logdir_spec")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int)
parser.add_argument("--reload_interval")
parser.add_argument("--window_title")
parser.add_argument("--samples_per_plugin")
args, _ = parser.parse_known_args()

time.sleep(float(os.environ.get("FAKE_TB_DELAY", "0")))
code = os.environ.get("FAKE_TB_EXIT")
if code:
    print("fake tensorboard failing on purpose", file=sys.stderr)
    sys.exit(int(code))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer((args.host, args.port), Handler) as server:
    server.serve_forever()
'''


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate registry + state directories."""
    monkeypatch.setenv("TENSORWATCH_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("TENSORWATCH_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def fake_tb(tmp_path):
    script = tmp_path / "fake_tensorboard.py"
    script.write_text(FAKE_TB, encoding="utf-8")
    return (sys.executable, str(script))


@pytest.fixture
def free_port():
    def pick() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    return pick


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.05):
    """Poll ``predicate`` until it returns a truthy value; return it or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None
