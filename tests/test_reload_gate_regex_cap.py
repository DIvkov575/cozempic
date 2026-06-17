"""Regression guards for the 64KB cap on reload-gate block-regex scans.

Both detect_in_flight (guard.py) and extract_team_state (team.py) iterate
uncapped text through DOTALL lazy-star regexes:

  guard.py:2509  _TN_BLOCK_RE.findall(text)            — text from _completion_text(msg)
  team.py:862    _TASK_NOTIF_BLOCK_RE.finditer(content) — raw string content from JSONL

An attacker-sized message (many <task-notification> openers without closers)
triggers O(openers × len) catastrophic backtracking, freezing the 30-second
checkpoint/reload-gate loop — a quadratic-regex DoS (L3/L0, MED). Measured
at ~4-5s with 10,000 openers (~185KB); capped at 64KB takes <0.6s.

recap.py already solved this for its own regexes (text[:32768] / text[:8000]).
Fix mirrors that pattern: cap both scan sites at _RELOAD_GATE_SCAN_CAP = 65536.

The cap is FAIL-SAFE: a notification beyond 64KB is MISSED → the launch stays
"in-flight" → the gate OVER-DEFERS the reload (recoverable). It never
UNDER-BLOCKS, which would SIGKILL. 64KB is ~64× the size of a real notification.

REGRESSION GUARD tests (proven RED at base — timing blowup without the cap):
  test_detect_in_flight_quadratic_input_bounded  — guard.py site
  test_extract_team_state_quadratic_input_bounded — team.py site

CORRECTNESS GUARD tests (GREEN at base and after fix):
  test_detect_in_flight_real_notification_still_clears   — happy path
  test_extract_team_state_real_notification_still_clears — happy path
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

# ── degenerate input that triggers catastrophic backtracking ──────────────────
# Many <task-notification> openers without matching closers.  The DOTALL
# lazy-star `(.*?)` must scan to the end of string for each opener trying to
# find a closing tag — O(openers × len) work without a cap.  10k openers
# (~185KB) gives ~4-5s uncapped on a modern machine; the 64KB cap reduces this
# to <0.6s.  Large enough to stay >2s at base with margin; small enough to
# complete within pytest's run window.
_MANY_OPENERS = "<task-notification>" * 10_000   # 185 KB

# A real task-notification payload well within the 64KB cap.
_REAL_NOTIF = (
    "<task-notification>"
    "<task-id>agent-xyz</task-id>"
    "<status>completed</status>"
    "<result>done</result>"
    "</task-notification>"
)


class TestDetectInFlightReDoSCap(unittest.TestCase):
    """guard.py _TN_BLOCK_RE.findall(text) must be capped at _RELOAD_GATE_SCAN_CAP."""

    def _detect(self, raw_text: str) -> dict:
        """Call detect_in_flight with a single user message carrying raw_text."""
        from cozempic.guard import detect_in_flight
        msgs = [{"type": "user", "content": raw_text}]
        return detect_in_flight(msgs)

    def test_detect_in_flight_quadratic_input_bounded(self):
        """REGRESSION GUARD — RED at base: uncapped scan on 185KB of openers blows up.

        Without the _RELOAD_GATE_SCAN_CAP[:65536] slice the regex must scan the
        entire 185KB string for each of the 10,000 openers — measured wall time
        is 4-5s (catastrophic backtracking). With the cap the truncated 64KB
        input completes in <0.6s.

        Budget: 2.0s.
        """
        t0 = time.monotonic()
        result = self._detect(_MANY_OPENERS)
        elapsed = time.monotonic() - t0
        self.assertLess(
            elapsed, 2.0,
            f"detect_in_flight took {elapsed:.3f}s on degenerate input — "
            "the _TN_BLOCK_RE scan is not capped at _RELOAD_GATE_SCAN_CAP (64KB). "
            "Without the cap, 10,000 openers trigger O(openers × len) backtracking."
        )
        # The degenerate input has no valid closed blocks — no completions cleared.
        self.assertFalse(
            result.get("agent") or result.get("background") or result.get("workflow"),
            "Degenerate openers-only input must not affect in-flight detection"
        )

    def test_detect_in_flight_real_notification_still_clears(self):
        """Correctness guard: a real notification within the 64KB cap clears the launch.

        Proves the cap does NOT break the happy path AND that the test is a real guard:
        - tool_result spawn-ack populates launched_agent (without it agent is never
          registered, making the assertFalse trivially vacuous).
        - _REAL_NOTIF (within cap) → agent cleared → assertFalse passes.
        - With cap=1 the notification is truncated before the regex can match →
          agent stays in-flight → assertFalse FAILS — proving the cap matters.

        RED-at-base (cap=1 patch): assertFalse(result.get("agent")) raises
        AssertionError because agent-xyz is registered but NOT cleared.
        """
        import cozempic.guard as _guard

        # Full message sequence:
        #   1. Agent tool_use (adds tu-1 to use_ids)
        #   2. tool_result spawn-ack (populates launched_agent = {"agent-xyz"})
        #   3. user message with task-notification (adds "agent-xyz" to completed)
        # Without step 2 the test is vacuous: launched_agent stays empty → agent
        # is never registered → assertFalse passes for the wrong reason.
        msgs = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tu-1", "name": "Agent",
                         "input": {"name": "finder-p1"}}
                    ],
                }
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu-1",
                         "content": "Async agent launched successfully. agentId: agent-xyz"}
                    ],
                }
            },
            # Harness delivers the task-notification as a user message content string.
            {"type": "user", "content": _REAL_NOTIF},
        ]
        result = _guard.detect_in_flight(msgs)
        self.assertFalse(
            result.get("agent"),
            "detect_in_flight must clear the Agent launch when a completed "
            "task-notification is present within the 64KB cap; "
            f"got result={result}"
        )

    def test_detect_in_flight_notification_beyond_cap_stays_inflight(self):
        """Cap-truncation guard: a notification beyond cap=1 is missed → agent in-flight.

        Patches _RELOAD_GATE_SCAN_CAP=1 so the user message content is sliced to
        1 character before the block-regex runs.  The <task-notification>…</task-notification>
        block is never found → agent-xyz stays in launched_agent − completed → agent is
        truthy.  This is the RED behaviour that proves the test above is a real guard
        (not vacuous): with a real cap the notification clears the agent; without it
        the agent stays stranded.
        """
        import cozempic.guard as _guard

        msgs = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tu-2", "name": "Agent",
                         "input": {"name": "finder-p1"}}
                    ],
                }
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu-2",
                         "content": "Async agent launched successfully. agentId: agent-xyz"}
                    ],
                }
            },
            {"type": "user", "content": _REAL_NOTIF},
        ]
        with patch.object(_guard, "_RELOAD_GATE_SCAN_CAP", 1):
            result = _guard.detect_in_flight(msgs)
        self.assertTrue(
            result.get("agent"),
            "With cap=1 the notification is truncated before matching; "
            "agent-xyz must stay in-flight (launched but not cleared). "
            f"got result={result}"
        )


def _extract_isolated(msgs):
    """Call extract_team_state with config isolation (no live ~/.claude/teams/).

    Shared by TestExtractTeamStateReDoSCap and TestExtractTeamStateTeammateMsgCap
    to avoid duplicating the patch context manager in each class.
    """
    from cozempic.team import extract_team_state
    with patch("cozempic.team.load_team_configs", return_value=[]):
        return extract_team_state(msgs)


def _tu(idx: int, id_: str, name: str, inp: dict) -> tuple:
    """3-tuple (idx, msg_dict, size) for an Agent tool_use — matches Message type."""
    d = {"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": id_, "name": name, "input": inp}
    ]}}
    return (idx, d, 200)


def _tr(idx: int, tool_use_id: str, text: str) -> tuple:
    """3-tuple for a tool_result message."""
    d = {"message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    ]}}
    return (idx, d, len(text))


def _uc(idx: int, text: str, team_name: str | None = None) -> tuple:
    """3-tuple for a user message with plain string content.

    team_name: set a top-level teamName field — genuine harness teammate-message
    carriers always do, and #134's H-1 gate only transitions an idle-notification
    when it is present (so a user-typed <teammate-message> can't phantom-IDLE a
    live agent).
    """
    d = {"message": {"role": "user", "content": text}}
    if team_name is not None:
        d["teamName"] = team_name
    return (idx, d, len(text))


def _qo(idx: int, text: str) -> tuple:
    """3-tuple for a queue-operation message — the ONLY surface #134 (C-2) parses
    task-notifications from (a user-typed <task-notification> in plain content is
    ignored to prevent phantom-terminating a live agent)."""
    d = {"type": "queue-operation", "content": text}
    return (idx, d, len(text))


def _task_spawn_with_notif() -> list:
    """Task tool spawn + completed task-notification for 'finder@myteam'.

    Shared by test_extract_team_state_notification_transitions_existing_subagent
    and test_extract_team_state_notification_beyond_cap_stays_running.

    Uses tool_use_id='finder@myteam' so seen_subagents['finder@myteam'] is keyed
    by the same string the notification's <task-id> carries — this routes through
    the IF branch (task_id in seen_subagents) rather than the else-branch.
    """
    notif_text = (
        "<task-notification>"
        "<task-id>finder@myteam</task-id>"
        "<status>completed</status>"
        "<result>all done</result>"
        "</task-notification>"
    )
    return [
        _tu(0, "finder@myteam", "Task", {"description": "find bugs"}),
        _qo(1, notif_text),  # #134 C-2: task-notifications parse from queue-op only
    ]


# Three of PR #132's cases originally asserted transitions from PLAIN user content.
# PR #134 (merged after #132 was opened) narrowed the surfaces for security: task-
# notifications parse from the queue-operation surface ONLY (C-2), and idle-
# notifications transition only when the carrier sets a top-level teamName (H-1).
# Below they are restored on the REAL current surfaces (queue-op / teamName) so they
# still exercise the create/transition paths under the new contract.
class TestNotificationSurfaceUnder134(unittest.TestCase):
    """Salvaged #132 create/transition coverage, adapted to #134's C-2/H-1 surfaces."""

    def _extract(self, msgs):
        return _extract_isolated(msgs)

    @staticmethod
    def _agent_spawn():
        return [
            _tu(0, "tu-a1", "Agent", {"name": "finder", "description": "find bugs"}),
            _tr(1, "tu-a1",
                "Spawned successfully.\nagent_id: finder@myteam\nname: finder\nteam_name: myteam\n"),
        ]

    def test_queueop_notification_creates_new_subagent(self):
        """A completed task-notification on the queue-operation surface for an
        agent-spawned worker transitions it to 'completed' (C-2 path)."""
        notif = ("<task-notification><task-id>finder@myteam</task-id>"
                 "<status>completed</status><result>done</result></task-notification>")
        state = self._extract(self._agent_spawn() + [_qo(2, notif)])
        finder = next((s for s in (state.subagents or []) if "finder" in s.agent_id), None) \
            or next((t for t in (state.teammates or []) if "finder" in (t.name or "")), None)
        self.assertIsNotNone(finder, "queue-op notification must reach finder")
        self.assertEqual(finder.status, "completed",
                         f"queue-op task-notification must set status 'completed'; got {finder.status!r}")

    def test_queueop_notification_transitions_existing_subagent(self):
        """A queue-op task-notification transitions a pre-registered running subagent
        (the if-branch). _task_spawn_with_notif now delivers via queue-op."""
        state = self._extract(_task_spawn_with_notif())
        finder = next((s for s in (state.subagents or []) if "finder" in s.agent_id), None)
        self.assertIsNotNone(finder, "Task spawn must pre-register finder@myteam")
        self.assertEqual(finder.status, "completed",
                         f"queue-op task-notification must transition to 'completed'; got {finder.status!r}")

    def test_teammate_idle_within_cap_transitions_with_teamname(self):
        """An idle-notification WITHIN the cap, on a carrier with teamName (H-1),
        transitions the teammate to idle — the cap doesn't break the happy path."""
        idle = ('<teammate-message teammate_id="finder" summary="done">'
                '{"type":"idle_notification","from":"finder"}</teammate-message>')
        state = self._extract(self._agent_spawn() + [_uc(2, idle, team_name="myteam")])
        worker = next((t for t in (state.teammates or [])
                       if "finder" in t.agent_id or "finder" in (t.name or "")), None)
        self.assertIsNotNone(worker, "finder teammate must be registered")
        self.assertEqual((worker.status or "").lower(), "idle",
                         f"idle-notification (teamName present, within cap) must transition to 'idle'; "
                         f"got {worker.status!r}")
class TestExtractTeamStateReDoSCap(unittest.TestCase):
    """team.py _TASK_NOTIF_BLOCK_RE.finditer(content) must be capped at _RELOAD_GATE_SCAN_CAP."""

    @staticmethod
    def _agent_spawn_msgs():
        """Minimal Agent tool_use + result 3-tuples that register a subagent."""
        return [
            _tu(0, "tu-a1", "Agent", {"name": "finder", "description": "find bugs"}),
            _tr(1, "tu-a1", (
                "Spawned successfully.\n"
                "agent_id: finder@myteam\n"
                "name: finder\n"
                "team_name: myteam\n"
            )),
        ]

    def _extract(self, msgs):
        return _extract_isolated(msgs)

    def test_extract_team_state_quadratic_input_bounded(self):
        """REGRESSION GUARD — RED at base: uncapped scan on a message with 185KB of
        openers freezes extract_team_state (called every checkpoint cycle).

        team.py iterates _TASK_NOTIF_BLOCK_RE over the raw `content` string without
        slicing — the same catastrophic backtracking as guard.py's site. Measured
        at 4-5s on this machine with 10k openers. With _RELOAD_GATE_SCAN_CAP[:65536]
        the truncated input completes in <0.6s.

        Budget: 2.0s.
        """
        msgs = self._agent_spawn_msgs() + [_uc(2, _MANY_OPENERS)]

        t0 = time.monotonic()
        self._extract(msgs)
        elapsed = time.monotonic() - t0
        self.assertLess(
            elapsed, 2.0,
            f"extract_team_state took {elapsed:.3f}s with degenerate input — "
            "the _TASK_NOTIF_BLOCK_RE scan is not capped at _RELOAD_GATE_SCAN_CAP (64KB). "
            "Without the cap, 10,000 openers trigger O(openers × len) backtracking."
        )

    def test_extract_team_state_notification_beyond_cap_stays_running(self):
        """Cap-truncation guard: a notification beyond cap=1 is missed → existing
        subagent stays 'running' (if-branch is never reached).

        Patches _RELOAD_GATE_SCAN_CAP=1 so content[:1] = '<' which doesn't match
        the block-regex → no notification processed → seen_subagents['finder@myteam']
        keeps status='running'.  This is the RED behaviour that proves the
        if-branch test above is a real guard: without truncation the subagent IS
        transitioned; with truncation it stays stranded.

        Fail-safe: missed notification → over-defers reload (recoverable), never
        under-blocks (SIGKILL).
        """
        import cozempic.team as _team

        msgs = _task_spawn_with_notif()
        with patch.object(_team, "_RELOAD_GATE_SCAN_CAP", 1):
            state = self._extract(msgs)
        subagents = state.subagents if state else []
        finder = next((s for s in subagents if "finder" in s.agent_id), None)
        self.assertIsNotNone(finder, "Task spawn must register finder@myteam")
        self.assertEqual(
            finder.status, "running",
            f"With cap=1 the notification is truncated; finder@myteam must stay "
            f"'running' (if-branch never fires); got status={finder.status!r}"
        )


def _tc(idx: int, team_name: str, teammate_name: str, agent_id: str) -> tuple:
    """3-tuple for a TeamCreate tool_use that registers a teammate.

    Both `name` and `agentId` are required: extract_team_state only adds to
    seen_teammates when agentId is non-empty (see team.py:667).
    """
    d = {"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": f"tc-{idx}", "name": "TeamCreate",
         "input": {"team_name": team_name,
                   "teammates": [{"name": teammate_name, "agentId": agent_id}]}}
    ]}}
    return (idx, d, 200)


def _tc_result(idx: int, team_name: str, tc_idx: int | None = None) -> tuple:
    """3-tuple for the TeamCreate tool_result.

    tc_idx: the idx used for the matching _tc() call.  Defaults to idx-1 for
    backward compat with existing consecutive-call patterns, but explicit is
    preferred to avoid arithmetic coupling when the two calls are not adjacent.
    """
    ref_idx = tc_idx if tc_idx is not None else idx - 1
    text = f"Team '{team_name}' created."
    d = {"message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"tc-{ref_idx}", "content": text}
    ]}}
    return (idx, d, len(text))


class TestExtractTeamStateTeammateMsgCap(unittest.TestCase):
    """team.py _TEAMMATE_MSG_RE.finditer(content) must be capped at _RELOAD_GATE_SCAN_CAP.

    _TEAMMATE_MSG_RE uses a negative-lookahead form ((?:(?!<teammate-message).)*?)
    which is O(n) linear for opener-only input (the NLA fails fast on '<').  It is
    therefore NOT quadratic on opener-only strings, so a timing test is not the right
    proof mechanism here.

    The correct RED/GREEN proof is a BOUNDARY test: an idle-notification placed PAST
    the 64KB cap boundary must NOT be processed after the cap is applied (teammate
    stays "running" — the fail-safe over-defer direction).  Without the cap the same
    notification IS processed (teammate transitions to "idle").

    This proves the cap is applied to _TEAMMATE_MSG_RE (class-of-bug fold: all three
    uncapped DOTALL block-regex scans on the same `content` are now capped).
    """

    def _extract(self, msgs):
        return _extract_isolated(msgs)

    @staticmethod
    def _team_spawn_msgs():
        """TeamCreate + result 3-tuples that register 'worker@myteam' as a teammate.

        Both name='worker' and agentId='worker@myteam' are required so that:
        1. seen_teammates['worker@myteam'] is created (agentId gate at team.py:667)
        2. _name_to_agent_id['worker'] -> 'worker@myteam' is set (name lookup index)
        3. The idle-notification uses teammate_id="worker" which resolves via index
        """
        return [
            _tc(0, "myteam", "worker", "worker@myteam"),
            _tc_result(1, "myteam", tc_idx=0),  # explicit: references _tc(0, ...)
            # SendMessage to worker → marks it as "running" in seen_teammates
            _tu(2, "sm-1", "SendMessage", {"to": "worker", "message": "start"}),
            _tr(3, "sm-1", "delivered"),
        ]

    def test_teammate_msg_cap_boundary_does_not_transition(self):
        """RED without cap: idle-notification PAST 64KB transitions teammate to idle.
        GREEN with cap:  idle-notification PAST 64KB is not scanned → stays running.

        Fail-safe direction: missed notification → teammate stays "running" →
        safe_to_reload/agents_active keep protecting it → gate OVER-DEFERS (recoverable),
        never UNDER-BLOCKS (SIGKILL).

        To reproduce RED manually: revert team.py:914 to
          `for tm_match in _TEAMMATE_MSG_RE.finditer(content):`
        and run this test — it will fail because the status becomes "idle".
        """
        from cozempic.team import _RELOAD_GATE_SCAN_CAP

        idle_notif = (
            '<teammate-message teammate_id="worker" summary="done">'
            '{"type":"idle_notification","from":"worker"}'
            '</teammate-message>'
        )
        # Pad so the notification starts PAST the cap boundary.
        pad = " " * (_RELOAD_GATE_SCAN_CAP + 10)
        content_past_cap = pad + idle_notif

        msgs = self._team_spawn_msgs() + [_uc(4, content_past_cap)]
        state = self._extract(msgs)
        teammates = state.teammates if state else []
        # Registered as agent_id='worker@myteam', name='worker'
        worker = next(
            (t for t in teammates if "worker" in t.agent_id or "worker" in (t.name or "")),
            None,
        )
        self.assertIsNotNone(worker, "worker teammate must be registered after TeamCreate")
        self.assertNotEqual(
            worker.status, "idle",
            "idle-notification PAST the 64KB cap must NOT transition the teammate — "
            "the _TEAMMATE_MSG_RE scan must be capped at _RELOAD_GATE_SCAN_CAP. "
            f"Got status={worker.status!r}. Fail-safe: staying 'running' over-defers "
            "the reload (recoverable), never SIGKILL."
        )

class TestScanCapSharedConstant(unittest.TestCase):
    """_RELOAD_GATE_SCAN_CAP must come from _constants so guard and team share one object.

    RED at base (22feb3b): _constants.py does not exist → ImportError on
    `from cozempic import _constants`.  All three tests are RED.

    GREEN after P0-A: both guard.py and team.py import from _constants → assertIs
    identity checks pass (same int object via module attribute, not interning).
    """

    def test_guard_cap_imported_from_constants(self):
        """guard._RELOAD_GATE_SCAN_CAP must be the same object as _constants._RELOAD_GATE_SCAN_CAP."""
        from cozempic import guard
        from cozempic import _constants
        self.assertIs(
            guard._RELOAD_GATE_SCAN_CAP,
            _constants._RELOAD_GATE_SCAN_CAP,
            "guard._RELOAD_GATE_SCAN_CAP must be imported from _constants, not defined "
            "locally — a future change to _constants must update both scan sites atomically.",
        )

    def test_team_cap_imported_from_constants(self):
        """team._RELOAD_GATE_SCAN_CAP must be the same object as _constants._RELOAD_GATE_SCAN_CAP."""
        from cozempic import team
        from cozempic import _constants
        self.assertIs(
            team._RELOAD_GATE_SCAN_CAP,
            _constants._RELOAD_GATE_SCAN_CAP,
            "team._RELOAD_GATE_SCAN_CAP must be imported from _constants.",
        )

    def test_guard_and_team_caps_are_same_object(self):
        """guard and team must reference the exact same constant object."""
        from cozempic import guard, team
        self.assertIs(
            guard._RELOAD_GATE_SCAN_CAP,
            team._RELOAD_GATE_SCAN_CAP,
            "guard and team must use the SAME _RELOAD_GATE_SCAN_CAP object — "
            "if they diverge, detect_in_flight and extract_team_state scan "
            "different character windows for the same content.",
        )


class TestTNIDREAttributeTolerance(unittest.TestCase):
    """guard.py _TN_ID_RE must tolerate XML attributes on the <task-id> tag.

    team.py uses `r"<task-id(?:\\s[^>]*)?>([^<]+)</task-id>"` (attribute-tolerant).
    guard.py used the strict `r"<task-id>([^<]+)</task-id>"` which silently misses
    notifications with attributes like `<task-id xmlns="ns">X</task-id>`.

    Divergence: a notification the harness generates with an attributed <task-id>
    would clear the subagent via team.py's extract_team_state but NOT via guard.py's
    detect_in_flight — the launch stays "in-flight", over-deferring the reload
    indefinitely (never SIGKILL, but wedges the gate).

    RED at base (cc292cb / 3eac817 / 22feb3b): _TN_ID_RE = r"<task-id>…"
    → findall on '<task-id xmlns="ns">agent-xyz</task-id>' returns [] → agent not
    cleared → assertFalse(result.get("agent")) FAILS.

    GREEN after P0-B: _TN_ID_RE = r"<task-id(?:\\s[^>]*)?>…" → findall returns
    ["agent-xyz"] → agent cleared → assertFalse passes.
    """

    def _detect(self, raw_text: str) -> dict:
        from cozempic.guard import detect_in_flight
        # Full sequence: Agent launch → spawn-ack (populates launched_agent)
        # → task-notification with attributed <task-id> (must clear it).
        msgs = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "tu-b1", "name": "Agent",
                         "input": {"name": "finder-p1"}}
                    ],
                }
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu-b1",
                         "content": "Async agent launched successfully. agentId: agent-xyz"}
                    ],
                }
            },
            {"type": "user", "content": raw_text},
        ]
        return detect_in_flight(msgs)

    def test_plain_task_id_clears_agent(self):
        """Sanity check: plain <task-id> (no attributes) clears the agent."""
        notif = (
            "<task-notification>"
            "<task-id>agent-xyz</task-id>"
            "<status>completed</status>"
            "<result>done</result>"
            "</task-notification>"
        )
        result = self._detect(notif)
        self.assertFalse(
            result.get("agent"),
            f"Plain <task-id> must clear the agent; got result={result}"
        )

    def test_attributed_task_id_clears_agent(self):
        """_TN_ID_RE must match <task-id xmlns="ns">X</task-id> (attribute-tolerant).

        RED at base: guard.py strict pattern r"<task-id>…" returns [] for
        '<task-id xmlns="ns">agent-xyz</task-id>' → agent not cleared → assertFalse FAILS.
        GREEN after P0-B: attribute-tolerant pattern finds "agent-xyz" → cleared.
        """
        notif_with_attr = (
            "<task-notification>"
            '<task-id xmlns="ns">agent-xyz</task-id>'
            "<status>completed</status>"
            "<result>done</result>"
            "</task-notification>"
        )
        result = self._detect(notif_with_attr)
        self.assertFalse(
            result.get("agent"),
            "detect_in_flight must clear the agent when <task-id> carries XML "
            "attributes — _TN_ID_RE must be attribute-tolerant like team.py's "
            "_TASK_NOTIF_ID_RE; "
            f"got result={result}"
        )

    def test_task_id_with_whitespace_attribute_clears_agent(self):
        """_TN_ID_RE must also match <task-id  data-x="1">X</task-id> (leading space)."""
        notif_space_attr = (
            "<task-notification>"
            '<task-id  data-x="1">agent-xyz</task-id>'
            "<status>completed</status>"
            "<result>done</result>"
            "</task-notification>"
        )
        result = self._detect(notif_space_attr)
        self.assertFalse(
            result.get("agent"),
            "detect_in_flight must clear agent for <task-id> with leading-space "
            "attribute; "
            f"got result={result}"
        )

    def test_attributed_status_clears_agent(self):
        """_TN_STATUS_RE must match <status priority="high">X</status> (attribute-tolerant).

        SIBLING of _TN_ID_RE's attribute-tolerance — same "two parsers agree on the same
        bytes" contract; team.py's _TASK_NOTIF_STATUS_RE is already tolerant.

        RED at base (0bab302): guard.py strict r"<status>…" returns [] for an attributed
        <status> → completion not recorded → agent not cleared → assertFalse FAILS.
        GREEN after the fold: attribute-tolerant pattern finds "completed" → cleared.
        """
        notif_attr_status = (
            "<task-notification>"
            "<task-id>agent-xyz</task-id>"
            '<status priority="high">completed</status>'
            "<result>done</result>"
            "</task-notification>"
        )
        result = self._detect(notif_attr_status)
        self.assertFalse(
            result.get("agent"),
            "detect_in_flight must clear the agent when <status> carries XML "
            "attributes — _TN_STATUS_RE must be attribute-tolerant like team.py's "
            f"_TASK_NOTIF_STATUS_RE; got result={result}"
        )


if __name__ == "__main__":
    unittest.main()
