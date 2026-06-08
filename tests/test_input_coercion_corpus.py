"""Standing safeguard (1.8.23): a table-driven ADVERSARIAL CORPUS thrown at every
numeric input-validator, so the NaN/inf/huge-int "gate-disable" class (PR #116 +
the COZEMPIC_RELOAD_WARN_GRACE follow-up) cannot regress.

Why this exists: IEEE-754 makes every comparison with NaN False, and `inf <= 0` is
False, so a NaN/inf that slips past a `value <= 0` check silently DISABLES the
threshold/gate it controls (it compares False to everything). A huge int (10**400)
does the same by magnitude. These bugs are silent — no crash — so logic tests miss
them. This corpus catches the whole class with an OUTPUT-shaped invariant:

    For every adversarial input, a validator must EITHER reject it (raise / clean
    argparse error) OR return a finite, in-range, correctly-typed value.

Adding a new COZEMPIC_* numeric knob? Add one row to _ENV_VALIDATORS and the corpus
is applied automatically. Zero dependencies (cozempic is stdlib-only — no hypothesis).
"""

import argparse
import math
import os
import unittest
from contextlib import contextmanager

import cozempic.cli as cli
import cozempic.guard as g
import cozempic.tokens as t
from cozempic._validation import ConfigError, coerce_positive_float, coerce_positive_int

# env / CLI inputs are ALWAYS strings
STR_CORPUS = ["nan", "NaN", "inf", "+inf", "-inf", "infinity", "1e999", "-1e999",
              "-0", "", "   ", "0", "-1", "-0.5", "abc", "١٢٣",
              "1" + "0" * 400, "1.5", "50"]
# native-typed corpus for the config-DICT helpers (built in-process, not from a string)
NATIVE_CORPUS = [float("nan"), float("inf"), float("-inf"), -0.0, 10 ** 400,
                 -1, 0, True, False, None, "x", [], {}]

_UPPER = 10 ** 12  # any CLI/env validator output must be well under this


@contextmanager
def _env(name, value):
    old = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


def _assert_finite(tc, r):
    tc.assertIsInstance(r, (int, float))
    if isinstance(r, float):
        tc.assertTrue(math.isfinite(r), f"validator leaked a non-finite value: {r!r}")


def _assert_finite_inrange(tc, r):
    _assert_finite(tc, r)
    tc.assertLessEqual(abs(r), _UPPER, f"validator leaked an out-of-bound value: {r!r}")


# (validator, ENV var) — these return a default on bad input (never raise), so the
# RETURN value must always be sane.
_ENV_VALIDATORS = [
    (g._reload_warn_grace, "COZEMPIC_RELOAD_WARN_GRACE"),
    (g._force_reload_pct, "COZEMPIC_FORCE_RELOAD_PCT"),
    (g._idle_reload_cycles, "COZEMPIC_IDLE_RELOAD_CYCLES"),
    (g._idle_backoff_cycles, "COZEMPIC_IDLE_BACKOFF_CYCLES"),
    (g._read_min_prune_ratio, "COZEMPIC_MIN_PRUNE_RATIO"),
    (g._read_hard_exit_threshold, "COZEMPIC_GUARD_HARD_EXIT_K"),
    (t.get_chars_per_token, "COZEMPIC_CHARS_PER_TOKEN"),
]


class TestEnvNumericCorpus(unittest.TestCase):
    def test_env_validators_never_leak_nonfinite_or_huge(self):
        for fn, var in _ENV_VALIDATORS:
            for raw in STR_CORPUS:
                with self.subTest(fn=fn.__name__, value=raw), _env(var, raw):
                    _assert_finite_inrange(self, fn())


class TestCliValidatorCorpus(unittest.TestCase):
    def test_positive_float_int_reject_or_return_sane(self):
        for fn in (cli._positive_float, cli._positive_int):
            for raw in STR_CORPUS:
                with self.subTest(fn=fn.__name__, value=raw):
                    try:
                        r = fn(raw)
                    except argparse.ArgumentTypeError:
                        continue  # a clean reject is acceptable
                    _assert_finite_inrange(self, r)


class TestConfigDictCorpus(unittest.TestCase):
    def test_coerce_helpers_reject_or_return_finite(self):
        for fn in (coerce_positive_float, coerce_positive_int):
            for val in NATIVE_CORPUS:
                with self.subTest(fn=fn.__name__, value=repr(val)):
                    try:
                        r = fn({"k": val}, "k", 1)
                    except (ConfigError, ValueError, TypeError):
                        continue  # a clean reject is acceptable
                    _assert_finite(self, r)  # a silent-accept of nan/inf fails here


if __name__ == "__main__":
    unittest.main()
