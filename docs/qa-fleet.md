# Cozempic adversarial QA fleet

This is the canonical list of adversarial lenses we run for **"full QA"** — derived
from the bug classes our contributors (especially [@ynaamane](https://github.com/ynaamane))
have repeatedly surfaced. The goal: **catch these ourselves before review, not after.**

## How to run a full QA pass

For any non-trivial change (or a release), dispatch **one adversarial agent per lens
below**, in parallel, each scoped to: (a) the diff, and (b) the named subsystem. Each
agent tries to *break* the invariant, builds a `/tmp` repro to prove a finding, and
returns `{severity, title, file:line, repro, fix, confidence}`. Then verify findings
adversarially (independent skeptics), fix P0/P1, and re-run the standing tests.

Standing tests (the lenses that fit a deterministic corpus) live in the suite and run
on every `pytest` — they are the regression floor so a fixed class can't silently
come back. **When you fix a new class from a contributor, add a lens row here and a
standing test if one fits.**

Rule of thumb: contributors keep finding **silent** failures (no crash, no log) —
wrong-but-finite values, races that only lose under contention, leaks that only show
at 22h. Logic tests miss these; adversarial + property/corpus testing catches them.

---

## Lenses

### L1 — Input-validation edge cases  ·  standing test: `tests/test_input_coercion_corpus.py`
Throw the adversarial corpus at **every** user-input coercion (CLI argparse `type=`,
`COZEMPIC_*` env reads, `~/.cozempic/config.json` fields): `nan`, `±inf`, the strings
`"inf"/"nan"/"1e999"`, `-0.0`, huge int `10**400`, `""`, whitespace, unicode digits
`"١٢٣"`, bool, `None`, negative, type-confusion. Invariant: **reject cleanly, or return
a finite / in-range / correctly-typed value** — never a silent NaN/inf/huge that
disables the gate it feeds (IEEE-754: every NaN/inf comparison is False).
*Motivating PRs: #116, #83, #79, #26, #97, #10.*

### L2 — Daemon concurrency & lifecycle
Two+ daemons racing one session: exactly one wins the pidfile (CAS), no lost-update,
no torn write. PID-identity gates (recycled-live-PID resurrection), atomic pidfile,
start-time tracking. **Every** exit path (SIGTERM, K-exit, KeyboardInterrupt, each
`break`, reload) unlinks the pidfile and stops the watcher — no leak. Idempotent
hooks (double SessionStart spawns one guard). `claude_pid` forwarded at all reload
tiers. *Motivating PRs: #92, #93, #94, #86, #98, #87, #24.*

### L3 — Resource exhaustion / unboundedness
Memory: long-session ingest is incremental (no full re-read each cycle → no 3GB/22h
leak). Loops: every retry/back-off has a bound + K-exit (no infinite kill→resume).
Regexes: no catastrophic backtracking (ReDoS) on attacker-sized input. Caps:
JSONL line-size limit, oversized-tool-output, `/tmp` artifact accumulation (doctor
sweep). *Motivating PRs: #89, #92, #104, #26, #78.*

### L4 — Filesystem safety
Atomic writes (temp + `os.replace`, temp cleaned on failure) — no torn/partial files,
no `.tmp` orphans. Symlink-safe writes (don't follow a hostile symlink). Path/slug:
project-slug normalization (`_`,`.`→`-`), strict cross-project resolution (no wrong-
project write), `CLAUDE_CONFIG_DIR` honored (no cross-profile leak). Watcher: detect
growth after prune (shrink + inode-swap), missing-file fallback, deterministic close.
*Motivating PRs: #104, #107, #108, #3.*

### L5 — Cross-platform (Windows / macOS / Linux)  ·  standing test: `tests/test_hooks_sync.py` (+ platform-guarded suites)
Windows: `msvcrt` locking (fd position normalized to 0), tempdir, `os.kill` `OSError`,
dict-shape differences, `_SettingsLock`/`_HostFileLock` branches. macOS kqueue vs
Linux poll watcher parity. `PYTHONIOENCODING` on hook pipes. *Motivating PRs: #100,
#96, #91, #112, #113.*

### L6 — Untrusted-input / injection / digest pollution
Transcript/recap content is untrusted: sanitize before injection, cap turns
(`max_turns`), ReDoS cap. Behavioral digest: reject Claude-Code synthetic noise (don't
learn from tool scaffolding), `_infer_scope` word-boundary (no silent scope drift),
two-occurrence activation gate. *Motivating PRs: #104, #84, #85.*

### L7 — Prune safety / data integrity
Pruning must never break the conversation DAG (uuid/parentUuid intact), never drop
`summary`/`queue-operation`, never touch protected team messages. Post-prune structural
validation + a floor (don't over-prune below a safe ratio). Refuse to prune an active/
in-flight session. Metadata-singleton protection. Team state survives compaction
(checkpoint + PostCompact re-inject). *Motivating PRs: #114, #102, #22, #110.*

### L0 — Harness-contract / marker drift  ·  standing test: `tests/test_reload_gate_contract.py`
**The class that has bitten us most (1.8.22 Agent-marker blindness, PR #116, PR #117).**
cozempic's safety gate recognizes harness activity by *hardcoded, assumed* tool names,
input keys, and result-string markers (`name` vs `team_name`; `agent_id:` vs
`agentId:`; "Async agent launched" vs "Spawned successfully"). When the assumed shape
is wrong or drifts, the matcher silently misses → empty roster → the gate reloads
through live work. Our *synthetic* unit fixtures CANNOT catch this — they encode the
same assumption. Defenses: (1) **ground-truth every literal marker against REAL
transcripts** (grep `~/.claude/projects`, excluding our own meta-discussion) — list
the exact tool-use names, input keys, and result strings, and test casing/key
variants (camelCase↔snake, `to`/`agentId`/`recipient`); (2) commit **redacted REAL
fixtures** (`tests/fixtures/harness/`) and assert the detectors match them — a matcher
with zero real-fixture coverage is unverified; (3) **deny-by-default**: if the
transcript shows any coordination signal the parser can't resolve, the gate must
BLOCK, never SIGKILL. A matcher should fail toward "block", not "reload".

### L8 — Reload safety (the 1.8.x guard line)  ·  standing tests: `tests/test_guard_safe_point.py`, `tests/test_interactive_*`, `tests/test_guard_team_agent_spawn.py`, `tests/test_reload_gate_contract.py`
A reload SIGKILLs + resumes, so: NEVER reload through in-flight work (running Workflow
/ background `Agent` subagent / agent team / open tool call) — defer instead.
Interactive: warn-before-reload, reload only at a sustained idle breakpoint, force only
near the wall. Armed-sentinel lifecycle: cleared on **every** reload path
(`_terminate_and_resume` choke point), atomic writes, no stale `warned` survives a
reload. Markers must be matched to the REAL harness output (verify against live
transcripts, not fixtures).
Agent-spawn team visibility: `extract_team_state` must see teammates created via the
`Agent` tool (not just `TeamCreate`). `safe_to_reload` must block when any teammate is
non-benign/non-terminal, and must NOT be wedged by a stale cross-session TeamState
(session anti-wedge). Standing test: `tests/test_guard_team_agent_spawn.py`.
*Motivating: the 1.8.19–1.8.23 guard line; F1 fix (v1.8.24).*

### L9 — Error handling & hook protocol
Hook commands NEVER block and ALWAYS exit 0 (a crash must not break the session); no
uncaught traceback to a stdout-protocol channel. Clean `ConfigError`/argparse error
vs a bare exception. `ValueError` dispatch coverage. Stop-hook stdout is pure JSON
(no updater/auto-init chatter prepended). Auto-init suppressed when global hooks exist.
*Motivating PRs: #88, #103, #92, #111.*

### L10 — Token / threshold accuracy
Heuristic divisor calibration, exact-from-`usage` token counts, 1M vs 200K window
auto-detect, model→window mapping kept current, thresholds scale with the window.
A wrong window mis-times every guard tier + nudge. *Motivating PRs: #97, #27, #28, #20, #76.*

### L11 — Test isolation
Tests must not leak env (`COZEMPIC_*`) into each other, must not write real
`~/.claude`/`~/.cozempic`/`/tmp/cozempic_*` (patch `_guard_tmp_root`, mock the spawn
claim, `mkdtemp`+teardown), and must be order-independent. *Motivating PR: #95.*

### L12 — Schema / packaging integrity  ·  standing test: `tests/test_hooks_sync.py`
`src/cozempic/data/hooks.json` ↔ `plugin/hooks/hooks.json` byte-identical; hook schema
version synced (`init.py` `HOOK_SCHEMA_VERSION` + every marker); version string consistent
across the 6 release files (pyproject, `__init__`, cli `--version`, npm, plugin,
marketplace). The published artifact actually contains the new code (verify, don't assume).

---

## When you fix a contributor-found bug

1. Add/extend the matching **standing test** (corpus row, property assertion, or regression test).
2. If it's a new class, add a **lens row** above.
3. Note it in the release so the class is visibly covered going forward.
