"""F1 fix — RED→GREEN regression guards for Agent-spawned team visibility.

Each test that is a genuine regression guard is PROVEN RED at base 4f15d6d before
any fix is applied; characterization / invariant-preservation tests that pass at
base are clearly labelled as such in their docstrings.

Test classes:
  TestTeamNameExtraction         — P0-A: team_name key mismatch
  TestAgentSpawnRecognition      — P0-B: Agent tool recognition in extract_team_state
  TestSendMessageByNameLookup    — P0-C: by-name SendMessage resolution
  TestIdleNotificationTransition — P0-D: idle_notification terminal transition
  TestSessionScopedAntiWedge     — P0-E: session-scoped anti-wedge in safe_to_reload
  TestFullScenario               — integration: pure-SendMessage team, no shared tasks

Isolation:
  All tests go through _extract() which patches load_team_configs to return []
  (no live ~/.claude/teams/ config merge contamination — L11 isolation principle).
  No real 'cozempic guard --daemon', no os.kill on real PIDs.
"""

import unittest
from pathlib import Path
from unittest.mock import patch


# ─── helpers ────────────────────────────────────────────────────────────────

def _extract(msgs):
    """Call extract_team_state with config isolation (no live ~/.claude/teams/ merge).

    All tests in this module must go through this wrapper to avoid contamination
    from the test machine's real team config.json files — which can cause tests to
    pass at base for the wrong reason (config-merge finding the live config before
    the JSONL extraction bug is fixed). L11 isolation principle.
    """
    from cozempic.team import extract_team_state
    with patch("cozempic.team.load_team_configs", return_value=[]):
        return extract_team_state(msgs)


def _tool_use(id_, name, inp):
    return {"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": id_, "name": name, "input": inp}
    ]}}


def _tool_result(tool_use_id, text):
    return {"message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id,
         "content": text}
    ]}}


def _user_content(text):
    """A user message with a plain-string content (e.g. teammate-message XML)."""
    return {"message": {"role": "user", "content": text}}


# Canonical Agent-spawn result text as observed in real transcripts (2026-06-08)
_SPAWN_RESULT_P1 = (
    "Spawned successfully.\n"
    "agent_id: finder-p1@myteam\n"
    "name: finder-p1\n"
    "team_name: myteam\n"
    "The agent is now running and will receive instructions via mailbox."
)

_SPAWN_RESULT_P2 = (
    "Spawned successfully.\n"
    "agent_id: finder-p2@myteam\n"
    "name: finder-p2\n"
    "team_name: myteam\n"
    "The agent is now running and will receive instructions via mailbox."
)

_IDLE_NOTIFICATION_P2 = (
    '<teammate-message teammate_id="finder-p2" summary="idle">'
    '{"type":"idle_notification","from":"finder-p2","timestamp":"2026-06-08T10:00:00Z","idleReason":"available"}'
    '</teammate-message>'
)


def _spawn_msgs_p1(idx_offset=0):
    """Two messages: Agent tool_use + Agent tool_result for finder-p1."""
    return [
        (idx_offset, _tool_use("u1", "Agent", {"name": "finder-p1", "description": "find bugs"}), 200),
        (idx_offset + 1, _tool_result("u1", _SPAWN_RESULT_P1), 300),
    ]


# ─── TestTeamNameExtraction (P0-A) ──────────────────────────────────────────

class TestTeamNameExtraction(unittest.TestCase):
    """P0-A: TeamCreate emits 'team_name' key, but extract_team_state reads 'name'.

    Regression guard: test_teamcreate_team_name_key_extracted is RED at base
    (extract_team_state returns team_name="" before the fix).

    Invariant test: test_teamcreate_name_key_still_works is GREEN at base
    (backward compat — sessions that already used 'name' keep working).
    """

    def test_teamcreate_team_name_key_extracted(self):
        """REGRESSION GUARD — RED at base: inp.get('name') misses 'team_name' key.

        Before fix: state.team_name == "" (the key 'name' is absent; only
        'team_name' is present, so the fallback in the old code is the empty
        default).  After fix: state.team_name == "myteam".
        """
        msgs = [
            (0, _tool_use("u1", "TeamCreate", {
                "team_name": "myteam",
                "description": "test team",
            }), 100),
        ]
        state = _extract(msgs)
        self.assertEqual(state.team_name, "myteam",
                         "TeamCreate with 'team_name' key must set state.team_name")

    def test_teamcreate_name_key_still_works(self):
        """INVARIANT (GREEN at base): backward compat — 'name' key still works.

        This test documents that sessions using the 'name' key continue to work
        after the fix. It is NOT a regression guard (passes at base).
        """
        msgs = [
            (0, _tool_use("u1", "TeamCreate", {
                "name": "myteam",
                "description": "test team",
            }), 100),
        ]
        state = _extract(msgs)
        self.assertEqual(state.team_name, "myteam",
                         "'name' key fallback must still work for backward compat")

    def test_teamcreate_team_name_key_with_inline_teammates(self):
        """REGRESSION GUARD — RED at base: team_name="" when key is 'team_name'.

        Asserts team_name is set correctly; the teammate is a secondary check.
        """
        msgs = [
            (0, _tool_use("u1", "TeamCreate", {
                "team_name": "audit-team",
                "description": "audit team",
                "teammates": [{"agentId": "finder-p1@audit-team", "name": "finder-p1"}],
            }), 200),
        ]
        state = _extract(msgs)
        self.assertEqual(state.team_name, "audit-team",
                         "team_name must be extracted from 'team_name' key")
        self.assertTrue(any(t.name == "finder-p1" for t in state.teammates),
                        "inline teammate must still be populated")


# ─── TestAgentSpawnRecognition (P0-B) ────────────────────────────────────────

class TestAgentSpawnRecognition(unittest.TestCase):
    """P0-B: 'Agent' tool-use is unrecognised; teammates stay empty.

    All tests in this class are REGRESSION GUARDs — RED at base because
    'Agent' ∉ TEAM_TOOL_NAMES so the spawn is completely invisible.
    """

    def test_agent_spawn_creates_teammate_with_running_status(self):
        """REGRESSION GUARD — RED at base: Agent spawn not recognised; teammates=[].

        After fix: len(state.teammates) >= 1, with a non-benign, non-terminal
        status ('running') that blocks safe_to_reload.
        """
        from cozempic.guard import _TEAMMATE_BENIGN, _STATUS_TERMINAL
        state = _extract(_spawn_msgs_p1())
        self.assertGreaterEqual(len(state.teammates), 1,
                                "Agent spawn must create a TeammateInfo entry")
        mate = next((t for t in state.teammates if t.name == "finder-p1"), None)
        self.assertIsNotNone(mate, "teammate must have name 'finder-p1'")
        s = (mate.status or "").strip().lower()
        self.assertNotIn(s, _TEAMMATE_BENIGN,
                         f"spawn status {s!r} must not be benign; must block reload")
        self.assertNotIn(s, _STATUS_TERMINAL,
                         f"spawn status {s!r} must not be terminal; work is ongoing")

    def test_agent_spawn_sets_team_name_from_result(self):
        """REGRESSION GUARD — RED at base: Agent spawn result not parsed; team_name=''.

        After fix: state.team_name == 'myteam' (parsed from spawn result).
        Only fires if no prior TeamCreate set team_name.
        """
        state = _extract(_spawn_msgs_p1())
        self.assertEqual(state.team_name, "myteam",
                         "team_name must be extracted from the Agent spawn result")

    def test_agent_spawn_real_agent_id_extracted(self):
        """REGRESSION GUARD — RED at base: spawn result not parsed; agentId stays placeholder.

        After fix: the TeammateInfo's agent_id is 'finder-p1@myteam' (the real
        agentId from the result text), not the tool_use_id placeholder.
        """
        state = _extract(_spawn_msgs_p1())
        self.assertGreaterEqual(len(state.teammates), 1)
        # The real agentId format includes the @team suffix
        agent_ids = {t.agent_id for t in state.teammates}
        self.assertIn("finder-p1@myteam", agent_ids,
                      "real agentId 'finder-p1@myteam' must be parsed from spawn result")

    def test_agent_spawn_triggers_unsafe_reload(self):
        """REGRESSION GUARD — RED at base: spawn invisible; safe_to_reload returns True.

        This is the core bug scenario: an active Agent-spawned teammate with no
        completion signal must block safe_to_reload (return False).
        After fix: safe_to_reload returns (False, reason) with 'teammate' in reason.
        """
        from cozempic.guard import safe_to_reload
        msgs = _spawn_msgs_p1()
        state = _extract(msgs)
        safe, reason = safe_to_reload(state, msgs, Path("/tmp/fake_session.jsonl"))
        self.assertFalse(safe,
                         "Active Agent-spawned teammate must block safe_to_reload")
        self.assertIn("teammate", reason,
                      f"block reason must mention 'teammate', got: {reason!r}")

    def test_agent_spawn_pruning_non_regression(self):
        """INVARIANT (GREEN at base, must stay GREEN): 'Agent' must NOT be in TEAM_TOOL_NAMES.

        Q-A decision: 'Agent' is NOT added to TEAM_TOOL_NAMES. It is only used in
        extract_team_state's own scope via _TEAM_EXTRACT_TOOL_NAMES. This ensures
        prune_with_team_protect does NOT over-protect non-team Agent calls.
        """
        from cozempic.team import TEAM_TOOL_NAMES
        self.assertNotIn("Agent", TEAM_TOOL_NAMES,
                         "'Agent' must NOT be in TEAM_TOOL_NAMES (would over-protect "
                         "non-team Agent calls in prune_with_team_protect)")

    def test_non_team_agent_call_not_protected_by_is_team_message(self):
        """INVARIANT (GREEN at base, must stay GREEN): _is_team_message returns False
        for Agent tool_use (since 'Agent' ∉ TEAM_TOOL_NAMES).

        Proves the decouple invariant: extract_team_state can recognize Agent spawns
        via its own internal predicate, while prune_with_team_protect's
        _is_team_message call does NOT tag them as team-protected.
        """
        from cozempic.team import _is_team_message
        agent_msg = {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "u99", "name": "Agent",
             "input": {"name": "solo-agent", "description": "one-off task"}}
        ]}}
        # With no pending_task_ids, _is_team_message must return False for Agent tool_use
        result = _is_team_message(agent_msg, pending_task_ids=set())
        self.assertFalse(result,
                         "_is_team_message must return False for Agent tool_use "
                         "(Agent ∉ TEAM_TOOL_NAMES — decouple invariant)")


# ─── TestSendMessageByNameLookup (P0-C) ─────────────────────────────────────

class TestSendMessageByNameLookup(unittest.TestCase):
    """P0-C: SendMessage to: bare name misses seen_teammates keyed by agentId.

    Regression guards: RED at base because SendMessage to="alice" (bare name)
    misses seen_teammates keyed by "alice@myteam" (full agentId).
    """

    def test_sendmessage_bare_name_reactivates_after_completion(self):
        """REGRESSION GUARD — RED at base: bare-name lookup misses agentId-keyed teammate.

        TeamCreate → SendMessage → completion → SendMessage (re-activate by bare name).
        The second SendMessage must resolve "alice" → "alice@myteam" and set
        status="running" (chronology: last SendMessage is AFTER the completion).

        Before fix: seen_teammates keyed by "alice@myteam"; to="alice" misses
        → the re-activation at line 3 is lost → status stays "completed"
        → safe_to_reload incorrectly says safe (but the teammate was re-activated).
        """
        msgs = [
            (0, _tool_use("u1", "TeamCreate", {
                "team_name": "myteam",
                "teammates": [{"agentId": "alice@myteam", "name": "alice"}],
            }), 100),
            (1, _tool_use("u2", "SendMessage", {"to": "alice", "message": "phase1"}), 100),
            (2, {"type": "queue-operation",
                 "content": ("<task-notification><task-id>alice@myteam</task-id>"
                             "<status>completed</status><summary>done</summary>"
                             "<result>r</result></task-notification>")}, 100),
            (3, _tool_use("u3", "SendMessage", {"to": "alice", "message": "phase2"}), 100),
        ]
        state = _extract(msgs)
        mate = next((t for t in state.teammates
                     if t.name == "alice" or t.agent_id == "alice@myteam"), None)
        self.assertIsNotNone(mate, "alice must be in teammates")
        self.assertEqual(mate.status, "running",
                         "SendMessage to bare 'alice' after completion must re-activate "
                         "teammate back to 'running'")

    def test_sendmessage_to_agent_spawn_by_name(self):
        """REGRESSION GUARD — RED at base: Agent-spawn + SendMessage by bare name.

        After Agent spawns finder-p1 (agentId = "finder-p1@myteam"),
        a SendMessage to="finder-p1" (bare name) must be recognized
        (bare-name → agentId index) and the teammate must remain active.
        """
        msgs = list(_spawn_msgs_p1()) + [
            (2, _tool_use("u2", "SendMessage", {"to": "finder-p1", "message": "start work"}), 100),
        ]
        state = _extract(msgs)
        mate = next((t for t in state.teammates
                     if t.name == "finder-p1" or "finder-p1" in t.agent_id), None)
        self.assertIsNotNone(mate, "finder-p1 must be in teammates after spawn")
        s = (mate.status or "").strip().lower()
        from cozempic.guard import _TEAMMATE_BENIGN, _STATUS_TERMINAL
        self.assertNotIn(s, _TEAMMATE_BENIGN,
                         f"After spawn + SendMessage, status must not be benign, got {s!r}")
        self.assertNotIn(s, _STATUS_TERMINAL,
                         f"After spawn + SendMessage, status must not be terminal, got {s!r}")


# ─── TestIdleNotificationTransition (P0-D) ───────────────────────────────────

class TestIdleNotificationTransition(unittest.TestCase):
    """P0-D: <teammate-message> idle_notification in user messages must transition
    the teammate to 'idle' (benign) status.

    All tests in this class are REGRESSION GUARDs — RED at base because:
    (a) P0-B makes Agent spawns invisible (teammates=[]), and
    (b) the second pass does not scan for idle_notification blocks.
    """

    def _msgs_with_idle(self):
        """Spawn + spawn result + user message with idle_notification for finder-p2."""
        return [
            (0, _tool_use("u1", "Agent", {"name": "finder-p2", "description": "find issues"}), 200),
            (1, _tool_result("u1", _SPAWN_RESULT_P2), 300),
            (2, _user_content(_IDLE_NOTIFICATION_P2), 200),
        ]

    def test_idle_notification_transitions_to_idle(self):
        """REGRESSION GUARD — RED at base: idle_notification invisible; teammate stays 'running'.

        After fix: the teammate's status becomes 'idle' (∈ _TEAMMATE_BENIGN).
        """
        from cozempic.guard import _TEAMMATE_BENIGN
        state = _extract(self._msgs_with_idle())
        mate = next((t for t in state.teammates
                     if t.name == "finder-p2" or "finder-p2" in t.agent_id), None)
        self.assertIsNotNone(mate,
                             "finder-p2 must be in teammates after Agent spawn + result")
        s = (mate.status or "").strip().lower()
        self.assertIn(s, _TEAMMATE_BENIGN,
                      f"idle_notification must transition status to benign, got {s!r}")

    def test_idle_notification_allows_safe_reload(self):
        """REGRESSION GUARD — RED at base: spawn invisible; 'safe' for wrong reason (is_empty).

        After fix: the correct path — teammate exists + is idle → safe.
        The test also verifies state is NOT empty (proving the correct path is taken).
        """
        from cozempic.guard import safe_to_reload
        msgs = self._msgs_with_idle()
        state = _extract(msgs)
        self.assertFalse(state.is_empty(),
                         "state must not be empty after Agent spawn + idle_notification")
        safe, _ = safe_to_reload(state, msgs, Path("/tmp/fake_session.jsonl"))
        self.assertTrue(safe,
                        "After idle_notification, safe_to_reload must return True")

    def test_idle_notification_not_applied_if_later_sendmessage(self):
        """REGRESSION GUARD — RED at base: all events invisible; cannot test chronology.

        After fix: a SendMessage AFTER the idle_notification (re-activation) must
        keep the teammate's status as 'running' (not 'idle'). Chronology guard:
        the SendMessage at line 3 is AFTER the idle at line 2 → stays blocking.
        """
        from cozempic.guard import safe_to_reload, _TEAMMATE_BENIGN
        msgs = [
            (0, _tool_use("u1", "Agent", {"name": "finder-p2", "description": "find issues"}), 200),
            (1, _tool_result("u1", _SPAWN_RESULT_P2), 300),
            (2, _user_content(_IDLE_NOTIFICATION_P2), 200),         # idle at line 2
            (3, _tool_use("u3", "SendMessage", {"to": "finder-p2", "message": "one more task"}), 100),  # re-activate at line 3
        ]
        state = _extract(msgs)
        mate = next((t for t in state.teammates
                     if t.name == "finder-p2" or "finder-p2" in t.agent_id), None)
        self.assertIsNotNone(mate, "finder-p2 must be in teammates")
        s = (mate.status or "").strip().lower()
        self.assertNotIn(s, _TEAMMATE_BENIGN,
                         "SendMessage after idle_notification must re-activate teammate "
                         f"(status must not be benign), got {s!r}")
        safe, reason = safe_to_reload(state, msgs, Path("/tmp/fake_session.jsonl"))
        self.assertFalse(safe,
                         "Re-activated teammate (SendMessage after idle) must block reload")

    def test_prose_teammate_message_does_not_transition(self):
        """INVARIANT: prose-only <teammate-message> must NOT transition to idle.

        Only the exact JSON idle_notification body triggers the transition.
        This prevents false-idle transitions from progress-update prose blocks.
        """
        from cozempic.guard import _TEAMMATE_BENIGN
        prose_msg = (
            '<teammate-message teammate_id="finder-p2" summary="progress update">'
            'I have completed the first 3 files and found no issues.'
            '</teammate-message>'
        )
        msgs = [
            (0, _tool_use("u1", "Agent", {"name": "finder-p2", "description": "find issues"}), 200),
            (1, _tool_result("u1", _SPAWN_RESULT_P2), 300),
            (2, _user_content(prose_msg), 200),
        ]
        state = _extract(msgs)
        mate = next((t for t in state.teammates
                     if t.name == "finder-p2" or "finder-p2" in t.agent_id), None)
        if mate is not None:
            s = (mate.status or "").strip().lower()
            self.assertNotIn(s, _TEAMMATE_BENIGN,
                             f"Prose teammate-message must NOT transition to benign, got {s!r}")


# ─── TestSessionScopedAntiWedge (P0-E) ───────────────────────────────────────

class TestSessionScopedAntiWedge(unittest.TestCase):
    """P0-E: stale/cross-session config must NOT block safe_to_reload.

    A stale team's config.json persisting on disk with non-benign teammates
    must be silently bypassed when the team belongs to a different session.
    """

    def test_stale_cross_session_team_does_not_wedge_reload(self):
        """REGRESSION GUARD — RED at base: no session-scope check; 'running' teammate wedges.

        A TeamState with lead_session_id that does NOT match the session_path.stem
        must NOT block safe_to_reload. Only the current session's team matters.
        """
        from cozempic.guard import safe_to_reload
        from cozempic.team import TeamState, TeammateInfo
        state = TeamState(
            team_name="old-team",
            lead_session_id="deadbeef-0000-0000-0000-000000000000",
            teammates=[TeammateInfo("alice@old-team", "alice", status="running")],
        )
        # Current session has a DIFFERENT id in the path stem
        session_path = Path("/tmp/aabbccdd1234abcd.jsonl")
        safe, reason = safe_to_reload(state, [], session_path)
        self.assertTrue(safe,
                        "Stale/cross-session running teammate must NOT wedge reload; "
                        f"got safe={safe}, reason={reason!r}")

    def test_same_session_running_teammate_blocks_reload(self):
        """INVARIANT (GREEN at base — blocks; correct reason after fix):
        when lead_session_id matches the guarded session, a running teammate blocks.

        This is an invariant-preservation test, not a regression guard.
        """
        from cozempic.guard import safe_to_reload
        from cozempic.team import TeamState, TeammateInfo
        state = TeamState(
            team_name="live-team",
            lead_session_id="aabbccdd1234abcd",
            teammates=[TeammateInfo("alice@live-team", "alice", status="running")],
        )
        session_path = Path("/tmp/aabbccdd1234abcd.jsonl")
        safe, reason = safe_to_reload(state, [], session_path)
        self.assertFalse(safe,
                         "Same-session running teammate must block reload")
        self.assertIn("teammate", reason)

    def test_no_session_id_in_state_is_conservative(self):
        """INVARIANT (GREEN at base): empty lead_session_id → conservative → blocks.

        Unknown session → assume it IS the current session → block if running.
        Safe-fail mode.
        """
        from cozempic.guard import safe_to_reload
        from cozempic.team import TeamState, TeammateInfo
        state = TeamState(
            team_name="unknown-team",
            lead_session_id="",
            teammates=[TeammateInfo("alice@unknown-team", "alice", status="running")],
        )
        session_path = Path("/tmp/aabbccdd1234abcd.jsonl")
        safe, _ = safe_to_reload(state, [], session_path)
        self.assertFalse(safe,
                         "Unknown lead_session_id must be conservative → blocks reload")

    def test_none_session_path_is_conservative(self):
        """INVARIANT: session_path=None → cannot compare → conservative → blocks."""
        from cozempic.guard import safe_to_reload
        from cozempic.team import TeamState, TeammateInfo
        state = TeamState(
            team_name="t",
            lead_session_id="some-session-id",
            teammates=[TeammateInfo("a1", "alice", status="running")],
        )
        safe, _ = safe_to_reload(state, [], None)
        self.assertFalse(safe,
                         "session_path=None must be conservative → blocks reload")


# ─── TestFullScenario (integration) ──────────────────────────────────────────

class TestFullScenario(unittest.TestCase):
    """End-to-end integration: the exact audit scenario.

    The critical regression guard is test_pure_sendmessage_team_no_tasks_unsafe:
    a team coordinated purely via Agent spawns + SendMessage, with no shared tasks,
    must NOT be considered quiescent by safe_to_reload.

    This test class proves the entire fix chain from P0-A through P0-E working
    together on a realistic minimal message list.
    """

    def _pure_sendmessage_msgs(self, n_spawns=3):
        """Build a message list with n Agent spawns + SendMessages, no TaskCreate.

        Every tool_use must have a matching tool_result so that
        detect_in_flight.open_call stays False; otherwise open-call detection
        returns False for the wrong reason, masking the teammate gate under test.
        """
        msgs = []
        idx = 0
        # TeamCreate with 'team_name' key (P0-A trigger)
        msgs.append((idx, _tool_use("u0", "TeamCreate", {
            "team_name": "myteam",
            "description": "test team",
        }), 100))
        idx += 1
        # TeamCreate result (required to close the open_call for u0)
        msgs.append((idx, _tool_result("u0", "Team created successfully."), 50))
        idx += 1
        # n Agent spawns (P0-B trigger)
        for i in range(1, n_spawns + 1):
            spawn_result = (
                f"Spawned successfully.\n"
                f"agent_id: finder-p{i}@myteam\n"
                f"name: finder-p{i}\n"
                f"team_name: myteam\n"
                "The agent is now running and will receive instructions via mailbox."
            )
            msgs.append((idx, _tool_use(f"us{i}", "Agent", {
                "name": f"finder-p{i}",
                "description": f"finder {i}",
            }), 200))
            idx += 1
            msgs.append((idx, _tool_result(f"us{i}", spawn_result), 300))
            idx += 1
        # SendMessages to each (P0-C trigger: bare names).
        # Each SendMessage must have a matching tool_result so that
        # detect_in_flight.open_call stays False.  Without the result the
        # un-paired tool_use id lands in use_ids - res_ids → open_call=True →
        # safe_to_reload returns False for the WRONG reason (open tool call
        # instead of active teammate), masking the teammate-based gate under test.
        for i in range(1, n_spawns + 1):
            msgs.append((idx, _tool_use(f"um{i}", "SendMessage", {
                "to": f"finder-p{i}",
                "message": "start your task",
            }), 100))
            idx += 1
            msgs.append((idx, _tool_result(f"um{i}", "Message delivered."), 50))
            idx += 1
        return msgs

    def test_pure_sendmessage_team_no_tasks_unsafe(self):
        """REGRESSION GUARD — RED at base: pure-SendMessage team → is_empty()=True → safe.

        The exact audit scenario: finders spawned via Agent tool, coordinated
        via SendMessage only (no TaskCreate/TaskUpdate at all).
        Before fix: team_state.is_empty() → True (teammates=[], subagents=[], tasks=[])
        → safe_to_reload returns (True, 'quiescent') → silent SIGKILL of working team.
        After fix: teammates extracted with running status → blocks reload.

        This is THE critical regression guard for F1.
        """
        from cozempic.guard import safe_to_reload
        msgs = self._pure_sendmessage_msgs(n_spawns=3)
        state = _extract(msgs)
        # After fix, state must not be empty (teammates are visible)
        self.assertFalse(state.is_empty(),
                         "pure-SendMessage team state must not be empty after fix; "
                         f"got team_name={state.team_name!r}, "
                         f"teammates={len(state.teammates)}, "
                         f"subagents={len(state.subagents)}, "
                         f"tasks={len(state.tasks)}")
        safe, reason = safe_to_reload(state, msgs, Path("/tmp/fake_session.jsonl"))
        self.assertFalse(safe,
                         "Pure-SendMessage active team must block safe_to_reload; "
                         f"got safe={safe}, reason={reason!r}")

    def test_pure_sendmessage_team_after_all_idle_is_safe(self):
        """REGRESSION GUARD (partial — also GREEN at base for wrong reason):
        after ALL teammates send idle_notification → safe_to_reload returns True.

        Before fix: is_empty()=True → returns True (correct but for wrong reason;
        no protection was active). After fix: teammates exist + all are idle → True.
        This test validates the CORRECT path post-fix (all idle → benign → safe).
        """
        from cozempic.guard import safe_to_reload
        n = 3
        msgs = list(self._pure_sendmessage_msgs(n_spawns=n))
        idx = len(msgs)
        for i in range(1, n + 1):
            idle = (
                f'<teammate-message teammate_id="finder-p{i}" summary="idle">'
                f'{{"type":"idle_notification","from":"finder-p{i}",'
                f'"timestamp":"2026-06-08T10:00:00Z","idleReason":"available"}}'
                f'</teammate-message>'
            )
            msgs.append((idx, _user_content(idle), 200))
            idx += 1
        state = _extract(msgs)
        safe, _ = safe_to_reload(state, msgs, Path("/tmp/fake_session.jsonl"))
        self.assertTrue(safe,
                        "After ALL idle_notifications, safe_to_reload must return True")


if __name__ == "__main__":
    unittest.main()
