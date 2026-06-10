"""RCA + regression for the f641174c reload-loop (2026-06-10).

The guard reload-looped 202x on a 776K-token session it could not prune below the
55% hard threshold, hanging the user's `claude --resume`. Root cause: a READ-ONLY
live prune sets ``live_write_skipped``, which the futile-loop circuit breaker
treated as a benign "busy but prunable" skip and never counted toward K-exit — even
when the COMPUTED prune was itself futile (776,558 -> ~773,700 tokens, 0.4% < the
10% _MIN_PRUNE_RATIO). So a fundamentally UNPRUNABLE live session looped forever
instead of K-exiting and backing off.

Ground truth from the live guard log:
    HARD THRESHOLD (55%): 776,558 tokens >= 550,000 (55%)
    Standard prune + reload (cycle #N)... Pruned: 0 tokens freed (0.0%)
    ... x202, no K-exit, no back-off.

The fix: ``_hard_prune_counts_as_futile`` counts a live skip toward K-exit when the
COMPUTED prune barely helps, while a live skip whose computed prune WOULD help (a
busy-but-prunable session) stays benign so a long agent run can't trip the K-exit.
"""

import unittest

from cozempic.guard import _hard_prune_counts_as_futile, _MIN_PRUNE_RATIO


class TestUnprunableLiveLoopF641174c(unittest.TestCase):
    def test_unprunable_live_skip_counts_toward_kexit(self):
        # The exact f641174c shape: read-only live skip, computed prune frees ~0% of
        # the 2.95MB session (byte signal — always present on the read-only return).
        r = {"live_write_skipped": True, "would_free_mb": 0.024, "original_bytes": 2_950_000}
        self.assertTrue(
            _hard_prune_counts_as_futile(r),
            "an UNPRUNABLE live session must count toward K-exit (the 202-cycle loop)")

    def test_negative_byte_reduction_counts(self):
        # The REAL f641174c case: pruning GROWS the file (team-recovery injection) —
        # would_free is negative → definitely futile.
        r = {"live_write_skipped": True, "would_free_mb": -0.0045, "original_bytes": 2_950_810}
        self.assertTrue(_hard_prune_counts_as_futile(r))

    def test_busy_but_prunable_live_skip_stays_benign(self):
        # Live, but the computed prune WOULD free ~35% — benign, must NOT count
        # (a long agent run can't be allowed to trip the K-exit).
        r = {"live_write_skipped": True, "would_free_mb": 1.0, "original_bytes": 2_950_000}
        self.assertFalse(_hard_prune_counts_as_futile(r))

    def test_token_projection_fallback(self):
        # When byte data is absent (older return / project=True path), fall back to
        # the token projection: 776K -> 773K (0.4%) is futile.
        r = {"live_write_skipped": True, "final_tokens": 776558, "projected_final_tokens": 773700}
        self.assertTrue(_hard_prune_counts_as_futile(r))
        r2 = {"live_write_skipped": True, "final_tokens": 600000, "projected_final_tokens": 400000}
        self.assertFalse(_hard_prune_counts_as_futile(r2))

    def test_live_skip_without_any_signal_stays_benign(self):
        # No byte or token data → can't judge → benign (never K-exit on unknown).
        self.assertFalse(_hard_prune_counts_as_futile({"live_write_skipped": True}))

    def test_existing_futile_signals_still_count(self):
        self.assertTrue(_hard_prune_counts_as_futile({"futile_reload_skipped": True}))
        self.assertTrue(_hard_prune_counts_as_futile({"prune_deferred_conflict": True}))
        self.assertTrue(_hard_prune_counts_as_futile({"saved_mb": 0}))
        self.assertTrue(_hard_prune_counts_as_futile({"saved_mb": -0.5}))

    def test_successful_prune_does_not_count(self):
        self.assertFalse(_hard_prune_counts_as_futile({"saved_mb": 1.5, "final_tokens": 500000}))

    def test_min_ratio_boundary(self):
        pre = 1000
        proj_exactly = int(round(pre * (1 - _MIN_PRUNE_RATIO)))  # exactly the ratio
        self.assertFalse(
            _hard_prune_counts_as_futile(
                {"live_write_skipped": True, "final_tokens": pre, "projected_final_tokens": proj_exactly}),
            "exactly _MIN_PRUNE_RATIO reduction is NOT futile")
        self.assertTrue(
            _hard_prune_counts_as_futile(
                {"live_write_skipped": True, "final_tokens": pre, "projected_final_tokens": proj_exactly + 1}),
            "just below _MIN_PRUNE_RATIO IS futile")

    def test_deferred_conflict_live_skip_counts(self):
        # A deferred-conflict that also flags live_write_skipped must still count
        # (the original PilotCC class — a deferred-writer FAILURE, not a benign skip).
        r = {"live_write_skipped": True, "prune_deferred_conflict": True,
             "final_tokens": 776558, "projected_final_tokens": 1}
        self.assertTrue(_hard_prune_counts_as_futile(r))


if __name__ == "__main__":
    unittest.main()
