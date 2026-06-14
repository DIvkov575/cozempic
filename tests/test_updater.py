"""Tests for auto-update logic."""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestVersionTuple(unittest.TestCase):
    def test_parses_version(self):
        from cozempic.updater import _version_tuple
        self.assertEqual(_version_tuple("1.2.0"), (1, 2, 0))
        self.assertEqual(_version_tuple("2.0.0"), (2, 0, 0))

    def test_bad_version_returns_zeros(self):
        from cozempic.updater import _version_tuple
        self.assertEqual(_version_tuple("bad"), (0,))


class TestShouldCheck(unittest.TestCase):
    def test_no_cache_file_means_should_check(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / ".cozempic_update_check"
            with patch("cozempic.updater._CACHE_FILE", cache):
                from cozempic.updater import _should_check
                self.assertTrue(_should_check())

    def test_recent_check_means_skip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / ".cozempic_update_check"
            cache.write_text(str(time.time()))
            with patch("cozempic.updater._CACHE_FILE", cache):
                from cozempic.updater import _should_check
                self.assertFalse(_should_check())

    def test_old_check_means_should_check(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / ".cozempic_update_check"
            cache.write_text(str(time.time() - 90000))  # 25 hours ago
            with patch("cozempic.updater._CACHE_FILE", cache):
                from cozempic.updater import _should_check
                self.assertTrue(_should_check())


class TestMaybeAutoUpdate(unittest.TestCase):
    def test_skips_when_env_var_set(self):
        """COZEMPIC_NO_AUTO_UPDATE=1 disables all update activity."""
        with patch.dict(os.environ, {"COZEMPIC_NO_AUTO_UPDATE": "1"}):
            with patch("cozempic.updater._should_check") as mock_check:
                from cozempic.updater import maybe_auto_update
                maybe_auto_update()
                mock_check.assert_not_called()

    def test_works_without_tty(self):
        """Auto-update should work even without TTY (hooks, daemons)."""
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            with patch("cozempic.updater._should_check", return_value=False) as mock_check:
                from cozempic.updater import maybe_auto_update
                maybe_auto_update()
                mock_check.assert_called()  # Should still check (TTY no longer blocks)

    def test_skips_when_already_checked(self):
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = True
            with patch("cozempic.updater._should_check", return_value=False):
                with patch("cozempic.updater._get_latest_version") as mock_get:
                    from cozempic.updater import maybe_auto_update
                    maybe_auto_update()
                    mock_get.assert_not_called()

    def test_skips_when_already_up_to_date(self):
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = True
            with patch("cozempic.updater._should_check", return_value=True), \
                 patch("cozempic.updater._mark_checked"), \
                 patch("cozempic.updater._get_latest_version", return_value="0.0.1"), \
                 patch("cozempic.updater._do_upgrade") as mock_upgrade:
                from cozempic.updater import maybe_auto_update
                maybe_auto_update()
                mock_upgrade.assert_not_called()

    def test_upgrades_when_newer_version_available(self, capsys=None):
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = True
            with patch("cozempic.updater._should_check", return_value=True), \
                 patch("cozempic.updater._mark_checked"), \
                 patch("cozempic.updater._get_latest_version", return_value="99.99.99"), \
                 patch("cozempic.updater._do_upgrade", return_value=True) as mock_upgrade:
                from cozempic.updater import maybe_auto_update
                maybe_auto_update()
                mock_upgrade.assert_called_once_with("99.99.99")

    def test_prints_failure_message_on_upgrade_error(self):
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = True
            calls = []
            mock_stdout.write = lambda s: calls.append(s)
            with patch("cozempic.updater._should_check", return_value=True), \
                 patch("cozempic.updater._mark_checked"), \
                 patch("cozempic.updater._get_latest_version", return_value="99.99.99"), \
                 patch("cozempic.updater._do_upgrade", return_value=False), \
                 patch("builtins.print") as mock_print:
                from cozempic.updater import maybe_auto_update
                maybe_auto_update()
                printed = " ".join(str(a) for call in mock_print.call_args_list for a in call[0])
                self.assertIn("auto-update failed", printed)

    def test_no_op_when_pypi_unreachable(self):
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = True
            with patch("cozempic.updater._should_check", return_value=True), \
                 patch("cozempic.updater._mark_checked"), \
                 patch("cozempic.updater._get_latest_version", return_value=None), \
                 patch("cozempic.updater._do_upgrade") as mock_upgrade:
                from cozempic.updater import maybe_auto_update
                maybe_auto_update()
                mock_upgrade.assert_not_called()


class TestInstallMethodDetection(unittest.TestCase):
    """1.8.22: auto-update must pick a mechanism that matches the install method —
    pip can't upgrade a Homebrew keg or a `uv tool` install (the binary on PATH
    never moves), which is why brew/uvx users silently stayed behind."""

    def _method_for(self, path):
        from cozempic import updater
        with patch.object(updater, "__file__", path):
            return updater._install_method()

    def test_detects_brew_keg(self):
        self.assertEqual(self._method_for(
            "/opt/homebrew/Cellar/cozempic/1.8.22/libexec/lib/python3.12/site-packages/cozempic/updater.py"),
            "brew")

    def test_detects_uv_tool(self):
        self.assertEqual(self._method_for(
            "/Users/x/.local/share/uv/tools/cozempic/lib/python3.12/site-packages/cozempic/updater.py"),
            "uv-tool")

    def test_detects_pipx(self):
        self.assertEqual(self._method_for(
            "/Users/x/.local/pipx/venvs/cozempic/lib/python3.12/site-packages/cozempic/updater.py"),
            "pipx")

    def test_defaults_to_pip(self):
        self.assertEqual(self._method_for(
            "/Users/x/proj/.venv/lib/python3.12/site-packages/cozempic/updater.py"),
            "pip")


class TestDoUpgradeDispatch(unittest.TestCase):
    def test_brew_never_autoruns(self):
        from cozempic import updater
        with patch.object(updater, "_install_method", return_value="brew"), \
             patch("cozempic.updater.subprocess.run") as run:
            self.assertFalse(updater._do_upgrade("9.9.9"))
            run.assert_not_called()

    def test_uv_tool_runs_uv_tool_upgrade(self):
        from cozempic import updater
        with patch.object(updater, "_install_method", return_value="uv-tool"), \
             patch("cozempic.updater.shutil.which", return_value="/usr/bin/uv"), \
             patch("cozempic.updater.subprocess.run",
                   return_value=MagicMock(returncode=0)) as run:
            self.assertTrue(updater._do_upgrade("9.9.9"))
            self.assertEqual(run.call_args[0][0], ["uv", "tool", "upgrade", "cozempic"])

    def test_pip_uses_install_chain(self):
        from cozempic import updater
        with patch.object(updater, "_install_method", return_value="pip"), \
             patch("cozempic.updater.shutil.which", return_value=None), \
             patch("cozempic.updater.subprocess.run",
                   return_value=MagicMock(returncode=0)) as run:
            self.assertTrue(updater._do_upgrade("9.9.9"))
            # first attempt is `pip install cozempic==…` via sys.executable
            self.assertIn("install", run.call_args[0][0])


class TestAutoUpdateOptOuts(unittest.TestCase):
    """#123: the documented kill switch must actually stop the Python updater,
    and COZEMPIC_PIN holds a reviewed version without auto-installing it."""

    def setUp(self):
        for k in ("COZEMPIC_NO_AUTO_UPDATE", "COZEMPIC_PIN"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k in ("COZEMPIC_NO_AUTO_UPDATE", "COZEMPIC_PIN"):
            os.environ.pop(k, None)

    def test_no_auto_update_skips_everything(self):
        from cozempic import updater
        os.environ["COZEMPIC_NO_AUTO_UPDATE"] = "1"
        with patch("cozempic.updater._should_check") as sc, \
             patch("cozempic.updater._do_upgrade") as up:
            updater.maybe_auto_update(force=True)
            sc.assert_not_called()   # returns before even checking
            up.assert_not_called()

    def test_pin_disables_autoupdate(self):
        from cozempic import updater
        os.environ["COZEMPIC_PIN"] = updater.__version__  # pinned to current
        with patch("cozempic.updater._get_latest_version", return_value="99.0.0"), \
             patch("cozempic.updater._do_upgrade") as up, \
             patch("cozempic.updater._should_check", return_value=True):
            updater.maybe_auto_update(force=True)
            up.assert_not_called()   # never upgrades while pinned

    def test_pin_warns_on_drift(self):
        import io
        from cozempic import updater
        os.environ["COZEMPIC_PIN"] = "1.0.0"  # != current installed version
        buf = io.StringIO()
        with patch("sys.stdout", buf), \
             patch("cozempic.updater._should_check", return_value=True), \
             patch("cozempic.updater._mark_checked"), \
             patch("cozempic.updater._do_upgrade") as up:
            updater.maybe_auto_update(force=True)
            up.assert_not_called()
        out = buf.getvalue()
        self.assertIn("pinned to 1.0.0", out)
        self.assertIn("pip install 'cozempic==1.0.0'", out)

    def test_pin_matching_current_is_silent(self):
        import io
        from cozempic import updater
        os.environ["COZEMPIC_PIN"] = updater.__version__
        buf = io.StringIO()
        with patch("sys.stdout", buf), \
             patch("cozempic.updater._should_check", return_value=True), \
             patch("cozempic.updater._do_upgrade"):
            updater.maybe_auto_update(force=True)
        self.assertEqual(buf.getvalue(), "", "no drift warning when pin == current")

    def test_pinned_version_helper(self):
        from cozempic import updater
        self.assertIsNone(updater._pinned_version())
        os.environ["COZEMPIC_PIN"] = " 1.8.30 "
        self.assertEqual(updater._pinned_version(), "1.8.30")  # trimmed


class TestHookHonorsOptOuts(unittest.TestCase):
    """#123 Defect 1: the SessionStart hook's shell upgrade must be gated on the
    SAME env vars the README advertises, not bypass them."""

    def test_sessionstart_upgrade_is_guarded(self):
        import json
        from pathlib import Path
        import cozempic
        hooks = json.loads((Path(cozempic.__file__).parent / "data" / "hooks.json").read_text())
        cmd = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        # The pip --upgrade must be inside a guard that checks both opt-outs.
        guard = 'if [ -z "$COZEMPIC_NO_AUTO_UPDATE" ] && [ -z "$COZEMPIC_PIN" ]; then'
        self.assertIn(guard, cmd)
        # And the guard must come BEFORE the upgrade in the command string.
        self.assertLess(cmd.index(guard), cmd.index("pip install --upgrade cozempic"))


class TestMaybeAutoUpdateBrew(unittest.TestCase):
    def test_brew_prints_hint_and_does_not_attempt(self):
        import io
        from cozempic import updater
        buf = io.StringIO()
        with patch("sys.stdout", buf), \
             patch("cozempic.updater._should_check", return_value=True), \
             patch("cozempic.updater._mark_checked"), \
             patch("cozempic.updater._get_latest_version", return_value="99.0.0"), \
             patch.object(updater, "_install_method", return_value="brew"), \
             patch("cozempic.updater._do_upgrade") as up:
            updater.maybe_auto_update()
            up.assert_not_called()
        out = buf.getvalue()
        # Fully-qualified so brew's untrusted-tap gate doesn't block the upgrade.
        self.assertIn("brew upgrade Ruya-AI/cozempic/cozempic", out)
        self.assertNotIn("updating", out.lower())
