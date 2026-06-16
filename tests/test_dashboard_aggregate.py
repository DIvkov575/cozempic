"""Tests for the dashboard aggregation layer (dashboard/aggregate.py) — D2."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cozempic.dashboard.aggregate import aggregate, load_receipts
from cozempic.metrics import ClaudeMetricsAdapter, TriggerInfo, ValidationInfo, build_receipt
from cozempic.receipts import receipts_dir, write_receipt
from cozempic.types import PrescriptionResult, StrategyResult


def _receipt(*, outcome="committed", tokens=(2000, 1500), agent="claude", session="s1",
             ts="2026-06-16T09:00:00Z", strategies=(("tool-output-trim", 800, 1), ("thinking", 200, 1)),
             tier="standard", window=200000):
    srs = [StrategyResult(name, [], 1000, 1000 - b, n, 0, n, "") for name, b, n in strategies]
    before, after = tokens
    res = PrescriptionResult("standard", srs, 5000, 5000 - (before - after) * 2, 10,
                             10 - sum(n for _, _, n in strategies), before, after,
                             "exact", "claude-opus-4-8", window)

    class _A:
        name = agent
        schema_version = "1"

        def agent_version(self):
            return None

    return build_receipt(
        res, adapter=_A(), session_id=session,
        trigger=TriggerInfo("manual", tier, "standard"),
        outcome=outcome, ts=ts, receipt_id="r" + ts,
        validation=ValidationInfo(passed=outcome == "committed",
                                  deferred=outcome == "deferred"),
    )


class TestAggregate(unittest.TestCase):
    def test_empty(self):
        a = aggregate([])
        self.assertEqual(a["lifetime"]["prunes_total"], 0)
        self.assertEqual(a["lifetime"]["tokens_reclaimed"], 0)
        self.assertEqual(a["per_strategy"], [])
        self.assertEqual(a["lifetime"]["deferral_rate"], 0.0)

    def test_committed_only_counts_toward_savings(self):
        recs = [
            _receipt(outcome="committed", tokens=(2000, 1500), ts="2026-06-16T09:00:00Z"),
            _receipt(outcome="deferred", tokens=(2000, 1500), ts="2026-06-16T09:05:00Z"),
        ]
        a = aggregate(recs)
        self.assertEqual(a["lifetime"]["prunes_total"], 2)
        self.assertEqual(a["lifetime"]["committed"], 1)
        self.assertEqual(a["lifetime"]["deferred"], 1)
        self.assertEqual(a["lifetime"]["deferral_rate"], 0.5)
        # only the committed prune's 500 tokens count
        self.assertEqual(a["lifetime"]["tokens_reclaimed"], 500)

    def test_strategy_leaderboard_sorted_committed_only(self):
        a = aggregate([_receipt(outcome="committed")])
        ldr = a["per_strategy"]
        self.assertEqual(ldr[0]["id"], "tool-output-trim")  # 800-byte share -> more tokens
        self.assertGreater(ldr[0]["tokens_reclaimed"], ldr[1]["tokens_reclaimed"])
        # deferred prunes contribute nothing to the leaderboard
        a2 = aggregate([_receipt(outcome="deferred")])
        self.assertEqual(a2["per_strategy"], [])

    def test_multi_agent_grouping(self):
        recs = [
            _receipt(agent="claude", session="c1", tokens=(2000, 1500)),
            _receipt(agent="codex", session="x1", tokens=(3000, 1000)),
        ]
        a = aggregate(recs)
        by = {row["agent"]: row for row in a["per_agent"]}
        self.assertIn("claude", by)
        self.assertIn("codex", by)
        self.assertEqual(by["codex"]["tokens_reclaimed"], 2000)
        self.assertEqual(by["claude"]["tokens_reclaimed"], 500)

    def test_per_session_timeline_sorted_with_context_pct(self):
        recs = [
            _receipt(session="s1", ts="2026-06-16T09:05:00Z", tokens=(2000, 1500)),
            _receipt(session="s1", ts="2026-06-16T09:00:00Z", tokens=(3000, 1000)),
        ]
        a = aggregate(recs)
        sess = a["per_session"][0]
        self.assertEqual(sess["prunes"], 2)
        # timeline ascending by ts despite input order
        ts_order = [e["ts"] for e in sess["timeline"]]
        self.assertEqual(ts_order, sorted(ts_order))
        # context_pct = after/window*100 (1500/200000*100 = 0.8)
        self.assertAlmostEqual(sess["timeline"][1]["context_pct_after"], 0.8)

    def test_by_tier_counts_all_outcomes(self):
        recs = [_receipt(tier="gentle"), _receipt(tier="aggressive", outcome="deferred")]
        a = aggregate(recs)
        self.assertEqual(a["by_tier"], {"gentle": 1, "aggressive": 1})

    def test_none_tokens_safe(self):
        r = _receipt()
        r["tokens"]["reclaimed"] = None  # unknown
        a = aggregate([r])
        self.assertEqual(a["lifetime"]["tokens_reclaimed"], 0)


class TestRobustness(unittest.TestCase):
    def _odd(self, **over):
        r = {"outcome": "committed", "tokens": {"reclaimed": 100}, "bytes": {"reclaimed": 50},
             "session": {"id_hash": "sha256:x"}, "agent": {"name": "claude"},
             "trigger": {"tier": "standard"}, "model": {"context_window": 200000},
             "strategies": [], "ts": "2026-06-16T09:00:00Z"}
        r.update(over)
        return r

    def test_non_dict_nested_fields_do_not_crash(self):
        # each passes _is_receipt but has a non-dict nested field — must degrade, not raise
        for over in ({"session": None}, {"agent": "claude"}, {"trigger": 7}, {"model": None}):
            a = aggregate([self._odd(**over)])
            self.assertEqual(a["lifetime"]["committed"], 1)

    def test_non_list_strategies_does_not_crash(self):
        self.assertEqual(aggregate([self._odd(strategies=None)])["per_strategy"], [])

    def test_missing_ts_does_not_crash_outer_sort(self):
        recs = [
            self._odd(ts="2026-06-16T09:00:00Z", session={"id_hash": "a"}),
            {"outcome": "committed", "tokens": {}, "bytes": {},
             "session": {"id_hash": "b"}, "agent": {"name": "c"}},  # no ts at all
        ]
        self.assertEqual(aggregate(recs)["lifetime"]["prunes_total"], 2)  # no TypeError

    def test_aggregate_does_not_mutate_input(self):
        import copy

        recs = [_receipt(), _receipt(outcome="deferred")]
        snapshot = copy.deepcopy(recs)
        aggregate(recs)
        self.assertEqual(recs, snapshot)

    def test_mixed_agents_one_session_does_not_crash(self):
        recs = [_receipt(agent="claude", session="s"), _receipt(agent="codex", session="s")]
        sess = [s for s in aggregate(recs)["per_session"] if s["prunes"] == 2]
        self.assertEqual(len(sess), 1)
        self.assertIn(sess[0]["agent"], {"claude", "codex"})

    def test_leaderboard_many_strategies_sorted_desc(self):
        strategies = (("s1", 100, 1), ("s2", 400, 1), ("s3", 250, 1), ("s4", 50, 1))
        a = aggregate([_receipt(strategies=strategies)])
        toks = [s["tokens_reclaimed"] for s in a["per_strategy"]]
        self.assertEqual(toks, sorted(toks, reverse=True))
        self.assertEqual(a["per_strategy"][0]["id"], "s2")  # biggest byte share leads


class TestLoadReceipts(unittest.TestCase):
    def setUp(self):
        os.environ.pop("COZEMPIC_NO_RECEIPTS", None)

    def test_missing_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_receipts(Path(tmp) / "nope"), [])

    def test_loads_written_receipts_skips_index_and_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_receipt(_receipt(session="a"), base_dir=base)
            write_receipt(_receipt(session="b"), base_dir=base)
            # a torn line in a session file + a non-receipt json line
            d = receipts_dir(base)
            with open(d / "a.jsonl", "a") as f:
                f.write("{ broken json\n")
                f.write('{"hello":"world"}\n')  # valid json, not a receipt
            loaded = load_receipts(base)
            # 2 real receipts; garbage + index excluded
            self.assertEqual(len(loaded), 2)
            self.assertTrue(all("outcome" in r for r in loaded))

    def test_skips_json_array_and_scalar_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_receipt(_receipt(session="a"), base_dir=base)
            with open(receipts_dir(base) / "a.jsonl", "a") as f:
                f.write("[1,2,3]\n")
                f.write("42\n")
                f.write('"hello"\n')
            self.assertEqual(len(load_receipts(base)), 1)  # only the real receipt

    def test_load_then_aggregate_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            write_receipt(_receipt(outcome="committed", tokens=(2000, 1500)), base_dir=base)
            a = aggregate(load_receipts(base))
            self.assertEqual(a["lifetime"]["committed"], 1)
            self.assertEqual(a["lifetime"]["tokens_reclaimed"], 500)


if __name__ == "__main__":
    unittest.main()
