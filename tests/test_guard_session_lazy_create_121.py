"""RCA repro for #121 (regression of #73): the guard daemon dies at startup and
never respawns when Claude Code creates the session JSONL LATER than the 15s
`_resolve_session_by_id` budget.

Ground truth from the reporter (@AndrewChemis): CC creates the session JSONL
*lazily, on the first user turn* — user-paced, observed at 109s after SessionStart
(the daemon had given up at 15s). Resolution itself works once the file exists; the
daemon simply gave up before it appeared and `sys.exit(1)`'d with no recovery.

These tests PROVE that offline: (1) the current 10-retry budget gives up before a
late-born JSONL appears; (2) resolution succeeds the instant the file exists — so
the fix is to WAIT patiently for an explicit session, not to change resolution.
"""

import math
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import cozempic.guard as guard


class TestGuardSessionLazyCreate121(unittest.TestCase):
    SESS = {"session_id": "f0702a2b", "path": Path("/x/f0702a2b.jsonl"),
            "size": 0, "project": "p"}

    def test_resolve_gives_up_before_a_late_jsonl_appears(self):
        # The JSONL is "born" only after more polls than the 10-retry budget allows
        # (modeling the real 109s-vs-15s gap). _resolve_session_by_id returns None,
        # so start_guard's `if not sess: ... sys.exit(1)` fires — and nothing
        # respawns the daemon. This is the #121 bug, reproduced offline.
        polls = {"n": 0}

        def late_find():
            polls["n"] += 1
            return [self.SESS] if polls["n"] > 30 else []  # appears only after 30 polls

        with patch.object(guard, "find_sessions", side_effect=late_find):
            r = guard._resolve_session_by_id("f0702a2b", max_retries=10, retry_delay=0)
        self.assertIsNone(
            r, "BUG #121: the retry budget gives up before the lazily-created JSONL appears")

    def test_resolution_works_the_instant_the_file_exists(self):
        # The reporter's key observation: detection is NOT broken. Once find_sessions()
        # includes the session, resolution is immediate. So #121 is a WAIT problem,
        # not a resolution problem — the fix must keep waiting for an explicit session.
        with patch.object(guard, "find_sessions", return_value=[self.SESS]):
            r = guard._resolve_session_by_id("f0702a2b", max_retries=1, retry_delay=0)
        self.assertIsNotNone(r)
        self.assertEqual(r["session_id"], "f0702a2b")

    def test_budget_is_only_15s(self):
        # Pin the too-short budget so the fix (patient wait for explicit sessions)
        # is a visible, deliberate change: 10 retries x 1.5s = 15s.
        import inspect
        sig = inspect.signature(guard._resolve_session_by_id)
        self.assertEqual(sig.parameters["max_retries"].default, 10)
        self.assertEqual(sig.parameters["retry_delay"].default, 1.5)


class TestPatientSessionWait121(unittest.TestCase):
    """The FIX: for an EXPLICIT (harness-vouched) session id, wait patiently for the
    lazily-created JSONL instead of dying at 15s — bounded by Claude liveness (when
    known) and a generous env-tunable budget."""

    SESS = {"session_id": "f0702a2b", "path": Path("/x/f0702a2b.jsonl"),
            "size": 0, "project": "p"}

    def test_patient_wait_picks_up_late_jsonl(self):
        # The JSONL appears only after the initial 15s budget (poll >12, past the
        # 10-retry initial attempt) — the patient wait keeps polling and resolves it.
        polls = {"n": 0}

        def late_find():
            polls["n"] += 1
            return [self.SESS] if polls["n"] > 12 else []

        with patch.object(guard, "find_sessions", side_effect=late_find), \
             patch.object(guard.time, "sleep"), \
             patch.dict(os.environ, {"COZEMPIC_SESSION_WAIT_SECONDS": "3600"}):
            r = guard._resolve_session_patiently("f0702a2b")
        self.assertIsNotNone(r, "FIX: patient wait must resolve a lazily-created JSONL")
        self.assertEqual(r["session_id"], "f0702a2b")

    def test_patient_wait_exits_when_claude_pid_dead(self):
        # A known-but-dead claude_pid means the session is gone — don't wait it out.
        with patch.object(guard, "find_sessions", return_value=[]), \
             patch.object(guard, "_pid_is_alive", return_value=False), \
             patch.object(guard.time, "sleep"):
            r = guard._resolve_session_patiently("f0702a2b", claude_pid=999999)
        self.assertIsNone(r)

    def test_patient_wait_gives_up_after_budget(self):
        # Truly-abandoned session (file never appears) → bounded give-up, no hang.
        with patch.object(guard, "find_sessions", return_value=[]), \
             patch.object(guard.time, "sleep"), \
             patch.dict(os.environ, {"COZEMPIC_SESSION_WAIT_SECONDS": "30"}):
            r = guard._resolve_session_patiently("nope")
        self.assertIsNone(r)

    def test_patient_wait_disabled_when_budget_zero(self):
        with patch.object(guard, "find_sessions", return_value=[]), \
             patch.object(guard.time, "sleep"), \
             patch.dict(os.environ, {"COZEMPIC_SESSION_WAIT_SECONDS": "0"}):
            r = guard._resolve_session_patiently("f0702a2b")
        self.assertIsNone(r)

    def test_wait_budget_rejects_nonfinite_and_clamps(self):
        # Same gate-disable class as the other COZEMPIC_* knobs: a NaN/inf budget
        # would make `waited < budget` never bound the loop.
        for bad in ("nan", "NaN", "inf", "-inf", "1e999", "-5", "abc", ""):
            with patch.dict(os.environ, {"COZEMPIC_SESSION_WAIT_SECONDS": bad}):
                v = guard._session_wait_budget()
                self.assertTrue(math.isfinite(v) and v >= 0, f"bad {bad!r} leaked {v}")
        with patch.dict(os.environ, {"COZEMPIC_SESSION_WAIT_SECONDS": "999999"}):
            self.assertEqual(guard._session_wait_budget(), 3600.0)  # clamped
        with patch.dict(os.environ, {"COZEMPIC_SESSION_WAIT_SECONDS": "120"}):
            self.assertEqual(guard._session_wait_budget(), 120.0)


if __name__ == "__main__":
    unittest.main()
