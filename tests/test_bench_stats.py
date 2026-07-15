"""N-run statistics for benchmark metrics — distributions + CIs, not point estimates.

Any metric on a (possibly non-deterministic) pipeline must be reported as a
distribution: mean, and a bootstrap/normal confidence interval, over N trials.
"""

from __future__ import annotations

import pytest


def test_summary_reports_mean_and_ci():
    from cozempic.bench.stats import summarize

    s = summarize([1.0, 1.0, 1.0, 1.0])
    assert s["mean"] == 1.0
    assert s["n"] == 4
    assert s["ci_low"] == pytest.approx(1.0)
    assert s["ci_high"] == pytest.approx(1.0)


def test_ci_widens_with_variance():
    from cozempic.bench.stats import summarize

    tight = summarize([0.5, 0.5, 0.5, 0.5, 0.5])
    wide = summarize([0.0, 1.0, 0.0, 1.0, 0.5])
    assert (wide["ci_high"] - wide["ci_low"]) > (tight["ci_high"] - tight["ci_low"])


def test_mean_matches_arithmetic_mean():
    from cozempic.bench.stats import summarize

    s = summarize([0.0, 1.0])  # 1/20-style flip
    assert s["mean"] == pytest.approx(0.5)


def test_run_trials_collects_n_samples():
    from cozempic.bench.stats import run_trials

    calls = {"n": 0}

    def sample():
        calls["n"] += 1
        return float(calls["n"] % 2)  # alternating 1,0,1,0...

    result = run_trials(sample, n=10)
    assert result["n"] == 10
    assert calls["n"] == 10
    assert 0.0 <= result["mean"] <= 1.0


def test_empty_is_safe():
    from cozempic.bench.stats import summarize

    s = summarize([])
    assert s["n"] == 0
    assert s["mean"] == 0.0
