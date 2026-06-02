"""Round-2 RED tests for C-1 (CRITICAL) + M-2 + H-1 abort-contract.

RED = these tests FAIL against the current pre-fix implementation and PASS
after the fix. The failure mode is PruneValidationError raised on legitimate
orphan-shell scenarios (assertion-RED, not import-RED).

M-3 compliance: docstrings document the NAIVE behavior that causes the failure
vs what the CORRECT implementation must do. Additional tests in
TestAssertionRedProofs demonstrate the naive behavior inline.
"""

from __future__ import annotations

import json
import pytest


# ── Helpers ─────────────────────────────────────────────────────────────────

def _m(idx, *, t, uuid, parent="UNSET", **kw):
    d = {"type": t, "uuid": uuid}
    if parent != "UNSET":
        d["parentUuid"] = parent
    d.update(kw)
    return (idx, d, len(json.dumps(d, separators=(",", ":"))))


def _user(idx, uuid, parent="UNSET", *, content="hi"):
    return _m(idx, t="user", uuid=uuid, parent=parent,
              message={"content": content, "role": "user"})


def _asst(idx, uuid, parent="UNSET"):
    return _m(idx, t="assistant", uuid=uuid, parent=parent,
              message={"content": "ok", "role": "assistant"})


def _tool_result_root(idx, uuid, tool_use_id, *, parent="UNSET"):
    """User message whose sole content is a cross-session tool_result.

    Shape of the first message in a RESUMED session: the tool_result's
    tool_use_id references the prior session's tool_use call (not in this
    file). orphan-fix legitimately strips the block and drops the shell.
    """
    msg = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}],
    }
    return _m(idx, t="user", uuid=uuid, parent=parent, message=msg)


# ── Class 1: C-1 orphan-shell root must NOT trigger C2/C1 aborts ─────────────

class TestOrphanShellRootExclusion:
    """Assertion-RED against pre-fix validate_post_prune.

    NAIVE C2: checks `original_root_uuids & surviving_uuids` without knowing
    the orphan-shell root was legitimately dropped by orphan-fix.
    → Raises C2 for every resumed session whose root is a cross-session tool_result.

    NAIVE C1: `parent ∈ before_uuids AND parent ∉ surviving_uuids → raise`.
    Does not exclude orphan-shell parents → raises C1 for children of dropped orphan-shells.

    CORRECT: compute legit_removed_orphan_shells, exclude from eligible_roots (C2)
    and from the dangling-parent check (C1).

    Tests that must be assertion-RED (FAIL pre-fix, PASS post-fix):
      - test_c2_orphan_shell_root_dropped_must_not_raise
      - test_c1_child_of_orphan_shell_root_must_not_raise
      - test_run_prescription_on_resumed_session_must_not_raise

    Tests that must stay GREEN both pre and post fix (regression guards):
      - test_c2_non_orphan_root_dropped_still_raises
      - test_c1_real_prune_induced_break_still_raises
    """

    def _resumed_session(self):
        """Minimal resumed-session shape (4 messages).

        Layout:
          0  user  root   parentUuid=None  content=[tool_result(tool_use_id='ext-1')]
          1  asst  a-001  parentUuid=root
          2  user  u-001  parentUuid=a-001
          3  asst  a-002  parentUuid=u-001

        'ext-1' is NOT in any message in this file → cross-session orphan.
        orphan-fix drops root's only block → empty → drops the whole message.
        """
        return [
            _tool_result_root(0, "root", "ext-1", parent=None),
            _asst(1, "a-001", "root"),
            _user(2, "u-001", "a-001"),
            _asst(3, "a-002", "u-001"),
        ]

    def test_c2_orphan_shell_root_dropped_must_not_raise(self):
        """RED: validate_post_prune raises C2 when orphan-shell root is absent from after.

        PRE-FIX behavior (assertion-RED):
          original_root_uuids = {'root'}, surviving_uuids ∌ 'root'
          → C2 fires: "every original session root uuid was dropped".
        POST-FIX behavior (GREEN):
          'root' ∈ legit_removed_orphan_shells → excluded from eligible_roots
          → C2 check skipped → no raise.

        This test FAILS pre-fix (PruneValidationError raised unexpectedly)
        and PASSES post-fix (no exception raised).
        """
        from cozempic.safety import validate_post_prune

        before = self._resumed_session()
        # Simulate what happens after orphan-fix drops root: 'root' absent from after.
        # Relinking: a-001's parentUuid stays 'root' here — we test validate directly.
        after = [before[1], before[2], before[3]]

        # POST-FIX: must NOT raise. PRE-FIX: raises C2 → this test FAILS.
        validate_post_prune(before, after)

    def test_c1_child_of_orphan_shell_root_must_not_raise(self):
        """RED: C1 fires when a survivor's parent is the dropped orphan-shell root.

        PRE-FIX behavior:
          'root' ∈ before_uuids, 'root' ∉ surviving_uuids → C2 fires first
          (same scenario). Even if C2 were fixed, naive C1 would then fire
          on a-001.parentUuid='root'.
        POST-FIX behavior:
          'root' ∈ legit_removed_orphan_shells → excluded from C2 eligible_roots
          AND skipped in C1 dangling-parent check → no raise.

        This test FAILS pre-fix (PruneValidationError raised) and PASSES post-fix.
        """
        from cozempic.safety import validate_post_prune

        before = self._resumed_session()
        # a-001 still has parentUuid='root' (as if not relinked yet).
        a001 = (1, {"type": "assistant", "uuid": "a-001", "parentUuid": "root",
                    "message": {"content": "ok", "role": "assistant"}}, 50)
        after = [a001, before[2], before[3]]

        # POST-FIX: must NOT raise. PRE-FIX: raises (C2 or C1).
        validate_post_prune(before, after)

    def test_c2_non_orphan_root_dropped_still_raises(self):
        """Regression guard: a genuine (non-orphan-shell) root drop must STILL raise C2.

        This test PASSES both pre-fix AND post-fix. It guards against an over-broad
        fix that exempts ALL root drops instead of only orphan-shell drops.
        """
        from cozempic.safety import validate_post_prune, PruneValidationError

        before = [
            _user(0, "real-root", None),  # normal user msg — NOT an orphan shell
            _asst(1, "a-001", "real-root"),
            _user(2, "u-001", "a-001"),
        ]
        after = [before[1], before[2]]  # real-root dropped by strategy

        with pytest.raises(PruneValidationError) as exc_info:
            validate_post_prune(before, after)
        assert exc_info.value.evidence["failed_check"] == "C2"

    def test_c1_real_prune_induced_break_still_raises(self):
        """Regression guard: a genuine prune-induced chain break must STILL raise C1.

        PASSES both pre-fix and post-fix. Guards against over-broad orphan exclusion.
        """
        from cozempic.safety import validate_post_prune, PruneValidationError

        before = [
            _user(0, "root", None),
            _asst(1, "a-001", "root"),
            _user(2, "u-001", "a-001"),   # strategy-dropped (non-orphan)
            _asst(3, "a-002", "u-001"),    # parent u-001 gone → dangling
        ]
        after = [before[0], before[1], before[3]]  # u-001 dropped, a-002 NOT relinked

        with pytest.raises(PruneValidationError) as exc_info:
            validate_post_prune(before, after)
        assert exc_info.value.evidence["failed_check"] == "C1"


class TestOrphanShellNonRoot:
    """M-2: non-root orphan-shell messages also trigger false-positive C1 aborts.

    Assertion-RED: the current C1 raises on a legitimate mid-chain orphan-shell drop.
    """

    def test_non_root_orphan_shell_dropped_must_not_cause_c1(self):
        """RED: C1 fires when a survivor's parent is a dropped non-root orphan-shell.

        PRE-FIX: 'u-orphan' ∈ before_uuids, 'u-orphan' ∉ surviving_uuids
          → u-001.parentUuid='u-orphan' triggers C1.
        POST-FIX: 'u-orphan' ∈ legit_removed_orphan_shells → skip in C1 → no raise.

        FAILS pre-fix (PruneValidationError raised), PASSES post-fix.
        """
        from cozempic.safety import validate_post_prune

        # u-orphan: a mid-chain user whose sole content is a cross-session tool_result
        u_orphan = (2, {
            "type": "user",
            "uuid": "u-orphan",
            "parentUuid": "a-001",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "ext-2", "content": "y"}],
            }
        }, 80)

        before = [
            _user(0, "root", None),
            _asst(1, "a-001", "root"),
            u_orphan,
            _user(3, "u-001", "u-orphan"),   # child of orphan-shell
            _asst(4, "a-002", "u-001"),
        ]
        # After orphan-fix drops u-orphan; u-001 still references it (pre-relink state).
        after = [before[0], before[1], before[3], before[4]]

        # POST-FIX: must NOT raise. PRE-FIX: raises C1.
        validate_post_prune(before, after)


class TestFullPipelineOrphanShell:
    """C-1 end-to-end: run_prescription must NOT raise on resumed sessions."""

    def _build_resumed_session(self, n_pairs: int = 5):
        """Session whose root is a cross-session tool_result carrier."""
        msgs = []
        root = (0, {
            "type": "user",
            "uuid": "root",
            "parentUuid": None,
            "message": {
                "role": "user",
                "content": [{"type": "tool_result",
                             "tool_use_id": "cross-session-tool",
                             "content": "done"}],
            },
        }, 120)
        msgs.append(root)

        prev = "root"
        for i in range(n_pairs):
            a_uuid = f"a-{i:03d}"
            u_uuid = f"u-{i:03d}"
            msgs.append(_asst(len(msgs), a_uuid, prev))
            msgs.append(_user(len(msgs), u_uuid, a_uuid))
            prev = u_uuid
        return msgs

    def test_run_prescription_on_resumed_session_must_not_raise(self):
        """RED: run_prescription raises C2 on a resumed session (pre-fix).

        PRE-FIX: orphan-fix drops root → root absent from surviving → C2 fires.
        POST-FIX: root ∈ legit_removed_orphan_shells → C2 skipped → no raise.

        This reproduces the reviewer's live-reproduced abort.
        FAILS pre-fix (PruneValidationError raised), PASSES post-fix.
        """
        import cozempic.strategies  # noqa: F401
        from cozempic.executor import run_prescription

        session = self._build_resumed_session(n_pairs=5)
        # POST-FIX: must NOT raise. PRE-FIX: raises C2 (or C1).
        run_prescription(session, ["compact-summary-collapse"], {})


# ── H-1: rewrite abort-contract test ─────────────────────────────────────────

class TestGuardAbortContractRewritten:
    """H-1: correct mock target + 3 assertions.

    The old test patched cozempic.executor.run_prescription. Guard.py binds
    via `from .executor import run_prescription` at import time, so the mock
    never fires. The test was GREEN via the saved_bytes<=0 early-return, NOT
    via the abort branch.

    This rewrite patches cozempic.guard.prune_with_team_protect (what guard
    actually calls) and asserts all three abort-contract invariants.
    """

    def test_abort_contract_correct_mock_target(self, tmp_path):
        """Abort-contract: correct patch target + 3 invariants.

        (a) result contains 'validation_error' + 'evidence' keys
        (b) session file is byte-identical before and after
        (c) _terminate_and_resume was NOT called
        """
        import json as _json
        from unittest.mock import patch
        from cozempic.safety import PruneValidationError
        from cozempic.guard import guard_prune_cycle

        # Large enough session that saved_bytes won't be ≤0 if the mock fires
        msgs = [
            {"type": "user", "uuid": "u-001", "parentUuid": None,
             "message": {"content": "hi " * 200, "role": "user"}},
            {"type": "assistant", "uuid": "a-001", "parentUuid": "u-001",
             "message": {"content": "ok " * 200, "role": "assistant"}},
        ]
        session_path = tmp_path / "test.jsonl"
        content = "\n".join(_json.dumps(m) for m in msgs) + "\n"
        session_path.write_bytes(content.encode())
        bytes_before = session_path.read_bytes()

        def _raising(*args, **kwargs):
            raise PruneValidationError(
                "forced validation failure",
                {"failed_check": "C2", "surviving_count": 0},
            )

        with patch("cozempic.guard.prune_with_team_protect",
                   side_effect=_raising) as mock_pwtp, \
             patch("cozempic.guard._terminate_and_resume") as mock_tar:
            result = guard_prune_cycle(
                session_path=session_path,
                rx_name="standard",
                config={},
            )

        # (a) abort branch keys
        assert "validation_error" in result, (
            "guard_prune_cycle abort path did not set 'validation_error'. "
            "The abort branch at guard.py:988+ was not executed."
        )
        assert "evidence" in result, "abort path did not set 'evidence' key"
        assert result["evidence"]["failed_check"] == "C2"
        assert result.get("saved_mb", 0) == 0.0
        assert result.get("reloading", False) is False

        # (b) file untouched
        assert session_path.read_bytes() == bytes_before, (
            "Session file was modified despite PruneValidationError abort."
        )

        # (c) Claude not killed
        mock_tar.assert_not_called()

    def test_old_mock_target_is_a_noop_for_guard(self, tmp_path):
        """Documents the tautology: patching executor.run_prescription does NOT affect guard.

        Guard.py binds the name via `from .executor import run_prescription` at
        module import time. Patching the executor module attribute after import
        does NOT rebind guard.py's local reference.
        """
        from cozempic import guard as guard_mod

        original_fn = guard_mod.run_prescription
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "cozempic.executor.run_prescription"
        ) as mock_rp:
            current_fn = guard_mod.run_prescription

        # Guard's local name is unchanged by the patch
        assert current_fn is original_fn, (
            "Patching cozempic.executor.run_prescription should NOT rebind "
            "guard.py's already-imported reference."
        )
        mock_rp.assert_not_called()


# ── M-3: assertion-RED proofs (inline naive vs correct) ──────────────────────

class TestAssertionRedProofs:
    """M-3 compliance: prove C1/C2 tests are assertion-RED, not just import-RED.

    These tests document the NAIVE behavior inline so reviewers can verify the
    tests would fail against a wrong implementation.
    """

    def test_naive_c2_structural_check_is_bypassed_by_relink(self):
        """Structural C2 (any parentUuid is None in msgs_after) is bypassable.

        When real-root is dropped and _relink_parent_chain sets a-001.parentUuid=None,
        a naive structural check `any(parentUuid is None)` passes — it sees the
        pseudo-root descendant and thinks there is an anchor.

        The baseline-relative C2 (checks original root uuids) correctly raises C2
        because 'real-root' is not in surviving_uuids.

        This test verifies both: (1) naive check passes (demonstrates the gap),
        (2) our C2 raises (proves we catch what naive misses).
        """
        from cozempic.safety import validate_post_prune, PruneValidationError

        before = [
            _user(0, "real-root", None),
            _asst(1, "a-001", "real-root"),
            _user(2, "u-002", "a-001"),
            _asst(3, "a-002", "u-002"),
        ]
        # real-root dropped; _relink_parent_chain sets a-001.parentUuid=None
        a001_relinked = (1, {
            "type": "assistant",
            "uuid": "a-001",
            "parentUuid": None,  # relinked to None — pseudo-root
            "message": {"content": "ok", "role": "assistant"},
        }, 60)
        after = [a001_relinked, before[2], before[3]]

        # Naive structural check passes (demonstrates the gap naive check has):
        naive_passes = any(m.get("parentUuid") is None for _, m, _ in after)
        assert naive_passes, "Naive structural check PASSES — confirms it misses the root drop"

        # Our baseline-relative C2 catches the dropped original root:
        with pytest.raises(PruneValidationError) as exc_info:
            validate_post_prune(before, after)
        assert exc_info.value.evidence["failed_check"] == "C2", (
            "Baseline-relative C2 must catch the dropped real root even when "
            "a descendant was relinked to parentUuid=None."
        )

    def test_naive_c1_false_positive_on_cross_session_parent(self):
        """Non-baseline-relative C1 would raise on cross-session parents (false positive).

        NAIVE C1: any parent ∉ surviving_uuids → raise (no before_uuids filter).
        This would flag external/prior-session parentUuids as chain breaks.

        Our baseline-relative C1: only raise when parent ∈ before_uuids.
        Cross-session parents (∉ before_uuids) are skipped.

        This test verifies our C1 does NOT raise for cross-session pointers.
        (It's GREEN both pre and post fix — documents that C1 baseline-relative
        logic was already correct in round-1 for the cross-session case.)
        """
        from cozempic.safety import validate_post_prune

        # root references a prior-session uuid (not in before)
        before = [
            _user(0, "root", "external-prior-session-uuid"),
            _asst(1, "a-001", "root"),
        ]
        after = before[:]
        # Must NOT raise — cross-session pointer is NOT a prune-induced break.
        validate_post_prune(before, after)
