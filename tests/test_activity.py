from __future__ import annotations

import os
import time
from pathlib import Path

from conftest import wait_until
from tensorwatch import activity


def event(root: Path, *parts: str, age: float = 0.0) -> Path:
    """Create an event file `age` seconds old."""
    path = root.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00")
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


def test_scan_finds_the_newest_event_file_anywhere_below(tmp_path):
    """A board watching `runs/` must see writes in any child, however deep."""
    event(tmp_path, "runs", "old-run", "events.out.tfevents.1.host", age=7200)
    newest = event(tmp_path, "runs", "live-run", "tb", "events.out.tfevents.2.host", age=5)
    event(tmp_path, "runs", "live-run", "checkpoint.pt")  # not an event file

    sample = activity.scan(tmp_path / "runs")
    assert sample.newest == newest
    assert sample.files == 2
    assert 0 <= sample.age() < 60
    assert activity.writing(sample) is True


def test_quiet_and_empty_logdirs(tmp_path):
    event(tmp_path, "runs", "run", "events.out.tfevents.1.host", age=activity.WRITING_WINDOW + 60)
    quiet = activity.scan(tmp_path / "runs")
    assert quiet.mtime is not None
    assert activity.writing(quiet) is False

    (tmp_path / "empty").mkdir()
    empty = activity.scan(tmp_path / "empty")
    assert empty.mtime is None and empty.files == 0
    assert activity.writing(empty) is False
    assert activity.writing(None) is False


def test_scan_skips_noise_and_respects_the_cap(tmp_path):
    event(tmp_path, "runs", ".git", "events.out.tfevents.9.host")
    event(tmp_path, "runs", "__pycache__", "events.out.tfevents.9.host")
    real = event(tmp_path, "runs", "r1", "events.out.tfevents.1.host")
    sample = activity.scan(tmp_path / "runs")
    assert sample.files == 1 and sample.newest == real

    for index in range(30):
        event(tmp_path, "big", f"r{index}", f"events.out.tfevents.{index}.host")
    capped = activity.scan(tmp_path / "big", cap=10)
    assert capped.truncated is True


def test_refresh_uses_the_hint_before_walking(tmp_path, monkeypatch):
    """An appending run is one stat, not a walk of thousands of files."""
    live = event(tmp_path, "runs", "live", "events.out.tfevents.1.host", age=30)
    first = activity.scan(tmp_path / "runs")
    assert first.newest == live

    walks = []
    real_scan = activity.scan
    monkeypatch.setattr(activity, "scan", lambda *a, **k: walks.append(1) or real_scan(*a, **k))

    # The hint has not moved: no cheap answer, so it falls back to a walk.
    activity.refresh(tmp_path / "runs", first)
    assert len(walks) == 1

    os.utime(live, None)  # the run appends
    again = activity.refresh(tmp_path / "runs", first)
    assert len(walks) == 1  # still one: the hint answered
    assert again.newest == live and again.age() < 5


def test_watcher_publishes_and_notifies(tmp_path):
    live = tmp_path / "live" / "runs"
    quiet = tmp_path / "quiet" / "runs"
    event(tmp_path, "live", "runs", "r1", "events.out.tfevents.1.host", age=5)
    event(tmp_path, "quiet", "runs", "r1", "events.out.tfevents.1.host", age=99999)

    changes: list[int] = []
    watcher = activity.Watcher(
        lambda: {"live": live, "quiet": quiet},
        lambda: changes.append(1),
        fast_interval=0.05,
        full_interval=0.1,
    )
    watcher.start()
    try:
        state = wait_until(lambda: watcher.snapshot() if len(watcher.snapshot()) == 2 else None)
        assert state is not None
        assert activity.writing(state["live"]) is True
        assert activity.writing(state["quiet"]) is False
        assert changes  # the first sample is a change
    finally:
        watcher.shutdown()


def test_watcher_drops_boards_that_left_the_registry(tmp_path):
    event(tmp_path, "a", "runs", "r", "events.out.tfevents.1.host")
    dirs = {"a": tmp_path / "a" / "runs"}
    watcher = activity.Watcher(lambda: dict(dirs), None, fast_interval=0.05, full_interval=0.05)

    watcher.sample()
    assert set(watcher.snapshot()) == {"a"}
    dirs.clear()
    watcher.sample()
    assert watcher.snapshot() == {}
