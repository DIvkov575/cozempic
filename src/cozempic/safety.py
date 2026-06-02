"""Safety guards for the session pruner.

Implements P0-B and P0-C from the prune-safety defense-in-depth port
onto the v1.8.18 terminate-first flow:

P0-B — Post-prune structural validation:
  - ``PruneValidationError`` exception with reason + evidence dict
  - ``validate_post_prune(msgs_before, msgs_after, strict)`` — raises on C1-C7
  - ``simulate_replay_readiness(messages)`` — structural replay probe

P0-C — Floor preservation:
  - ``enforce_floor(msgs_before, msgs_after, cfg)`` — re-adds must-preserve msgs

P0-D helpers (executor.py owns the tagging; this module owns enforcement):
  - ``FloorConfig`` re-exported from config.py for ergonomic imports

P0-A (idle guard) is intentionally out of scope for this PR.
"""

from __future__ import annotations

import math

from .config import FloorConfig

__all__ = [
    "PruneValidationError",
    "FloorConfig",
    "validate_post_prune",
    "enforce_floor",
    "simulate_replay_readiness",
]


# ── P0-B — Post-prune structural validation ───────────────────────────────────


class PruneValidationError(Exception):
    """Raised when the pruned message list fails structural validation.

    ``reason`` is a human-readable summary. ``evidence`` is a dict that
    callers (guard daemon, CLI) log; it always contains a ``failed_check``
    key matching one of ``"C1".."C7"`` so log aggregators can group failures.
    """

    def __init__(self, reason: str, evidence: dict):
        self.reason = reason
        self.evidence = evidence
        super().__init__(f"Pruned session would not replay cleanly: {reason}")


def _last_of_type(
    messages: list[tuple[int, dict, int]],
    msg_type: str,
) -> dict | None:
    """Return the last message dict whose ``type`` equals ``msg_type``."""
    last: dict | None = None
    for _, msg, _ in messages:
        if msg.get("type") == msg_type:
            last = msg
    return last


def _last_compact_boundary(
    messages: list[tuple[int, dict, int]],
) -> dict | None:
    """Return the last system/subtype=compact_boundary entry."""
    last: dict | None = None
    for _, msg, _ in messages:
        if (msg.get("type") == "system"
                and msg.get("subtype") == "compact_boundary"):
            last = msg
    return last


def validate_post_prune(
    msgs_before: list[tuple[int, dict, int]],
    msgs_after: list[tuple[int, dict, int]],
    *,
    strict: bool = True,
) -> None:
    """Validate the pruned message list. Raise PruneValidationError on failure.

    Checks run fail-fast in order C3 → C2 → C4 → C5 → C6 → C7 → C1 (semantic
    checks before structural so the failure attribution is actionable):

      C1. parentUuid resolution — baseline-relative: only flag a parent that
          WAS in msgs_before (existed pre-prune) but is absent from msgs_after
          (prune introduced a chain break). Cross-session pointers (parent not
          in before_uuids) are valid external anchors; skip them.
      C2. Root preserved — at least one of the original ``parentUuid=null``
          uuids from msgs_before must survive. Multi-root sessions supported
          via set-intersection (REVIEW-max B.11).
      C3. Conversation survival — ≥1 user AND ≥1 assistant survives.
      C4. compact_boundary — if msgs_before had a system/compact_boundary
          entry, the LAST such entry MUST survive.
      C5. permission-mode — if msgs_before had permission-mode entries, the
          LAST one MUST survive.
      C6. last-prompt — if msgs_before had last-prompt entries, the LAST one
          MUST survive.
      C7. ai-title — if msgs_before had ai-title entries, the LAST one MUST
          survive (REVIEW-max E.4).

    Each check is CONDITIONAL on its precondition existing in msgs_before —
    a session that never had a permission-mode entry passes C5 trivially.
    """
    surviving_uuids: set[str] = {
        msg.get("uuid", "") for _, msg, _ in msgs_after if msg.get("uuid")
    }

    # ── C3: conversation survival ─────────────────────────────────────────────
    # Check before C2/C1 because a wholesale wipe is more actionable to report
    # as "no conversation" than as "root dropped" or "chain break".
    surviving_user = sum(1 for _, m, _ in msgs_after if m.get("type") == "user")
    surviving_asst = sum(1 for _, m, _ in msgs_after if m.get("type") == "assistant")
    before_user = any(m.get("type") == "user" for _, m, _ in msgs_before)
    before_asst = any(m.get("type") == "assistant" for _, m, _ in msgs_before)
    if (before_user and surviving_user == 0) or (before_asst and surviving_asst == 0):
        raise PruneValidationError(
            reason=(
                f"conversation wiped — surviving users={surviving_user}, "
                f"assistants={surviving_asst}"
            ),
            evidence={
                "failed_check": "C3",
                "surviving_user_count": surviving_user,
                "surviving_assistant_count": surviving_asst,
            },
        )

    # ── C2: original root uuid preserved ─────────────────────────────────────
    # Review finding H-1: a structural ``any(parentUuid is None)`` check is
    # bypassed by _relink_parent_chain re-pointing dead-end chains to None
    # when the original root is dropped. Require that AT LEAST ONE of the
    # original parentUuid=null uuids from msgs_before survives (REVIEW-max B.11).
    original_root_uuids: set[str] = set()
    for _, msg, _ in msgs_before:
        if msg.get("parentUuid") is None and msg.get("uuid"):
            original_root_uuids.add(msg["uuid"])
    if original_root_uuids and not (original_root_uuids & surviving_uuids):
        raise PruneValidationError(
            reason=(
                f"every original session root uuid was dropped "
                f"(expected one of {sorted(original_root_uuids)} to survive)"
            ),
            evidence={
                "failed_check": "C2",
                "expected_root_uuid": sorted(original_root_uuids)[0],
                "expected_root_uuids": sorted(original_root_uuids),
                "before_count": len(msgs_before),
                "after_count": len(msgs_after),
            },
        )

    # ── C4: last compact_boundary preserved ──────────────────────────────────
    last_before_cb = _last_compact_boundary(msgs_before)
    if last_before_cb is not None:
        last_after_cb = _last_compact_boundary(msgs_after)
        if last_after_cb is None or (
            last_after_cb.get("uuid") != last_before_cb.get("uuid")
        ):
            raise PruneValidationError(
                reason="last compact_boundary entry was dropped",
                evidence={
                    "failed_check": "C4",
                    "expected_uuid": last_before_cb.get("uuid"),
                    "actual_uuid": last_after_cb.get("uuid") if last_after_cb else None,
                },
            )

    # ── C5: last permission-mode preserved ───────────────────────────────────
    last_before_pm = _last_of_type(msgs_before, "permission-mode")
    if last_before_pm is not None:
        last_after_pm = _last_of_type(msgs_after, "permission-mode")
        if last_after_pm is None or (
            last_after_pm.get("uuid") != last_before_pm.get("uuid")
        ):
            raise PruneValidationError(
                reason="last permission-mode entry was dropped",
                evidence={
                    "failed_check": "C5",
                    "expected_uuid": last_before_pm.get("uuid"),
                    "actual_uuid": last_after_pm.get("uuid") if last_after_pm else None,
                },
            )

    # ── C6: last last-prompt preserved ───────────────────────────────────────
    last_before_lp = _last_of_type(msgs_before, "last-prompt")
    if last_before_lp is not None:
        last_after_lp = _last_of_type(msgs_after, "last-prompt")
        if last_after_lp is None or (
            last_after_lp.get("uuid") != last_before_lp.get("uuid")
        ):
            raise PruneValidationError(
                reason="last last-prompt entry was dropped",
                evidence={
                    "failed_check": "C6",
                    "expected_uuid": last_before_lp.get("uuid"),
                    "actual_uuid": last_after_lp.get("uuid") if last_after_lp else None,
                },
            )

    # ── C7: last ai-title preserved ───────────────────────────────────────────
    # REVIEW-max E.4: mirror C5/C6 for the third member of
    # executor._LAST_OF_TYPE_PROTECTED; any path that bypasses the singleton
    # tag would silently drop the last ai-title without this check.
    last_before_at = _last_of_type(msgs_before, "ai-title")
    if last_before_at is not None:
        last_after_at = _last_of_type(msgs_after, "ai-title")
        if last_after_at is None or (
            last_after_at.get("uuid") != last_before_at.get("uuid")
        ):
            raise PruneValidationError(
                reason="last ai-title entry was dropped",
                evidence={
                    "failed_check": "C7",
                    "expected_uuid": last_before_at.get("uuid"),
                    "actual_uuid": last_after_at.get("uuid") if last_after_at else None,
                },
            )

    # ── C1: parent chain resolves (baseline-relative) ────────────────────────
    # Defense-in-depth fallback. The executor's _relink_parent_chain step
    # SHOULD ensure every surviving parentUuid resolves; this re-verifies.
    # REVIEW-max B.10: treat falsy parentUuid (None, "", 0, ...) as equivalent
    # to None — empty string isn't a chain reference.
    #
    # PR #102 fix — unconditionally baseline-relative:
    # Skip any parent absent from before_uuids (never existed in this session
    # before the prune) — it is a cross-session pointer and is NOT a regression
    # introduced by this prune. Only raise when the parent WAS in before_uuids
    # (existed pre-prune) but is absent from surviving_uuids (prune removed it,
    # breaking the chain).
    before_uuids: set[str] = {
        msg.get("uuid", "") for _, msg, _ in msgs_before if msg.get("uuid")
    }
    for _, msg, _ in msgs_after:
        parent = msg.get("parentUuid")
        if not parent:
            continue
        if parent not in surviving_uuids:
            if parent not in before_uuids:
                # Parent was never in this file — cross-session pointer; skip.
                continue
            # Parent was in before_uuids (existed pre-prune) but absent after
            # — the prune introduced this chain break. Raise C1.
            raise PruneValidationError(
                reason=(
                    f"parentUuid {parent!r} on uuid {msg.get('uuid')!r} "
                    f"resolved before prune but is absent after — "
                    f"prune introduced a chain break"
                ),
                evidence={
                    "failed_check": "C1",
                    "dangling_uuid": msg.get("uuid"),
                    "dangling_parent": parent,
                    "surviving_count": len(surviving_uuids),
                },
            )


def simulate_replay_readiness(
    messages: list[tuple[int, dict, int]],
) -> tuple[bool, str]:
    """Structural probe: walk the parentUuid graph as Claude Code's resume would.

    Returns ``(ok, reason)``. ``ok=False`` means the session would not
    bootstrap cleanly. ``ok=True`` returns reason="".

    PR #102 fix: cross-session pointers (parentUuid not defined as any uuid
    in this file) are treated as external anchors, NOT chain breaks. A valid
    session anchor is any message whose parentUuid is either None OR an
    external UUID not defined in this file (both are valid chain heads).
    """
    if not messages:
        return False, "empty message list"

    surviving_uuids: set[str] = {
        m.get("uuid", "") for _, m, _ in messages if m.get("uuid")
    }

    # At least one message must be a chain anchor: parentUuid is absent from
    # surviving_uuids (either None = true root, or external UUID = cross-session
    # anchor). A session where every parentUuid resolves within the file in a
    # cycle has no bootstrappable entry point.
    has_anchor = any(
        m.get("parentUuid") is None or m.get("parentUuid") not in surviving_uuids
        for _, m, _ in messages
        if m.get("uuid")  # ignore metadata entries with no uuid
    )
    if not has_anchor:
        return False, "no chain anchor (no parentUuid=null or external entry; possible cycle)"

    # Conversation must include at least one user AND one assistant.
    has_user = any(m.get("type") == "user" for _, m, _ in messages)
    has_asst = any(m.get("type") == "assistant" for _, m, _ in messages)
    if not (has_user and has_asst):
        return False, "no conversation (zero users or zero assistants)"

    return True, ""


# ── P0-C — Floor preservation ─────────────────────────────────────────────────


def enforce_floor(
    msgs_before: list[tuple[int, dict, int]],
    msgs_after: list[tuple[int, dict, int]],
    *,
    cfg: FloorConfig,
) -> list[tuple[int, dict, int]]:
    """Re-add must-preserve messages dropped by strategies.

    Algorithm:

      1. Compute ``kept_uuids`` = uuids present in ``msgs_after``.
      2. Identify ``must_preserve_uuids`` from ``msgs_before``:
         (a) First parentUuid=null message (if ``preserve_first_message``).
         (b) Last ``preserve_last_k_turns`` user + assistant by line order.
         (c) Enough additional user/assistant to bring survival ≥
             ``(1 - max_user_assistant_drop_pct)``, most-recent first.
      3. Pair-counterpart closure (REVIEW-max C.5): when re-adding a message
         that carries a tool_use/tool_result, also add the paired counterpart
         to avoid orphaned blocks after orphan-fix.
      4. Re-insert the ORIGINAL msgs_before entries at positions that preserve
         line-index ordering.
      5. Re-run _relink_parent_chain to fix any newly-broken pointers.

    A replaced-in-place message (same uuid, modified payload) is already in
    ``kept_uuids``, so the floor does NOT revert the replacement — the
    truncated/modified version stays (REVIEW-round3 F.N4).
    """
    from .executor import _relink_parent_chain

    # ── Step 1: what survived the strategies ─────────────────────────────────
    kept_uuids: set[str] = {
        m.get("uuid", "") for _, m, _ in msgs_after if m.get("uuid")
    }

    # ── Step 2: must-preserve candidates ─────────────────────────────────────
    must_preserve: set[str] = set()

    if cfg.preserve_first_message:
        for _, msg, _ in msgs_before:
            if msg.get("parentUuid") is None and msg.get("uuid"):
                must_preserve.add(msg["uuid"])
                break

    users_in_order = [
        (idx, m) for idx, m, _ in msgs_before if m.get("type") == "user"
    ]
    asst_in_order = [
        (idx, m) for idx, m, _ in msgs_before if m.get("type") == "assistant"
    ]

    last_k = max(0, int(cfg.preserve_last_k_turns))
    for _, m in (users_in_order[-last_k:] if last_k > 0 else []):
        if m.get("uuid"):
            must_preserve.add(m["uuid"])
    for _, m in (asst_in_order[-last_k:] if last_k > 0 else []):
        if m.get("uuid"):
            must_preserve.add(m["uuid"])

    # (c) Survival cap: top up to (1 - max_drop_pct) of each kind, most-recent first.
    # REVIEW-max E.6: use math.ceil for the survival-target rounding. Skip the cap
    # entirely on micro-sessions (total < 2) — intended for bulk-prune disasters,
    # not single-message sessions.
    survival_floor_pct = 1.0 - float(cfg.max_user_assistant_drop_pct)
    if survival_floor_pct > 0.0:
        for in_order in (users_in_order, asst_in_order):
            total = len(in_order)
            if total < 2:
                continue
            preserved = sum(
                1 for _, m in in_order
                if (u := m.get("uuid", "")) and (u in kept_uuids or u in must_preserve)
            )
            target = math.ceil(survival_floor_pct * total)
            if preserved >= target:
                continue
            for _, m in reversed(in_order):
                if preserved >= target:
                    break
                u = m.get("uuid", "")
                if not u or u in kept_uuids or u in must_preserve:
                    continue
                must_preserve.add(u)
                preserved += 1

    # ── Step 3: pair-counterpart closure (REVIEW-max C.5) ────────────────────
    # When re-adding a message that carries a tool_use or tool_result, also add
    # its paired counterpart. Repeat until stable (normally 1-hop).
    before_by_uuid: dict[str, tuple[int, dict, int]] = {
        m.get("uuid", ""): (idx, m, size)
        for idx, m, size in msgs_before
        if m.get("uuid")
    }
    # Build set-valued maps (defensive against malformed sessions with duplicate ids).
    tool_use_id_to_owner: dict[str, set[str]] = {}
    tool_use_id_to_results: dict[str, set[str]] = {}
    for _, m, _ in msgs_before:
        u = m.get("uuid", "")
        if not u:
            continue
        content = m.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                tid = block.get("id", "")
                if tid:
                    tool_use_id_to_owner.setdefault(tid, set()).add(u)
            elif btype == "tool_result":
                tid = block.get("tool_use_id", "")
                if tid:
                    tool_use_id_to_results.setdefault(tid, set()).add(u)

    while True:
        new_additions: set[str] = set()
        for u in must_preserve:
            entry = before_by_uuid.get(u)
            if entry is None:
                continue
            _, m, _ = entry
            content = m.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    tid = block.get("id", "")
                    for p in tool_use_id_to_results.get(tid, set()):
                        if p not in must_preserve:
                            new_additions.add(p)
                elif btype == "tool_result":
                    tid = block.get("tool_use_id", "")
                    for owner in tool_use_id_to_owner.get(tid, set()):
                        if owner not in must_preserve:
                            new_additions.add(owner)
        if not new_additions:
            break
        must_preserve.update(new_additions)

    # ── Step 4: re-insert dropped must-preserve entries in line-index order ──
    # to_re_add excludes kept_uuids so in-place replacements (same uuid,
    # modified payload) are NEVER re-inserted from msgs_before (REVIEW-round3 F.N4).
    to_re_add = must_preserve - kept_uuids
    assert kept_uuids.isdisjoint(to_re_add), (
        "enforce_floor invariant violated: a kept (possibly replaced) uuid was "
        "scheduled for re-insertion from msgs_before. Re-inserting would silently "
        "revert any strategy replacement."
    )
    if not to_re_add:
        return msgs_after

    re_add_entries = [before_by_uuid[u] for u in to_re_add if u in before_by_uuid]

    # Merge sorted by line index to preserve JSONL line-order invariant.
    merged = list(msgs_after) + re_add_entries
    merged.sort(key=lambda t: t[0])

    # ── Step 5: re-link parent chains ────────────────────────────────────────
    # Re-added entries may have their original parentUuid pointing to a
    # still-removed ancestor; relink resolves the chain forward.
    merged_uuids = {m.get("uuid", "") for _, m, _ in merged if m.get("uuid")}
    effective_removals: set[int] = set()
    for idx, msg, _ in msgs_before:
        u = msg.get("uuid", "")
        if u and u not in merged_uuids:
            effective_removals.add(idx)

    return _relink_parent_chain(msgs_before, merged, removals=effective_removals)
