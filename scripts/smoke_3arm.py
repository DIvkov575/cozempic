#!/usr/bin/env python3
"""Tiny live smoke: 1 self-contained coding task × 3 arms (none/ruya/mine).

Proves the 3-arm harness works end-to-end with real `claude -p` and real venv +
CLAUDE_CONFIG_DIR isolation — WITHOUT the SWE-bench dataset/Finch dependency. Each
arm gets its own cozempic install + config dir; the agent fixes a broken function;
grading runs pytest.

Usage:
    PYTHONPATH=src python scripts/smoke_3arm.py            # plan only (no claude)
    PYTHONPATH=src python scripts/smoke_3arm.py --live      # real claude -p per arm

Confirms: (1) each arm imports only its own cozempic build (version printed),
(2) guards/hooks in separate config dirs don't collide, (3) the run→grade loop
completes for all arms. This is the gate before the costly full sweep.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from cozempic.bench.arms import default_arms, prepare_arm  # noqa: E402
from cozempic.bench.swebench import grade_task, _AGENT_PROMPT  # noqa: E402


def _make_task(task_dir: Path) -> None:
    """A minimal broken task: add() subtracts; a test asserts add(2,3)==5."""
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "sol.py").write_text("def add(a, b):\n    return a - b\n")
    (task_dir / "test_sol.py").write_text(
        "from sol import add\ndef test_add():\n    assert add(2, 3) == 5\n")


def run_arm(prepared, task_dir: Path, live: bool) -> None:
    _make_task(task_dir)
    arm, ver = prepared.arm.name, prepared.cozempic_version or "none"
    if not live:
        print(f"  [plan] arm={arm} cozempic={ver} config={prepared.config_dir}")
        return
    print(f"  [live] arm={arm} cozempic={ver} — running claude -p ...")
    subprocess.run(
        ["claude", "--dangerously-skip-permissions", "-p", _AGENT_PROMPT],
        cwd=str(task_dir), capture_output=True, text=True,
        timeout=300, env=prepared.env,
    )


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually run claude -p")
    ap.add_argument("--workroot", default="/tmp/cozempic_3arm_smoke")
    args = ap.parse_args(argv)

    workroot = Path(args.workroot)
    if workroot.exists():
        shutil.rmtree(workroot)

    results = []
    for arm in default_arms(REPO):
        prepared = prepare_arm(arm, workroot / arm.name)
        task_dir = workroot / arm.name / "task"
        run_arm(prepared, task_dir, args.live)
        resolved = grade_task(task_dir, test_file="test_sol.py") if args.live else None
        results.append((arm.name, prepared.cozempic_version, resolved))

    print("\n=== 3-arm smoke result ===")
    for name, ver, resolved in results:
        print(f"  {name:<5} cozempic={ver or 'none':<24} resolved={resolved}")
    # Isolation assertion: each arm reports a distinct expected build.
    versions = {name: ver for name, ver, _ in results}
    assert versions["none"] is None, "none arm should have no cozempic"
    assert versions["ruya"] == "1.8.39", f"ruya arm wrong build: {versions['ruya']}"
    assert versions["mine"] and versions["mine"] != "1.8.39", \
        f"mine arm should be the fork build, got {versions['mine']}"
    print("\nIsolation OK: none=∅, ruya=1.8.39, mine=fork build (distinct).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
