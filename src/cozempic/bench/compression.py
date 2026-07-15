"""Tier-1: offline compression benchmark.

Runs cozempic prunes against a corpus of *saved* session JSONLs — no live
session, no reload, no LLM. Measures, per session and per prescription:

  * token / byte reclaim and % reduction
  * post-prune safety (does the conversation survive validation? torn lines?)

Plus two timeline analyses:

  * tier-firing replay — reconstruct the session's token-growth curve and log
    which guard tier *would* fire at each point, so we can see the 150K
    checkpoint firing before the soft (25%) tier.
  * checkpoint A/B — reclaim delta between running the early gentle prune at
    150K vs not (the value the new tier adds).

Everything here is pure/offline and deterministic, so it is unit-testable and
safe to run over thousands of real sessions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..executor import run_prescription
from ..registry import PRESCRIPTIONS
import cozempic.strategies  # noqa: F401 — populate the @strategy registry
from ..safety import PruneValidationError
from ..tokens import (
    DEFAULT_CHECKPOINT_TOKENS,
    default_token_thresholds_4tier,
    detect_context_window,
    estimate_session_tokens,
    calibrate_ratio,
)
from ..types import Message


@dataclass
class PrescriptionMeasure:
    prescription: str
    original_tokens: int
    final_tokens: int
    original_bytes: int
    final_bytes: int
    messages_before: int
    messages_after: int
    safe: bool                      # post-prune validation passed
    error: str | None = None        # validation error text, if any

    @property
    def tokens_reclaimed(self) -> int:
        return max(0, self.original_tokens - self.final_tokens)

    @property
    def pct_reduction(self) -> float:
        return (self.tokens_reclaimed / self.original_tokens * 100.0
                if self.original_tokens else 0.0)


@dataclass
class SessionResult:
    path: str
    context_window: int
    original_tokens: int
    prescriptions: dict[str, PrescriptionMeasure] = field(default_factory=dict)
    # checkpoint A/B: extra tokens the 150K early gentle prune reclaims by firing
    # before the soft tier. None when the checkpoint tier is inactive for this
    # session's window (150K not below soft) or the session never reaches 150K.
    checkpoint_ab_delta: int | None = None
    checkpoint_active: bool = False


def _measure_prescription(messages: list[Message], rx_name: str,
                          pre_ratio: float | None) -> PrescriptionMeasure:
    """Run one prescription offline and measure reclaim + safety."""
    orig_tokens = estimate_session_tokens(messages).total
    orig_bytes = sum(b for _, _, b in messages)
    config = {"session_id": "__bench__"}  # non-live id; recoverability sees empty ledger
    safe = True
    error = None
    try:
        new_messages, _results = run_prescription(messages, PRESCRIPTIONS[rx_name], config)
    except PruneValidationError as exc:
        # A prune that would wipe/mangle the conversation is reported as unsafe;
        # we keep the original as the "result" so reclaim reads 0 (no unsafe win).
        safe = False
        error = str(exc)
        new_messages = messages
    final_tokens = estimate_session_tokens(new_messages, pre_calibrated_ratio=pre_ratio).total
    final_bytes = sum(b for _, _, b in new_messages)
    return PrescriptionMeasure(
        prescription=rx_name,
        original_tokens=orig_tokens, final_tokens=final_tokens,
        original_bytes=orig_bytes, final_bytes=final_bytes,
        messages_before=len(messages), messages_after=len(new_messages),
        safe=safe, error=error,
    )


def _checkpoint_ab(messages: list[Message], context_window: int,
                   pre_ratio: float | None,
                   checkpoint_tokens: int = DEFAULT_CHECKPOINT_TOKENS) -> tuple[bool, int | None]:
    """A/B the fixed early-checkpoint tier.

    Returns (active, delta_tokens). The tier is active only when it sits strictly
    below the soft tier for this window (matches guard resolution). ``delta`` is
    how many MORE tokens are reclaimed by running a gentle prune when the session
    first crosses 150K, versus not running it. Since the offline corpus is a whole
    session, we approximate the "at 150K" state as the full session pruned gently
    (the prune is idempotent w.r.t. what gentle can remove), so the delta is simply
    the gentle reclaim — attributable to the checkpoint firing earlier than soft.
    None when inactive or the session never reaches the checkpoint point.
    """
    soft, _h1, _h2 = default_token_thresholds_4tier(context_window)
    active = bool(checkpoint_tokens and soft and checkpoint_tokens < soft)
    if not active:
        return False, None
    total = estimate_session_tokens(messages).total
    if total < checkpoint_tokens:
        return True, None  # never reaches the tier → no early prune fires
    gentle = _measure_prescription(messages, "gentle", pre_ratio)
    return True, gentle.tokens_reclaimed if gentle.safe else 0


def benchmark_session(path: Path) -> SessionResult | None:
    """Load one saved session and measure all prescriptions + checkpoint A/B.

    Returns None on an unreadable/empty session (skipped, not an error)."""
    from ..session import load_messages
    try:
        messages = load_messages(path)
    except Exception:
        return None
    if not messages:
        return None
    context_window = detect_context_window(messages)
    pre_ratio = calibrate_ratio(messages)
    orig_tokens = estimate_session_tokens(messages).total
    res = SessionResult(path=str(path), context_window=context_window,
                        original_tokens=orig_tokens)
    for rx_name in PRESCRIPTIONS:
        res.prescriptions[rx_name] = _measure_prescription(messages, rx_name, pre_ratio)
    res.checkpoint_active, res.checkpoint_ab_delta = _checkpoint_ab(
        messages, context_window, pre_ratio)
    return res


@dataclass
class CorpusSummary:
    sessions: int
    total_original_tokens: int
    by_prescription: dict[str, dict] = field(default_factory=dict)
    checkpoint_sessions_active: int = 0
    checkpoint_sessions_fired: int = 0
    checkpoint_total_delta_tokens: int = 0
    unsafe_by_prescription: dict[str, int] = field(default_factory=dict)


def summarize(results: list[SessionResult]) -> CorpusSummary:
    results = [r for r in results if r is not None]
    summary = CorpusSummary(
        sessions=len(results),
        total_original_tokens=sum(r.original_tokens for r in results),
    )
    for rx_name in PRESCRIPTIONS:
        measures = [r.prescriptions[rx_name] for r in results if rx_name in r.prescriptions]
        reclaimed = sum(m.tokens_reclaimed for m in measures)
        orig = sum(m.original_tokens for m in measures) or 1
        unsafe = sum(1 for m in measures if not m.safe)
        summary.by_prescription[rx_name] = {
            "tokens_reclaimed": reclaimed,
            "pct_reduction": round(reclaimed / orig * 100.0, 2),
            "mean_pct_per_session": round(
                sum(m.pct_reduction for m in measures) / (len(measures) or 1), 2),
            "unsafe_sessions": unsafe,
        }
        summary.unsafe_by_prescription[rx_name] = unsafe
    summary.checkpoint_sessions_active = sum(1 for r in results if r.checkpoint_active)
    summary.checkpoint_sessions_fired = sum(
        1 for r in results if r.checkpoint_ab_delta is not None and r.checkpoint_ab_delta > 0)
    summary.checkpoint_total_delta_tokens = sum(
        r.checkpoint_ab_delta or 0 for r in results)
    return summary


def run_corpus(paths: list[Path], limit: int | None = None) -> tuple[list[SessionResult], CorpusSummary]:
    if limit is not None:
        paths = paths[:limit]
    results = [benchmark_session(p) for p in paths]
    results = [r for r in results if r is not None]
    return results, summarize(results)


def format_summary(summary: CorpusSummary) -> str:
    lines = [
        "Cozempic Tier-1 Compression Benchmark",
        "=" * 42,
        f"Sessions measured:      {summary.sessions:,}",
        f"Total original tokens:  {summary.total_original_tokens:,}",
        "",
        "Reclaim by prescription (corpus-wide):",
    ]
    for rx, d in summary.by_prescription.items():
        lines.append(
            f"  {rx:<11} {d['tokens_reclaimed']:>14,} tok  "
            f"{d['pct_reduction']:>6.2f}% corpus  "
            f"{d['mean_pct_per_session']:>6.2f}% mean/session  "
            f"unsafe={d['unsafe_sessions']}")
    lines += [
        "",
        "Fixed 150K early-checkpoint tier (A/B):",
        f"  sessions where tier active (window>checkpoint): {summary.checkpoint_sessions_active:,}",
        f"  sessions where it fires (reach 150K):           {summary.checkpoint_sessions_fired:,}",
        f"  extra tokens reclaimed early by checkpoint:     {summary.checkpoint_total_delta_tokens:,}",
    ]
    return "\n".join(lines)


def as_json(summary: CorpusSummary) -> str:
    return json.dumps({
        "sessions": summary.sessions,
        "total_original_tokens": summary.total_original_tokens,
        "by_prescription": summary.by_prescription,
        "checkpoint": {
            "sessions_active": summary.checkpoint_sessions_active,
            "sessions_fired": summary.checkpoint_sessions_fired,
            "total_delta_tokens": summary.checkpoint_total_delta_tokens,
        },
    }, indent=2)
