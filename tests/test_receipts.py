"""Tests for receipt persistence (receipts.py) — D1 of the dashboard path."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cozempic.metrics import ClaudeMetricsAdapter, TriggerInfo, ValidationInfo, validate_receipt
from cozempic.receipts import (
    INDEX_FILENAME,
    _session_stem,
    emit_receipt,
    receipts_dir,
    receipts_enabled,
    write_receipt,
)
from cozempic.types import PruneAction, PrescriptionResult, StrategyResult


def _result():
    s = StrategyResult("tool-output-trim", [PruneAction(1, "replace", "t", 1000, 200, {})],
                       1000, 200, 1, 0, 1, "trimmed")
    return PrescriptionResult("standard", [s], 5000, 4000, 10, 9, 2000, 1500,
                              "exact", "claude-opus-4-8", 200000)


def _emit(tmp, **kw):
    defaults = dict(
        adapter=ClaudeMetricsAdapter(),
        session_id="sess-1",
        trigger=TriggerInfo("manual", "standard", "standard", "test"),
        base_dir=Path(tmp),
        tool_version="1.8.32",
    )
    defaults.update(kw)
    return emit_receipt(_result(), **defaults)


class TestWriteReceipt(unittest.TestCase):
    def setUp(self):
        # ensure opt-out env is not set from the ambient environment
        self._patch = patch.dict(os.environ, {}, clear=False)
        self._patch.start()
        os.environ.pop("COZEMPIC_NO_RECEIPTS", None)

    def tearDown(self):
        self._patch.stop()

    def test_receipt_files_and_dir_are_user_only(self):
        # de-identified but local provenance — must not be world-readable on a
        # shared host (receipt file 0o600, receipts dir 0o700).
        import stat

        with tempfile.TemporaryDirectory() as tmp:
            path = _emit(tmp)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)  # no group/other
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode) & 0o077, 0)

    def test_emit_creates_session_file_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _emit(tmp)
            self.assertIsNotNone(path)
            self.assertTrue(path.exists())
            self.assertEqual(path.parent, receipts_dir(Path(tmp)))
            # one receipt line, valid JSON, and a structurally valid receipt
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            validate_receipt(rec)  # the emitted receipt passes the D0 contract guard
            self.assertEqual(rec["agent"]["name"], "claude")
            self.assertEqual(rec["tokens"]["reclaimed"], 500)
            # index summary present
            idx = (receipts_dir(Path(tmp)) / INDEX_FILENAME).read_text().splitlines()
            self.assertEqual(len(idx), 1)
            summary = json.loads(idx[0])
            self.assertEqual(summary["outcome"], "committed")
            self.assertEqual(summary["tokens_reclaimed"], 500)

    def test_appends_multiple_receipts_same_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            _emit(tmp)
            path = _emit(tmp)
            self.assertEqual(len(path.read_text().splitlines()), 2)
            idx = (receipts_dir(Path(tmp)) / INDEX_FILENAME).read_text().splitlines()
            self.assertEqual(len(idx), 2)

    def test_filename_is_hashed_not_raw_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _emit(tmp, session_id="my-secret-session")
            self.assertNotIn("my-secret-session", path.name)
            self.assertNotIn("secret", path.read_text())

    def test_deferred_outcome_emits_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _emit(
                tmp,
                outcome="deferred",
                validation=ValidationInfo(passed=False, deferred=True, defer_reason="locked"),
            )
            rec = json.loads(path.read_text().splitlines()[0])
            self.assertEqual(rec["outcome"], "deferred")

    def test_optout_env_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"COZEMPIC_NO_RECEIPTS": "1"}):
                self.assertFalse(receipts_enabled())
                self.assertIsNone(_emit(tmp))
            self.assertFalse((receipts_dir(Path(tmp))).exists())

    def test_write_receipt_returns_none_on_bad_dir(self):
        # base_dir points at an existing FILE → mkdir fails → None, no raise
        with tempfile.NamedTemporaryFile() as f:
            r = {"session": {"id_hash": "sha256:abc"}, "ts": "t", "receipt_id": "i",
                 "agent": {"name": "claude"}, "outcome": "committed",
                 "trigger": {"tier": "x"}, "tokens": {"reclaimed": 0}, "bytes": {"reclaimed": 0}}
            self.assertIsNone(write_receipt(r, base_dir=Path(f.name)))

    def test_emit_never_raises_on_garbage_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            # adapter missing required attrs → build_receipt raises internally →
            # emit must swallow and return None
            class Broken:
                pass

            self.assertIsNone(_emit(tmp, adapter=Broken()))

    def test_tool_version_autofilled_when_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _emit(tmp, tool_version=None)
            rec = json.loads(path.read_text().splitlines()[0])
            self.assertEqual(rec["tool"]["name"], "cozempic")
            self.assertTrue(rec["tool"]["version"])  # non-empty, auto-filled

    def test_survives_preexisting_corrupt_index(self):
        # a torn/garbage index line must not stop a new receipt from being written
        with tempfile.TemporaryDirectory() as tmp:
            d = receipts_dir(Path(tmp))
            d.mkdir(parents=True)
            (d / INDEX_FILENAME).write_text("{ this is not json\n")
            path = _emit(tmp)
            self.assertIsNotNone(path)
            self.assertEqual(len(path.read_text().splitlines()), 1)  # receipt still written


class TestSessionStem(unittest.TestCase):
    def test_strips_sha256_prefix(self):
        self.assertEqual(_session_stem({"session": {"id_hash": "sha256:abc123"}}), "abc123")

    def test_none_and_missing_session(self):
        self.assertEqual(_session_stem({"session": {"id_hash": None}}), "unknown")
        self.assertEqual(_session_stem({}), "unknown")

    def test_sanitizes_path_separators_and_leading_dots(self):
        stem = _session_stem({"session": {"id_hash": "../../etc/passwd"}})
        self.assertNotIn("/", stem)
        self.assertNotIn("\\", stem)
        self.assertFalse(stem.startswith("."))  # cannot become a dotfile / escape dir

    def test_truncated_to_32(self):
        self.assertLessEqual(len(_session_stem({"session": {"id_hash": "x" * 100}})), 32)


if __name__ == "__main__":
    unittest.main()
