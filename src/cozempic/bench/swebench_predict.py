"""Generate SWE-bench predictions with an agent (cozempic on/off) → A/B.

Flow per instance: check out the repo at its base commit, run an agent to edit
it, capture `git diff` as the SWE-bench `model_patch`. Feed the resulting
predictions to the official harness (run in Finch) for real test-graded scoring.

The agent is injected — deterministic in tests; `claude_repo_agent` is the live
`claude -p` driver with cozempic toggled via env.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

# agent(repo_dir: Path, arm_env: dict) -> None
RepoAgent = Callable[[Path, dict], None]


def capture_diff(repo_dir: Path) -> str:
    """Return `git diff` of tracked changes in the repo (the candidate patch)."""
    proc = subprocess.run(["git", "diff"], cwd=str(repo_dir),
                          capture_output=True, text=True)
    return proc.stdout


def make_prediction(instance_id: str, model_name: str, patch: str) -> dict:
    return {
        "instance_id": instance_id,
        "model_name_or_path": model_name,
        "model_patch": patch,
    }


def generate_prediction(instance_id: str, repo_dir: Path, agent: RepoAgent,
                        arm_env: dict, model_name: str) -> dict:
    """Run the agent on the checkout and capture its diff as a prediction."""
    agent(Path(repo_dir), arm_env)
    patch = capture_diff(Path(repo_dir))
    return make_prediction(instance_id, model_name, patch)


_AGENT_PROMPT = (
    "This repository has a bug described in the issue below. Edit the source "
    "files in place to fix it so the project's tests pass. Do not edit tests.\n\n"
    "ISSUE:\n{problem}"
)


def claude_repo_agent(repo_dir: Path, arm_env: dict, problem: str,
                      timeout: float = 600.0) -> None:
    """Live agent: run `claude -p` in the repo checkout to fix the issue.

    ``arm_env`` is the per-arm environment overlay. Non-deterministic —
    used only in real sweeps, not unit tests.
    """
    import os
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (arm_env or {}).items()})
    subprocess.run(
        ["claude", "--dangerously-skip-permissions", "-p",
         _AGENT_PROMPT.format(problem=problem)],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=timeout, env=env,
    )
