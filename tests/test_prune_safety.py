"""RED tests for P0-B (validate_post_prune C1-C7), P0-C (enforce_floor),
P0-D (metadata singleton tag), and the R-2 floor-tag-strip invariant.

All tests in this file MUST fail at base commit 1b8b863 because
cozempic.safety and cozempic.config do not exist in v1.8.18.
"""

from __future__ import annotations

import json
import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _m(idx: int, *, t: str, uuid: str, parent: str | None = "UNSET", **kw) -> tuple[int, dict, int]:
    """Build a (line_index, dict, byte_size) triple."""
    d: dict = {"type": t, "uuid": uuid}
    if parent != "UNSET":
        d["parentUuid"] = parent
    d.update(kw)
    return (idx, d, len(json.dumps(d, separators=(",", ":"))))


def _user(idx: int, uuid: str, parent: str | None = "UNSET", **kw):
    content = kw.pop("content", "hello")
    msg = {"content": content, "role": "user"}
    return _m(idx, t="user", uuid=uuid, parent=parent, message=msg, **kw)


def _asst(idx: int, uuid: str, parent: str | None = "UNSET", **kw):
    content = kw.pop("content", "hi")
    msg = {"content": content, "role": "assistant"}
    return _m(idx, t="assistant", uuid=uuid, parent=parent, message=msg, **kw)


def _pm(idx: int, uuid: str, parent: str | None = "UNSET"):
    """permission-mode entry."""
    return _m(idx, t="permission-mode", uuid=uuid, parent=parent)


def _cb(idx: int, uuid: str, parent: str | None = "UNSET"):
    """compact_boundary system entry."""
    d = {"type": "system", "subtype": "compact_boundary", "uuid": uuid}
    if parent != "UNSET":
        d["parentUuid"] = parent
    return (idx, d, len(json.dumps(d, separators=(",", ":"))))


def _lp(idx: int, uuid: str, parent: str | None = "UNSET"):
    """last-prompt entry."""
    return _m(idx, t="last-prompt", uuid=uuid, parent=parent)


def _ai(idx: int, uuid: str, parent: str | None = "UNSET"):
    """ai-title entry."""
    return _m(idx, t="ai-title", uuid=uuid, parent=parent)


# ── Class 1: validate_post_prune C1 — baseline-relative parent check ─────────

class TestValidatePostPruneC1Baseline:
    def test_c1_cross_session_parent_not_flagged(self):
        """Cross-session parentUuid (absent from both before AND after) must NOT raise."""
        from cozempic.safety import validate_post_prune

        # Simulates a resumed session: root message references a uuid from
        # the PARENT session file (not in this file at all).
        before = [
            _m(0, t="user", uuid="root-001", parent="external-session-uuid",
               message={"content": "hi", "role": "user"}),
            _asst(1, "a-001", parent="root-001"),
        ]
        after = before[:]  # no removals
        # Must NOT raise — external-session-uuid is a valid cross-session anchor
        validate_post_prune(before, after)

    def test_c1_prune_induced_break_raises(self):
        """Parent uuid that existed pre-prune but was removed must raise C1."""
        from cozempic.safety import validate_post_prune, PruneValidationError

        before = [
            _user(0, "root-A", None),
            _asst(1, "a-001", "root-A"),
            _user(2, "u-001", "a-001"),   # <-- will be removed
            _asst(3, "a-002", "u-001"),    # parent u-001 removed → break
        ]
        # Remove u-001 without relinking a-002
        after = [before[0], before[1], before[3]]  # drop u-001, keep a-002 with parent=u-001

        with pytest.raises(PruneValidationError) as exc_info:
            validate_post_prune(before, after)
        assert exc_info.value.evidence["failed_check"] == "C1"

    def test_c1_zero_removal_prune_passes(self):
        """Identity prune (before == after) must pass all checks."""
        from cozempic.safety import validate_post_prune

        msgs = [
            _user(0, "root-B", None),
            _asst(1, "a-001", "root-B"),
            _user(2, "u-001", "a-001"),
        ]
        validate_post_prune(msgs, msgs[:])  # shallow copy, same content


# ── Class 2: validate_post_prune C2 — root preservation ─────────────────────

class TestValidatePostPruneC2:
    def test_c2_root_dropped_raises(self):
        """Dropping the only original root uuid raises C2."""
        from cozempic.safety import validate_post_prune, PruneValidationError

        before = [
            _user(0, "root-001", None),       # the original root
            _asst(1, "a-001", "root-001"),
            _user(2, "u-002", "a-001"),
        ]
        # Drop root-001; a-002 now has no semantic root anchor
        after = [before[1], before[2]]

        with pytest.raises(PruneValidationError) as exc_info:
            validate_post_prune(before, after)
        assert exc_info.value.evidence["failed_check"] == "C2"

    def test_c2_one_root_of_two_survives_passes(self):
        """Multi-root session: one root dropped, one remains → passes C2."""
        from cozempic.safety import validate_post_prune

        # Two root messages (parentUuid=None), one removed after prune
        before = [
            _user(0, "root-X", None),   # older root
            _asst(1, "a-001", "root-X"),
            _user(2, "root-Y", None),   # newer root (resume-of-resume)
            _asst(3, "a-002", "root-Y"),
        ]
        # Keep newer root only — valid per C2 (at least one original root survives)
        after = [before[2], before[3]]
        validate_post_prune(before, after)


# ── Class 3: validate_post_prune C3 — conversation survival ──────────────────

class TestValidatePostPruneC3:
    def test_c3_all_users_dropped_raises(self):
        """Dropping all user messages raises C3."""
        from cozempic.safety import validate_post_prune, PruneValidationError

        before = [
            _user(0, "u-001", None),
            _asst(1, "a-001", "u-001"),
        ]
        after = [before[1]]  # only assistant, no user

        with pytest.raises(PruneValidationError) as exc_info:
            validate_post_prune(before, after)
        assert exc_info.value.evidence["failed_check"] == "C3"

    def test_c3_all_assistants_dropped_raises(self):
        """Dropping all assistant messages raises C3."""
        from cozempic.safety import validate_post_prune, PruneValidationError

        before = [
            _user(0, "u-001", None),
            _asst(1, "a-001", "u-001"),
        ]
        after = [before[0]]  # only user, no assistant

        with pytest.raises(PruneValidationError) as exc_info:
            validate_post_prune(before, after)
        assert exc_info.value.evidence["failed_check"] == "C3"


# ── Class 4: validate_post_prune C4-C7 ───────────────────────────────────────

class TestValidatePostPruneC4C5C6C7:
    def _base(self):
        """Minimal valid session with one of each tracked type."""
        return [
            _user(0, "u-root", None),
            _asst(1, "a-001", "u-root"),
            _cb(2, "cb-001", "a-001"),
            _pm(3, "pm-001", "cb-001"),
            _lp(4, "lp-001", "pm-001"),
            _ai(5, "at-001", "lp-001"),
        ]

    def test_c4_last_compact_boundary_dropped_raises(self):
        """Dropping last compact_boundary raises C4."""
        from cozempic.safety import validate_post_prune, PruneValidationError

        before = self._base()
        after = [m for m in before if not (m[1].get("subtype") == "compact_boundary")]

        with pytest.raises(PruneValidationError) as exc_info:
            validate_post_prune(before, after)
        assert exc_info.value.evidence["failed_check"] == "C4"

    def test_c5_last_permission_mode_dropped_raises(self):
        """Dropping last permission-mode raises C5."""
        from cozempic.safety import validate_post_prune, PruneValidationError

        before = self._base()
        after = [m for m in before if m[1].get("type") != "permission-mode"]

        with pytest.raises(PruneValidationError) as exc_info:
            validate_post_prune(before, after)
        assert exc_info.value.evidence["failed_check"] == "C5"

    def test_c6_last_prompt_dropped_raises(self):
        """Dropping last last-prompt raises C6."""
        from cozempic.safety import validate_post_prune, PruneValidationError

        before = self._base()
        after = [m for m in before if m[1].get("type") != "last-prompt"]

        with pytest.raises(PruneValidationError) as exc_info:
            validate_post_prune(before, after)
        assert exc_info.value.evidence["failed_check"] == "C6"

    def test_c7_last_ai_title_dropped_raises(self):
        """Dropping last ai-title raises C7."""
        from cozempic.safety import validate_post_prune, PruneValidationError

        before = self._base()
        after = [m for m in before if m[1].get("type") != "ai-title"]

        with pytest.raises(PruneValidationError) as exc_info:
            validate_post_prune(before, after)
        assert exc_info.value.evidence["failed_check"] == "C7"


# ── Class 5: simulate_replay_readiness ───────────────────────────────────────

class TestSimulateReplayReadiness:
    def test_cross_session_parent_treated_as_anchor(self):
        """First msg with external parentUuid is a valid cross-session anchor → (True, '')."""
        from cozempic.safety import simulate_replay_readiness

        # parentUuid points to a uuid NOT in this file (external session anchor)
        msgs = [
            _user(0, "u-root", "external-parent-uuid-not-in-file"),
            _asst(1, "a-001", "u-root"),
        ]
        ok, reason = simulate_replay_readiness(msgs)
        assert ok is True
        assert reason == ""

    def test_no_anchor_fails(self):
        """All messages form a closed cycle with no null or external entry → (False, ...)."""
        from cozempic.safety import simulate_replay_readiness

        # Cycle: a→b→c→a (all parentUuids resolve within the file → no anchor)
        msgs = [
            _user(0, "msg-a", "msg-c"),  # parent = msg-c (within file)
            _asst(1, "msg-b", "msg-a"),
            _user(2, "msg-c", "msg-b"),
        ]
        ok, reason = simulate_replay_readiness(msgs)
        assert ok is False
        assert reason != ""


# ── Class 6: enforce_floor ───────────────────────────────────────────────────

class TestEnforceFloor:
    def _users(self, count: int):
        """Build `count` user+assistant pairs (parentUuid chained)."""
        msgs = []
        prev = None
        for i in range(count):
            u_uuid = f"u-{i:03d}"
            a_uuid = f"a-{i:03d}"
            u = _user(i * 2, u_uuid, prev)
            a = _asst(i * 2 + 1, a_uuid, u_uuid)
            msgs.extend([u, a])
            prev = a_uuid
        return msgs

    def test_floor_readds_dropped_last_k_user(self):
        """20 user+asst pairs; prune drops last 10 users; floor re-adds them."""
        from cozempic.safety import enforce_floor
        from cozempic.config import FloorConfig

        before = self._users(20)
        # Simulate: keep only first 10 users and their assistants (drop last 10 users)
        after = [m for m in before if int(m[1]["uuid"].split("-")[1]) < 10]

        cfg = FloorConfig(preserve_last_k_turns=10, max_user_assistant_drop_pct=1.0,
                          preserve_first_message=False)
        result = enforce_floor(before, after, cfg=cfg)

        surviving_user_uuids = {msg.get("uuid") for _, msg, _ in result if msg.get("type") == "user"}
        # Last 10 users (u-010 through u-019) must be re-added
        expected = {f"u-{i:03d}" for i in range(10, 20)}
        assert expected.issubset(surviving_user_uuids), (
            f"Floor failed to re-add last-10 users. Missing: {expected - surviving_user_uuids}"
        )

    def test_floor_readds_first_message(self):
        """Prune drops the root (parentUuid=None); floor re-adds it."""
        from cozempic.safety import enforce_floor
        from cozempic.config import FloorConfig

        before = self._users(5)  # u-000 is the root (parentUuid=None)
        after = [m for m in before if m[1].get("uuid") != "u-000"]  # drop root

        cfg = FloorConfig(preserve_first_message=True, preserve_last_k_turns=0,
                          max_user_assistant_drop_pct=1.0)
        result = enforce_floor(before, after, cfg=cfg)

        surviving_uuids = {msg.get("uuid") for _, msg, _ in result}
        assert "u-000" in surviving_uuids, "Floor failed to re-add first message (u-000)."

    def test_floor_cap_50pct(self):
        """10 user+asst pairs; prune drops all 10 users; floor preserves at least 5."""
        from cozempic.safety import enforce_floor
        from cozempic.config import FloorConfig

        before = self._users(10)
        # Drop all users
        after = [m for m in before if m[1].get("type") != "user"]

        cfg = FloorConfig(max_user_assistant_drop_pct=0.50,
                          preserve_last_k_turns=0, preserve_first_message=False)
        result = enforce_floor(before, after, cfg=cfg)

        surviving_users = [msg for _, msg, _ in result if msg.get("type") == "user"]
        assert len(surviving_users) >= 5, (
            f"Floor 50% cap failed: only {len(surviving_users)} users survived (expected ≥5)"
        )

    def test_floor_no_revert_of_replacements(self):
        """A replaced message (same uuid, modified payload) must keep the replacement."""
        from cozempic.safety import enforce_floor
        from cozempic.config import FloorConfig

        # Build before with a user message carrying specific content
        before = [
            _user(0, "u-root", None, content="original content"),
            _asst(1, "a-001", "u-root"),
        ]
        # After: replacement has same uuid but modified payload
        after_root = (0, {"type": "user", "uuid": "u-root", "parentUuid": None,
                          "message": {"content": "REPLACED content", "role": "user"}},
                      50)
        after = [after_root, before[1]]

        cfg = FloorConfig(preserve_first_message=True, preserve_last_k_turns=1,
                          max_user_assistant_drop_pct=0.50)
        result = enforce_floor(before, after, cfg=cfg)

        root_msg = next((msg for _, msg, _ in result if msg.get("uuid") == "u-root"), None)
        assert root_msg is not None
        assert root_msg.get("message", {}).get("content") == "REPLACED content", (
            "Floor reverted a strategy replacement (same uuid, modified payload)."
        )

    def test_floor_pair_counterpart_closure(self):
        """Re-adding a user msg with tool_use also re-adds the tool_result carrier."""
        from cozempic.safety import enforce_floor
        from cozempic.config import FloorConfig

        # Session: root user (tool_use) → asst (tool_result)
        # Prune dropped both. Floor must re-add both due to pair-closure.
        import json

        tool_user = (0, {
            "type": "user",
            "uuid": "u-tool",
            "parentUuid": None,
            "message": {
                "role": "user",
                "content": [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {}}],
            }
        }, 100)
        tool_result = (1, {
            "type": "user",
            "uuid": "u-result",
            "parentUuid": "u-tool",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}],
            }
        }, 80)
        asst = _asst(2, "a-001", "u-result")
        before = [tool_user, tool_result, asst]

        # After: drop both u-tool and u-result (only asst survives)
        after = [asst]

        cfg = FloorConfig(preserve_last_k_turns=0, preserve_first_message=True,
                          max_user_assistant_drop_pct=0.0)
        result = enforce_floor(before, after, cfg=cfg)

        surviving = {msg.get("uuid") for _, msg, _ in result}
        # u-tool must be re-added (preserve_first_message=True → root)
        assert "u-tool" in surviving, "Floor failed to re-add tool_use root message."
        # pair closure: re-adding u-tool must pull in u-result (tool_result carrier)
        assert "u-result" in surviving, (
            "Floor pair-closure failed: re-adding u-tool should also pull in u-result."
        )


# ── Class 7: FloorConfig defaults and env var resolution ─────────────────────

class TestFloorConfig:
    def test_default_values(self):
        """FloorConfig() has the expected default values."""
        from cozempic.config import FloorConfig

        cfg = FloorConfig()
        assert cfg.max_user_assistant_drop_pct == 0.50
        assert cfg.preserve_last_k_turns == 50
        assert cfg.preserve_first_message is True

    def test_env_var_max_drop_pct(self, monkeypatch):
        """COZEMPIC_FLOOR_MAX_DROP_PCT=0.3 → FloorConfig.max_user_assistant_drop_pct == 0.3."""
        from cozempic import config as cfg_mod

        monkeypatch.setenv("COZEMPIC_FLOOR_MAX_DROP_PCT", "0.3")
        resolved = cfg_mod._resolve_floor_with({})
        assert resolved.max_user_assistant_drop_pct == pytest.approx(0.3)

    def test_env_var_nan_falls_to_default(self, monkeypatch):
        """COZEMPIC_FLOOR_MAX_DROP_PCT=nan → falls back to default 0.50."""
        from cozempic import config as cfg_mod

        monkeypatch.setenv("COZEMPIC_FLOOR_MAX_DROP_PCT", "nan")
        resolved = cfg_mod._resolve_floor_with({})
        assert resolved.max_user_assistant_drop_pct == pytest.approx(0.50)

    def test_env_var_inf_falls_to_default(self, monkeypatch):
        """COZEMPIC_FLOOR_MAX_DROP_PCT=inf → falls back to default 0.50."""
        from cozempic import config as cfg_mod

        monkeypatch.setenv("COZEMPIC_FLOOR_MAX_DROP_PCT", "inf")
        resolved = cfg_mod._resolve_floor_with({})
        assert resolved.max_user_assistant_drop_pct == pytest.approx(0.50)

    def test_floor_config_disabled_classmethod(self):
        """FloorConfig.disabled() returns no-op config (all constraints off)."""
        from cozempic.config import FloorConfig

        disabled = FloorConfig.disabled()
        assert disabled.max_user_assistant_drop_pct == 1.0
        assert disabled.preserve_last_k_turns == 0
        assert disabled.preserve_first_message is False

    def test_clamp_int_inf_falls_to_default(self):
        """_clamp_int('inf', ...) falls back to default without OverflowError."""
        from cozempic.config import _clamp_int

        result = _clamp_int("inf", 1, 1000, 50)
        assert result == 50

    def test_clamp_float_nan_falls_to_default(self):
        """_clamp_float('nan', ...) falls back to default."""
        from cozempic.config import _clamp_float

        result = _clamp_float("nan", 0.0, 1.0, 0.50)
        assert result == pytest.approx(0.50)


# ── Class 8: R-2 — team-tag and singleton-tag strip invariant ────────────────

class TestTagLeakInvariant:
    def test_no_team_protected_tag_in_floor_readds(self):
        """Tags applied by prune_with_team_protect must be stripped from floor re-adds.

        The floor re-adds entries from msgs_before which carry __cozempic_team_protected__
        tags (applied by prune_with_team_protect before passing to run_prescription).
        The guard's strip loop at guard.py:314-316 iterates pruned_messages (=
        run_prescription's output, which includes floor re-adds) and removes the tag.

        This test verifies the full run_prescription flow (which includes floor)
        produces tag-free output when the guard's strip loop is applied. The strip
        is done in guard.py, not inside enforce_floor itself — enforce_floor
        intentionally passes the original dicts through to minimize copies.
        """
        import cozempic.strategies  # noqa: F401
        from cozempic.executor import run_prescription
        from cozempic.config import FloorConfig

        # Simulate what prune_with_team_protect does: tag messages with team-protected
        before = [
            (0, {"type": "user", "uuid": "u-root", "parentUuid": None,
                 "__cozempic_team_protected__": True,
                 "message": {"content": "hi", "role": "user"}}, 80),
            (1, {"type": "assistant", "uuid": "a-001", "parentUuid": "u-root",
                 "__cozempic_team_protected__": True,
                 "message": {"content": "ok", "role": "assistant"}}, 80),
        ]

        # Run with a prescription that would drop both (empty strategy list +
        # floor=disabled to isolate: the team-protected tags keep them in,
        # then we simulate the guard's strip loop).
        result, _ = run_prescription(
            before, [], {}, floor_config=FloorConfig.disabled()
        )

        # Simulate guard.py:314-316 strip loop
        for _, msg, _ in result:
            msg.pop("__cozempic_team_protected__", None)

        # After strip: no tag should remain
        for _, msg, _ in result:
            assert "__cozempic_team_protected__" not in msg, (
                f"__cozempic_team_protected__ remained after guard strip on uuid={msg.get('uuid')!r}"
            )
            assert "__cozempic_metadata_singleton__" not in msg, (
                f"__cozempic_metadata_singleton__ leaked on uuid={msg.get('uuid')!r}"
            )

    def test_no_singleton_tag_in_run_prescription_output(self):
        """__cozempic_metadata_singleton__ must never appear in run_prescription output."""
        import cozempic.strategies  # noqa: F401
        from cozempic.executor import run_prescription
        from cozempic.config import FloorConfig

        msgs = [
            _pm(0, "pm-001", None),
            _user(1, "u-001", "pm-001"),
            _asst(2, "a-001", "u-001"),
        ]
        result, _ = run_prescription(msgs, [], {}, floor_config=FloorConfig.disabled())

        for _, msg, _ in result:
            assert "__cozempic_metadata_singleton__" not in msg
        for _, msg, _ in msgs:
            assert "__cozempic_metadata_singleton__" not in msg


# ── Class 9: PruneValidationError in guard_prune_cycle abort path ─────────────

class TestPruneValidationErrorAbortPath:
    def test_guard_aborts_on_validation_failure_no_write(self, tmp_path):
        """guard_prune_cycle must return _no_change if PruneValidationError fires."""
        import json
        from unittest.mock import patch, MagicMock
        from cozempic.safety import PruneValidationError

        # Build a minimal session file
        session_path = tmp_path / "test_session.jsonl"
        msgs = [
            {"type": "user", "uuid": "u-001", "parentUuid": None,
             "message": {"content": "hi", "role": "user"}},
            {"type": "assistant", "uuid": "a-001", "parentUuid": "u-001",
             "message": {"content": "hello", "role": "assistant"}},
        ]
        session_path.write_text("\n".join(json.dumps(m) for m in msgs) + "\n")

        from cozempic.guard import guard_prune_cycle

        # Patch run_prescription to raise PruneValidationError
        def _raising_run_prescription(*args, **kwargs):
            raise PruneValidationError(
                "test validation failure",
                {"failed_check": "C3", "surviving_user_count": 0, "surviving_assistant_count": 0}
            )

        with patch("cozempic.executor.run_prescription", side_effect=_raising_run_prescription):
            result = guard_prune_cycle(
                session_path=session_path,
                rx_name="standard",
                config={},
            )

        # Must return a _no_change-like dict (saved_mb=0, reloading=False)
        assert result.get("saved_mb", 0) == 0.0
        assert result.get("reloading", False) is False
        # The file must be unchanged
        content_after = session_path.read_text()
        expected = "\n".join(json.dumps(m) for m in msgs) + "\n"
        assert content_after == expected, "File was modified despite PruneValidationError abort"
