# TensorWatch

One registry of TensorBoard logdirs, one supervisor that keeps them running across
reboots, one window that shows all of them — plus the local `mlq` queue, so the
runs that are training and the runs that are waiting sit next to the boards that
plot them.

- **Registry** — a hand-editable TOML file (`~/.config/tensorwatch/config.toml`).
  Each `[[board]]` is a name, a logdir and a stable port.
- **Supervisor** — one `tensorboard` process per board, health-checked and
  restarted on failure, started at boot through a `systemd --user` unit.
- **Dashboard** — `http://127.0.0.1:6005/`: sidebar of boards with live state,
  memory and CPU; the selected board renders in the pane; the mlq queue sits at
  the bottom of the sidebar.
- **Desktop app** — `tensorwatch install` adds a *TensorWatch* launcher entry, so
  it opens in its own window like any other app.

Zero third-party dependencies: stdlib Python 3.11+, so the boot-time unit cannot
break on a virtualenv.

## Quickstart

```bash
cd tensorwatch

bin/tensorwatch add ~/Documents/repositories/cleanrl/runs        # register a logdir
bin/tensorwatch scan ~/Documents/repositories --depth 4          # discover more; --add registers
bin/tensorwatch install                                          # service + app launcher + PATH
bin/tensorwatch open                                             # window against the running service
bin/tensorwatch list                                             # registry + live state
bin/tensorwatch queue                                            # mlq queue as the dashboard sees it
```

`install` writes `~/.config/systemd/user/tensorwatch.service`, enables it, runs
`loginctl enable-linger` (so boards come up at boot, not at first login), installs
the launcher entry and icon, and symlinks `~/.local/bin/tensorwatch`.

## Registry format

```toml
[server]
port = 6005        # dashboard
port_base = 6100   # first auto-assigned board port (6006-6010 stay free for ad-hoc boards)
keep_warm = 2      # board panes kept mounted in the dashboard

[queue]
enabled = true     # mlq panel in the sidebar
visible = 5        # queued jobs shown before "+N more"

[defaults]         # applied to every board
reload_interval = 60

[[board]]
name = "cleanrl"
logdir = "~/Documents/repositories/cleanrl/runs"
port = 6100
autostart = "always"                # always | on_demand | manual
reload_interval = 300               # seconds between event-file rescans
samples_per_plugin = "scalars=2000,images=0"
args = ["--load_fast=true"]         # any extra tensorboard flags
env = { CUDA_VISIBLE_DEVICES = "" }
description = "RL ablations"
```

Other keys: `logdir_spec` (instead of `logdir`), `enabled`, `command` (a launcher
other than `tensorboard`, e.g. `["uv", "run", "--project", "/repo", "tensorboard"]`),
`cwd`, `host`, `idle_timeout`, `start_timeout`.

Ports are part of the contract — the dashboard links straight at them — so an
auto-assigned port is written back into the registry the first time it is used and
never moves afterwards, including across reboots.

That matters beyond bookmarks: TensorBoard keeps pinned cards, smoothing and the
rest of its UI state in browser storage keyed by origin, i.e. by port. So
allocation is monotonic — a removed board's port is never handed to another logdir
— and changing a board's `port` by hand (or with `set`) leaves its saved
TensorBoard state behind on the old one.

Edits made by `add/rm/set/enable/disable/scan` are line-level: comments, ordering
and formatting survive, and an edit that would not parse is rejected before
anything is written.

## Autostart policies

| policy | behaviour |
| --- | --- |
| `always` | started with the manager, restarted forever. The reboot case. |
| `on_demand` | started when the dashboard opens the board; stopped after `idle_timeout` (default 15 min) without a viewer. Use for huge archives. |
| `manual` | only ever started by an explicit `tensorwatch start` or dashboard button. |

## mlq queue panel

TensorWatch **subscribes** to `mlqd`: it opens the mlqueue Unix socket, sends one
`subscribe` op, and then receives a full queue snapshot every time the queue
changes. No polling, no `mlq` subprocess in the loop. (Needs an mlqueue build with
the `subscribe` op; `mlq subscribe` streams the same snapshots as NDJSON.)

The panel at the bottom of the sidebar shows running jobs with elapsed time, then
the first `visible` queued jobs with mlq's own admission reason
(`protected_drain`, `backfill_window_open`, …), and collapses the rest behind
`+N more`.

Jobs are matched to boards by the paths they name: a run that passes
`--run-dir runs/vapo` or `--output samples/rl/runs/x` identifies its own logdir, so
only the board watching that path is marked. The working directory is a fallback,
used only when it leaves exactly one candidate — a repository holding several
watched logdirs (`tb_logs`, `postraining/runs`) cannot say which one is moving, and
a run writing somewhere nobody watches marks nothing. That gives two things:

- the job row is tagged with the board (`▸ xxscreeps`) and clicking it opens that
  board, so the queue and the plots are one click apart;
- the board row itself shows what is happening to it: `▶ 8m` while a run of that
  project is training (the state dot picks up a soft halo), `+2` when work for it
  is still queued. The tooltip names the jobs.

If mlqd is not running the panel says so and keeps retrying with backoff; set
`[queue] enabled = false` to hide it entirely, or `[queue] socket = "/path"` to
point at a non-standard mlqueue runtime directory.

## Performance decisions

TensorBoard is the expensive part, so TensorWatch stays out of the way:

- **No proxy in the data path.** Boards are embedded directly on their own
  loopback ports; TensorBoard's multi-megabyte payloads never pass through this
  process, so it cannot become a bottleneck or stall a board.
- **Panes mount lazily and are evicted (LRU).** A hidden iframe still holds a
  complete TensorBoard front-end — tens of MB of JS heap plus its own polling — so
  only `keep_warm` panes stay alive. `w` drops the open one, `d` detaches it into
  its own window.
- **Staggered cold start.** Every board walks its whole logdir on startup, so
  starts are spaced by `start_stagger` with at most `max_warming` boards warming
  at once: login is not an I/O storm.
- **Slower rescans.** TensorBoard's default `--reload_interval` of 5 s means a
  permanent rescan of every event file; the default here is 60 s, and huge trees
  should go higher. `samples_per_plugin` caps what is held in memory.
- **Push, not poll.** One SSE stream carries board state and the queue. Liveness
  is a TCP connect; per-board RSS/CPU come from a single `/proc` pass every 5 s.
- **Cheap front-end.** Rows are created once and patched in place, renders are
  coalesced into an animation frame, and all rendering, timers and heartbeats stop
  while the window is hidden.

## Commands

```
serve [--config P] [--port N] [--window]   supervisor + dashboard (foreground)
install [--no-service] [--no-linger]       systemd unit + app launcher + PATH symlink
uninstall [--keep-service]
add PATH [--name N] [--port N] [--autostart M] [--reload-interval S]
        [--samples-per-plugin S] [--arg=--flag] [--command ...] [--description D]
        [--logdir-spec S] [--disabled] [--force]
rm NAME | enable NAME | disable NAME
set NAME KEY=VALUE ...                     e.g. set cleanrl autostart=on_demand
list | status [NAME] [--json] | queue [--json]
start|stop|restart NAME                    control the running manager
logs NAME [-n N] [-f]                      that board's tensorboard output
open [NAME] [--tab] [--no-start]           window; starts the service if it is down
scan ROOT [--depth N] [--add] [--autostart M]
doctor                                     registry, ports, commands, logdirs, exposure
install-service [--no-enable] [--linger] | uninstall-service
```

`SIGHUP` (`systemctl --user reload tensorwatch`) re-reads the registry without
dropping running boards; only boards whose command line changed are restarted.

## Dashboard keys

`1`–`9` select board, `/` filter, `w` close the open pane, `d` detach it,
`Shift`+`R` reload it, `q` collapse the queue, `r` reload the registry.

## Trust boundary

Everything binds loopback and nothing is authenticated, which is right for a
single-user workstation but worth stating:

- The dashboard refuses cross-origin requests (Host allowlist + `Origin` check)
  and forbids being framed, so no web page can drive it. Any *process* running as
  your user can, because there is no credential — a shared machine wants an SSH
  tunnel to a firewalled host instead.
- Boards serve whole logdir trees with no auth, so a non-loopback `host` (or
  `--bind_all` in `args`) is **rejected** unless `[server] allow_remote = true` is
  set explicitly; `doctor` keeps flagging those boards.
- Board logs (`~/.local/state/tensorwatch/logs/`) are 0600 and rotate in place at
  8 MB, keeping the last 512 KB in `<board>.log.1`.

## Layout

```
src/tensorwatch/config.py       registry model + validation
src/tensorwatch/registry.py     comment-preserving TOML edits
src/tensorwatch/supervisor.py   process supervision, health, backoff, idle stop
src/tensorwatch/procstats.py    per-process-group RSS/CPU from one /proc pass
src/tensorwatch/queue.py        mlqd subscription client (framed JSON over a Unix socket)
src/tensorwatch/httpd.py        dashboard assets + JSON/SSE control API
src/tensorwatch/web/            dashboard (vanilla JS, no build step)
src/tensorwatch/cli.py          command line interface
src/tensorwatch/service.py      systemd --user unit
src/tensorwatch/desktop.py      launcher entry, icon, PATH symlink
```

State lives in `~/.local/state/tensorwatch/` (`logs/<board>.log`, `logs/supervisor.log`).

## Tests

```bash
python3 -m pytest tests -q
```

The supervisor tests drive a fake `tensorboard` that binds the requested port and
can be made to hang or crash; the queue tests drive a fake `mlqd` over a real Unix
socket, so start-up, health, backoff, idle-stop, reload, subscription and
reconnect paths are all exercised against real processes and sockets.
