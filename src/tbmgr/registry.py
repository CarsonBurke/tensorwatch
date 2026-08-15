"""Comment-preserving edits of the TOML registry.

``tomllib`` reads TOML but cannot write it, and the registry is meant to be
hand-edited, so edits are done as line surgery on ``[[board]]`` blocks: adding a
board appends a block, removing one deletes exactly its lines, and setting a key
rewrites (or inserts) a single line.  Everything the user typed elsewhere -
comments, ordering, blank lines - survives.
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

HEADER = """\
# tbmgr registry - one [[board]] per TensorBoard instance.
# Edit by hand or with `tbmgr add` / `tbmgr rm` / `tbmgr set`.
#
#   [[board]]
#   name = "cleanrl"                 # url path + log file name
#   logdir = "~/repos/cleanrl/runs"  # or logdir_spec = "a:/p/a,b:/p/b"
#   port = 6100                      # stable; the dashboard links here directly
#   autostart = "always"             # always | on_demand | manual
#   reload_interval = 60             # seconds between event-file rescans
#   samples_per_plugin = "scalars=2000,images=0"   # caps memory on huge runs
#   args = ["--load_fast=true"]      # extra tensorboard flags

[server]
port = 6005
port_base = 6100
keep_warm = 2
"""

_TABLE_START = "["


class RegistryError(Exception):
    """Raised when an edit targets something that is not there."""


@dataclass(frozen=True, slots=True)
class Block:
    """A ``[[board]]`` block located in the registry text."""

    name: str
    data: Mapping[str, Any]
    start: int  # index of the [[board]] header line
    end: int  # exclusive, trailing blank lines excluded


def _ends_block(line: str) -> bool:
    """True for a table header that starts something other than this board.

    ``[board.env]`` is a sub-table of the *current* ``[[board]]`` entry, so it
    belongs to the block: cutting the block short there would leave the sub-table
    behind, where TOML silently re-binds it to the previous board.
    """
    stripped = line.lstrip()
    if not stripped.startswith(_TABLE_START):
        return False
    return not re.match(r"\[\s*board\s*\.", stripped)


def _is_board_header(line: str) -> bool:
    stripped = line.strip().replace(" ", "")
    return stripped.startswith("[[board]]")


def blocks(text: str) -> list[Block]:
    """Locate every ``[[board]]`` block, in file order."""
    lines = text.splitlines(keepends=True)
    found: list[Block] = []
    for index, line in enumerate(lines):
        if not _is_board_header(line):
            continue
        end = len(lines)
        for probe in range(index + 1, len(lines)):
            if _ends_block(lines[probe]):
                end = probe
                break
        while end > index + 1 and not lines[end - 1].strip():
            end -= 1
        chunk = "".join(lines[index:end])
        try:
            data = tomllib.loads(chunk)["board"][0]
        except Exception as exc:  # malformed block: surface it with context
            raise RegistryError(f"cannot parse board block at line {index + 1}: {exc}") from exc
        found.append(Block(name=_name_of(data), data=data, start=index, end=end))
    return found


def _name_of(data: Mapping[str, Any]) -> str:
    name = data.get("name")
    if isinstance(name, str) and name:
        return name
    logdir = data.get("logdir")
    if isinstance(logdir, str) and logdir:
        return Path(os.path.expandvars(logdir)).expanduser().name
    return ""


def _find(text: str, name: str) -> Block:
    for block in blocks(text):
        if block.name == name:
            return block
    known = ", ".join(b.name for b in blocks(text)) or "<none>"
    raise RegistryError(f"no board named {name!r} (registered: {known})")


def render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, Path):
        return _quote(str(value))
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, Mapping):
        inner = ", ".join(f"{k} = {render_value(v)}" for k, v in value.items())
        return "{ " + inner + " }" if inner else "{}"
    if isinstance(value, Sequence):
        return "[" + ", ".join(render_value(item) for item in value) + "]"
    raise TypeError(f"cannot render {type(value).__name__} into TOML")


def _quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


#: Stable key order so generated blocks read the same way every time.
_KEY_ORDER = (
    "name", "logdir", "logdir_spec", "port", "enabled", "autostart", "description",
    "command", "args", "env", "cwd", "host", "reload_interval",
    "samples_per_plugin", "idle_timeout", "start_timeout",
)


def render_block(entry: Mapping[str, Any]) -> str:
    keys = [k for k in _KEY_ORDER if k in entry]
    keys += [k for k in entry if k not in _KEY_ORDER]
    body = "".join(f"{key} = {render_value(entry[key])}\n" for key in keys)
    return "[[board]]\n" + body


def add(text: str, entry: Mapping[str, Any]) -> str:
    """Append a board block; the name must be free."""
    name = _name_of(entry)
    if not name:
        raise RegistryError("entry needs a name")
    if any(block.name == name for block in blocks(text)):
        raise RegistryError(f"board {name!r} already registered")
    prefix = text if text.endswith("\n") or not text else text + "\n"
    separator = "" if prefix.endswith("\n\n") or not prefix else "\n"
    return prefix + separator + render_block(entry)


def remove(text: str, name: str) -> str:
    block = _find(text, name)
    lines = text.splitlines(keepends=True)
    end = block.end
    # Swallow the blank separator that followed the block, if any.
    if end < len(lines) and not lines[end].strip():
        end += 1
    return "".join(lines[: block.start] + lines[end:])


def _key_lines(lines: list[str], block: Block) -> dict[str, int]:
    """Map each top-level key of the block to its line index.

    Continuation lines of a multi-line value (``args = [`` ... ``]``) are skipped:
    a bare ``"port = 1"`` array element must never be mistaken for the key.
    """
    found: dict[str, int] = {}
    depth = 0
    for index in range(block.start + 1, block.end):
        line = lines[index]
        if depth == 0 and not line.lstrip().startswith(("#", "[")) and "=" in line:
            key = line.split("=", 1)[0].strip().strip('"')
            found.setdefault(key, index)
        depth += sum(line.count(open_) - line.count(close) for open_, close in (("[", "]"), ("{", "}")))
        depth = max(0, depth)
    return found


def set_key(text: str, name: str, key: str, value: Any) -> str:
    """Set (or insert) ``key`` inside board ``name``."""
    block = _find(text, name)
    lines = text.splitlines(keepends=True)
    rendered = f"{key} = {render_value(value)}\n"
    index = _key_lines(lines, block).get(key)
    if index is None:
        lines.insert(block.start + 1, rendered)
    else:
        lines[index] = rendered
    return "".join(lines)


def unset_key(text: str, name: str, key: str) -> str:
    block = _find(text, name)
    lines = text.splitlines(keepends=True)
    index = _key_lines(lines, block).get(key)
    if index is not None:
        del lines[index]
    return "".join(lines)


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` atomically, following symlinks and keeping mode 0600.

    Following the link matters: a registry symlinked into a dotfiles repo must
    stay a symlink instead of being silently replaced by a regular file.
    """
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, prefix=target.name + ".", suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(handle.name, 0o600)
        os.replace(handle.name, target)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def ensure(path: Path) -> str:
    """Create the registry with a documented header if it does not exist."""
    if not path.exists():
        write_atomic(path, HEADER)
    return read(path)


def edit(path: Path, mutate) -> str:
    """Read-modify-write the registry through ``mutate(text) -> text``."""
    text = ensure(path)
    updated = mutate(text)
    if updated != text:
        try:
            tomllib.loads(updated)  # never persist a registry we cannot read back
        except tomllib.TOMLDecodeError as exc:
            raise RegistryError(f"refusing to write unparseable registry: {exc}") from exc
        write_atomic(path, updated)
    return updated


def set_ports(path: Path, ports: Mapping[str, int]) -> None:
    """Persist auto-assigned ports so they never move between restarts."""
    if not ports:
        return

    def mutate(text: str) -> str:
        for name, port in ports.items():
            text = set_key(text, name, "port", port)
        return text

    edit(path, mutate)


def names(path: Path) -> Iterable[str]:
    return [block.name for block in blocks(read(path))]
