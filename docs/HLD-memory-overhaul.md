# HLD: Cozempic Memory Overhaul — Recoverability-Gated Pruning (`memory-overhaul`)

**Date:** 2026-07-01 · **Author:** divkov · **Status:** Draft

## 1. Overview

### 1.1 Background
Cozempic 1.8.33 (verified: `pyproject.toml` version = "1.8.33") ships a **regex-only
behavioral digest** (`digest.py`, 1086 lines): heuristic extraction of correction
signals → structured rules in `~/.cozempic/behavioral-digest.json`, capped at 20 active
rules, injected via a PostToolUse hook (top-5 rules + CLAUDE.md `NEVER/MUST/CRITICAL`
lines) and a SessionStart `digest inject`. There is **no LLM backend in the shipped
tree** — prior worktrees that built a `claude -p` extractor were deleted; this design
builds it fresh.

A separate, mature memory system already exists and is the intended store:
- **`mymemories`** (private repo, `MEM_HOME=~/workplace/mymemories`) — per-project
  partitions, one fact per `.md` file (frontmatter `name`/`description`/`type` + dense
  body), a `MEMORY.md` index per partition, and an offline embedding index `index.json`.
- **`mymemories-tool`** (`~/workplace/mymemories-tool`) — `embed.py update` (rebuild
  index) and `embed.py query <text>` → ranked `score  partition/file.md` lines; the
  `/memorize` and `/recall` skills drive it. Verified: README + `embed.py` argv surface.

Cozempic can rewrite the session JSONL atomically (`session.save_messages`, verified),
so it can not only delete messages but **reorder** them and **append** a synthesized
block — the enabling primitive for tail placement.

### 1.2 Problem Statement
- Regex extraction only catches phrased corrections ("don't do X"); it misses decisions,
  constraints, and learned facts — the bulk of what's worth keeping.
- Cozempic and `mymemories` are two disconnected memory systems. Extracted signal dies in
  a local JSON cap-20 store instead of the durable, indexed, cross-project repo.
- Pruning is conservative because eviction is lossy: once a span leaves the window it is
  gone. There is no "it's safe to drop this because it's saved elsewhere" signal.
- The most important standing context (goal, open todos, hard directives) sits wherever it
  first appeared — usually buried mid-conversation, where "lost in the middle" degrades
  adherence.

### 1.3 Goals
- Replace regex extraction with **atomize-style LLM extraction** of atomic insights.
- **Bridge** cozempic → `mymemories`: extracted insights persist as indexed fact files;
  cozempic keeps only a thin session-scoped bridge, not a rival long-term store.
- **Lazy load**: inject compact stubs (title + hook + id), not bodies; full text re-enters
  only via `/recall` on demand.
- **Conservative capture**: prefer preserving original wording over lossy summarization;
  compress only where clearly safe. Losing the original phrasing is the failure mode to avoid.
- **Consolidate early, in the background**: run extraction/consolidation *ahead* of the
  prune threshold, off the critical path — never as a blocking step the instant a threshold
  trips.
- **Recoverability-gated pruning**: a span becomes eligible for aggressive eviction *once
  its content is captured as a durable memory*. Aggression scales with capture, not clock.
- **Tail placement**: regenerate a northstar/todo/directives block at the **end** of the
  conversation on every prune.
- **Inline thinking distillation** (avenue 2): replace a fat reasoning/thinking turn with
  its distilled *decision-point* text in place, instead of blindly deleting or truncating it.
- **Lossless blob offload** (avenue 3): move a large **stable text asset** (document, chunked
  document, big tool-result) out of the window verbatim and leave a retrievable **pointer**,
  instead of lossy head/tail truncation.
- **Consolidate the strategy set**: retire/merge the strategies these avenues subsume so
  there is one owner per concern (no two strategies fighting over the same block).

## 2. Requirements

### 2.1 Functional

**F1 — Insight extraction.** Decompose a span into atomic, standalone insights (one fact
each: decision / constraint / correction / learned-fact), each tagged by source-trust
class (see F5), deduped against existing partition memories. Extraction method is
**pluggable** — an atomize-style `claude -p` pass is the default, but the contract is just
"span in → tagged insights out"; a cheaper heuristic or a different model can satisfy it.
**Conservative by default: preserve original wording over lossy paraphrase.** An insight
should quote/lift the source span, not re-summarize it, unless compression is clearly safe.
F1 is distinct from F2 (persistence) — extraction can run without immediately writing, and
persistence can accept insights from any extractor.

**F1a — Early, background consolidation (not threshold-blocking).** Consolidation does
**not** fire synchronously the instant a context threshold is crossed. It runs
**proactively and in the background, earlier than the prune threshold** — e.g. on an idle
breakpoint or a low-water mark well below the prune trigger — so that by the time pruning
is warranted the insights are already extracted and capture-confirmed (F6). Blocking
extraction at the threshold is explicitly rejected: the work is scheduled ahead of need,
not on the critical path.

**F2 — mymemories bridge.** Takes accepted insights from *any* extractor (F1) and writes
each as a `mymemories` fact file for the current project partition, following `format.md`
exactly (frontmatter `name`/`description`/`type`, body, `MEMORY.md` index line), then runs
`embed.py update`. Persistence is a standalone stage with a clean interface (`insight →
slug`); it neither knows nor cares how the insight was extracted. Partition resolution reuses the tool's convention: `readlink
~/.claude/projects/<mangled-cwd>/memory` → `<MEM_HOME>/<partition>`. If the project is not
partitioned, extraction no-ops with a one-line stderr note (no auto-install).

**F3 — Stub injection (lazy load).** Cozempic injects **stubs only** — `title · one-line
hook · partition/slug` — for memories relevant to the session. Bodies are never
auto-injected. `/recall` remains the on-demand path to full text. Relevance = `embed.py
query` against a session-derived query string; inject top-K stubs (default K=7, bounded).

**F4 — Tail composer.** On every prune, cozempic (re)builds a single **tail block** placed
as the last message(s) of the rewritten JSONL, containing: extracted **northstar** (user's
stated goal), **open todos**, and **standing directives/corrections**. Content is derived
fresh each prune from the session + active memories; the prior tail block is replaced, not
appended (idempotent, tagged with a protection marker like the existing
`__cozempic_behavioral_digest__`).

**F5 — Source-trust tagging.** Each extracted insight carries a trust class:
`user-directive` (intent/preference/correction — verbatim, high weight) ·
`agent-provisional` (model-generated claim — stored only if corroborated by a tool result,
artifact, or user confirmation; else dropped or flagged provisional) · never record a
user's *world-fact assertion* as ground truth. This governs both what F2 persists and how
F4 weights the tail.

**F6 — Recoverability-gated pruning.** The prune strategies gain a signal: a message span
is **capture-confirmed** once F1's job has persisted its content (F2 returns written
slugs). Capture-confirmed spans are eligible for aggressive eviction on the *next* prune
cycle regardless of age/threshold. Uncaptured spans fall back to today's conservative
content-type rules. "Prune early / compact early" is the emergent effect: capture, then
shed.

**F7 — Inline thinking distillation (avenue 2).** A fat `thinking` block is compacted into
its **decision points** — the conclusions/choices the reasoning reached — and the block is
replaced *in place* with that distilled text, rather than deleted (`thinking-blocks`
`remove`) or blindly cut (`truncate`). Because distillation needs an LLM, it runs in the
**existing background worker** (F1a), never on the sync prune path: the worker distills the
thinking span, persists the decision-point text (F2), and records a per-message ledger
pointer; a sync strategy then swaps the original thinking block for the distilled text
(with a `→ recall <slug>` pointer to the full reasoning). If a block has not yet been
distilled, the strategy leaves it for the incumbent lossless `signature-only` handling — it
never blocks. F7 **replaces** the `thinking-blocks` strategy (see §7), absorbing its
`signature-only` mode as the not-yet-distilled fallback.

**F8 — Lossless blob offload + pointer (avenue 3).** A large **stable text asset** — a
document, a chunked document, or a big `tool_result` blob (NOT thinking → F7; NOT binary
images → `image-strip`) — over a byte threshold is written **verbatim** to the mymemories
partition and replaced in-window with a compact **pointer stub** (`[cozempic asset: <path>
— NKB · recall <slug>]`). This is **lossless** (the bytes are recoverable via `/recall`),
unlike today's head/tail truncation. No LLM needed — it is a pure byte move, so it runs as a
**synchronous** strategy. The pointer records into the **same bridge ledger** as F2/F6 so
recoverability treats offloaded assets as capture-confirmed. Offloaded-asset pointers are
**reinjected into the tail block** (F4) alongside memory stubs so the agent knows the asset
exists and can `/recall` it. F8 **subsumes** the dead-end (non-retrievable) stub tiers of
`tool-result-age` (old-age) and `mega-block-trim`, and demotes `tool-output-trim` to a
small/volatile fallback (see §7).

### 2.2 Out of Scope
- No logit-level context steering (no API surface — background rationale only).
- No multi-agent debate / self-consistency (single-session tool).
- No change to `mymemories-tool` internals or `embed.py` — cozempic is a *client*.
- No auto-partitioning of projects not already in `manifest.tsv`.
- No synchronous/blocking LLM extraction in hooks.

## 3. Solution Options

**Decision 1 — When/how extraction runs.**
- A — Blocking at threshold. Fire extraction synchronously the instant the context
  threshold trips. Rejected: puts LLM latency on the critical path exactly when the session
  is busiest.
- B ★ — **Early + background.** Schedule extraction *ahead* of the prune threshold (idle
  breakpoint / low-water mark), running detached so the hook returns immediately. By the
  time pruning is warranted, insights are already captured. Extractor itself is pluggable
  (default: atomize-style `claude -p`; the contract is span→insights, not a specific model).
- C — Hybrid LLM-or-regex fallback wired into the hook. Extra complexity for a degraded
  path; deferred.

**Decision 2 — Persistence target.**
- A — Keep cozempic's own `~/.cozempic` store, swap extractor only. Self-contained but
  perpetuates two rival memory systems.
- B — Fully replace with `mymemories`. Clean, but loses a fast session-scoped scratch store
  for stubs/queue.
- C ★ — **Bridge.** `mymemories` is the durable indexed store of record; cozempic keeps a
  thin local **bridge/queue** (pending-extraction work items, injected-stub ledger). Matches
  user directive ("both / bridge").

**Decision 3 — Lazy-load mechanism.**
- A — Auto-inject top-K full bodies. Bounded but still spends body tokens every session.
- B — On-demand only via `/recall`. Leanest, but the agent may not know a memory exists.
- C ★ — **Stub injection.** Cheap presence (title+hook+id at the tail), lazy body via
  `/recall`. Agent knows what exists without paying for bodies.

## 4. Current State

```
 session JSONL
     │
     ├─ PostToolUse hook ──► digest.py (regex) ──► ~/.cozempic/behavioral-digest.json
     │                              │                        (cap 20 rules)
     │                              └─ inject top-5 rules ──► Claude context (mid-stream)
     │
     └─ PreCompact/Stop/guard ──► strategies (gentle/standard/aggressive)
                                        └─ save_messages() rewrites JSONL (drop bloat)

 mymemories (embedding-indexed, per-project)  ◄──  ONLY reached by human /memorize, /recall
        └── DISCONNECTED from cozempic
```

What breaks today: extraction misses non-correction signal; captured signal is trapped in a
cap-20 local JSON; the rich indexed store is never fed by the tool that watches every
session; pruning can't be aggressive because eviction is unrecoverable; key directives are
not repositioned for adherence.

## 5. Design Proposal

### 5.1 Architecture

```
                       prune event (PreCompact / Stop / guard terminate-resume)
                                          │
        ┌─────────────────────────────────┼──────────────────────────────────┐
        │                                  │                                  │
        ▼                                  ▼                                  ▼
  ┌───────────┐   candidate span    ┌────────────┐   session-derived   ┌──────────────┐
  │  prune    │  (early, off-path)  │  extractor │   query             │ stub injector│
  │ strategy  │───────────────────► │ (pluggable,│                     │  embed.py    │
  │  (F6)     │◄── capture-confirmed │  bg,detach)│                     │  query top-K │
  └─────┬─────┘    slugs (bridge)   └─────┬──────┘                     └──────┬───────┘
        │                                 │ atomic insights (F1)               │ stubs
        │ rewrite JSONL                   │ + source-trust tags (F5)           │
        │ (drop captured spans)           ▼                                    │
        │                          ┌──────────────┐                           │
        │                          │  mymemories  │  format.md fact files     │
        │                          │  bridge (F2) │  + MEMORY.md + embed update│
        │                          └──────────────┘                           │
        ▼                                                                      ▼
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │  rewritten session JSONL: [ pruned body … ]  +  TAIL BLOCK (F4):               │
  │      northstar · open todos · standing directives · relevant memory stubs (F3) │
  └──────────────────────────────────────────────────────────────────────────────┘
```

### 5.2 Extractor (`extract.py`, new) — pluggable, early, background
- **Interface (the stable contract):** `span + light session context → [insight]`, where
  each insight is `{slug, title, description, type, trust_class, body}`. Any backend that
  honors this is swappable — the rest of the system depends on the interface, not the model.
- **Default backend:** a new `claude_cli` module — `claude -p <prompt>` subprocess, JSON
  output, fence-stripping parser.
- **Scheduling (Decision 1B):** invoked *early* — on an idle breakpoint or a low-water mark
  below the prune threshold — as a **detached background** process, so no hook ever blocks
  on it. Writes a completion marker the next cycle reads. Debounced so overlapping triggers
  don't stack jobs.
- **Conservative extraction:** the prompt instructs *preserve original wording* — quote/lift
  the source span into the `body`, don't paraphrase into a lossy summary; compress only
  where clearly safe. Prompt contract: "Decompose into atomic standalone insights, one fact
  per unit, **preserving the original phrasing of each fact**. Classify each: `user-directive`
  | `agent-provisional` | `world-fact`. Drop `world-fact` asserted only by the user. Drop
  `agent-provisional` unless corroborated by a tool result / artifact / user confirmation.
  Dedup against: <existing partition slugs + hooks>."

### 5.3 mymemories bridge (`mem_bridge.py`, new)
- Partition resolve: `readlink ~/.claude/projects/<mangled-cwd>/memory`; require it to
  resolve under `MEM_HOME` (default `~/workplace/mymemories`, env-overridable). Else no-op.
- Write each insight as `<partition>/<slug>.md` per `format.md`; append `MEMORY.md` index
  line; run `python3 $TOOL_DIR/embed.py update`. **Does not** auto-commit/push by default
  (leave that to the user's `/memorize` habit or a config flag) — avoids surprise git
  writes from a hook.
- Returns written slugs → cozempic records them in the local **bridge ledger**
  (`~/.cozempic/bridge/<session-id>.json`): `{msg_span_hash → slug}`. This ledger is the
  capture-confirmation source for F6.

### 5.4 Stub injector (extends `digest inject`)
- Build a query string from the session (recent user turns + tail northstar).
- `embed.py query <string>` → take rows with score > 0.4, top-K (default 7).
- Emit stubs into the **tail block**, not mid-stream: `- [title] — hook  (partition/slug)`.
  Sanitized via the existing `_sanitize_for_injection` (untrusted-text injection guard).

### 5.5 Tail composer (`tail.py`, new)
- Compose one block, tagged with a protection marker so the next prune replaces (not
  duplicates) it — mirrors `__cozempic_behavioral_digest__` handling.
- Sections, in order: **Northstar** (extracted user goal) · **Open todos** (from TodoWrite
  state / session) · **Standing directives** (`user-directive` memories + CLAUDE.md
  critical lines) · **Relevant memory stubs** (F3).
- Placement: appended as the final message(s) after `save_messages` reorders — last is the
  highest-adherence position. Idempotent: strip any prior marked block before writing.

### 5.6 Recoverability-gated pruning (extends strategies)
- Strategies read the bridge ledger. A span whose `msg_span_hash` has a recorded slug is
  **capture-confirmed**.
- New eviction tier: capture-confirmed spans are droppable aggressively (earlier, lower
  threshold) since recoverable via stub+`/recall`. Uncaptured spans keep today's
  conservative content-type rules.
- Because extraction runs early and in the background, capture-confirmation normally lands
  *before* pruning is even warranted — the early scheduling exists precisely so the ledger
  is already populated when the strategy wants to evict. Worst case it is one cycle behind
  (cycle N extracts, N+1 evicts); never evict a span that isn't capture-confirmed.

### 5.7 Digest teardown
- `digest.py` regex extraction is removed; the file's surviving role is the injection
  plumbing (`_sanitize_for_injection`, the inject CLI action) reused by the stub injector.
- `~/.cozempic/behavioral-digest.json` is superseded by the `mymemories` partition +
  bridge ledger. Provide a one-shot migration: extract existing active rules as
  `user-directive` memories on first run, then retire the JSON.

### 5.8 Inline thinking distillation (F7 — `thinking_distill`, replaces `thinking-blocks`)
- **Background half (worker, LLM):** when the consolidation worker (F1a) processes a span,
  for each large `thinking` block it also produces a **decision-point distillation** —
  the conclusions/choices reached — via the same pluggable extractor backend. The
  distilled text is persisted (F2) and a per-message ledger entry records
  `{thinking_span_hash → slug}`.
- **Ledger namespace:** F7/F8 record under a **block**-hash key (`span_hash([block])`),
  which is a different namespace from `recoverability`'s whole-**message** hash
  (`span_hash([msg])`). So a distilled/offloaded block never makes its host message
  look capture-confirmed to `recoverability` — the pointer survives in-window.
- **Sync half (strategy):** `thinking_distill` replaces the `thinking-blocks` registration.
  For each `thinking` block:
  - If the block is **distilled** (ledger has its *block* hash), `replace` it in-window with the
    distilled decision-point text + a `→ recall <slug>` pointer to the full reasoning.
  - Else fall back to the incumbent **`signature-only`** behavior (strip the crypto
    signature, keep the reasoning) — never a blocking LLM call, never blind deletion.
- Config: `thinking_mode` retained for back-compat (`distill` default; `signature-only`
  and `remove` still selectable for users who want the old behavior).

### 5.9 Lossless blob offload (F8 — `asset-offload`, new sync strategy)
- **Target selection:** content blocks that are large *stable text assets* — `tool_result`
  blocks and text blocks over `asset_offload_min_bytes` (default 8192) — excluding
  `thinking` (F7 owns it) and `image` blocks (`image-strip` owns them). "Stable" = not
  already a cozempic stub/marker.
- **Offload:** write the blob **verbatim** to the mymemories partition (a fact file whose
  body is the raw asset, `type: reference`), via `mem_bridge`. Reuses the partition store
  per the user directive; a `type: reference` tag keeps it distinguishable from semantic
  insights. Record `{block_span_hash → slug}` in the bridge ledger.
- **Replace:** swap the block for a compact pointer stub
  `[cozempic asset: <name> — <N>KB · recall <slug>]` (sanitized). Lossless: `/recall`
  returns the exact bytes.
- **Tail reinjection:** offloaded-asset pointers are surfaced in the tail block (F4)
  alongside memory stubs, so the agent sees the asset is available.
- **Sync, no LLM:** pure byte move on the prune path — safe to run synchronously.
- **Ledger interaction (deliberate):** F8 does **not** record into the *recoverability*
  ledger. That ledger gates whole-**message** removal by hash; F8 mutates a message in
  place (block → pointer) and the pointer must *survive*. Recording it there could let
  `recoverability` delete the very message that holds the pointer. Instead the pointer stub
  is self-describing (`recall <slug>`), and `/recall` resolves the slug directly. Blob
  provenance (slug → offloading session) may be tracked in a **separate** offload ledger if
  needed, never the removal-gating one.

## 6. Design Analysis

### 6.1 Key Improvements
- Extraction captures decisions/constraints/facts, not just phrased corrections.
- One durable, indexed, cross-project memory store instead of two disconnected ones.
- Window becomes a scratchpad; source of truth is recoverable — enabling genuinely
  aggressive, *safe* early pruning.
- Highest-adherence context (goal/todos/directives) is anchored at the tail every prune.
- Source-trust tagging encodes the one defensible half of the "trust hierarchy" (agent
  claims provisional; user *corrections* authoritative; user *facts* never ground truth).

### 6.2 Risks
| Risk | Mitigation |
|---|---|
| Background extraction lags → span evicted before capture | Extraction runs *early* (ahead of threshold) so the ledger is usually populated before eviction is wanted; F6 gates on *confirmed* slugs only and never evicts on unconfirmed capture. |
| Lossy summarization drops information the user needed | Conservative extraction: preserve original phrasing in the body, compress only where clearly safe; original span survives in the JSONL until capture is confirmed. |
| `claude -p` cost/latency | Early + background + debounced, off the critical path; only over the candidate span, not the whole session; config to disable. |
| Hook writing to `mymemories` git repo surprises user | Bridge writes files + index but does **not** auto-commit/push by default; gated behind a config flag. |
| LLM extraction injects prompt-injection via stored body | Reuse `_sanitize_for_injection` on every injected stub; bodies enter only via user-driven `/recall`. |
| Project not partitioned in `mymemories` | Extraction no-ops with stderr note; no auto-install, no data loss. |
| Losing the cap-20 adherence property (IFScale: >30 rules degrades) | Tail injects bounded top-K stubs, not all memories; bodies stay out of window. |
| Regex digest removal breaks existing `~/.cozempic` users | One-shot migration of active rules → `user-directive` memories before retiring the JSON. |
| F7 distillation drops a reasoning step the agent still needed | Distillation keeps a `→ recall <slug>` pointer to the full thinking; not-yet-distilled blocks fall back to lossless `signature-only`, never blind deletion. |
| F8 offloads an asset the agent immediately re-reads → churn | Byte threshold (8192) + stable-only selection; pointer reinjected in tail so the agent can `/recall` without a round-trip to disk; ledger-gated so recoverability doesn't double-handle. |
| Two strategies fighting over the same block after cleanup | §7 consolidation gives exactly one owner per concern: F7 owns `thinking`, F8 owns large stable text assets, `image-strip` owns images; `mega-block-trim` retained only as a post-avenue backstop. |

## 7. Strategy Consolidation (cleanup contract)

Ground truth from an audit of `src/cozempic/strategies/`. One owner per concern; retire the
redundant/dead-end pieces.

| Strategy | Verdict | Action |
|---|---|---|
| `thinking-blocks` | **REPLACE** | Becomes `thinking_distill` (F7); `signature-only` kept as the not-yet-distilled fallback mode. |
| `tool-result-age` (old-age stub tier) | **MERGE → F8** | The old-age tier already emits a stub but it points nowhere; upgrade it to an `asset-offload` retrievable pointer. Keep the mid-age *lossless* minify tier unchanged. |
| `mega-block-trim` | **KEEP (backstop)** | Runs after F7/F8 as a last-resort truncation for anything the avenues didn't claim. |
| `tool-output-trim` | **DEMOTE** | Fires only for blobs below F8's threshold or too volatile to offload (small/unstable fallback). |
| `image-strip` | **KEEP** | Orthogonal (binary); F8 explicitly excludes images. |
| `document-dedup` | **KEEP** | Handles multi-occurrence dedup (in-window); complementary to F8's single-large-asset offload. |
| `tool-use-result-strip`, `envelope-strip`, `stale-reads`, `system-reminder-dedup`, gentle tier | **KEEP** | Orthogonal, cheap, effectively lossless. |
| `recoverability` | **KEEP** | Stays first in the aggressive tier. Its ledger is keyed by whole-**message** hash. F7 and F8 key their ledger entries by **block** hash (distinct namespace), so neither can trick `recoverability` into deleting a message that still holds a pointer. |

**Prescription changes:** in `standard` and `aggressive`, `thinking-blocks` → `thinking_distill`;
insert `asset-offload` before `tool-output-trim` (so offload claims large stable assets first,
and `tool-output-trim` only mops up the small/volatile remainder). `mega-block-trim` stays last.
