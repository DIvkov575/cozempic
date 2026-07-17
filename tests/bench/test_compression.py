"""Tests for the Tier-1 offline compression benchmark."""

import json
from pathlib import Path

from cozempic.bench.compression import (
    benchmark_session, run_corpus, summarize, format_summary, as_json,
)

FIXT = Path(__file__).resolve().parents[1] / "fixtures" / "sessions"


def _fixtures():
    return sorted(FIXT.glob("*.jsonl"))


def test_benchmark_session_measures_all_prescriptions():
    paths = _fixtures()
    assert paths, "no session fixtures found"
    res = benchmark_session(paths[0])
    assert res is not None
    # gentle ⊂ standard ⊂ aggressive → reclaim is monotonic non-decreasing
    g = res.prescriptions["gentle"].tokens_reclaimed
    s = res.prescriptions["standard"].tokens_reclaimed
    a = res.prescriptions["aggressive"].tokens_reclaimed
    assert g <= s <= a
    # everything is a real, bounded measurement
    for m in res.prescriptions.values():
        assert m.final_tokens <= m.original_tokens
        assert 0.0 <= m.pct_reduction <= 100.0


def test_reclaim_is_safe_on_fixtures():
    """No fixture prune should be flagged unsafe (they're valid transcripts)."""
    _results, summary = run_corpus(_fixtures())
    for rx, unsafe in summary.unsafe_by_prescription.items():
        assert unsafe == 0, f"{rx} produced unsafe prunes on clean fixtures"


def test_summary_and_json_roundtrip():
    _results, summary = run_corpus(_fixtures())
    text = format_summary(summary)
    assert "Compression Benchmark" in text
    assert "gentle" in text
    payload = json.loads(as_json(summary))
    assert payload["sessions"] == summary.sessions
    assert set(payload["by_prescription"]) == {"gentle", "standard", "aggressive"}


def test_empty_and_missing_paths_are_skipped(tmp_path):
    missing = tmp_path / "nope.jsonl"
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert benchmark_session(missing) is None
    assert benchmark_session(empty) is None
