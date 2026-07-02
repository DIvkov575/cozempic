"""Recoverability-gated pruning: drop spans whose content is durably captured.

A message is removable once its content lives as a memory (ledger has its span hash).
Uncaptured messages are untouched — other strategies decide their fate.
"""

from __future__ import annotations

from ..helpers import is_protected
from ..memory import ledger
from ..registry import strategy
from ..types import Message, PruneAction, StrategyResult


@strategy("recoverability", "Drop spans already captured as durable memories",
          "aggressive", "10-40%")
def strategy_recoverability(messages: list[Message], config: dict) -> StrategyResult:
    session_id = config.get("session_id", "")
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)

    if session_id:
        for idx, msg, size in messages:
            if is_protected(msg):
                continue
            if ledger.is_captured(session_id, ledger.span_hash([msg])):
                actions.append(PruneAction(
                    line_index=idx,
                    action="remove",
                    reason="captured as durable memory (recoverable via /recall)",
                    original_bytes=size,
                    pruned_bytes=0,
                ))

    removed = len(actions)
    pruned_bytes = sum(a.original_bytes for a in actions)
    return StrategyResult(
        strategy_name="recoverability",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=pruned_bytes,
        messages_affected=removed,
        messages_removed=removed,
        messages_replaced=0,
        summary=f"Removed {removed} capture-confirmed message(s)",
    )
