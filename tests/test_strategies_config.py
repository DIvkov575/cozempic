"""Tests for strategies._config config-validation helpers + integration with strategies."""

from __future__ import annotations

import unittest

from cozempic.strategies._config import (
    coerce_choice,
    coerce_non_negative_int,
    coerce_ordered_pair,
    ConfigError,
)


class TestCoerceNonNegativeInt(unittest.TestCase):
    """Validation helper for integer-typed options like `tool_output_max_bytes`."""

    def test_returns_default_when_key_absent(self):
        result = coerce_non_negative_int({}, "max_bytes", default=8192)
        self.assertEqual(result, 8192)

    def test_returns_value_when_valid_int(self):
        result = coerce_non_negative_int({"max_bytes": 4096}, "max_bytes", default=8192)
        self.assertEqual(result, 4096)

    def test_accepts_zero(self):
        result = coerce_non_negative_int({"max_bytes": 0}, "max_bytes", default=10)
        self.assertEqual(result, 0)

    def test_rejects_negative(self):
        with self.assertRaises(ConfigError) as ctx:
            coerce_non_negative_int({"max_bytes": -1}, "max_bytes", default=10)
        self.assertIn("max_bytes", str(ctx.exception))
        self.assertIn("non-negative", str(ctx.exception))

    def test_rejects_string(self):
        with self.assertRaises(ConfigError) as ctx:
            coerce_non_negative_int({"max_bytes": "4096"}, "max_bytes", default=10)
        self.assertIn("max_bytes", str(ctx.exception))
        self.assertIn("int", str(ctx.exception))

    def test_rejects_float(self):
        """4096.5 is not a valid integer — reject to surface config typos early."""
        with self.assertRaises(ConfigError):
            coerce_non_negative_int({"max_bytes": 4096.5}, "max_bytes", default=10)

    def test_rejects_bool(self):
        """Python-ism: True is an int. We reject bool to prevent accidental
        YAML `max_bytes: yes` (parsed as True) from being treated as max_bytes=1."""
        with self.assertRaises(ConfigError):
            coerce_non_negative_int({"max_bytes": True}, "max_bytes", default=10)


class TestCoerceChoice(unittest.TestCase):
    """Validation helper for string-enum options like `thinking_mode`."""

    _CHOICES = ("remove", "truncate", "signature-only")

    def test_returns_default_when_key_absent(self):
        result = coerce_choice({}, "mode", self._CHOICES, default="remove")
        self.assertEqual(result, "remove")

    def test_returns_value_when_in_choices(self):
        result = coerce_choice({"mode": "truncate"}, "mode", self._CHOICES, default="remove")
        self.assertEqual(result, "truncate")

    def test_rejects_value_not_in_choices(self):
        with self.assertRaises(ConfigError) as ctx:
            coerce_choice({"mode": "bogus"}, "mode", self._CHOICES, default="remove")
        msg = str(ctx.exception)
        self.assertIn("mode", msg)
        self.assertIn("bogus", msg)
        # Error should list the valid choices so user can self-correct
        self.assertIn("remove", msg)
        self.assertIn("truncate", msg)

    def test_rejects_non_string(self):
        with self.assertRaises(ConfigError):
            coerce_choice({"mode": 42}, "mode", self._CHOICES, default="remove")


class TestCoerceOrderedPair(unittest.TestCase):
    """Helper enforcing `min < max` invariants on related options
    (e.g. `tool_result_mid_age` must be strictly less than `tool_result_old_age`)."""

    def test_returns_defaults_when_both_absent(self):
        lo, hi = coerce_ordered_pair({}, "mid", "old", defaults=(15, 40))
        self.assertEqual((lo, hi), (15, 40))

    def test_returns_values_when_valid(self):
        lo, hi = coerce_ordered_pair({"mid": 10, "old": 30}, "mid", "old", defaults=(15, 40))
        self.assertEqual((lo, hi), (10, 30))

    def test_rejects_inverted(self):
        """The canonical bug: user swaps values by mistake."""
        with self.assertRaises(ConfigError) as ctx:
            coerce_ordered_pair({"mid": 50, "old": 30}, "mid", "old", defaults=(15, 40))
        msg = str(ctx.exception)
        self.assertIn("mid", msg)
        self.assertIn("old", msg)
        self.assertIn("50", msg)
        self.assertIn("30", msg)

    def test_rejects_equal(self):
        """Edge: equal values collapse the mid-age tier to nothing."""
        with self.assertRaises(ConfigError):
            coerce_ordered_pair({"mid": 20, "old": 20}, "mid", "old", defaults=(15, 40))

    def test_rejects_negative(self):
        with self.assertRaises(ConfigError):
            coerce_ordered_pair({"mid": -1, "old": 10}, "mid", "old", defaults=(15, 40))

    def test_rejects_non_int(self):
        with self.assertRaises(ConfigError):
            coerce_ordered_pair({"mid": "15", "old": 40}, "mid", "old", defaults=(15, 40))


# ── Integration: strategies must raise ConfigError on bad input, not TypeError ──

class TestStrategyConfigIntegration(unittest.TestCase):
    """Each strategy using config must surface a helpful ConfigError on bad
    values rather than a cryptic TypeError or silent no-op that destroys data."""

    _SINGLE_USER_MSG = [(0, {"type": "user", "message": {"content": "hi"}}, 100)]

    def _run(self, strategy_name: str, config: dict):
        from cozempic.registry import STRATEGIES
        import cozempic.strategies  # noqa: F401 — ensure registry populated
        return STRATEGIES[strategy_name].func(self._SINGLE_USER_MSG, config)

    # tool-output-trim
    def test_tool_output_trim_rejects_string_max_bytes(self):
        with self.assertRaises(ConfigError):
            self._run("tool-output-trim", {"tool_output_max_bytes": "8192"})

    def test_tool_output_trim_rejects_negative_max_bytes(self):
        with self.assertRaises(ConfigError):
            self._run("tool-output-trim", {"tool_output_max_bytes": -1})

    # tool-result-age
    def test_tool_result_age_rejects_inverted_ages(self):
        with self.assertRaises(ConfigError):
            self._run("tool-result-age", {"tool_result_mid_age": 50, "tool_result_old_age": 30})

    def test_tool_result_age_rejects_string_mid_age(self):
        with self.assertRaises(ConfigError):
            self._run("tool-result-age", {"tool_result_mid_age": "15", "tool_result_old_age": 40})

    def test_tool_result_age_rejects_negative_values(self):
        with self.assertRaises(ConfigError):
            self._run("tool-result-age", {"tool_result_mid_age": -5, "tool_result_old_age": -1})

    # thinking-blocks
    def test_thinking_blocks_rejects_unknown_mode(self):
        with self.assertRaises(ConfigError):
            self._run("thinking-blocks", {"thinking_mode": "bogus-mode"})

    def test_thinking_blocks_rejects_non_string_mode(self):
        with self.assertRaises(ConfigError):
            self._run("thinking-blocks", {"thinking_mode": 42})

    # document-dedup
    def test_document_dedup_rejects_string_min_bytes(self):
        with self.assertRaises(ConfigError):
            self._run("document-dedup", {"document_dedup_min_bytes": "1024"})

    # mega-block-trim
    def test_mega_block_trim_rejects_string_max_bytes(self):
        with self.assertRaises(ConfigError):
            self._run("mega-block-trim", {"mega_block_max_bytes": "32768"})

    def test_mega_block_trim_rejects_negative(self):
        with self.assertRaises(ConfigError):
            self._run("mega-block-trim", {"mega_block_max_bytes": -1})

    # ── Backwards compatibility: valid configs still behave unchanged ──

    def test_valid_tool_result_age_defaults_still_work(self):
        """Default config (no keys set) must not regress — production uses this path."""
        result = self._run("tool-result-age", {})
        self.assertEqual(result.strategy_name, "tool-result-age")

    def test_valid_thinking_blocks_default_mode(self):
        result = self._run("thinking-blocks", {})
        self.assertEqual(result.strategy_name, "thinking-blocks")

    def test_valid_thinking_blocks_all_modes(self):
        for mode in ("distill", "signature-only", "remove"):
            with self.subTest(mode=mode):
                result = self._run("thinking-blocks", {"thinking_mode": mode})
                self.assertEqual(result.strategy_name, "thinking-blocks")


if __name__ == "__main__":
    unittest.main()
