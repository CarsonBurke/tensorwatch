"""Command line interface: ``tensorwatch <command>``."""

from __future__ import annotations

import argparse
import json
import os
import resource
import secrets
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
from typing import Any, Callable, Sequence

from . import activity, desktop, discover, httpd, queue, registry, service
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
from .supervisor import Supervisor, probe, raise_file_limit

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


def open_window(url: str, app_mode: bool = True) -> str:
    """Open ``url`` in a chrome-style app window (a plain window if unavailable)."""
    candidates = (
        "chromium", "google-chrome-stable", "google-chrome", "brave", "brave-browser",
        "microsoft-edge", "vivaldi",
    )
    env = os.environ.copy()
    # Chromium looks up its window/tray icon via this desktop file name.
    env["CHROME_DESKTOP"] = f"{desktop.APP_ID}.desktop"
    desktop.install_window_entry(url)
    profile = desktop.chrome_profile()
    for name in candidates:
        binary = shutil.which(name)
        if not binary:
            continue
        argv = [binary, f"--app={url}"] if app_mode else [binary, "--new-window", url]
        # A shared Chromium instance ignores --class; a private profile does not.
        argv.extend((
            f"--class={desktop.APP_ID}",
            f"--name={desktop.APP_NAME}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
        ))
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
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
    soft, _hard = raise_file_limit()
    if soft < 4096:
        print(
            f"tensorwatch: warning: only {soft} file descriptors available; boards with "
            "thousands of event files will fail to load",
            file=sys.stderr,
        )

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
        ops.forget_suggestions()
        supervisor.reload(fresh)
        if subscriber is not None:
            # Re-match the queue against the new registry instead of waiting for
            # mlqd's next push, which for a long run can be hours away.
            subscriber.refresh()

    watcher = activity.Watcher(
        lambda: current["cfg"].board_dirs, supervisor.publish
    )
    supervisor.set_activity_source(watcher.snapshot)
    watcher.start()

    subscriber = None
    if cfg.queue.enabled:
        subscriber = queue.start(
            Path(cfg.queue.socket).expanduser() if cfg.queue.socket else None,
            lambda: current["cfg"].board_dirs,
            supervisor.publish,
            lambda: {
                name
                for name, sample in watcher.snapshot().items()
                if activity.writing(sample)
            },
        )
        supervisor.set_queue_source(subscriber.snapshot)

    def do_cancel(job: int):
        path = Path(current["cfg"].queue.socket).expanduser() if current["cfg"].queue.socket else None
        return queue.cancel(job, path)

    ops = RegistryOps(path, do_reload, lambda: current["cfg"])

    server = httpd.serve(
        supervisor,
        cfg.server.host,
        cfg.server.port,
        do_reload,
        do_cancel if cfg.queue.enabled else None,
        ops.add,
        ops.autostart,
        ops.remove,
        ops.restore,
        discover.complete,
        ops.suggest,
    )
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
    watcher.shutdown()
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


def set_autostart(name: str, mode: str, path: Path | None = None) -> Config:
    """Persist a board's autostart policy in the registry.

    The registry is the only place this lives, so a mode flipped from the
    dashboard survives a manager restart exactly like a hand edit does.
    """
    if mode not in AUTOSTART_MODES:
        raise registry.RegistryError(
            f"autostart must be one of {', '.join(AUTOSTART_MODES)}, got {mode!r}"
        )

    def mutate(text: str) -> str:
        return registry.set_key(text, name, "autostart", mode)

    return _apply(path or config_path(), mutate)


def add_board(
    logdir: str | None = None,
    *,
    logdir_spec: str | None = None,
    name: str | None = None,
    force: bool = False,
    port: int | None = None,
    autostart: str = "always",
    reload_interval: float | None = None,
    samples_per_plugin: str | None = None,
    command: Sequence[str] | None = None,
    extra_args: Sequence[str] | None = None,
    description: str | None = None,
    disabled: bool = False,
    path: Path | None = None,
) -> dict[str, Any]:
    """Append a board to the registry. Raises ``RegistryError`` on a bad request."""
    path = path or config_path()
    registry.ensure(path)
    cfg = load(path)

    entry: dict[str, Any] = {}
    if logdir_spec:
        entry["logdir_spec"] = logdir_spec
        default_name = discover.slug(logdir_spec.split(":", 1)[0])
    else:
        if not logdir:
            raise registry.RegistryError("give a logdir or --logdir-spec")
        target = Path(os.path.expandvars(logdir)).expanduser().resolve()
        if not target.exists() and not force:
            raise registry.RegistryError(f"{target} does not exist")
        if target.exists() and not target.is_dir():
            raise registry.RegistryError(f"{target} is not a directory")
        # Two boards on one logdir are two TensorBoards doing the same work.
        twin = next((spec for spec in cfg.boards if _same_dir(spec.logdir, target)), None)
        if twin is not None:
            raise registry.RegistryError(f"{target} is already registered as {twin.name!r}")
        entry["logdir"] = str(target)
        # A second logdir in the same repository still gets a name of its own.
        default_name = discover.unique_name(target, lambda taken: cfg.board(taken) is not None)

    resolved = name or default_name
    if not NAME_RE.match(resolved):
        raise registry.RegistryError(
            f"invalid name {resolved!r} (allowed: letters, digits, . _ -)"
        )
    if cfg.board(resolved):
        raise registry.RegistryError(f"board {resolved!r} already registered")

    taken = {spec.port for spec in cfg.boards} | {cfg.server.port}
    assigned = port or next_free_port(cfg.server.port_base, taken)
    if assigned in taken:
        raise registry.RegistryError(f"port {assigned} already used by another board")

    entry = {"name": resolved, **entry, "port": assigned}
    if autostart != "always":
        entry["autostart"] = autostart
    if reload_interval is not None:
        entry["reload_interval"] = reload_interval
    if samples_per_plugin:
        entry["samples_per_plugin"] = samples_per_plugin
    if command:
        entry["command"] = command
    if extra_args:
        entry["args"] = list(extra_args)
    if description:
        entry["description"] = description
    if disabled:
        entry["enabled"] = False

    _apply(path, lambda text: registry.add(text, entry))
    return entry


def suggest_boards(cfg: Config, limit: int = 24) -> list[dict[str, Any]]:
    """Unregistered logdirs near the registered ones, with the name each would get."""
    registered = [spec.logdir.resolve() for spec in cfg.boards if spec.logdir is not None]
    taken = {spec.name for spec in cfg.boards}
    items: list[dict[str, Any]] = []
    for root in discover.workspaces(registered):
        for candidate in discover.discover(root):
            # A parent or child of a registered logdir would show the same runs.
            if any(candidate == known or candidate.is_relative_to(known)
                   or known.is_relative_to(candidate) for known in registered):
                continue
            name = discover.unique_name(candidate, taken.__contains__)
            taken.add(name)
            items.append(
                {
                    "path": discover.tilde(candidate),
                    "name": name,
                    "events": discover.count_events(
                        candidate, limit=1000, budget=discover.COUNT_BUDGET
                    ),
                }
            )
    # The busiest logdirs first: an empty-looking one is rarely what is wanted.
    items.sort(key=lambda item: (-item["events"], item["path"]))
    return items[:limit]


def _same_dir(logdir: Path | None, target: Path) -> bool:
    return logdir is not None and (logdir == target or logdir.resolve() == target)


def resolve_board(cfg: Config, ref: str) -> str:
    """A board's name, given its name or its logdir."""
    if cfg.board(ref):
        return ref
    target = Path(os.path.expandvars(ref)).expanduser().resolve()
    for spec in cfg.boards:
        if _same_dir(spec.logdir, target):
            return spec.name
    known = ", ".join(spec.name for spec in cfg.boards) or "<none>"
    raise registry.RegistryError(f"no board named {ref!r} (registered: {known})")


def remove_board(name: str, path: Path | None = None) -> registry.Taken:
    """Unregister a board; the result is what :func:`restore_board` needs to undo it."""
    taken: list[registry.Taken] = []

    def mutate(text: str) -> str:
        updated, cut = registry.take(text, name)
        taken.append(cut)
        return updated

    _apply(path or config_path(), mutate)
    return taken[0]


def restore_board(taken: registry.Taken, path: Path | None = None) -> str:
    """Put a removed board back where it was; returns its name."""
    path = path or config_path()
    before = {spec.name for spec in load(path).boards}
    cfg = _apply(path, lambda text: registry.insert(text, taken))
    return next(spec.name for spec in cfg.boards if spec.name not in before)


class RegistryOps:
    """The registry edits the dashboard may ask for.

    They arrive on HTTP threads, so they are serialised here: two additions at
    once would otherwise both read the registry, pick the same port, and one
    would overwrite the other.

    Removal is undoable, and the removed block never leaves this process. The
    dashboard gets an opaque token instead: the HTTP API is unauthenticated, and
    a client that could hand back registry text could register a board with any
    ``command`` it liked.
    """

    #: How long, and how many, removed boards stay restorable.
    UNDO_TTL = 900.0
    UNDO_KEEP = 32
    #: Suggestions walk every repository next to a registered one.
    SUGGEST_TTL = 60.0

    def __init__(
        self,
        path: Path,
        reload: Callable[[], None],
        config: Callable[[], Config],
    ) -> None:
        self._path = path
        self._reload = reload
        self._config = config
        self._lock = threading.RLock()
        self._undo: dict[str, tuple[float, registry.Taken]] = {}
        #: Board order with the removed-but-restorable ones still in their place,
        #: so undoing several removals in any order rebuilds it.
        self._order: list[str] = []
        self._suggested: tuple[float, list[dict[str, Any]]] | None = None
        self._generation = 0

    def add(
        self, logdir: str, name: str | None = None, autostart: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            entry = add_board(logdir, name=name, autostart=autostart or "always", path=self._path)
            self._reload()
        return {
            "ok": True,
            "board": entry["name"],
            "logdir": entry.get("logdir"),
            "port": entry["port"],
            "events": discover.has_events(entry["logdir"]),
        }

    def autostart(self, name: str, mode: str) -> dict[str, Any]:
        with self._lock:
            set_autostart(name, mode, self._path)
            self._reload()
        return {"ok": True, "board": name, "autostart": mode}

    def remove(self, name: str) -> dict[str, Any]:
        with self._lock:
            self._remember_order()
            taken = remove_board(name, self._path)
            now = time.monotonic()
            self._undo = {
                token: kept for token, kept in self._undo.items()
                if now - kept[0] < self.UNDO_TTL
            }
            while len(self._undo) >= self.UNDO_KEEP:
                del self._undo[next(iter(self._undo))]
            token = secrets.token_urlsafe(16)
            self._undo[token] = (now, taken)
            self._reload()
        return {"undo": token}

    def restore(self, token: str) -> dict[str, Any]:
        with self._lock:
            kept = self._undo.get(token)
            if kept is None or time.monotonic() - kept[0] >= self.UNDO_TTL:
                self._undo.pop(token, None)
                raise registry.RegistryError("that removal can no longer be undone")
            taken = kept[1]
            removed = registry.blocks(taken.chunk)[0].name
            # What followed it then may itself be gone; the remembered order knows
            # what should follow it now.
            after = self._order[self._order.index(removed) + 1 :] if removed in self._order else []
            name = restore_board(replace(taken, following=tuple(after)), self._path)
            del self._undo[token]
            self._reload()
        return {"ok": True, "board": name}

    def _remember_order(self) -> None:
        current = [spec.name for spec in load(self._path).boards]
        pending = {registry.blocks(taken.chunk)[0].name for _, taken in self._undo.values()}
        known = [name for name in self._order if name in current or name in pending]
        self._order = known + [name for name in current if name not in known]

    def suggest(self) -> list[dict[str, Any]]:
        with self._lock:
            generation = self._generation
            cached = self._suggested
        if cached is not None and time.monotonic() - cached[0] < self.SUGGEST_TTL:
            return cached[1]
        items = suggest_boards(self._config())
        with self._lock:
            # A registry change while walking makes this answer stale already.
            if generation == self._generation:
                self._suggested = (time.monotonic(), items)
        return items

    def forget_suggestions(self) -> None:
        with self._lock:
            self._generation += 1
            self._suggested = None


def cmd_add(args: argparse.Namespace) -> int:
    logdirs = args.logdir or [None]
    if len(logdirs) > 1 and (args.name or args.port or args.logdir_spec):
        return _fail("--name, --port and --logdir-spec describe one board; add the others separately")
    failed = 0
    added = 0
    for logdir in logdirs:
        try:
            entry = add_board(
                logdir,
                logdir_spec=args.logdir_spec,
                name=args.name,
                force=args.force,
                port=args.port,
                autostart=args.autostart,
                reload_interval=args.reload_interval,
                samples_per_plugin=args.samples_per_plugin,
                command=args.command,
                extra_args=args.arg,
                description=args.description,
                disabled=args.disabled,
            )
        except (registry.RegistryError, ConfigError) as exc:
            hint = " (pass --force to register anyway)" if "does not exist" in str(exc) else ""
            failed += _fail(f"{exc}{hint}")
            continue
        added += 1
        target = entry.get("logdir") or entry.get("logdir_spec")
        print(f"registered {entry['name']} -> {target} on port {entry['port']}")
        if "logdir" in entry and not discover.has_events(entry["logdir"]):
            print("  note: no event files there yet", file=sys.stderr)
    if added and _notify_reload(load()):
        print("running manager reloaded")
    return 1 if failed else 0


def cmd_rm(args: argparse.Namespace) -> int:
    path = config_path()
    cfg = load(path)
    failed = 0
    removed = 0
    for ref in args.name:
        try:
            name = resolve_board(load(path), ref)
            remove_board(name, path)
        except (registry.RegistryError, ConfigError) as exc:
            failed += _fail(str(exc))
            continue
        removed += 1
        print(f"removed {name}")
    if removed and _notify_reload(cfg):
        print("running manager reloaded")
    return 1 if failed else 0


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
        if key == "port":
            print(
                "  note: TensorBoard keeps pins and settings per origin, so this "
                "board's saved UI state stays with its old port",
                file=sys.stderr,
            )
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


def cmd_scan(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser()
    if not root.is_dir():
        return _fail(f"{root} is not a directory")
    found = discover.discover(root, args.depth)
    if not found:
        print(f"no event files under {root} (depth {args.depth})")
        return 0

    cfg = load()
    known = {str(spec.logdir.resolve()): spec.name for spec in cfg.boards if spec.logdir}
    taken = {spec.port for spec in cfg.boards} | {cfg.server.port}
    rows, additions = [], []
    for candidate in found:
        if str(candidate) in known:
            rows.append([known[str(candidate)], "registered", "-", str(candidate)])
            continue
        name = discover.unique_name(candidate, lambda n: bool(cfg.board(n)) or any(e["name"] == n for e in additions))
        port = next_free_port(cfg.server.port_base, taken)
        taken.add(port)
        rows.append([name, "new", str(port), f"{candidate} ({discover.count_events(candidate)} event files)"])
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
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    print(f"open files   soft {soft}, hard {hard}")
    unit = service.unit_path()
    print(f"systemd unit {unit} {'(installed)' if unit.exists() else '(not installed)'}")
    if unit.exists():
        print(f"             {service.unit_state()}")

    for spec in cfg.boards:
        binary = shutil.which(spec.command[0]) if not os.path.isabs(spec.command[0]) else spec.command[0]
        issues = []
        if binary is None or not Path(binary).exists():
            issues.append(f"command {spec.command[0]!r} not found in PATH")
        events = 0
        if spec.logdir is not None and not spec.logdir.exists():
            issues.append("logdir missing")
        elif spec.logdir is not None:
            events = discover.count_events(spec.logdir)
            if events == 0:
                issues.append("no event files found")
        # TensorBoard's data server holds a descriptor per event file; too few and
        # the board serves "No dashboards are active for the current data set".
        limit = board_file_limit()
        if events and limit < events + 256:
            issues.append(
                f"{events} event files but only {limit} descriptors per board; run "
                "`tensorwatch install-service` to raise LimitNOFILE"
            )
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


def board_file_limit() -> int:
    """Descriptors a board process gets with the current configuration."""
    if service.unit_path().exists() and shutil.which("systemctl") is not None:
        shown = subprocess.run(
            ["systemctl", "--user", "show", "tensorwatch.service", "-p", "LimitNOFILESoft",
             "--value"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if shown.isdigit():
            return int(shown)
    return resource.getrlimit(resource.RLIMIT_NOFILE)[0]


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
    p_add.add_argument("logdir", nargs="*", help="directory passed to tensorboard --logdir")
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
    p_rm.add_argument("name", nargs="+", help="board name or its logdir")
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
    except BrokenPipeError:
        # `tensorwatch list | head` closes the pipe: exit quietly, but keep
        # SIGPIPE ignored - the serve command is a long-lived HTTP server and
        # SIG_DFL would let a disconnecting client kill it.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 128 + 13
    except ConfigError as exc:
        return _fail(str(exc))
    except registry.RegistryError as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
