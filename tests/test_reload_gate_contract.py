"""Standing safeguard — the HARNESS-CONTRACT / deny-by-default invariant for the
safe-point reload gate.

THE BUG CLASS this catches (the one that produced the 1.8.22 Agent-marker blindness
AND PR #117's Agent-team blindness): cozempic's safety gate recognizes Claude Code
harness activity by *hardcoded, assumed* tool names / input keys / result-string
markers (`name` vs `team_name`; `agent_id:` vs `agentId:`; "Async agent launched"
vs "Spawned successfully"). When the assumed shape is wrong or drifts, the matcher
silently misses → the gate sees an empty roster → it returns "quiescent" → it
SIGKILLs live work. It is SILENT (no crash), SAFETY-CRITICAL (false-negative), and
our *synthetic* unit tests can't catch it because they encode the same assumptions.

THE INVARIANT (deny-by-default): if the raw transcript contains ANY sign of team /
agent coordination — a `TEAM_TOOL_NAMES` tool_use, OR an agent-spawn marker in a
tool_result — then `safe_to_reload` must NOT return "quiescent". Either the
extractor resolves it to a live roster (→ block), or `detect_in_flight` catches it,
or — if we couldn't parse it — we DENY rather than SIGKILL. A reload is only safe
when there is *no* unexplained coordination signal.

NOTE ON STATUS: several cases below are RED on a gate that hasn't been hardened for
Agent-spawned teams (e.g. the `team_name` case fails on 1.8.23 pre-#117; the
camelCase `agentId:` case fails even with #117's snake-only matcher). That is the
POINT — this file is the regression bar the gate must clear, and a deliberate guard
against re-shipping the class. Pair it with REAL redacted transcript fixtures
(tests/fixtures/harness/, captured from an actual agent-team session) so the
matchers are verified against reality, not against our assumptions.
"""

import json
import unittest
from pathlib import Path

from cozempic.guard import detect_in_flight, safe_to_reload
from cozempic.team import TEAM_TOOL_NAMES, extract_team_state
from cozempic.session import load_messages


def _tu(i, name, inp):
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}}


def _tr(i, content):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": i, "content": content}]}}


def _idle_lead(text="Waiting for the team to finish."):
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": text}]}}


def _write(tmp, rows):
    p = tmp / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


# Each case: a transcript with a LIVE team, the lead idle, ALL tool calls CLOSED,
# and NO active shared-list task — i.e. the gate would otherwise call it quiescent.
# A reload here SIGKILLs the live team, so the gate must NOT return quiescent.
_LIVE_TEAM_CASES = {
    "teamcreate_team_name_key": [
        _tu("u1", "TeamCreate", {"team_name": "squad", "teammates": [{"name": "alice"}]}),
        _tr("u1", "Team 'squad' created with 1 teammate."),
        _tu("u2", "SendMessage", {"to": "alice", "body": "start"}), _tr("u2", "delivered"),
        _idle_lead(),
    ],
    "agent_spawn_snake_id": [
        _tu("u1", "Agent", {"description": "spin alice", "subagent_type": "general-purpose"}),
        _tr("u1", "Spawned successfully. agent_id: alice@squad"),
        _tu("u2", "SendMessage", {"to": "alice", "body": "go"}), _tr("u2", "delivered"),
        _idle_lead(),
    ],
    # The camelCase smell: the SHIPPED 1.8.22 background marker is `agentId:` (see
    # _AGENT_LAUNCH_RE), so a team spawn very plausibly uses camelCase too. A
    # snake-only matcher misses it → the fix silently does nothing.
    "agent_spawn_camelCase_id": [
        _tu("u1", "Agent", {"description": "spin alice", "subagent_type": "general-purpose"}),
        _tr("u1", "Spawned successfully. agentId: alice@squad"),
        _tu("u2", "SendMessage", {"to": "alice", "body": "go"}), _tr("u2", "delivered"),
        _idle_lead(),
    ],
    "spawnteammate_tool": [
        _tu("u1", "SpawnTeammate", {"name": "alice", "role": "finder"}),
        _tr("u1", "Teammate alice spawned."),
        _idle_lead(),
    ],
    "teammate_message_active": [
        _tu("u1", "TeamCreate", {"team_name": "squad", "teammates": [{"name": "alice"}]}),
        _tr("u1", "created"),
        {"type": "user", "message": {"role": "user", "content":
            '<teammate-message teammate_id="alice" summary="progress">working on 3 of 10</teammate-message>'}},
        _idle_lead(),
    ],
}


class TestReloadGateContract(unittest.TestCase):
    """The reload gate must never call a live-team transcript 'quiescent'."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="cozempic_contract_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gate(self, rows):
        p = _write(self.tmp, rows)
        msgs = load_messages(p)
        return safe_to_reload(extract_team_state(msgs), msgs, p)

    def test_live_team_must_not_be_quiescent(self):
        failures = []
        for name, rows in _LIVE_TEAM_CASES.items():
            safe, reason = self._gate(rows)
            if safe:  # quiescent → would SIGKILL the live team
                failures.append(f"{name}: safe_to_reload returned (True, {reason!r})")
        self.assertEqual(
            failures, [],
            "Reload gate called a LIVE-TEAM transcript quiescent (would SIGKILL it):\n  "
            + "\n  ".join(failures),
        )

    def test_deny_by_default_on_unparsed_coordination(self):
        """Cross-check independent of any specific marker: if the raw transcript has
        a TEAM_TOOL_NAMES tool_use but the gate is quiescent, that's an unparsed
        coordination signal → it must deny, not SIGKILL."""
        for name, rows in _LIVE_TEAM_CASES.items():
            msgs = load_messages(_write(self.tmp, rows))
            has_team_tooluse = any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                and b.get("name") in TEAM_TOOL_NAMES
                for m in msgs for b in ((m[1].get("message") or {}).get("content") or [])
                if isinstance((m[1].get("message") or {}).get("content"), list)
            )
            if not has_team_tooluse:
                continue
            safe, reason = safe_to_reload(extract_team_state(msgs), msgs, None)
            self.assertFalse(
                safe,
                f"{name}: a {TEAM_TOOL_NAMES & {b.get('name') for m in msgs for b in (((m[1].get('message') or {}).get('content')) or []) if isinstance(b, dict)}} "
                f"tool_use is present but the gate is quiescent — unparsed coordination "
                f"must DENY (reason was {reason!r}).",
            )


class TestRealHarnessFixtures(unittest.TestCase):
    """Verify the gate's matchers against REAL redacted harness output, not our
    assumptions. Drop redacted `.jsonl` samples (one live team, one finished team)
    under tests/fixtures/harness/ — captured from a genuine agent-team session — and
    this asserts the gate behaves correctly on real shapes. Skips (loudly) until a
    fixture exists, so the gap stays visible instead of silently 'passing'."""

    FIX = Path(__file__).parent / "fixtures" / "harness"

    def test_live_team_fixture_defers(self):
        f = self.FIX / "live_team.jsonl"
        if not f.exists():
            self.skipTest("NO real live-team fixture yet — capture one (tests/fixtures/harness/live_team.jsonl). "
                          "Until then the team-coordination markers are UNVERIFIED.")
        msgs = load_messages(f)
        safe, reason = safe_to_reload(extract_team_state(msgs), msgs, f)
        self.assertFalse(safe, f"real live team must defer; got quiescent ({reason})")

    def test_finished_team_fixture_reloads(self):
        f = self.FIX / "finished_team.jsonl"
        if not f.exists():
            self.skipTest("NO real finished-team fixture yet — capture one (tests/fixtures/harness/finished_team.jsonl).")
        msgs = load_messages(f)
        safe, _ = safe_to_reload(extract_team_state(msgs), msgs, f)
        self.assertTrue(safe, "real finished team must be allowed to reload")


if __name__ == "__main__":
    unittest.main()


class TestReloadGateHardening1824(unittest.TestCase):
    """1.8.24 hardening on top of #117 — the over-block reducers + fail-safes that
    keep FINISHED teams reloading while LIVE/ambiguous ones block."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="cozempic_h1824_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gate(self, rows):
        p = _write(self.tmp, rows)
        m = load_messages(p)
        return safe_to_reload(extract_team_state(m), m, p)

    def test_camelcase_agentid_blocks(self):
        # camelCase agentId (the shipped 1.8.22 convention) must parse → block.
        safe, _ = self._gate([
            _tu("u1", "Agent", {"description": "spin"}),
            _tr("u1", "Spawned successfully. agentId: alice@squad"),
            _idle_lead()])
        self.assertFalse(safe, "camelCase agentId must be recognized → block")

    def test_teamdelete_lets_team_reload(self):
        # A disbanded team (TeamDelete) must NOT wedge — members go terminal.
        safe, _ = self._gate([
            _tu("u1", "Agent", {"description": "spin"}),
            _tr("u1", "Spawned successfully. agent_id: alice@squad"),
            _tu("u2", "TeamDelete", {"team_name": "squad"}),
            _tr("u2", "All agents terminated."), _idle_lead()])
        self.assertTrue(safe, "disbanded team must be allowed to reload (no wedge)")

    def test_unparseable_successful_spawn_fails_safe(self):
        # A spawn result with no parseable id AND no failure word → keep running → block.
        safe, _ = self._gate([
            _tu("u1", "Agent", {"description": "spin"}),
            _tr("u1", "Agent ready: finder-p1@team"), _idle_lead()])
        self.assertFalse(safe, "unparseable-but-successful spawn must fail toward block")

    def test_affirmative_failed_spawn_reloads(self):
        # A spawn that affirmatively FAILED (quota/error) is terminal → reloads.
        safe, _ = self._gate([
            _tu("u1", "Agent", {"description": "spin"}),
            _tr("u1", "Error: agent quota exceeded, spawn rejected."), _idle_lead()])
        self.assertTrue(safe, "an affirmatively-failed spawn must not block forever")

    def test_net_does_not_overblock_finished_idle_team(self):
        # A team whose teammate sent an idle_notification is finished → reloads
        # (the deny-by-default net must not over-block a parsed, idle roster).
        safe, _ = self._gate([
            _tu("u1", "Agent", {"description": "spin"}),
            _tr("u1", "Spawned successfully. agent_id: alice@squad"),
            {"type": "user", "message": {"role": "user", "content":
                '<teammate-message teammate_id="alice@squad">{"type":"idle_notification"}</teammate-message>'}},
            _idle_lead()])
        self.assertTrue(safe, "a finished (idle) team must reload, not over-block")
