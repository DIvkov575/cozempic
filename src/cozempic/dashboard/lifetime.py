"""Lifetime savings ledger — surfaces cozempic's running totals on the dashboard.

The dashboard's per-prune views (D2) only see receipts, which are new. cozempic
has ALSO kept a lifetime counter at ``~/.cozempic_savings.json`` since long before
receipts existed (see helpers.record_savings). This loader reads that ledger so the
dashboard can lead with the user's TRUE totals (e.g. 456M tokens across 3,309
prunes) instead of just the handful of receipts recorded so far.

Pure-ish: ``load_lifetime`` does tolerant I/O and returns a normalized dict (or
None). It never raises.
"""

from __future__ import annotations

import json
from pathlib import Path

from .._constants import _MAX_RECEIPT_INT


def lifetime_path() -> Path:
    """Where helpers.record_savings writes the lifetime ledger."""
    return Path.home() / ".cozempic_savings.json"


def _num_or_zero(value) -> int:
    """Coerce a numeric ledger field to int (bool/garbage/huge/negative -> 0;
    floats truncated).

    The `int(value)` call never raises for a Python arbitrary-precision int,
    so the `except (ValueError, OverflowError)` branch handles only
    float('nan') -> ValueError and float('inf') -> OverflowError.

    Out-of-bound values (> _MAX_RECEIPT_INT or < 0) return 0, mirroring
    aggregate._int for sibling parity.  Returning 10**15 for a huge int
    would fabricate a "1 billion M tokens" headline in the Lifetime band;
    returning 0 causes the early-out in load_lifetime to suppress the band
    entirely, which is the correct response to corrupt data.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        try:
            result = int(value)
        except (ValueError, OverflowError):
            return 0
        if result < 0 or result > _MAX_RECEIPT_INT:
            return 0
        return result
    return 0


def load_lifetime(path: Path | None = None) -> dict | None:
    """Read + normalize the lifetime ledger. Returns None if absent/empty/garbage.

    The returned dict has: tokens_saved, tokens_processed, prune_count,
    turns_gained, since (str|None), savings_rate_pct (float|None).
    """
    p = path if path is not None else lifetime_path()
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    saved = _num_or_zero(data.get("tokens_saved"))
    if saved <= 0:
        return None  # nothing meaningful to show
    processed = _num_or_zero(data.get("tokens_processed"))
    rate = round(saved / processed * 100, 1) if processed > 0 else None
    if rate is not None and not (0 <= rate <= 100):
        rate = None  # processed < saved => corrupt/untrustworthy ledger; suppress
    prune_count = _num_or_zero(data.get("prune_count"))
    sessions = _num_or_zero(data.get("sessions"))
    # Measured per-pruned-session extension = 1 + avg_prunes_per_session * reclaim.
    # MUST use tracked_prunes (forward-only), NOT the lifetime prune_count — the
    # latter holds pre-tracking prunes and would divide by a tiny new session count
    # (e.g. 3,309/5 -> absurd 117x). Both operands here cover the same window.
    tracked_prunes = _num_or_zero(data.get("tracked_prunes"))
    multiplier = None
    if sessions >= 5 and rate is not None and tracked_prunes > 0:
        multiplier = round(1 + (tracked_prunes / sessions) * (rate / 100), 2)
    since = data.get("since")
    return {
        "tokens_saved": saved,
        "tokens_processed": processed,
        "prune_count": prune_count,
        "turns_gained": _num_or_zero(data.get("turns_gained")),
        "sessions": sessions,
        "session_multiplier_x": multiplier,
        "since": since if isinstance(since, str) else None,
        "savings_rate_pct": rate,
    }
