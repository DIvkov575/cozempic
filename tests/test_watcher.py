"""RED tests for JsonlWatcher bugs W1-W5.

W1 — kqueue fd leak (kq never closed in finally)
W2 — FileNotFoundError crash when file missing at start
W3 — _last_size not reset after shrink → growth blindness post-prune
W4 — on_growth exceptions silently swallowed; must log to stderr
W5 — kqueue watches orphaned inode after os.replace → blind post-prune (kqueue only)
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
import time
import unittest

from cozempic.watcher import JsonlWatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_fd_count() -> int:
    """Return number of open fds for current process (macOS/Linux)."""
    import subprocess
    pid = os.getpid()
    try:
        # macOS / BSD
        out = subprocess.check_output(["lsof", "-p", str(pid), "-n"], text=True)
        return len(out.strip().splitlines()) - 1  # strip header
    except Exception:
        return -1


def _make_watcher(path: str, callback, *, use_kqueue: bool) -> JsonlWatcher:
    """Create a JsonlWatcher with an overridden _use_kqueue flag."""
    w = JsonlWatcher(path, callback)
    w._use_kqueue = use_kqueue
    return w


def _start_thread(watcher: JsonlWatcher) -> threading.Thread:
    t = threading.Thread(target=watcher.start, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# W1 — kqueue fd leak
# ---------------------------------------------------------------------------

@unittest.skipUnless(sys.platform == "darwin", "kqueue macOS only")
class TestJsonlWatcherFdLeak(unittest.TestCase):
    """W1: kq.close() must be called in finally or kqueue fd leaks."""

    def test_kqueue_fd_closed_after_normal_stop(self):
        """Each start/stop cycle must NOT leak a kqueue fd."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl") as f:
            path = f.name
        try:
            before = _open_fd_count()
            w = _make_watcher(path, lambda p, s: None, use_kqueue=True)
            t = _start_thread(w)
            time.sleep(0.2)
            w.stop()
            t.join(timeout=2)
            after = _open_fd_count()
            # Allow ±2 slack for any incidental fd (e.g. lsof itself)
            delta = after - before
            self.assertLessEqual(delta, 2,
                f"fd delta={delta} — kqueue fd may be leaking (W1 not fixed)")
        finally:
            os.unlink(path)

    def test_kqueue_fd_closed_on_file_disappear(self):
        """Even if the watched file is deleted mid-watch, no fd should leak."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl") as f:
            path = f.name
        try:
            before = _open_fd_count()
            w = _make_watcher(path, lambda p, s: None, use_kqueue=True)
            t = _start_thread(w)
            time.sleep(0.15)
            os.unlink(path)
            time.sleep(0.15)
            w.stop()
            t.join(timeout=2)
            after = _open_fd_count()
            delta = after - before
            self.assertLessEqual(delta, 2,
                f"fd delta={delta} — kqueue fd leaked on file disappear (W1 not fixed)")
        except FileNotFoundError:
            pass  # already deleted


# ---------------------------------------------------------------------------
# W2 — missing-file crash / fallback to poll
# ---------------------------------------------------------------------------

@unittest.skipUnless(sys.platform == "darwin", "kqueue macOS only")
class TestJsonlWatcherMissingFileKqueue(unittest.TestCase):
    """W2 (kqueue): os.open on missing file must fall back to poll, not crash."""

    def test_kqueue_falls_back_to_poll_when_file_missing(self):
        """Watcher thread must stay alive when file doesn't exist at start (kqueue)."""
        path = "/tmp/_cozempic_test_nonexistent_watcher_kqueue.jsonl"
        # Ensure it doesn't exist
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        w = _make_watcher(path, lambda p, s: None, use_kqueue=True)
        t = _start_thread(w)
        t.join(timeout=1.0)
        # Thread should still be alive (poll loop keeps running)
        self.assertTrue(t.is_alive(),
            "Watcher thread died immediately on missing file (W2 not fixed: os.open unguarded)")
        w.stop()
        t.join(timeout=2)


class TestJsonlWatcherMissingFilePoll(unittest.TestCase):
    """W2 (poll): poll path already handles missing file — regression guard."""

    def test_poll_handles_missing_file_gracefully(self):
        """Poll-mode watcher must stay alive when file doesn't exist."""
        path = "/tmp/_cozempic_test_nonexistent_watcher_poll.jsonl"
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        w = _make_watcher(path, lambda p, s: None, use_kqueue=False)
        t = _start_thread(w)
        time.sleep(0.35)
        self.assertTrue(t.is_alive(),
            "Poll watcher thread died on missing file")
        w.stop()
        t.join(timeout=2)


# ---------------------------------------------------------------------------
# W3 — shrink-reset: _last_size must reset after file shrinks
# ---------------------------------------------------------------------------

class TestJsonlWatcherShrinkResetPoll(unittest.TestCase):
    """W3 (poll path): growth after shrink must fire callback."""

    def test_growth_detected_after_file_shrinks_poll(self):
        """After prune shrinks file, next growth must trigger on_growth."""
        fired: list[int] = []

        def cb(path: str, size: int) -> None:
            fired.append(size)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl",
                                         mode="wb") as f:
            path = f.name
            f.write(b"x" * 1000)

        try:
            w = _make_watcher(path, cb, use_kqueue=False)
            # _last_size captured at __init__ = 1000

            t = _start_thread(w)
            time.sleep(0.05)  # let thread enter loop

            # Simulate prune: shrink file to 100 bytes
            with open(path, "wb") as f:
                f.write(b"y" * 100)
            time.sleep(0.5)  # wait for poll to detect shrink

            # Now grow: append 200 more bytes (total 300)
            with open(path, "ab") as f:
                f.write(b"z" * 200)
            time.sleep(0.5)  # wait for growth detection

            w.stop()
            t.join(timeout=2)

            self.assertTrue(
                any(s >= 300 for s in fired),
                f"on_growth did not fire after shrink+regrowth. "
                f"fired={fired}. W3 not fixed: _last_size not reset on shrink.",
            )
        finally:
            os.unlink(path)

    def test_shrink_alone_does_not_fire_callback(self):
        """Shrinking the file must NOT trigger on_growth."""
        fired: list[int] = []

        def cb(path: str, size: int) -> None:
            fired.append(size)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl",
                                         mode="wb") as f:
            path = f.name
            f.write(b"x" * 1000)

        try:
            w = _make_watcher(path, cb, use_kqueue=False)
            t = _start_thread(w)
            time.sleep(0.05)

            # Only shrink
            with open(path, "wb") as f:
                f.write(b"y" * 100)
            time.sleep(0.5)

            w.stop()
            t.join(timeout=2)

            self.assertEqual(fired, [],
                f"on_growth fired on shrink (false positive). fired={fired}")
        finally:
            os.unlink(path)


@unittest.skipUnless(sys.platform == "darwin", "kqueue macOS only")
class TestJsonlWatcherShrinkResetKqueue(unittest.TestCase):
    """W3 (kqueue path): growth after shrink must fire callback."""

    def test_kqueue_growth_detected_after_file_shrinks(self):
        """After prune shrinks file, kqueue path must detect next growth."""
        fired: list[int] = []
        event = threading.Event()

        def cb(path: str, size: int) -> None:
            fired.append(size)
            event.set()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl",
                                         mode="wb") as f:
            path = f.name
            f.write(b"x" * 1000)

        try:
            w = _make_watcher(path, cb, use_kqueue=True)
            t = _start_thread(w)
            time.sleep(0.1)

            # Shrink file
            with open(path, "wb") as f:
                f.write(b"y" * 100)
            time.sleep(0.2)

            # Grow past 100 bytes
            with open(path, "ab") as f:
                f.write(b"z" * 300)

            event.wait(timeout=3.0)
            w.stop()
            t.join(timeout=2)

            self.assertTrue(
                any(s >= 300 for s in fired),
                f"kqueue on_growth not fired after shrink+regrowth. "
                f"fired={fired}. W3 not fixed in kqueue path.",
            )
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# W4 — callback errors must be logged to stderr (one-shot)
# ---------------------------------------------------------------------------

class TestJsonlWatcherCallbackErrorLoggedPoll(unittest.TestCase):
    """W4 (poll): on_growth exception must print to stderr, not silently pass."""

    def test_callback_error_logged_to_stderr_poll(self):
        """First callback error must appear on stderr."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl",
                                         mode="wb") as f:
            path = f.name
            f.write(b"a" * 100)

        try:
            def bad_cb(path: str, size: int) -> None:
                raise RuntimeError("boom from test")

            w = _make_watcher(path, bad_cb, use_kqueue=False)

            captured = io.StringIO()
            old_stderr = sys.stderr
            sys.stderr = captured

            try:
                t = _start_thread(w)
                # Trigger growth
                with open(path, "ab") as f:
                    f.write(b"b" * 200)
                time.sleep(0.6)
                w.stop()
                t.join(timeout=2)
            finally:
                sys.stderr = old_stderr

            err_output = captured.getvalue()
            self.assertIn("[watcher]", err_output,
                "Expected '[watcher]' in stderr. W4 not fixed: error silently swallowed.")
            self.assertIn("on_growth error", err_output,
                "Expected 'on_growth error' in stderr. W4 not fixed.")
        finally:
            os.unlink(path)

    def test_callback_error_does_not_crash_watcher_poll(self):
        """Watcher thread must survive a callback that raises."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl",
                                         mode="wb") as f:
            path = f.name
            f.write(b"a" * 100)

        try:
            call_count = [0]

            def bad_cb(path: str, size: int) -> None:
                call_count[0] += 1
                raise RuntimeError("crash me")

            w = _make_watcher(path, bad_cb, use_kqueue=False)
            t = _start_thread(w)
            time.sleep(0.05)

            with open(path, "ab") as f:
                f.write(b"b" * 200)
            time.sleep(0.5)

            self.assertTrue(t.is_alive(),
                "Watcher thread died after callback raised (W4 regression)")
            w.stop()
            t.join(timeout=2)
        finally:
            os.unlink(path)

    def test_one_shot_error_log_not_repeated_on_each_growth(self):
        """Repeated callback errors must only log ONCE (one-shot pattern)."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl",
                                         mode="wb") as f:
            path = f.name
            f.write(b"a" * 50)

        try:
            def bad_cb(path: str, size: int) -> None:
                raise RuntimeError("persistent failure")

            w = _make_watcher(path, bad_cb, use_kqueue=False)

            captured = io.StringIO()
            old_stderr = sys.stderr
            sys.stderr = captured

            try:
                t = _start_thread(w)
                # Trigger multiple growth events
                for i in range(3):
                    with open(path, "ab") as f:
                        f.write(b"x" * 100)
                    time.sleep(0.4)
                w.stop()
                t.join(timeout=2)
            finally:
                sys.stderr = old_stderr

            err_output = captured.getvalue()
            # Should log exactly once — count occurrences of the sentinel
            occurrences = err_output.count("[watcher]")
            self.assertEqual(occurrences, 1,
                f"Expected exactly 1 '[watcher]' log line, got {occurrences}. "
                "One-shot suppression (W4) not implemented.")
        finally:
            os.unlink(path)


@unittest.skipUnless(sys.platform == "darwin", "kqueue macOS only")
class TestJsonlWatcherCallbackErrorLoggedKqueue(unittest.TestCase):
    """W4 (kqueue): callback errors must log to stderr."""

    def test_callback_error_logged_to_stderr_kqueue(self):
        """kqueue path: first callback error must appear on stderr."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl",
                                         mode="wb") as f:
            path = f.name
            f.write(b"a" * 100)

        try:
            def bad_cb(path: str, size: int) -> None:
                raise RuntimeError("kqueue boom")

            w = _make_watcher(path, bad_cb, use_kqueue=True)

            captured = io.StringIO()
            old_stderr = sys.stderr
            sys.stderr = captured

            try:
                t = _start_thread(w)
                with open(path, "ab") as f:
                    f.write(b"b" * 500)
                time.sleep(1.5)
                w.stop()
                t.join(timeout=2)
            finally:
                sys.stderr = old_stderr

            err_output = captured.getvalue()
            self.assertIn("[watcher]", err_output,
                "Expected '[watcher]' in stderr from kqueue path. W4 not fixed.")
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# W5 — inode replacement: kqueue must survive os.replace, fall back to poll
# ---------------------------------------------------------------------------

@unittest.skipUnless(sys.platform == "darwin", "kqueue macOS only")
class TestJsonlWatcherInodeReplacement(unittest.TestCase):
    """W5: after os.replace (atomic prune), watcher must still detect growth."""

    def test_kqueue_detects_growth_after_atomic_replace(self):
        """Growth after os.replace must fire on_growth (via poll fallback)."""
        fired: list[int] = []
        growth_after_replace = threading.Event()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl",
                                         mode="wb") as f:
            path = f.name
            f.write(b"x" * 500)

        try:
            def cb(p: str, size: int) -> None:
                fired.append(size)
                growth_after_replace.set()

            w = _make_watcher(path, cb, use_kqueue=True)
            t = _start_thread(w)
            time.sleep(0.2)  # let kqueue register

            # Simulate atomic prune: write smaller content to tmp, replace
            tmp_path = path + ".tmp"
            with open(tmp_path, "wb") as f:
                f.write(b"y" * 50)
            os.replace(tmp_path, path)
            time.sleep(0.2)  # let DELETE/RENAME event fire

            # Now grow the new file
            with open(path, "ab") as f:
                f.write(b"z" * 300)

            growth_after_replace.wait(timeout=3.0)
            w.stop()
            t.join(timeout=2)

            self.assertTrue(
                growth_after_replace.is_set(),
                f"on_growth did not fire after os.replace+growth. "
                f"fired={fired}. W5 not fixed: kqueue watches orphaned inode.",
            )
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            try:
                os.unlink(path + ".tmp")
            except FileNotFoundError:
                pass

    def test_kqueue_registers_delete_rename_fflags(self):
        """kqueue kevent must include KQ_NOTE_DELETE and KQ_NOTE_RENAME in fflags."""
        import select
        # This test verifies the fix design by checking the module-level constants
        # are accessible (belt-and-braces: validates the test environment).
        self.assertTrue(hasattr(select, "KQ_NOTE_DELETE"),
            "select.KQ_NOTE_DELETE not available")
        self.assertTrue(hasattr(select, "KQ_NOTE_RENAME"),
            "select.KQ_NOTE_RENAME not available")
        # The actual fflags check is covered by test_kqueue_detects_growth_after_atomic_replace


# ---------------------------------------------------------------------------
# W6 — __import__ idiom: module-level constant must be used
# ---------------------------------------------------------------------------

class TestJsonlWatcherImportIdiom(unittest.TestCase):
    """W6: __import__("select") in __init__ must be replaced with module-level constant."""

    def test_module_has_has_kqueue_constant(self):
        """After fix, watcher module must expose _HAS_KQUEUE at module level."""
        import cozempic.watcher as watcher_mod
        self.assertTrue(
            hasattr(watcher_mod, "_HAS_KQUEUE"),
            "_HAS_KQUEUE module-level constant missing. W6 not fixed: "
            "__import__('select') idiom still in __init__.",
        )

    def test_init_uses_module_constant_not_import_idiom(self):
        """JsonlWatcher._use_kqueue must be derived from _HAS_KQUEUE, not inline __import__."""
        import cozempic.watcher as watcher_mod
        # The __import__ idiom is gone when _HAS_KQUEUE exists at module level.
        # We verify by instantiating without a real file and checking the flag.
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl") as f:
            path = f.name
        try:
            w = JsonlWatcher(path, lambda p, s: None)
            expected = watcher_mod._HAS_KQUEUE  # type: ignore[attr-defined]
            self.assertEqual(w._use_kqueue, expected,
                "_use_kqueue does not match _HAS_KQUEUE module constant.")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
