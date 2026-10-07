from __future__ import annotations

from pathlib import Path

import pytest

from tensorwatch import discover

EVENT = "events.out.tfevents.1700000000.host.1"


def run(root: Path, *relative: str) -> None:
    for item in relative:
        target = root / item / EVENT
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00")


def found(root: Path, depth: int = 4) -> list[str]:
    return [str(path.relative_to(root)) for path in discover.discover(root, depth)]


def test_a_run_container_is_proposed_once_however_many_runs_it_holds(tmp_path):
    run(tmp_path, *(f"proj/runs/ppo_{i}/seed_1" for i in range(30)))
    assert found(tmp_path) == ["proj/runs"]


def test_a_container_is_found_even_when_its_runs_are_past_the_depth_limit(tmp_path):
    # The container is within reach; what is inside it is not walked, only probed.
    run(tmp_path, "proj/runs/a/b/c/d/e")
    assert found(tmp_path, depth=2) == ["proj/runs"]


def test_an_empty_container_is_not_proposed(tmp_path):
    (tmp_path / "proj" / "runs" / "checkpoints").mkdir(parents=True)
    (tmp_path / "proj" / "runs" / "checkpoints" / "model.pt").write_bytes(b"\x00")
    assert found(tmp_path) == []


def test_sibling_runs_are_grouped_and_a_lone_run_stays_itself(tmp_path):
    run(tmp_path, "proj/ablations/a", "proj/ablations/b", "other/single")
    assert found(tmp_path) == ["other/single", "proj/ablations"]


def test_a_per_run_tensorboard_directory_counts_as_its_run(tmp_path):
    # `exp1/tensorboard/events...`: the runs are exp1 and exp2, grouped as usual.
    run(tmp_path, "proj/sweep/exp1/tensorboard", "proj/sweep/exp2/tensorboard")
    assert found(tmp_path) == ["proj/sweep"]


def test_pruned_and_hidden_directories_are_never_walked(tmp_path):
    run(tmp_path, "proj/node_modules/pkg/runs/a", "proj/.venv/runs/a", "proj/.hidden/runs/a")
    assert found(tmp_path) == []


def test_has_events_stops_at_its_budget(tmp_path):
    run(tmp_path, "a/b/c/d")
    assert discover.has_events(tmp_path)
    # Four directories are opened before the one holding the event file.
    assert not discover.has_events(tmp_path, budget=4)
    assert discover.has_events(tmp_path, budget=5)


def test_a_sibling_full_of_files_does_not_hide_a_run(tmp_path):
    checkpoints = tmp_path / "logs" / "ckpt"
    checkpoints.mkdir(parents=True)
    for index in range(3000):
        (checkpoints / f"step_{index}.pt").touch()
    for index in range(40):
        (checkpoints / f"sub{index}" / "deeper").mkdir(parents=True)
    run(tmp_path, "logs/zz_tb/run1")
    # Files cost nothing and the walk is breadth-first, so the shallow run is
    # reached long before the checkpoint tree is exhausted.
    assert discover.has_events(tmp_path / "logs", budget=discover.COMPLETION_BUDGET)
    assert found(tmp_path) == ["logs"]


def test_count_events_is_capped(tmp_path):
    run(tmp_path, *(f"r{i}" for i in range(12)))
    assert discover.count_events(tmp_path) == 12
    assert discover.count_events(tmp_path, limit=5) == 5
    # One directory opened is the root itself, which holds no event file.
    assert discover.count_events(tmp_path, budget=1) == 0
    assert 0 < discover.count_events(tmp_path, budget=4) < 12


def test_unique_name_qualifies_a_second_logdir_in_the_same_repository(tmp_path):
    (tmp_path / "cleanrl" / ".git").mkdir(parents=True)
    taken: set[str] = set()
    names = []
    for leaf in ("runs", "runs_old", "runs_old"):
        name = discover.unique_name(tmp_path / "cleanrl" / leaf, taken.__contains__)
        taken.add(name)
        names.append(name)
    assert names == ["cleanrl", "cleanrl-runs_old", "cleanrl-runs_old-2"]


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_complete_lists_matching_directories_and_marks_logdirs(fake_home):
    run(fake_home, "work/cleanrl/runs/a")
    (fake_home / "work" / "cleaner").mkdir()
    (fake_home / "work" / "clean.txt").write_text("not a directory")
    (fake_home / "work" / ".cleanup").mkdir()

    assert discover.complete("~/work/cle") == [
        {"path": "~/work/cleaner", "events": False},
        {"path": "~/work/cleanrl", "events": True},
    ]
    # A trailing slash lists what is inside.
    assert discover.complete("~/work/cleanrl/") == [{"path": "~/work/cleanrl/runs", "events": True}]
    # Matching ignores case; an exact match sorts first.
    (fake_home / "work" / "Runs").mkdir()
    (fake_home / "work" / "runs").mkdir()
    assert [item["path"] for item in discover.complete("~/work/runs")] == [
        "~/work/runs", "~/work/Runs",
    ]
    # Hidden directories only once the dot is typed.
    assert [item["path"] for item in discover.complete("~/work/.c")] == ["~/work/.cleanup"]


def test_complete_is_quiet_about_paths_it_cannot_list(fake_home):
    assert discover.complete("relative/path") == []
    assert discover.complete("~/does/not/exist/") == []
    (fake_home / "docs").mkdir()
    assert discover.complete("") == discover.complete("~") == [{"path": "~/docs", "events": False}]


def test_workspaces_are_the_directories_holding_registered_repositories(fake_home):
    for repo in ("ws/a", "ws/b", "ws/nested/c", "elsewhere/d"):
        (fake_home / repo / ".git").mkdir(parents=True)
    logdirs = [fake_home / "ws/a/runs", fake_home / "ws/b/x/runs", fake_home / "ws/nested/c/runs",
               fake_home / "elsewhere/d/runs"]
    # `ws/nested` is inside `ws`, so searching `ws` already covers it.
    assert discover.workspaces(logdirs) == [fake_home / "ws", fake_home / "elsewhere"]


def test_the_home_directory_and_top_level_mounts_are_never_workspaces(fake_home):
    (fake_home / "repo" / ".git").mkdir(parents=True)
    assert discover.workspaces(
        [fake_home / "repo" / "runs", fake_home / "loose-runs", Path("/data/runs"), Path("/runs")]
    ) == []
