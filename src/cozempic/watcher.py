"""File watcher for JSONL growth detection.

Uses kqueue on macOS (sub-millisecond latency, 0% CPU idle) with
os.stat() polling fallback on other platforms (200ms interval).

Stdlib only — no dependencies.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable


class JsonlWatcher:
    """Watch a JSONL file for size growth. Sub-second on macOS via kqueue."""

    def __init__(self, filepath: str, on_growth: Callable[[str, int], None]):
        self.filepath = filepath
        self.on_growth = on_growth
        self._running = False
        self._last_size = self._get_size()
        self._use_kqueue = hasattr(__import__("select"), "kqueue")
        self._cb_error_logged = False

    def _get_size(self) -> int:
        try:
            return os.stat(self.filepath).st_size
        except OSError:
            return 0

    def start(self) -> None:
        """Block and watch for file growth. Run in a daemon thread."""
        self._running = True
        if self._use_kqueue:
            self._watch_kqueue()
        else:
            self._watch_poll()

    def stop(self) -> None:
        self._running = False

    def _log_cb_error(self, exc: Exception) -> None:
        """Log the first on_growth callback error to stderr; suppress repeats."""
        if not self._cb_error_logged:
            print(f"  [watcher] on_growth error: {exc}", file=sys.stderr)
            self._cb_error_logged = True

    def _watch_kqueue(self) -> None:
        """macOS kqueue watcher — 0.04ms wake latency, 0% CPU idle.

        Falls back to poll if the watched inode is replaced (os.replace / atomic prune).
        """
        import select

        try:
            fd = os.open(self.filepath, os.O_RDONLY)
        except FileNotFoundError:
            # File not yet created — poll path handles OSError gracefully
            self._watch_poll()
            return
        kq = select.kqueue()
        inode_replaced = False
        try:
            ev = select.kevent(
                fd,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=(select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND
                        | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME),
            )
            while self._running:
                # Block up to 1s, then re-check _running
                events = kq.control([ev], 1, 1.0)
                for event in events:
                    if event.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                        # Inode replaced (atomic prune via os.replace); drop to poll
                        inode_replaced = True
                        break
                    new_size = self._get_size()
                    if new_size < self._last_size:
                        # File shrank (prune completed) — reset baseline
                        self._last_size = new_size
                    if new_size > self._last_size:
                        self._last_size = new_size
                        try:
                            self.on_growth(self.filepath, new_size)
                        except Exception as exc:
                            self._log_cb_error(exc)  # don't crash the watcher thread
                if inode_replaced:
                    break
        finally:
            kq.close()
            os.close(fd)
        if inode_replaced and self._running:
            self._watch_poll()

    def _watch_poll(self) -> None:
        """Fallback polling watcher — 200ms interval."""
        while self._running:
            time.sleep(0.2)
            new_size = self._get_size()
            if new_size < self._last_size:
                # File shrank (prune completed) — reset baseline
                self._last_size = new_size
            if new_size > self._last_size:
                self._last_size = new_size
                try:
                    self.on_growth(self.filepath, new_size)
                except Exception as exc:
                    self._log_cb_error(exc)  # don't crash the watcher thread
