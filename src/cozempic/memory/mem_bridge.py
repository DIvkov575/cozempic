"""Persist Insights to the mymemories repo and record capture in the ledger.

Standalone stage: `insight -> slug`. Knows nothing about how insights were extracted.
Partition resolution reuses the mymemories-tool symlink convention. No auto-commit.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import ledger
from .insight import Insight

TOOL_DIR = Path(os.path.expanduser("~/workplace/mymemories-tool"))
MEM_HOME = Path(os.environ.get("MEM_HOME", os.path.expanduser("~/workplace/mymemories")))


def _mangled_cwd() -> str:
    """Claude Code mangles the cwd to a dir name by replacing '/' with '-'."""
    return os.getcwd().replace("/", "-")


def _claude_projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
    return Path(base) / "projects"


def resolve_partition() -> Path | None:
    """Return the mymemories partition dir for the cwd, or None if not installed."""
    link = _claude_projects_dir() / _mangled_cwd() / "memory"
    if not link.is_symlink():
        return None
    target = link.resolve()
    try:
        target.relative_to(MEM_HOME.resolve())
    except ValueError:
        return None
    return target if target.is_dir() else None


def _write_fact_file(partition: Path, ins: Insight) -> None:
    fm = (
        "---\n"
        f"name: {ins.slug}\n"
        f"description: {ins.description}\n"
        f"type: {ins.type}\n"
        "---\n\n"
    )
    (partition / f"{ins.slug}.md").write_text(fm + ins.body.rstrip() + "\n", encoding="utf-8")


def _append_index_line(partition: Path, ins: Insight) -> None:
    idx = partition / "MEMORY.md"
    line = f"- [{ins.title}]({ins.slug}.md) — {ins.description}\n"
    prior = idx.read_text(encoding="utf-8") if idx.exists() else "# Memories\n"
    if line not in prior:
        if not prior.endswith("\n"):
            prior += "\n"
        idx.write_text(prior + line, encoding="utf-8")


def _reindex() -> None:
    """Best-effort incremental embedding index update. Never raises."""
    embed = TOOL_DIR / "embed.py"
    if not embed.exists():
        return
    try:
        subprocess.run(["python3", str(embed), "update"], capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        pass


def persist_insights(session_id: str, items: list[tuple[Insight, str]]) -> list[str]:
    """Write each (insight, span_hash); record ledger; reindex once. Returns slugs written.

    No-op (returns []) if the project isn't partitioned into mymemories.
    """
    partition = resolve_partition()
    if partition is None:
        return []
    written: list[str] = []
    for ins, span_h in items:
        _write_fact_file(partition, ins)
        _append_index_line(partition, ins)
        ledger.record(session_id, span_h, ins.slug)
        written.append(ins.slug)
    if written:
        _reindex()
    return written
