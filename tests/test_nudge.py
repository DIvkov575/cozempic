"""1.8.22 — tiered nudge (cozempic nudge, Stop hook). Non-blocking, once-per-tier."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _transcript(tmp: Path, total: int, model: str = "claude-sonnet-4-6") -> Path:
    p = tmp / "t.jsonl"
    rec = {"type": "assistant", "message": {"role": "assistant", "model": model,
           "content": [{"type": "text", "text": "x"}],
           "usage": {"input_tokens": total, "cache_creation_input_tokens": 0,
                     "cache_read_input_tokens": 0, "output_tokens": 0}}}
    p.write_text(json.dumps(rec) + "\n")
    return p


class TestNudge(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cozempic_nudge_"))
        self.home = Path(tempfile.mkdtemp(prefix="cozempic_home_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def _run(self, total: int, session: str = "s1", env: dict | None = None):
        from cozempic.cli import cmd_nudge
        t = _transcript(self.tmp, total)
        payload = json.dumps({"transcript_path": str(t), "session_id": session})
        out = io.StringIO()
        e = {k: v for k, v in os.environ.items() if not k.startswith("COZEMPIC_NUDGE")}
        if env:
            e.update(env)
        with patch("sys.stdin", io.StringIO(payload)), patch("sys.stdout", out), \
             patch("pathlib.Path.home", return_value=self.home), \
             patch.dict(os.environ, e, clear=True):
            cmd_nudge(None)
        s = out.getvalue()
        return json.loads(s)["systemMessage"] if s.strip() else None

    def test_silent_below_25(self):
        self.assertIsNone(self._run(100_000))  # 10% of 1M

    def test_25_tier_informational(self):
        m = self._run(260_000, "s25")  # 26%
        self.assertIsNotNone(m)
        self.assertIn("26%", m)
        self.assertIn("Optional", m)
        self.assertNotIn("auto-reload", m.lower())  # 25% has no reload-pending line

    def test_55_tier_actionable(self):
        m = self._run(560_000, "s55")  # 56%
        self.assertIsNotNone(m)
        self.assertIn("56%", m)
        self.assertIn("cozempic reload", m)
        self.assertIn("your next idle", m.lower())

    def test_80_tier_urgent_no_emergency_word(self):
        m = self._run(820_000, "s80")  # 82%
        self.assertIsNotNone(m)
        self.assertIn("82%", m)
        self.assertIn("autocompact wall", m)
        self.assertNotIn("emergency", m.lower())  # user explicitly removed "emergency"

    def test_once_per_tier(self):
        self.assertIsNotNone(self._run(560_000, "sx"))
        self.assertIsNone(self._run(560_000, "sx"))  # silent on repeat in same tier

    def test_rearm_after_drop_then_recross(self):
        self.assertIsNotNone(self._run(560_000, "sy"))   # cross 55 → fires
        self.assertIsNone(self._run(560_000, "sy"))       # latched → silent
        self.assertIsNone(self._run(100_000, "sy"))       # dropped to 10% (< all tiers) → silent, clears latch
        self.assertIsNotNone(self._run(560_000, "sy"))    # re-cross 55 → re-fires

    def test_off_env_silences(self):
        self.assertIsNone(self._run(560_000, "soff", env={"COZEMPIC_NUDGE_OFF": "1"}))

    def test_non_blocking_bad_stdin(self):
        from cozempic.cli import cmd_nudge
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("not json")), patch("sys.stdout", out), \
             patch("pathlib.Path.home", return_value=self.home):
            cmd_nudge(None)  # must not raise
        self.assertEqual(out.getvalue().strip(), "")

    def test_projected_pct_from_armed_sentinel(self):
        # when the daemon armed with a projected_pct, the 55% message shows it
        from cozempic import guard
        with patch("cozempic.guard._guard_tmp_root", return_value=self.tmp):
            guard.write_armed("sp1", None, 55, 38.0)
            m = self._run(560_000, "sp1")
        self.assertIsNotNone(m)
        self.assertIn("~38%", m)


if __name__ == "__main__":
    unittest.main()
