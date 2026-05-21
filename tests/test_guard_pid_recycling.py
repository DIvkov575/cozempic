"""RED tests for PID start-time tracking (recycled-PID resurrection vector).

Architect spec: AUDIT_REPORT_pid_recycling.md (commit 5af7c0d)
Vector: os.kill(pid, 0) answers liveness, not identity. If Claude dies and its
PID is recycled to a live unrelated process AND the JSONL is freshly written,
_is_claude_process's mtime fallback still returns True → resurrection.
Fix: record (pid, start_time) at guard startup via _record_claude_identity;
gate _terminate_and_resume with _pid_identity_match before _is_claude_process.

These 4 tests are expected to FAIL (AttributeError / AssertionError) until
the implementation in fix commit 2 + 3 lands.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# Test 1 — _pid_identity_match: real psutil, recycled PID detected
# ---------------------------------------------------------------------------
class TestPidIdentityMatchRecycledPid(unittest.TestCase):
    """Unit test: record real process start_time, then mock _get_pid_start_time
    to return a different start_time (simulating PID recycling after the original
    process died). _pid_identity_match must return False.
    """

    def setUp(self):
        import cozempic.guard as g
        g._CLAUDE_IDENTITY.clear()

    def tearDown(self):
        import cozempic.guard as g
        g._CLAUDE_IDENTITY.clear()

    def test_recycled_pid_start_time_mismatch_returns_false(self):
        import cozempic.guard as g

        # Use a real live PID (ourselves) and record its real start_time.
        pid = os.getpid()
        session_id = "aaaa1111bbbb2222cccc3333dddd4444"
        g._record_claude_identity(session_id, pid)

        # Confirm identity was recorded.
        self.assertIn(session_id, g._CLAUDE_IDENTITY)
        recorded_pid, recorded_start_time = g._CLAUDE_IDENTITY[session_id]
        self.assertEqual(recorded_pid, pid)

        # Simulate PID recycled: same PID but different start_time (10000s later).
        recycled_start_time = recorded_start_time + 10000.0
        with patch("cozempic.guard._get_pid_start_time", return_value=recycled_start_time):
            result = g._pid_identity_match(pid, session_id)

        self.assertFalse(
            result,
            "recycled PID with different start_time must return False",
        )

    def test_matching_pid_start_time_returns_true(self):
        import cozempic.guard as g

        pid = os.getpid()
        session_id = "aaaa1111bbbb2222cccc3333dddd5555"
        g._record_claude_identity(session_id, pid)

        recorded_pid, recorded_start_time = g._CLAUDE_IDENTITY[session_id]

        # Same start_time: identity matches.
        with patch("cozempic.guard._get_pid_start_time", return_value=recorded_start_time):
            result = g._pid_identity_match(pid, session_id)

        self.assertTrue(result, "same PID + same start_time must return True")

    def test_different_pid_than_recorded_returns_false(self):
        import cozempic.guard as g

        pid = os.getpid()
        session_id = "aaaa1111bbbb2222cccc3333dddd6666"
        g._record_claude_identity(session_id, pid)

        # Pass a completely different PID.
        with patch("cozempic.guard._get_pid_start_time", return_value=9999999.0):
            result = g._pid_identity_match(pid + 9999, session_id)

        self.assertFalse(result, "different PID than recorded must return False")


import os  # noqa: E402 — placed after class for import order clarity


# ---------------------------------------------------------------------------
# Test 2 — No resurrection when PID recycled (integration)
# ---------------------------------------------------------------------------
class TestNoResurrectionOnRecycledPid(unittest.TestCase):
    """Integration test: full _terminate_and_resume with a recycled PID.

    Scenario (architect's reproducer § "POST-fix flow"):
    - session_id recorded with (pid=89113, start_time=T0)
    - Claude dies, JSONL mtime refreshed by save_messages → fresh
    - OS recycles pid=89113 to an unrelated process with start_time=T1
    - _terminate_and_resume is called
    Expected: _pid_identity_match returns False → early return → NO SIGTERM,
    NO SIGKILL, NO sentinel write, NO _spawn_reload_watcher call.
    """

    def setUp(self):
        import cozempic.guard as g
        g._CLAUDE_IDENTITY.clear()

    def tearDown(self):
        import cozempic.guard as g
        g._CLAUDE_IDENTITY.clear()

    def test_recycled_pid_no_resurrection(self):
        import cozempic.guard as g

        session_id = "ddddddddeeee1111222233334444aaaa"
        fake_pid = 89113
        T0 = 1716220000.0
        T1 = 1716230000.0  # 10000s later — clearly different process

        # Record original identity.
        g._CLAUDE_IDENTITY[session_id] = (fake_pid, T0)

        with tempfile.TemporaryDirectory() as td:
            jsonl = Path(td) / "session.jsonl"
            jsonl.write_text("{}\n")  # fresh mtime — would fool mtime fallback

            # _pid_is_alive returns True (recycled process IS alive).
            # _get_pid_start_time returns T1 (different process).
            with patch("cozempic.guard._pid_is_alive", return_value=True), \
                 patch("cozempic.guard._get_pid_start_time", return_value=T1), \
                 patch("cozempic.guard._spawn_reload_watcher") as mock_watcher, \
                 patch("cozempic.guard.write_reload_sentinel") as mock_sentinel, \
                 patch("cozempic.guard.os.kill") as mock_kill, \
                 patch("cozempic.guard._detect_terminal_env", return_value="plain"):
                g._terminate_and_resume(
                    claude_pid=fake_pid,
                    project_dir=td,
                    session_id=session_id,
                    session_path=jsonl,
                )

        mock_watcher.assert_not_called()
        mock_sentinel.assert_not_called()
        mock_kill.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3 — _pid_identity_match degrades gracefully when psutil unavailable
# ---------------------------------------------------------------------------
class TestPidIdentityMatchDegrades(unittest.TestCase):
    """Unit test: psutil unavailable → _pid_identity_match returns True (fail-OPEN).

    Fail-OPEN rationale: without psutil we can't check start_time; we fall through
    to the existing _pid_is_alive + _is_claude_process layers (same risk as v1.8.16).
    Fail-CLOSED would break non-Claude-Code installs and CI without psutil.
    """

    def setUp(self):
        import cozempic.guard as g
        g._CLAUDE_IDENTITY.clear()

    def tearDown(self):
        import cozempic.guard as g
        g._CLAUDE_IDENTITY.clear()

    def test_psutil_unavailable_returns_true(self):
        import cozempic.guard as g

        pid = os.getpid()
        session_id = "aaaa1111bbbb2222cccc3333eeee7777"
        # Record an identity so the session_id IS in _CLAUDE_IDENTITY.
        g._CLAUDE_IDENTITY[session_id] = (pid, 1716220000.0)

        # _get_pid_start_time returns None when psutil raises ImportError.
        with patch("cozempic.guard._get_pid_start_time", return_value=None):
            result = g._pid_identity_match(pid, session_id)

        self.assertTrue(
            result,
            "psutil unavailable (None start_time) must return True (fail-OPEN)",
        )

    def test_no_session_id_returns_true(self):
        import cozempic.guard as g

        result = g._pid_identity_match(os.getpid(), session_id=None)
        self.assertTrue(result, "None session_id must return True (conservative allow)")

    def test_session_id_not_recorded_returns_true(self):
        import cozempic.guard as g

        # Session not in _CLAUDE_IDENTITY at all.
        result = g._pid_identity_match(os.getpid(), session_id="not-recorded-xxxx")
        self.assertTrue(result, "unrecorded session_id must return True (conservative allow)")


# ---------------------------------------------------------------------------
# Test 4 — _record_claude_identity called during start_guard
# ---------------------------------------------------------------------------
class TestRecordClaudeIdentityOnStartGuard(unittest.TestCase):
    """Verify _record_claude_identity(session_id, pid) is called once during
    start_guard after find_claude_pid() resolves, before the watchdog loop runs.
    """

    def setUp(self):
        import cozempic.guard as g
        g._CLAUDE_IDENTITY.clear()

    def tearDown(self):
        import cozempic.guard as g
        g._CLAUDE_IDENTITY.clear()

    def test_record_called_with_correct_args(self):
        import cozempic.guard as g

        fake_pid = 12345
        fake_session_id = "sssssssstttt1111222233334444ssss"

        with tempfile.TemporaryDirectory() as td:
            jsonl = Path(td) / "session.jsonl"
            jsonl.write_text("{}\n")

            fake_session = {
                "session_id": fake_session_id,
                "session_path": str(jsonl),
                "path": jsonl,
                "project_dir": td,
            }

            calls = []

            def fake_record(sid, pid):
                calls.append((sid, pid))

            # start_guard calls _resolve_session_by_id (not find_current_session)
            # when session_id is passed, then load_messages, detect_context_window,
            # record_session, etc. Patch everything between session resolution and
            # the watchdog loop so we reach line 503 (_record_claude_identity call).
            with patch("cozempic.guard._resolve_session_by_id", return_value=fake_session), \
                 patch("cozempic.guard.find_claude_pid", return_value=fake_pid), \
                 patch("cozempic.guard._record_claude_identity", side_effect=fake_record), \
                 patch("cozempic.guard.load_messages", return_value=[]), \
                 patch("cozempic.tokens.detect_context_window", return_value=200000), \
                 patch("cozempic.tokens.default_token_thresholds_4tier", return_value=(50000, 110000, 160000)), \
                 patch("cozempic.session.record_session"), \
                 patch("cozempic.guard._cleanup_stale_watchers"), \
                 patch("cozempic.guard.ping_install_if_new"), \
                 patch("cozempic.guard.maybe_auto_update"), \
                 patch("cozempic.guard.checkpoint_team"), \
                 patch("cozempic.guard.time.sleep", side_effect=RuntimeError("stop loop")):
                try:
                    g.start_guard(session_id=fake_session_id, claude_pid=fake_pid)
                except (RuntimeError, SystemExit):
                    pass

        self.assertEqual(len(calls), 1, "_record_claude_identity must be called exactly once")
        self.assertEqual(calls[0], (fake_session_id, fake_pid))
