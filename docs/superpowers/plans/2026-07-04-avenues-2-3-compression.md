# Avenues 2 & 3: Thinking Distillation + Lossless Blob Offload — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two compression avenues to cozempic and retire the redundant pieces they subsume: (F7) distill fat `thinking` blocks into inline decision-point text with a `→ recall` pointer, and (F8) offload large stable text assets verbatim to the mymemories store, replacing them in-window with a lossless retrievable pointer stub.

**Architecture:** F8 is a pure synchronous strategy (`asset-offload`) — no LLM, a byte move + pointer. F7 has two halves: the existing background worker (F1a) gains a thinking-distillation pass that persists decision-point text and records a **block-hash** ledger entry; a sync strategy (`thinking_distill`, replacing `thinking-blocks`) swaps distilled blocks for their decision text and falls back to lossless `signature-only` otherwise. Both key their ledger entries by `span_hash([block])` — a distinct namespace from `recoverability`'s `span_hash([msg])` — so a mutated-in-place block never makes its host message look removable. Prescriptions are updated and the subsumed strategies retired/demoted per HLD §7.

**Tech Stack:** Python 3.11, stdlib, pytest. Reuses: `@strategy` registry + `StrategyResult`/`PruneAction`/`Message` (`types.py`); `helpers.get_content_blocks`/`set_content_blocks`/`text_of`/`get_msg_type`/`is_protected`/`content_block_bytes`/`msg_bytes`; `memory/ledger.py` (`span_hash`, `record`, `is_captured`, `slug_for`); `memory/mem_bridge.py` (`persist_insights`, `resolve_partition`); `memory/insight.py` (`Insight`, `TrustClass`); `memory/extract.py` (pluggable backend); `memory/schedule.py` (`consolidate_worker`); `memory/tail.py` (`build_tail_message`). Verified against the branch HEAD (`docs/HLD-memory-overhaul.md` §5.8, §5.9, §7).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/cozempic/memory/ledger.py` (modify) | Add `record_block(session_id, block, slug)` + reuse `span_hash([block])`; block-hash namespace helper. |
| `src/cozempic/memory/blobref.py` (new) | Detect offload-eligible blocks; write blob verbatim as a `type: reference` fact; build the pointer stub string. Pure, no LLM. |
| `src/cozempic/strategies/asset_offload.py` (new) | `@strategy("asset-offload", ...)` — F8 sync strategy: offload large stable text assets, replace block with pointer. |
| `src/cozempic/memory/distill.py` (new) | `distill_thinking(thinking_text, backend) -> str | None` — LLM decision-point distillation (pluggable, reuses extract backend). |
| `src/cozempic/memory/schedule.py` (modify) | `consolidate_worker` also distills large thinking blocks → persist decision text + `record_block`. |
| `src/cozempic/strategies/thinking_distill.py` (new) | `@strategy("thinking-blocks", ...)` renamed impl → `thinking_distill`; distilled→inline decision text, else `signature-only`. Replaces `standard.py`'s `strategy_thinking_blocks`. |
| `src/cozempic/strategies/standard.py` (modify) | Remove `strategy_thinking_blocks` (moved to thinking_distill.py). |
| `src/cozempic/strategies/__init__.py` (modify) | Import the two new strategy modules. |
| `src/cozempic/registry.py` (modify) | Prescription edits per HLD §7. |
| `src/cozempic/memory/tail.py` (modify) | Tail block gains an "Offloaded assets" section (F8 pointers). |
| `tests/memory/`, `tests/strategies/` | One test module per unit. |

**Block hashing contract (shared):** F7/F8 ledger entries are keyed by `ledger.span_hash([block])` where `block` is the content-block dict. This is deliberately the SAME hash function `recoverability` uses, but applied to a *block* dict rather than a *message* dict — the two never collide in practice (different dict shapes) and semantically live in separate namespaces. Add `record_block`/`is_block_captured`/`slug_for_block` thin wrappers so the namespace intent is explicit in code.

---

### Task 1: Ledger block-namespace helpers

**Files:**
- Modify: `src/cozempic/memory/ledger.py`
- Test: `tests/memory/test_ledger_block.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_ledger_block.py
from cozempic.memory import ledger


def test_record_block_and_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    block = {"type": "thinking", "thinking": "long reasoning here"}
    assert ledger.is_block_captured("s1", block) is False
    ledger.record_block("s1", block, "decision-slug")
    assert ledger.is_block_captured("s1", block) is True
    assert ledger.slug_for_block("s1", block) == "decision-slug"


def test_block_and_message_namespaces_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    # A block captured for distillation must NOT make a message-hash lookup hit.
    block = {"type": "thinking", "thinking": "x"}
    ledger.record_block("s1", block, "slug-a")
    # recoverability would look up span_hash([msg]); a different dict shape → different hash
    msg = {"role": "assistant", "content": [block]}
    assert ledger.is_captured("s1", ledger.span_hash([msg])) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/memory/test_ledger_block.py -v`
Expected: FAIL — `AttributeError: module 'cozempic.memory.ledger' has no attribute 'record_block'`

- [ ] **Step 3: Add the helpers to `src/cozempic/memory/ledger.py`**

Append after the existing `slug_for`:

```python
def record_block(session_id: str, block: dict, slug: str) -> None:
    """Record a distilled/offloaded content BLOCK (namespace distinct from message spans)."""
    record(session_id, span_hash([block]), slug)


def is_block_captured(session_id: str, block: dict) -> bool:
    return is_captured(session_id, span_hash([block]))


def slug_for_block(session_id: str, block: dict) -> str | None:
    return slug_for(session_id, span_hash([block]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/memory/test_ledger_block.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cozempic/memory/ledger.py tests/memory/test_ledger_block.py
git commit -m "feat(memory): block-hash ledger namespace for distill/offload"
```

---

### Task 2: Blob reference writer (`blobref.py`)

**Files:**
- Create: `src/cozempic/memory/blobref.py`
- Test: `tests/memory/test_blobref.py`

Pure helpers for F8: decide eligibility, write a verbatim blob as a `type: reference` fact, and build the pointer stub. No LLM. Persistence reuses `mem_bridge`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_blobref.py
from cozempic.memory import blobref, mem_bridge
from cozempic.memory.insight import Insight, TrustClass


def test_is_offload_eligible():
    big = "x" * 9000
    assert blobref.is_offload_eligible({"type": "text", "text": big}, min_bytes=8192) is True
    assert blobref.is_offload_eligible({"type": "text", "text": "short"}, min_bytes=8192) is False
    # thinking and image are NOT F8's job
    assert blobref.is_offload_eligible({"type": "thinking", "thinking": big}, min_bytes=8192) is False
    assert blobref.is_offload_eligible({"type": "image", "source": {}}, min_bytes=8192) is False
    # already a cozempic stub → not eligible
    assert blobref.is_offload_eligible({"type": "text", "text": "[cozempic asset: a — 9KB · recall s]"}, min_bytes=1) is False


def test_blob_text_of_handles_str_and_list_tool_result():
    assert blobref.blob_text_of({"type": "text", "text": "hello"}) == "hello"
    assert blobref.blob_text_of({"type": "tool_result", "content": "raw"}) == "raw"


def test_build_pointer_stub_shape():
    stub = blobref.build_pointer_stub(name="report.md", n_bytes=9216, slug="report-md-abc123")
    assert "cozempic asset" in stub
    assert "report.md" in stub
    assert "recall report-md-abc123" in stub
    assert "9" in stub  # KB shown


def test_offload_block_writes_reference_and_returns_slug(tmp_path, monkeypatch):
    part = tmp_path / "proj"; part.mkdir(); (part / "MEMORY.md").write_text("# Memories\n")
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: part)
    monkeypatch.setattr(mem_bridge, "_reindex", lambda: None)
    block = {"type": "text", "text": "y" * 9000}
    slug = blobref.offload_block(block, name="doc")
    assert slug is not None
    files = list(part.glob("*.md"))
    body = next(f for f in files if f.name != "MEMORY.md").read_text()
    assert "y" * 9000 in body           # verbatim
    assert "type: reference" in body


def test_offload_block_noop_when_unpartitioned(monkeypatch):
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: None)
    assert blobref.offload_block({"type": "text", "text": "z" * 9000}, name="doc") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/memory/test_blobref.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cozempic.memory.blobref'`

- [ ] **Step 3: Write `src/cozempic/memory/blobref.py`**

```python
# src/cozempic/memory/blobref.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/memory/test_blobref.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cozempic/memory/blobref.py tests/memory/test_blobref.py
git commit -m "feat(memory): verbatim blob offload writer + pointer stub (F8 core)"
```

---

### Task 3: `asset-offload` strategy (F8)

**Files:**
- Create: `src/cozempic/strategies/asset_offload.py`
- Modify: `src/cozempic/strategies/__init__.py` (import to register)
- Test: `tests/strategies/test_asset_offload.py` (create `tests/strategies/__init__.py` if missing)

Sync strategy: for each non-protected message, offload eligible large text-asset blocks and replace them in-window with the pointer stub. Reuses `blobref`. `blobref.offload_block` is monkeypatched in tests so no real store is touched.

- [ ] **Step 1: Write the failing test**

```python
# tests/strategies/test_asset_offload.py
import json
from cozempic.strategies import asset_offload
from cozempic.memory import blobref


def _asst(idx, blocks):
    d = {"type": "assistant", "message": {"role": "assistant", "content": blocks}}
    return (idx, d, len(json.dumps(d)))


def test_offloads_large_text_block_and_leaves_pointer(monkeypatch):
    monkeypatch.setattr(blobref, "offload_block", lambda block, name: "doc-abc12345")
    big = {"type": "text", "text": "x" * 9000}
    small = {"type": "text", "text": "keep me"}
    result = asset_offload.strategy_asset_offload([_asst(0, [big, small])], {"asset_offload_min_bytes": 8192})
    assert result.messages_replaced == 1
    repl = result.actions[0].replacement
    from cozempic.helpers import get_content_blocks
    blocks = get_content_blocks(repl)
    assert any("cozempic asset" in b.get("text", "") and "recall doc-abc12345" in b.get("text", "") for b in blocks)
    assert any(b.get("text") == "keep me" for b in blocks)  # small block untouched


def test_skips_when_offload_returns_none(monkeypatch):
    monkeypatch.setattr(blobref, "offload_block", lambda block, name: None)  # unpartitioned
    big = {"type": "text", "text": "x" * 9000}
    result = asset_offload.strategy_asset_offload([_asst(0, [big])], {})
    assert result.actions == []  # nothing changed, block left intact


def test_ignores_thinking_and_images(monkeypatch):
    monkeypatch.setattr(blobref, "offload_block", lambda block, name: "should-not-be-called")
    think = {"type": "thinking", "thinking": "x" * 9000}
    img = {"type": "image", "source": {"data": "x" * 9000}}
    result = asset_offload.strategy_asset_offload([_asst(0, [think, img])], {})
    assert result.actions == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/strategies/test_asset_offload.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cozempic.strategies.asset_offload'`

- [ ] **Step 3: Write `src/cozempic/strategies/asset_offload.py`**

```python
# src/cozempic/strategies/asset_offload.py
"""F8 — lossless blob offload: move large stable text assets to the durable store and
leave a retrievable pointer stub in-window. Sync, no LLM."""

from __future__ import annotations

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
```

- [ ] **Step 4: Register — add to `src/cozempic/strategies/__init__.py`**

Read the file first; append alongside existing imports:

```python
from . import asset_offload  # noqa: F401  (registers @strategy)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/strategies/test_asset_offload.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add src/cozempic/strategies/asset_offload.py src/cozempic/strategies/__init__.py tests/strategies/test_asset_offload.py tests/strategies/__init__.py
git commit -m "feat(strategies): asset-offload (F8) lossless blob pointer"
```

---

### Task 4: Thinking distillation — worker half (`distill.py` + schedule)

**Files:**
- Create: `src/cozempic/memory/distill.py`
- Modify: `src/cozempic/memory/schedule.py`
- Test: `tests/memory/test_distill.py`

`distill_thinking` turns a thinking block's text into a compact decision-point string via the pluggable backend (default `claude_cli.run_claude`). The worker distills each large thinking block, persists the decision text (F2), and records a **block-hash** ledger entry.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_distill.py
from cozempic.memory.distill import distill_thinking, build_distill_prompt


def test_prompt_asks_for_decision_points():
    p = build_distill_prompt("I considered A, rejected B, chose C because fast")
    assert "decision" in p.lower()
    assert "chose C" in p  # source text embedded


def test_distill_returns_text_from_backend():
    out = distill_thinking("reasoning...", backend=lambda _p: "Decision: chose C (fast).")
    assert out == "Decision: chose C (fast)."


def test_distill_empty_on_blank_backend():
    assert distill_thinking("reasoning...", backend=lambda _p: "") is None
    assert distill_thinking("", backend=lambda _p: "anything") is None
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/memory/test_distill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cozempic.memory.distill'`

- [ ] **Step 3: Write `src/cozempic/memory/distill.py`**

```python
# src/cozempic/memory/distill.py
"""F7 worker half — distill a thinking block into its decision points (LLM, pluggable)."""

from __future__ import annotations

from typing import Callable

from .claude_cli import run_claude

Backend = Callable[[str], str]

_PROMPT = """\
Below is a model's internal reasoning (thinking) block. Extract ONLY the decision points —
the conclusions and choices it actually reached (what was decided, chosen, or ruled out and
why), in at most a few terse lines. Preserve the wording of each decision; do not add
commentary or restate the deliberation. Output plain text, no preamble.

THINKING:
{text}
"""


def build_distill_prompt(text: str) -> str:
    return _PROMPT.format(text=text)


def distill_thinking(text: str, backend: Backend = run_claude) -> str | None:
    """Return decision-point text, or None if input/backend is empty."""
    if not text.strip():
        return None
    out = backend(build_distill_prompt(text)).strip()
    return out or None
```

- [ ] **Step 4: Extend `consolidate_worker` in `src/cozempic/memory/schedule.py`**

Read the current `consolidate_worker` first. After the existing insight extract→persist→record_span, add a thinking-distillation pass over the span's assistant thinking blocks. Add imports at top: `from .distill import distill_thinking`, `from .blobref import` is NOT needed here, and `from .insight import Insight, TrustClass`. Extend the worker:

```python
def _distill_thinking_blocks(session_id: str, span_msgs: list[dict]) -> None:
    """For each large thinking block in the span, persist its decision-point distillation
    and record a block-hash ledger entry (F7 worker half)."""
    from .mem_bridge import persist_insights
    from ..helpers import get_content_blocks
    MIN = 500  # only distill blocks worth the LLM call
    for m in span_msgs:
        for block in get_content_blocks(m):
            if block.get("type") != "thinking":
                continue
            text = block.get("thinking", "") or ""
            if len(text) < MIN:
                continue
            if ledger.is_block_captured(session_id, block):
                continue  # already distilled
            decision = distill_thinking(text)
            if not decision:
                continue
            slug = f"decision-{ledger.span_hash([block])}"
            ins = Insight(
                slug=slug,
                title="decision point",
                description=decision[:120],
                type="reference",
                trust_class=TrustClass.AGENT_PROVISIONAL,
                body=decision,
            )
            written = persist_insights(session_id, [ins])
            if written:
                ledger.record_block(session_id, block, slug)
```

Then call it at the end of `consolidate_worker` (after the existing `record_span`):

```python
    _distill_thinking_blocks(session_id, span_msgs)
```

- [ ] **Step 5: Add a worker test to `tests/memory/test_distill.py`**

```python
def test_worker_distills_and_records_block(tmp_path, monkeypatch):
    from cozempic.memory import schedule, ledger
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    monkeypatch.setattr(schedule, "distill_thinking", lambda t: "Decision: X")
    captured = {}
    monkeypatch.setattr(schedule, "persist_insights", lambda sid, ins: (captured.update(n=len(ins)) or ["decision-slug"]))
    block = {"type": "thinking", "thinking": "z" * 600}
    msg = {"type": "assistant", "message": {"role": "assistant", "content": [block]}}
    schedule._distill_thinking_blocks("s1", [msg])
    assert captured.get("n") == 1
    assert ledger.is_block_captured("s1", block)
```

Note: `schedule.persist_insights` must be importable at module scope in schedule.py for this monkeypatch — verify the existing import (`from .mem_bridge import persist_insights`) is at module top; if `_distill_thinking_blocks` imports it locally instead, change the test to patch `mem_bridge.persist_insights` and `schedule.distill_thinking`. Adjust the test to whatever the real module-level names are.

- [ ] **Step 6: Run tests**

Run: `.venv/bin/python -m pytest tests/memory/test_distill.py tests/memory/test_schedule.py -v`
Expected: PASS (existing schedule tests still green + 4 new)

- [ ] **Step 7: Commit**

```bash
git add src/cozempic/memory/distill.py src/cozempic/memory/schedule.py tests/memory/test_distill.py
git commit -m "feat(memory): thinking-block decision-point distillation in worker (F7 worker half)"
```

---

### Task 5: `thinking_distill` strategy (F7 sync half, replaces `thinking-blocks`)

**Files:**
- Create: `src/cozempic/strategies/thinking_distill.py`
- Modify: `src/cozempic/strategies/standard.py` (remove `strategy_thinking_blocks`)
- Modify: `src/cozempic/strategies/__init__.py` (import new module)
- Test: `tests/strategies/test_thinking_distill.py`

The strategy keeps the registered NAME `thinking-blocks` (so prescriptions/tests referencing the name keep working) but the impl moves to `thinking_distill.py` and gains the distilled-inline behavior. It reads `config["session_id"]` to look up the block ledger.

- [ ] **Step 1: Write the failing test**

```python
# tests/strategies/test_thinking_distill.py
import json
from cozempic.strategies import thinking_distill
from cozempic.memory import ledger


def _asst(idx, blocks):
    d = {"type": "assistant", "message": {"role": "assistant", "content": blocks}}
    return (idx, d, len(json.dumps(d)))


def test_distilled_block_replaced_inline(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    block = {"type": "thinking", "thinking": "z" * 600}
    ledger.record_block("s1", block, "decision-slug")
    monkeypatch.setattr(thinking_distill, "_load_decision", lambda slug: "Decision: chose C")
    result = thinking_distill.strategy_thinking_distill([_asst(0, [block])], {"session_id": "s1"})
    from cozempic.helpers import get_content_blocks
    blocks = get_content_blocks(result.actions[0].replacement)
    text = " ".join(b.get("text", "") for b in blocks)
    assert "Decision: chose C" in text
    assert "recall decision-slug" in text


def test_not_distilled_falls_back_to_signature_only(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    block = {"type": "thinking", "thinking": "keep this reasoning", "signature": "SIG"}
    result = thinking_distill.strategy_thinking_distill([_asst(0, [block])], {"session_id": "s1"})
    from cozempic.helpers import get_content_blocks
    blocks = get_content_blocks(result.actions[0].replacement)
    tb = next(b for b in blocks if b.get("type") == "thinking")
    assert tb["thinking"] == "keep this reasoning"  # reasoning kept (lossless)
    assert "signature" not in tb                     # signature stripped


def test_signature_only_mode_forced(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    block = {"type": "thinking", "thinking": "z" * 600, "signature": "SIG"}
    ledger.record_block("s1", block, "decision-slug")
    # mode override should skip distillation entirely
    result = thinking_distill.strategy_thinking_distill([_asst(0, [block])],
                                                        {"session_id": "s1", "thinking_mode": "signature-only"})
    from cozempic.helpers import get_content_blocks
    tb = next(b for b in get_content_blocks(result.actions[0].replacement) if b.get("type") == "thinking")
    assert tb["thinking"] == "z" * 600  # not distilled
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/strategies/test_thinking_distill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cozempic.strategies.thinking_distill'`

- [ ] **Step 3: Write `src/cozempic/strategies/thinking_distill.py`**

```python
# src/cozempic/strategies/thinking_distill.py
"""F7 sync half — replace distilled thinking blocks with their decision points inline,
else fall back to lossless signature-only. Replaces the old thinking-blocks strategy."""

from __future__ import annotations

from ..helpers import get_content_blocks, get_msg_type, is_protected, msg_bytes, set_content_blocks
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
                        new_blocks.append({"type": "text",
                                           "text": f"[distilled reasoning · recall {slug}]\n{decision}"})
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
```

- [ ] **Step 4: Remove the old strategy from `standard.py` and register the new module**

- In `src/cozempic/strategies/standard.py`: delete `strategy_thinking_blocks` (the whole `@strategy("thinking-blocks", ...)` function) and the now-unused `_THINKING_MODES` constant if only it used them. Verify `coerce_choice` is still used elsewhere in the file before removing its import (it likely is — leave imports that other functions use).
- In `src/cozempic/strategies/__init__.py`: add `from . import thinking_distill  # noqa: F401`. Ensure `standard` is still imported (other strategies live there).
- IMPORTANT: the `@strategy` name is still `"thinking-blocks"`, so the registry has exactly one registration of that name. Confirm no double-registration: `.venv/bin/python -c "import cozempic.strategies; from cozempic.registry import STRATEGIES; print(STRATEGIES['thinking-blocks'].func.__module__)"` should print `cozempic.strategies.thinking_distill`.

- [ ] **Step 5: Run tests + regression**

Run: `.venv/bin/python -m pytest tests/strategies/test_thinking_distill.py -v`
Expected: PASS (3 passed)

Run: `.venv/bin/python -m pytest tests/ -k "thinking or standard or registry or strategy" -q`
Expected: PASS. Some existing `tests/test_*` cases asserting the OLD `thinking-blocks` `remove`/`truncate` behavior will fail — rewrite ONLY those to the new default (`distill` with no ledger entry → signature-only fallback) or set `thinking_mode="remove"` where the test intends deletion. Report which cases changed and why. Do NOT weaken unrelated assertions.

- [ ] **Step 6: Commit**

```bash
git add src/cozempic/strategies/thinking_distill.py src/cozempic/strategies/standard.py src/cozempic/strategies/__init__.py tests/strategies/test_thinking_distill.py tests/  # + any rewritten test files
git commit -m "feat(strategies): thinking_distill replaces thinking-blocks (F7 sync half)"
```

---

### Task 6: Prescription cleanup + tail asset section + full verification

**Files:**
- Modify: `src/cozempic/registry.py`
- Modify: `src/cozempic/memory/tail.py`
- Modify: `tests/test_track2.py` (strategy-count / ordering invariants)
- Test: `tests/memory/test_tail.py` (asset section)

- [ ] **Step 1: Update prescriptions in `src/cozempic/registry.py`**

Per HLD §7. In BOTH `standard` and `aggressive`: insert `"asset-offload"` immediately BEFORE `"tool-output-trim"`. The `"thinking-blocks"` name stays (now backed by thinking_distill). Result for `standard`:

```python
    "standard": [
        "compact-summary-collapse", "attribution-snapshot-strip", "progress-collapse",
        "file-history-dedup", "metadata-strip", "thinking-blocks",
        "asset-offload", "tool-output-trim", "tool-result-age",
        "stale-reads", "system-reminder-dedup", "tool-use-result-strip",
    ],
```

Add `"asset-offload"` in the same relative position (before `tool-output-trim`) in `aggressive`. Leave all other entries and order unchanged.

- [ ] **Step 2: Add the asset-pointer section to the tail composer**

In `src/cozempic/memory/tail.py`, extend `build_tail_message` to accept an optional `assets: list[str] | None = None` param and render an "Offloaded assets (recall to load)" section (sanitized, same pattern as stubs). Read the current `build_tail_message`/`compose_tail` signatures first and thread the param through `compose_tail` with a default of `None` so existing callers are unaffected.

Add test to `tests/memory/test_tail.py`:

```python
def test_tail_includes_offloaded_assets():
    from cozempic.memory import tail
    msg = tail.build_tail_message(northstar="G", todos=[], directives=[], stubs=[],
                                  assets=["[cozempic asset: doc — 9KB · recall doc-abc]"])
    text = tail._text_of(msg)
    assert "recall doc-abc" in text
    assert tail.TAIL_MARKER in text
```

- [ ] **Step 3: Update strategy-count/ordering invariants in `tests/test_track2.py`**

Read the current invariants. `asset-offload` adds ONE strategy to `standard` (was 11 → 12) and ONE to `aggressive` (was 19 → 20). Update those counts. If an ordering test asserts `tool-output-trim`'s neighbors, update it to reflect `asset-offload` now preceding it. Change ONLY the counts/ordering the new registration affects; do not weaken other assertions.

- [ ] **Step 4: Run the strategy/registry/tail subset**

Run: `.venv/bin/python -m pytest tests/ -k "track2 or registry or strategy or tail" -q`
Expected: PASS.

- [ ] **Step 5: Full suite + smoke**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass. KNOWN-FLAKY (ignore if ONLY these fail; re-run in isolation): `test_guard_reload_watcher_poll.py::...ResumeCmdNonzeroExit...` and some `test_watcher.py` kqueue cases (osascript/timing).

Run registry smoke (note: must import strategies first to trigger `@strategy` side-effects):
```bash
.venv/bin/python -c "import cozempic.strategies; from cozempic.registry import STRATEGIES, PRESCRIPTIONS; assert 'asset-offload' in STRATEGIES; assert STRATEGIES['thinking-blocks'].func.__module__=='cozempic.strategies.thinking_distill'; assert 'asset-offload' in PRESCRIPTIONS['standard']; print('registry ok')"
```
Expected: `registry ok`

- [ ] **Step 6: Commit**

```bash
git add src/cozempic/registry.py src/cozempic/memory/tail.py tests/test_track2.py tests/memory/test_tail.py
git commit -m "feat: wire asset-offload into prescriptions + tail asset section; retire thinking-blocks name to distill"
```

---

## Self-Review Notes

- **Spec coverage:** F7 worker half → Task 4; F7 sync half → Task 5; F8 core → Task 2; F8 strategy → Task 3; block-hash namespace (prevents recoverability from deleting pointer-bearing messages) → Task 1; tail asset reinjection → Task 6; strategy consolidation (HLD §7) → Tasks 5 (thinking-blocks REPLACE) + 6 (prescription edits). `tool-output-trim` DEMOTE and `mega-block-trim` KEEP-as-backstop are achieved by ordering (`asset-offload` runs first), no code change needed to them.
- **Namespace safety:** F7/F8 use `span_hash([block])`; `recoverability` uses `span_hash([msg])`. Task 1 asserts they don't collide, so an in-place-mutated message is never seen as capture-confirmed.
- **No-LLM-on-sync-path invariant preserved:** F8 (Task 3) is pure byte move; F7's LLM work is confined to the background worker (Task 4); F7's sync strategy (Task 5) only *reads* pre-distilled text from the store, never calls an LLM.
- **Interface consistency:** `blobref.offload_block(block, name) -> str|None` (Tasks 2,3); `ledger.record_block/is_block_captured/slug_for_block(session_id, block)` (Tasks 1,4,5); `distill_thinking(text, backend) -> str|None` (Task 4); `_load_decision(slug) -> str|None` (Task 5).
- **Flagged reads-before-edit:** Tasks 4 (schedule `consolidate_worker` shape + module-level import names), 5 (removing from standard.py without breaking shared imports), 6 (tail signature, track2 invariants) each start by reading real code — signatures confirmed on-site.
- **Opt-out:** F8's store writes go through `mem_bridge` (no-op when unpartitioned → strategy leaves blocks intact); F7 worker is inside the `COZEMPIC_MEMORY_OFF`-guarded consolidation path.
