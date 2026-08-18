# TensorWatch

A local supervisor for TensorBoard.

You train across several repos, each with its own `runs/` (or `tb_logs/`, …).
Starting `tensorboard --logdir …` by hand for each one does not scale: ports
get forgotten, processes die on reboot, and you end up with a pile of tabs.

1. Register each logdir once (`~/.config/tensorwatch/config.toml`).
2. A `systemd --user` service keeps one TensorBoard process per board.
3. One dashboard at `http://127.0.0.1:6005/` lists them and embeds the one
   you pick.

If [mlq](https://github.com/CarsonBurke/mlqueue) is running, the GPU queue
shows up in the same sidebar, tagged with the board each job writes to.

It is a single-user Linux workstation tool. Everything binds loopback. There
is no authentication. It is not a TensorBoard fork and not a remote service.

## Install

Needs Python 3.11+ and `tensorboard` on `PATH`. No other Python packages.

```bash
cd tensorwatch
bin/tensorwatch add ~/Documents/repositories/cleanrl/runs
bin/tensorwatch scan ~/Documents/repositories --add   # find more logdirs
bin/tensorwatch install
bin/tensorwatch open
```

`install` writes `~/.config/systemd/user/tensorwatch.service`, enables linger
so boards start at boot, adds a desktop launcher, and puts `tensorwatch` on
`PATH` (`~/.local/bin`). After that, open it from the app menu or
`tensorwatch open`.

Without systemd: `tensorwatch serve` (optionally `--window`).

## Daily use

```bash
tensorwatch add ~/path/to/runs          # register
tensorwatch list                        # registry + live state
tensorwatch open                        # dashboard window
tensorwatch open cleanrl                # that board only
tensorwatch set cleanrl autostart=on_demand
tensorwatch logs cleanrl -f
tensorwatch doctor
```

`scan` walks a tree for `events.out.tfevents*` files and groups them: a
`runs/` or `tb_logs/` ancestor becomes one board, not one per timestamped run.

## Registry

Hand-editable TOML at `~/.config/tensorwatch/config.toml`. `add` / `rm` /
`set` / `scan` edit it in place (comments and formatting stay). Reload with
`r` in the dashboard or `systemctl --user reload tensorwatch`.

```toml
[server]
port = 6005          # dashboard
port_base = 6100     # first auto-assigned board port (6006–6010 left free)

[defaults]
reload_interval = 60

[[board]]
name = "cleanrl"
logdir = "~/Documents/repositories/cleanrl/runs"
port = 6100
autostart = "always"              # always | on_demand | manual
```

Useful extras on a board: `logdir_spec`, `samples_per_plugin`, `args`,
`command` (launcher other than `tensorboard`), `cwd`, `env`, `description`,
`enabled`, `idle_timeout`. `[queue] enabled = false` hides the mlq panel.

Ports are sticky. TensorBoard stores pins and UI state per origin, so an
auto-assigned port is written back and never reused for a different logdir.
Changing a board's port by hand leaves its saved UI on the old one.

## Autostart

| Policy | When it runs |
| --- | --- |
| `always` | With the service, restarted forever. Default. |
| `on_demand` | When you open it; stopped after 15 min idle. Use for huge archives. |
| `manual` | Only `tensorwatch start NAME` or the dashboard button. |

## Commands

| Command | |
| --- | --- |
| `add PATH` | Register a logdir |
| `rm NAME` | Unregister |
| `set NAME KEY=VALUE …` | Change a board (`autostart=on_demand`, `port=6104`, …) |
| `enable` / `disable NAME` | Leave it in the file, stop supervising it |
| `scan DIR [--add]` | Discover logdirs |
| `list` / `status [NAME]` | Registry / live state |
| `start` / `stop` / `restart NAME` | Control a running board |
| `logs NAME [-f]` | That board's TensorBoard output |
| `open [NAME]` | Dashboard (or one board) in an app window |
| `queue` | mlq queue as the dashboard sees it |
| `doctor` | Registry, ports, commands, missing logdirs |
| `serve` | Supervisor + dashboard in the foreground |
| `install` / `uninstall` | Service, launcher, PATH symlink |

`tensorwatch <cmd> -h` for flags.

## Dashboard

`1`–`9` switch board, `Ctrl`+`1`–`0` the same through board 10, `/` filter,
`w` close the open pane, `d` detach it, `Shift`+`R` reload it, `q` collapse
the queue, `r` reload the registry. Each queue row has a cancel control.

A row marked `▶ 12s` is receiving data: that is the newest `events.out.tfevents*`
write under its logdir, so it counts any run under `runs/` no matter who started
it. `○2` counts mlq jobs still queued for that board.

The dashboard only keeps a few TensorBoard UIs mounted (`keep_warm`, default
2). The processes themselves follow each board's autostart policy.

## Notes

- Loopback only. A non-loopback `host` or `--bind_all` is rejected unless
  `[server] allow_remote = true`. TensorBoard has no auth.
- State and logs: `~/.local/state/tensorwatch/`.
- TensorBoard opens one file per event file, so the service sets
  `LimitNOFILE=65536`. With the usual 1024 a board with thousands of runs loads
  nothing and says "No dashboards are active for the current data set";
  `doctor` flags that.
- Tests: `python3 -m pytest tests -q`.
