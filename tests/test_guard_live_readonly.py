"""Regression tests for #106 — the guard must not rewrite a live session.

The no-reload tiers (SOFT 25%, agents-active HARD 55%) reach guard_prune_cycle
with read_only_live=True. They must NEVER os.replace the session file Claude
holds open (TOCTOU + inode-swap data loss; and the on-disk rewrite can't shrink
the live context anyway). They checkpoint team state read-only and skip the
write. The reload tiers (which terminate Claude first) are unaffected.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock


def _make_session_file(tmpdir: Path, size_bytes: int = 100_000) -> Path:
    path = tmpdir / "fake_session.jsonl"
    line = '{"type":"user","message":{"content":"' + "x" * 100 + '"}}\n'
    n = max(1, size_bytes // len(line.encode()))
    path.write_text(line * n)
    return path


class TestReadOnlyLiveGuard(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="cozempic_106_"))
        self.session_path = _make_session_file(self.tmpdir, 100_000)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_mocked(self, read_only_live):
        """guard_prune_cycle with a mocked prune that DOES save bytes, so any
        skipped write is attributable to the flag, not to an empty prune."""
        from cozempic.team import TeamState
        from cozempic.guard import guard_prune_cycle

        team = MagicMock(spec=TeamState)
        team.is_empty.return_value = True
        team.team_name = None
        team.message_count = 0
        orig = [(0, {"type": "user"}, 100_000)]
        pruned = [(0, {"type": "user"}, 40_000)]  # 60% saving — well past futile floor
        save_calls = []

        with patch("cozempic.guard.load_messages", return_value=orig), \
             patch("cozempic.guard.prune_with_team_protect",
                   return_value=(pruned, {}, team)), \
             patch("cozempic.guard.save_messages",
                   side_effect=lambda *a, **k: save_calls.append(True)), \
             patch("cozempic.guard.snapshot_session", return_value=MagicMock()), \
             patch("cozempic.tokens.estimate_session_tokens",
                   return_value=MagicMock(total=50000)), \
             patch("cozempic.tokens.calibrate_ratio", return_value=0.5):
            result = guard_prune_cycle(
                session_path=self.session_path,
                rx_name="gentle",
                config=None,
                auto_reload=False,
                read_only_live=read_only_live,
            )
        return result, save_calls

    def test_read_only_live_never_calls_save(self):
        result, save_calls = self._run_mocked(read_only_live=True)
        self.assertEqual(save_calls, [], "save_messages must NOT be called in read-only mode")
        self.assertTrue(result.get("live_write_skipped"))
        self.assertEqual(result.get("saved_mb"), 0.0)
        self.assertFalse(result.get("reloading"))

    def test_default_still_writes_when_prune_saves(self):
        result, save_calls = self._run_mocked(read_only_live=False)
        self.assertEqual(len(save_calls), 1, "save_messages SHOULD be called by default")
        self.assertFalse(result.get("live_write_skipped"))

    def test_real_file_bytes_unchanged_in_read_only(self):
        """End-to-end on a real file (no mocks): the read-only cycle must not
        modify the bytes and must not leave a .bak — proving no os.replace."""
        from cozempic.guard import guard_prune_cycle

        before = self.session_path.read_bytes()
        result = guard_prune_cycle(
            session_path=self.session_path,
            rx_name="gentle",
            config=None,
            auto_reload=False,
            read_only_live=True,
        )
        after = self.session_path.read_bytes()
        self.assertEqual(before, after, "read-only cycle must not rewrite the live session file")
        self.assertTrue(result.get("live_write_skipped"))
        self.assertEqual(list(self.tmpdir.glob("*.bak")), [], "read-only cycle must not create a backup")


if __name__ == "__main__":
    unittest.main()
