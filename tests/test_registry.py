from __future__ import annotations

import pytest

from tbmgr import config, registry

SAMPLE = """\
# my notes survive edits
[server]
port = 6005

# cleanrl is the big one
[[board]]
name = "cleanrl"
logdir = "/data/cleanrl/runs"
port = 6010
# keep the reload slow, this tree is huge
reload_interval = 300

[[board]]
name = "golf"
logdir = "/data/golf/tb_logs"
port = 6011
"""


def test_blocks_are_located_with_values():
    blocks = registry.blocks(SAMPLE)
    assert [block.name for block in blocks] == ["cleanrl", "golf"]
    assert blocks[0].data["reload_interval"] == 300
    # The block must not swallow the following [[board]] header.
    assert blocks[0].end <= blocks[1].start


def test_add_appends_and_rejects_duplicates():
    updated = registry.add(SAMPLE, {"name": "kraggi", "logdir": "/data/k/runs", "port": 6012})
    assert "# my notes survive edits" in updated
    assert updated.count("[[board]]") == 3
    assert config.parse(updated).boards[-1].name == "kraggi"

    with pytest.raises(registry.RegistryError, match="already registered"):
        registry.add(updated, {"name": "kraggi", "logdir": "/x", "port": 6013})


def test_remove_only_deletes_its_own_block():
    updated = registry.remove(SAMPLE, "cleanrl")
    assert 'name = "cleanrl"' not in updated
    assert "reload_interval = 300" not in updated  # the whole block went, not just the name
    assert "# my notes survive edits" in updated
    # Comments the user wrote outside the block are never attributed to it.
    assert "# cleanrl is the big one" in updated
    boards = config.parse(updated).boards
    assert [board.name for board in boards] == ["golf"]

    with pytest.raises(registry.RegistryError, match="no board named"):
        registry.remove(updated, "cleanrl")


def test_set_key_updates_in_place_and_inserts():
    updated = registry.set_key(SAMPLE, "cleanrl", "reload_interval", 60)
    assert "reload_interval = 60\n" in updated
    assert "reload_interval = 300" not in updated
    assert "# keep the reload slow" in updated

    updated = registry.set_key(updated, "golf", "autostart", "on_demand")
    board = config.parse(updated).board("golf")
    assert board.autostart == "on_demand"

    updated = registry.unset_key(updated, "golf", "autostart")
    assert config.parse(updated).board("golf").autostart == "always"


def test_render_value_round_trips():
    entry = {
        "name": "quoted",
        "logdir": '/data/with "quotes"/runs',
        "port": 6010,
        "enabled": False,
        "reload_interval": 12.5,
        "args": ["--load_fast=true", "--purge_orphaned_data=false"],
        "env": {"CUDA_VISIBLE_DEVICES": ""},
    }
    board = config.parse(registry.render_block(entry)).boards[0]
    assert board.logdir.name == "runs"
    assert board.args == ("--load_fast=true", "--purge_orphaned_data=false")
    assert board.env == {"CUDA_VISIBLE_DEVICES": ""}
    assert board.enabled is False
    assert board.reload_interval == 12.5


def test_edit_is_atomic_and_ensures_header(home):
    path = config.config_path()
    registry.ensure(path)
    assert "[server]" in path.read_text()

    registry.edit(path, lambda text: registry.add(text, {"name": "a", "logdir": "/tmp", "port": 6010}))
    assert config.load(path).board("a").port == 6010
    assert not list(path.parent.glob("*.tmp"))  # temp file cleaned up


def test_set_ports_persists_auto_assignments(home):
    path = config.config_path()
    registry.write_atomic(path, '[[board]]\nname = "a"\nlogdir = "/tmp"\n')
    cfg = config.load(path)
    assert cfg.assigned_ports == {"a": config.DEFAULT_PORT_BASE}

    registry.set_ports(path, cfg.assigned_ports)
    reloaded = config.load(path)
    assert reloaded.assigned_ports == {}
    assert reloaded.board("a").port == config.DEFAULT_PORT_BASE


def test_malformed_block_is_reported():
    with pytest.raises(registry.RegistryError, match="cannot parse board block"):
        registry.blocks('[[board]]\nname = broken\n')


SUBTABLE = """\
[[board]]
name = "alpha"
logdir = "/data/a"
port = 6100

[[board]]
name = "beta"
logdir = "/data/b"
port = 6101
[board.env]
CUDA_VISIBLE_DEVICES = "1"
"""


def test_board_subtable_belongs_to_its_block():
    blocks = registry.blocks(SUBTABLE)
    assert blocks[1].data["env"] == {"CUDA_VISIBLE_DEVICES": "1"}

    # Removing beta must take its [board.env] with it; otherwise TOML re-binds the
    # sub-table to alpha and silently hands over the GPU pin.
    updated = registry.remove(SUBTABLE, "beta")
    boards = config.parse(updated).boards
    assert [board.name for board in boards] == ["alpha"]
    assert boards[0].env == {}
    assert "CUDA_VISIBLE_DEVICES" not in updated


def test_set_key_ignores_multiline_value_contents():
    text = """\
[[board]]
name = "alpha"
logdir = "/data/a"
port = 6100
args = [
  "--samples_per_plugin",
  "port = 1",
]
"""
    updated = registry.set_key(text, "alpha", "port", 6200)
    board = config.parse(updated).boards[0]
    assert board.port == 6200
    assert board.args == ("--samples_per_plugin", "port = 1")


def test_edit_refuses_to_persist_unparseable_text(home):
    path = config.config_path()
    registry.write_atomic(path, '[[board]]\nname = "a"\nlogdir = "/tmp"\nport = 6100\n')
    before = path.read_text()

    with pytest.raises(registry.RegistryError, match="unparseable"):
        registry.edit(path, lambda text: text + "\nthis is not toml\n")
    assert path.read_text() == before


def test_write_atomic_follows_symlinks_and_restricts_mode(home, tmp_path):
    real = tmp_path / "dotfiles" / "tbmgr.toml"
    real.parent.mkdir()
    real.write_text("[server]\n", encoding="utf-8")
    link = config.config_path()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)

    registry.write_atomic(link, '[[board]]\nname = "a"\nlogdir = "/tmp"\nport = 6100\n')
    assert link.is_symlink()  # a registry kept in a dotfiles repo stays linked
    assert config.load(link).board("a").port == 6100
    assert real.stat().st_mode & 0o777 == 0o600
