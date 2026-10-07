"""Run dashboard behavior tests without adding browser or npm dependencies."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_dashboard_heartbeat():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for dashboard JavaScript tests")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("web_test.cjs"))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
