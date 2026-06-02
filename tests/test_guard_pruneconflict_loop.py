"""Regression tests for the guard daemon's frozen reload-loop on a sustained
deferred prune-write FAILURE (the #102 / qa-edge "frozen reload-loop").

Root cause being guarded against:

  R1 — ``live_write_skipped`` is overloaded. It is set both for the benign
       #106 read-only deferral (agents active at 55%) AND for a *failed*
       deferred write (PruneConflictError / PruneLockError / OSError after the
       post-kill terminate). The HARD1 handler bypassed the circuit breaker on
       ``live_write_skipped`` to protect the benign agents-active path — but a
       repeatedly-failing deferred write took that same bypass, so
       ``consecutive_empty_hard_prunes`` never incremented → no backoff, no
       exit → the daemon spun forever at the fixed 30s interval.

  R2 — the HARD2 (80%) emergency tier had NO circuit breaker at all. A
       sustained deferred-conflict / futile prune at 80% spun forever
       (kill → no-write → resume → kill …).

The fix introduces ``result["prune_deferred_conflict"]`` (set only on a real
deferred-write failure, NOT on the benign read-only deferral) and routes both
HARD1 and HARD2 through a shared ``_account_hard_prune`` breaker.

Mirrors the harness in ``test_guard_hard_loop_backoff.py``.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _stub_session(tmpdir: Path, session_id: str):
    path = tmpdir / "fake_session.jsonl"
    path.write_text('{"type":"user","message":{"content":"hi"}}\n')
    return {"session_id": session_id, "path": path}


class _StopAfterNSleeps(Exception):
    """Sentinel to break out of the guard loop deterministically."""


class _FakeState:
    subagents = []  # type: ignore[var-annotated]
    tasks = []  # type: ignore[var-annotated]
    message_count = 0

    def is_empty(self) -> bool:
        return True


class _GuardLoopHarness(unittest.TestCase):
    """Drives ``start_guard`` with a sequence of canned prune results so the
    daemon-loop circuit-breaker accounting can be observed without real I/O."""

    def _run_loop(
        self,
        prune_results,
        token_estimate=600_000,
        interval=30,
        threshold_tokens=500_000,
        extra_cap=4,
    ):
        """``prune_results`` is an iterable of dicts merged into the canned
        result returned by ``guard_prune_cycle`` each cycle. After the sequence
        is exhausted the last entry repeats. The loop is forced to stop via
        ``_StopAfterNSleeps`` once ``max_sleeps`` sleeps have occurred so the
        test terminates even if the breaker never exits (the bug)."""
        from cozempic import guard as guard_mod

        tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, ignore_errors=True))
        session = _stub_session(tmpdir, "cafe1234-5678-9abc-def0-2026060299bb")

        sleep_calls: list[float] = []
        # Each cycle does 1 baseline sleep + up to 1 back-off sleep.
        max_sleeps = (len(prune_results) * 2) + extra_cap

        def fake_sleep(duration):
            sleep_calls.append(float(duration))
            if len(sleep_calls) >= max_sleeps:
                raise _StopAfterNSleeps()

        results_iter = iter(prune_results)
        last_overlay = {"saved_mb": 0.0}

        def fake_prune_cycle(**kwargs):
            nonlocal last_overlay
            try:
                last_overlay = next(results_iter)
            except StopIteration:
                pass  # repeat the final overlay forever
            base = {
                "saved_mb": 0.0,
                "original_tokens": 600_000,
                "final_tokens": 600_000,
                "team_name": None,
                "team_messages": 0,
                "checkpoint_path": None,
                "backup_path": None,
                "reloading": False,
            }
            base.update(last_overlay)
            return base

        with (
            patch.object(guard_mod.time, "sleep", side_effect=fake_sleep),
            patch.object(guard_mod, "_resolve_session_by_id", return_value=session),
            patch.object(guard_mod, "find_current_session", return_value=session),
            patch.object(guard_mod, "find_claude_pid", return_value=None),
            patch.object(guard_mod, "checkpoint_team", return_value=_FakeState()),
            patch.object(guard_mod, "guard_prune_cycle", side_effect=fake_prune_cycle),
            patch.object(
                guard_mod, "quick_token_estimate", return_value=token_estimate
            ),
            patch.object(guard_mod, "load_messages", return_value=[]),
            patch("cozempic.session.record_session"),
            patch.object(guard_mod, "_cleanup_stale_watchers"),
            patch.object(guard_mod, "ping_install_if_new"),
            patch.object(guard_mod, "maybe_auto_update"),
            patch.object(guard_mod, "cleanup_old_backups"),
            patch("cozempic.tokens.detect_context_window", return_value=1_000_000),
        ):
            captured = io.StringIO()
            with patch.object(sys, "stdout", captured):
                try:
                    guard_mod.start_guard(
                        cwd=str(tmpdir),
                        threshold_mb=100.0,
                        soft_threshold_mb=50.0,
                        rx_name="standard",
                        interval=interval,
                        auto_reload=False,
                        reactive=False,
                        threshold_tokens=threshold_tokens,
                        soft_threshold_tokens=250_000,
                        session_id=session["session_id"],
                    )
                    raised = None
                    exit_code = None
                except _StopAfterNSleeps as e:
                    raised = e
                    exit_code = None
                except SystemExit as e:
                    raised = None
                    exit_code = e.code

            return {
                "sleeps": sleep_calls,
                "raised": raised,
                "exit_code": exit_code,
                "stdout": captured.getvalue(),
            }


class TestPruneDeferredConflictBreaker(_GuardLoopHarness):
    """(a) Core regression — a sustained deferred-write FAILURE must trip the
    breaker (backoff + eventual exit), NOT spin forever at fixed interval."""

    def test_deferred_conflict_loop_exits(self):
        """A run where every cycle's deferred write fails
        (``prune_deferred_conflict=True``, ``saved_mb=0``, and the overloaded
        ``live_write_skipped=True``) must increment K and eventually
        ``sys.exit(0)`` — it must NOT loop forever."""
        from cozempic.guard import HARD_LOOP_EXIT_THRESHOLD

        failure = {
            "saved_mb": 0.0,
            "live_write_skipped": True,
            "prune_deferred_conflict": True,
        }
        # Enough cycles to cross the exit threshold with headroom.
        result = self._run_loop([failure] * (HARD_LOOP_EXIT_THRESHOLD + 5))

        self.assertEqual(
            result["exit_code"],
            0,
            "Sustained deferred-write failure did NOT exit — the daemon is "
            "stuck in the frozen reload-loop. "
            f"exit_code={result['exit_code']!r}, raised={result['raised']!r}.",
        )
        self.assertIn(
            "powerless against live-context dominance",
            result["stdout"],
            "Breaker exit diagnostic not printed.",
        )

    def test_deferred_conflict_engages_backoff_not_fixed_interval(self):
        """qa-edge reproducer: across many failing cycles the sleeps must NOT
        all be exactly the fixed interval — backoff must kick in. (The frozen
        loop bug fired at a flat 30s forever.)"""
        failure = {
            "saved_mb": 0.0,
            "live_write_skipped": True,
            "prune_deferred_conflict": True,
        }
        # Cap sleeps high so we observe ~30 cycles' worth before the harness
        # would stop — but the breaker should exit well before that.
        result = self._run_loop([failure] * 30, extra_cap=30)

        # It must have exited (breaker fired) rather than running to the cap.
        self.assertEqual(
            result["exit_code"], 0,
            "30 failing cycles did not exit — frozen loop regression.",
        )
        # And among the sleeps recorded, at least one must exceed the fixed
        # interval (proof the exponential backoff engaged).
        self.assertTrue(
            any(s > 30 for s in result["sleeps"]),
            f"No backoff sleep > interval observed; sleeps={result['sleeps']}. "
            "The loop ran at a flat fixed interval (frozen-loop signature).",
        )


class TestBenignReadOnlySkipDoesNotTripBreaker(_GuardLoopHarness):
    """(b) No-regression — the BENIGN #106 read-only deferral
    (``live_write_skipped=True`` WITHOUT ``prune_deferred_conflict``) must NOT
    increment the breaker, so a long agents-active run never trips the K-exit."""

    def test_benign_live_write_skipped_does_not_exit(self):
        benign = {
            "saved_mb": 0.0,
            "live_write_skipped": True,
            # NOTE: no prune_deferred_conflict — this is the benign path.
        }
        # Far more cycles than the exit threshold; must NOT exit.
        result = self._run_loop([benign] * 25, extra_cap=6)

        self.assertIsNone(
            result["exit_code"],
            "Benign read-only deferral tripped the breaker and exited — "
            f"regression to the agents-active path. exit_code={result['exit_code']}.",
        )
        self.assertIsInstance(
            result["raised"],
            _StopAfterNSleeps,
            "Expected the loop to run to the sleep cap (no breaker activity).",
        )
        self.assertNotIn(
            "powerless against live-context dominance",
            result["stdout"],
            "Benign path emitted the powerless diagnostic — breaker wrongly tripped.",
        )
        # All sleeps stayed at the fixed interval (no backoff engaged).
        self.assertTrue(
            all(s == 30 for s in result["sleeps"]),
            f"Benign path engaged backoff; sleeps={result['sleeps']}.",
        )


class TestHard2TierGetsBreaker(_GuardLoopHarness):
    """(c) HARD2 (80%) now also breaks/exits on sustained failure. Previously
    the emergency tier had no breaker and spun forever."""

    def test_hard2_deferred_conflict_loop_exits(self):
        from cozempic.guard import HARD_LOOP_EXIT_THRESHOLD

        failure = {
            "saved_mb": 0.0,
            "live_write_skipped": True,
            "prune_deferred_conflict": True,
        }
        # token_estimate >= 800_000 (0.80 * 1_000_000 window) routes through the
        # HARD2 emergency tier instead of HARD1.
        result = self._run_loop(
            [failure] * (HARD_LOOP_EXIT_THRESHOLD + 5),
            token_estimate=850_000,
        )

        self.assertEqual(
            result["exit_code"],
            0,
            "HARD2 (80%) sustained deferred-write failure did NOT exit — the "
            "emergency tier still has no circuit breaker. "
            f"exit_code={result['exit_code']!r}, raised={result['raised']!r}.",
        )
        self.assertIn(
            "powerless against live-context dominance",
            result["stdout"],
            "HARD2 breaker exit diagnostic not printed.",
        )


if __name__ == "__main__":
    unittest.main()
