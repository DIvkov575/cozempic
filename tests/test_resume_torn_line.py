"""#147: torn-trailing-line repair — shared helper, guard reload path, doctor check."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cozempic.session import repair_torn_trailing_line


def _write(path, lines):
    path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")


def _valid(uuid, parent=None):
    return json.dumps({"type": "user", "uuid": uuid, "parentUuid": parent,
                       "message": {"role": "user", "content": "hi"}})


class TestRepairHelper(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cz147_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_drops_torn_trailing_line(self):
        p = self.tmp / "s.jsonl"
        _write(p, [_valid("a"), _valid("b", "a"), '{"type":"user","uuid":"c","par'])  # torn
        self.assertTrue(repair_torn_trailing_line(p))
        lines = p.read_text().splitlines()
        self.assertEqual(len(lines), 2)  # torn line dropped
        for l in lines:
            json.loads(l)  # all remaining lines valid -> resumable
        # original preserved as .torn.bak
        self.assertTrue((self.tmp / "s.jsonl.torn.bak").exists())

    def test_noop_when_last_line_valid(self):
        p = self.tmp / "s.jsonl"
        _write(p, [_valid("a"), _valid("b", "a")])
        before = p.read_text()
        self.assertFalse(repair_torn_trailing_line(p))  # nothing torn
        self.assertEqual(p.read_text(), before)  # untouched
        self.assertFalse((self.tmp / "s.jsonl.torn.bak").exists())

    def test_does_not_blank_single_torn_line(self):
        p = self.tmp / "s.jsonl"
        _write(p, ['{"torn'])  # only line is torn
        self.assertFalse(repair_torn_trailing_line(p))  # too destructive -> leave it
        self.assertTrue(p.read_text())  # not blanked

    def test_tolerates_trailing_blank_lines_after_torn(self):
        p = self.tmp / "s.jsonl"
        p.write_text(_valid("a") + "\n" + '{"torn' + "\n\n", encoding="utf-8")
        self.assertTrue(repair_torn_trailing_line(p))
        self.assertEqual(p.read_text().splitlines(), [_valid("a")])

    def test_missing_file_returns_false(self):
        self.assertFalse(repair_torn_trailing_line(self.tmp / "nope.jsonl"))

    def test_empty_file_returns_false(self):
        p = self.tmp / "e.jsonl"
        p.write_text("")
        self.assertFalse(repair_torn_trailing_line(p))


class TestDoctorUnresumable(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cz147d_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _session(self, name, lines):
        p = self.tmp / name
        _write(p, lines)
        return {"session_id": name.replace(".jsonl", ""), "path": p}

    def test_check_flags_and_fix_repairs(self):
        from cozempic import doctor

        torn = self._session("aaaaaaaa-torn.jsonl", [_valid("a"), '{"type":"user","par'])
        clean = self._session("bbbbbbbb-ok.jsonl", [_valid("x")])
        with patch.object(doctor, "find_sessions", return_value=[torn, clean]):
            res = doctor.check_unresumable_session()
            self.assertEqual(res.status, "issue")
            self.assertIn("aaaaaaaa", res.message)
            msg = doctor.fix_unresumable_session()
            self.assertIn("Repaired 1", msg)
            # re-check is clean now
            self.assertEqual(doctor.check_unresumable_session().status, "ok")
        # clean session untouched (no .torn.bak)
        self.assertFalse((self.tmp / "bbbbbbbb-ok.jsonl.torn.bak").exists())

    def test_check_ok_when_all_clean(self):
        from cozempic import doctor

        s = self._session("cccccccc.jsonl", [_valid("a"), _valid("b", "a")])
        with patch.object(doctor, "find_sessions", return_value=[s]):
            self.assertEqual(doctor.check_unresumable_session().status, "ok")

    def test_registered_in_all_checks(self):
        from cozempic import doctor

        names = [n for n, _, _ in doctor.ALL_CHECKS]
        self.assertIn("unresumable-session", names)
        # it has a fix fn wired
        fix = dict((n, f) for n, _, f in doctor.ALL_CHECKS)["unresumable-session"]
        self.assertIsNotNone(fix)


class TestGuardRepairsOnTerminate(unittest.TestCase):
    """The guard's deferred-write conflict path repairs a torn trailing line."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cz147g_"))
        self.session = self.tmp / "fake.jsonl"
        # a clean file + a torn trailing line, simulating Claude killed mid-append
        self.session.write_text(_valid("a") + "\n" + _valid("b", "a") + "\n"
                                + '{"type":"user","uuid":"c","par', encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_conflict_skip_repairs_torn_line(self):
        from cozempic.guard import guard_prune_cycle
        from cozempic.session import PruneConflictError
        from cozempic.team import TeamState
        from types import SimpleNamespace

        team = MagicMock(spec=TeamState)
        team.is_empty.return_value = True
        team.team_name = None
        team.message_count = 0
        orig = [(0, {"type": "user"}, 100_000)]
        pruned = [(0, {"type": "user"}, 40_000)]
        totals = iter([100_000, 40_000])

        def est(*a, **k):
            try:
                t = next(totals)
            except StopIteration:
                t = 40_000
            return SimpleNamespace(total=t, context_pct=0.0, method="exact",
                                   confidence="high", model="claude-opus-4-8", context_window=200000)

        with patch("cozempic.guard._guard_tmp_root", return_value=self.tmp), \
                patch("cozempic.guard.load_messages_and_snapshot", return_value=(orig, MagicMock())), \
                patch("cozempic.guard.load_messages", return_value=orig), \
                patch("cozempic.guard.prune_with_team_protect", return_value=(pruned, [], team)), \
                patch("cozempic.guard.snapshot_session", return_value=MagicMock()), \
                patch("cozempic.tokens.estimate_session_tokens", side_effect=est), \
                patch("cozempic.tokens.calibrate_ratio", return_value=0.5), \
                patch("cozempic.guard.save_messages", side_effect=PruneConflictError("changed")):
            result = guard_prune_cycle(session_path=self.session, rx_name="gentle",
                                       config=None, auto_reload=False)
            writer = result.get("_deferred_writer")
            self.assertIsNotNone(writer)
            writer()  # conflict -> skip write -> repair torn line

        # the torn trailing line is gone; the file is now fully parseable
        lines = self.session.read_text().splitlines()
        for l in lines:
            json.loads(l)
        self.assertEqual(len(lines), 2)

    def test_oserror_skip_repairs_torn_line(self):
        # the OSError branch (disk-full/EIO at the post-kill write) also leaves
        # Claude's file in place -> it must repair the torn line too, and must
        # not raise (it runs after terminate, before the resume watcher spawns).
        from cozempic.guard import guard_prune_cycle
        from cozempic.team import TeamState
        from types import SimpleNamespace

        team = MagicMock(spec=TeamState)
        team.is_empty.return_value = True
        team.team_name = None
        team.message_count = 0
        orig = [(0, {"type": "user"}, 100_000)]
        pruned = [(0, {"type": "user"}, 40_000)]
        totals = iter([100_000, 40_000])

        def est(*a, **k):
            try:
                t = next(totals)
            except StopIteration:
                t = 40_000
            return SimpleNamespace(total=t, context_pct=0.0, method="exact",
                                   confidence="high", model="claude-opus-4-8", context_window=200000)

        with patch("cozempic.guard._guard_tmp_root", return_value=self.tmp), \
                patch("cozempic.guard.load_messages_and_snapshot", return_value=(orig, MagicMock())), \
                patch("cozempic.guard.load_messages", return_value=orig), \
                patch("cozempic.guard.prune_with_team_protect", return_value=(pruned, [], team)), \
                patch("cozempic.guard.snapshot_session", return_value=MagicMock()), \
                patch("cozempic.tokens.estimate_session_tokens", side_effect=est), \
                patch("cozempic.tokens.calibrate_ratio", return_value=0.5), \
                patch("cozempic.guard.save_messages", side_effect=OSError("disk full")):
            result = guard_prune_cycle(session_path=self.session, rx_name="gentle",
                                       config=None, auto_reload=False)
            result.get("_deferred_writer")()  # OSError -> skip write -> repair, must not raise

        lines = self.session.read_text().splitlines()
        for l in lines:
            json.loads(l)
        self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
