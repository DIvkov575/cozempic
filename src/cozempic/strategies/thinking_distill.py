"""F7 sync half — replace distilled thinking blocks with their decision points inline,
else fall back to lossless signature-only. Replaces the old thinking-blocks strategy."""

from __future__ import annotations

from ..helpers import (
    _sanitize_for_injection, get_content_blocks, get_msg_type, is_protected,
    msg_bytes, set_content_blocks,
)
from ..memory import ledger
from ..registry import strategy
from ..types import Message, PruneAction, StrategyResult
from ._config import coerce_choice

_THINKING_MODES = ("distill", "signature-only", "remove")


def _load_decision(slug: str) -> str | None:
    """Read the distilled decision text back from the store by slug. Best-effort."""
    from ..memory.mem_bridge import resolve_partition
    part = resolve_partition()
    if part is None:
        return None
    f = part / f"{slug}.md"
    if not f.exists():
        return None
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return None
    # strip frontmatter (--- ... ---) → body
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3:]
    return text.strip() or None


def _strip_signature(block: dict) -> dict:
    return {k: v for k, v in block.items() if k != "signature"}


@strategy("thinking-blocks", "Distill thinking into decision points inline (fallback: signature-only)",
          "standard", "2-8%")
def strategy_thinking_distill(messages: list[Message], config: dict) -> StrategyResult:
    mode = coerce_choice(config, "thinking_mode", _THINKING_MODES, default="distill")
    session_id = config.get("session_id", "")
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0
    replaced = 0

    for idx, msg, size in messages:
        if is_protected(msg):
            continue
        if get_msg_type(msg) != "assistant":
            continue
        blocks = get_content_blocks(msg)
        if not blocks:
            continue

        new_blocks = []
        changed = False
        for block in blocks:
            if block.get("type") == "thinking":
                if mode == "remove":
                    changed = True
                    continue
                if mode == "distill" and session_id and ledger.is_block_captured(session_id, block):
                    slug = ledger.slug_for_block(session_id, block)
                    decision = _load_decision(slug) if slug else None
                    if decision:
                        safe_decision = _sanitize_for_injection(decision, limit=1000)
                        new_blocks.append({"type": "text",
                                           "text": f"[distilled reasoning · recall {slug}]\n{safe_decision}"})
                        changed = True
                        continue
                # fallback: signature-only (lossless — keep reasoning, drop signature)
                nb = _strip_signature(block)
                new_blocks.append(nb)
                if nb != block:
                    changed = True
            else:
                if "signature" in block:
                    changed = True
                    new_blocks.append(_strip_signature(block))
                else:
                    new_blocks.append(block)

        if changed:
            new_msg = set_content_blocks(msg, new_blocks)
            new_size = msg_bytes(new_msg)
            saved = size - new_size
            if saved > 0:
                actions.append(PruneAction(
                    line_index=idx, action="replace", reason=f"thinking-distill ({mode})",
                    original_bytes=size, pruned_bytes=new_size, replacement=new_msg,
                ))
                total_pruned += saved
                replaced += 1

    return StrategyResult(
        strategy_name="thinking-blocks", actions=actions, original_bytes=total_orig,
        pruned_bytes=total_pruned, messages_affected=replaced, messages_removed=0,
        messages_replaced=replaced, summary=f"Thinking: {replaced} block(s) (mode={mode})",
    )
