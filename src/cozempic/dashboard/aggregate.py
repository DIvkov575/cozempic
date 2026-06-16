"""Receipt aggregation — D2 of the dashboard build path.

Pure over the receipt log: ``load_receipts`` does tolerant I/O (skips unparseable
or structurally-implausible lines — receipts are loss-tolerant by design), and
``aggregate`` is a PURE function (receipts in, views out) so it is trivially
testable and reusable by any surface (the D3 static-HTML renderer, a future TUI).

Honesty rule: only **committed** prunes count toward reclaimed tokens/bytes and the
strategy leaderboard — a deferred/failed prune saved nothing. Deferrals are counted
separately (a high deferral rate is itself a signal worth surfacing).
"""

from __future__ import annotations

import json
from pathlib import Path

from ..receipts import INDEX_FILENAME, receipts_dir

# Minimal shape a line must have to be treated as a receipt (forward-compatible:
# we do NOT hard-validate schema_version, so a newer minor schema still loads).
_MIN_KEYS = ("outcome", "tokens", "bytes", "session", "agent")


def _is_receipt(obj) -> bool:
    return (
        isinstance(obj, dict)
        and all(k in obj for k in _MIN_KEYS)
        and isinstance(obj.get("tokens"), dict)
        and isinstance(obj.get("bytes"), dict)
    )


def load_receipts(base_dir: Path | None = None) -> list[dict]:
    """Load every receipt from ``~/.cozempic/receipts/*.jsonl`` (excluding the
    index). Unparseable / non-receipt lines are skipped, never fatal."""
    directory = receipts_dir(base_dir)
    receipts: list[dict] = []
    if not directory.is_dir():
        return receipts
    for path in sorted(directory.glob("*.jsonl")):
        if path.name == INDEX_FILENAME:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue  # torn/partial line — tolerated
            if _is_receipt(obj):
                receipts.append(obj)
    return receipts


def _int(value) -> int:
    """Coerce a possibly-None / non-int metric to int (unknown or bool -> 0)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _d(obj, key) -> dict:
    """Nested dict or ``{}`` — keeps aggregate tolerant of odd/partial receipts
    whose ``session``/``agent``/``trigger``/``model`` is present but not a dict
    (``_is_receipt`` only guarantees ``tokens``/``bytes`` are dicts)."""
    val = obj.get(key) if isinstance(obj, dict) else None
    return val if isinstance(val, dict) else {}


def _context_pct(receipt: dict):
    """Post-prune context usage %, if both numbers are present."""
    after = _d(receipt, "tokens").get("after")
    window = _d(receipt, "model").get("context_window")
    if isinstance(after, int) and isinstance(window, int) and window:
        return round(after / window * 100, 1)
    return None


def aggregate(receipts: list[dict]) -> dict:
    """Reduce receipts to dashboard views. Pure. Defensive against odd receipts."""
    total = len(receipts)
    committed = [r for r in receipts if r.get("outcome") == "committed"]

    def _count(outcome: str) -> int:
        return sum(1 for r in receipts if r.get("outcome") == outcome)

    deferred = _count("deferred")
    # ISO-8601 'Z'-suffixed UTC sorts lexically (contract invariant); filter non-str.
    timestamps = [t for t in (r.get("ts") for r in receipts) if isinstance(t, str)]

    lifetime = {
        "prunes_total": total,
        "committed": len(committed),
        "deferred": deferred,
        "noop": _count("noop"),
        "failed": _count("failed"),
        "deferral_rate": round(deferred / total, 3) if total else 0.0,
        "tokens_reclaimed": sum(_int(_d(r, "tokens").get("reclaimed")) for r in committed),
        "bytes_reclaimed": sum(_int(_d(r, "bytes").get("reclaimed")) for r in committed),
        "sessions": len({_d(r, "session").get("id_hash") or "unknown" for r in receipts}),
        "first_ts": min(timestamps) if timestamps else None,
        "last_ts": max(timestamps) if timestamps else None,
    }

    # Per-strategy leaderboard (committed only) — which strategies pull weight.
    strat: dict[str, dict] = {}
    for r in committed:
        strategies = r.get("strategies")
        for s in strategies if isinstance(strategies, list) else []:
            if not isinstance(s, dict):
                continue
            row = strat.setdefault(
                s.get("id", "unknown"),
                {"id": s.get("id", "unknown"), "tier": s.get("tier", "unknown"),
                 "tokens_reclaimed": 0, "bytes_reclaimed": 0, "count": 0},
            )
            row["tokens_reclaimed"] += _int(s.get("tokens_reclaimed"))
            row["bytes_reclaimed"] += _int(s.get("bytes_reclaimed"))
            row["count"] += 1
    per_strategy = sorted(strat.values(), key=lambda x: x["tokens_reclaimed"], reverse=True)

    # Per-agent grouping (claude now; codex later, free).
    agents: dict[str, dict] = {}
    for r in receipts:
        name = _d(r, "agent").get("name") or "unknown"
        row = agents.setdefault(name, {"agent": name, "prunes": 0, "committed": 0,
                                       "tokens_reclaimed": 0})
        row["prunes"] += 1
        if r.get("outcome") == "committed":
            row["committed"] += 1
            row["tokens_reclaimed"] += _int(_d(r, "tokens").get("reclaimed"))
    per_agent = sorted(agents.values(), key=lambda x: x["tokens_reclaimed"], reverse=True)

    # Tier distribution (all outcomes).
    by_tier: dict[str, int] = {}
    for r in receipts:
        tier = _d(r, "trigger").get("tier") or "unknown"
        by_tier[tier] = by_tier.get(tier, 0) + 1

    # Per-session timelines (sorted by ts) — context % over time + prune events.
    sessions: dict[str, dict] = {}
    for r in receipts:
        sid = _d(r, "session").get("id_hash") or "unknown"
        is_committed = r.get("outcome") == "committed"
        row = sessions.setdefault(
            sid, {"session": sid, "agent": _d(r, "agent").get("name") or "unknown",
                  "prunes": 0, "tokens_reclaimed": 0, "timeline": []},
        )
        row["prunes"] += 1
        tok = _int(_d(r, "tokens").get("reclaimed"))
        byt = _int(_d(r, "bytes").get("reclaimed"))
        if is_committed:
            row["tokens_reclaimed"] += tok
        row["timeline"].append({
            "ts": r.get("ts"),
            "outcome": r.get("outcome"),
            "tier": _d(r, "trigger").get("tier"),
            # honesty: only committed prunes actually reclaimed anything
            "tokens_reclaimed": tok if is_committed else 0,
            "bytes_reclaimed": byt if is_committed else 0,
            "context_pct_after": _context_pct(r),
        })
    for row in sessions.values():
        row["timeline"].sort(key=lambda e: e.get("ts") or "")
    per_session = sorted(
        sessions.values(),
        key=lambda x: (x["timeline"][-1].get("ts") if x["timeline"] else "") or "",
        reverse=True,
    )

    return {
        "lifetime": lifetime,
        "per_strategy": per_strategy,
        "per_agent": per_agent,
        "by_tier": by_tier,
        "per_session": per_session,
    }
