"""Graceful C3 fallback — aggressive that would wipe the conversation falls back
to the heaviest prescription that survives validation (instead of exit 5)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from cozempic.cli import _RX_LADDER, _fallback_order, run_prescription_with_fallback
from cozempic.safety import PruneValidationError


def _c3(rx):
    return PruneValidationError(
        f"conversation wiped under {rx}",
        evidence={"failed_check": "C3"},
    )


class TestFallbackOrder(unittest.TestCase):
    def test_order_is_heaviest_safe_first(self):
        self.assertEqual(_fallback_order("aggressive"), ["aggressive", "standard", "gentle"])
        self.assertEqual(_fallback_order("standard"), ["standard", "gentle"])
        self.assertEqual(_fallback_order("gentle"), ["gentle"])

    def test_custom_prescription_has_no_lighter_sibling(self):
        self.assertEqual(_fallback_order("my-custom"), ["my-custom"])

    def test_ladder_is_lightest_to_heaviest(self):
        self.assertEqual(_RX_LADDER, ["gentle", "standard", "aggressive"])


class TestRunWithFallback(unittest.TestCase):
    def _runner(self, wipes):
        """Return a fake run_prescription that raises C3 for strategy-sets whose
        prescription is in `wipes`, else returns a sentinel result."""
        from cozempic.registry import PRESCRIPTIONS
        names_to_rx = {tuple(v): k for k, v in PRESCRIPTIONS.items()}

        def fake(messages, strategy_names, config):
            rx = names_to_rx.get(tuple(strategy_names), "?")
            if rx in wipes:
                raise _c3(rx)
            return ([("ok", {}, 1)], [f"applied:{rx}"])
        return fake

    def test_falls_back_to_heaviest_safe(self):
        # aggressive + standard wipe, gentle survives -> apply gentle
        with patch("cozempic.cli.run_prescription", side_effect=self._runner({"aggressive", "standard"})):
            fb = run_prescription_with_fallback([], "aggressive", {})
        self.assertEqual(fb["applied_rx"], "gentle")
        self.assertTrue(fb["fell_back"])
        self.assertEqual(fb["requested_rx"], "aggressive")
        self.assertIsNone(fb["error"])
        self.assertEqual(fb["results"], ["applied:gentle"])

    def test_uses_requested_when_it_passes(self):
        with patch("cozempic.cli.run_prescription", side_effect=self._runner(set())):
            fb = run_prescription_with_fallback([], "aggressive", {})
        self.assertEqual(fb["applied_rx"], "aggressive")
        self.assertFalse(fb["fell_back"])

    def test_falls_back_one_step(self):
        # only aggressive wipes -> standard applied
        with patch("cozempic.cli.run_prescription", side_effect=self._runner({"aggressive"})):
            fb = run_prescription_with_fallback([], "aggressive", {})
        self.assertEqual(fb["applied_rx"], "standard")
        self.assertTrue(fb["fell_back"])

    def test_total_failure_when_even_gentle_wipes(self):
        with patch("cozempic.cli.run_prescription", side_effect=self._runner({"aggressive", "standard", "gentle"})):
            fb = run_prescription_with_fallback([], "aggressive", {})
        self.assertIsNone(fb["messages"])
        self.assertIsInstance(fb["error"], PruneValidationError)
        self.assertEqual(fb["error"].evidence["failed_check"], "C3")

    def test_strict_disables_fallback(self):
        with patch("cozempic.cli.run_prescription", side_effect=self._runner({"aggressive"})):
            fb = run_prescription_with_fallback([], "aggressive", {}, strict=True)
        self.assertIsNone(fb["messages"])      # no fallback attempted
        self.assertIsInstance(fb["error"], PruneValidationError)

    def test_gentle_request_no_fallback_available(self):
        with patch("cozempic.cli.run_prescription", side_effect=self._runner({"gentle"})):
            fb = run_prescription_with_fallback([], "gentle", {})
        self.assertIsNone(fb["messages"])  # nothing lighter than gentle
        self.assertIsInstance(fb["error"], PruneValidationError)


class TestCmdTreatIntegration(unittest.TestCase):
    """Drive cmd_treat's fallback path (dry-run) and assert the user-facing notice."""

    def _runner_aggressive_wipes(self):
        from cozempic.registry import PRESCRIPTIONS
        names_to_rx = {tuple(v): k for k, v in PRESCRIPTIONS.items()}

        def fake(messages, strategy_names, config):
            rx = names_to_rx.get(tuple(strategy_names), "?")
            if rx == "aggressive":
                raise _c3(rx)
            return (messages, [])  # standard succeeds, no-op result
        return fake

    def test_treat_falls_back_and_notifies(self):
        import argparse
        import contextlib
        import io
        from cozempic import cli

        # messages with a couple of real entries so downstream byte/token calc is fine
        msgs = [(0, {"type": "user", "message": {"role": "user", "content": "hi"}}, 30),
                (1, {"type": "assistant", "message": {"role": "assistant", "content": "yo"}}, 30)]
        args = argparse.Namespace(session="x", rx="aggressive", execute=False, project=None,
                                  thinking_mode=None, strict=False, protect_pattern=None)
        buf = io.StringIO()
        from pathlib import Path
        est = type("E", (), {"total": 100, "method": "exact", "model": "m",
                             "context_window": 200000, "confidence": "high"})()
        with patch("cozempic.cli.resolve_session", return_value=Path("/x/s.jsonl")), \
                patch("cozempic.cli.load_messages_and_snapshot", return_value=(msgs, object())), \
                patch("cozempic.cli.calibrate_ratio", return_value=0.5), \
                patch("cozempic.cli.run_prescription", side_effect=self._runner_aggressive_wipes()), \
                patch("cozempic.cli.estimate_session_tokens", return_value=est):
            with contextlib.redirect_stdout(buf):
                try:
                    cli.cmd_treat(args)
                except SystemExit:
                    pass
        out = buf.getvalue()
        self.assertIn("would have wiped the conversation", out)
        self.assertIn("applied the safe maximum 'standard'", out)


if __name__ == "__main__":
    unittest.main()
