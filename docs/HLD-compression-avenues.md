# HLD: Cozempic Compression Avenues 2 & 3 (`compression-avenues`)

**Date:** 2026-07-04 · **Author:** divkov · **Status:** Design

## 1. Overview

### 1.1 Background
Cozempic prunes Claude Code session JSONL via registered `@strategy` functions grouped
into three prescriptions (`gentle`/`standard`/`aggressive`) in `registry.py` (verified).
A memory subsystem (`src/cozempic/memory/`) already extracts atomic insights to the
`mymemories` store and gates deletion via the `recoverability` strategy and a per-session
bridge ledger. Two compression avenues extend this: inline thinking distillation (F7) and
lossless blob offload (F8).

### 1.2 Problem Statement
- The prior `thinking-blocks` strategy only deleted or blind-truncated reasoning — lossy,
  no decision-point retention.
- Large stable text assets (documents, big tool results) were head/tail truncated —
  the trimmed bytes were unrecoverable, and stubs from `tool-result-age`/`mega-block-trim`
  pointed nowhere.

### 1.3 Scope
- **Covers:** F7 (thinking → inline decision points + pointer), F8 (large text asset →
  verbatim store + retrievable pointer), and the prescription/cleanup changes wiring them in.
- **Non-goals:** binary images (`image-strip` owns them); no LLM call on the synchronous
  prune path; no change to `recoverability`, `document-dedup`, or gentle-tier strategies.

## 2. Behavior

**F7 — Inline thinking distillation** (registered under name `thinking-blocks`, impl
`strategies/thinking_distill.py`, verified):
- Background worker (`memory/schedule.py`) distills each `thinking` block ≥ 500 chars into
  its decision points via `memory/distill.py` (LLM, pluggable backend), persists that text
  to the store, and records a **block-hash** ledger entry (`ledger.record_block`).
- At prune time, for a distilled block the strategy replaces it in-window with a `text`
  block: `[distilled reasoning · recall <slug>]` + the sanitized decision text.
- For a not-yet-distilled block it falls back to lossless `signature-only` (reasoning kept,
  crypto signature stripped). `thinking_mode` config: `distill` (default), `signature-only`,
  `remove`.

**F8 — Lossless blob offload** (`strategies/asset_offload.py`, verified):
- Targets `text` blocks and str-content `tool_result` blocks whose UTF-8 length ≥
  `asset_offload_min_bytes` (default 8192). Excludes `thinking`, `image`, and blocks already
  bearing the `[cozempic asset:` stub prefix.
- Writes the block's text **verbatim** to the `mymemories` partition as a `type: reference`
  fact (`memory/blobref.py`), then replaces the block with a sanitized pointer stub:
  `[cozempic asset: <name> — <N>KB · recall <slug>]`.
- No LLM — a pure byte move; safe on the synchronous prune path. No-op (block left intact)
  when the project is not partitioned.

**Rehydration:** both pointers carry `recall <slug>`; the full text re-enters only via
`/recall`. F8 pointers are also surfaced in the tail block's "Offloaded assets" section.

**Trigger:** F7's worker runs in the existing early/background consolidation path
(`maybe_consolidate`, off the critical path). F7's strategy and F8 run inside a normal
prune, as ordinary strategies.

## 3. Architecture

```
 background worker (LLM, off-path)          synchronous prune (no LLM)
 ┌───────────────────────────┐             ┌────────────────────────────────┐
 │ consolidate_worker         │             │ prescription strategies         │
 │  ├ extract insights        │             │  ...                            │
 │  └ _distill_thinking_blocks│             │  thinking-blocks (thinking_distill)
 │     distill → persist →    │   ledger    │    distilled? → inline decision │
 │     record_block(block)    │───(block────│    else       → signature-only  │
 └───────────────────────────┘    hash)     │  asset-offload                  │
                                   ▲         │    eligible?  → verbatim store  │
 store (mymemories partition)      └─────────│                 + pointer stub  │
   decision facts + reference blobs          │  tool-output-trim (small/volatile)
   read back by slug via /recall             │  ...                            │
                                             └────────────────────────────────┘
```

- **`memory/ledger.py`** — adds `record_block`/`is_block_captured`/`slug_for_block`, keyed
  by `span_hash([block])`. This is a distinct namespace from `recoverability`'s whole-message
  `span_hash([msg])`, so a distilled/offloaded block never makes its host message look
  removable to `recoverability`.
- **`memory/distill.py`** — `distill_thinking(text, backend) -> str | None`; prompt requests
  decision points only, preserving wording. Returns None on empty input/output.
- **`memory/blobref.py`** — `is_offload_eligible`, `offload_block(block, name) -> slug|None`,
  `build_pointer_stub`. Persists via the existing `mem_bridge.persist_insights`.
- **`strategies/thinking_distill.py`** — reads `config["session_id"]`, looks up the block in
  the ledger, loads decision text from the store (`_load_decision`, strips frontmatter),
  sanitizes it via `digest._sanitize_for_injection` before injection.
- **`strategies/asset_offload.py`** — offloads eligible blocks; sanitizes the pointer stub
  before it enters the window.
- **`memory/tail.py`** — `build_tail_message`/`compose_tail` gain an optional `assets`
  section rendering offloaded-asset pointers.

## 4. Data & Interfaces

- **Ledger entry (block namespace):** `{ span_hash([block]) : slug }` in
  `~/.cozempic/bridge/<session>.json`; same file/format as message-span entries, disjoint keys.
- **Store fact (F7 decision):** `Insight(type="reference", trust_class=agent-provisional,
  body=<decision text>)`, slug `decision-<block-hash>`.
- **Store fact (F8 asset):** `Insight(type="reference", body=<verbatim asset bytes>)`, slug
  `<name>-<sha256[:8]>`.
- **Pointer stubs (in-window):** `[distilled reasoning · recall <slug>]\n<decision>` (F7);
  `[cozempic asset: <name> — <N>KB · recall <slug>]` (F8). Both pass through
  `_sanitize_for_injection` before entering the transcript.
- **Config surface:** `thinking_mode` (F7); `asset_offload_min_bytes` (F8, default 8192).
- **Prescriptions (verified):** `standard` and `aggressive` place `asset-offload` immediately
  after `thinking-blocks` and before `tool-output-trim`; `recoverability` remains first in
  `aggressive`. Counts: standard 12, aggressive 20.

## 5. Strategy Consolidation (as built)

| Strategy | State |
|---|---|
| `thinking-blocks` | Implementation replaced by `thinking_distill`; registered name unchanged (single registration). |
| `asset-offload` | New; runs before `tool-output-trim` in standard + aggressive. |
| `tool-output-trim` | Retained as the small/volatile fallback (runs after `asset-offload` claims large stable assets). |
| `tool-result-age`, `mega-block-trim`, `image-strip`, `document-dedup`, gentle tier | Unchanged. |
| `recoverability` | Unchanged; message-hash namespace disjoint from F7/F8 block-hash namespace. |
