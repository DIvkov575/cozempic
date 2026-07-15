"""A/B coding-task harness — SWE-bench-shaped, Docker-free, test-graded.

The trustworthy metric we actually want: does cozempic pruning change an agent's
ability to complete a *context-dependent coding task*, graded by REAL test
execution (not an LLM judge, not substring recall)?

- ``grade_task``: run pytest in a task dir; True iff the target test passes.
- ``run_ab``: for each task, run the agent with cozempic ON and OFF, grade both,
  report resolve-rate per arm. Agent is injected — deterministic in tests.
- ``claude_agent``: the live agent, driving ``claude -p`` in the task dir. Cozempic
  on/off is toggled via the guard's env controls. Opt-in (non-deterministic).

Swap ``grade_task`` for the SWE-bench Docker harness to run the real benchmark;
the A/B structure is identical.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

# agent(task_dir: Path, arm_env: dict) -> None   (edits files in task_dir; arm_env
# is the per-arm environment overlay, e.g. {"COZEMPIC_CHECKPOINT_TOKENS": "0"}).
Agent = Callable[[Path, dict], None]

# Default A/B arms for the changes on this branch. Each maps an arm name to the
# environment overlay applied for that arm. "baseline" is full cozempic (all
# defaults on); "no-checkpoint" disables the fixed 150K early tier via the real,
# honored env var (see guard._checkpoint_threshold_tokens). Extend freely.
DEFAULT_ARMS: dict[str, dict] = {
    "baseline": {},
    "no-checkpoint": {"COZEMPIC_CHECKPOINT_TOKENS": "0"},
}


def _pytest_cmd() -> list[str]:
    """Prefer the `pytest` binary on PATH; fall back to `python -m pytest`.

    `sys.executable -m pytest` fails when the running interpreter lacks pytest
    (common with multiple Pythons) — the binary on PATH is the reliable path.
    """
    exe = shutil.which("pytest")
    return [exe] if exe else [sys.executable, "-m", "pytest"]


def grade_task(task_dir: Path, test_file: str, timeout: float = 120.0) -> bool:
    """True iff `test_file` passes under pytest in `task_dir`."""
    try:
        proc = subprocess.run(
            _pytest_cmd() + [test_file, "-q"],
            cwd=str(task_dir), capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def run_ab(task_dirs: list[Path], agent: Agent, test_file: str,
           arms: dict[str, dict] | None = None) -> dict:
    """Run each task under each arm; report resolve-rate per arm.

    ``arms`` maps arm name → per-arm environment overlay passed to the agent
    (defaults to ``DEFAULT_ARMS`` — baseline vs. no-checkpoint). Each (task, arm)
    runs on the task dir as prepared by the agent. A crashing agent counts as
    unresolved and never aborts the sweep.
    """
    arms = arms or DEFAULT_ARMS
    result = {arm: {"resolved": 0, "total": 0} for arm in arms}
    for task_dir in task_dirs:
        for arm, arm_env in arms.items():
            result[arm]["total"] += 1
            try:
                agent(Path(task_dir), arm_env)
                if grade_task(Path(task_dir), test_file=test_file):
                    result[arm]["resolved"] += 1
            except Exception as e:  # agent failure ≠ harness failure
                print(f"  [swebench] agent crashed on {task_dir} (arm={arm}): {e!r}",
                      file=sys.stderr)
    for arm in arms:
        t = result[arm]["total"]
        result[arm]["resolve_rate"] = result[arm]["resolved"] / t if t else 0.0
    return result


_AGENT_PROMPT = ("Fix the code in this directory so the tests pass. Edit the "
                 "source file(s) in place. Do not edit the test files.")


def claude_agent(task_dir: Path, arm_env: dict, timeout: float = 300.0) -> None:
    """Live agent: run `claude -p` inside the task dir to fix the code.

    ``arm_env`` is the per-arm environment overlay (e.g. the real, honored
    ``COZEMPIC_CHECKPOINT_TOKENS`` to A/B the fixed early tier). Non-deterministic —
    exercised only by the opt-in live test / real sweeps.
    """
    import os
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (arm_env or {}).items()})
    # --dangerously-skip-permissions: the subprocess doesn't inherit an interactive
    # shell alias, so the agent can't write files without it (headless eval only).
    subprocess.run(["claude", "--dangerously-skip-permissions", "-p", _AGENT_PROMPT],
                   cwd=str(task_dir), capture_output=True, text=True,
                   timeout=timeout, env=env)
