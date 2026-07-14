"""Early, background consolidation. Fires ahead of the prune threshold, off the critical
path, debounced. The hook that calls maybe_consolidate() never blocks on extraction.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from . import ledger
from .distill import distill_thinking
from .extract import extract_insights
from .insight import Insight, TrustClass
from .mem_bridge import persist_insights

BRIDGE_DIR = ledger.BRIDGE_DIR

# Fire consolidation once context reaches this fraction of the window — deliberately
# BELOW the prune threshold so memories are captured before pruning is warranted.
LOW_WATER = 0.30
_DEBOUNCE_S = 300


def _marker(session_id: str) -> Path:
    return BRIDGE_DIR / f"{session_id.replace('/', '_')}.consolidated"


def _recently_fired(session_id: str) -> bool:
    m = _marker(session_id)
    if not m.exists():
        return False
    try:
        return (time.time() - m.stat().st_mtime) < _DEBOUNCE_S
    except OSError:
        return False


def _touch(session_id: str) -> None:
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    _marker(session_id).write_text("", encoding="utf-8")


def _existing_slugs() -> list[str]:
    from .mem_bridge import resolve_partition
    part = resolve_partition()
    if part is None:
        return []
    return [p.stem for p in part.glob("*.md") if p.name != "MEMORY.md"]


def _span_text(msgs: list[dict]) -> str:
    out = []
    for m in msgs:
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
        out.append(f"{m.get('role', '?')}: {c}")
    return "\n".join(out)


def consolidate_worker(session_id: str, span_msgs: list[dict]) -> None:
    """Synchronous work unit: extract → persist. Run directly (worker) or via _spawn."""
    insights = extract_insights(_span_text(span_msgs), _existing_slugs())
    if not insights:
        return
    written = persist_insights(session_id, insights)
    if not written:
        return  # unpartitioned / nothing saved — don't record capture
    # Per-message ledger entries so recoverability's per-message lookups match.
    slug = written[0]
    ledger.record_span(session_id, span_msgs, slug)
    _distill_thinking_blocks(session_id, span_msgs)


def _distill_thinking_blocks(session_id: str, span_msgs: list[dict]) -> None:
    """For each large thinking block in the span, persist its decision-point distillation
    and record a block-hash ledger entry (F7 worker half)."""
    from ..helpers import get_content_blocks
    MIN = 500  # only distill blocks worth the LLM call
    for m in span_msgs:
        for block in get_content_blocks(m):
            if not isinstance(block, dict) or block.get("type") != "thinking":
                continue
            text = block.get("thinking", "") or ""
            if len(text) < MIN:
                continue
            if ledger.is_block_captured(session_id, block):
                continue  # already distilled
            decision = distill_thinking(text)
            if not decision:
                continue
            slug = f"decision-{ledger.span_hash([block])}"
            ins = Insight(
                slug=slug,
                title="decision point",
                description=decision[:120],
                type="reference",
                trust_class=TrustClass.AGENT_PROVISIONAL,
                body=decision,
            )
            written = persist_insights(session_id, [ins])
            if written:
                ledger.record_block(session_id, block, slug)


def _spawn(session_id: str, span_msgs: list[dict]) -> None:
    """Launch a detached worker process; return immediately without blocking."""
    payload = json.dumps({"session_id": session_id, "msgs": span_msgs})
    proc = subprocess.Popen(
        [sys.executable, "-m", "cozempic.memory.schedule", "--worker"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        proc.stdin.write(payload.encode())
        proc.stdin.close()   # deliver EOF so the worker's sys.stdin.read() returns
    except (BrokenPipeError, OSError):
        pass


def maybe_consolidate(session_id: str, span_msgs: list[dict], fraction: float) -> bool:
    """Fire background consolidation if at/above low-water and not debounced.

    Returns True if a worker was spawned. Never blocks on extraction.
    """
    if fraction < LOW_WATER:
        return False
    if _recently_fired(session_id):
        return False
    _touch(session_id)
    _spawn(session_id, span_msgs)
    return True


if __name__ == "__main__":  # detached worker entrypoint
    if "--worker" in sys.argv:
        data = json.loads(sys.stdin.read() or "{}")
        if data:
            consolidate_worker(data["session_id"], data["msgs"])
