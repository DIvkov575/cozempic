"""Persist Insights to the mymemories repo via the `mymem` CLI.

Standalone stage: `insight -> slug`. Knows nothing about how insights were
extracted. Writes go through `mymem save`, the tool's supported, deterministic
(no-LLM) write path — it resolves the partition, writes the atomic fact file in
the current format, regenerates MEMORY.md, and commits/pushes. We do NOT hand-roll
the fact file or index anymore (that drifted from the tool). Recording span-capture
in the ledger is a separate concern (see ledger.record_span).

Reads (e.g. thinking_distill loading a distilled decision back by slug) still use
`resolve_partition()` — the same symlink convention the tool exposes per project.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .insight import Insight

TOOL_DIR = Path(os.path.expanduser("~/workplace/mymemories-tool"))
MEM_HOME = Path(os.environ.get("MEM_HOME", os.path.expanduser("~/workplace/mymemories")))

# How long to allow a single `mymem save` (includes a git pull/commit/push).
_SAVE_TIMEOUT = 120


def _mangled_cwd() -> str:
    """Claude Code mangles the cwd to a dir name by replacing '/' with '-'."""
    return os.getcwd().replace("/", "-")


def _claude_projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
    return Path(base) / "projects"


def resolve_partition() -> Path | None:
    """Return the mymemories partition DIR for the cwd, or None if not linked.

    The tool exposes a partition to a harness as a ``memory`` symlink under the
    Claude projects dir (``mymem link``). We follow it and confirm it lives inside
    MEM_HOME. Used by readers (thinking_distill) and to derive the partition name
    for ``mymem save``.
    """
    link = _claude_projects_dir() / _mangled_cwd() / "memory"
    if not link.is_symlink():
        return None
    target = link.resolve()
    try:
        target.relative_to(MEM_HOME.resolve())
    except ValueError:
        return None
    return target if target.is_dir() else None


def _partition_name() -> str | None:
    """The partition's NAME (its dir name under MEM_HOME), for ``mymem save --partition``."""
    part = resolve_partition()
    return part.name if part is not None else None


def _mymem_cmd() -> list[str] | None:
    """Command prefix to invoke the mymem CLI, or None if the tool isn't present."""
    mymem = TOOL_DIR / "mymem"
    if not mymem.exists():
        return None
    # Invoke via python3 so we don't depend on the exec bit / PATH.
    return ["python3", str(mymem)]


def _save_one(insight: Insight, partition: str, session_id: str) -> bool:
    """Persist one insight via `mymem save`. Returns True on success. Never raises."""
    base = _mymem_cmd()
    if base is None:
        return False
    cmd = base + [
        "save", insight.slug,
        "--partition", partition,
        "--type", insight.type,
        "--description", insight.description,
        "--content", insight.body.rstrip() + "\n",
        "--evidence", session_id or "cozempic",
        "--origin", "cozempic",
        "--no-push",  # cozempic runs on the prune hot path; don't block on a network push.
    ]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=_SAVE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0


def persist_insights(session_id: str, insights: list[Insight]) -> list[str]:
    """Persist each insight through `mymem save`; return the slugs written.

    No-op (returns []) if the project isn't linked into mymemories or the tool is
    absent. Recording span-capture in the ledger is intentionally NOT done here
    (different cardinality: insights != messages) — callers use ledger.record_span.
    """
    partition = _partition_name()
    if partition is None:
        return []
    written: list[str] = []
    for ins in insights:
        if _save_one(ins, partition, session_id):
            written.append(ins.slug)
    return written
