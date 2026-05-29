"""Regression tests for #106 — the guard must not rewrite a live session.

The no-reload tiers (SOFT 25%, agents-active HARD 55%) reach guard_prune_cycle
with read_only_live=True. They must NEVER os.replace the session file Claude
holds open (TOCTOU + inode-swap data loss; and the on-disk rewrite can't shrink
the live context anyway). They checkpoint team state read-only and skip the
write. The reload tiers (which terminate Claude first) are unaffected.
"""

import json
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
            # Number of saves done INLINE (i.e. before any post-death write).
            inline_saves = len(save_calls)
            # Invoke the deferred writer INSIDE the patch context (so save_messages
            # is still the mock) to simulate the post-death write.
            writer = result.get("_deferred_writer")
            if writer is not None:
                writer()
        return result, save_calls, inline_saves

    def test_read_only_live_never_calls_save(self):
        result, save_calls, inline_saves = self._run_mocked(read_only_live=True)
        self.assertEqual(save_calls, [], "save_messages must NOT be called in read-only mode")
        self.assertIsNone(result.get("_deferred_writer"), "read-only must NOT return a writer")
        self.assertTrue(result.get("live_write_skipped"))
        self.assertEqual(result.get("saved_mb"), 0.0)
        self.assertFalse(result.get("reloading"))

    def test_default_defers_write_to_post_death_writer(self):
        # #106: with read_only_live=False + auto_reload=False (overflow-style), the
        # save is NOT inline — guard_prune_cycle returns a deferred writer that the
        # caller invokes only AFTER Claude is terminated. The live file is never
        # rewritten while held. Invoking the writer performs the actual save.
        result, save_calls, inline_saves = self._run_mocked(read_only_live=False)
        self.assertEqual(inline_saves, 0, "save must be deferred, not written inline (#106)")
        self.assertIsNotNone(result.get("_deferred_writer"), "must return a deferred writer")
        self.assertFalse(result.get("live_write_skipped"))
        self.assertEqual(len(save_calls), 1, "deferred writer must perform the save")

    def test_live_fd_held_guard_refuses_mutation(self):
        """@Snailflyer's exact race (issue #106): hold a LIVE writer fd on the
        JSONL, append a sentinel line through it, then run a no-reload guard
        cycle — and assert the guard REFUSES to mutate the live file: same inode
        (no os.replace / inode-swap out from under the harness), sentinel intact,
        no backup. This is the measurable single-writer boundary he asked for."""
        from cozempic.guard import guard_prune_cycle
        from cozempic.team import TeamState

        base = [json.dumps({"type": "user", "uuid": f"u{i}", "message": {"content": "x" * 150}})
                for i in range(3)]
        self.session_path.write_text("\n".join(base) + "\n")
        inode_before = self.session_path.stat().st_ino

        team = MagicMock(spec=TeamState)
        team.is_empty.return_value = True
        team.team_name = None
        team.message_count = 0
        pruned_subset = [(0, json.loads(base[0]), len(base[0]))]  # a prune WOULD shrink it

        # Claude holds the file open and appends a sentinel mid-session.
        with open(self.session_path, "a", encoding="utf-8") as live_fd:
            live_fd.write(json.dumps({"type": "user", "uuid": "SENTINEL",
                                      "message": {"content": "live append"}}) + "\n")
            live_fd.flush()
            with patch("cozempic.guard.prune_with_team_protect",
                       return_value=(pruned_subset, {}, team)), \
                 patch("cozempic.tokens.estimate_session_tokens", return_value=MagicMock(total=9999)), \
                 patch("cozempic.tokens.calibrate_ratio", return_value=3.0):
                result = guard_prune_cycle(
                    session_path=self.session_path, rx_name="gentle",
                    auto_reload=False, read_only_live=True,
                )
            self.assertTrue(result.get("live_write_skipped"), "guard must refuse the live mutation")

        # Boundary assertions: the live-held inode was never swapped, the live
        # append survived, and nothing pruned the file out from under the fd.
        self.assertEqual(inode_before, self.session_path.stat().st_ino,
                         "guard must NOT inode-swap a live-held transcript (#106)")
        final = self.session_path.read_text()
        self.assertIn("SENTINEL", final, "the live append must survive")
        self.assertIn("u1", final)  # nothing was pruned (read-only)
        self.assertEqual(list(self.tmpdir.glob("*.bak")), [], "no backup — no write occurred")

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


class TestTerminateFirstWriteGating(unittest.TestCase):
    """#106: _terminate_and_resume must invoke write_pruned ONLY after confirming
    Claude is dead — never while it may still hold the file open."""

    def _run_plain(self, alive_sequence):
        from cozempic import guard
        wrote = []
        with patch.object(guard, "_detect_terminal_env", return_value="plain"), \
             patch.object(guard, "_detect_claude_flags", return_value=""), \
             patch.object(guard, "_pid_is_alive", side_effect=alive_sequence), \
             patch.object(guard, "_pid_identity_match", return_value=True), \
             patch.object(guard, "_is_claude_process", return_value=True), \
             patch.object(guard, "_wait_for_exit", return_value=True), \
             patch.object(guard, "_spawn_reload_watcher"), \
             patch.object(guard, "write_reload_sentinel"), \
             patch.object(guard.os, "kill"), \
             patch.object(guard.platform, "system", return_value="Darwin"):
            guard._terminate_and_resume(
                12345, "/tmp/x",
                session_id="sess", session_path=Path("/tmp/x.jsonl"),
                write_pruned=lambda: wrote.append("wrote"),
            )
        return wrote

    def test_write_fires_after_confirmed_death(self):
        # Alive at the entry gate, dead at the write gate → the prune is persisted.
        self.assertEqual(self._run_plain([True, False]), ["wrote"])

    def test_write_skipped_if_claude_survived_the_kill(self):
        # Alive at entry AND still alive at the write gate (kill failed) → NO write;
        # Claude resumes from the untouched full file rather than risk corruption.
        self.assertEqual(self._run_plain([True, True]), [])

    def test_no_write_when_claude_already_dead_at_entry(self):
        # Anti-resurrection entry gate: if Claude already exited (user closed it
        # mid-prune), _terminate_and_resume returns before killing/writing — the
        # closed session's file is left intact and is NOT resurrected.
        from cozempic import guard
        wrote = []
        with patch.object(guard, "_detect_terminal_env", return_value="plain"), \
             patch.object(guard, "_detect_claude_flags", return_value=""), \
             patch.object(guard, "_pid_is_alive", return_value=False):
            guard._terminate_and_resume(
                12345, "/tmp/x", session_id="sess",
                session_path=Path("/tmp/x.jsonl"),
                write_pruned=lambda: wrote.append("wrote"),
            )
        self.assertEqual(wrote, [], "must not write (or resurrect) an already-dead session")


class TestDeferredWriteAppendPreservation(unittest.TestCase):
    """#106 / Snailflyer invariant: a line Claude appends between the prune
    snapshot and its death must survive the deferred (post-death) write."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="cozempic_106_append_"))
        self.session_path = self.tmpdir / "s.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_late_append_preserved_by_deferred_write(self):
        from cozempic.guard import guard_prune_cycle
        from cozempic.team import TeamState

        # Real 3-line session.
        lines = [json.dumps({"type": "user", "uuid": f"u{i}", "message": {"content": "x" * 200}})
                 for i in range(3)]
        self.session_path.write_text("\n".join(lines) + "\n")

        team = MagicMock(spec=TeamState)
        team.is_empty.return_value = True
        team.team_name = None
        team.message_count = 0
        # Prune drops the middle message (real save_messages + real snapshot).
        kept = [(0, json.loads(lines[0]), len(lines[0])),
                (2, json.loads(lines[2]), len(lines[2]))]

        with patch("cozempic.guard.prune_with_team_protect", return_value=(kept, {}, team)), \
             patch("cozempic.tokens.estimate_session_tokens", return_value=MagicMock(total=9999)), \
             patch("cozempic.tokens.calibrate_ratio", return_value=3.0):
            result = guard_prune_cycle(
                session_path=self.session_path, rx_name="gentle",
                auto_reload=False,  # overflow-style: returns a deferred writer
            )
            writer = result.get("_deferred_writer")
            self.assertIsNotNone(writer)
            # Claude appends a late line AFTER the snapshot, BEFORE the deferred write.
            sentinel = json.dumps({"type": "user", "uuid": "LATE", "message": {"content": "sentinel"}})
            with open(self.session_path, "a", encoding="utf-8") as f:
                f.write(sentinel + "\n")
            # Post-death write must preserve the late append (append-aware save).
            writer()

        final = self.session_path.read_text()
        self.assertIn("LATE", final, "late append must survive the deferred write")
        self.assertIn("u0", final)
        self.assertIn("u2", final)
        self.assertNotIn("u1", final)  # the pruned-out message stays pruned


if __name__ == "__main__":
    unittest.main()
