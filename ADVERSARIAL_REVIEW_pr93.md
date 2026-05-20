# Adversarial Review — PR #93 (polish-pr92-followups)

Branch: `fix/guard-polish-pr92-followups`, HEAD `8b1fc39`.
Reviewer: devil-advocate subagent, round 1.
Methodology: Read-tool on production source for every claim (per FIX DISCIPLINE rule).

Files read (signals):
- `src/cozempic/guard.py` (lines 40-90, 360-490, 505-740, 1300-1460, 1690-1810, 1920-2060)
- `src/cozempic/spawn_lock.py` (full, 443 lines)
- `src/cozempic/data/hooks.json` (line 9, full payload — diffed against plugin/hooks/hooks.json: identical)
- `src/cozempic/init.py` (lines 30-150)
- `src/cozempic/cli.py` (lines 1351-1600, 1603-1660)
- Cross-tree grep: `_spawn_locks`, `_spawn_locks_mu` (zero hits in `src/`, only docs/tests)

## Round 1 — PR #93 (devil-advocate)

### Verdict: **PASS WITH CONDITIONS**

No CRIT findings. 1 HIGH (operator-noise during v8→v9 hook migration window), 2 MED, 2 LOW. All mitigations exist in adjacent code or are acceptable trade-offs. PR is ship-ready for upstream after addressing **H1** with a docs/changelog note.

### Findings ranked

#### HIGH

**H1 — v8→v9 hook migration window: stale hooks reading 3-line pidfiles**

When v1.8.15 (with PR #93) is installed on an operator whose `.claude/settings.json` still carries v8 hooks (no `v9` marker, no `head -n 1`), the v8 hook executes `kill -0 "$(cat $pid_file)"`. The new daemon writes a 3-line pidfile (`pid\ntimestamp\ninitiator\n`). Bash word-splits `cat`'s output into 3 args → `kill -0 <pid> 2026-05-19T... spawn-claim-daemon`. Bash's `kill` parses `2026-05-19T...` as an integer leading digit and either:
- exits nonzero (good — hook falls through to spawn fresh, but wastes one cycle re-spawning a duplicate that loses on the spawn_lock O_CREAT|O_EXCL race → DaemonAlreadyStarting → benign)
- exits zero coincidentally if PID 2026 exists on the host (bad — hook sees "daemon alive" but the wrong PID; next cycle re-checks correctly once `_maybe_auto_init` refreshes the hook on the next CLI call)

The window closes on the first cozempic subcommand invocation post-upgrade (auto-init refreshes to v9). But during that window operators may see one spurious "Cozempic: guard active" message AND one extra subprocess that silently fails the spawn claim. Not a crash, but noisy.

**Severity rationale**: production-visible during upgrade, transient, self-healing, no data loss. Net annoyance ~1 hook fire per session post-upgrade. Acceptable to ship if release notes / CHANGELOG mentions: *"On v1.8.15 upgrade, the first SessionStart after upgrade may produce a duplicate-spawn log line — auto-init refreshes hooks on next cozempic CLI call and resolves this."*

**Recommended action**: add a CHANGELOG line (no code change required). The race-handling in `DaemonSpawnClaim._claim` correctly classifies the duplicate as `DaemonAlreadyStarting` so no double daemon survives.

#### MED

**M1 — CAS in `_safe_unlink_session_pidfile` has TOCTOU between `_pid_file_points_to` and `unlink`**

`guard.py:1404-1408`: the check (`_pid_file_points_to`) and the act (`unlink`) are not atomic. A sister process can `os.rename(tmp, pid_path)` between the check and the unlink, and our `unlink(missing_ok=True)` will destroy their fresh claim.

**Mitigation**: this is the SAME pattern used by `reload_self_daemon` (guard.py:2000, 2007, 2021, 2027) and predates PR #93 — sister-module precedent. The blast radius is bounded: a clobbered pidfile only causes the next operation to spawn cleanly (via spawn_lock's O_CREAT|O_EXCL gate). NOT a regression introduced by PR #93.

**Recommended action**: none for PR #93. File a follow-up to consider `renameat2(RENAME_EXCHANGE)` (Linux) or open(O_EXCL) + compare-then-unlink-by-fd as a hardening pass in a future release. Document the known window in `_safe_unlink_session_pidfile`'s docstring (it acknowledges the CAS pattern but does not name the TOCTOU window).

**M2 — Hard cap of 4.17h may cut some BMAD investigations short**

`HARD_LOOP_HARD_EXIT_THRESHOLD=50 × HARD_LOOP_BACKOFF_CAP_SECONDS=300 = 15,000s = 4.17h` worst case. Real BMAD investigations under heavy debate can run 2-4h; an extreme outlier (multi-round adversarial review with 5+ researchers) could brush 5h. At the hard cap, the daemon exits and the diagnostic explicitly does NOT recommend `/clear` (good — line 684-693 reads correctly: *"Subagents are still active; their state may be lost on the next compaction. Consider letting current subagents finish then starting a fresh session."*).

**Mitigation**: `COZEMPIC_GUARD_HARD_EXIT_K` env var allows ops to lift the cap up to 1000 (≈3.5 days). The cap is the circuit breaker, not the primary design.

**Recommended action**: none for PR #93. The default is correct for 95% of sessions; the env var is documented in the docstring (line 65-67).

#### LOW

**L1 — `_parse_pidfile_pid` does not catch UnicodeDecodeError on malformed UTF-8 pidfile content**

`spawn_lock.py:173`: `content = pid_path.read_text()` uses the locale default encoding. On a pidfile with non-UTF-8 bytes (impossible via cozempic-written content, but possible via filesystem corruption or external write), `UnicodeDecodeError` propagates past the `except OSError`. **No realistic path** — cozempic only writes ASCII digits + ISO timestamps + ASCII initiator strings.

**Recommended action**: add `errors="replace"` to the read_text call OR wrap with `try/except (OSError, UnicodeDecodeError)`. One-line hardening, not a blocker.

**L2 — `_pid_file_points_to` does not re-check freshness**

`guard.py:1935-1952`: returns True if the pidfile contains `expected_pid`, but does not verify the pidfile is still fresh (mtime within the fresh window). In an extreme PID-reuse scenario (our PID dies, kernel recycles it to a different process that happens to also write our PID into the pidfile via stale tmp → impossible without cozempic running there), this could match incorrectly. Theoretical, not practical.

**Recommended action**: none. The PID match is already a strong signal; adding mtime would be defense-in-depth with no realistic attack vector.

### Per-commit closure verdict

| Commit | Verdict | Notes |
|---|---|---|
| `c631918` (item #2 + #3) | **PASS** | `_spawn_locks` cleanly removed (verified via grep across full worktree). `_safe_unlink_session_pidfile` correctly wired in `finally:` at guard.py:777-798 covering all 6 exit paths. CAS pattern matches sister-module precedent. |
| `b8e2c39` (item #5 + N3/M1/M2) | **PASS** | 3-line payload parity confirmed at guard.py:1733-1737 (daemon-write) + spawn_lock.py:369-373 (parent-claim-write). `_parse_pidfile_pid` tolerates 1-line + 3-line. Hook v9 schema applied to both `data/hooks.json` and `plugin/hooks/hooks.json` (diff returns empty). fsync(tmp_fd) + fsync(parent_dir) both present. EACCES → True conservative in `_is_pidfile_fresh`. |
| `8b1fc39` (item #4 K=10 defer) | **PASS** | Defer logic at guard.py:634-740 correctly distinguishes 3 cases: (a) no-agents → original K=10 exit, (b) agents + K<hard_cap → defer + continue prune cycles, (c) agents + K>=hard_cap → hard-cap exit with non-`/clear` diagnostic. `deferred_exit_announced` one-shot prevents log spam. `COZEMPIC_GUARD_HARD_EXIT_K` clamp at (10, 1000] is correct. |
| `658063c` (RED tests) | **PASS** (out of scope — test code, not production) |
| `4195228` (docs) | **PASS** (out of scope) |

### Cross-cutting with 86cb258b investigation

**Confirmed**: PR #93 does NOT solve the transient-daemon reload race surfaced by the 86cb258b investigation. The sequence is unchanged:
1. Reload chain spawns transient daemon for OLD claude-pid
2. Transient writes pidfile with OLD session_id slug (matches what the NEW Claude's SessionStart hook will compute, because slug is `_slug_for(session_id)[:12]` and the session_id stays the same across reload)
3. New Claude's SessionStart fires, sees the transient's pidfile → skips spawn → new Claude UNPROTECTED
4. Transient eventually exits; with PR #93 c631918 it now CLEANS its pidfile in the `finally` block
5. New Claude remains unprotected for the rest of its session because SessionStart does not re-fire on activity

**Net effect of PR #93 on 86cb258b bug**: marginal NET-POSITIVE — the pidfile no longer persists indefinitely after the transient dies. A SUBSEQUENT operation (e.g., next manual `cozempic` call, or `_maybe_auto_init` from a daemon-spawning subcommand) can now re-spawn cleanly because the pidfile is gone. But the core bug (the race during reload itself) is untouched.

**Recommendation for PR #94 scope**: the fix for 86cb258b is independent of PR #93's surface and should land separately. The two PRs do not conflict.

### Conditions for PASS

1. **CHANGELOG/release notes mention H1**: one line acknowledging that the v8→v9 hook upgrade may cause one spurious "duplicate spawn skipped" log line on the first SessionStart post-upgrade. No code change required.

2. **Optional follow-up (NOT a PR #93 blocker)**: file a future-release ticket for L1 (UnicodeDecodeError hardening) and M1 (CAS TOCTOU documentation).

### Confidence envelope

- Confidence: 92% — PR #93 is ship-ready pending the H1 changelog note.
- Signals: Read tool on `src/cozempic/guard.py`, `src/cozempic/spawn_lock.py`, `src/cozempic/data/hooks.json`, `plugin/hooks/hooks.json`, `src/cozempic/init.py`, `src/cozempic/cli.py`. Grep across full worktree for `_spawn_locks` (0 hits in src/, only in docs/tests). Diff between data/hooks.json and plugin/hooks/hooks.json (identical, empty diff). Cross-checked exit-path coverage by enumerating all `sys.exit`, `os._exit` (none), `SystemExit` (raised by sys.exit), `KeyboardInterrupt`, and `break` paths in `start_guard`'s main loop.
- Cross-checked: all 12 attack vectors from the brief, plus cross-cutting analysis with 86cb258b.
- Not verified: end-to-end live-daemon test (the 786 pytest + 900/900 V4 stress reports are taken at face value from the validator's claim — I did not re-run them).
