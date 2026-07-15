#!/usr/bin/env python3
"""Full SWE-bench 3-arm sweep: none / ruya / mine, test-graded on Finch.

For each SWE-bench instance, run `claude -p` to fix the issue under each of three
isolated cozempic BUILDS, capture the diff as a prediction, and emit per-arm
prediction files for the official harness to grade in Finch/Docker.

  none — plain Claude Code, cozempic NOT wired
  ruya — upstream cozempic==1.8.39
  mine — this fork's build (150K checkpoint + memory-overhaul + tail removal)

PREREQUISITES:
  - Finch running:  finch vm start
      export DOCKER_HOST=unix:///Applications/Finch/lima/data/finch/sock/finch.sock
      export DOCKER_CONFIG=$HOME/.finch
  - grading venv with `pip install swebench datasets`

Usage:
  PYTHONPATH=src python scripts/swebench_3arm.py --instances <id> [<id> ...]
      # plan only — prepares arms, prints what it would run
  PYTHONPATH=src python scripts/swebench_3arm.py --instances <id> ... --live
      # actually run claude -p per (instance, arm); writes preds_<arm>.json
  # then grade each arm with the official harness (see printed commands).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from cozempic.bench.arms import default_arms, prepare_arm  # noqa: E402
from cozempic.bench.swebench_predict import capture_diff, make_prediction  # noqa: E402

_PROMPT = (
    "This repository has a bug described in the issue below. Edit the source "
    "files in place to fix it so the project's tests pass. Do not edit tests.\n\n"
    "ISSUE:\n{problem}"
)


def _checkout(instance: dict, dest: Path) -> Path:
    repo_dir = dest / "repo"
    url = f"https://github.com/{instance['repo']}.git"
    subprocess.run(["git", "clone", "-q", url, str(repo_dir)], check=True)
    subprocess.run(["git", "checkout", "-q", instance["base_commit"]],
                   cwd=str(repo_dir), check=True)
    return repo_dir


def _run_agent(repo_dir: Path, prepared, problem: str, timeout: float = 900.0) -> None:
    """Drive claude -p in the checkout under the arm's isolated env."""
    subprocess.run(
        ["claude", "--dangerously-skip-permissions", "-p", _PROMPT.format(problem=problem)],
        cwd=str(repo_dir), capture_output=True, text=True,
        timeout=timeout, env=prepared.env,
    )


def run_sweep(instance_ids: list[str], dataset: str, live: bool,
              workroot: Path) -> None:
    from datasets import load_dataset

    ds = load_dataset(dataset, split="test")
    by_id = {r["instance_id"]: r for r in ds}
    missing = [i for i in instance_ids if i not in by_id]
    if missing:
        print(f"WARNING: instances not in {dataset}: {missing}", file=sys.stderr)

    arms = default_arms(REPO)
    # Prepare each arm's isolated env ONCE (venv + config dir + cozempic init).
    prepared_arms = {}
    for arm in arms:
        p = prepare_arm(arm, workroot / arm.name)
        prepared_arms[arm.name] = p
        print(f"[arm] {arm.name}: cozempic={p.cozempic_version or 'none'} "
              f"config={p.config_dir}")

    from cozempic.bench.jitter import probe_peak_tokens
    usage_rows = []  # (arm, iid, peak_tokens)
    for arm in arms:
        prepared = prepared_arms[arm.name]
        preds = []
        for iid in instance_ids:
            if iid not in by_id:
                continue
            inst = by_id[iid]
            if not live:
                print(f"[plan] {arm.name}: clone {inst['repo']}@{inst['base_commit'][:8]}, "
                      f"run claude (cozempic={prepared.cozempic_version or 'none'})")
                continue
            with tempfile.TemporaryDirectory() as td:
                repo_dir = _checkout(inst, Path(td))
                _run_agent(repo_dir, prepared, inst["problem_statement"])
                patch = capture_diff(repo_dir)
                preds.append(make_prediction(iid, arm.name, patch))
                # peak context the agent accumulated under this arm — shows whether
                # the task stressed context and whether cozempic held it lower.
                peak = probe_peak_tokens(prepared.config_dir)
                usage_rows.append((arm.name, iid, peak))
                print(f"[{arm.name}] {iid}: patch {len(patch)} bytes  "
                      f"peak_ctx={peak:,} tok" if peak else
                      f"[{arm.name}] {iid}: patch {len(patch)} bytes  peak_ctx=n/a")
        if live:
            out = REPO / f"preds_{arm.name}.json"
            out.write_text(json.dumps(preds))
            print(f"[{arm.name}] wrote {out} ({len(preds)} preds)")

    if live and usage_rows:
        print("\n=== peak context per (arm, instance) — did tasks stress context? ===")
        for arm_name, iid, peak in usage_rows:
            print(f"  {arm_name:<5} {iid:<40} {peak:,} tok" if peak
                  else f"  {arm_name:<5} {iid:<40} n/a")

    if live:
        print("\n=== grade each arm (in the grading venv, Finch running) ===")
        for arm in arms:
            print(f"  python -m swebench.harness.run_evaluation "
                  f"--dataset_name {dataset} --predictions_path preds_{arm.name}.json "
                  f"--run_id 3arm_{arm.name} --max_workers 2")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", nargs="+", required=True)
    ap.add_argument("--dataset", default="SWE-bench/SWE-bench_Lite")
    ap.add_argument("--live", action="store_true", help="actually run claude (slow/costly)")
    ap.add_argument("--workroot", default="/tmp/cozempic_3arm_sweep")
    args = ap.parse_args(argv)
    run_sweep(args.instances, args.dataset, args.live, Path(args.workroot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
