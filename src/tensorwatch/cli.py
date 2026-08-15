"""Command line interface: ``tensorwatch <command>``."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from . import desktop, httpd, queue, registry, service
from .config import (
    AUTOSTART_MODES,
    Config,
    ConfigError,
    NAME_RE,
    config_path,
    load,
    log_dir,
    next_free_port,
    parse,
    state_dir,
)
from .supervisor import Supervisor, probe

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
EVENT_PREFIX = "events.out.tfevents"
EVENT_GLOB = EVENT_PREFIX + "*"


def _is_run_container(name: str) -> bool:
    return re.split(r"[-_.]+", name.lower())[0] in RUN_DIR_TOKENS


# --------------------------------------------------------------------- helpers


def _fail(message: str) -> int:
    print(f"tensorwatch: {message}", file=sys.stderr)
    return 1


def _api(cfg: Config, path: str, method: str = "GET", timeout: float = 5.0) -> Any:
    url = cfg.dashboard_url.rstrip("/") + path
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode()
    return json.loads(body) if body else None


def _live_state(cfg: Config) -> dict[str, Any] | None:
    try:
        return _api(cfg, "/api/state")
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def _slug(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    return cleaned or "board"


def _table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(header.upper().ljust(widths[i]) for i, header in enumerate(headers))
    out = [line.rstrip()]
    for row in rows:
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(out)


def _human_bytes(value: int | None) -> str:
    if not value:
        return "-"
    units = ["B", "K", "M", "G", "T"]
    size = float(value)
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    return f"{size:.1f}{units[unit]}" if unit and size < 10 else f"{size:.0f}{units[unit]}"


def _count_events(path: Path, limit: int = 5000) -> int:
    total = 0
    for _ in path.rglob(EVENT_GLOB):
        total += 1
        if total >= limit:
            break
    return total


def open_window(url: str, app_mode: bool = True) -> str:
    """Open ``url`` in a chrome-style app window (a plain window if unavailable)."""
    candidates = (
        "chromium", "google-chrome-stable", "google-chrome", "brave", "brave-browser",
        "microsoft-edge", "vivaldi",
    )
    for name in candidates:
        binary = shutil.which(name)
        if not binary:
            continue
        argv = [binary, f"--app={url}"] if app_mode else [binary, "--new-window", url]
        argv.append("--class=tensorwatch")
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return name
    import webbrowser

    webbrowser.open(url)
    return "default browser"


# ---------------------------------------------------------------------- serve


def cmd_serve(args: argparse.Namespace) -> int:
    path = Path(args.config).expanduser() if args.config else config_path()
    registry.ensure(path)
    cfg = load(path)
    registry.set_ports(path, cfg.assigned_ports)
    if cfg.assigned_ports:
        cfg = load(path)

    if args.port:
        cfg = replace(cfg, server=replace(cfg.server, port=args.port))

    if probe(cfg.server.host, cfg.server.port):
        return _fail(
            f"something already listens on {cfg.server.host}:{cfg.server.port} "
            "(another tensorwatch?); stop it or change [server].port"
        )

    log_dir().mkdir(parents=True, exist_ok=True)
    state_dir().mkdir(parents=True, exist_ok=True)

    supervisor = Supervisor(cfg)
    reload_requested = threading.Event()
    stop = threading.Event()
    current: dict[str, Config] = {"cfg": cfg}

    def do_reload() -> None:
        fresh = load(path)
        registry.set_ports(path, fresh.assigned_ports)
        if fresh.assigned_ports:
            fresh = load(path)
        if fresh.queue != current["cfg"].queue:
            print(
                "tensorwatch: [queue] enabled/socket changes need a service restart",
                file=sys.stderr,
            )
        current["cfg"] = fresh
        supervisor.reload(fresh)
        if subscriber is not None:
            # Re-match the queue against the new registry instead of waiting for
            # mlqd's next push, which for a long run can be hours away.
            subscriber.refresh()

    subscriber = None
    if cfg.queue.enabled:
        subscriber = queue.start(
            Path(cfg.queue.socket).expanduser() if cfg.queue.socket else None,
            lambda: current["cfg"].board_dirs,
            supervisor.publish,
        )
        supervisor.set_queue_source(subscriber.snapshot)

    server = httpd.serve(supervisor, cfg.server.host, cfg.server.port, do_reload)
    supervisor.start()

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        signal.signal(signal.SIGHUP, lambda *_: reload_requested.set())
    except (AttributeError, ValueError):
        pass

    print(f"tensorwatch serving {cfg.dashboard_url} ({len(cfg.boards)} board(s), registry {path})")
    for spec in cfg.boards:
        print(f"  {spec.name:<20} {spec.url:<24} {spec.target}")
    if args.window:
        opened = open_window(cfg.dashboard_url)
        print(f"opened dashboard window via {opened}")

    while not stop.is_set():
        if reload_requested.is_set():
            reload_requested.clear()
            try:
                do_reload()
                print("registry reloaded")
            except ConfigError as exc:
                print(f"tensorwatch: reload failed: {exc}", file=sys.stderr)
        stop.wait(1.0)

    print("tensorwatch shutting down")
    server.shutdown()
    server.server_close()
    if subscriber is not None:
        subscriber.shutdown()
    supervisor.shutdown()
    return 0


# ----------------------------------------------------------------- registry ops


def _notify_reload(cfg: Config) -> bool:
    try:
        _api(cfg, "/api/reload", method="POST")
        return True
    except (urllib.error.URLError, OSError):
        return False


def _apply(path: Path, mutate) -> Config:
    """Mutate the registry, but only write once the result parses.

    A rejected edit must leave a working registry behind: the manager may reload
    this file at any moment, including from systemd at boot.
    """
    text = registry.ensure(path)
    updated = mutate(text)
    cfg = parse(updated, path)
    if updated != text:
        registry.write_atomic(path, updated)
    return cfg


def cmd_add(args: argparse.Namespace) -> int:
    path = config_path()
    registry.ensure(path)
    cfg = load(path)

    entry: dict[str, Any] = {}
    if args.logdir_spec:
        entry["logdir_spec"] = args.logdir_spec
        default_name = _slug(args.logdir_spec.split(":", 1)[0])
    else:
        if not args.logdir:
            return _fail("give a logdir or --logdir-spec")
        target = Path(args.logdir).expanduser().resolve()
        if not target.exists() and not args.force:
            return _fail(f"{target} does not exist (pass --force to register anyway)")
        entry["logdir"] = str(target)
        default_name = _derive_name(target)

    name = args.name or default_name
    if not NAME_RE.match(name):
        return _fail(f"invalid name {name!r}")
    if cfg.board(name):
        return _fail(f"board {name!r} already registered")

    taken = {spec.port for spec in cfg.boards} | {cfg.server.port}
    port = args.port or next_free_port(cfg.server.port_base, taken)
    if port in taken:
        return _fail(f"port {port} already used by another board")

    entry = {"name": name, **entry, "port": port}
    if args.autostart != "always":
        entry["autostart"] = args.autostart
    if args.reload_interval is not None:
        entry["reload_interval"] = args.reload_interval
    if args.samples_per_plugin:
        entry["samples_per_plugin"] = args.samples_per_plugin
    if args.command:
        entry["command"] = args.command
    if args.arg:
        entry["args"] = list(args.arg)
    if args.description:
        entry["description"] = args.description
    if args.disabled:
        entry["enabled"] = False

    _apply(path, lambda text: registry.add(text, entry))
    print(f"registered {name} -> {entry.get('logdir') or entry.get('logdir_spec')} on port {port}")
    if _notify_reload(cfg):
        print("running manager reloaded")
    return 0


def _derive_name(target: Path) -> str:
    """Name a board after the project, not the log folder.

    ``.../parameter-golf/tb_logs`` -> ``parameter-golf``;
    ``.../xxscreeps/samples/rl/runs`` -> ``rl``.
    """
    parts = list(target.parts)
    while len(parts) > 1 and _is_run_container(parts[-1]):
        parts.pop()
    return _slug(parts[-1])


def _unique_name(target: Path, is_taken) -> str:
    """``cleanrl/runs`` -> ``cleanrl``; a second one -> ``cleanrl-runs_old``."""
    base = _derive_name(target)
    if not is_taken(base):
        return base
    qualified = _slug(f"{base}-{target.name}")
    if not is_taken(qualified):
        return qualified
    index = 2
    while is_taken(f"{qualified}-{index}"):
        index += 1
    return f"{qualified}-{index}"


def cmd_rm(args: argparse.Namespace) -> int:
    path = config_path()
    cfg = load(path)
    try:
        _apply(path, lambda text: registry.remove(text, args.name))
    except registry.RegistryError as exc:
        return _fail(str(exc))
    print(f"removed {args.name}")
    if _notify_reload(cfg):
        print("running manager reloaded")
    return 0


def _coerce(raw: str) -> Any:
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
    return raw


def cmd_set(args: argparse.Namespace) -> int:
    path = config_path()
    cfg = load(path)
    updates: list[tuple[str, Any]] = []
    for assignment in args.assignment:
        if "=" not in assignment:
            return _fail(f"expected KEY=VALUE, got {assignment!r}")
        key, raw = assignment.split("=", 1)
        updates.append((key.strip(), _coerce(raw.strip())))

    def mutate(text: str) -> str:
        for key, value in updates:
            text = registry.set_key(text, args.name, key, value)
        return text

    try:
        _apply(path, mutate)
    except (registry.RegistryError, ConfigError) as exc:
        return _fail(str(exc))
    for key, value in updates:
        print(f"{args.name}.{key} = {value!r}")
    if _notify_reload(cfg):
        print("running manager reloaded")
    return 0


def cmd_toggle(args: argparse.Namespace) -> int:
    enabled = args.command_name == "enable"
    path = config_path()
    cfg = load(path)
    try:
        _apply(path, lambda text: registry.set_key(text, args.name, "enabled", enabled))
    except registry.RegistryError as exc:
        return _fail(str(exc))
    print(f"{args.name} {'enabled' if enabled else 'disabled'}")
    if _notify_reload(cfg):
        print("running manager reloaded")
    return 0


# ------------------------------------------------------------------- inspection


def cmd_list(args: argparse.Namespace) -> int:
    cfg = load()
    if not cfg.boards:
        print(f"no boards registered in {cfg.path}; try `tensorwatch scan <dir> --add`")
        return 0
    live = _live_state(cfg)
    states = {b["name"]: b for b in (live or {}).get("boards", [])}
    rows = []
    for spec in cfg.boards:
        status = states.get(spec.name, {})
        rows.append([
            spec.name,
            status.get("state", "-" if live else "?"),
            str(spec.port),
            spec.autostart if spec.enabled else "disabled",
            _human_bytes(status.get("rss_bytes")),
            f"{status.get('cpu_percent') or 0:.0f}%" if status else "-",
            spec.target,
        ])
    print(_table(rows, ["board", "state", "port", "policy", "rss", "cpu", "logdir"]))
    if live is None:
        print(f"\nmanager not running at {cfg.dashboard_url} (start it with `tensorwatch serve`)")
    else:
        print(f"\ndashboard {cfg.dashboard_url}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load()
    live = _live_state(cfg)
    if live is None:
        return _fail(f"manager not running at {cfg.dashboard_url}")
    boards = live["boards"]
    if args.name:
        boards = [b for b in boards if b["name"] == args.name]
        if not boards:
            return _fail(f"no board {args.name!r}")
    if args.json:
        print(json.dumps(boards if args.name else live, indent=2))
        return 0
    for board in boards:
        age = f" up {int(time.time() - board['since'])}s" if board.get("since") else ""
        print(f"{board['name']:<20} {board['state']:<9} :{board['port']}{age}  {board['message']}")
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    cfg = load()
    if cfg.board(args.name) is None:
        return _fail(f"no board {args.name!r} in {cfg.path}")
    try:
        _api(cfg, f"/api/boards/{args.name}/{args.command_name}", method="POST")
    except (urllib.error.URLError, OSError) as exc:
        return _fail(f"manager not reachable at {cfg.dashboard_url}: {exc}")
    print(f"{args.command_name} {args.name}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    cfg = load()
    spec = cfg.board(args.name)
    if spec is None:
        return _fail(f"no board {args.name!r}")
    path = spec.log_path
    if not path.exists():
        return _fail(f"no log file yet at {path}")
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
        sys.stdout.write("".join(lines[-args.lines :]))
        if not args.follow:
            return 0
        try:
            while True:
                chunk = handle.read()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                else:
                    time.sleep(0.4)
        except KeyboardInterrupt:
            return 0


def _ensure_running(cfg: Config, timeout: float = 40.0) -> bool:
    """Start the systemd unit if the manager is not up, then wait for the port.

    The desktop launcher goes through here, so clicking the icon must produce a
    working dashboard even after the service was stopped.
    """
    if probe(cfg.server.host, cfg.server.port):
        return True
    if not service.unit_path().exists() or shutil.which("systemctl") is None:
        print(
            "manager not running and no systemd unit installed; run `tensorwatch install` "
            "or `tensorwatch serve`",
            file=sys.stderr,
        )
        return False
    print("manager not running; starting tensorwatch.service")
    subprocess.run(["systemctl", "--user", "start", "tensorwatch.service"], check=False)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe(cfg.server.host, cfg.server.port):
            return True
        time.sleep(0.4)
    print(f"tensorwatch.service did not come up within {timeout:.0f}s", file=sys.stderr)
    return False


def cmd_open(args: argparse.Namespace) -> int:
    cfg = load()
    url = cfg.dashboard_url
    if args.name:
        spec = cfg.board(args.name)
        if spec is None:
            return _fail(f"no board {args.name!r}")
        url = spec.url
    if not args.no_start:
        _ensure_running(cfg)
    elif not probe(cfg.server.host, cfg.server.port):
        print(f"warning: manager not running at {url}", file=sys.stderr)
    print(f"opening {url} via {open_window(url, app_mode=not args.tab)}")
    return 0


# -------------------------------------------------------------------- discovery


def _run_dirs(root: Path, max_depth: int) -> list[Path]:
    """Directories that directly contain event files."""
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        has_events = False
        children: list[Path] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False):
                        has_events = has_events or entry.name.startswith(EVENT_PREFIX)
                    elif (
                        entry.is_dir(follow_symlinks=False)
                        and not entry.name.startswith(".")
                        and entry.name not in PRUNE_DIRS
                    ):
                        children.append(Path(entry.path))
        except OSError:
            continue
        if has_events:
            found.append(directory)
        if depth < max_depth:
            stack.extend((child, depth + 1) for child in children)
    return found


def _proposals(root: Path, run_dirs: list[Path]) -> set[Path]:
    """Turn directories full of events into the logdirs worth registering.

    Three rules, in order:

    1. A run-container ancestor (``runs``, ``tb_logs``, ``runs_old``, ...) wins, so
       ``runs_old/ppo/2026-06-08`` registers once as ``runs_old`` instead of once
       per timestamped run.
    2. Otherwise sibling runs are grouped: a directory holding two or more runs
       (``hl-gauss-ablations/<run>/...``) is registered once.
    3. A lone run is registered as itself - never as its parent, because pointing
       TensorBoard at a whole project directory makes it walk checkpoints and
       datasets too.
    """
    proposals: set[Path] = set()
    grouped: dict[Path, set[Path]] = {}
    for run_dir in run_dirs:
        parts = run_dir.relative_to(root).parts
        ancestor = next((i for i, part in enumerate(parts[:-1]) if _is_run_container(part)), None)
        if ancestor is not None:
            proposals.add(root.joinpath(*parts[: ancestor + 1]))
            continue
        # `exp/tensorboard` is one run living in a per-run subdirectory.
        unit = run_dir.parent if _is_run_container(run_dir.name) and run_dir.parent != root else run_dir
        grouped.setdefault(unit.parent, set()).add(unit)
    for parent, units in grouped.items():
        if len(units) >= 2 and parent != root:
            proposals.add(parent)
        else:
            proposals |= units
    return proposals


def _discover(root: Path, max_depth: int) -> list[Path]:
    """Logdirs worth registering under ``root``, shallowest first."""
    root = root.expanduser().resolve()
    proposals = _proposals(root, _run_dirs(root, max_depth))
    ordered = sorted(proposals, key=lambda path: (len(path.parts), str(path)))
    kept: list[Path] = []
    for candidate in ordered:
        if any(candidate.is_relative_to(existing) for existing in kept):
            continue
        kept.append(candidate)
    return kept


def cmd_scan(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser()
    if not root.is_dir():
        return _fail(f"{root} is not a directory")
    found = _discover(root, args.depth)
    if not found:
        print(f"no event files under {root} (depth {args.depth})")
        return 0

    cfg = load()
    known = {str(spec.logdir) for spec in cfg.boards if spec.logdir}
    taken = {spec.port for spec in cfg.boards} | {cfg.server.port}
    rows, additions = [], []
    for candidate in found:
        if str(candidate) in known:
            rows.append([_derive_name(candidate), "registered", "-", str(candidate)])
            continue
        name = _unique_name(candidate, lambda n: bool(cfg.board(n)) or any(e["name"] == n for e in additions))
        port = next_free_port(cfg.server.port_base, taken)
        taken.add(port)
        rows.append([name, "new", str(port), f"{candidate} ({_count_events(candidate)} event files)"])
        additions.append({"name": name, "logdir": str(candidate), "port": port,
                          "autostart": args.autostart})
    print(_table(rows, ["name", "status", "port", "logdir"]))

    if not args.add:
        print("\nre-run with --add to register the new ones")
        return 0

    def mutate(text: str) -> str:
        for entry in additions:
            payload = dict(entry)
            if payload["autostart"] == "always":
                payload.pop("autostart")
            text = registry.add(text, payload)
        return text

    _apply(config_path(), mutate)
    print(f"\nregistered {len(additions)} board(s) in {config_path()}")
    if _notify_reload(cfg):
        print("running manager reloaded")
    return 0


# ------------------------------------------------------------------ diagnostics


def cmd_doctor(args: argparse.Namespace) -> int:
    problems = 0
    path = config_path()
    print(f"registry     {path} {'(missing)' if not path.exists() else ''}")
    try:
        cfg = load(path)
    except ConfigError as exc:
        return _fail(f"registry invalid: {exc}")
    print(f"state        {state_dir()}")
    print(f"dashboard    {cfg.dashboard_url} "
          f"{'(running)' if probe(cfg.server.host, cfg.server.port) else '(not running)'}")
    unit = service.unit_path()
    print(f"systemd unit {unit} {'(installed)' if unit.exists() else '(not installed)'}")
    if unit.exists():
        print(f"             {service.unit_state()}")

    for spec in cfg.boards:
        binary = shutil.which(spec.command[0]) if not os.path.isabs(spec.command[0]) else spec.command[0]
        issues = []
        if binary is None or not Path(binary).exists():
            issues.append(f"command {spec.command[0]!r} not found in PATH")
        if spec.logdir is not None and not spec.logdir.exists():
            issues.append("logdir missing")
        elif spec.logdir is not None and _count_events(spec.logdir, limit=1) == 0:
            issues.append("no event files found")
        if spec.exposed:
            issues.append(
                f"reachable off this machine (host {spec.host}); TensorBoard has no auth"
            )
        listening = probe(spec.host, spec.port)
        state = "listening" if listening else "free"
        print(f"board {spec.name:<18} port {spec.port} {state:<10} {spec.target}")
        for issue in issues:
            problems += 1
            print(f"  ! {issue}")
    print("\nno problems found" if not problems else f"\n{problems} problem(s) found")
    return 1 if problems else 0


def cmd_service(args: argparse.Namespace) -> int:
    if args.command_name == "install-service":
        return service.install(enable=not args.no_enable, linger=args.linger)
    return service.uninstall()


def cmd_install(args: argparse.Namespace) -> int:
    """Install everything: the boot service, the launcher entry and `tensorwatch` on PATH."""
    status = 0
    if not args.no_service:
        status |= service.install(enable=True, linger=not args.no_linger)
    for note in desktop.install():
        print(note)
    cfg = load()
    print(
        f"\n{desktop.APP_NAME} installed. Launch it from your application menu, "
        f"or run `tensorwatch open` ({cfg.dashboard_url})."
    )
    return status


def cmd_uninstall(args: argparse.Namespace) -> int:
    for note in desktop.uninstall():
        print(note)
    if not args.keep_service:
        service.uninstall()
    return 0


# ---------------------------------------------------------------------- parser


def cmd_queue(args: argparse.Namespace) -> int:
    """One-shot view of the mlq queue, straight from the mlqd socket."""
    cfg = load()
    path = Path(cfg.queue.socket).expanduser() if cfg.queue.socket else None
    snapshot = queue.one_shot(path, cfg.board_dirs)
    if args.json:
        print(json.dumps(snapshot.to_json(), indent=2))
        return 0 if snapshot.connected else 1
    if not snapshot.connected:
        return _fail(f"mlq queue unavailable: {snapshot.error}")

    limit = f"/{snapshot.effective_limit}" if snapshot.effective_limit else ""
    print(f"{snapshot.active_leases}{limit} running, {len(snapshot.queued)} queued"
          + (" (admission blocked)" if snapshot.admission_blocked else ""))
    rows = []
    for job in (*snapshot.running, *snapshot.queued):
        age = _age(job.since if job.state == "running" else job.queued_at)
        rows.append([
            str(job.id), job.name, job.state, age,
            job.reason or "-", job.board or "-", job.project or "-",
        ])
    if rows:
        print(_table(rows, ["job", "name", "state", "age", "reason", "board", "project"]))
    return 0


def _age(since: float | None) -> str:
    if not since:
        return "-"
    seconds = max(0.0, time.time() - since)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tensorwatch",
        description="Registry + supervisor + dashboard for long-lived TensorBoard instances.",
    )
    sub = parser.add_subparsers(dest="command_name", required=True)

    p_serve = sub.add_parser("serve", help="run the supervisor and dashboard (foreground)")
    p_serve.add_argument("--config", help="registry path (default ~/.config/tensorwatch/config.toml)")
    p_serve.add_argument("--port", type=int, help="override the dashboard port")
    p_serve.add_argument("--window", action="store_true", help="also open a dashboard window")
    p_serve.set_defaults(func=cmd_serve)

    p_add = sub.add_parser("add", help="register a logdir")
    p_add.add_argument("logdir", nargs="?", help="directory passed to tensorboard --logdir")
    p_add.add_argument("--logdir-spec", help="tensorboard --logdir_spec value instead of a logdir")
    p_add.add_argument("--name")
    p_add.add_argument("--port", type=int)
    p_add.add_argument("--autostart", choices=AUTOSTART_MODES, default="always")
    p_add.add_argument("--reload-interval", type=float)
    p_add.add_argument("--samples-per-plugin", help="e.g. scalars=2000,images=0")
    p_add.add_argument("--command", nargs="+", help="launcher instead of `tensorboard`")
    p_add.add_argument("--arg", action="append", help="extra tensorboard flag (repeatable)")
    p_add.add_argument("--description")
    p_add.add_argument("--disabled", action="store_true")
    p_add.add_argument("--force", action="store_true", help="register a missing logdir")
    p_add.set_defaults(func=cmd_add)

    p_rm = sub.add_parser("rm", help="unregister a board")
    p_rm.add_argument("name")
    p_rm.set_defaults(func=cmd_rm)

    p_set = sub.add_parser("set", help="set board keys, e.g. tensorwatch set cleanrl autostart=on_demand")
    p_set.add_argument("name")
    p_set.add_argument("assignment", nargs="+")
    p_set.set_defaults(func=cmd_set)

    for verb in ("enable", "disable"):
        node = sub.add_parser(verb, help=f"{verb} a board in the registry")
        node.add_argument("name")
        node.set_defaults(func=cmd_toggle)

    p_list = sub.add_parser("list", help="list registered boards")
    p_list.set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="live status from the running manager")
    p_status.add_argument("name", nargs="?")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    for verb in ("start", "stop", "restart"):
        node = sub.add_parser(verb, help=f"{verb} one board in the running manager")
        node.add_argument("name")
        node.set_defaults(func=cmd_control)

    p_logs = sub.add_parser("logs", help="show a board's tensorboard output")
    p_logs.add_argument("name")
    p_logs.add_argument("-n", "--lines", type=int, default=200)
    p_logs.add_argument("-f", "--follow", action="store_true")
    p_logs.set_defaults(func=cmd_logs)

    p_open = sub.add_parser("open", help="open the dashboard (or one board) in a window")
    p_open.add_argument("name", nargs="?")
    p_open.add_argument("--tab", action="store_true", help="normal browser tab instead of app window")
    p_open.add_argument("--no-start", action="store_true",
                        help="do not start the service when it is down")
    p_open.set_defaults(func=cmd_open)

    p_queue = sub.add_parser("queue", help="show the mlq queue as tensorwatch sees it")
    p_queue.add_argument("--json", action="store_true")
    p_queue.set_defaults(func=cmd_queue)

    p_scan = sub.add_parser("scan", help="find logdirs under a directory")
    p_scan.add_argument("root")
    p_scan.add_argument("--depth", type=int, default=4)
    p_scan.add_argument("--add", action="store_true", help="register everything new")
    p_scan.add_argument("--autostart", choices=AUTOSTART_MODES, default="always")
    p_scan.set_defaults(func=cmd_scan)

    p_doctor = sub.add_parser("doctor", help="check registry, ports, commands and logdirs")
    p_doctor.set_defaults(func=cmd_doctor)

    p_install = sub.add_parser("install-service", help="install the systemd --user unit")
    p_install.add_argument("--no-enable", action="store_true", help="write the unit but do not enable")
    p_install.add_argument("--linger", action="store_true",
                           help="also enable-linger so boards start at boot without a login")
    p_install.set_defaults(func=cmd_service)

    p_uninstall = sub.add_parser("uninstall-service", help="remove the systemd --user unit")
    p_uninstall.set_defaults(func=cmd_service)

    p_app = sub.add_parser("install", help="install the boot service, the app launcher and `tensorwatch` on PATH")
    p_app.add_argument("--no-service", action="store_true", help="skip the systemd unit")
    p_app.add_argument("--no-linger", action="store_true",
                       help="do not enable-linger (boards then start at first login)")
    p_app.set_defaults(func=cmd_install)

    p_app_rm = sub.add_parser("uninstall", help="remove the app launcher and the systemd unit")
    p_app_rm.add_argument("--keep-service", action="store_true")
    p_app_rm.set_defaults(func=cmd_uninstall)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        return _fail(str(exc))
    except registry.RegistryError as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
