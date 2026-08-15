"""Per-process-group resource sampling from ``/proc``.

Each board runs in its own session (``start_new_session=True``), so its process
group id equals the child pid and a single pass over ``/proc`` prices every board
at once - one scan instead of N subprocess calls, which matters because the
supervisor samples on a timer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Collection

CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

# /proc/<pid>/stat fields, 1-based per proc(5). Index below is into the token
# list that follows the "(comm)" field, i.e. field N -> tokens[N - 3].
_F_PGRP = 5 - 3
_F_UTIME = 14 - 3
_F_STIME = 15 - 3
_F_RSS = 24 - 3


@dataclass(frozen=True, slots=True)
class GroupSample:
    """Aggregated cost of one process group."""

    rss_bytes: int
    cpu_seconds: float
    processes: int


def sample(pgids: Collection[int]) -> dict[int, GroupSample]:
    """Aggregate RSS and CPU time for the given process groups."""
    wanted = {int(pgid) for pgid in pgids if pgid}
    if not wanted:
        return {}

    rss: dict[int, int] = {}
    ticks: dict[int, int] = {}
    count: dict[int, int] = {}

    with os.scandir("/proc") as entries:
        for entry in entries:
            name = entry.name
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as handle:
                    raw = handle.read()
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            close = raw.rfind(b")")
            if close < 0:
                continue
            tokens = raw[close + 2 :].split()
            if len(tokens) <= _F_RSS:
                continue
            try:
                pgid = int(tokens[_F_PGRP])
            except ValueError:
                continue
            if pgid not in wanted:
                continue
            rss[pgid] = rss.get(pgid, 0) + int(tokens[_F_RSS]) * PAGE_SIZE
            ticks[pgid] = ticks.get(pgid, 0) + int(tokens[_F_UTIME]) + int(tokens[_F_STIME])
            count[pgid] = count.get(pgid, 0) + 1

    return {
        pgid: GroupSample(
            rss_bytes=rss[pgid],
            cpu_seconds=ticks[pgid] / CLOCK_TICKS,
            processes=count[pgid],
        )
        for pgid in rss
    }
