"""Prune-metrics contract (agent-agnostic) — D0 of the dashboard build path.

One ``PruneReceipt`` is emitted per prune from the codec/executor seam. The
schema is shared across agents (Claude, Codex, future); only the *measurement*
(token counting, byte sizing) is per-adapter, supplied through ``MetricsAdapter``.

Design:
  * ``build_receipt`` is a PURE rollup of a ``PrescriptionResult`` into the
    canonical receipt dict — no I/O, no clock, no randomness (caller stamps
    ``ts``/``receipt_id``) so it is trivially testable.
  * The receipt is a plain ``dict`` (a serialized JSON document), not a
    dataclass, to avoid shadowing builtins (``bytes``) and because the D2
    aggregator + dashboard consume JSON, not Python objects.
  * Privacy: no content, no raw paths — session id / transcript path / cwd are
    hashed. Local-only persistence (writer lands in D1); honors the opt-out.

This module is dependency-light on purpose: it imports token/byte helpers but
NOT the strategy registry, so the codec layer can emit receipts without cycles.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, Sequence, runtime_checkable

from .helpers import msg_bytes
from .tokens import detect_context_window, estimate_session_tokens
from .types import Message, PrescriptionResult

SCHEMA_VERSION = "1.0"

# Token methods the codebase actually emits: "exact" (usage-derived, highest
# confidence — see tokens.estimate_session_tokens) and "heuristic". "usage" is
# accepted as an alias other adapters may produce. A PrescriptionResult carries
# a method but not a confidence (the result type predates this contract), so we
# derive confidence here. NOTE: the live value is "exact", NOT "usage".
_TOKEN_METHODS = ("exact", "usage", "heuristic", "unknown")
_METHOD_CONFIDENCE = {"exact": "high", "usage": "high", "heuristic": "medium"}

# Free-text fields (trigger.reason, validation.defer_reason) are capped and must
# not carry paths/content/ids — callers should pass short codes. See build_receipt.
_MAX_REASON_LEN = 200


# --------------------------------------------------------------------------- #
# Inputs the caller supplies (small typed records; the receipt itself is dict) #
# --------------------------------------------------------------------------- #
@dataclass
class TokenCount:
    """Result of an adapter counting tokens for a set of entries."""

    total: int
    method: str  # "exact" | "heuristic" | "unknown" (adapters may emit "usage")
    confidence: str  # "high" | "medium" | "low" | "none"


@dataclass
class TriggerInfo:
    """Why this prune ran."""

    source: str  # "manual" | "guard" | "hook" | "precompact" | "overflow"
    tier: str  # overall prescription tier: "gentle"|"standard"|"aggressive"|"custom"
    prescription: str
    reason: str = ""


@dataclass
class ValidationInfo:
    """Post-prune validation outcome."""

    passed: bool = True
    deferred: bool = False
    defer_reason: str | None = None
    checks_run: list[str] = field(default_factory=list)


@dataclass
class ProtectedInfo:
    """Transparency: what the prune refused to touch, by reason."""

    entries: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# The measurement seam — each agent implements this. Codex drops in at D5.     #
# --------------------------------------------------------------------------- #
@runtime_checkable
class MetricsAdapter(Protocol):
    """Per-adapter measurement. The ONLY agent-specific surface in metrics.

    ``name`` is the agent identity stamped on every receipt (``"claude"``,
    ``"codex"``). ``agent_version`` is the agent CLI's version when knowable
    (Codex: ``0.139.0``; Claude: often unknown → ``None``).

    ``entries`` is intentionally agent-neutral (``Sequence[Any]``): Claude passes
    its ``Message`` tuples, a Codex adapter will pass rollout lines. Any string an
    adapter surfaces (``name``, ``agent_version``, and any ``model.name`` it feeds
    the result) MUST be a bounded identifier — never a path, endpoint, or
    user-supplied text — since those land unhashed in the receipt.
    """

    name: str
    schema_version: str

    def agent_version(self) -> str | None: ...
    def count_tokens(self, entries: Sequence[Any]) -> TokenCount: ...
    def context_window(self, entries: Sequence[Any]) -> int: ...
    def entry_bytes(self, entry: Any) -> int: ...


class ClaudeMetricsAdapter:
    """Claude measurement seam — wraps existing tokens.py / helpers.py."""

    name = "claude"
    # The adapter's own contract version, stamped as receipt.agent.adapter_schema_version.
    # Independent of the receipt SCHEMA_VERSION ("1.0") — bump only when THIS
    # adapter's emitted fields change.
    schema_version = "1"

    def agent_version(self) -> str | None:
        # Claude Code's own version is not reliably visible to cozempic.
        return None

    def count_tokens(self, entries: list[Message]) -> TokenCount:
        est = estimate_session_tokens(entries)
        return TokenCount(total=est.total, method=est.method, confidence=est.confidence)

    def context_window(self, entries: list[Message]) -> int:
        return detect_context_window(entries)

    def entry_bytes(self, entry: dict) -> int:
        return msg_bytes(entry)


class CodexMetricsAdapter:
    """Codex measurement seam (D5) — proves the contract is agent-agnostic.

    Entries are Codex rollout lines (``{timestamp, type, payload}``), NOT Claude
    Message tuples. Token counting prefers Codex's own ``token_count`` telemetry
    and falls back to a byte heuristic — no Claude/``tokens.py`` dependency. The
    full Codex codec/locator/guard is the separate 1.10.0 effort; this is only
    the metrics seam, so a Codex prune emits the SAME PruneReceipt the dashboard
    already renders.
    """

    name = "codex"
    schema_version = "1"
    # gpt-5.x family default; the 1.10.0 codec will detect the real window.
    _DEFAULT_WINDOW = 272_000

    def __init__(self, agent_version: str | None = None):
        self._version = agent_version

    def agent_version(self) -> str | None:
        return self._version

    @staticmethod
    def _token_count_payloads(entries):
        for e in entries:
            payload = e.get("payload") if isinstance(e, dict) else None
            if isinstance(payload, dict) and payload.get("type") == "token_count":
                yield payload

    def count_tokens(self, entries) -> TokenCount:
        # Real Codex (0.139): the cumulative total is nested at
        # payload.info.total_token_usage.total_tokens. Fall back to a flat
        # total/total_tokens (older/synthetic) then to a byte heuristic.
        total = None
        for payload in self._token_count_payloads(entries):
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            usage = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
            t = usage.get("total_tokens")
            if not isinstance(t, int) or isinstance(t, bool):
                t = payload.get("total", payload.get("total_tokens"))
            if isinstance(t, int) and not isinstance(t, bool):
                total = t  # last token_count event wins
        if total is not None:
            return TokenCount(total, "exact", "high")
        approx = sum(self.entry_bytes(e) for e in entries) // 4
        return TokenCount(approx, "heuristic", "medium")

    def context_window(self, entries) -> int:
        # Prefer the real window Codex records (info.model_context_window).
        window = None
        for payload in self._token_count_payloads(entries):
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            w = info.get("model_context_window")
            if isinstance(w, int) and not isinstance(w, bool) and w > 0:
                window = w
        return window if window is not None else self._DEFAULT_WINDOW

    def entry_bytes(self, entry) -> int:
        try:
            return len(json.dumps(entry, separators=(",", ":")).encode("utf-8"))
        except Exception:
            return 0


# --------------------------------------------------------------------------- #
# Helpers callers use to stamp non-deterministic fields outside build_receipt  #
# --------------------------------------------------------------------------- #
def new_receipt_id() -> str:
    """Fresh receipt id (caller-side so build_receipt stays deterministic)."""
    return uuid.uuid4().hex


def utc_now_iso() -> str:
    """UTC timestamp, seconds precision, ``Z`` suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def hash_id(value: str | None) -> str | None:
    """Privacy-safe, stable, truncated hash. ``None`` passes through.

    Never store raw session ids, transcript paths, or cwds — only ``sha256:``
    prefixed 12-hex-char digests, enough to correlate a session across receipts
    without revealing the underlying value.

    This is de-identification for correlation, NOT a confidentiality guarantee:
    the digest is unsalted, so anyone holding both a receipt and a candidate
    value can confirm a match. 48 bits is ample to avoid accidental collisions
    across one machine's sessions; it is not a security boundary.
    """
    if value is None:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"


# --------------------------------------------------------------------------- #
# The rollup: PrescriptionResult -> canonical receipt dict (PURE)             #
# --------------------------------------------------------------------------- #
def build_receipt(
    result: PrescriptionResult,
    *,
    adapter: MetricsAdapter,
    session_id: str | None,
    transcript_path: str | None = None,
    cwd: str | None = None,
    trigger: TriggerInfo,
    mode: str = "edit_resume",
    outcome: str = "committed",
    validation: ValidationInfo | None = None,
    protected: ProtectedInfo | None = None,
    strategy_tiers: dict[str, str] | None = None,
    timing_ms: dict | None = None,
    ts: str | None = None,
    receipt_id: str | None = None,
    tool_version: str = "",
) -> dict:
    """Roll a ``PrescriptionResult`` up into a v1.0 ``PruneReceipt`` dict.

    Pure: no I/O, clock, or randomness. ``ts``/``receipt_id`` default to stamped
    values only when omitted, so tests can pin them. Per-strategy token reclaim
    is not tracked by ``PrescriptionResult``; it is apportioned by each
    strategy's share of reclaimed bytes (documented estimate, drives the
    dashboard leaderboard).
    """
    validation = validation or ValidationInfo()
    protected = protected or ProtectedInfo()
    strategy_tiers = strategy_tiers or {}

    # Cap free-text at the contract boundary (defense-in-depth vs a caller that
    # leaks a path/content into reason/defer_reason). Callers should pass codes.
    reason = (trigger.reason or "")[:_MAX_REASON_LEN]
    defer_reason = validation.defer_reason
    if defer_reason is not None:
        defer_reason = defer_reason[:_MAX_REASON_LEN]

    tokens_before = result.original_tokens
    tokens_after = result.final_tokens
    tokens_reclaimed = (
        tokens_before - tokens_after
        if tokens_before is not None and tokens_after is not None
        else None
    )
    # Guard against OverflowError (huge tokens_before from corrupt data) and
    # ZeroDivisionError (tokens_before=0 edge case not caught by truthiness).
    # math.isfinite guard handles any NaN/inf that could slip through.
    reclaimed_pct = 0.0
    if tokens_reclaimed is not None and tokens_before:
        try:
            candidate = round(tokens_reclaimed / tokens_before * 100, 1)
            reclaimed_pct = candidate if math.isfinite(candidate) else 0.0
        except (OverflowError, ZeroDivisionError):
            reclaimed_pct = 0.0
    method = result.token_method or "unknown"

    bytes_before = result.original_total_bytes
    bytes_after = result.final_total_bytes
    bytes_reclaimed = bytes_before - bytes_after

    # Apportion total token reclaim across strategies by byte share, using
    # largest-remainder (Hamilton) so the per-strategy values sum EXACTLY to
    # tokens_reclaimed (plain rounding drifts and loses/gains tokens). This is an
    # estimate — PrescriptionResult carries no per-strategy token counts. Skipped
    # for non-positive reclaim (e.g. a session that grew): per-strategy stays 0.
    strat_bytes = [max(sr.original_bytes - sr.pruned_bytes, 0) for sr in result.strategy_results]
    total_strategy_bytes = sum(strat_bytes)
    sr_token_alloc = [0] * len(strat_bytes)
    if tokens_reclaimed and tokens_reclaimed > 0 and total_strategy_bytes:
        # Integer arithmetic only — Python ints are arbitrary-precision so
        # tokens_reclaimed * b never overflows, even for huge tokens_reclaimed
        # (e.g. 10**400). The old float path raised OverflowError on huge values,
        # zeroing ALL strategy attributions. Total_strategy_bytes is always a
        # non-negative int (sum of non-negative ints above), so no ValueError.
        #
        # Hamilton (largest-remainder) in int:
        #   floor_share[i]   = tokens_reclaimed * b[i] // total_strategy_bytes
        #   fractional[i]    = tokens_reclaimed * b[i] %  total_strategy_bytes
        # Distribute leftover (tokens_reclaimed - sum(floors)) to strategies with
        # the largest fractional parts so the per-strategy values sum EXACTLY to
        # tokens_reclaimed.
        # divmod computes floor and remainder in one multiplication per strategy.
        pairs = [divmod(tokens_reclaimed * b, total_strategy_bytes) for b in strat_bytes]
        floors = [q for q, _ in pairs]
        remainders = [r for _, r in pairs]
        leftover = tokens_reclaimed - sum(floors)  # non-negative by construction
        order = sorted(range(len(strat_bytes)), key=lambda i: remainders[i], reverse=True)
        for k in range(leftover):
            floors[order[k]] += 1
        sr_token_alloc = floors

    strategies = [
        {
            "id": sr.strategy_name,
            "tier": strategy_tiers.get(sr.strategy_name, "unknown"),
            "tokens_reclaimed": sr_token_alloc[i],
            "bytes_reclaimed": strat_bytes[i],
            "entries_affected": sr.messages_affected,
        }
        for i, sr in enumerate(result.strategy_results)
    ]

    entries_removed = sum(sr.messages_removed for sr in result.strategy_results)
    entries_replaced = sum(sr.messages_replaced for sr in result.strategy_results)

    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_id": receipt_id if receipt_id is not None else new_receipt_id(),
        "ts": ts if ts is not None else utc_now_iso(),
        "tool": {"name": "cozempic", "version": tool_version},
        "agent": {
            "name": adapter.name,
            "version": adapter.agent_version(),
            "adapter_schema_version": adapter.schema_version,
        },
        "session": {
            "id_hash": hash_id(session_id),
            "transcript_hash": hash_id(transcript_path),
            "cwd_hash": hash_id(cwd),
        },
        "trigger": {
            "source": trigger.source,
            "tier": trigger.tier,
            "prescription": trigger.prescription,
            "reason": reason,
        },
        "mode": mode,
        "model": {"name": result.model, "context_window": result.context_window},
        "entries": {
            "before": result.original_message_count,
            "after": result.final_message_count,
            "removed": entries_removed,
            "replaced": entries_replaced,
        },
        "bytes": {
            "before": bytes_before,
            "after": bytes_after,
            "reclaimed": bytes_reclaimed,
        },
        "tokens": {
            "before": tokens_before,
            "after": tokens_after,
            "reclaimed": tokens_reclaimed,
            "reclaimed_pct": reclaimed_pct,
            "method": method,
            "confidence": _METHOD_CONFIDENCE.get(method, "none"),
        },
        "strategies": strategies,
        "protected": {"entries": protected.entries, "reasons": dict(protected.reasons)},
        "validation": {
            "passed": validation.passed,
            "deferred": validation.deferred,
            "defer_reason": defer_reason,
            "checks_run": list(validation.checks_run),
        },
        "outcome": outcome,
        "timing_ms": dict(timing_ms) if timing_ms else {},
    }


# --------------------------------------------------------------------------- #
# Serialization + structural validation (shared with the D2 aggregator)       #
# --------------------------------------------------------------------------- #
_REQUIRED_TOP_KEYS = (
    "schema_version", "receipt_id", "ts", "tool", "agent", "session", "trigger",
    "mode", "model", "entries", "bytes", "tokens", "strategies", "protected",
    "validation", "outcome", "timing_ms",
)


def serialize_receipt(receipt: dict) -> str:
    """One compact JSON line (JSONL), trailing newline excluded.

    ``allow_nan=False`` raises ``ValueError`` on NaN/inf rather than writing
    the non-standard Python literals ``NaN`` / ``Infinity`` that are invalid
    JSON for any non-Python consumer.  ``validate_receipt`` is a standalone
    contract validator (used by tests / external callers); this is the
    production output-boundary guard.
    """
    return json.dumps(receipt, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def validate_receipt(receipt: dict) -> None:
    """Assert a dict is a structurally valid v1.0 receipt. Raises ValueError.

    A cheap contract guard used by tests and by the aggregator before trusting
    a receipt line. Not a full JSON Schema — checks presence + the few invariants
    the dashboard relies on.
    """
    missing = [k for k in _REQUIRED_TOP_KEYS if k not in receipt]
    if missing:
        raise ValueError(f"receipt missing keys: {missing}")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {receipt['schema_version']!r}")
    if receipt["outcome"] not in {"committed", "deferred", "noop", "failed"}:
        raise ValueError(f"bad outcome {receipt['outcome']!r}")
    if receipt["mode"] not in {"edit_resume", "live"}:
        raise ValueError(f"bad mode {receipt['mode']!r}")
    if receipt["tokens"].get("method") not in _TOKEN_METHODS:
        raise ValueError(f"bad tokens.method {receipt['tokens'].get('method')!r}")
    if receipt["trigger"].get("source") not in {
        "manual", "guard", "hook", "precompact", "overflow",
    }:
        raise ValueError(f"bad trigger.source {receipt['trigger'].get('source')!r}")
    for key in ("before", "after", "reclaimed"):
        if key not in receipt["bytes"]:
            raise ValueError(f"bytes.{key} missing")
    if not isinstance(receipt["strategies"], list):
        raise ValueError("strategies must be a list")
    for s in receipt["strategies"]:
        if not isinstance(s, dict) or "id" not in s:
            raise ValueError("each strategy must be a dict with an 'id'")
    # Numeric fields must not contain NaN/inf — json.dumps emits non-standard
    # Python-specific literals ("NaN", "Infinity") that are invalid JSON for
    # any non-Python consumer.  Check the float fields that build_receipt can
    # produce; ints are not affected (JSON integers are always finite).
    def _assert_finite(section: str, key: str, val) -> None:
        if isinstance(val, float) and not math.isfinite(val):
            raise ValueError(f"{section}.{key} must be finite, got {val!r}")

    _tokens = receipt.get("tokens", {})
    for _key in ("before", "after", "reclaimed", "reclaimed_pct"):
        _assert_finite("tokens", _key, _tokens.get(_key))
    _bytes = receipt.get("bytes", {})
    for _key in ("before", "after", "reclaimed"):
        _assert_finite("bytes", _key, _bytes.get(_key))
    _assert_finite("model", "context_window", receipt.get("model", {}).get("context_window"))
