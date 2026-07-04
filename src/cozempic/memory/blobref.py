"""Lossless blob offload (F8): move a large stable text asset to the mymemories store
verbatim and produce a retrievable pointer stub. No LLM — pure byte move."""

from __future__ import annotations

import hashlib

from . import mem_bridge
from .insight import Insight, TrustClass

_STUB_PREFIX = "[cozempic asset:"


def blob_text_of(block: dict) -> str:
    """Extract the raw text of a text or str-content tool_result block; else ''."""
    btype = block.get("type", "")
    if btype == "text":
        return block.get("text", "") or ""
    if btype == "tool_result" and isinstance(block.get("content"), str):
        return block.get("content", "") or ""
    return ""


def is_offload_eligible(block: dict, min_bytes: int) -> bool:
    """True for large stable TEXT assets. Excludes thinking (F7), images (image-strip),
    and blocks that are already a cozempic stub."""
    text = blob_text_of(block)
    if not text:
        return False
    if text.lstrip().startswith(_STUB_PREFIX):
        return False
    return len(text.encode("utf-8")) >= min_bytes


def _slug_for(name: str, text: str) -> str:
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    safe = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-") or "asset"
    return f"{safe[:40]}-{h}"


def build_pointer_stub(name: str, n_bytes: int, slug: str) -> str:
    kb = max(1, round(n_bytes / 1024))
    return f"{_STUB_PREFIX} {name} — {kb}KB · recall {slug}]"


def offload_block(block: dict, name: str) -> str | None:
    """Persist the block's raw text verbatim as a `type: reference` fact. Returns the slug,
    or None if the project isn't partitioned (caller then leaves the block untouched)."""
    text = blob_text_of(block)
    if not text:
        return None
    slug = _slug_for(name, text)
    ins = Insight(
        slug=slug,
        title=f"asset: {name}",
        description=f"offloaded asset {name} ({len(text.encode('utf-8'))} bytes)",
        type="reference",
        trust_class=TrustClass.AGENT_PROVISIONAL,
        body=text,
    )
    written = mem_bridge.persist_insights("__asset_offload__", [ins])
    return slug if written else None
