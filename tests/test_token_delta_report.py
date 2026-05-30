"""Regression tests for #105 — `treat` reported a nonsense token delta.

When an aggressive prune runs `metadata-strip`, the `usage` frames that the
exact token count anchors on are removed, so the post-prune exact count
re-anchors on an earlier turn with a different `cache_read`. The before/after
delta is then non-comparable and could go negative — surfacing to the user as
"Saved -648.7K tokens (-401.6%) freed" (the original report).

The report must never print a negative / over-100% "saved" line; when the delta
is unreliable it falls back to the byte savings with a clear note.
"""

import io
import unittest
from contextlib import redirect_stdout

from cozempic.types import PrescriptionResult


def _render(original_tokens, final_tokens):
    from cozempic.cli import print_prescription_result

    pr = PrescriptionResult(
        prescription_name="aggressive",
        strategy_results=[],
        original_total_bytes=18_980_000,   # 18.98MB, from the #105 report
        final_total_bytes=13_030_000,      # 13.03MB — bytes DID shrink
        original_message_count=9249,
        final_message_count=9249,
        original_tokens=original_tokens,
        final_tokens=final_tokens,
        token_method="exact",
        context_window=1_000_000,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_prescription_result(pr)
    return buf.getvalue()


class TestTreatTokenDeltaReport(unittest.TestCase):

    def test_reanchored_count_does_not_print_nonsense(self):
        # The exact numbers from issue #105: tokens "grew" 161.5K -> 810.2K
        # even though the file shrank ~31%.
        out = _render(original_tokens=161_500, final_tokens=810_200)
        # No negative "saved" tokens, no negative/over-100% percentage.
        self.assertNotIn("-401", out)
        self.assertNotIn("Saved    -", out)
        self.assertNotRegex(out, r"Saved.*-\d")          # no negative on the Saved line
        self.assertNotRegex(out, r"\(-\d+\.\d+%\)")       # no negative percentage
        # Falls back to the reliable byte savings + a clear note.
        self.assertIn("token delta n/a", out)

    def test_reanchored_reports_byte_savings(self):
        out = _render(original_tokens=161_500, final_tokens=810_200)
        # The byte savings line must still convey a positive reduction.
        self.assertIn("Saved", out)
        self.assertNotIn("401", out)
        # Bytes freed ~5.95MB and a positive byte percentage (~31%).
        self.assertRegex(out, r"\(3[01]\.\d%\)")

    def test_normal_reduction_still_shows_token_savings(self):
        # The healthy case (after < before) must be unchanged.
        out = _render(original_tokens=385_300, final_tokens=108_900)
        self.assertIn("tokens (", out)            # "Saved  276.4K tokens (71.7%)"
        self.assertRegex(out, r"\(7\d\.\d%\)")    # ~71% saved
        self.assertNotIn("token delta n/a", out)

    def test_equal_tokens_is_not_treated_as_error(self):
        out = _render(original_tokens=200_000, final_tokens=200_000)
        self.assertIn("0.0%", out)
        self.assertNotIn("token delta n/a", out)


class TestGuardPruneResultFormat(unittest.TestCase):

    def test_negative_delta_reports_bytes_only(self):
        from cozempic.guard import _fmt_prune_result

        msg = _fmt_prune_result(
            {"original_tokens": 161_500, "final_tokens": 810_200, "saved_mb": 5.95}
        )
        self.assertNotIn("-", msg)            # no negative figure anywhere
        self.assertNotIn("tokens freed", msg)
        self.assertIn("MB saved", msg)

    def test_positive_delta_reports_tokens(self):
        from cozempic.guard import _fmt_prune_result

        msg = _fmt_prune_result(
            {"original_tokens": 385_300, "final_tokens": 108_900, "saved_mb": 5.43}
        )
        self.assertIn("tokens freed", msg)
        self.assertIn("%", msg)
        self.assertNotRegex(msg, r"-\d")


if __name__ == "__main__":
    unittest.main()
