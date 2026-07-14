"""Orphan deadman backstop — the guard must not run forever without a Claude anchor.

ROOT CAUSE (this file's regression target): the daemon's ONLY self-termination
anchor is the watchdog at start_guard(), gated on ``if claude_pid and claude_alive``.
A detached daemon (start_new_session=True) is reparented to init, so a child that
could not resolve claude_pid at spawn re-runs find_claude_pid() against its OWN
(anchorless) process tree → None forever → the watchdog block is skipped every
cycle. With no other liveness check and no lifetime backstop, a below-threshold
idle session loops IMMORTALLY. Observed in the wild: guards running 4–10h with
no --claude-pid in argv, pinned or idle, long after their session died.

THE FIX: a deadman backstop. When the guard has NO live Claude anchor, it exits
once the transcript has stayed idle past ORPHAN_DEADMAN_SECONDS. Strictly scoped
to the anchorless case — a healthy session (live claude_pid) is never affected,
however long it idles, because the watchdog owns that exit.

These tests are RED until the fix lands (AttributeError on the missing helper /
constant, then the integration test hangs-guard → TimeoutError sentinel).
"""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import cozempic.guard as G


class _EmptyState:
    subagents: list = []
    tasks: list = []
    message_count = 0

    def is_empty(self):
        return True


# ---------------------------------------------------------------------------
# Unit: the pure decision helper
# ---------------------------------------------------------------------------
class TestOrphanDeadmanHelper(unittest.TestCase):
    def test_fires_when_anchorless_and_idle_past_window(self):
        self.assertTrue(
            G._orphan_deadman_tripped(has_live_anchor=False, idle_elapsed=1800, deadman_s=1800)
        )

    def test_not_fire_before_window(self):
        self.assertFalse(
            G._orphan_deadman_tripped(has_live_anchor=False, idle_elapsed=1799, deadman_s=1800)
        )

    def test_never_fires_when_anchor_alive(self):
        # A healthy session idling for a week must NOT be killed by the deadman —
        # the watchdog owns that exit path.
        self.assertFalse(
            G._orphan_deadman_tripped(has_live_anchor=True, idle_elapsed=10**9, deadman_s=1800)
        )

    def test_zero_disables(self):
        self.assertFalse(
            G._orphan_deadman_tripped(has_live_anchor=False, idle_elapsed=10**9, deadman_s=0)
        )

    def test_env_reader_default_and_clamp(self):
        with mock.patch.dict("os.environ", {}, clear=False) as _e:
            _e.pop("COZEMPIC_GUARD_ORPHAN_DEADMAN_SECONDS", None)
            self.assertEqual(G._read_orphan_deadman_seconds(), 1800)
        with mock.patch.dict("os.environ", {"COZEMPIC_GUARD_ORPHAN_DEADMAN_SECONDS": "0"}):
            self.assertEqual(G._read_orphan_deadman_seconds(), 0)  # 0 = disabled
        with mock.patch.dict("os.environ", {"COZEMPIC_GUARD_ORPHAN_DEADMAN_SECONDS": "garbage"}):
            self.assertEqual(G._read_orphan_deadman_seconds(), 1800)
        with mock.patch.dict("os.environ", {"COZEMPIC_GUARD_ORPHAN_DEADMAN_SECONDS": "-5"}):
            self.assertEqual(G._read_orphan_deadman_seconds(), 1800)


# ---------------------------------------------------------------------------
# Integration: drive the REAL loop, anchorless + below-threshold + idle
# ---------------------------------------------------------------------------
class TestAnchorlessIdleGuardExits(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.session = Path(self._td.name) / "dead1234.jsonl"
        # A small, BELOW-threshold session that never grows (idle).
        self.session.write_text(
            '{"type":"user","message":{"role":"user","content":"x"}}\n',
            encoding="utf-8",
        )
        self.sess = {"path": self.session, "session_id": "dead1234", "project": "p",
                     "size": self.session.stat().st_size, "mtime": 0, "lines": 1}
        self.sleeps: list = []

    def tearDown(self):
        self._td.cleanup()

    def _run(self, deadman_s=10):
        # Sleep stub records intended sleeps (deterministic idle-time accrual) and
        # HARD-caps the loop: if the daemon does NOT self-terminate it would spin
        # forever and hang the suite, so raise a distinctive sentinel well past the
        # expected trip so a regression fails loudly instead of hanging.
        cap = {"n": 0}

        def fake_sleep(s):
            self.sleeps.append(s)
            cap["n"] += 1
            if cap["n"] > 200:
                raise TimeoutError("guard did not self-terminate — deadman regression")

        patches = {
            "find_current_session": lambda *a, **k: self.sess,
            "load_messages": lambda *a, **k: [(0, {"type": "user"}, 10)],
            "checkpoint_team": lambda *a, **k: _EmptyState(),
            "quick_token_estimate": lambda *a, **k: 50_000,   # < 25% of 1M → no tier fires
            "cleanup_old_backups": lambda *a, **k: None,
            "ping_install_if_new": lambda *a, **k: None,
            "maybe_auto_update": lambda *a, **k: None,
            "_cleanup_stale_watchers": lambda *a, **k: None,
            "_detect_interactive": lambda *a, **k: False,
            "find_claude_pid": lambda *a, **k: None,          # ANCHORLESS
            "_safe_unlink_session_pidfile": lambda *a, **k: None,
        }
        cms = [mock.patch.object(G, name, fn) for name, fn in patches.items()]
        cms.append(mock.patch.object(G.time, "sleep", fake_sleep))
        cms.append(mock.patch.object(G, "ORPHAN_DEADMAN_SECONDS", deadman_s))
        cms.append(mock.patch("cozempic.tokens.detect_context_window", lambda *a, **k: 1_000_000))
        cms.append(mock.patch("cozempic.tokens.default_token_thresholds_4tier",
                              lambda cw: (250_000, 550_000, 800_000)))
        cms.append(mock.patch("cozempic.session.record_session", lambda *a, **k: None))
        for c in cms:
            c.start()
        try:
            with redirect_stdout(io.StringIO()):
                # Deadman is a sibling of the watchdog "Claude exited" break: it
                # returns cleanly (falls through `finally`), it does not sys.exit.
                # A regression that never breaks would spin to the 200-cycle cap
                # and raise the TimeoutError sentinel.
                G.start_guard(cwd=self._td.name, threshold_mb=50.0,
                              interval=2, reactive=False, auto_reload=True)
        finally:
            for c in reversed(cms):
                c.stop()

    def test_anchorless_idle_guard_self_terminates(self):
        self._run(deadman_s=10)
        # It stopped well before the 200-cycle safety cap (no TimeoutError raised).
        self.assertLess(len(self.sleeps), 200,
                        "guard must self-terminate via deadman, not spin to the cap")


if __name__ == "__main__":
    unittest.main()
