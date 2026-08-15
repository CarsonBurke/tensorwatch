# tensorboard-manager (`tbmgr`)

One registry of TensorBoard logdirs, one supervisor that keeps them running across
reboots, one dashboard window that shows all of them.

- **Registry**: a hand-editable TOML file (`~/.config/tbmgr/config.toml`). Each
  `[[board]]` is a name, a logdir and a stable port.
- **Supervisor**: one `tensorboard` process per board, restarted on failure,
  started at boot through a `systemd --user` unit.
- **Dashboard**: `http://127.0.0.1:6005/` - status for every board, one-click
  start/stop/restart, log tails, and an embedded pane per board.

Zero dependencies: stdlib Python 3.11+, so the boot-time unit does not depend on a
virtualenv resolving correctly.

## Quickstart

```bash
cd tensorboard-manager

# register logdirs (name and port are derived; both can be overridden)
bin/tbmgr add ~/Documents/repositories/cleanrl/runs
bin/tbmgr add ~/Documents/repositories/parameter-golf/tb_logs
bin/tbmgr scan ~/Documents/repositories --depth 3        # discover; --add to register

bin/tbmgr serve --window        # foreground: supervisor + dashboard + window
bin/tbmgr install-service --linger   # or: run it forever, from boot
bin/tbmgr list                  # registry + live state
bin/tbmgr open                  # dashboard window against the running manager
```

`install-service` writes `~/.config/systemd/user/tbmgr.service` and enables it.
`--linger` additionally runs `loginctl enable-linger`, which starts the boards at
boot instead of at first login.

## Registry format

```toml
[server]
port = 6005        # dashboard
port_base = 6100   # first auto-assigned board port
keep_warm = 2      # board panes kept mounted in the dashboard

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

Ports are part of the contract - the dashboard links straight at them - so an
auto-assigned port is written back into the registry the first time it is used and
never moves afterwards.

Edits made by `tbmgr add/rm/set/enable/disable/scan` are line-level: your comments,
ordering and formatting survive. An edit that would not parse is rejected before
anything is written.

## Autostart policies

| policy | behaviour |
| --- | --- |
| `always` | started with the manager, restarted forever. The reboot case. |
| `on_demand` | started when the dashboard opens the board; stopped after `idle_timeout` (default 15 min) without a viewer. Use for huge logdirs. |
| `manual` | only ever started by an explicit `tbmgr start` / dashboard button. |

## Trust boundary

Everything binds loopback and nothing is authenticated, which is the right shape
for a single-user workstation but worth stating plainly:

- The dashboard refuses cross-origin requests (Host allowlist + `Origin` check)
  and forbids being framed, so no web page can drive it. Any *process* running as
  any local user can, because there is no credential - a shared machine wants an
  SSH tunnel to a firewalled host instead.
- Boards serve whole logdir trees with no auth, so a non-loopback `host` (or
  `--bind_all` in `args`) is **rejected** unless `[server] allow_remote = true` is
  set explicitly; `tbmgr doctor` then keeps flagging those boards.
- Board logs (`~/.local/state/tbmgr/logs/`) are 0600 and rotate in place at 8 MB,
  keeping the last 512 KB in `<board>.log.1`.

## Performance decisions

TensorBoard is the expensive part, so the manager's job is to stay out of the way:

- **No proxy in the data path.** Boards are embedded directly on their own
  loopback ports. TensorBoard's multi-megabyte scalar payloads never pass through
  this process, so the manager cannot become a bottleneck or stall a board.
- **Panes are mounted lazily and evicted (LRU).** A hidden iframe still holds a
  complete TensorBoard front-end - tens of MB of JS heap plus its own polling - so
  the dashboard keeps only `keep_warm` panes alive and unmounts the rest. `x` on a
  tab (or `w`) frees one immediately; `d` detaches a board into its own window.
- **Staggered cold start.** Every board walks its whole logdir on startup. Starts
  are spaced by `start_stagger` seconds with at most `max_warming` boards warming
  at once, so login is not an I/O storm.
- **Slower rescans.** TensorBoard's default `--reload_interval` of 5s means a
  permanent rescan of every event file; the default here is 60s, and huge trees
  should go higher. `samples_per_plugin` caps what is held in memory.
- **Push, not poll.** Status reaches the browser over one SSE stream. Liveness is
  a TCP connect, and per-board RSS/CPU come from a single `/proc` pass every 5s
  (visible in the sidebar, so it is obvious which board is eating the machine).

## Commands

```
serve [--config P] [--port N] [--window]   run supervisor + dashboard (foreground)
add PATH [--name N] [--port N] [--autostart M] [--reload-interval S]
        [--samples-per-plugin S] [--arg=--flag] [--command ...] [--description D]
        [--logdir-spec S] [--disabled] [--force]
rm NAME | enable NAME | disable NAME
set NAME KEY=VALUE ...                     e.g. set cleanrl autostart=on_demand
list                                       registry + live state
status [NAME] [--json]                     live state from the running manager
start|stop|restart NAME                    control the running manager
logs NAME [-n N] [-f]                      that board's tensorboard output
open [NAME] [--tab]                        app window for the dashboard or a board
scan ROOT [--depth N] [--add] [--autostart M]
doctor                                     registry, ports, commands, logdirs
install-service [--no-enable] [--linger] | uninstall-service
```

`SIGHUP` (`systemctl --user reload tbmgr`) re-reads the registry without dropping
running boards; only boards whose command line changed are restarted.

## Dashboard keys

`1`-`9` switch board, `/` filter, `w` unmount pane, `d` detach into its own
window, `r` reload pane.

## Layout

```
src/tbmgr/config.py       registry model + validation
src/tbmgr/registry.py     comment-preserving TOML edits
src/tbmgr/supervisor.py   process supervision, health, backoff, idle stop
src/tbmgr/procstats.py    per-process-group RSS/CPU from one /proc pass
src/tbmgr/httpd.py        dashboard assets + JSON/SSE control API
src/tbmgr/web/            dashboard (vanilla JS, no build step)
src/tbmgr/cli.py          command line interface
src/tbmgr/service.py      systemd --user unit
```

State lives in `~/.local/state/tbmgr/` (`logs/<board>.log`, `logs/supervisor.log`).

## Tests

```bash
python3 -m pytest tests -q
```

The supervisor tests run a fake `tensorboard` that binds the requested port and
can be made to hang or crash, so start-up, health, backoff, idle-stop and reload
paths are exercised against real processes.
