# src/cozempic/strategies/asset_offload.py
"""F8 — lossless blob offload: move large stable text assets to the durable store and
leave a retrievable pointer stub in-window. Sync, no LLM."""

from __future__ import annotations

from ..digest import _sanitize_for_injection
from ..helpers import get_content_blocks, get_msg_type, is_protected, msg_bytes, set_content_blocks
from ..memory import blobref
from ..registry import strategy
from ..types import Message, PruneAction, StrategyResult
from ._config import coerce_non_negative_int


def _asset_name(block: dict, idx: int) -> str:
    """Best-effort human name for the pointer. tool_result → tool id; else positional."""
    tid = block.get("tool_use_id") or block.get("id")
    if isinstance(tid, str) and tid:
        return tid[:40]
    return f"asset-{idx}"


@strategy("asset-offload", "Offload large stable text assets to durable store, leave pointer",
          "standard", "5-40%")
def strategy_asset_offload(messages: list[Message], config: dict) -> StrategyResult:
    min_bytes = coerce_non_negative_int(config, "asset_offload_min_bytes", default=8192)
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)
    total_pruned = 0
    replaced = 0

    for idx, msg, size in messages:
        if is_protected(msg):
            continue
        if get_msg_type(msg) not in ("assistant", "user"):
            continue
        blocks = get_content_blocks(msg)
        if not blocks:
            continue

        new_blocks = []
        changed = False
        for block in blocks:
            if blobref.is_offload_eligible(block, min_bytes):
                name = _asset_name(block, idx)
                slug = blobref.offload_block(block, name)
                if slug:
                    n_bytes = len(blobref.blob_text_of(block).encode("utf-8"))
                    stub = blobref.build_pointer_stub(name, n_bytes, slug)
                    # `name` derives from tool_use_id/id (model/tool-controlled =
                    # untrusted); the stub becomes in-window text, so sanitize it
                    # before re-embedding to neutralize injected markdown/newlines.
                    stub = _sanitize_for_injection(stub, limit=200)
                    new_blocks.append({"type": "text", "text": stub})
                    changed = True
                    continue
            new_blocks.append(block)

        if changed:
            new_msg = set_content_blocks(msg, new_blocks)
            new_size = msg_bytes(new_msg)
            saved = size - new_size
            if saved > 0:
                actions.append(PruneAction(
                    line_index=idx,
                    action="replace",
                    reason="asset-offload",
                    original_bytes=size,
                    pruned_bytes=new_size,
                    replacement=new_msg,
                ))
                total_pruned += saved
                replaced += 1

    return StrategyResult(
        strategy_name="asset-offload",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=total_pruned,
        messages_affected=replaced,
        messages_removed=0,
        messages_replaced=replaced,
        summary=f"Offloaded assets in {replaced} message(s)",
    )
