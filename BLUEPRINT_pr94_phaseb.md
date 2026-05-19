# PR #94 Phase B — Implementation Blueprint

**Author**: planner-pr94-phaseb (team guard-crash-bmad)
**Date**: 2026-05-19
**Design source**: `AUDIT_REPORT_pr94_transient_daemon_race.md` (architect, 86% confidence)
**Worktree**: `/Users/yanisnaamane/Algo/cozempic/.claude/worktrees/transient-daemon-pr94`
**Branch**: `fix/guard-transient-daemon-race` (rebased onto `fix/guard-polish-pr92-followups` / PR #93)

---

## Pre-flight: PR #93 symbol verification

Before writing ANY code, confirm each symbol is present in merged main with the exact shape below. If drift detected, write `PR93_DRIFT_REPORT.md` and stop.

| Symbol | Expected location | Expected shape |
|---|---|---|
| `INIT_SPAWN_PARENT = "spawn-claim-parent"` | `spawn_lock.py:151` | module-level str constant |
| `INIT_SPAWN_DAEMON = "spawn-claim-daemon"` | `spawn_lock.py:152` | module-level str constant |
| `_parse_pidfile_pid(pid_path)` | `spawn_lock.py:155` | `(Path) -> int`, tolerates 1-line + 3-line |
| 3-line pidfile schema `pid\nts\ninitiator\n` | `spawn_lock.py:368-376` and `guard.py:1733-1738` | confirmed 3-line write |
| `_safe_unlink_session_pidfile(session_id)` | `guard.py:1375` | CAS via `_pid_file_points_to` |
| `HARD_LOOP_HARD_EXIT_THRESHOLD` | `guard.py:90` | `= _read_hard_exit_threshold()` default 50 |
| `_hard_loop_backoff_sleep(K, interval)` | `guard.py:2085` (approx) | present in source |
| `deferred_exit_announced` flag pattern | `guard.py:468, 661, 740` | bool local in `start_guard` |
| hook v9 schema marker | `data/hooks.json` line 9 | `# cozempic-hook-schema=v9` |

**Current confirmed state** (read from this worktree):
- `reload_lock.py`: 306 lines — NO sentinel symbols yet (`write_reload_sentinel`, `SENTINEL_TTL_SECONDS`, `_reload_sentinel_path_for` are ALL absent). Safe to add.
- `guard.py`: 2122 lines — `_MIN_PRUNE_RATIO`, `_read_min_prune_ratio`, `RELOAD_WATCHER_POLL_TIMEOUT_SECONDS` ALL absent. Safe to add.
- `data/hooks.json`: v9 schema, SessionStart command ends with `# cozempic-hook-schema=v9`.
- `spawn_lock.py`: 443 lines — `INIT_SPAWN_PARENT`, `INIT_SPAWN_DAEMON` at lines 151-152, `_parse_pidfile_pid` at line 155. All present.

---

## Reproducer-scenario walk (hypothesis validation)

The architect's 86cb258b sequence traced against the CURRENT worktree code:

**Step 1** — `_terminate_and_resume` (guard.py:1127) calls `_spawn_reload_watcher` (guard.py:1240) and returns. The OLD guard's `finally` block calls `_safe_unlink_session_pidfile` (guard.py:1375). **Slot is FREE. NO sentinel written.** (This is the bug.)

**Step 2** — Upgrade-chain re-fires SessionStart hook. The hook's fast-path (`GUARD_PID_FILE` check) passes because slot is empty. Hook invokes `cozempic guard --daemon` → `start_guard_daemon` (guard.py:1495). `find_claude_pid()` walks up the process tree, finds OLD Claude (PID 89113, still dying, in process table). `_is_guard_running_for_session(session_id)` → None (slot empty). `DaemonSpawnClaim._claim` (spawn_lock.py:337) → **WINS the O_CREAT|O_EXCL**. Writes `89113-parent-pid\nts\nspawn-claim-parent\n`. Atomic rename writes `transient_guard_pid\nts\nspawn-claim-daemon\n`. **TRANSIENT guard slot claimed.**

**Step 3** — NEW Claude (PID 94466) starts 68s later. Its SessionStart hook fires. Fast-path check: `[ -f $GUARD_PID_FILE ] && kill -0 $(head -n 1 $GUARD_PID_FILE)` → pidfile EXISTS, transient guard alive → **fast-path taken, `cozempic guard --daemon` NOT invoked.** NEW Claude UNPROTECTED.

**OR** if Python path invoked: `start_guard_daemon` → `_is_guard_running_for_session(session_id)` → returns transient PID → returns `already_running=True`. Same result.

**What Phase B does**: `_terminate_and_resume` writes `/tmp/cozempic_reload_<sid12>.in-flight` BEFORE calling `_spawn_reload_watcher`. Both the bash fast-path AND the Python `start_guard_daemon` check this sentinel. Session-start hook exits immediately. No transient daemon spawned. NEW Claude's SessionStart sees no pidfile AND no sentinel (watcher unlinked it after osascript) → spawns a clean guard for PID 94466. PROTECTED.

**Test `test_86cb258b_full_sequence_new_claude_unprotected_on_current_code`**: currently RED because on current code `already_running=True` is returned (Step 3 above confirmed). After Phase B this test goes GREEN.

---

## Class-of-bug fold validation

The architect's GAP-A → GAP-B fold and GAP-C → NEW-1 fold are CONFIRMED:

- **GAP-A** (osascript fire-and-forget, watcher exits ~2-3s after dispatch) and **GAP-B** (watcher has zero observability into whether new Claude started) share the same root: `_spawn_reload_watcher`'s bash script (guard.py:1290-1295) is a one-liner with no post-osascript verification. A single extension of that script (Commit 4) fixes both.
- **GAP-C** (guard exits with no replacement-spawn coordination) IS NEW-1. The sentinel in option (c) IS the replacement-spawn coordination mechanism. Single commit (Commit 2) covers both.

Only ONE fire-and-forget IPC chain instance needs the watcher-poll treatment: `_spawn_reload_watcher`. `start_guard_daemon`'s Popen (guard.py:1689) is already covered by the pidfile contract. `_cleanup_stale_watchers` SIGTERM is best-effort by design. Fold is clean.

---

## Sister-module parity confirmation

`_ReloadLock` patterns in `reload_lock.py` that the sentinel MUST mirror:

| `_ReloadLock` pattern | Line | Sentinel equivalent |
|---|---|---|
| `_lock_path_for(session_id)` | 76-79 | `_reload_sentinel_path_for(session_id)` — same `_slug_for` call |
| `_read_lock_metadata(lock_path)` | 130-157 | `_read_sentinel_metadata` — same tolerant parse, returns (pid, ts, age) |
| `WEDGE_TTL_SECONDS = 60` | 45 | `SENTINEL_TTL_SECONDS = 120` (longer: covers full reload chain) |
| `O_CREAT \| O_EXCL \| O_NOFOLLOW` | 210-216 | Sentinel write uses same flags |
| `payload = f"{os.getpid()}\n{datetime.now().isoformat()}\n{self.initiator}\n"` | 223-227 | Sentinel payload: `f"{claude_pid}\n{datetime.now().isoformat()}\n{INIT_RELOAD_SENTINEL}\n"` |
| `_is_process_alive(pid)` | 82-94 | Reuse (already in `reload_lock.py`) |
| mtime-based stale detection | 255 (age > WEDGE_TTL_SECONDS) | age > SENTINEL_TTL_SECONDS |

`_slug_for` in `reload_lock.py` (line 60-73) uses `_SAFE_CHARS_RE = re.compile(r"[^a-zA-Z0-9_-]")` (NOT lowercased). `spawn_lock._slug_for` lowercases first. The sentinel must use `reload_lock._slug_for` for the sentinel path so that `_reload_lock._lock_path_for` and `_reload_sentinel_path_for` are consistent — the sentinel lives in the same namespace as the lock.

---

## Commit 2 — NEW-1 sentinel (option c + b defense-in-depth)

### Files modified

- `src/cozempic/reload_lock.py` — add: `SENTINEL_TTL_SECONDS`, `INIT_RELOAD_SENTINEL`, `_reload_sentinel_path_for`, `_read_sentinel_metadata`, `write_reload_sentinel`, `unlink_reload_sentinel`, `_reload_sentinel_active` (~60 LOC)
- `src/cozempic/guard.py` — modify: `_terminate_and_resume` (add sentinel write + `**kwargs` for test compat), `start_guard` watchdog block (add early unlink at line 499-500), `start_guard_daemon` (add sentinel check before DaemonSpawnClaim) (~25 LOC net change)
- `src/cozempic/data/hooks.json` — bump v9→v10, add sentinel skip prologue + status surface prologue (~30 chars added to the SessionStart command string)

**LOC estimate**: ~85 LOC added/modified across 3 files.

### Implementation order (line-by-line)

#### A. `reload_lock.py` additions (append after line 306, i.e., after `acquire_with_wait`)

1. **Add `SENTINEL_TTL_SECONDS = 120`** and **`INIT_RELOAD_SENTINEL = "reload-sentinel"`** as module-level constants, after the existing `INIT_OVERFLOW = "overflow"` line (line 51). Document: "Sentinel TTL is longer than WEDGE_TTL_SECONDS because it guards the full reload chain (SIGTERM + osascript + Terminal startup + claude -r auth), not just the lock window."

2. **Add `_reload_sentinel_path_for(session_id: str) -> Path`** — mirrors `_lock_path_for` (line 76-79), same `_slug_for` call, but filename is `cozempic_reload_<slug>.in-flight` instead of `cozempic_reload_<slug>.lock`. Validate the path stays within `tempfile.gettempdir()` (no path traversal).

3. **Add `_read_sentinel_metadata(sentinel_path: Path) -> tuple[int, Optional[float]]`** — returns `(claude_pid, age_sec)`. Mirrors `_read_lock_metadata` (line 130-157): tolerant parse (content.strip().split("\n")), line 0 = pid (int), line 1 = ISO timestamp (age), line 2 = initiator (verify == INIT_RELOAD_SENTINEL for diagnostics). Returns `(0, None)` on any parse failure. Does NOT return initiator (caller only needs pid + age for the GC decision).

4. **Add `write_reload_sentinel(session_id: str, claude_pid: int) -> Path`** — uses `O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW` (mirroring `_ReloadLock._try_create` at line 210-216). Payload: `f"{claude_pid}\n{datetime.now().isoformat(timespec='seconds')}\n{INIT_RELOAD_SENTINEL}\n"`. If `FileExistsError` (sentinel already exists from a prior reload cycle that leaked), unlink the stale one and retry ONCE (same stale-cleanup pattern as `_ReloadLock._acquire` line 243-250). Returns the sentinel path. Raises `OSError` only on persistent failure (callers must handle).

5. **Add `unlink_reload_sentinel(session_id: str) -> None`** — best-effort unlink with `missing_ok=True`. No CAS needed (the sentinel is unlinked by the watcher, not by competing processes). Swallows `OSError`.

6. **Add `_reload_sentinel_active(session_id: str) -> bool`** — the Python-side check called by `start_guard_daemon`. Returns True if:
   - sentinel file exists AND
   - `_read_sentinel_metadata` returns age_sec < SENTINEL_TTL_SECONDS (fresh)
   Returns False if file missing OR age >= SENTINEL_TTL_SECONDS (stale → GC by unlinking and returning False).

#### B. `guard.py` modifications

7. **Import `write_reload_sentinel`, `_reload_sentinel_active` from `.reload_lock`** — add to the existing `from .reload_lock import (...)` block at lines 925-927 (inside `guard_prune_cycle`). Also add a top-level import at the module's import block (lines 92-116) so `start_guard_daemon` can use it without a nested import. Specifically, add to the `from .reload_lock import` line:
   ```python
   from .reload_lock import (
       _ReloadLock, ReloadLockHeld,
       INIT_GUARD_HARD1, INIT_GUARD_HARD2,
       write_reload_sentinel, _reload_sentinel_active,  # NEW
   )
   ```
   This top-level import goes at lines 92-116. The existing inline import inside `guard_prune_cycle` (line 925) needs updating too.

8. **Modify `_terminate_and_resume` signature (guard.py:1127-1132)** — add `**kwargs` to the parameter list to be forward-compatible with the test's extra arguments (`rx_name`, `config`, `auto_reload`). The test at `test_guard_transient_race.py:77-84` passes these kwargs; the current signature will raise `TypeError` if called with them, making the test fail for the wrong reason (not "sentinel missing" but "unexpected keyword argument"). The cleanest fix: add `**_ignored_kwargs: object` to the signature. This also future-proofs the function if `guard_prune_cycle`'s call site at line 934 is ever extended.

   New signature:
   ```python
   def _terminate_and_resume(
       claude_pid: int,
       project_dir: str,
       session_id: str | None = None,
       session_path: Path | None = None,
       **_ignored_kwargs: object,
   ) -> None:
   ```

9. **Write sentinel in `_terminate_and_resume`, BEFORE `_spawn_reload_watcher` call (guard.py:1240)**. Insert at line 1238 (after the SIGKILL attempt block, before `_spawn_reload_watcher` call):
   ```python
   # Option (c): write the reload sentinel before spawning the watcher so
   # the SessionStart hook (if re-fired by the upgrade chain or any other
   # path) skips the daemon spawn during the reload window.
   if session_id:
       try:
           write_reload_sentinel(session_id, claude_pid)
       except OSError:
           pass  # best-effort; GC will clear any stale sentinel
   ```
   After this block is the existing `_spawn_reload_watcher(claude_pid, project_dir, session_id=session_id)` call (line 1240 — now shifted to ~1247 after insertion).

10. **Option (b) defense-in-depth: early pidfile unlink in `start_guard` watchdog** (guard.py:499-503). Currently the `if not claude_alive:` block at line 499 calls `checkpoint_team` (line 501) THEN logs "Guard stopping" THEN breaks. The pidfile is unlinked only in the `finally` block. Insert `_safe_unlink_session_pidfile` call BEFORE `checkpoint_team`:

    Current code (lines 499-503):
    ```python
    if not claude_alive:
        print(f"  [{_now()}] Claude process exited (PID {claude_pid}). Final checkpoint...")
        checkpoint_team(session_path=session_path, quiet=False)
        print(f"  Guard stopping (Claude exited).")
        break
    ```
    New code:
    ```python
    if not claude_alive:
        print(f"  [{_now()}] Claude process exited (PID {claude_pid}). Final checkpoint...")
        # Option (b) defense-in-depth: unlink pidfile IMMEDIATELY so a
        # concurrent SessionStart for the new Claude doesn't see a stale
        # transient-daemon slot. The finally-block call is a no-op after
        # this (CAS fails cleanly — we no longer own the file).
        _safe_unlink_session_pidfile(sess.get("session_id"))
        checkpoint_team(session_path=session_path, quiet=False)
        print(f"  Guard stopping (Claude exited).")
        break
    ```
    Note: at this point in `start_guard`, `sess` is the session dict (set at line ~444). The session_id can be extracted as `sess.get("session_id")`. Verify `sess` is in scope at line 499 by reading lines 440-460 of guard.py.

11. **Add sentinel check in `start_guard_daemon` (guard.py:1545-1555)**, immediately AFTER the `session_id` normalization block and BEFORE the existing `_is_guard_running_for_session` call. Insert after line 1543 (`_cleanup_legacy_pid(cwd)`):
    ```python
    # Sentinel check: if a reload is in flight for this session, do NOT
    # spawn a new guard. The reload watcher will unlink the sentinel after
    # osascript fires; the new Claude's own SessionStart spawns the real guard.
    if session_id and _reload_sentinel_active(session_id):
        return {
            "started": False,
            "reason": "reload in flight",
            "pid": None,
            "pid_file": None,
            "log_file": None,
            "already_running": False,
        }
    ```
    This block runs BEFORE the `DaemonSpawnClaim` so the transient daemon is never created.

#### C. `data/hooks.json` modifications — v9 → v10

12. **Edit the SessionStart hook command string** (hooks.json line 9). The existing command is a single-line shell string. Apply two additions (both POSIX-compliant, no bashisms):

    **Addition 1 — sentinel skip prologue**: Insert at the START of the subshell body (after `[ -n "$SESSION_ID" ] && (`), before the `export COZEMPIC_NO_GLOBAL_INIT=1` line:
    ```bash
    SENTINEL_FILE="/tmp/cozempic_reload_${SESSION_ID:0:12}.in-flight"; if [ -f "$SENTINEL_FILE" ] && [ $(( $(date +%s) - $(stat -c %Y "$SENTINEL_FILE" 2>/dev/null || stat -f %m "$SENTINEL_FILE" 2>/dev/null || echo 0) )) -lt 120 ]; then echo "Cozempic: reload in flight, skipping guard spawn"; exit 0; fi;
    ```
    Note: `stat -c %Y` (Linux) vs `stat -f %m` (macOS) — the fallback to `|| echo 0` ensures a parse error produces 0 which makes the age check always false (safe: spawn proceeds). This is POSIX-compatible.

    **Addition 2 — status file surface prologue**: Insert AFTER the sentinel check, before the flock-guarded upgrade check:
    ```bash
    STATUS_FILE="/tmp/cozempic_reload_${SESSION_ID:0:12}.status"; if [ -f "$STATUS_FILE" ]; then echo "Cozempic: previous reload may have failed — $(head -n 3 "$STATUS_FILE" | tail -n 1)"; rm -f "$STATUS_FILE"; fi;
    ```

    **Addition 3 — schema version bump**: Change `# cozempic-hook-schema=v9` to `# cozempic-hook-schema=v10` at the end of the command string.

13. **Extend `_spawn_reload_watcher` bash watcher script** (guard.py:1290-1295) to unlink the sentinel after osascript fires. Current watcher script structure:
    ```
    while kill -0 <pid>; do sleep 1; done; sleep 1; <resume_cmd>; echo "$(date): ..." >> log
    ```
    New structure (insert between `<resume_cmd>` and the `echo` log line):
    ```bash
    ; SENTINEL_FILE=/tmp/cozempic_reload_<sid12>.in-flight; rm -f "$SENTINEL_FILE";
    ```
    This is injected into the f-string at guard.py:1290-1295. The `session_id` is already available in `_spawn_reload_watcher`'s scope via the `session_id` parameter (guard.py:1243). Use the same slug logic (`_slug_for(session_id)[:12]`) — import from `reload_lock` or inline `_slug_for` result.

    The sentinel path must be computed using the same slug as `_reload_sentinel_path_for`. To keep the bash script self-contained (not depending on Python being available in the watcher), compute the slug in Python at watcher-script-generation time and embed it literally in the bash string.

    Also add the post-osascript poll loop for GAP-B liveness (see Commit 4 — these two watcher extensions MUST be sequenced: sentinel unlink goes AFTER osascript AND BEFORE the poll loop to avoid a false-negative sentinel read from a concurrent spawn).

    Actual ordering in the watcher bash script after Phase B:
    ```
    while kill -0 <old_pid>; do sleep 1; done
    sleep 1
    <resume_cmd>
    RESUME_EXIT=$?
    SENTINEL_FILE=/tmp/cozempic_reload_<sid12>.in-flight
    rm -f "$SENTINEL_FILE"
    # [GAP-B poll loop follows here — see Commit 4]
    ```

### RED→GREEN tests this commit flips

From `tests/test_guard_transient_race.py`:
- `TestReloadWritesInFlightSentinel::test_reload_writes_in_flight_sentinel`
- `TestSessionStartHookSkipsSpawnDuringSentinel::test_session_start_hook_skips_spawn_during_reload`
- `TestWatcherUnlinksSentinelAfterOsascript::test_watcher_unlinks_sentinel_after_osascript`
- `TestSentinelMtimeGCAfterStaleWindow::test_sentinel_mtime_gc_after_stale_window`
- `TestPidfileUnlinkedImmediatelyOnWatchedClaudeDeath::test_pidfile_unlinked_immediately_on_watched_claude_death`
- `TestRaceUnderContention::test_race_under_contention`

From `tests/test_transient_daemon_reproducer.py`:
- `TestTransientDaemonReproducer::test_86cb258b_full_sequence_new_claude_unprotected_on_current_code` — FLIPS from "HYPOTHESIS CONFIRMED RED" to GREEN (no longer returns `already_running=True`)
- `TestTransientDaemonReproducer::test_sentinel_would_prevent_race_when_present` — FLIPS GREEN (sentinel check in `start_guard_daemon` returns `reason="reload in flight"`)

`TestReproducer86cb258bNoTransientUnprotectedState::test_86cb258b_reproducer_no_transient_unprotected_state` (in `test_guard_transient_race.py`) — FLIPS GREEN.

**Stays RED after Commit 2**: `test_watcher_writes_status_on_no_new_claude`, `test_watcher_logs_success_when_new_claude_appears`, and all GAP-B + GAP-D tests (those require Commits 3-4).

### Edge cases the implementer must handle

1. **`session_id` is None in `_terminate_and_resume`**: The sentinel write is gated on `if session_id:` (step 9 above). If session_id is None, skip sentinel silently — the watcher bash will also have no session_id to embed a sentinel path. This degrades gracefully: no sentinel = no suppression = small race window (acceptable for the None case which is the pre-1.6.13 backward-compat path).

2. **`sess.get("session_id")` in `start_guard` watchdog (step 10)**: `sess` is in scope at line 499 (it's the session dict used throughout `start_guard`). However, if `session_id` was not determinable at startup, `sess.get("session_id")` returns None and `_safe_unlink_session_pidfile(None)` is a no-op (line 1402 of guard.py: `if not session_id: return`). Safe.

3. **Sentinel `FileExistsError` race in `write_reload_sentinel` (step 4)**: If a prior reload leaked a sentinel AND `_terminate_and_resume` is called again within SENTINEL_TTL_SECONDS on the same session, the O_CREAT|O_EXCL will raise `FileExistsError`. The implementation unlinks the stale one and retries once. If BOTH the first attempt and the retry fail (extremely rare: another process won the retry race), the outer try/except OSError swallows the error. The old sentinel remains in place — which is CORRECT behavior (it means the reload window is already active).

4. **Slug consistency**: the sentinel path is `cozempic_reload_<slug>.in-flight` where `slug = _slug_for(session_id)[:12]` from `reload_lock._slug_for`. The hook bash uses `${SESSION_ID:0:12}` — which is the raw session_id first 12 chars. These are different if session_id contains chars outside `[a-zA-Z0-9_-]` (e.g. the bash regex-sanitized version). In practice, session_ids are UUID hex strings (no special chars), so `_slug_for(sid)[:12] == sid.lower()[:12]`. The bash `${SESSION_ID:0:12}` is already lowercase (the hook sanitizes via `python3 -c "...re.sub(r'[^a-z0-9_-]','_',s.lower())"` in the hook's SESSION_ID derivation). Slugs will be consistent. But the implementer MUST use the hook's `SESSION_ID` variable (already sanitized lowercase) in the sentinel path, not raw `$HOOK_DATA`.

5. **`stat` portability in the hook bash sentinel-age check**: `stat -c %Y` is Linux (GNU stat); `stat -f %m` is macOS (BSD stat). The hook runs on macOS (this project). Use `stat -f %m "$SENTINEL_FILE" 2>/dev/null` as the primary, `stat -c %Y "$SENTINEL_FILE" 2>/dev/null` as fallback. Combine with `||  echo 0` so a missing stat binary degrades to "treat as stale (age=0, spawns proceed)".

6. **`_terminate_and_resume` called from tmux/screen paths (lines 1164-1214)**: The sentinel write (step 9) is inserted in the "plain terminal — SIGTERM + spawn resume watcher" path at line ~1238 (before `_spawn_reload_watcher`). The tmux path (lines 1164-1190) and screen path (lines 1192-1214) both `return` early WITHOUT calling `_spawn_reload_watcher`. They should ALSO write the sentinel because the tmux send-keys `/exit` + `claude --resume` chain has the same transient-daemon race risk. Move the sentinel write to BEFORE the `if term_env == "ssh":` guard (line 1154) so it fires for all code paths. Exception: SSH path returns early at line 1156 — gating on `if session_id:` is sufficient since SSH path does not spawn a watcher or open a new terminal.

   Correct insertion point: after line 1150 (after `term_env = _detect_terminal_env()`), before line 1154 (`if term_env == "ssh":`):
   ```python
   if session_id:
       try:
           write_reload_sentinel(session_id, claude_pid)
       except OSError:
           pass
   ```
   This is earlier than step 9 implied. The watcher sentinel-unlink step in the bash script applies only to the plain-terminal path (where a watcher is spawned). For tmux/screen paths, the sentinel must be unlinked differently — either by a Python call at the end of `_terminate_and_resume` for those paths, or by writing the sentinel with a short TTL for tmux/screen (since the resume is synchronous, not async). **Recommended**: add `if session_id: try: unlink_reload_sentinel(session_id); except: pass` at the END of the tmux block (line 1190) and screen block (line 1214). The sentinel is written before the block and unlinked after the block completes. For the plain-terminal path, the bash watcher unlinks it after osascript (async unlink is correct since the watcher is the asynchronous actor).

7. **`_reload_sentinel_active` GC side-effect**: when `_reload_sentinel_active` finds a stale sentinel (age >= 120s), it unlinks it and returns False. This is a write side-effect from a function named `active`. The function should be `_is_reload_sentinel_active_or_gc` but that's verbose. Document this behavior clearly in the docstring.

### Risk + mitigation

| Risk | Mitigation |
|---|---|
| Sentinel leaked (watcher SIGKILL between sentinel write and sentinel unlink) | mtime GC: sentinel older than `SENTINEL_TTL_SECONDS=120` is treated as stale and ignored. `_reload_sentinel_active` unlinks and returns False. |
| Hook bash `stat` portability failure on some shells | `\|\| echo 0` fallback makes age=0 which bypasses the sentinel check (spawn proceeds). Conservative: worse than stale-sentinel, but never causes permanently suppressed spawns. |
| Hook v9→v10 bump breaks operators who haven't upgraded | v10 is strictly additive. Operators on v9 hooks get no sentinel behavior but their existing guard behavior is unchanged. |
| `_terminate_and_resume` called from tmux/screen without sentinel unlink | Mitigated by step 10 per-path unlink (tmux block line 1190, screen block line 1214). |
| `_slug_for` inconsistency between Python and hook bash | UUID session_ids are lowercase hex, no special chars. Both produce identical 12-char slug. Documented in the function. |
| `write_reload_sentinel` raises at a bad time (OSError on `/tmp` full) | Wrapped in `try/except OSError: pass` at all call sites. Missing sentinel = no suppression = race window remains (degraded, not broken). |

---

## Commit 3 — GAP-D futile reload abort

### Files modified

- `src/cozempic/guard.py` — add: `_MIN_PRUNE_RATIO` constant, `_read_min_prune_ratio()` reader, modify `guard_prune_cycle` (add early return after `saved_bytes <= 0` check), modify `start_guard` (extend K-counter increment + add `_futile_skip_announced` flag) (~45 LOC net change)

**LOC estimate**: ~45 LOC added/modified in guard.py only.

### Implementation order (line-by-line)

#### A. New constant and reader function in `guard.py`

1. **Add `_DEFAULT_MIN_PRUNE_RATIO = 0.10`** and **`_MIN_PRUNE_RATIO_MAX = 1.0`** after `HARD_LOOP_HARD_EXIT_THRESHOLD = _read_hard_exit_threshold()` (guard.py:90). Group with the existing threshold constants:
   ```python
   # Minimum fraction of session bytes that prune must save to justify a
   # reload. If saved_bytes / original_bytes < _MIN_PRUNE_RATIO, the resumed
   # Claude would re-trigger HARD immediately (session bloat is dominated by
   # immutable tool-result blocks that prune cannot touch). Skip the reload,
   # persist the prune output, and let the K-counter advance to the natural
   # exit threshold.
   #
   # Override via env var COZEMPIC_MIN_PRUNE_RATIO. Read at module import
   # time only — restart the daemon to apply a new value.
   _DEFAULT_MIN_PRUNE_RATIO = 0.10
   ```

2. **Add `_read_min_prune_ratio() -> float`** immediately after, following the exact pattern of `_read_hard_exit_threshold` (guard.py:68-87):
   ```python
   def _read_min_prune_ratio() -> float:
       """Read COZEMPIC_MIN_PRUNE_RATIO env var. Clamps to (0.0, 1.0) exclusive.

       Read at module import time only — restart the daemon to apply a new
       value. Invalid values (non-numeric, NaN, inf, <= 0.0, >= 1.0) fall
       back to the default 0.10.
       """
       raw = os.environ.get("COZEMPIC_MIN_PRUNE_RATIO")
       if raw is None:
           return _DEFAULT_MIN_PRUNE_RATIO
       try:
           val = float(raw)
       except (TypeError, ValueError):
           return _DEFAULT_MIN_PRUNE_RATIO
       if not math.isfinite(val) or val <= 0.0 or val >= 1.0:
           return _DEFAULT_MIN_PRUNE_RATIO
       return val

   _MIN_PRUNE_RATIO = _read_min_prune_ratio()
   ```
   Note: `math` is already imported in guard.py (via `_read_hard_exit_threshold`'s sibling module spawn_lock.py which imports it — but guard.py itself should import `math` if not already present. Check guard.py imports at lines 19-27: `math` is NOT in the current import list. ADD `import math` at line 22 (between `import re` and `import signal`).

#### B. `guard_prune_cycle` modification (guard.py:860)

3. **Insert futile-reload-skip early return** between the existing `saved_bytes <= 0` return (lines 860-870) and the `post_te = estimate_session_tokens(...)` line (line 873). Current code at line 872-873:
   ```python
           # Token estimate after pruning — pass pre-calibrated ratio
           post_te = estimate_session_tokens(pruned_messages, pre_calibrated_ratio=pre_ratio)
   ```
   Insert BEFORE line 873:
   ```python
           # Futile-reload abort: if prune saved less than _MIN_PRUNE_RATIO of
           # original bytes, resumed Claude would re-trigger HARD immediately.
           # Skip the reload; persist the prune output; let K-counter advance.
           if 0 < saved_bytes < original_bytes * _MIN_PRUNE_RATIO:
               # Still write checkpoint and save messages (prune output is valid).
               checkpoint_path = None
               if not team_state.is_empty():
                   project_dir = session_path.parent
                   checkpoint_path = write_team_checkpoint(team_state, project_dir)
               backup = save_messages(session_path, pruned_messages, create_backup=True, snapshot=snap)
               if backup:
                   cleanup_old_backups(session_path, keep=3)
               return {
                   "saved_mb": saved_bytes / 1024 / 1024,
                   "original_tokens": pre_te.total,
                   "final_tokens": pre_te.total,  # can't estimate post without running post_te
                   "team_name": team_state.team_name,
                   "team_messages": team_state.message_count,
                   "checkpoint_path": str(checkpoint_path) if checkpoint_path else None,
                   "backup_path": str(backup) if backup else None,
                   "reloading": False,
                   "futile_reload_skipped": True,
               }
   ```
   Note: `saved_messages` and `cleanup_old_backups` must be called BEFORE the early return — mirroring the code at lines 882-886 that follows in the normal path. The checkpoint write must also happen so the user has a team recovery path (GAP-D test 7 requirement).

4. **The `PruneLockError` / `PruneConflictError` exception handlers** (lines 888-893) are outside the `with _PruneLock` block, so the insertion at step 3 (which is inside the `with` block) is correct. The `try:` / `except PruneLockError:` structure means the early return is inside the lock — that's correct, it releases the lock via the `with` context manager's `__exit__`.

#### C. `start_guard` modifications (guard.py:616-740 region)

5. **Extend the `consecutive_empty_hard_prunes` increment condition** (guard.py:616-617). Current code:
   ```python
   if result.get("saved_mb", 0) <= 0:
       consecutive_empty_hard_prunes += 1
   ```
   New code:
   ```python
   if result.get("saved_mb", 0) <= 0 or result.get("futile_reload_skipped"):
       consecutive_empty_hard_prunes += 1
   ```

6. **Add `_futile_skip_announced = False`** local variable in `start_guard`'s variable initialization block, alongside `deferred_exit_announced = False` (guard.py:468). This is the one-shot flag for the futile-skip log message.

7. **Add the futile-skip log emission** in the `if result.get("saved_mb", 0) <= 0 or result.get("futile_reload_skipped"):` branch. Insert BEFORE the existing K-threshold check at line 634. The message emits only once per defer window (mirrors `deferred_exit_announced` pattern):
   ```python
   if result.get("futile_reload_skipped") and not _futile_skip_announced:
       _saved_pct = (result.get("saved_mb", 0) * 1024 * 1024 / original_bytes * 100
                     if original_bytes > 0 else 0)
       checkpoint_ref = (
           f" Checkpoint: {result['checkpoint_path']}" if result.get("checkpoint_path") else ""
       )
       print(
           f"  [{_now()}] Hard prune freed {result.get('saved_mb', 0):.3f}MB "
           f"(~{_saved_pct:.0f}%) — below {int(_MIN_PRUNE_RATIO * 100)}% threshold. "
           f"Reload skipped: resumed Claude would re-trigger HARD immediately. "
           f"Likely cause: subagent transcripts or large tool-results dominate context. "
           f"Recommend: /clear (loses subagent state) or fresh session with restored "
           f"team checkpoint.{checkpoint_ref}",
           flush=True,
       )
       _futile_skip_announced = True
   ```
   Note: `original_bytes` is not in scope at this point in `start_guard` (it's a local in `guard_prune_cycle`). The message can use `result.get("saved_mb")` directly. Use `saved_mb / (1 - saved_fraction)` to back-calculate `original_bytes` — OR simplest: just emit `saved_mb` and ratio. The simpler approach: compute from `original_tokens` and `final_tokens` if available in the result. OR: accept that the `~N%` annotation requires adding `original_bytes` to the result dict from `guard_prune_cycle`. **Recommend**: add `"original_bytes": original_bytes` to the futile_reload_skipped return dict in step 3, then use it here for the percentage message.

8. **Reset `_futile_skip_announced = False`** at the same point `deferred_exit_announced = False` is reset (guard.py:740): when `result.get("saved_mb", 0) > 0` and `not result.get("futile_reload_skipped")`. This mirrors `deferred_exit_announced` exactly.

### RED→GREEN tests this commit flips

From `tests/test_guard_futile_reload.py`:
- `TestMarginalPruneSkipsReload::test_marginal_prune_skips_reload`
- `TestSubstantialPruneProceedsWithReload::test_substantial_prune_proceeds_with_reload`
- `TestMinPruneRatioEnvVarOverride::test_min_prune_ratio_env_var_override`
- `TestMinPruneRatioInvalidFallsBack::test_min_prune_ratio_invalid_falls_back`
- `TestFutileReloadIncrementsKCounter::test_futile_reload_increments_k_counter`
- `TestFutileReloadLogMessageEmitsOnce::test_futile_reload_log_message_emits_once`
- `TestFutileReloadWritesTeamCheckpoint::test_futile_reload_writes_team_checkpoint`

### Edge cases the implementer must handle

1. **`original_bytes == 0`**: Guard against division-by-zero in the `< original_bytes * _MIN_PRUNE_RATIO` check. The condition `0 < saved_bytes < original_bytes * _MIN_PRUNE_RATIO` is already safe when `original_bytes == 0` because `saved_bytes > 0` and `original_bytes * 0.10 == 0`, so `saved_bytes < 0` is False → condition fails → falls through to normal path. Safe.

2. **`saved_bytes` very slightly negative due to floating point**: `saved_bytes = original_bytes - final_bytes` where both are integers (byte counts from `sum(b for _, _, b in messages)`). No floating-point issue — these are integer arithmetic. Safe.

3. **`math` module import**: Add `import math` to guard.py's top-level imports (lines 19-27). Currently absent. Place between `import re` (line 21) and `import signal` (line 22).

4. **`write_team_checkpoint` availability inside the `with _PruneLock` block**: `write_team_checkpoint` is imported at line 110. Available. The futile-skip early return calls it correctly.

5. **Test `test_min_prune_ratio_env_var_override` patches `_MIN_PRUNE_RATIO` via `patch.object`**: the test uses `patch.object(guard_mod, "_MIN_PRUNE_RATIO", 0.05)`. This works only if `_MIN_PRUNE_RATIO` is a module-level name. Confirm it is (step 2 above creates it at module level). The `guard_prune_cycle` function must reference `_MIN_PRUNE_RATIO` as a module-level name (not a local variable captured at function-definition time) so `patch.object` can intercept it.

### Risk + mitigation

| Risk | Mitigation |
|---|---|
| `_MIN_PRUNE_RATIO = 0.10` is too conservative (edge case: small session where 10% = 500 bytes saved) | Env-var override `COZEMPIC_MIN_PRUNE_RATIO` lets operators tune. Field feedback loop built-in. |
| Futile-skip K-counter advancement causes premature exit when agents are active | The existing `HARD_LOOP_HARD_EXIT_THRESHOLD` + `agents_active` defer logic already handles this. The futile-skip count advances K, which is the correct behavior — eventually the daemon exits (circuit breaker). |
| `save_messages` called twice (once in futile-skip early return, once in the normal path) | No double-save: the early return exits the `with _PruneLock` block, releasing the lock. The normal path is never reached. |

---

## Commit 4 — GAP-B watcher poll + status file

### Files modified

- `src/cozempic/guard.py` — add: `RELOAD_WATCHER_POLL_TIMEOUT_SECONDS = 30`, `RELOAD_WATCHER_POLL_INTERVAL_SECONDS = 1`, modify `_spawn_reload_watcher` (extend bash script with poll loop + status write) (~50 LOC change to the watcher script f-string)
- `src/cozempic/data/hooks.json` — v10 already bumped in Commit 2; the status file surface prologue was added in Commit 2 as well (Addition 2 in step 12). If deferred to Commit 4, add it here.

**LOC estimate**: ~50 LOC added/modified in guard.py. hooks.json already updated in Commit 2 if the status surface prologue was included there.

### Implementation order (line-by-line)

#### A. New constants in `guard.py`

1. **Add `RELOAD_WATCHER_POLL_TIMEOUT_SECONDS = 30`** and **`RELOAD_WATCHER_POLL_INTERVAL_SECONDS = 1`** as module-level constants after `HARD_LOOP_HARD_EXIT_THRESHOLD` (guard.py:90):
   ```python
   # Watcher poll window: after osascript fires, the watcher checks for a new
   # claude process (pgrep -f "claude.*<sid12>") for up to this many seconds.
   # 30s matches the scale of _ReloadLock.acquire_with_wait (30s default).
   RELOAD_WATCHER_POLL_TIMEOUT_SECONDS = 30
   RELOAD_WATCHER_POLL_INTERVAL_SECONDS = 1
   ```

#### B. `_spawn_reload_watcher` bash script extension (guard.py:1290-1295)

2. **Compute the sentinel slug** at the start of `_spawn_reload_watcher` (guard.py:1243-1254 region). The `session_id` parameter is already available. Add:
   ```python
   from .reload_lock import _slug_for as _rl_slug_for
   sid12 = _rl_slug_for(session_id)[:12] if session_id else ""
   sentinel_path = f"/tmp/cozempic_reload_{sid12}.in-flight" if sid12 else ""
   status_path = f"/tmp/cozempic_reload_{sid12}.status" if sid12 else ""
   ```

3. **Replace the `watcher_script` f-string** (guard.py:1290-1295) with the extended version. Current:
   ```python
   watcher_script = (
       f"while kill -0 {int(claude_pid)} 2>/dev/null; do sleep 1; done; "
       f"sleep 1; "
       f"{resume_cmd}; "
       f"echo \"$(date): Cozempic guard resumed Claude in {log_dir}\" >> /tmp/cozempic_guard.log"
   )
   ```
   New (multi-line f-string for readability — the bash interpreter does not care about newlines in the string):
   ```python
   _sentinel_unlink = f"rm -f {sentinel_path!r}; " if sentinel_path else ""
   _pgrep_pattern = f"claude.*{sid12}" if sid12 else "claude"
   _status_file = status_path or "/dev/null"

   watcher_script = (
       f"while kill -0 {int(claude_pid)} 2>/dev/null; do sleep 1; done; "
       f"sleep 1; "
       f"{resume_cmd}; "
       f"RESUME_EXIT=$?; "
       # Unlink sentinel AFTER osascript so the new Claude's SessionStart
       # hook can spawn its own guard cleanly (sentinel no longer in-flight).
       f"{_sentinel_unlink}"
       # Poll for the new claude process (up to RELOAD_WATCHER_POLL_TIMEOUT_SECONDS).
       f"deadline=$(( $(date +%s) + {RELOAD_WATCHER_POLL_TIMEOUT_SECONDS} )); "
       f"new_pid=''; "
       f"while [ $(date +%s) -lt $deadline ]; do "
       f"  new_pid=$(pgrep -f '{_pgrep_pattern}' 2>/dev/null | head -n 1); "
       f"  [ -n \"$new_pid\" ] && break; "
       f"  sleep {RELOAD_WATCHER_POLL_INTERVAL_SECONDS}; "
       f"done; "
       # Success: new Claude found — log it.
       f"if [ -n \"$new_pid\" ]; then "
       f"  echo \"$(date): Cozempic guard resumed Claude in {log_dir} (new PID $new_pid)\" >> /tmp/cozempic_guard.log; "
       # Failure: new Claude not found — write structured status file.
       f"else "
       f"  printf '%s\\n%s\\n%s\\n%s\\n' 'failed' "
       f"    \"$(date -Iseconds)\" "
       f"    \"new Claude did not start within {RELOAD_WATCHER_POLL_TIMEOUT_SECONDS}s after resume_cmd (exit=$RESUME_EXIT)\" "
       f"    'investigate: Terminal automation permission / claude -r auth / JSONL path / network' "
       f"    > {_status_file!r}; "
       f"  echo \"$(date): Cozempic guard reload FAILED — no new Claude after {RELOAD_WATCHER_POLL_TIMEOUT_SECONDS}s\" >> /tmp/cozempic_guard.log; "
       f"fi"
   )
   ```
   Note on `date -Iseconds` portability: GNU `date -Iseconds` works on Linux; macOS BSD `date` uses `date -Iseconds` but it's available on macOS 10.8+. Safe. `date +%s` is POSIX on both.

4. **`pgrep -f` pattern safety**: The pattern `"claude.*{sid12}"` will NOT match the OLD Claude because the OLD Claude is dead by the time the poll starts (the `while kill -0 <old_pid>` loop has already exited). However, the pattern could match a concurrent test process with a similar argv. The 12-char slug is specific enough for production use. Tests that use this watcher must mock `pgrep -f` to return controlled output (already done in the RED test's `_fake_popen` implementation).

5. **Shell quoting of `_sentinel_path` in the bash string**: use `shlex.quote` (or the existing `shell_quote` helper from `helpers.py`) to safely embed the sentinel path. Since the path is `/tmp/cozempic_reload_<slug>.in-flight` (no special chars), quoting is cosmetic but good practice.

### RED→GREEN tests this commit flips

From `tests/test_guard_reload_watcher_poll.py`:
- `TestWatcherWritesStatusOnNoNewClaude::test_watcher_writes_status_on_no_new_claude`
- `TestWatcherLogsSuccessWhenNewClaudeAppears::test_watcher_logs_success_when_new_claude_appears`
- `TestWatcherHandlesResumeCmdNonzeroExit::test_watcher_handles_resume_cmd_nonzero_exit`
- `TestSessionStartHookSurfacesPriorStatus::test_session_start_hook_surfaces_prior_status`
- `TestStatusFilePerSessionIsolation::test_status_file_per_session_isolation`
- `TestPollPatternDoesNotMatchUnrelatedClaude::test_poll_pattern_does_not_match_unrelated_claude`

### Edge cases the implementer must handle

1. **`session_id is None` in `_spawn_reload_watcher`**: If `sid12` is empty, `_sentinel_path` and `_status_path` are empty strings. The sentinel-unlink step is omitted (`_sentinel_unlink = ""`). The status file write target falls back to `/dev/null`. The poll pattern degrades to `"claude"` (too broad but won't crash). This is acceptable: without a session_id, the sentinel mechanism doesn't apply.

2. **`date -Iseconds` in the status file write**: Both macOS and Linux have this. If it fails (very old bash, embedded system), `date -Iseconds` exits non-zero and the timestamp slot is empty — not catastrophic.

3. **`pgrep` not available**: Some minimal Linux containers lack `pgrep`. The `pgrep -f ... 2>/dev/null | head -n 1` fallback means `pgrep` failure returns empty output → `$new_pid` empty → poll loop exhausts → status file written. Status file mentions "new Claude did not start" — correct behavior (if pgrep is unavailable, we can't verify). Acceptable degradation.

4. **Watcher poll finds the OLD Claude**: Can happen if the OLD Claude's PID is somehow recycled within 30s to a new `claude` process. `pgrep -f "claude.*<sid12>"` would match a new Claude with any argv containing both "claude" and the sid12 prefix — not necessarily a resumed Claude. If the OS recycles the PID to an unrelated `claude` process (different session), the pid12 won't be in its argv, so the match fails. Correct.

5. **Status file path per-session isolation**: The status file is at `cozempic_reload_<sid12>.status`. With sid12 = first 12 chars of the session UUID, collisions are astronomically rare (12 hex chars = 2^48 distinct slugs). Test 5 in the GAP-B suite verifies isolation.

### Risk + mitigation

| Risk | Mitigation |
|---|---|
| Watcher poll adds 30s to the watcher process lifetime (if new Claude never starts) | Watcher is detached (`start_new_session=True`), so it doesn't block the old guard's exit. 30s extension is invisible to the user. |
| Status file persists if SessionStart never fires (user doesn't resume) | Status file has no TTL. Worst case: stale status file in `/tmp`. Operator cleanup: `rm /tmp/cozempic_reload_*.status`. Consider adding a 24h mtime-based GC in a future PR. |
| `pgrep -f` matches too broadly (OTHER sessions' claude processes) | The `.<sid12>` pattern (12 hex chars) is tight enough for production. Test 6 validates this. |
| Shell quoting of sentinel path with special characters | Path is `/tmp/cozempic_reload_<12-hex-chars>.in-flight` — no shell metacharacters. Single-quoting in the bash string is safe. |

---

## Commit ordering rationale

**Why Commit 2 (sentinel) before Commit 3 (futile-reload) before Commit 4 (poll)**:

1. **Commit 2 is the PRIMARY fix**: it closes the 86cb258b race class. The reproducer test (`test_86cb258b_full_sequence_new_claude_unprotected_on_current_code`) must flip GREEN as early as possible so the reviewer can see the core fix in isolation.

2. **Commit 3 (GAP-D) is independent of Commit 2**: GAP-D operates in `guard_prune_cycle`, sentinel operates in `_terminate_and_resume` and `start_guard_daemon`. No code dependency. But GAP-D logically comes AFTER the spawn fix because: (a) GAP-D prevents the reload from happening when futile, reducing the frequency of the race GAP-B introduced; (b) ordering by "blast radius" (spawn > prune > watcher) is the project's convention.

3. **Commit 4 (GAP-B) is the LAST commit**: the watcher bash extension is the most complex change (longest bash string) and the most platform-specific (osascript, pgrep, stat portability). Isolating it in Commit 4 means the reviewer-e2e agent can validate Commits 2+3 independently before tackling the platform-specific logic.

4. **Commit 1 (RED tests) is already done** (per the task specification — the 7+7+6+2 = 22 RED tests are already in the worktree). This blueprint covers Commits 2-4 only.

**Dependency graph**:
```
Commit 2 (reload_lock.py sentinel + guard.py wiring + hooks.json v10)
    ├── Commit 3 (guard.py GAP-D, independent)
    └── Commit 4 (guard.py _spawn_reload_watcher, depends on sentinel slug from Commit 2)
```
Commit 4 depends on the `_slug_for` import from `reload_lock` which is introduced in Commit 2. Strict sequential: 2 → 3 → 4.

---

## Out-of-scope confirmations

The following are explicitly **NOT** in Phase B:

| Item | Rationale |
|---|---|
| GAP-E: auto-launch fresh session when reload futile | Bigger UX feature, belongs in ROADMAP. Phase B's GAP-D message points the user to the checkpoint path — sufficient for v1.8.15. |
| Orphan daemon reaper (stale PIDs 7085, 11159) | Separate PR "planned" in LESSONS_LEARNED 2026-05-18 upstream-catchup. Not related to the race bug class. |
| `_MIN_PRUNE_RATIO` calibration against production data | Threshold is design-defensible; 0.10 is the initial value, tunable via env-var. Field calibration is a follow-up PR after user feedback. |
| Hook bash `dash` / `busybox sh` portability CI test | The added bash is POSIX-compatible (`[ -f ]`, `$((date +%s))`, `pgrep`). A CI `bash --posix` run is a validator's V6 live-smoke item, not an implementer item. |
| Status file 24h TTL GC | Noted as "future PR" in GAP-B risk table. Not blocking. |
| macOS Automation permission denied UX for osascript | Needs real macOS box for testing. DA round item. Out of scope for the implementer. |
| Windows `start cmd` watcher extension for GAP-B | The poll loop uses `pgrep` which is POSIX-only. Windows path (`elif system == "Windows":`) needs a `tasklist` equivalent. Out of scope for this PR (Windows support is experimental). |

---

## Validation contract for reviewer-e2e

After the implementer delivers Commits 2-4, the reviewer-e2e agent must verify:

### Structural checks (source inspection)

1. `reload_lock.py` exports: `SENTINEL_TTL_SECONDS`, `INIT_RELOAD_SENTINEL`, `_reload_sentinel_path_for`, `write_reload_sentinel`, `unlink_reload_sentinel`, `_reload_sentinel_active` — all present, all with docstrings.
2. `guard.py` line ~1238: `write_reload_sentinel` call precedes `_spawn_reload_watcher` call — verify via `grep -n "write_reload_sentinel\|_spawn_reload_watcher" guard.py`.
3. `guard.py` line ~499: `_safe_unlink_session_pidfile` call precedes `checkpoint_team` call in the `if not claude_alive:` block — verify via source inspection.
4. `guard.py` `start_guard_daemon`: `_reload_sentinel_active` check exists BEFORE `DaemonSpawnClaim.__enter__` call — verify via source inspection.
5. `guard.py` `guard_prune_cycle`: `futile_reload_skipped` early return exists between `saved_bytes <= 0` check (line 860) and `post_te = estimate_session_tokens` (line 873 original numbering) — verify via source inspection.
6. `data/hooks.json`: `# cozempic-hook-schema=v10` present, sentinel skip prologue present (contains "in-flight"), status surface prologue present (contains ".status").
7. `_spawn_reload_watcher` bash script contains: `deadline=$(( $(date +%s) +`, `pgrep -f`, `.status`, `rm -f`.

### Test suite checks

8. Run: `python -m pytest tests/test_guard_transient_race.py tests/test_guard_futile_reload.py tests/test_guard_reload_watcher_poll.py tests/test_transient_daemon_reproducer.py -v` — expect ALL 22 tests GREEN.
9. Run: `python -m pytest tests/ -v --ignore=tests/test_guard_transient_race.py --ignore=tests/test_guard_futile_reload.py --ignore=tests/test_guard_reload_watcher_poll.py --ignore=tests/test_transient_daemon_reproducer.py` — expect NO regressions (all pre-existing tests remain GREEN).

### Functional checks

10. `python -c "from cozempic.reload_lock import write_reload_sentinel, _reload_sentinel_active, SENTINEL_TTL_SECONDS; print(SENTINEL_TTL_SECONDS)"` — must print 120.
11. `python -c "from cozempic.guard import _MIN_PRUNE_RATIO, RELOAD_WATCHER_POLL_TIMEOUT_SECONDS; print(_MIN_PRUNE_RATIO, RELOAD_WATCHER_POLL_TIMEOUT_SECONDS)"` — must print `0.1 30`.
12. `python -c "import math; from cozempic.guard import _read_min_prune_ratio; print(_read_min_prune_ratio())"` — must print `0.1`.
13. Sentinel lifecycle: `write_reload_sentinel("abcdef012345678901234567890abcde", 89113)` creates `/tmp/cozempic_reload_abcdef012345.in-flight` with content starting with `89113`; `_reload_sentinel_active(...)` returns True; `unlink_reload_sentinel(...)` removes the file; `_reload_sentinel_active(...)` returns False.

### DA / adversarial checks (for the DA subagent)

14. **Sentinel leak stress test**: spawn 5 concurrent `_terminate_and_resume` calls on the same session_id. Verify only 1 sentinel file exists at the end (O_CREAT|O_EXCL ensures this).
15. **Stale sentinel age**: write a sentinel, set mtime to `time.time() - 121` (SENTINEL_TTL_SECONDS + 1). Call `_reload_sentinel_active`. Must return False AND unlink the file.
16. **Futile-reload threshold boundary**: test at exactly `_MIN_PRUNE_RATIO` (10% savings = 10,000 bytes on 100,000 byte session). The condition is `saved_bytes < original_bytes * _MIN_PRUNE_RATIO` (strict less than). At exactly 10% the early return should NOT fire. Verify.
17. **`_terminate_and_resume` with `**kwargs`**: call with `rx_name="standard", config=None, auto_reload=True` — must not raise `TypeError`.
18. **Hook bash sentinel check with macOS `stat`**: run the sentinel-age check bash fragment directly on macOS; verify it correctly returns `0` for a fresh sentinel and allows `> 120` to produce a stale detection.

---

## Appendix: current worktree line map (for implementer reference)

All line references are for the REBASED worktree at `/Users/yanisnaamane/Algo/cozempic/.claude/worktrees/transient-daemon-pr94` as of 2026-05-19:

| Location | Current line | Notes |
|---|---|---|
| `guard.py:HARD_LOOP_BACKOFF_START` | 46 | Adjacent to new constants (Commit 3) |
| `guard.py:HARD_LOOP_EXIT_THRESHOLD` | 48 | Reference for docstring |
| `guard.py:HARD_LOOP_HARD_EXIT_THRESHOLD` | 90 | Insert new constants AFTER this |
| `guard.py:_read_hard_exit_threshold` | 68 | Mirror pattern for `_read_min_prune_ratio` |
| `guard.py:start_guard def` | 279 | |
| `guard.py:deferred_exit_announced = False` | 468 | Add `_futile_skip_announced = False` alongside |
| `guard.py:claude_alive = True` | 458 | |
| `guard.py:if not claude_alive:` | 499 | Insert `_safe_unlink_session_pidfile` HERE (before checkpoint_team) |
| `guard.py:checkpoint_team call after not claude_alive` | 501 | Must come AFTER the inserted unlink |
| `guard.py:if result.get("saved_mb", 0) <= 0:` | 616 | Add `or result.get("futile_reload_skipped")` |
| `guard.py:consecutive_empty_hard_prunes += 1` | 617 | Moved into the extended condition |
| `guard.py:deferred_exit_announced = False` (reset) | 740 | Add `_futile_skip_announced = False` reset alongside |
| `guard.py:guard_prune_cycle def` | 801 | |
| `guard.py:saved_bytes <= 0 early return` | 860 | Insert futile-skip return AFTER this block (after line 870) |
| `guard.py:post_te = estimate_session_tokens(...)` | 873 | Futile-skip return goes BEFORE this line |
| `guard.py:_terminate_and_resume def` | 1127 | Add `**_ignored_kwargs` to signature |
| `guard.py:_spawn_reload_watcher call` | 1240 | Sentinel write goes BEFORE this line |
| `guard.py:_spawn_reload_watcher def` | 1243 | |
| `guard.py:watcher_script = (...)` | 1290 | Replace with extended version |
| `guard.py:subprocess.Popen([bash, -c, watcher_script])` | 1297 | Unchanged |
| `guard.py:_safe_unlink_session_pidfile def` | 1375 | Already present (PR #93) |
| `guard.py:start_guard_daemon def` | 1495 | |
| `guard.py:_cleanup_legacy_pid(cwd)` | 1543 | Insert sentinel check AFTER this |
| `guard.py:existing_pid = _is_guard_running_for_session` | 1547 | Sentinel check goes BEFORE this block |
| `reload_lock.py:INIT_OVERFLOW = "overflow"` | 51 | Add new constants AFTER this |
| `reload_lock.py:acquire_with_wait def` | 265 | New sentinel functions go AFTER line 307 (EOF) |
| `spawn_lock.py:INIT_SPAWN_PARENT` | 151 | Confirmed present |
| `spawn_lock.py:INIT_SPAWN_DAEMON` | 152 | Confirmed present |
| `data/hooks.json:SessionStart command` | 9 | Extend the command string |
