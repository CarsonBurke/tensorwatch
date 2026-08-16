"""Is a board's data moving right now?

The honest answer comes from the logdir, not from the queue: a run appends to its
event file whether it was launched through mlq, from a shell, or by something else
entirely, and a board watching ``runs/`` should light up for *any* run underneath
it rather than only for a job whose command line happens to name the right child.

Sampling is cheap because an active run keeps appending to the same file: remember
that file and stat it (one syscall) on the fast path, and only walk the tree again
on the slow path, bounded by an entry cap.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

EVENT_PREFIX = "events.out.tfevents"
#: Entries visited by one full walk; a tree bigger than this reports what it saw.
DEFAULT_CAP = 50_000
#: Directories that never hold live runs but can hold thousands of files.
SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".ipynb_checkpoints"})


@dataclass(frozen=True, slots=True)
class Activity:
    """Newest event-file write seen under a logdir."""

    mtime: float | None = None
    #: File that carried it, reused as the next sample's fast path.
    newest: Path | None = None
    files: int = 0
    truncated: bool = False
    scanned_at: float = 0.0

    def age(self, now: float | None = None) -> float | None:
        if self.mtime is None:
            return None
        return max(0.0, (now if now is not None else time.time()) - self.mtime)


def touch(hint: Path | None) -> float | None:
    """Fast path: mtime of the file that was newest last time, if it still exists."""
    if hint is None:
        return None
    try:
        return hint.stat().st_mtime
    except OSError:
        return None


def scan(root: Path, cap: int = DEFAULT_CAP) -> Activity:
    """Walk ``root`` for the newest event file, visiting at most ``cap`` entries."""
    newest: Path | None = None
    newest_mtime: float | None = None
    files = 0
    visited = 0
    truncated = False
    stack: list[Path] = [root]

    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > cap:
                        truncated = True
                        stack.clear()
                        break
                    if entry.name.startswith(EVENT_PREFIX):
                        files += 1
                        try:
                            mtime = entry.stat().st_mtime
                        except OSError:
                            continue
                        if newest_mtime is None or mtime > newest_mtime:
                            newest_mtime, newest = mtime, Path(entry.path)
                    elif (
                        entry.is_dir(follow_symlinks=False)
                        and not entry.name.startswith(".")
                        and entry.name not in SKIP_DIRS
                    ):
                        stack.append(Path(entry.path))
        except OSError:
            continue

    return Activity(
        mtime=newest_mtime,
        newest=newest,
        files=files,
        truncated=truncated,
        scanned_at=time.time(),
    )


def refresh(root: Path, previous: Activity | None, cap: int = DEFAULT_CAP) -> Activity:
    """Sample ``root`` cheaply when possible, falling back to a full walk.

    A live run appends to the file that was newest last time, so one stat usually
    settles it; the walk is only needed when that file stopped moving (a new run
    directory, or a board that was idle).
    """
    if previous is not None and previous.newest is not None:
        mtime = touch(previous.newest)
        if mtime is not None and previous.mtime is not None and mtime > previous.mtime:
            return Activity(
                mtime=mtime,
                newest=previous.newest,
                files=previous.files,
                truncated=previous.truncated,
                scanned_at=time.time(),
            )
    return scan(root, cap)


#: A board whose newest event file was written this recently is receiving data.
WRITING_WINDOW = 180.0


def writing(sample: Activity | None, now: float | None = None) -> bool:
    age = sample.age(now) if sample is not None else None
    return age is not None and age <= WRITING_WINDOW


class Watcher(threading.Thread):
    """Samples every board's logdir on its own thread.

    Kept off the supervisor thread because the first walk of a tree with thousands
    of runs can take a second, and process supervision must stay responsive.
    """

    def __init__(
        self,
        board_dirs: Callable[[], Mapping[str, Path]],
        on_change: Callable[[], None] | None = None,
        fast_interval: float = 5.0,
        full_interval: float = 120.0,
        cap: int = DEFAULT_CAP,
    ) -> None:
        super().__init__(name="tensorwatch-activity", daemon=True)
        self._board_dirs = board_dirs
        self._on_change = on_change
        self._fast = max(0.05, fast_interval)
        self._full = max(self._fast, full_interval)
        self._cap = cap
        self._state: dict[str, Activity] = {}
        self._full_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def snapshot(self) -> dict[str, Activity]:
        with self._lock:
            return dict(self._state)

    def shutdown(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception:  # a sampler must never take the process down
                pass
            self._stop.wait(self._fast)

    def sample(self) -> None:
        """One pass over every board; cheap unless a full walk is due."""
        dirs = self._board_dirs()
        now = time.monotonic()
        with self._lock:
            state = dict(self._state)
        changed = False

        for name, logdir in dirs.items():
            previous = state.get(name)
            if previous is None or now - self._full_at.get(name, 0.0) >= self._full:
                fresh = scan(logdir, self._cap)
                self._full_at[name] = now
            else:
                mtime = touch(previous.newest)
                if mtime is None or previous.mtime is None or mtime <= previous.mtime:
                    continue  # nothing new; the next full walk picks up new runs
                fresh = Activity(
                    mtime=mtime,
                    newest=previous.newest,
                    files=previous.files,
                    truncated=previous.truncated,
                    scanned_at=time.time(),
                )
            if writing(previous) != writing(fresh):
                changed = True
            state[name] = fresh

        for name in [name for name in state if name not in dirs]:
            del state[name]
            self._full_at.pop(name, None)
            changed = True

        with self._lock:
            self._state = state
        if changed and self._on_change is not None:
            self._on_change()
