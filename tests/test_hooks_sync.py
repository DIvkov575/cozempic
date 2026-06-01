"""Guard against drift between the two on-disk copies of hooks.json.

`src/cozempic/data/hooks.json` is the canonical source loaded by `cozempic init`
into a user's project. `plugin/hooks/hooks.json` ships via the Claude Code
plugin marketplace. They MUST stay byte-identical so users get the same
behavior regardless of install path. If you edit one, edit both — this test
catches the slip.
"""
import json
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_HOOKS = REPO_ROOT / "src" / "cozempic" / "data" / "hooks.json"
PLUGIN_HOOKS = REPO_ROOT / "plugin" / "hooks" / "hooks.json"


class TestHooksSync(unittest.TestCase):
    def test_both_files_exist(self):
        self.assertTrue(DATA_HOOKS.exists(), f"Missing canonical hooks file: {DATA_HOOKS}")
        self.assertTrue(PLUGIN_HOOKS.exists(), f"Missing plugin hooks file: {PLUGIN_HOOKS}")

    def test_hook_definitions_match(self):
        """The 'hooks' section of both files must be structurally identical."""
        canonical = json.loads(DATA_HOOKS.read_text(encoding="utf-8"))
        plugin = json.loads(PLUGIN_HOOKS.read_text(encoding="utf-8"))
        self.assertEqual(
            canonical.get("hooks"),
            plugin.get("hooks"),
            msg=(
                "data/hooks.json and plugin/hooks/hooks.json have drifted. "
                "After editing one, run: cp plugin/hooks/hooks.json src/cozempic/data/hooks.json"
            ),
        )


    def test_all_hooks_export_no_auto_init(self):
        """Every hook command must set COZEMPIC_NO_AUTO_INIT=1."""
        canonical = json.loads(DATA_HOOKS.read_text(encoding="utf-8"))
        for event, entries in canonical.get("hooks", {}).items():
            for entry in entries:
                for h in entry.get("hooks", []):
                    cmd = h.get("command", "")
                    self.assertIn(
                        "COZEMPIC_NO_AUTO_INIT=1",
                        cmd,
                        msg=f"Hook {event}[{entry.get('matcher', '')}] missing COZEMPIC_NO_AUTO_INIT=1",
                    )

    def test_schema_marker_is_current(self):
        """Every hook command must have the current schema marker."""
        from cozempic.init import HOOK_SCHEMA_MARKER
        canonical = json.loads(DATA_HOOKS.read_text(encoding="utf-8"))
        for event, entries in canonical.get("hooks", {}).items():
            for entry in entries:
                for h in entry.get("hooks", []):
                    cmd = h.get("command", "")
                    self.assertIn(
                        HOOK_SCHEMA_MARKER,
                        cmd,
                        msg=f"Hook {event}[{entry.get('matcher', '')}] has stale schema marker",
                    )


if __name__ == "__main__":
    unittest.main()
