"""Receipt persistence — D1 of the dashboard build path.

Writes ``PruneReceipt`` records (see :mod:`cozempic.metrics`) to a LOCAL,
append-only log the dashboard reads. Never network — this is local provenance,
distinct from any telemetry, and honors a dedicated opt-out.

Hard invariant: **a receipt must never break or defer a prune.** Every public
function here is exception-isolated and returns ``None`` on any failure rather
than propagating — losing a receipt is acceptable; corrupting a prune is not.

Layout under ``~/.cozempic/receipts/``:
  * ``<session_id_hash>.jsonl`` — full receipts for one session, one per line.
  * ``index.jsonl``            — compact per-prune summaries for fast dashboard load.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .metrics import (
    ProtectedInfo,
    TriggerInfo,
    ValidationInfo,
    build_receipt,
    serialize_receipt,
)

RECEIPTS_DIRNAME = "receipts"
INDEX_FILENAME = "index.jsonl"
_OPT_OUT_ENV = "COZEMPIC_NO_RECEIPTS"


def receipts_dir(base_dir: Path | None = None) -> Path:
    """Directory receipts live in (``~/.cozempic/receipts`` by default)."""
    base = base_dir if base_dir is not None else (Path.home() / ".cozempic")
    return Path(base) / RECEIPTS_DIRNAME


def receipts_enabled() -> bool:
    """False if the user opted out via ``COZEMPIC_NO_RECEIPTS``."""
    return not os.environ.get(_OPT_OUT_ENV)


def _tool_version() -> str:
    """Best-effort cozempic version for receipt provenance."""
    try:
        from . import __version__

        return __version__
    except Exception:
        return ""


def _session_stem(receipt: dict) -> str:
    """Filesystem-safe per-session filename stem from the HASHED id (never raw).

    Defensive even if id_hash is malformed: strips path separators and leading
    dots so the stem can never escape the receipts dir or create a dotfile.
    """
    sid = (receipt.get("session") or {}).get("id_hash") or "unknown"
    stem = sid.replace("sha256:", "")
    for ch in ("/", "\\", os.sep, os.altsep or ""):
        if ch:
            stem = stem.replace(ch, "_")
    stem = stem.lstrip(".")
    return stem[:32] or "unknown"


def _append_line(path: Path, line: str) -> None:
    """Append one line via a single ``os.write`` to an ``O_APPEND`` fd.

    A single write to an O_APPEND descriptor is atomic for payloads under
    PIPE_BUF (>=512 bytes — index summaries and most receipts qualify), so
    concurrent appenders — INCLUDING different sessions racing the shared
    ``index.jsonl`` (which no per-session ``_PruneLock`` protects) — cannot
    interleave or tear those lines. Oversized receipt lines may still split
    under contention; that is loss-tolerant by design — the D2 aggregator skips
    any unparseable line and the prune itself is never affected.
    """
    data = (line + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def write_receipt(receipt: dict, *, base_dir: Path | None = None) -> Path | None:
    """Persist a receipt dict to the session log + index. Returns the session
    log path, or ``None`` if disabled or on any failure (never raises)."""
    if not receipts_enabled():
        return None
    try:
        directory = receipts_dir(base_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_session_stem(receipt)}.jsonl"
        _append_line(path, serialize_receipt(receipt))
        _append_index(directory, receipt)
        return path
    except Exception:
        return None


def _append_index(directory: Path, receipt: dict) -> None:
    """Append a compact summary line for fast dashboard aggregation."""
    try:
        summary = {
            "ts": receipt["ts"],
            "receipt_id": receipt["receipt_id"],
            "agent": receipt["agent"]["name"],
            "session": receipt["session"]["id_hash"],
            "outcome": receipt["outcome"],
            "tier": receipt["trigger"]["tier"],
            "tokens_reclaimed": receipt["tokens"]["reclaimed"],
            "bytes_reclaimed": receipt["bytes"]["reclaimed"],
        }
        _append_line(directory / INDEX_FILENAME, json.dumps(summary, separators=(",", ":")))
    except Exception:
        pass  # index is an optimization; its loss must not fail the receipt


def emit_receipt(
    result,
    *,
    adapter,
    session_id: str | None,
    trigger: TriggerInfo,
    outcome: str = "committed",
    mode: str = "edit_resume",
    validation: ValidationInfo | None = None,
    protected: ProtectedInfo | None = None,
    strategy_tiers: dict[str, str] | None = None,
    transcript_path: str | None = None,
    cwd: str | None = None,
    timing_ms: dict | None = None,
    tool_version: str | None = None,
    base_dir: Path | None = None,
) -> Path | None:
    """Build + persist a receipt for a completed prune. The one call sites use.

    Fully exception-isolated: returns the receipt path, or ``None`` if receipts
    are disabled or anything goes wrong. A caller can fire-and-forget this on
    every prune outcome (committed/deferred/noop) without a try/except.
    """
    if not receipts_enabled():
        return None
    try:
        receipt = build_receipt(
            result,
            adapter=adapter,
            session_id=session_id,
            transcript_path=transcript_path,
            cwd=cwd,
            trigger=trigger,
            mode=mode,
            outcome=outcome,
            validation=validation,
            protected=protected,
            strategy_tiers=strategy_tiers,
            timing_ms=timing_ms,
            tool_version=tool_version if tool_version is not None else _tool_version(),
        )
        return write_receipt(receipt, base_dir=base_dir)
    except Exception:
        return None
