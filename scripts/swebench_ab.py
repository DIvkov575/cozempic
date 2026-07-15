#!/usr/bin/env python3
"""Real SWE-bench A/B: Claude harness with cozempic ON vs OFF, test-graded.

Pipeline (per instance):
  1. clone the repo at the instance's base commit
  2. run `claude -p` to fix the issue (cozempic on, then off)
  3. capture each diff as a SWE-bench prediction
  4. grade both prediction sets with the official harness (runs in Finch/Docker)

PREREQUISITES (Amazon):
  - Finch (toolbox install finch; finch vm init)   ← Amazon's local container tool
  - export DOCKER_HOST=unix:///Applications/Finch/lima/data/finch/sock/finch.sock
  - export DOCKER_CONFIG=$HOME/.finch
  - pip install swebench datasets   (in a venv; Python <=3.12 recommended)

VERIFIED: the official harness runs on Finch — gold-patch eval of
astropy__astropy-12907 returned resolved=True (2026-07-01).

Usage:
  PYTHONPATH=src python scripts/swebench_ab.py --instances astropy__astropy-12907
  # add --live to actually run claude; without it, prints the plan only.
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


def _checkout(instance: dict, dest: Path) -> Path:
    """Clone the instance repo at its base commit into dest/repo."""
    repo_dir = dest / "repo"
    url = f"https://github.com/{instance['repo']}.git"
    subprocess.run(["git", "clone", "-q", url, str(repo_dir)], check=True)
    subprocess.run(["git", "checkout", "-q", instance["base_commit"]],
                   cwd=str(repo_dir), check=True)
    return repo_dir


def run_ab(instance_ids: list[str], dataset: str, live: bool) -> None:
    from datasets import load_dataset
    from cozempic.bench.swebench import DEFAULT_ARMS
    from cozempic.bench.swebench_predict import (
        generate_prediction, claude_repo_agent)

    ds = load_dataset(dataset, split="test")
    by_id = {r["instance_id"]: r for r in ds}

    for arm, arm_env in DEFAULT_ARMS.items():
        preds = []
        for iid in instance_ids:
            inst = by_id[iid]
            if not live:
                print(f"[plan] {arm}: would clone {inst['repo']}@"
                      f"{inst['base_commit'][:8]}, run claude (env={arm_env or 'defaults'})")
                continue
            with tempfile.TemporaryDirectory() as td:
                repo_dir = _checkout(inst, Path(td))
                pred = generate_prediction(
                    iid, repo_dir,
                    lambda rd, env: claude_repo_agent(rd, env, problem=inst["problem_statement"]),
                    arm_env=arm_env, model_name=arm)
                preds.append(pred)
                print(f"[{arm}] {iid}: patch {len(pred['model_patch'])} bytes")
        if live:
            out = REPO / f"preds_{arm}.json"
            out.write_text(json.dumps(preds))
            print(f"[{arm}] wrote {out}")
            print(f"  grade with: python -m swebench.harness.run_evaluation "
                  f"--dataset_name {dataset} --predictions_path {out} "
                  f"--run_id ab_{arm} --max_workers 1")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", nargs="+", required=True)
    ap.add_argument("--dataset", default="SWE-bench/SWE-bench_Lite")
    ap.add_argument("--live", action="store_true", help="actually run claude (slow/costly)")
    args = ap.parse_args(argv)
    run_ab(args.instances, args.dataset, args.live)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
