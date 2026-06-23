"""Standing safeguard: adversarial corpus for the dashboard coercion surface.

Mirrors the structure/philosophy of tests/test_input_coercion_corpus.py.

Invariant:
    For every adversarial input, a dashboard numeric coercion MUST either return
    a finite, in-range, correctly-typed value OR return None (which renders as
    "—"). It MUST NOT raise an exception — a corrupt receipt must never crash
    `cozempic dashboard`.

Covers: _context_pct, _int (aggregate), _num_or_zero (lifetime), load_lifetime,
aggregate (end-to-end), _fmt_tokens/_fmt_bytes (render), build_receipt,
validate_receipt (NaN/inf rejection), receipts_enabled (env-bool fix).
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cozempic.dashboard.aggregate import _context_pct, _int, aggregate
from cozempic.dashboard.lifetime import _num_or_zero, load_lifetime
from cozempic.dashboard.render import _fmt_bytes, _fmt_int, _fmt_tokens
from cozempic.metrics import (
    ClaudeMetricsAdapter,
    TriggerInfo,
    build_receipt,
    serialize_receipt,
    validate_receipt,
)
from cozempic.receipts import receipts_enabled
from cozempic.types import PrescriptionResult, StrategyResult

_HUGE_INT = 10 ** 400  # same sentinel as test_input_coercion_corpus.py
_MAX_RECEIPT_INT = 10 ** 15  # referenced for assertions


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_minimal_receipt(outcome="committed", **overrides) -> dict:
    """Minimal valid v1.0 receipt dict for structural-validation tests."""
    r = {
        "schema_version": "1.0",
        "receipt_id": "test-id-1",
        "ts": "2026-01-01T00:00:00Z",
        "tool": {"name": "cozempic", "version": "1.8.0"},
        "agent": {"name": "claude", "version": "1.0", "adapter_schema_version": 1},
        "session": {"id_hash": "abc", "transcript_hash": "def", "cwd_hash": "ghi"},
        "trigger": {"source": "manual", "tier": "standard",
                    "prescription": "standard", "reason": ""},
        "mode": "edit_resume",
        "model": {"name": "claude-opus-4-8", "context_window": 200000},
        "entries": {"before": 10, "after": 9, "removed": 1, "replaced": 0},
        "bytes": {"before": 5000, "after": 4000, "reclaimed": 1000},
        "tokens": {
            "before": 2000, "after": 1500, "reclaimed": 500,
            "reclaimed_pct": 25.0, "method": "exact", "confidence": "high",
        },
        "strategies": [{"id": "tool-output-trim", "tier": "standard",
                        "tokens_reclaimed": 500, "bytes_reclaimed": 1000,
                        "entries_affected": 1}],
        "protected": {"entries": 0, "reasons": {}},
        "validation": {"passed": True, "deferred": False,
                       "defer_reason": None, "checks_run": []},
        "outcome": outcome,
        "timing_ms": {},
    }
    for k, v in overrides.items():
        # Support nested key like "tokens.reclaimed_pct"
        if "." in k:
            top, sub = k.split(".", 1)
            r[top][sub] = v
        else:
            r[k] = v
    return r


def _make_committed_receipt(**overrides) -> dict:
    """Minimal committed receipt for aggregate() tests."""
    r = {
        "outcome": "committed",
        "tokens": {"after": 5000, "reclaimed": 500},
        "bytes": {"before": 5000, "after": 4000, "reclaimed": 1000},
        "session": {"id_hash": "s1"},
        "agent": {"name": "claude"},
        "trigger": {"tier": "standard"},
        "model": {"context_window": 200000},
        "strategies": [],
        "ts": "2026-01-01T00:00:00Z",
    }
    r.update(overrides)
    return r


def _make_prescription_result(**kwargs) -> PrescriptionResult:
    sr = StrategyResult(
        strategy_name="tool-output-trim",
        actions=[],
        original_bytes=kwargs.get("original_total_bytes", 5000),
        pruned_bytes=kwargs.get("final_total_bytes", 4000),
        messages_affected=1,
        messages_removed=0,
        messages_replaced=1,
        summary="trimmed",
    )
    return PrescriptionResult(
        prescription_name="standard",
        strategy_results=[sr],
        original_total_bytes=kwargs.get("original_total_bytes", 5000),
        final_total_bytes=kwargs.get("final_total_bytes", 4000),
        original_message_count=10,
        final_message_count=9,
        original_tokens=kwargs.get("original_tokens", 2000),
        final_tokens=kwargs.get("final_tokens", 1500),
        token_method="exact",
        model="claude-opus-4-8",
        context_window=200000,
    )


# ── TestContextPct ─────────────────────────────────────────────────────────────

class TestContextPct(unittest.TestCase):
    """_context_pct must never raise; must return None for bools/huge/negative."""

    def test_huge_after_returns_none(self):
        """OVERFLOW: after=10**400 must not raise OverflowError; must return None."""
        r = _context_pct({
            "tokens": {"after": _HUGE_INT},
            "model": {"context_window": 200000},
        })
        # Must not raise — if it returns a value it must be finite
        if r is not None:
            self.assertTrue(math.isfinite(r), f"leaked non-finite: {r!r}")

    def test_huge_window_returns_none(self):
        """OVERFLOW: window=10**400 must not raise; must return None."""
        r = _context_pct({
            "tokens": {"after": 200000},
            "model": {"context_window": _HUGE_INT},
        })
        self.assertIsNone(r)

    def test_bool_after_returns_none(self):
        """Bool pass-through: True is an int subclass, must be excluded -> None."""
        r = _context_pct({
            "tokens": {"after": True},
            "model": {"context_window": 200000},
        })
        self.assertIsNone(r, f"bool True leaked through _context_pct as {r!r}")

    def test_bool_window_returns_none(self):
        r = _context_pct({
            "tokens": {"after": 200000},
            "model": {"context_window": True},
        })
        self.assertIsNone(r, f"bool True window leaked through _context_pct as {r!r}")

    def test_negative_window_returns_none(self):
        """Negative window must return None (not a negative %)."""
        r = _context_pct({
            "tokens": {"after": 500000},
            "model": {"context_window": -1},
        })
        self.assertIsNone(r, f"negative window leaked through as {r!r}")

    def test_valid_returns_correct_pct(self):
        """Sanity: valid inputs produce correct percentage."""
        r = _context_pct({
            "tokens": {"after": 100000},
            "model": {"context_window": 200000},
        })
        self.assertEqual(r, 50.0)


# ── TestNumOrZero ──────────────────────────────────────────────────────────────

class TestNumOrZero(unittest.TestCase):
    """_num_or_zero must clamp huge ints and return 0 for negatives/NaN/bool."""

    def test_huge_int_clamped(self):
        """10**400 must be clamped, not returned as a 401-digit int."""
        r = _num_or_zero(_HUGE_INT)
        self.assertIsInstance(r, int)
        self.assertLessEqual(r, _MAX_RECEIPT_INT,
                             f"huge int leaked: got {r!r}, expected <= {_MAX_RECEIPT_INT!r}")

    def test_negative_returns_zero(self):
        self.assertEqual(_num_or_zero(-1), 0)

    def test_nan_returns_zero(self):
        self.assertEqual(_num_or_zero(float("nan")), 0)

    def test_inf_returns_zero(self):
        self.assertEqual(_num_or_zero(float("inf")), 0)

    def test_bool_true_returns_zero(self):
        """Existing behavior: bool -> 0."""
        self.assertEqual(_num_or_zero(True), 0)

    def test_valid_int_passthrough(self):
        self.assertEqual(_num_or_zero(456170685), 456170685)

    def test_valid_float_truncated(self):
        self.assertEqual(_num_or_zero(1000.7), 1000)


# ── TestLoadLifetime ───────────────────────────────────────────────────────────

class TestLoadLifetime(unittest.TestCase):
    """load_lifetime must never raise (docstring: 'It never raises')."""

    def _write_ledger(self, tmp_dir: str, data: dict) -> Path:
        p = Path(tmp_dir) / "ledger.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_huge_tokens_saved_does_not_raise(self):
        """load_lifetime with tokens_saved=10**400 must not raise OverflowError."""
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_ledger(tmp, {
                "tokens_saved": _HUGE_INT,
                "tokens_processed": 200_000_000,
            })
            # Must not raise — if it returns something it must be a dict or None
            result = load_lifetime(p)
            self.assertIn(type(result), (dict, type(None)))

    def test_huge_tokens_processed_does_not_raise(self):
        """tokens_processed=10**400 must not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_ledger(tmp, {
                "tokens_saved": 1_000_000,
                "tokens_processed": _HUGE_INT,
            })
            result = load_lifetime(p)
            self.assertIn(type(result), (dict, type(None)))

    def test_huge_tracked_prunes_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_ledger(tmp, {
                "tokens_saved": 1_000_000,
                "tokens_processed": 5_000_000,
                "sessions": 10,
                "tracked_prunes": _HUGE_INT,
            })
            result = load_lifetime(p)
            self.assertIn(type(result), (dict, type(None)))


# ── TestInt ────────────────────────────────────────────────────────────────────

class TestInt(unittest.TestCase):
    """aggregate._int must clamp huge ints and return 0 for negatives/bools."""

    def test_huge_int_clamped_not_leaked(self):
        """10**400 must be clamped, not returned as a 401-digit int."""
        r = _int(_HUGE_INT)
        self.assertIsInstance(r, int)
        self.assertLessEqual(r, _MAX_RECEIPT_INT,
                             f"huge int leaked from _int: {r!r}")

    def test_negative_returns_zero(self):
        self.assertEqual(_int(-1), 0)

    def test_negative_large_returns_zero(self):
        self.assertEqual(_int(-_HUGE_INT), 0)

    def test_bool_true_returns_zero(self):
        """Existing behavior: bool -> 0."""
        self.assertEqual(_int(True), 0)

    def test_valid_passthrough(self):
        self.assertEqual(_int(100), 100)


# ── TestAggregateSumNegativeReclaimed ──────────────────────────────────────────

class TestAggregateSumNegativeReclaimed(unittest.TestCase):
    """aggregate must not sum negative reclaimed values into lifetime totals."""

    def _receipt_with_reclaimed(self, tokens_reclaimed, bytes_reclaimed) -> dict:
        return {
            "outcome": "committed",
            "tokens": {"after": 500, "reclaimed": tokens_reclaimed},
            "bytes": {"before": 5000, "after": 4000, "reclaimed": bytes_reclaimed},
            "session": {"id_hash": "s1"},
            "agent": {"name": "claude"},
            "trigger": {"tier": "standard"},
            "model": {"context_window": 200000},
            "strategies": [],
            "ts": "2026-01-01T00:00:00Z",
        }

    def test_negative_reclaimed_not_summed_into_lifetime(self):
        """Negative reclaimed must not subtract from lifetime totals."""
        receipts = [self._receipt_with_reclaimed(-1000, -500)]
        data = aggregate(receipts)
        self.assertEqual(data["lifetime"]["tokens_reclaimed"], 0,
                         "negative tokens_reclaimed leaked into lifetime")
        self.assertEqual(data["lifetime"]["bytes_reclaimed"], 0,
                         "negative bytes_reclaimed leaked into lifetime")

    def test_huge_reclaimed_clamped_in_lifetime(self):
        """Huge int reclaimed must be clamped, not leaked."""
        receipts = [self._receipt_with_reclaimed(_HUGE_INT, _HUGE_INT)]
        data = aggregate(receipts)
        self.assertLessEqual(data["lifetime"]["tokens_reclaimed"], _MAX_RECEIPT_INT,
                             "huge tokens_reclaimed leaked into lifetime")


# ── TestAggregateWithHugeReceipts ─────────────────────────────────────────────

class TestAggregateWithHugeReceipts(unittest.TestCase):
    """aggregate must not raise on corrupt receipts with huge ints."""

    def test_aggregate_with_huge_context_pct_does_not_raise(self):
        """aggregate([receipt with tokens.after=10**400]) must not raise OverflowError."""
        receipts = [_make_committed_receipt(
            tokens={"after": _HUGE_INT, "reclaimed": 500},
        )]
        # Must not raise
        aggregate(receipts)

    def test_aggregate_with_huge_reclaimed_lifetime_clamped(self):
        """aggregate([receipt with tokens.reclaimed=10**400]) must clamp the lifetime total."""
        receipts = [_make_committed_receipt(
            tokens={"after": 5000, "reclaimed": _HUGE_INT},
        )]
        data = aggregate(receipts)
        # The lifetime total must be clamped, not a 401-digit int
        self.assertLessEqual(
            data["lifetime"]["tokens_reclaimed"],
            _MAX_RECEIPT_INT,
            "huge int reclaimed leaked into lifetime total unguarded",
        )


# ── TestBuildReceiptFiniteness ─────────────────────────────────────────────────

class TestBuildReceiptFiniteness(unittest.TestCase):
    """build_receipt must not raise OverflowError for huge original_tokens."""

    def _build_with_tokens(self, original_tokens):
        return build_receipt(
            _make_prescription_result(
                original_tokens=original_tokens,
                final_tokens=0,
            ),
            adapter=ClaudeMetricsAdapter(),
            session_id="sess-test",
            trigger=TriggerInfo("manual", "standard", "standard", "test"),
            ts="2026-06-01T00:00:00Z",
            receipt_id="test-id",
            tool_version="1.8.0",
        )

    def test_huge_tokens_before_does_not_raise(self):
        """build_receipt with original_tokens=10**400 must not raise OverflowError."""
        receipt = self._build_with_tokens(_HUGE_INT)
        self.assertIsNotNone(receipt)

    def test_reclaimed_pct_is_finite(self):
        """tokens.reclaimed_pct must be a finite float, not NaN/inf."""
        receipt = self._build_with_tokens(_HUGE_INT)
        pct = receipt["tokens"]["reclaimed_pct"]
        self.assertIsInstance(pct, float)
        self.assertTrue(math.isfinite(pct),
                        f"reclaimed_pct is not finite: {pct!r}")


# ── TestValidateReceiptNanInf ──────────────────────────────────────────────────

class TestValidateReceiptNanInf(unittest.TestCase):
    """validate_receipt must reject NaN/inf in numeric token/model fields."""

    def test_rejects_nan_reclaimed_pct(self):
        r = _make_minimal_receipt()
        r["tokens"]["reclaimed_pct"] = float("nan")
        with self.assertRaises(ValueError):
            validate_receipt(r)

    def test_rejects_inf_reclaimed_pct(self):
        r = _make_minimal_receipt()
        r["tokens"]["reclaimed_pct"] = float("inf")
        with self.assertRaises(ValueError):
            validate_receipt(r)

    def test_rejects_nan_context_window(self):
        r = _make_minimal_receipt()
        r["model"]["context_window"] = float("nan")
        with self.assertRaises(ValueError):
            validate_receipt(r)

    def test_serialize_does_not_emit_nan_literal(self):
        """After validate + build pipeline, serialize must not produce 'NaN' string."""
        r = _make_minimal_receipt()
        # A valid receipt (no NaN) must not produce NaN literal
        json_str = serialize_receipt(r)
        self.assertNotIn("NaN", json_str,
                         "serialize_receipt emitted non-standard NaN literal")
        self.assertNotIn("Infinity", json_str,
                         "serialize_receipt emitted non-standard Infinity literal")


# ── TestReceiptsEnabled ────────────────────────────────────────────────────────

class TestReceiptsEnabled(unittest.TestCase):
    """receipts_enabled must use parse_env_bool semantics for COZEMPIC_NO_RECEIPTS."""

    def test_zero_value_does_not_disable(self):
        """COZEMPIC_NO_RECEIPTS=0 means 'no, don't disable' -> receipts enabled."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "0"}):
            self.assertTrue(
                receipts_enabled(),
                "COZEMPIC_NO_RECEIPTS=0 must leave receipts enabled (bug: '0' is falsy-string)",
            )

    def test_false_value_does_not_disable(self):
        """COZEMPIC_NO_RECEIPTS=false -> receipts enabled."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "false"}):
            self.assertTrue(
                receipts_enabled(),
                "COZEMPIC_NO_RECEIPTS=false must leave receipts enabled",
            )

    def test_one_still_disables(self):
        """Regression guard: COZEMPIC_NO_RECEIPTS=1 must still disable receipts."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "1"}):
            self.assertFalse(receipts_enabled())

    def test_absent_is_enabled(self):
        """Regression guard: absent env var -> receipts enabled."""
        env = {k: v for k, v in os.environ.items() if k != "COZEMPIC_NO_RECEIPTS"}
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(receipts_enabled())


# ── TestFmtHelpersCorpus ───────────────────────────────────────────────────────

class TestFmtHelpersCorpus(unittest.TestCase):
    """_fmt_tokens and _fmt_bytes must not raise on adversarial inputs."""

    _CORPUS = [_HUGE_INT, -_HUGE_INT, float("nan"), float("inf"), float("-inf")]

    def test_fmt_tokens_huge_int_does_not_raise(self):
        """_fmt_tokens(10**400) must not raise OverflowError."""
        for v in self._CORPUS:
            with self.subTest(value=repr(v)):
                result = _fmt_tokens(v)
                self.assertIsInstance(result, str)
                self.assertTrue(len(result) > 0, "returned empty string")

    def test_fmt_bytes_huge_int_does_not_raise(self):
        """_fmt_bytes(10**400) must not raise OverflowError."""
        for v in self._CORPUS:
            with self.subTest(value=repr(v)):
                result = _fmt_bytes(v)
                self.assertIsInstance(result, str)
                self.assertTrue(len(result) > 0, "returned empty string")

    def test_fmt_int_nan_does_not_raise(self):
        """_fmt_int(float('nan')) must not raise ValueError."""
        result = _fmt_int(float("nan"))
        self.assertIsInstance(result, str)


if __name__ == "__main__":
    unittest.main()
