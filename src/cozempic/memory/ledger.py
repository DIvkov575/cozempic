"""Bridge ledger: which message spans have been durably captured as memories.

`{span_hash -> slug}` per session at ~/.cozempic/bridge/<session_id>.json.
This is the capture-confirmation source the recoverability strategy gates on.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

BRIDGE_DIR = Path.home() / ".cozempic" / "bridge"


def span_hash(msgs: list[dict]) -> str:
    """Stable, order-sensitive 16-hex hash of a contiguous message span."""
    canonical = "\n".join(
        json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for m in msgs
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _path(session_id: str) -> Path:
    safe = session_id.replace("/", "_")
    return BRIDGE_DIR / f"{safe}.json"


def _load(session_id: str) -> dict:
    p = _path(session_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def record(session_id: str, span_h: str, slug: str) -> None:
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    data = _load(session_id)
    data[span_h] = slug
    _path(session_id).write_text(json.dumps(data, indent=2), encoding="utf-8")


def record_span(session_id: str, msgs: list[dict], slug: str) -> None:
    """Record a per-message ledger entry for every message in the span.

    Recoverability reads per-message (span_hash([msg])), so the write side must
    record per-message too — a whole-span hash would never match a single-message
    lookup. Each message maps to the same consolidated `slug`.
    """
    for m in msgs:
        record(session_id, span_hash([m]), slug)


def is_captured(session_id: str, span_h: str) -> bool:
    return span_h in _load(session_id)


def slug_for(session_id: str, span_h: str) -> str | None:
    return _load(session_id).get(span_h)


def record_block(session_id: str, block: dict, slug: str) -> None:
    """Record a distilled/offloaded content BLOCK (namespace distinct from message spans)."""
    record(session_id, span_hash([block]), slug)


def is_block_captured(session_id: str, block: dict) -> bool:
    return is_captured(session_id, span_hash([block]))


def slug_for_block(session_id: str, block: dict) -> str | None:
    return slug_for(session_id, span_hash([block]))
