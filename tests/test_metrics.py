"""Tests for the prune-metrics contract (metrics.py) — D0 of the dashboard path."""

from __future__ import annotations

import json
import unittest

from cozempic.metrics import (
    SCHEMA_VERSION,
    ClaudeMetricsAdapter,
    MetricsAdapter,
    ProtectedInfo,
    TriggerInfo,
    ValidationInfo,
    build_receipt,
    hash_id,
    new_receipt_id,
    serialize_receipt,
    utc_now_iso,
    validate_receipt,
)
from cozempic.types import PruneAction, PrescriptionResult, StrategyResult


def _sample_result() -> PrescriptionResult:
    """A known prescription: two strategies, with bytes/tokens populated."""
    s1 = StrategyResult(
        strategy_name="tool-output-trim",
        actions=[PruneAction(3, "replace", "trim", 1000, 200, {"x": 1})],
        original_bytes=1000,
        pruned_bytes=200,  # reclaimed 800
        messages_affected=1,
        messages_removed=0,
        messages_replaced=1,
        summary="trimmed",
    )
    s2 = StrategyResult(
        strategy_name="thinking-blocks",
        actions=[PruneAction(5, "remove", "drop", 200, 0, None)],
        original_bytes=200,
        pruned_bytes=0,  # reclaimed 200
        messages_affected=1,
        messages_removed=1,
        messages_replaced=0,
        summary="dropped",
    )
    return PrescriptionResult(
        prescription_name="standard",
        strategy_results=[s1, s2],
        original_total_bytes=5000,
        final_total_bytes=4000,  # reclaimed 1000 bytes overall
        original_message_count=10,
        final_message_count=9,
        original_tokens=2000,
        final_tokens=1500,  # reclaimed 500 tokens
        token_method="exact",  # the value tokens.py actually emits (NOT "usage")
        model="claude-opus-4-8",
        context_window=200000,
    )


def _build(result=None, **kw):
    defaults = dict(
        adapter=ClaudeMetricsAdapter(),
        session_id="sess-abc",
        trigger=TriggerInfo("manual", "standard", "standard", "test run"),
        ts="2026-06-16T09:00:00Z",
        receipt_id="fixedid",
        tool_version="1.8.32",
    )
    defaults.update(kw)
    return build_receipt(result or _sample_result(), **defaults)


class TestBuildReceipt(unittest.TestCase):
    def test_top_level_shape_and_determinism(self):
        r = _build()
        validate_receipt(r)  # must not raise
        self.assertEqual(r["schema_version"], SCHEMA_VERSION)
        self.assertEqual(r["receipt_id"], "fixedid")
        self.assertEqual(r["ts"], "2026-06-16T09:00:00Z")
        self.assertEqual(r["tool"], {"name": "cozempic", "version": "1.8.32"})
        self.assertEqual(r["agent"]["name"], "claude")
        self.assertEqual(r["mode"], "edit_resume")
        self.assertEqual(r["outcome"], "committed")

    def test_bytes_rollup(self):
        b = _build()["bytes"]
        self.assertEqual(b, {"before": 5000, "after": 4000, "reclaimed": 1000})

    def test_token_rollup_and_pct(self):
        t = _build()["tokens"]
        self.assertEqual(t["before"], 2000)
        self.assertEqual(t["after"], 1500)
        self.assertEqual(t["reclaimed"], 500)
        self.assertEqual(t["reclaimed_pct"], 25.0)
        self.assertEqual(t["method"], "exact")  # live value
        self.assertEqual(t["confidence"], "high")  # "exact" maps to high, not "none"

    def test_entries_rollup(self):
        e = _build()["entries"]
        self.assertEqual(e, {"before": 10, "after": 9, "removed": 1, "replaced": 1})

    def test_per_strategy_token_apportionment_by_bytes(self):
        # strategy bytes reclaimed: 800 and 200 (total 1000); token reclaim 500
        # → 400 and 100 by byte share.
        strats = {s["id"]: s for s in _build()["strategies"]}
        self.assertEqual(strats["tool-output-trim"]["bytes_reclaimed"], 800)
        self.assertEqual(strats["tool-output-trim"]["tokens_reclaimed"], 400)
        self.assertEqual(strats["thinking-blocks"]["bytes_reclaimed"], 200)
        self.assertEqual(strats["thinking-blocks"]["tokens_reclaimed"], 100)

    def test_strategy_tier_lookup(self):
        r = _build(strategy_tiers={"tool-output-trim": "standard"})
        tiers = {s["id"]: s["tier"] for s in r["strategies"]}
        self.assertEqual(tiers["tool-output-trim"], "standard")
        self.assertEqual(tiers["thinking-blocks"], "unknown")  # unmapped → unknown

    def test_model_block(self):
        self.assertEqual(
            _build()["model"], {"name": "claude-opus-4-8", "context_window": 200000}
        )

    def test_none_tokens_safe(self):
        res = _sample_result()
        res.original_tokens = None
        res.final_tokens = None
        res.token_method = None
        t = _build(res)["tokens"]
        self.assertIsNone(t["before"])
        self.assertIsNone(t["reclaimed"])
        self.assertEqual(t["reclaimed_pct"], 0.0)
        self.assertEqual(t["method"], "unknown")
        self.assertEqual(t["confidence"], "none")

    def test_zero_token_reclaim_no_divzero(self):
        res = _sample_result()
        res.original_tokens = 0
        res.final_tokens = 0
        t = _build(res)["tokens"]
        self.assertEqual(t["reclaimed"], 0)
        self.assertEqual(t["reclaimed_pct"], 0.0)

    def test_deferred_outcome(self):
        r = _build(
            outcome="deferred",
            validation=ValidationInfo(
                passed=False, deferred=True, defer_reason="file changed",
                checks_run=["D1", "D2"],
            ),
        )
        validate_receipt(r)
        self.assertEqual(r["outcome"], "deferred")
        self.assertTrue(r["validation"]["deferred"])
        self.assertEqual(r["validation"]["defer_reason"], "file changed")

    def test_protected_block(self):
        r = _build(protected=ProtectedInfo(entries=3, reasons={"session_meta": 2, "compacted": 1}))
        self.assertEqual(r["protected"], {"entries": 3, "reasons": {"session_meta": 2, "compacted": 1}})

    def test_trigger_and_mode_passthrough(self):
        r = _build(trigger=TriggerInfo("guard", "aggressive", "aggressive", "soft tier"), mode="edit_resume")
        self.assertEqual(r["trigger"]["source"], "guard")
        self.assertEqual(r["trigger"]["tier"], "aggressive")


class _StubCodexAdapter:
    """Minimal non-Claude adapter — proves the contract is agent-agnostic."""

    name = "codex"
    schema_version = "2"

    def agent_version(self):
        return "0.139.0"

    def count_tokens(self, entries):
        from cozempic.metrics import TokenCount

        return TokenCount(0, "heuristic", "medium")

    def context_window(self, entries):
        return 400000

    def entry_bytes(self, entry):
        return 1


class TestAgentAgnostic(unittest.TestCase):
    def test_stub_adapter_satisfies_protocol(self):
        self.assertIsInstance(_StubCodexAdapter(), MetricsAdapter)

    def test_build_receipt_stamps_arbitrary_adapter(self):
        r = _build(adapter=_StubCodexAdapter())
        self.assertEqual(
            r["agent"], {"name": "codex", "version": "0.139.0", "adapter_schema_version": "2"}
        )
        validate_receipt(r)


class TestEdgeCases(unittest.TestCase):
    def test_empty_strategy_results_noop(self):
        res = PrescriptionResult("noop", [], 5000, 5000, 10, 10, 2000, 2000, "exact", "m", 200000)
        r = _build(res)
        validate_receipt(r)
        self.assertEqual(r["strategies"], [])
        self.assertEqual(r["bytes"]["reclaimed"], 0)
        self.assertEqual(r["entries"]["removed"], 0)
        self.assertEqual(r["tokens"]["reclaimed"], 0)

    def test_apportionment_sums_exactly_to_total(self):
        # two equal-byte strategies, 1 token reclaimed -> sums to 1 (no drift to 0)
        s1 = StrategyResult("a", [], 100, 50, 1, 0, 1, "")  # 50 bytes
        s2 = StrategyResult("b", [], 100, 50, 1, 0, 1, "")  # 50 bytes
        res = PrescriptionResult("p", [s1, s2], 1000, 900, 5, 5, 1000, 999, "exact", "m", 200000)
        r = _build(res)
        toks = sorted(s["tokens_reclaimed"] for s in r["strategies"])
        self.assertEqual(sum(toks), 1)  # exact, no rounding loss
        self.assertEqual(toks, [0, 1])

    def test_negative_reclaim_preserved(self):
        res = _sample_result()
        res.final_total_bytes = 6000  # grew
        res.original_tokens, res.final_tokens = 1500, 2000  # grew
        r = _build(res)
        self.assertEqual(r["bytes"]["reclaimed"], -1000)
        self.assertEqual(r["tokens"]["reclaimed"], -500)
        # negative reclaim -> per-strategy apportionment stays 0 (not garbage)
        self.assertTrue(all(s["tokens_reclaimed"] == 0 for s in r["strategies"]))

    def test_reason_capped(self):
        r = _build(trigger=TriggerInfo("manual", "standard", "standard", "x" * 500))
        self.assertLessEqual(len(r["trigger"]["reason"]), 200)

    def test_defer_reason_capped(self):
        r = _build(validation=ValidationInfo(passed=False, deferred=True, defer_reason="y" * 500))
        self.assertLessEqual(len(r["validation"]["defer_reason"]), 200)


class TestPrivacy(unittest.TestCase):
    def test_hash_id_prefix_and_length(self):
        h = hash_id("sess-abc")
        self.assertTrue(h.startswith("sha256:"))
        self.assertEqual(len(h), len("sha256:") + 12)

    def test_hash_id_stable(self):
        self.assertEqual(hash_id("x"), hash_id("x"))
        self.assertNotEqual(hash_id("x"), hash_id("y"))

    def test_hash_id_none_passthrough(self):
        self.assertIsNone(hash_id(None))

    def test_receipt_has_no_raw_identifiers(self):
        r = _build(session_id="sess-abc", transcript_path="/Users/me/.claude/x.jsonl", cwd="/Users/me/proj")
        blob = json.dumps(r)
        self.assertNotIn("sess-abc", blob)
        self.assertNotIn("/Users/me", blob)
        self.assertTrue(r["session"]["id_hash"].startswith("sha256:"))
        self.assertTrue(r["session"]["transcript_hash"].startswith("sha256:"))


class TestSerializationAndValidation(unittest.TestCase):
    def test_serialize_roundtrip(self):
        r = _build()
        line = serialize_receipt(r)
        self.assertNotIn("\n", line)
        self.assertEqual(json.loads(line), r)

    def test_validate_catches_missing_key(self):
        r = _build()
        del r["tokens"]
        with self.assertRaises(ValueError):
            validate_receipt(r)

    def test_validate_catches_bad_outcome(self):
        r = _build(outcome="committed")
        r["outcome"] = "exploded"
        with self.assertRaises(ValueError):
            validate_receipt(r)

    def test_validate_catches_bad_schema_version(self):
        r = _build()
        r["schema_version"] = "9.9"
        with self.assertRaises(ValueError):
            validate_receipt(r)

    def test_validate_catches_missing_bytes_reclaimed(self):
        r = _build()
        del r["bytes"]["reclaimed"]
        with self.assertRaises(ValueError):
            validate_receipt(r)

    def test_validate_catches_non_list_strategies(self):
        r = _build()
        r["strategies"] = {}
        with self.assertRaises(ValueError):
            validate_receipt(r)

    def test_validate_catches_bad_method(self):
        r = _build()
        r["tokens"]["method"] = "bogus"
        with self.assertRaises(ValueError):
            validate_receipt(r)

    def test_validate_catches_bad_mode_and_source(self):
        r = _build()
        r["mode"] = "telepathy"
        with self.assertRaises(ValueError):
            validate_receipt(r)

    def test_serialize_roundtrip_unicode(self):
        r = _build(trigger=TriggerInfo("manual", "standard", "standard", "résumé café 日本語"))
        line = serialize_receipt(r)
        self.assertIn("日本語", line)  # ensure_ascii=False keeps literal unicode
        self.assertEqual(json.loads(line), r)


class TestSeam(unittest.TestCase):
    def test_claude_adapter_satisfies_protocol(self):
        self.assertIsInstance(ClaudeMetricsAdapter(), MetricsAdapter)

    def test_claude_adapter_counts_and_sizes(self):
        a = ClaudeMetricsAdapter()
        msgs = [(0, {"type": "user", "message": {"role": "user", "content": "hi there"}}, 30)]
        tc = a.count_tokens(msgs)
        self.assertGreaterEqual(tc.total, 0)
        self.assertIn(tc.method, {"exact", "heuristic"})
        self.assertGreater(a.entry_bytes({"a": "b"}), 0)
        self.assertGreater(a.context_window(msgs), 0)


class TestHelpers(unittest.TestCase):
    def test_new_receipt_id_unique_hex(self):
        a, b = new_receipt_id(), new_receipt_id()
        self.assertNotEqual(a, b)
        int(a, 16)  # valid hex

    def test_utc_now_iso_z_suffix(self):
        ts = utc_now_iso()
        self.assertTrue(ts.endswith("Z"))
        self.assertNotIn("+00:00", ts)


if __name__ == "__main__":
    unittest.main()
