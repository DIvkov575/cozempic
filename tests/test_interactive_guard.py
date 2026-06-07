"""1.8.22 — interactive guard daemon helpers (E/F/H) + nudge arming.

Covers component H (_detect_interactive), F (_idle_backoff_cycles), E
(_force_reload_pct + _arm_nudge_from_result). The loop-level gating
(defer-mid-turn vs reload-at-idle) is exercised end-to-end by the safe-point
suite (guard_prune_cycle with auto_reload toggled) plus these helpers, which are
the only new branch inputs the loop reads.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestDetectInteractive(unittest.TestCase):
    def test_env_on_forces_true(self):
        from cozempic.guard import _detect_interactive
        with patch.dict("os.environ", {"COZEMPIC_INTERACTIVE": "on"}):
            self.assertTrue(_detect_interactive(None))
            self.assertTrue(_detect_interactive(12345))

    def test_env_off_forces_false(self):
        from cozempic.guard import _detect_interactive
        with patch.dict("os.environ", {"COZEMPIC_INTERACTIVE": "off"}):
            self.assertFalse(_detect_interactive(None))
            self.assertFalse(_detect_interactive(12345))

    def test_auto_no_pid_defaults_interactive(self):
        from cozempic.guard import _detect_interactive
        with patch.dict("os.environ", {"COZEMPIC_INTERACTIVE": "auto"}):
            self.assertTrue(_detect_interactive(None))

    def test_auto_with_tty_is_interactive(self):
        from cozempic import guard
        with patch.dict("os.environ", {"COZEMPIC_INTERACTIVE": "auto"}), \
             patch("cozempic.guard.subprocess.run",
                   return_value=MagicMock(stdout="ttys001\n")):
            self.assertTrue(guard._detect_interactive(4242))

    def test_auto_no_tty_is_headless(self):
        from cozempic import guard
        for ttyval in ("??", "?", "-", ""):
            with patch.dict("os.environ", {"COZEMPIC_INTERACTIVE": "auto"}), \
                 patch("cozempic.guard.subprocess.run",
                       return_value=MagicMock(stdout=ttyval + "\n")):
                self.assertFalse(guard._detect_interactive(4242), ttyval)

    def test_auto_ps_failure_defaults_interactive(self):
        from cozempic import guard
        with patch.dict("os.environ", {"COZEMPIC_INTERACTIVE": "auto"}), \
             patch("cozempic.guard.subprocess.run", side_effect=OSError):
            self.assertTrue(guard._detect_interactive(4242))


class TestIdleBackoffCycles(unittest.TestCase):
    def test_default_is_four(self):
        from cozempic.guard import _idle_backoff_cycles
        with patch.dict("os.environ", {}, clear=False) as _:
            import os
            os.environ.pop("COZEMPIC_IDLE_BACKOFF_CYCLES", None)
            self.assertEqual(_idle_backoff_cycles(), 4)

    def test_env_override(self):
        from cozempic.guard import _idle_backoff_cycles
        with patch.dict("os.environ", {"COZEMPIC_IDLE_BACKOFF_CYCLES": "10"}):
            self.assertEqual(_idle_backoff_cycles(), 10)

    def test_zero_disables(self):
        from cozempic.guard import _idle_backoff_cycles
        with patch.dict("os.environ", {"COZEMPIC_IDLE_BACKOFF_CYCLES": "0"}):
            self.assertEqual(_idle_backoff_cycles(), 0)

    def test_garbage_falls_back(self):
        from cozempic.guard import _idle_backoff_cycles
        with patch.dict("os.environ", {"COZEMPIC_IDLE_BACKOFF_CYCLES": "nope"}):
            self.assertEqual(_idle_backoff_cycles(), 4)


class TestForceReloadPct(unittest.TestCase):
    def test_default(self):
        from cozempic.guard import _force_reload_pct
        import os
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("COZEMPIC_FORCE_RELOAD_PCT", None)
            self.assertAlmostEqual(_force_reload_pct(), 0.88)

    def test_env_override(self):
        from cozempic.guard import _force_reload_pct
        with patch.dict("os.environ", {"COZEMPIC_FORCE_RELOAD_PCT": "0.95"}):
            self.assertAlmostEqual(_force_reload_pct(), 0.95)

    def test_out_of_range_disables(self):
        from cozempic.guard import _force_reload_pct
        for bad in ("0", "-1", "1.5", "2"):
            with patch.dict("os.environ", {"COZEMPIC_FORCE_RELOAD_PCT": bad}):
                self.assertEqual(_force_reload_pct(), 0.0, bad)

    def test_garbage_falls_back(self):
        from cozempic.guard import _force_reload_pct
        with patch.dict("os.environ", {"COZEMPIC_FORCE_RELOAD_PCT": "x"}):
            self.assertAlmostEqual(_force_reload_pct(), 0.88)


class TestArmNudgeFromResult(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="cozempic_arm_"))

    def tearDown(self):
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_arms_with_real_projected_pct(self):
        from cozempic import guard
        with patch("cozempic.guard._guard_tmp_root", return_value=self.scratch):
            guard._arm_nudge_from_result(
                "sess-arm-1", None, 55,
                {"original_tokens": 100_000, "final_tokens": 60_000},
            )
            armed = guard.read_armed("sess-arm-1", None)
        self.assertIsNotNone(armed)
        self.assertEqual(armed["tier"], 55)
        self.assertAlmostEqual(armed["projected_pct"], 40.0, places=1)

    def test_zero_reduction_arms_with_zero(self):
        from cozempic import guard
        with patch("cozempic.guard._guard_tmp_root", return_value=self.scratch):
            guard._arm_nudge_from_result(
                "sess-arm-2", None, 80,
                {"original_tokens": 100_000, "final_tokens": 100_000},
            )
            armed = guard.read_armed("sess-arm-2", None)
        self.assertEqual(armed["projected_pct"], 0.0)

    def test_missing_keys_does_not_raise(self):
        from cozempic import guard
        with patch("cozempic.guard._guard_tmp_root", return_value=self.scratch):
            guard._arm_nudge_from_result("sess-arm-3", None, 55, {})  # must not raise
            armed = guard.read_armed("sess-arm-3", None)
        self.assertEqual(armed["projected_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
