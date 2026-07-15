# Cozempic Benchmarks

Two independent benchmark tiers validate the changes on this branch (the fixed
150K early-checkpoint tier and the removal of memory tail placement):

- **Tier 1 — compression** (`cozempic.bench.compression`): cheap, offline,
  deterministic. Dry-run prunes over a corpus of *saved* session JSONLs; measures
  token/byte reclaim, post-prune safety, and A/Bs the 150K checkpoint. No live
  session, no LLM, no reload.
- **Tier 2 — task quality** (`cozempic.bench.swebench`): SWE-bench-shaped A/B
  harness graded by *real test execution*. Answers whether pruning changes an
  agent's ability to complete a context-dependent coding task. Deterministic
  pieces are unit-tested; the live `claude -p` sweep is opt-in.

## Tier 1 — Compression (offline A/B)

### How to run

```bash
# repo fixtures (fast smoke)
PYTHONPATH=src python -m cozempic.bench.run_compression

# real corpus
PYTHONPATH=src python -m cozempic.bench.run_compression \
    --corpus ~/.claude/projects --limit 400 [--json]
```

### What it measures

Per session, per prescription (`gentle` / `standard` / `aggressive`):
- **tokens/bytes reclaimed** and **% reduction** (corpus-wide and mean-per-session)
- **safety** — whether the pruned session passes cozempic's own post-prune
  validation (C1–C7). A prune that would wipe/mangle the conversation is counted
  **unsafe** and yields 0 reclaim (never an unsafe "win").

Plus the **150K checkpoint A/B**: how many sessions the fixed early tier is active
for (windows where 150K < soft), how many it actually fires on (sessions that
cross 150K tokens), and the extra tokens it reclaims by firing *before* the soft
(25%) tier.

### Results

Corpus: 400 real sessions from `~/.claude/projects` (≥300 KB each — the sessions
large enough to exercise pruning), 52.1M original tokens. Measured 2026-07-15 on
this branch.

| Prescription | Corpus reclaim | Mean/session | Unsafe sessions |
|---|---|---|---|
| gentle | 5.45% (2.84M tok) | 3.25% | 1 / 400 |
| standard | 10.34% (5.39M tok) | 6.71% | 1 / 400 |
| aggressive | 49.43% (25.77M tok) | 41.90% | 1 / 400 |

**150K early-checkpoint tier**
- active (window > checkpoint): 400 / 400 sessions (all 1M windows)
- fired (session reached 150K): **63 / 400**
- extra tokens reclaimed early by the checkpoint: **2.39M**

**Safety:** the single "unsafe" session per tier is the *same* session, where
cozempic's C3 conversation-survival check correctly **refused** a prune that would
have wiped the conversation (`surviving users=1, assistants=0`). This is the
safety mechanism working as designed — the harness reports it and keeps the
original transcript.

### Reading the checkpoint result

The 150K tier fires on ~16% of large sessions and reclaims 2.39M tokens *earlier*
than the soft tier would have. Reclaim is monotonic across prescriptions
(gentle ⊂ standard ⊂ aggressive), as expected from the strategy subsets.

## Tier 2 — Task quality (SWE-bench A/B)

### Design

A task is a repo checkout with a failing test. An **agent** edits the code; the
task is **graded by running pytest** (`grade_task`) — no LLM judge, no substring
recall. `run_ab` runs each task under every **arm** and reports resolve-rate.

Arms are per-arm **environment overlays** (`DEFAULT_ARMS`):
- `baseline` — full cozempic, all defaults on
- `no-checkpoint` — `COZEMPIC_CHECKPOINT_TOKENS=0` (disables the fixed 150K tier)

This uses the *real, honored* env var (`guard._checkpoint_threshold_tokens`), so
the A/B measures the actual code path. Extend `arms` to compare any config.

> Note: the original harness toggled a `COZEMPIC_DISABLE` env var that no code
> honored; the restored harness drops it in favor of real, honored env overlays.

### How to run

```bash
# deterministic unit tests (fake agent, real pytest grading)
PYTHONPATH=src python -m pytest tests/test_swebench_harness.py \
    tests/test_swebench_predict.py tests/test_bench_stats.py

# live SWE-bench sweep (slow, costs tokens; needs datasets + swebench + Finch)
PYTHONPATH=src python scripts/swebench_ab.py \
    --instances astropy__astropy-12907           # plan only
PYTHONPATH=src python scripts/swebench_ab.py \
    --instances astropy__astropy-12907 --live     # actually run claude -p
# grade the emitted preds_*.json with the official swebench harness on Finch.
```

The live agent path is gated behind `COZEMPIC_LIVE_LLM=1` in tests and `--live`
in the driver, so CI never spends tokens.

### Status

Harness is **functional and unit-tested** (21 tests, 1 live-skip): grading,
per-arm resolve-rate aggregation, agent-crash isolation, custom arms, N-run
statistics (mean + 95% CI). A full live SWE-bench sweep is a separate, costly
run — the harness is ready for it; the resolve-rate table will be filled in once
a sweep is executed on Finch.

## Test coverage

- `tests/bench/test_compression.py` — 7 tests (reclaim monotonicity, safety,
  checkpoint gating on/off, JSON summary)
- `tests/test_swebench_harness.py` — grading + arm A/B + crash isolation + custom arms
- `tests/test_swebench_predict.py` — prediction capture
- `tests/test_bench_stats.py` — N-run mean/CI
- `tests/test_checkpoint_tier.py` — 150K tier resolution/gating (7 tests)
