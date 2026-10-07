"""Finding logdirs: what to register, what to call it, and what to offer.

Everything here reads the filesystem and nothing writes, so the CLI (``scan``,
``add``) and the dashboard (path completion, suggestions) can share it.
"""

from __future__ import annotations

import os
import re
from collections import deque
from pathlib import Path
from typing import Callable, Iterable

from .activity import EVENT_PREFIX

#: A directory whose name *starts* with one of these tokens holds a family of runs
#: rather than being one run: ``runs``, ``runs_old``, ``tb_logs``,
#: ``lightning_logs``, ``logs_tokenizer_cartpole``.  Requiring the leading token
#: keeps project names such as ``Rubiks-tensorboard-debug`` out of the rule.
RUN_DIR_TOKENS = frozenset(
    {"run", "runs", "log", "logs", "tb", "tblog", "tblogs", "tensorboard",
     "summaries", "events", "lightning"}
)
#: Never walked while scanning: large and never a logdir.
PRUNE_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", "site-packages",
     ".pnpm-store", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache",
     "worktrees", "target", "dist", "build", "bazel-bin", "bazel-out"}
)
#: Directories one "does this hold any event file" probe may open.
PROBE_BUDGET = 5_000
#: The same for a path completion, which runs per keystroke and per candidate.
COMPLETION_BUDGET = 150
#: Directories opened to count one suggestion's event files.
COUNT_BUDGET = 3_000
#: Completions returned for one prefix; a directory with more is narrowed by typing.
COMPLETION_LIMIT = 40


def is_run_container(name: str) -> bool:
    return re.split(r"[-_.]+", name.lower())[0] in RUN_DIR_TOKENS


def slug(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    return cleaned or "board"


def _walkable(entry: os.DirEntry[str]) -> bool:
    return (
        entry.is_dir(follow_symlinks=False)
        and not entry.name.startswith(".")
        and entry.name not in PRUNE_DIRS
    )


def has_events(root: str | Path, budget: int = PROBE_BUDGET) -> bool:
    """True as soon as one event file turns up under ``root``.

    A run container can hold thousands of runs; knowing that it holds *any* is
    enough to propose it, and stops at the first one.  The walk is breadth-first
    and budgeted in directories opened, so a sibling full of checkpoints neither
    hides a shallow run behind its subtree nor uses the budget up with its files.
    """
    queue = deque([os.fspath(root)])
    while queue and budget > 0:
        budget -= 1
        try:
            with os.scandir(queue.popleft()) as entries:
                for entry in entries:
                    if entry.name.startswith(EVENT_PREFIX):
                        return True
                    if _walkable(entry):
                        queue.append(entry.path)
        except OSError:
            continue
    return False


def count_events(root: str | Path, limit: int = 5000, budget: int | None = None) -> int:
    """Event files under ``root``, counted up to ``limit``.

    ``budget`` caps the directories opened, for callers that must answer soon.
    """
    total = 0
    stack = [os.fspath(root)]
    while stack and (budget is None or budget > 0):
        if budget is not None:
            budget -= 1
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.name.startswith(EVENT_PREFIX):
                        total += 1
                        if total >= limit:
                            return total
                    elif _walkable(entry):
                        stack.append(entry.path)
        except OSError:
            continue
    return total


def _holds_directly(directory: str) -> bool:
    try:
        with os.scandir(directory) as entries:
            return any(entry.name.startswith(EVENT_PREFIX) for entry in entries)
    except OSError:
        return False


def _survey(root: Path, max_depth: int) -> tuple[list[Path], set[Path]]:
    """Run containers holding runs, and the single runs found outside them.

    A container is reported once and not walked: ``cleanrl/runs`` with thousands
    of timestamped runs costs one probe instead of a visit to each of them.
    """
    containers: list[Path] = []
    runs: set[Path] = set()
    top = os.fspath(root)
    stack: list[tuple[str, int]] = [(top, 0)]
    while stack:
        directory, depth = stack.pop()
        holds_events = False
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.startswith(EVENT_PREFIX):
                        holds_events = True
                    elif not _walkable(entry) or depth >= max_depth:
                        continue
                    elif not is_run_container(entry.name):
                        stack.append((entry.path, depth + 1))
                    elif _holds_directly(entry.path):
                        # `exp/tensorboard` is one run living in a per-run
                        # subdirectory: the run is `exp`.
                        runs.add(Path(entry.path if directory == top else directory))
                    elif has_events(entry.path):
                        containers.append(Path(entry.path))
        except OSError:
            continue
        if holds_events:
            runs.add(Path(directory))
    return containers, runs


def _group(root: Path, runs: Iterable[Path]) -> set[Path]:
    """Turn single runs into the logdirs worth registering.

    Sibling runs are grouped: a directory holding two or more runs
    (``hl-gauss-ablations/<run>/...``) is registered once.  A lone run is
    registered as itself - never as its parent, because pointing TensorBoard at a
    whole project directory makes it walk checkpoints and datasets too.
    """
    proposals: set[Path] = set()
    grouped: dict[Path, set[Path]] = {}
    for run in runs:
        grouped.setdefault(run.parent, set()).add(run)
    for parent, units in grouped.items():
        if len(units) >= 2 and parent != root:
            proposals.add(parent)
        else:
            proposals |= units
    return proposals


def discover(root: Path, max_depth: int = 4) -> list[Path]:
    """Logdirs worth registering under ``root``, shallowest first.

    A run-container directory (``runs``, ``tb_logs``, ``runs_old``, ...) wins over
    whatever is inside it, so ``runs_old/ppo/2026-06-08`` registers once as
    ``runs_old`` instead of once per timestamped run.
    """
    root = root.expanduser().resolve()
    containers, runs = _survey(root, max_depth)
    proposals = set(containers) | _group(root, runs)
    kept: list[Path] = []
    for candidate in sorted(proposals, key=lambda path: (len(path.parts), str(path))):
        if not any(candidate.is_relative_to(existing) for existing in kept):
            kept.append(candidate)
    return kept


# ------------------------------------------------------------------------ naming


def repo_root(path: Path) -> Path | None:
    """Nearest ancestor (or ``path`` itself) that is a git repository."""
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def derive_name(target: Path) -> str:
    """Name a board after what it watches, qualified by its repository.

    ``.../cleanrl/runs`` -> ``cleanrl`` (the logdir sits at the repo root);
    ``.../parameter-golf/postraining/runs`` -> ``parameter-golf-postraining``;
    ``.../xxscreeps/samples/rl/runs`` -> ``xxscreeps-rl``.

    A bare leaf like ``postraining`` or ``rl`` says nothing on its own, so a
    logdir nested inside a repository carries the repository name too.
    """
    parts = list(target.parts)
    while len(parts) > 1 and is_run_container(parts[-1]):
        parts.pop()
    leaf = Path(*parts) if parts else target
    repo = repo_root(leaf)
    if repo is not None and repo != leaf:
        return slug(f"{repo.name}-{leaf.name}")
    return slug(leaf.name)


def unique_name(target: Path, is_taken: Callable[[str], bool]) -> str:
    """``cleanrl/runs`` -> ``cleanrl``; a second one -> ``cleanrl-runs_old``."""
    base = derive_name(target)
    if not is_taken(base):
        return base
    qualified = slug(f"{base}-{target.name}")
    if not is_taken(qualified):
        return qualified
    index = 2
    while is_taken(f"{qualified}-{index}"):
        index += 1
    return f"{qualified}-{index}"


# -------------------------------------------------------------------- dashboard


def tilde(path: Path | str) -> str:
    """``/home/me/x`` -> ``~/x``: how a person would type and read it."""
    text = os.fspath(path)
    home = os.path.expanduser("~")
    if text == home:
        return "~"
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


def complete(prefix: str, limit: int = COMPLETION_LIMIT) -> list[dict[str, object]]:
    """Directories a half-typed path could continue into.

    Each carries whether it holds event files, so the dashboard can tell a logdir
    from a directory that merely leads to one.
    """
    typed = prefix.strip() or "~/"
    if typed == "~":
        typed = "~/"
    expanded = os.path.expanduser(os.path.expandvars(typed))
    if not os.path.isabs(expanded):
        return []
    parent, stem = (expanded, "") if expanded.endswith(os.sep) else os.path.split(expanded)
    needle = stem.lower()
    matches: list[tuple[bool, str, str]] = []
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                name = entry.name
                # Hidden directories only once the user starts typing the dot.
                if name.startswith(".") and not stem.startswith("."):
                    continue
                if not name.lower().startswith(needle):
                    continue
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                matches.append((name != stem, name.lower(), entry.path))
    except OSError:
        return []
    matches.sort()
    return [
        {"path": tilde(path), "events": has_events(path, budget=COMPLETION_BUDGET)}
        for _exact, _key, path in matches[:limit]
    ]


def workspaces(logdirs: Iterable[Path]) -> list[Path]:
    """Directories worth searching for more logdirs, judged by the ones we have.

    Boards cluster: every repository next to a registered one is a candidate.
    The home directory, the filesystem root and its direct children (``/data``,
    ``/mnt``) are never searched whole.
    """
    home = Path.home()
    roots: list[Path] = []
    for logdir in logdirs:
        repo = repo_root(logdir)
        root = repo.parent if repo is not None else logdir.parent
        if root == home or len(root.parts) < 3 or root in roots:
            continue
        roots.append(root)
    # A workspace inside another one is already covered by it.
    return [
        root for root in roots
        if not any(other != root and root.is_relative_to(other) for other in roots)
    ]
