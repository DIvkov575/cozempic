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
        self.assertIsNone(r, f"huge after leaked through _context_pct as {r!r}")

    def test_negative_after_returns_none(self):
        """F-5: negative after must return None (not a negative %)."""
        r = _context_pct({
            "tokens": {"after": -5},
            "model": {"context_window": 100},
        })
        self.assertIsNone(r, f"negative after leaked through _context_pct as {r!r}")

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
    """_num_or_zero must return 0 for huge ints/negatives/NaN/bool (sibling parity with _int)."""

    def test_huge_int_returns_zero(self):
        """10**400 must return 0 (sibling parity with _int huge->0; subsumes the old assertLessEqual guard)."""
        r = _num_or_zero(_HUGE_INT)
        self.assertEqual(r, 0,
                         f"_num_or_zero(10**400) must be 0 (sibling parity with _int), got {r!r}")

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

    def test_huge_tokens_saved_returns_none(self):
        """F-1: huge tokens_saved -> _num_or_zero returns 0 -> load_lifetime hits saved<=0 early-out -> None."""
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_ledger(tmp, {
                "tokens_saved": _HUGE_INT,
                "tokens_processed": 200_000_000,
            })
            result = load_lifetime(p)
            self.assertIsNone(result,
                              f"load_lifetime with huge tokens_saved must return None (not a fabricated dict), got {result!r}")

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

    def test_huge_int_returns_zero(self):
        """10**400 -> 0 (corruption sentinel), sibling-consistent with
        _num_or_zero. assertEqual(0), NOT assertLessEqual(_MAX_RECEIPT_INT):
        the latter would also pass a clamp-to-_MAX regression that leaks a
        1-quadrillion lifetime total onto the dashboard."""
        r = _int(_HUGE_INT)
        self.assertEqual(r, 0, f"huge int not zeroed from _int: {r!r}")

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

    def test_rejects_nan_bytes_before(self):
        """F-4: validate_receipt must reject bytes.before=NaN (same class as tokens.*)."""
        r = _make_minimal_receipt()
        r["bytes"]["before"] = float("nan")
        with self.assertRaises(ValueError,
                               msg="bytes.before=NaN must raise ValueError"):
            validate_receipt(r)

    def test_rejects_nan_bytes_after(self):
        """F-4: validate_receipt must reject bytes.after=NaN."""
        r = _make_minimal_receipt()
        r["bytes"]["after"] = float("nan")
        with self.assertRaises(ValueError,
                               msg="bytes.after=NaN must raise ValueError"):
            validate_receipt(r)

    def test_rejects_inf_bytes_reclaimed(self):
        """F-4: validate_receipt must reject bytes.reclaimed=inf."""
        r = _make_minimal_receipt()
        r["bytes"]["reclaimed"] = float("inf")
        with self.assertRaises(ValueError,
                               msg="bytes.reclaimed=inf must raise ValueError"):
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
    """receipts_enabled full truth table — privacy fail-safe direction.

    Opt-OUT env: any non-empty TRUTHY token ({1,true,yes,on}) disables.
    Explicit FALSY token ({0,false,no,off}) and unrecognized/garbage values
    must NOT disable (privacy fail-safe: ambiguous opt-out -> receipts OFF).
    Unset/empty/whitespace-only -> ON (default).
    """

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

    def test_no_value_does_not_disable(self):
        """F-2: COZEMPIC_NO_RECEIPTS=no -> receipts enabled (explicit falsy = keep ON)."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "no"}):
            self.assertTrue(
                receipts_enabled(),
                "COZEMPIC_NO_RECEIPTS=no must leave receipts enabled",
            )

    def test_off_value_does_not_disable(self):
        """F-2: COZEMPIC_NO_RECEIPTS=off -> receipts enabled (explicit falsy = keep ON)."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "off"}):
            self.assertTrue(
                receipts_enabled(),
                "COZEMPIC_NO_RECEIPTS=off must leave receipts enabled",
            )

    def test_disabled_string_disables(self):
        """F-2: COZEMPIC_NO_RECEIPTS=disabled -> False (privacy fail-safe: unrecognized opt-out -> OFF)."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "disabled"}):
            self.assertFalse(
                receipts_enabled(),
                "COZEMPIC_NO_RECEIPTS=disabled must disable receipts (privacy fail-safe: unrecognized -> OFF)",
            )

    def test_garbage_value_disables(self):
        """F-2: COZEMPIC_NO_RECEIPTS=nope -> False (privacy fail-safe: unrecognized opt-out -> OFF)."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "nope"}):
            self.assertFalse(
                receipts_enabled(),
                "COZEMPIC_NO_RECEIPTS=nope must disable receipts (privacy fail-safe)",
            )

    def test_digit_two_disables(self):
        """F-2: COZEMPIC_NO_RECEIPTS=2 -> False (privacy fail-safe: unrecognized -> OFF)."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "2"}):
            self.assertFalse(
                receipts_enabled(),
                "COZEMPIC_NO_RECEIPTS=2 must disable receipts (privacy fail-safe)",
            )

    def test_whitespace_only_enables(self):
        """F-2: COZEMPIC_NO_RECEIPTS='  ' (whitespace-only) -> True (treated as absent)."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "  "}):
            self.assertTrue(
                receipts_enabled(),
                "COZEMPIC_NO_RECEIPTS=whitespace-only must leave receipts enabled (treated as absent)",
            )

    def test_one_still_disables(self):
        """Regression guard: COZEMPIC_NO_RECEIPTS=1 must still disable receipts."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "1"}):
            self.assertFalse(receipts_enabled())

    def test_true_disables(self):
        """Regression guard: COZEMPIC_NO_RECEIPTS=true must disable receipts."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "true"}):
            self.assertFalse(receipts_enabled())

    def test_yes_disables(self):
        """Regression guard: COZEMPIC_NO_RECEIPTS=yes must disable receipts."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "yes"}):
            self.assertFalse(receipts_enabled())

    def test_on_disables(self):
        """Regression guard: COZEMPIC_NO_RECEIPTS=on must disable receipts."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "on"}):
            self.assertFalse(receipts_enabled())

    def test_absent_is_enabled(self):
        """Regression guard: absent env var -> receipts enabled. Targeted pop
        (not clear=True full-env-copy) so the test stays hermetic and can't be
        perturbed by other env vars read during the call."""
        with patch.dict(os.environ):
            os.environ.pop("COZEMPIC_NO_RECEIPTS", None)
            self.assertTrue(receipts_enabled())

    def test_empty_string_is_enabled(self):
        """F-2: COZEMPIC_NO_RECEIPTS='' -> True (empty treated as absent)."""
        with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": ""}):
            self.assertTrue(
                receipts_enabled(),
                "COZEMPIC_NO_RECEIPTS='' must leave receipts enabled (treated as absent)",
            )


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

    def test_fmt_bytes_huge_int_consistent_format(self):
        """F-3: _fmt_bytes(10**400) must return a GB string, not '0 B' (parity with _fmt_tokens)."""
        result = _fmt_bytes(_HUGE_INT)
        self.assertNotEqual(
            result, "0 B",
            f"_fmt_bytes(10**400) returned '0 B' — must clamp before float() like _fmt_tokens does",
        )
        # Must end with a unit suffix (not just "0 B")
        self.assertTrue(
            any(result.endswith(unit) for unit in (" B", " KB", " MB", " GB")),
            f"_fmt_bytes(10**400) returned unexpected format: {result!r}",
        )

    def test_fmt_int_nan_does_not_raise(self):
        """_fmt_int(float('nan')) must not raise ValueError."""
        result = _fmt_int(float("nan"))
        self.assertIsInstance(result, str)


# ── TestAggregateTimestampSort (F-A) ──────────────────────────────────────────

class TestAggregateTimestampSort(unittest.TestCase):
    """F-A: aggregate() must not crash when ts is a non-string (e.g. int)."""

    def _receipt_with_ts(self, ts_value, session_id="s1"):
        return {
            "outcome": "committed",
            "tokens": {"after": 5000, "reclaimed": 500},
            "bytes": {"before": 5000, "after": 4000, "reclaimed": 1000},
            "session": {"id_hash": session_id},
            "agent": {"name": "claude"},
            "trigger": {"tier": "standard"},
            "model": {"context_window": 200000},
            "strategies": [],
            "ts": ts_value,
        }

    def test_int_ts_mixed_with_str_ts_does_not_raise(self):
        """F-A: timeline.sort() with mixed str+int ts must not raise TypeError."""
        receipts = [
            self._receipt_with_ts("2026-01-01T00:00:00Z"),
            self._receipt_with_ts(9999999999),  # int ts — corrupt receipt
        ]
        # Must not raise TypeError: '<' not supported between instances of 'int' and 'str'
        aggregate(receipts)

    def test_per_session_sort_with_int_ts_does_not_raise(self):
        """F-A: per_session sorted() with an int last-ts must not raise TypeError."""
        receipts = [
            self._receipt_with_ts("2026-01-01T00:00:00Z", session_id="s1"),
            self._receipt_with_ts(9999999999, session_id="s2"),  # int ts session
        ]
        aggregate(receipts)

    def test_timeline_entry_ts_preserved(self):
        """F-A: after fix, valid str ts is preserved in timeline entries."""
        receipts = [self._receipt_with_ts("2026-06-01T12:00:00Z")]
        data = aggregate(receipts)
        session = data["per_session"][0]
        self.assertEqual(session["timeline"][0]["ts"], "2026-06-01T12:00:00Z")


# ── TestBuildReceiptHamiltonInt (F-B) ─────────────────────────────────────────

class TestBuildReceiptHamiltonInt(unittest.TestCase):
    """F-B: build_receipt with huge original_tokens must attribute strategies via int arithmetic."""

    def _build_two_strategy_receipt(self, original_tokens):
        sr1 = StrategyResult(
            strategy_name="tool-output-trim",
            actions=[],
            original_bytes=6000,
            pruned_bytes=3000,
            messages_affected=2,
            messages_removed=1,
            messages_replaced=1,
            summary="trimmed",
        )
        sr2 = StrategyResult(
            strategy_name="stale-tool-results",
            actions=[],
            original_bytes=4000,
            pruned_bytes=2000,
            messages_affected=1,
            messages_removed=1,
            messages_replaced=0,
            summary="dropped",
        )
        result = PrescriptionResult(
            prescription_name="standard",
            strategy_results=[sr1, sr2],
            original_total_bytes=10000,
            final_total_bytes=5000,
            original_message_count=10,
            final_message_count=8,
            original_tokens=original_tokens,
            final_tokens=0,
            token_method="exact",
            model="claude-opus-4-8",
            context_window=200000,
        )
        return build_receipt(
            result,
            adapter=ClaudeMetricsAdapter(),
            session_id="sess-fb",
            trigger=TriggerInfo("manual", "standard", "standard", "test"),
            ts="2026-06-01T00:00:00Z",
            receipt_id="test-fb",
            tool_version="1.8.0",
        )

    def test_huge_tokens_reclaimed_no_raise(self):
        """F-B: build_receipt(original=10**400, final=0) must not raise OverflowError."""
        receipt = self._build_two_strategy_receipt(10 ** 400)
        self.assertIsNotNone(receipt)

    def test_huge_tokens_strategies_non_zero(self):
        """F-B: with 2 strategies, each must get a non-zero attributed tokens_reclaimed."""
        receipt = self._build_two_strategy_receipt(10 ** 400)
        strategies = receipt["strategies"]
        self.assertEqual(len(strategies), 2)
        # Both strategies contributed bytes; both must get non-zero attribution
        for s in strategies:
            self.assertGreater(
                s["tokens_reclaimed"], 0,
                f"strategy {s['id']!r} got zero attribution from huge tokens_reclaimed "
                f"— OverflowError likely zeroed all strategies",
            )

    def test_huge_tokens_strategy_sum_equals_receipt_reclaimed(self):
        """F-B: sum of per-strategy tokens_reclaimed must equal receipt tokens.reclaimed."""
        receipt = self._build_two_strategy_receipt(10 ** 400)
        strategy_sum = sum(s["tokens_reclaimed"] for s in receipt["strategies"])
        total = receipt["tokens"]["reclaimed"]
        self.assertEqual(
            strategy_sum, total,
            f"strategy sum {strategy_sum} != receipt reclaimed {total} — Hamilton split broken",
        )


# ── TestSerializeReceiptNanRaises (F-C) ───────────────────────────────────────

class TestSerializeReceiptNanRaises(unittest.TestCase):
    """F-C: serialize_receipt must raise ValueError on NaN/inf (allow_nan=False)."""

    def _receipt_with_nan_field(self, section, key, value):
        r = _make_minimal_receipt()
        r[section][key] = value
        return r

    def test_nan_in_tokens_reclaimed_pct_raises(self):
        """F-C: serialize_receipt with tokens.reclaimed_pct=NaN must raise ValueError."""
        r = self._receipt_with_nan_field("tokens", "reclaimed_pct", float("nan"))
        with self.assertRaises(ValueError,
                               msg="serialize_receipt must raise ValueError on NaN (allow_nan=False)"):
            serialize_receipt(r)

    def test_inf_in_tokens_reclaimed_pct_raises(self):
        """F-C: serialize_receipt with tokens.reclaimed_pct=inf must raise ValueError."""
        r = self._receipt_with_nan_field("tokens", "reclaimed_pct", float("inf"))
        with self.assertRaises(ValueError):
            serialize_receipt(r)

    def test_nan_in_bytes_before_raises(self):
        """F-C: serialize_receipt with bytes.before=NaN must raise ValueError."""
        r = self._receipt_with_nan_field("bytes", "before", float("nan"))
        with self.assertRaises(ValueError):
            serialize_receipt(r)

    def test_valid_receipt_does_not_raise(self):
        """F-C regression: a valid receipt (no NaN) must still serialize cleanly."""
        r = _make_minimal_receipt()
        result = serialize_receipt(r)
        self.assertIsInstance(result, str)
        self.assertNotIn("NaN", result)
        self.assertNotIn("Infinity", result)


# ── TestLifetimeBandNonFloat (F-D) ────────────────────────────────────────────

class TestLifetimeBandNonFloat(unittest.TestCase):
    """F-D: render_html must not crash when ledger contains a non-float/non-finite field."""

    def _render_with_ledger(self, ledger):
        from cozempic.dashboard.render import render_html
        data = aggregate([])  # empty receipts — valid views dict
        render_html(data, generated_ts="2026-06-01T00:00:00Z", ledger=ledger)

    def test_savings_rate_pct_str_does_not_raise(self):
        """F-D: render_html with savings_rate_pct='bad' must not raise ValueError."""
        self._render_with_ledger({
            "tokens_saved": 1_000_000,
            "savings_rate_pct": "bad",
        })

    def test_savings_rate_pct_nan_does_not_raise(self):
        """F-D: render_html with savings_rate_pct=NaN must not raise."""
        self._render_with_ledger({
            "tokens_saved": 1_000_000,
            "savings_rate_pct": float("nan"),
        })

    def test_session_multiplier_str_does_not_raise(self):
        """F-D: render_html with session_multiplier_x='x' must not raise ValueError."""
        self._render_with_ledger({
            "tokens_saved": 1_000_000,
            "session_multiplier_x": "x",
        })

    def test_session_multiplier_inf_does_not_raise(self):
        """F-D: render_html with session_multiplier_x=inf must not raise."""
        self._render_with_ledger({
            "tokens_saved": 1_000_000,
            "session_multiplier_x": float("inf"),
        })


# ── TestSessionStemControlChars (F-E) ─────────────────────────────────────────

class TestSessionStemControlChars(unittest.TestCase):
    """F-E: _session_stem must strip control characters from id_hash."""

    def _make_receipt_with_id(self, id_hash):
        return {"session": {"id_hash": id_hash}}

    def _stem(self, id_hash):
        from cozempic.receipts import _session_stem
        return _session_stem(self._make_receipt_with_id(id_hash))

    def test_newline_stripped(self):
        """F-E: a session id containing \\n must produce a stem with no newline."""
        stem = self._stem("abc\ndef")
        self.assertNotIn("\n", stem,
                         f"newline not stripped from session stem: {stem!r}")

    def test_null_byte_stripped(self):
        """F-E: a session id containing \\x00 must produce a stem with no null byte."""
        stem = self._stem("abc\x00def")
        self.assertNotIn("\x00", stem,
                         f"null byte not stripped from session stem: {stem!r}")

    def test_control_chars_stripped(self):
        """F-E: control chars \\x01-\\x1f must be stripped."""
        stem = self._stem("abc\x01\x1fdef")
        for c in "\x01\x1f":
            self.assertNotIn(c, stem)

    def test_write_receipt_with_newline_id_creates_valid_file(self):
        """F-E: write_receipt with newline in id produces a discoverable, valid filename."""
        from cozempic.receipts import _session_stem
        receipt = _make_minimal_receipt()
        receipt["session"]["id_hash"] = "sha256:abc\ndef123456"
        stem = _session_stem(receipt)
        # The stem must be a valid filename component (no \n, no \x00)
        self.assertNotIn("\n", stem)
        self.assertNotIn("\x00", stem)
        # Must not be empty or just "unknown"
        self.assertTrue(len(stem) > 0)

    def test_del_char_stripped(self):
        """F-E: DEL (\\x7f) must also be stripped."""
        stem = self._stem("abc\x7fdef")
        self.assertNotIn("\x7f", stem)


# ── TestAggregateStrategyNullId (F-F) ─────────────────────────────────────────

class TestAggregateStrategyNullId(unittest.TestCase):
    """F-F: explicit null strategy id/tier must fall back to 'unknown'."""

    def _receipt_with_strategy_id(self, strategy_id):
        return {
            "outcome": "committed",
            "tokens": {"after": 5000, "reclaimed": 500},
            "bytes": {"before": 5000, "after": 4000, "reclaimed": 1000},
            "session": {"id_hash": "s1"},
            "agent": {"name": "claude"},
            "trigger": {"tier": "standard"},
            "model": {"context_window": 200000},
            "strategies": [{"id": strategy_id, "tier": "standard",
                             "tokens_reclaimed": 200, "bytes_reclaimed": 500}],
            "ts": "2026-01-01T00:00:00Z",
        }

    def test_null_id_becomes_unknown(self):
        """F-F: strategy id=null (JSON None) must produce leaderboard entry id='unknown'."""
        receipts = [self._receipt_with_strategy_id(None)]
        data = aggregate(receipts)
        self.assertTrue(
            len(data["per_strategy"]) > 0,
            "no leaderboard entry produced for null-id strategy",
        )
        # The leaderboard key must be "unknown", not None
        ids = [s["id"] for s in data["per_strategy"]]
        self.assertIn("unknown", ids,
                      f"null id did not become 'unknown': leaderboard ids = {ids!r}")
        self.assertNotIn(None, ids,
                         f"None slipped through as a leaderboard id: {ids!r}")


# ── TestSessionMultiplierCap (F-G) ────────────────────────────────────────────

class TestSessionMultiplierCap(unittest.TestCase):
    """F-G: session_multiplier_x must be capped at 100_000 (nonsensically huge -> None)."""

    def _write_ledger(self, tmp_dir, data):
        p = Path(tmp_dir) / "ledger.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_crafted_ledger_multiplier_capped(self):
        """F-G: tracked_prunes=_MAX_RECEIPT_INT, sessions=5, rate=100% -> multiplier~2e14 -> None."""
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_ledger(tmp, {
                "tokens_saved": 1_000_000_000,
                "tokens_processed": 1_000_000_000,   # rate = 100%
                "sessions": 5,
                "tracked_prunes": _MAX_RECEIPT_INT,   # 10**15 / 5 * 1.0 = 2e14
                "prune_count": 100,
            })
            result = load_lifetime(p)
            self.assertIsNotNone(result, "load_lifetime should return a dict for valid ledger")
            multiplier = result.get("session_multiplier_x")
            self.assertIsNone(
                multiplier,
                f"session_multiplier_x should be None for absurd ~2e14 value, got {multiplier!r}",
            )

    def test_reasonable_multiplier_preserved(self):
        """F-G regression: a reasonable multiplier (e.g. 5.0) must still come through."""
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write_ledger(tmp, {
                "tokens_saved": 2_000_000,
                "tokens_processed": 10_000_000,  # rate = 20%
                "sessions": 10,
                "tracked_prunes": 200,            # 200/10 * 0.20 = 4.0 -> 1+4=5.0
                "prune_count": 200,
            })
            result = load_lifetime(p)
            self.assertIsNotNone(result)
            multiplier = result.get("session_multiplier_x")
            self.assertIsNotNone(multiplier, "reasonable multiplier must not be None")
            self.assertLessEqual(multiplier, 100_000)


# ── TestContextPctCap (T-1) ───────────────────────────────────────────────────

class TestContextPctCap(unittest.TestCase):
    """T-1: _context_pct must cap at 100.0 when after > window."""

    def test_after_greater_than_window_returns_100(self):
        """T-1: after=300000, window=200000 -> 100.0 (not 150.0)."""
        r = _context_pct({
            "tokens": {"after": 300000},
            "model": {"context_window": 200000},
        })
        self.assertEqual(r, 100.0,
                         f"_context_pct with after>window must return 100.0, got {r!r}")

    def test_after_equals_window_returns_100(self):
        """Edge: after==window -> exactly 100.0."""
        r = _context_pct({
            "tokens": {"after": 200000},
            "model": {"context_window": 200000},
        })
        self.assertEqual(r, 100.0)

    def test_after_below_window_uncapped(self):
        """Regression: normal case is not affected by the cap."""
        r = _context_pct({
            "tokens": {"after": 100000},
            "model": {"context_window": 200000},
        })
        self.assertEqual(r, 50.0)


# ── TestFmtIntBounded (T-2) ───────────────────────────────────────────────────

class TestFmtIntBounded(unittest.TestCase):
    """T-2: _fmt_int huge-negative must be bounded+signed; -1000 -> '-1,000'."""

    def test_huge_negative_bounded_with_sign(self):
        """T-2: _fmt_int(-10**400) must return '-1,000,000,000,000,000' (bounded, sign preserved)."""
        result = _fmt_int(-10 ** 400)
        self.assertEqual(
            result, "-1,000,000,000,000,000",
            f"_fmt_int(-10**400) must be '-1,000,000,000,000,000', got {result!r}",
        )

    def test_negative_one_thousand(self):
        """T-2: _fmt_int(-1000) must return '-1,000' (sign sanity)."""
        result = _fmt_int(-1000)
        self.assertEqual(result, "-1,000",
                         f"_fmt_int(-1000) must be '-1,000', got {result!r}")

    def test_positive_huge_bounded(self):
        """T-2: _fmt_int(10**400) must return '1,000,000,000,000,000'."""
        result = _fmt_int(10 ** 400)
        self.assertEqual(result, "1,000,000,000,000,000",
                         f"_fmt_int(10**400) must be '1,000,000,000,000,000', got {result!r}")


# ── TestMaxReceiptIntBoundary (T-3) ───────────────────────────────────────────

class TestMaxReceiptIntBoundary(unittest.TestCase):
    """T-3: _int boundary — exactly 10**15 passes, 10**15+1 returns 0."""

    def test_max_receipt_int_passes(self):
        """T-3: _int(10**15) == 10**15 (at-bound value is valid)."""
        self.assertEqual(_int(_MAX_RECEIPT_INT), _MAX_RECEIPT_INT,
                         f"_int(10**15) must return 10**15, got {_int(_MAX_RECEIPT_INT)!r}")

    def test_max_receipt_int_plus_one_returns_zero(self):
        """T-3: _int(10**15+1) == 0 (one over bound -> corruption sentinel -> 0)."""
        self.assertEqual(_int(_MAX_RECEIPT_INT + 1), 0,
                         f"_int(10**15+1) must return 0, got {_int(_MAX_RECEIPT_INT + 1)!r}")

    def test_num_or_zero_max_receipt_int_passes(self):
        """T-3: _num_or_zero(10**15) == 10**15 (at-bound valid)."""
        self.assertEqual(_num_or_zero(_MAX_RECEIPT_INT), _MAX_RECEIPT_INT)

    def test_num_or_zero_max_receipt_int_plus_one_returns_zero(self):
        """T-3: _num_or_zero(10**15+1) == 0 (one over bound -> 0)."""
        self.assertEqual(_num_or_zero(_MAX_RECEIPT_INT + 1), 0,
                         f"_num_or_zero(10**15+1) must return 0, got {_num_or_zero(_MAX_RECEIPT_INT+1)!r}")


if __name__ == "__main__":
    unittest.main()
