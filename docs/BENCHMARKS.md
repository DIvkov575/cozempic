# Cozempic Benchmarks

> **2026-07-16 revert notice:** the 150K early-checkpoint tier, the 68%/68%
> low-jitter curve, and the `recoverability` strategy described in Tiers 1/3/4
> below were **reverted** to move the fork's gates/harnesses back toward
> upstream (see the revert commit). The historical results and A/B tooling are
> kept here as a record of what was measured — they no longer describe the
> current default behavior. Tier 4 (jitter) and the checkpoint A/B in Tier 1
> refer to a removed feature; current defaults are upstream's soft 25% / hard1
> 55% / hard2 80% curve, no checkpoint tier.

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
statistics (mean + 95% CI).

## Tier 3 — 3-arm build comparison (none / ruya / mine)

The env-overlay A/B above compares *configs of one build*. The 3-arm comparison
compares three **different cozempic builds** driving the same tasks, each in an
isolated venv + `CLAUDE_CONFIG_DIR` (see `cozempic.bench.arms`):

- **none** — plain Claude Code, cozempic not wired
- **ruya** — upstream `cozempic==1.8.39`
- **mine** — this fork (150K checkpoint + memory-overhaul + tail removal)

**Isolation correctness (critical):** each arm's subprocess has `PYTHONPATH`
stripped so a harness run under `PYTHONPATH=src` can't leak the working-tree build
into every arm and shadow its venv. Verified: the ruya arm resolves to a clean
`1.8.39`, mine to `1.8.39+divkov.checkpoint`, none to no install.

### How to run

```bash
# isolation smoke (builds 3 venvs, asserts distinct builds; no tokens)
PYTHONPATH=src python scripts/smoke_3arm.py
PYTHONPATH=src python scripts/smoke_3arm.py --live      # + real claude -p per arm

# full sweep: run claude -p per (instance, arm), capture predictions
PYTHONPATH=src DOCKER_HOST=... python scripts/swebench_3arm.py \
    --instances astropy__astropy-12907 --live
# then grade each preds_<arm>.json with the official harness on Finch:
python -m swebench.harness.run_evaluation --dataset_name SWE-bench/SWE-bench_Lite \
    --predictions_path preds_mine.json --run_id 3arm_mine --max_workers 2
```

### Results — first live sweep (2026-07-15)

Instance `astropy__astropy-12907` (SWE-bench_Lite), real `claude -p` per arm,
graded in Finch (x86 emulation via QEMU):

| Arm | cozempic build | resolved | patch |
|---|---|---|---|
| none | (none) | **1 / 1** | 506 B |
| ruya | 1.8.39 | **1 / 1** | 506 B |
| mine | 1.8.39+divkov.checkpoint | **1 / 1** | 506 B |

**Reading:** all three arms resolved the instance with an identical patch. For a
single, non-context-stressing task, pruning strategy is expected to make no
difference — the meaningful finding is a **negative result**: enabling cozempic
(ruya *or* mine) did **not** regress task success vs no pruning, and mine did not
regress vs ruya. Pruning's value shows on *long, context-heavy* tasks where the
`none` arm hits the autocompact wall; distinguishing the arms on resolve rate
requires a larger, longer-context instance set. The harness + isolation are
proven end-to-end; scaling the instance count is the next step.

### Smoke (isolation proof)

`scripts/smoke_3arm.py` builds all three arm venvs and asserts each imports only
its own build (none=∅, ruya=1.8.39, mine=fork), with distinct config dirs — the
gate that the comparison is valid before any token spend. Live smoke confirmed all
three arms complete the run→grade loop on a trivial task.

## Tier 4 — Jitter sweep (curve tuning on real growth curves)

Objective: **stability**, not maximum compression — keep usage under a ~700K
effective ceiling while reloading as rarely as possible. Only a *reload* rewrites
the model's live context (gentle tiers are read-only, #106), so **jitter == reload
count**.

`cozempic.bench.jitter` reconstructs each session's **real** token-growth curve
from the per-turn `usage` blocks (the API-reported cumulative context size — the
same total cozempic's exact estimator uses; NOT a char estimate, and NOT summable
across messages since usage is cumulative). It then replays that curve against
candidate policies: a single reload threshold + a depth target the reload drops to.

### How to run

```bash
PYTHONPATH=src python -m cozempic.bench.run_jitter --corpus ~/.claude/projects \
    --reload-at 680000 --depths 350000 450000 550000
```

### Results — sweep over all real sessions (reload at 680K)

| Prune depth | Sessions that reload | Total reloads | Reloads / session | Peak | >700K |
|---|---|---|---|---|---|
| → 350K (deep) | 5 | **8** | **1.60** | 682K | 0 |
| → 450K (moderate) | 5 | 9 | 1.80 | 685K | 0 |
| → 550K (shallow) | 5 | 15 | 3.00 | 708K | 1 |

**Finding:** deeper pruning = less jitter, empirically. Pruning to 350K nearly
halves reloads vs 550K (1.60 vs 3.00/session) and holds peak under 700K; the
shallow 550K target re-triggers near the threshold (one session still peaked at
708K). Only 5 sessions in the whole history ever grow large enough to reload —
jitter is rare regardless, but deep pruning minimizes it among those.

**Applied curve** (`DEFAULT_HARD*_TOKEN_PCT` in `tokens.py`): hard1 = hard2 = 68%
(~680K on a 1M window), colocated so the deep **aggressive** reload leads (no
shallow standard precursor to thrash). Aggressive reclaims ~48% → drops to ~350K.
Checkpoint (150K) and soft (250K) gentle tiers unchanged; force at 88%.

> Caveat: a saved session is one realized transcript; a real reload changes what
> the agent does next, so replayed counts compare policies rather than predict
> absolutes. Good enough to pick the lowest-jitter curve from your own history.

## Test coverage

- `tests/bench/test_compression.py` — 7 tests (reclaim monotonicity, safety,
  checkpoint gating on/off, JSON summary)
- `tests/test_swebench_harness.py` — grading + arm A/B + crash isolation + custom arms
- `tests/test_swebench_predict.py` — prediction capture
- `tests/test_bench_stats.py` — N-run mean/CI
- `tests/test_checkpoint_tier.py` — 150K tier resolution/gating (7 tests)
- `tests/bench/test_arms.py` — 3-arm specs, prepare_arm isolation, PYTHONPATH-strip,
  sweep + crash isolation (6 tests + 1 opt-in real-install)
