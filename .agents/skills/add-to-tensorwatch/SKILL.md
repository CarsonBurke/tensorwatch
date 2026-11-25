---
name: add-to-tensorwatch
description: Use when asked to register a TensorBoard logdir, training project, or runs in TensorWatch.
---

# Add to TensorWatch

Use `tensorwatch` on the machine that runs the dashboard and can read the logs. If the command is absent from PATH, use `bin/tensorwatch` from the TensorWatch checkout. Use the CLI to preserve registry comments, assign a port, and notify the running manager.

- Check `tensorwatch list` for an existing board with the same resolved logdir. If it is already registered, report its name. For a project without a specified logdir, run `tensorwatch scan "$project"` to preview candidates. Prefer a shared run directory such as `runs/` or `tb_logs/` when the request covers the project. Register individual run directories when the user wants those isolated.
- Run `tensorwatch add "$logdir"`. The CLI derives a name and assigns a port. Pass `--name` for an explicitly named board and `--autostart on_demand` for an archive the user wants to open only when needed. A directory may be empty while training is starting. Use `--force` only when the user intends to register a directory that does not exist yet.
- Use `scan --add` only when the request covers every new candidate from the scan. Otherwise add the selected paths explicitly.
- Verify the name and logdir with `tensorwatch list`. If the manager is running, check `tensorwatch status "$name"` and retry briefly if the reload is still pending. A stopped on-demand or manual board can be registered successfully.
