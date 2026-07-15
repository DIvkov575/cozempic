"""N-run statistics for benchmark metrics (stdlib-only).

A single run of a metric on a pipeline with any non-determinism is not valid
evidence. These helpers turn a sampler into a distribution: mean plus a 95%
normal confidence interval on the mean. Use ``run_trials`` to sample a metric N
times and summarize.
"""

from __future__ import annotations

import math
from typing import Callable

# 95% two-sided normal critical value.
_Z95 = 1.959963984540054


def summarize(samples: list[float]) -> dict:
    """Mean + 95% CI on the mean for a list of samples."""
    n = len(samples)
    if n == 0:
        return {"n": 0, "mean": 0.0, "ci_low": 0.0, "ci_high": 0.0, "stdev": 0.0}
    mean = sum(samples) / n
    if n == 1:
        return {"n": 1, "mean": mean, "ci_low": mean, "ci_high": mean, "stdev": 0.0}
    var = sum((x - mean) ** 2 for x in samples) / (n - 1)
    stdev = math.sqrt(var)
    half = _Z95 * stdev / math.sqrt(n)
    return {"n": n, "mean": mean, "ci_low": mean - half, "ci_high": mean + half,
            "stdev": stdev}


def run_trials(sample: Callable[[], float], n: int = 20) -> dict:
    """Call ``sample`` ``n`` times and summarize the results."""
    return summarize([float(sample()) for _ in range(n)])
