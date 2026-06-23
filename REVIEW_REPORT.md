# Adversarial Review — L1 Dashboard-Coercion Hardening (worktree-fix-dashboard-l1)

## Scope
- Commits reviewed: `origin/main`(b5e0aef, v1.8.34) .. HEAD (a593bdb) — 8 commits
  (93eeb20 RED, 73f0184 P0-A/B/C, 225e1b4 P0-D, dc1bc1d P0-E, b65bed9 P0-F, c7992ad P0-G, db06229 P0-H, a593bdb /simplify)
- Files read end-to-end (Read tool):
  - `src/cozempic/_constants.py` (lines 1-28)
  - `src/cozempic/dashboard/aggregate.py` (lines 1-210)
  - `src/cozempic/dashboard/lifetime.py` (lines 1-90)
  - `src/cozempic/dashboard/render.py` (lines 1-318)
  - `src/cozempic/metrics.py` (lines 1-446)
  - `src/cozempic/receipts.py` (lines 1-179)
  - `src/cozempic/_validation.py` (lines 255-293, parse_env_bool)
  - `tests/test_dashboard_coercion_corpus.py` (lines 1-475)
  - `PLAN.md` (lines 1-705)
- Methodology: cozempic CLAUDE.md FIX DISCIPLINE (class-of-bug fold, pre-existing test verification, lead-deviation confirm);
  `.claude/rules/team/pipeline.md` step 6 gates; PR #92 (Read-tool evidence per finding), PR #94 (pre-existing test gate +
  fold-invariant audit), PR #98 (realistic-install probe). Empirical reproduction in an isolated detached base worktree.

## Verdict
**PASS WITH CONDITIONS** — the core L1 crash-hardening is correct, RED-at-base genuine (31 subtest failures at base → all GREEN on
branch), full suite 1749 passed / 0 fail, zero new deps, fold-invariants for `_int`/`_context_pct`/`receipts_enabled` truthy path
preserved. BUT two lead-decision deviations remain unfixed (H-1, H-2) and the `receipts_enabled` rewrite silently reverses a privacy
opt-out far more broadly than the PLAN claims (H-3). These must be resolved before push.

---

## Findings

### CRITICAL (must fix before merge)
_None._ Every confirmed defect is either contained by a downstream clamp (dashboard read path never crashes) or is a
correctness/UX/privacy regression rather than a crash or data-loss. The crash-hardening goal of the PR is met.

### HIGH (should fix before merge)

#### H-1. `_num_or_zero` clamps huge int to 10**15 instead of returning 0 — CONTRADICTS the lead Q-C decision + sibling `_int`
- **Location:** `src/cozempic/dashboard/lifetime.py:45`
- **Read tool on:** lifetime.py (lines 26-46) — confirms `return min(result, _MAX_RECEIPT_INT)`; aggregate.py (lines 62-75) — `_int` returns 0 for `value > _MAX_RECEIPT_INT`.
- **Issue:** The lead's Q-C decision (per the spawn brief) was that `_num_or_zero` should return **0** for a huge int — matching
  `aggregate._int` ("huge -> 0") and avoiding a fabricated quadrillion in the lifetime ledger display. The shipped code returns
  `min(result, _MAX_RECEIPT_INT)` = `10**15`. So for the SAME corrupt value `10**400`: `_int` → `0` but `_num_or_zero` → `10**15`.
  Two sibling coercers on the same dashboard disagree.
- **Evidence:** `_num_or_zero(10**400)` = `1000000000000000`; `_int(10**400)` = `0` (reproduced).
  `load_lifetime({"tokens_saved": 10**400, "tokens_processed": 5_000_000})` returns a dict whose Lifetime band renders
  `_fmt_tokens(10**15)` = **`"1000000000.0M"`** (a fabricated "1 billion M tokens reclaimed" headline). With the lead's `→0`,
  `load_lifetime` would return `None` at the `saved <= 0` early-out (line 63-64) → no fabricated band at all.
- **Recommended fix:** `_num_or_zero`: `return 0` when `result > _MAX_RECEIPT_INT` (mirror `_int`). Fix the docstring (see H-2).
  Update `TestNumOrZero::test_huge_int_clamped` to assert `== 0` (re-prove RED at base — it currently asserts only `<= _MAX_RECEIPT_INT`, which 10**15 satisfies, so the test does NOT pin the lead decision).

#### H-2. `_num_or_zero` docstring is self-contradictory ("huge/negative -> 0" header vs "cap at _MAX_RECEIPT_INT" body)
- **Location:** `src/cozempic/dashboard/lifetime.py:27` (header) vs `:34` + `:45` (body)
- **Read tool on:** lifetime.py (lines 26-46) — header says `huge/negative -> 0`; body says `cap at _MAX_RECEIPT_INT` and code does `min(result, _MAX_RECEIPT_INT)`.
- **Issue:** The docstring header promises `huge -> 0`; the body and code deliver `huge -> 10**15`. A reader trusting the header is
  misled. (Resolved automatically if H-1 is fixed to actually return 0; otherwise the header must change to `huge -> _MAX_RECEIPT_INT`.)
- **Evidence:** Direct read; `_num_or_zero(10**400)` = `10**15`, not 0.
- **Recommended fix:** Make code and docstring agree — preferably both → 0 per H-1.

#### H-3. `receipts_enabled` rewrite silently FLIPS a privacy opt-out for every non-truthy-token value (over-broad; PLAN claim is false)
- **Location:** `src/cozempic/receipts.py:54`
- **Read tool on:** receipts.py (lines 42-55); _validation.py (lines 261-293, parse_env_bool token sets).
- **Issue:** Base semantics: `not os.environ.get(_OPT_OUT_ENV)` → **ANY non-empty value disables receipts** (truthiness opt-out).
  New semantics: only the 4 truthy tokens `{1,true,yes,on}` disable. So `COZEMPIC_NO_RECEIPTS=disabled` / `=nope` / `=2` / `=off`
  / `=false` / `=0` / whitespace — all of which DISABLED receipts at base — now **ENABLE** them. A user who set
  `COZEMPIC_NO_RECEIPTS=disabled` (a natural "disable" intent) will, after the SessionStart auto-upgrade, silently start writing
  session-hashed prune receipts to `~/.cozempic/receipts/` — a privacy-relevant behavior change. PLAN §1.6/§4.2 claims "the only
  breaking change is for users who wrote `COZEMPIC_NO_RECEIPTS=0`" — that is FALSE; EVERY non-truthy-token value flips.
- **Evidence (reproduced, branch vs base):**
  | value | base | branch |
  |---|---|---|
  | `disabled` | disabled | **ENABLED** |
  | `nope` / `2` / `off` | disabled | **ENABLED** |
  | `'  '` (whitespace) | disabled | **ENABLED** (lost invariant) |
  | `0` / `false` | disabled | **ENABLED** (intended fix) |
  | `1` / `true` / `yes` / `on` | disabled | disabled (preserved) |
- **Compounding:** the docstring (lines 51-53) claims "The module-level parse_env_bool call in _validation.py emits the warning
  once at startup if the knob is unrecognized." There is NO module-level read of `COZEMPIC_NO_RECEIPTS` anywhere (only
  `COZEMPIC_DEBUG` in digest.py:49). With `warn=False` and no module-level read, an unrecognized opt-out string is swallowed with
  **zero warning, ever** — the user is never told their opt-out is now ignored.
- **Recommended fix:** Decide intent with the lead. Either (a) keep the broad "any non-empty = opt-out" contract and only special-case
  the falsy tokens (`receipts_enabled = parse_env_bool(... )` semantics where unrecognized = TRUE/disabled), preserving the
  privacy-fail-safe; or (b) keep parse_env_bool but emit a one-time module-level warning for unrecognized opt-out values and remove the
  false docstring claim. For a PRIVACY opt-out, fail-safe should be "disabled" (option a) — a user who fat-fingers the value should NOT
  silently start writing receipts. The current fail-OPEN default is the wrong direction for a privacy knob.

### MEDIUM

#### M-1. Deviation #2 confirmed UNFIXED — `_fmt_bytes(huge)` returns `"0 B"` while `_fmt_tokens(huge)` clamps; the clamp is dead for ints >= 10**309
- **Location:** `src/cozempic/dashboard/render.py:82` (`raw = float(n)`) vs `:88` (clamp)
- **Read tool on:** render.py (lines 50-94) — `_fmt_tokens` clamps the INT magnitude (line 64) before float division; `_fmt_bytes` calls `float(n)` at line 82 BEFORE the clamp at line 88.
- **Issue:** For an int >= 10**309, `float(n)` overflows → caught by the `except (ValueError, OverflowError): return "0 B"` → the
  clamp at line 88 NEVER RUNS. So `_fmt_bytes(10**400)` = `"0 B"`, but `_fmt_tokens(10**400)` = `"1000000000.0M"`. The two sibling
  formatters silently diverge on identical corrupt input — exactly the deviation #2 the lead flagged. (For 10**15..10**308,
  `_fmt_bytes` does clamp correctly → `"931322.6 GB"`; the bug is the >=10**309 band only — but the inconsistency is real.)
- **Evidence (reproduced):** `_fmt_bytes(10**308)`=`"931322.6 GB"`, `_fmt_bytes(10**309)`=`"0 B"`, `_fmt_bytes(10**400)`=`"0 B"`;
  `_fmt_tokens(10**400)`=`"1000000000.0M"`. The test `test_fmt_bytes_huge_int_does_not_raise` PASSES because `"0 B"` is a non-empty
  str — it asserts "no raise", NOT consistency, so the divergence is untested.
- **Recommended fix:** Clamp the INT magnitude before the float conversion, mirroring `_fmt_tokens`:
  `try: i = int(n) except ...: return "0 B"; v = float(min(abs(i), _MAX_RECEIPT_INT))`. Add a corpus assertion that
  `_fmt_bytes(10**400)` returns a GB string (parity with `_fmt_tokens`), re-proven RED at base.

#### M-2. `validate_receipt` NaN/inf rejection is INCOMPLETE — `bytes.*` fields are NOT checked (same bug class half-folded)
- **Location:** `src/cozempic/metrics.py:438-445`
- **Read tool on:** metrics.py (lines 404-446) — the new loop checks only `tokens.{before,after,reclaimed,reclaimed_pct}` and `model.context_window`; `bytes.{before,after,reclaimed}` are absent.
- **Issue:** F4's intent is "no NaN/inf reaches `json.dumps` → no non-standard literal in the `.jsonl`." But `bytes.before/after/reclaimed = NaN/inf` still passes `validate_receipt` and `serialize_receipt` still emits the literal `NaN`/`Infinity`. Per CLAUDE.md class-of-bug fold rule, the same-PR fold should cover ALL fields of the same class, not defer `bytes.*` "to a follow-up" (PLAN §2.8).
- **Evidence (reproduced):** receipt with `bytes.reclaimed=float('inf')` → `validate_receipt` does NOT raise; receipt with
  `bytes.before=float('nan')` → `"NaN" in serialize_receipt(r)` is `True`.
- **Recommended fix:** Extend the finiteness loop to `bytes.{before,after,reclaimed}`. Add corpus tests asserting `validate_receipt`
  raises on `bytes.*` NaN/inf (RED at base).

#### M-3. `_context_pct` guards negative WINDOW but NOT negative `after` — sibling operand left unguarded (same class as the fixed bug)
- **Location:** `src/cozempic/dashboard/aggregate.py:95-102`
- **Read tool on:** aggregate.py (lines 86-103) — the guard checks `window > 0` and `after <= _MAX_RECEIPT_INT` but never `after >= 0`.
- **Issue:** The PR added `window > 0` to fix the negative-window → negative-% bug, but the `after` operand can still be negative.
  `_context_pct({'tokens':{'after':-100},'model':{'context_window':200000}})` returns `-0.1` — a nonsensical negative context-usage %.
  Same bug class (negative operand → negative pct) the PR fixed for `window`, left half-fixed for `after`.
- **Evidence (reproduced):** returns `-0.1` (finite, so `_sparkline`'s `math.isfinite` does not catch it; only the viewbox clamp at
  render.py:141 hides it visually, but the stored timeline value is wrong).
- **Recommended fix:** add `and after >= 0` to the guard. Add a corpus case `test_negative_after_returns_none` (RED at base).

#### M-4. `build_receipt` writes the raw huge int into `tokens.*` / `bytes.*` — receipt becomes non-portable JSON (huge-int number), only the WRITE path's `reclaimed_pct` is guarded
- **Location:** `src/cozempic/metrics.py:276-282, 295-297, 368-375`
- **Read tool on:** metrics.py (lines 276-297, 363-375) — `tokens_before/after/reclaimed` and `bytes_*` are stored verbatim from `PrescriptionResult`; only `reclaimed_pct` gets the finiteness guard.
- **Issue:** A corrupt `PrescriptionResult(original_tokens=10**400)` produces a receipt with `tokens.before = 10**400`. It serializes
  to *valid Python JSON* (big-int number) but a 401-digit JSON number is NOT portable: JS `JSON.parse` → `Infinity`/precision loss,
  Go/Rust strict int parsers overflow. This is the same observability-portability concern as F4 (NaN/inf), left unaddressed on the
  WRITE path. cozempic's own dashboard read path clamps it (so cozempic doesn't crash), but the on-disk artifact is non-portable.
- **Evidence (reproduced):** `build_receipt(...)['tokens']['before'] == 10**400`; `validate_receipt` accepts it (no upper-bound check);
  `serialize_receipt` produces a 1682-char line that `json.loads` round-trips in Python but is out-of-range for fixed-width int consumers.
- **Recommended fix:** out of strict scope (PR is dashboard-READ hardening), but per "never silently skip" — either clamp/validate
  numeric magnitudes in `validate_receipt` (upper bound), or document in `TODO.md` as a tracked follow-up (write-path numeric bound).
  Do NOT leave it silently undocumented.

#### M-5. Test-quality: subtest-masking hides RED/GREEN status in the `-v` PASSED/FAILED column
- **Location:** `tests/test_dashboard_coercion_corpus.py:451-465` (`TestFmtHelpersCorpus` uses `unittest.subTest`)
- **Read tool on:** the test file (lines 444-470) + base-worktree run output.
- **Issue:** At base, `test_fmt_bytes_huge_int_does_not_raise` and `test_fmt_tokens_huge_int_does_not_raise` show **`PASSED`** in
  the `-v` column even though their subtests (`value='10...400'`, `inf`, `-inf`) FAILED — pytest reports `unittest` subtest failures
  separately and does not flip the parent's PASSED/FAILED line. A reviewer using the `-v` PASSED list to identify "non-guard" tests is
  misled. This also explains the lead's "24 fail at base" vs the actual **31 fail** (subtests counted). The RED-at-base proof MUST count
  subtests, not the parent line.
- **Evidence:** base run: `31 failed, 14 passed, 3 subtests passed`; the two `_fmt_*` parents appear in BOTH the PASSED list and the
  SUBFAILED dump.
- **Recommended fix:** none required for correctness (the suite is honest in aggregate), but note in the PR/handoff that RED-at-base
  for the `_fmt_*` corpus is established via subtest count. Optionally split the huge-int subtests into named test methods so the
  `-v` column reflects reality.

### LOW

#### L-1. `TestContextPct::test_huge_after_returns_none` does not assert `None` — name lies, weak guard
- **Location:** `tests/test_dashboard_coercion_corpus.py:131-139`
- **Read tool on:** the test file (lines 131-139).
- **Issue:** Method named `..._returns_none` but the body only does `if r is not None: assertTrue(math.isfinite(r))`. If `_context_pct`
  regressed to return `100.0` for huge-after, this test would still PASS. The sibling `test_huge_window_returns_none` correctly uses
  `assertIsNone`. Inconsistent and weaker than its name implies. (It IS RED at base because base RAISES, so it functions as a
  no-crash guard — but it does not guard the `None` contract.)
- **Recommended fix:** `self.assertIsNone(r)` to match the method name and the actual contract.

#### L-2. `TestLoadLifetime` / `TestAggregateWithHugeReceipts` "does_not_raise" tests assert `type in (dict, NoneType)` — near-tautological
- **Location:** `tests/test_dashboard_coercion_corpus.py:223-253, 322-328`
- **Read tool on:** the test file (lines 213-253, 319-341).
- **Issue:** `assertIn(type(result), (dict, type(None)))` is satisfied by any function returning a dict or None; it only catches a raise.
  Acceptable as a "must not raise" guard (and RED at base because base raises), but it does not pin the clamped VALUE. Pair each with a
  value assertion (already done for `test_huge_reclaimed_clamped_in_lifetime`) for real regression coverage.
- **Recommended fix:** optional — add a value assertion (e.g. the returned `tokens_saved` is clamped/None) for stronger coverage.

#### L-3. Hamilton apportionment `except (OverflowError, ValueError): pass` silently zeros the ENTIRE per-strategy allocation when only `tokens_reclaimed` is corrupt
- **Location:** `src/cozempic/metrics.py:307-318`
- **Read tool on:** metrics.py (lines 299-318).
- **Issue:** A huge `tokens_reclaimed` makes `tokens_reclaimed * b / total_strategy_bytes` overflow → caught → ALL strategies get 0
  (apportionment lost wholesale), even though the per-strategy BYTE shares are fine. Documented as estimate-only and the top-level
  `tokens.reclaimed` survives, so blast radius is low; but the catch is broader than needed (`ValueError` has no reachable trigger here
  since `strat_bytes` are ints, never NaN, so `math.floor` can't ValueError). Over-broad-but-contained.
- **Evidence (reproduced):** `tokens_reclaimed=10**400` → `[0,0,0]`; one huge strat byte → `[1000,0,0]` (no raise).
- **Recommended fix:** acceptable as-is (estimate, documented). If tightening: drop `ValueError` from the catch (no reachable trigger) or
  clamp `tokens_reclaimed` to `_MAX_RECEIPT_INT` before apportioning so corruption degrades gracefully instead of zeroing.

#### L-4. `/simplify` changed the unreachable post-loop sentinel from a `GB` string to `"0 B"` — semantically odd (but inert)
- **Location:** `src/cozempic/dashboard/render.py:94`
- **Read tool on:** render.py (lines 89-94) + `git show a593bdb`.
- **Issue:** The post-loop `return` is genuinely unreachable (the `unit == "GB"` branch always returns). Changing it from a GB-formatted
  string to `"0 B"` is inert but semantically inconsistent (a fall-through after the GB unit would conceptually be GB-scale, not bytes).
  Cosmetic only; the comment correctly flags it unreachable.
- **Recommended fix:** none required; optionally restore a GB sentinel for semantic clarity.

#### L-5. `_fmt_tokens` renders the `_MAX_RECEIPT_INT` clamp as `"1000000000.0M"` rather than a clean magnitude (e.g. "1.0P")
- **Location:** `src/cozempic/dashboard/render.py:65-66`
- **Read tool on:** render.py (lines 50-69).
- **Issue:** When a value is clamped to `10**15`, `_fmt_tokens` emits `"1000000000.0M"` (1 billion M) — ugly and confusing in the UI
  (compounds H-1's fabricated lifetime headline). `_fmt_tokens` has no "P"/"B"/"T" tier above "M", so any value >= 10**12 renders as a
  giant "...M" string. Display-quality nit; the right fix for the corrupt case is H-1 (return 0 so it never reaches the formatter).
- **Recommended fix:** secondary to H-1; optionally add a "B"/"T" tier or cap the displayed magnitude.

---

## Realistic-install probe (PR #98 lesson)
- New imports added by the diff (src): `from .._constants import _MAX_RECEIPT_INT` (x3, in-package), `from ._validation import parse_env_bool` (in-package). No new top-level imports of any external module.
- `math` is used by the new code in metrics.py and render.py — confirmed ALREADY imported at base in BOTH files (`git show b5e0aef:...` → True), so no new dependency introduced.
- `pyproject.toml` `[project].dependencies` key is **ABSENT** (zero external deps, verified via tomllib). cozempic ships zero-deps; nothing in this diff gates behavior on an optional/lazy import. **No psutil-style no-op trap. PROBE PASSES.**

## Pre-existing test verification (PR #94 lesson)
- No test was classified "pre-existing failure" — the full branch suite is GREEN (1749 passed, 5 skipped, 0 failed; 53.6s).
- RED-at-base was VERIFIED in an isolated detached worktree at `b5e0aef`: the new corpus file copied onto base sources yields
  **31 failed, 14 passed, 3 subtests passed**. The genuine RED tests cover every real bug (huge-int OverflowError in `_context_pct`/
  `_fmt_tokens`/`load_lifetime`, bool/negative leaks in `_int`/`_context_pct`, NaN/inf accept in `validate_receipt`, `=0`/`=false`
  opt-out bug in `receipts_enabled`). All flip to GREEN on the branch. RED-at-base is GENUINE (not characterization).
- Caveat (M-5): two `_fmt_*` parent methods show `PASSED` in the base `-v` column due to subtest-masking; their RED status is
  established by the SUBFAILED count, not the parent line.

## Fold-invariant audit (PR #94 lesson)
Control-flow restructures audited for ALL old-preserved side-effects, not just the flagged one:
- **`_context_pct`** (early-return guard): float `after` → None (preserved), `window == 0` → None (preserved), `after == 0` valid → 0.0
  (preserved), non-int → None (preserved). Intended changes only: bool/huge/negative-window now rejected. **One residual gap: negative
  `after` is NOT guarded (M-3)** — this is a MISSING extension, not a lost invariant.
- **`_int`** (clamp + negative-zero): float → 0 (preserved), None → 0 (preserved), 0 → 0 (preserved). Intended change: negative → 0 (fix),
  huge → 0 (fix). No lost invariant.
- **`receipts_enabled`** (truthiness → parse_env_bool): truthy tokens `{1,true,yes,on}` still disable (preserved); empty string → enabled
  (preserved). **LOST invariant: whitespace-only and ALL non-truthy-token non-empty values flipped from disabled → enabled (H-3)** — the
  old "any non-empty = opt-out" privacy fail-safe was a side-effect the rewrite did not preserve.
- **Hamilton try/except** (metrics.py): the old code had NO try/except — the new catch is additive; it can now swallow a whole-allocation
  zeroing on corrupt `tokens_reclaimed` (L-3), contained because the estimate is documented and top-level reclaim survives.

## Builder deviation audit
- **Deviation #1 (`_num_or_zero` → 10**15 not 0, per lead Q-C):** NEW FINDING / NEEDS REVISION — **CONFIRMED unfixed.** Code returns
  `min(result, _MAX_RECEIPT_INT)`; lead decided 0. Sibling `_int` returns 0 for the same input. Plus self-contradictory docstring. → H-1, H-2.
- **Deviation #2 (`_fmt_bytes(huge)` → "0 B" vs `_fmt_tokens` clamp):** NEW FINDING / NEEDS REVISION — **CONFIRMED unfixed** for ints
  >= 10**309. The clamp code at render.py:88 is dead for those values because `float(n)` at line 82 overflows first. → M-2.
- PLAN open questions Q-A (no cli.py wrapper) ACCEPTED as designed. Q-B (single `_MAX_RECEIPT_INT=10**15`) ACCEPTED — calibration
  confirmed safe (MAX_CONTEXT_WINDOW=4M, byte ceiling ~1e10, 5+ orders of headroom, float(10**15) exact). Q-D (`_fmt_int` clamp added)
  ACCEPTED. Q-E (`allow_nan=False` deferred) ACCEPTED but see M-2/M-4 for the residual write-path gaps.

## Verification envelope

## Verification
- Confidence: 93% (every finding reproduced against the actual final-state code in this worktree; RED-at-base run in an isolated
  detached worktree at b5e0aef; full branch suite GREEN reproduced; behavior-flip tables for receipts_enabled and _fmt_* generated
  empirically, not reasoned).
- Signals (>=3 orthogonal): (1) Read tool end-to-end on all 8 changed/relevant source files + the test file + PLAN.md; (2) Python
  reproduction of each defect against the worktree code (`_num_or_zero`, `_int`, `_fmt_bytes`/`_fmt_tokens`, `_context_pct`,
  `validate_receipt`, `receipts_enabled`, `build_receipt`, Hamilton block); (3) isolated base-worktree RED-at-base run
  (31 failed/14 passed); (4) full branch suite (1749 passed/0 failed); (5) base-vs-branch behavior diff for receipts_enabled and
  _fmt_bytes across a value corpus; (6) realistic-install probe via tomllib on pyproject + `git show` of base imports.
- Cross-checked: deviation #1 (`_num_or_zero`=10**15, `_int`=0); deviation #2 (`_fmt_bytes`=0 B, `_fmt_tokens`=1000000000.0M);
  receipts_enabled flip table; validate_receipt bytes.* gap; _context_pct negative-after; build_receipt raw-huge-int write;
  _MAX_RECEIPT_INT calibration vs MAX_CONTEXT_WINDOW=4M; conftest `=1` isolation preserved; L11 corpus isolation (no real-home I/O).
- Not verified: end-to-end `cozempic dashboard` CLI invocation with a corrupt receipt on disk (relied on unit-level reproduction of
  each function the CLI path calls; cli.py:2199-2200 unwrapped-call risk is per-PLAN Q-A, accepted out of scope); whether any downstream
  external consumer of the `.jsonl` actually parses huge-int numbers (M-4 portability is a reasoned consequence, not observed against a
  real consumer).

---

## Round-2 Deferred (F-6)

**M-4 (write-path huge-int JSON portability):** `build_receipt` stores raw huge ints from `PrescriptionResult` into
`tokens.*` / `bytes.*` fields. The dashboard READ path now clamps them (P0-E), so cozempic itself never crashes.
However a 401-digit JSON number is non-portable (JS `JSON.parse` → `Infinity`, Go/Rust strict-int parsers overflow).
This is write-side robustness on corrupt input, out of scope for this dashboard-READ PR.
**Tracked in TODO.md (lead will add post-merge) — defer to follow-up PR.**
