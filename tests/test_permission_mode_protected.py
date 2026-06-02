"""RED tests for P0-D root-cause fix: 'permission-mode' missing from gentle._META_TYPES.

All three tests MUST fail at base commit 1b8b863 (v1.8.18 + merged #108) because:
  - test_permission_mode_in_meta_types: 'permission-mode' is absent from gentle._META_TYPES
  - test_compact_summary_keeps_last_permission_mode: compact-summary-collapse drops it
  - test_last_of_type_tag_protects_permission_mode: executor has no tagging machinery

These become GREEN after:
  Commit 1 — gentle.py: add 'permission-mode' to _META_TYPES
  Commit 2 — helpers.py + executor.py: _METADATA_SINGLETON_KEY + _tag_last_of_metadata_types
"""

from __future__ import annotations

import pytest


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_msg(idx: int, msg_type: str, uuid_val: str = "", **extra) -> tuple[int, dict, int]:
    """Build a (line_index, dict, byte_size) triple for test sessions."""
    import json
    d: dict = {"type": msg_type}
    if uuid_val:
        d["uuid"] = uuid_val
    d.update(extra)
    return (idx, d, len(json.dumps(d)))


# ── Class 1: gentle._META_TYPES presence ────────────────────────────────────

class TestPermissionModeInMetaTypes:
    def test_permission_mode_in_meta_types(self):
        """RED at base: 'permission-mode' is absent from gentle._META_TYPES."""
        from cozempic.strategies import gentle
        # gentle._META_TYPES is defined INSIDE strategy_compact_summary_collapse
        # (a local set). We verify via introspection or by triggering the strategy.
        # Access via the function's source inspection is fragile; instead build a
        # session and confirm the strategy keeps the last permission-mode entry.
        # This test checks module-level behaviour — if _META_TYPES is made module-
        # level (acceptable refactor), the assertion below also verifies it.
        src = gentle.strategy_compact_summary_collapse.__code__.co_consts
        # The local _META_TYPES set is stored as a frozenset in co_consts by CPython.
        # Individual strings in the set are NOT listed separately; they live inside
        # the frozenset literal. Check the frozenset directly.
        meta_types_sets = [c for c in src if isinstance(c, frozenset)]
        assert meta_types_sets, (
            "No frozenset found in strategy_compact_summary_collapse code constants. "
            "The _META_TYPES set definition may have changed form."
        )
        # At least one frozenset in the function should contain 'permission-mode'
        found = any("permission-mode" in fs for fs in meta_types_sets)
        assert found, (
            f"'permission-mode' is not in the _META_TYPES frozenset inside "
            f"strategy_compact_summary_collapse (found frozensets: {meta_types_sets}). "
            "Add 'permission-mode' to _META_TYPES — losing it silently breaks resume."
        )


# ── Class 2: compact-summary-collapse end-to-end ────────────────────────────

class TestCompactSummaryKeepsLastPermissionMode:
    def _build_session(self):
        """Return a session with a compact_boundary + 2 permission-mode entries.

        Layout (line indices 0-6):
          0  system/compact_boundary  uuid=cb-001
          1  user                     uuid=u-001  (isCompactSummary — kept by guard)
          2  permission-mode          uuid=pm-001  (older, MAY drop)
          3  user                     uuid=u-002
          4  assistant                uuid=a-001
          5  permission-mode          uuid=pm-002  (LAST — must survive)
          6  user                     uuid=u-003
        """
        import json
        entries = [
            (0, {"type": "system", "subtype": "compact_boundary", "uuid": "cb-001"}, 40),
            (1, {"type": "user", "isCompactSummary": True, "uuid": "u-001",
                 "message": {"content": "summary", "role": "user"}}, 60),
            (2, {"type": "permission-mode", "uuid": "pm-001", "parentUuid": "cb-001"}, 40),
            (3, {"type": "user", "uuid": "u-002", "parentUuid": "u-001",
                 "message": {"content": "hello", "role": "user"}}, 50),
            (4, {"type": "assistant", "uuid": "a-001", "parentUuid": "u-002",
                 "message": {"content": "hi", "role": "assistant"}}, 50),
            (5, {"type": "permission-mode", "uuid": "pm-002", "parentUuid": "a-001"}, 40),
            (6, {"type": "user", "uuid": "u-003", "parentUuid": "pm-002",
                 "message": {"content": "go", "role": "user"}}, 50),
        ]
        sized = []
        for idx, msg, _ in entries:
            sized.append((idx, msg, len(json.dumps(msg))))
        return sized

    def test_compact_summary_keeps_last_permission_mode(self):
        """RED at base: compact-summary-collapse drops pm-002 (last permission-mode)."""
        import cozempic.strategies  # noqa: F401 — ensure strategies registered
        from cozempic.executor import run_prescription
        from cozempic.config import FloorConfig

        session = self._build_session()

        # Run compact-summary-collapse only (no floor to avoid noise).
        result, _ = run_prescription(
            session,
            ["compact-summary-collapse"],
            {},
            floor_config=FloorConfig.disabled(),
        )

        surviving_uuids = {msg.get("uuid") for _, msg, _ in result}
        assert "pm-002" in surviving_uuids, (
            "Last permission-mode entry (pm-002) was dropped by compact-summary-collapse. "
            "Fix: add 'permission-mode' to gentle._META_TYPES."
        )


# ── Class 3: singleton tag protection ────────────────────────────────────────

class TestLastOfTypeTagProtectsPermissionMode:
    def _build_session_5pm(self):
        """Return a session with 5 permission-mode entries + minimal conversation."""
        import json
        entries = [
            (0, {"type": "permission-mode", "uuid": "pm-1", "parentUuid": None}, 40),
            (1, {"type": "user", "uuid": "u-1", "parentUuid": "pm-1",
                 "message": {"content": "a", "role": "user"}}, 50),
            (2, {"type": "assistant", "uuid": "a-1", "parentUuid": "u-1",
                 "message": {"content": "b", "role": "assistant"}}, 50),
            (3, {"type": "permission-mode", "uuid": "pm-2", "parentUuid": "a-1"}, 40),
            (4, {"type": "user", "uuid": "u-2", "parentUuid": "pm-2",
                 "message": {"content": "c", "role": "user"}}, 50),
            (5, {"type": "assistant", "uuid": "a-2", "parentUuid": "u-2",
                 "message": {"content": "d", "role": "assistant"}}, 50),
            (6, {"type": "permission-mode", "uuid": "pm-3", "parentUuid": "a-2"}, 40),
            (7, {"type": "user", "uuid": "u-3", "parentUuid": "pm-3",
                 "message": {"content": "e", "role": "user"}}, 50),
            (8, {"type": "assistant", "uuid": "a-3", "parentUuid": "u-3",
                 "message": {"content": "f", "role": "assistant"}}, 50),
            (9, {"type": "permission-mode", "uuid": "pm-4", "parentUuid": "a-3"}, 40),
            (10, {"type": "user", "uuid": "u-4", "parentUuid": "pm-4",
                  "message": {"content": "g", "role": "user"}}, 50),
            (11, {"type": "assistant", "uuid": "a-4", "parentUuid": "u-4",
                  "message": {"content": "h", "role": "assistant"}}, 50),
            (12, {"type": "permission-mode", "uuid": "pm-5", "parentUuid": "a-4"}, 40),
        ]
        sized = []
        for idx, msg, _ in entries:
            sized.append((idx, msg, len(json.dumps(msg))))
        return sized

    def test_last_of_type_tag_protects_permission_mode(self):
        """RED at base: executor has no tagging machinery; pm-5 can be dropped."""
        import cozempic.strategies  # noqa: F401
        from cozempic.executor import run_prescription, _tag_last_of_metadata_types
        from cozempic.config import FloorConfig

        session = self._build_session_5pm()

        # Verify the tag function exists and tags pm-5 as the last permission-mode
        _tag_last_of_metadata_types(session)
        tagged = [msg for _, msg, _ in session if msg.get("__cozempic_metadata_singleton__")]
        tagged_uuids = {msg.get("uuid") for msg in tagged}
        assert "pm-5" in tagged_uuids, (
            "_tag_last_of_metadata_types did not tag pm-5 as the last permission-mode. "
            "Fix: add 'permission-mode' to _LAST_OF_TYPE_PROTECTED in executor.py."
        )

        # Clean up the tags we set (run_prescription will handle this normally)
        for _, msg, _ in session:
            msg.pop("__cozempic_metadata_singleton__", None)

        # Now run through the full prescription and confirm pm-5 survives
        result, _ = run_prescription(
            session,
            ["compact-summary-collapse"],
            {},
            floor_config=FloorConfig.disabled(),
        )
        surviving_uuids = {msg.get("uuid") for _, msg, _ in result}
        assert "pm-5" in surviving_uuids, (
            "Last permission-mode (pm-5) was dropped even though _tag_last_of_metadata_types "
            "should protect it via is_protected()."
        )

    def test_singleton_tag_does_not_leak_to_output(self):
        """The internal __cozempic_metadata_singleton__ tag must never appear in run_prescription output."""
        import cozempic.strategies  # noqa: F401
        from cozempic.executor import run_prescription
        from cozempic.config import FloorConfig

        session = self._build_session_5pm()
        result, _ = run_prescription(
            session,
            ["compact-summary-collapse"],
            {},
            floor_config=FloorConfig.disabled(),
        )
        for _, msg, _ in result:
            assert "__cozempic_metadata_singleton__" not in msg, (
                f"Internal singleton tag leaked to output on msg uuid={msg.get('uuid')!r}"
            )
        # Also verify source session is clean
        for _, msg, _ in session:
            assert "__cozempic_metadata_singleton__" not in msg, (
                f"Internal singleton tag leaked back to source messages on uuid={msg.get('uuid')!r}"
            )
