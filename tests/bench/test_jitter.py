"""Tests for the jitter curve-replay (reload-count) benchmark."""

import json
from pathlib import Path

from cozempic.bench.jitter import (
    Policy, replay_session, summarize_policy, _usage_total, _growth_curve,
)


def _write_session(path: Path, usage_totals: list[int]) -> None:
    """Write a fake transcript whose assistant turns report the given cumulative
    usage totals (split across the four components so the sum equals each value)."""
    lines = []
    for tot in usage_totals:
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"usage": {
                "input_tokens": tot, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 0}},
        }))
    path.write_text("\n".join(lines) + "\n")


def test_usage_total_sums_components():
    msg = {"message": {"usage": {
        "input_tokens": 100, "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 300, "output_tokens": 5}}}
    assert _usage_total(msg) == 425


def test_usage_total_none_for_zero_or_missing():
    assert _usage_total({"message": {"usage": {"input_tokens": 0}}}) is None
    assert _usage_total({"type": "user"}) is None


def test_growth_curve_reads_snapshots(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_session(p, [100_000, 200_000, 500_000])
    assert _growth_curve(p) == [100_000, 200_000, 500_000]


def test_no_reload_when_curve_stays_below_threshold(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_session(p, [100_000, 300_000, 600_000])   # never reaches 680K
    r = replay_session(p, Policy("p", reload_at=680_000, depth_target=350_000))
    assert r.reloads == 0
    assert r.peak_tokens == 600_000


def test_reload_fires_and_offsets_subsequent_levels(tmp_path):
    p = tmp_path / "s.jsonl"
    # crosses 680K once at 700K, then keeps growing in raw terms
    _write_session(p, [600_000, 700_000, 900_000])
    r = replay_session(p, Policy("p", reload_at=680_000, depth_target=350_000))
    # one reload at the 700K snapshot; 900K raw - 350K reclaimed = 550K effective < 680K
    assert r.reloads == 1
    assert r.peak_tokens == 700_000          # highest effective level seen
    assert r.post_prune_levels == [350_000]


def test_deeper_target_yields_fewer_reloads(tmp_path):
    p = tmp_path / "s.jsonl"
    # steady climb well past threshold multiple times
    _write_session(p, [680_000, 760_000, 840_000, 920_000, 1_000_000])
    deep = replay_session(p, Policy("d", reload_at=680_000, depth_target=350_000))
    shallow = replay_session(p, Policy("s", reload_at=680_000, depth_target=600_000))
    assert deep.reloads <= shallow.reloads
    assert deep.reloads >= 1


def test_summarize_counts_reloaders_and_over_700k(tmp_path):
    a = tmp_path / "a.jsonl"; _write_session(a, [700_000, 900_000])  # reloads
    b = tmp_path / "b.jsonl"; _write_session(b, [100_000, 200_000])  # never reloads
    pol = Policy("p", reload_at=680_000, depth_target=350_000)
    res = [replay_session(a, pol), replay_session(b, pol)]
    s = summarize_policy(pol, res)
    assert s.sessions_evaluated == 2
    assert s.sessions_that_reload == 1
    assert s.over_700k_sessions == 0   # effective peak held under 700K


def test_missing_or_curveless_session_is_none(tmp_path):
    empty = tmp_path / "e.jsonl"; empty.write_text("")
    assert replay_session(empty, Policy("p", 680_000, 350_000)) is None
