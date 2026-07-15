"""3-arm build comparison: none / ruya / mine.

The env-overlay A/B in ``swebench.py`` compares configs of ONE installed cozempic
build. This module compares three *different builds* driving the same tasks:

  * ``none``  — plain Claude Code, cozempic NOT wired (no guard, no hooks).
  * ``ruya``  — upstream vanilla, ``pip install cozempic==<RUYA_VERSION>``.
  * ``mine``  — this fork's working tree, ``pip install <repo>``.

Each arm gets its OWN isolated environment so their guards/hooks/state cannot
collide:
  * a dedicated venv (``none`` needs none; ruya/mine each get their own).
  * a dedicated ``CLAUDE_CONFIG_DIR`` — cozempic writes hooks into settings.json
    there, so separate dirs keep one arm's guard from pruning another's session.

An ``Arm`` is a spec; ``prepare_arm`` builds its environment once (venv + config
dir + cozempic init), returning the env dict + interpreter to run the agent under.
``run_build_ab`` sweeps the arms over a set of tasks with a caller-supplied,
injectable runner (fake in tests, ``claude -p`` live) and reports per-arm outcomes.

Nothing here is Docker/Finch-specific; grading is delegated to a caller-supplied
grader (pytest locally, or the official SWE-bench harness on Finch).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Upstream version that represents the "ruya" arm. Pinned so the comparison is
# reproducible; bump deliberately.
RUYA_VERSION = "1.8.39"


@dataclass
class Arm:
    """A build-comparison arm.

    name:        arm label used in results.
    install:     None → don't install cozempic (the 'none' arm). "pypi" → install
                 ``cozempic==RUYA_VERSION``. A path str → ``pip install <path>``.
    wire_guard:  whether to run ``cozempic init`` so the guard/hooks are wired into
                 this arm's CLAUDE_CONFIG_DIR. Always False for the 'none' arm.
    env:         extra env overlay for this arm (e.g. checkpoint tuning).
    """
    name: str
    install: str | None
    wire_guard: bool = True
    env: dict = field(default_factory=dict)


def default_arms(repo_root: Path) -> list[Arm]:
    """The canonical 3 arms: none / ruya / mine."""
    return [
        Arm("none", install=None, wire_guard=False),
        Arm("ruya", install="pypi", wire_guard=True),
        Arm("mine", install=str(repo_root), wire_guard=True),
    ]


@dataclass
class PreparedArm:
    arm: Arm
    env: dict                 # environment to run the agent under
    python: str               # interpreter path for this arm's venv (or sys.executable)
    config_dir: Path
    cozempic_version: str | None   # resolved installed version, or None for 'none'


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _make_venv(venv_dir: Path) -> str:
    """Create a venv and return its python interpreter path."""
    _run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    py = venv_dir / "bin" / "python"
    return str(py if py.exists() else venv_dir / "Scripts" / "python.exe")


def _clean_env(base: dict | None = None) -> dict:
    """Env for arm subprocesses with PYTHONPATH stripped (see prepare_arm)."""
    env = dict(base if base is not None else os.environ)
    env.pop("PYTHONPATH", None)
    return env


def _pip_install(python: str, target: str, env: dict | None = None) -> None:
    _run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
          target], check=True, env=_clean_env(env))


def _cozempic_version(python: str, env: dict | None = None) -> str | None:
    proc = _run([python, "-c", "import cozempic; print(cozempic.__version__)"],
                env=_clean_env(env))
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def prepare_arm(arm: Arm, workdir: Path) -> PreparedArm:
    """Build one arm's isolated environment (venv + config dir + optional init).

    ``workdir`` is a per-arm scratch dir (caller owns cleanup). Returns a
    PreparedArm carrying the env/interpreter the agent should run under.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    config_dir = workdir / "claude_config"
    config_dir.mkdir(exist_ok=True)

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    # CRITICAL: strip PYTHONPATH so a caller running the harness with
    # PYTHONPATH=src (the dev/test setup) cannot leak the working-tree build into
    # an arm's subprocess and shadow whatever that arm's venv installed. Each arm
    # must import ONLY its own installed cozempic, or the whole comparison is void.
    env.pop("PYTHONPATH", None)
    # Never let an arm auto-update or auto-init off our controlled setup.
    env["COZEMPIC_NO_AUTO_UPDATE"] = "1"
    env["COZEMPIC_NO_GLOBAL_INIT"] = "1"
    env.update({k: str(v) for k, v in (arm.env or {}).items()})

    python = sys.executable
    version = None

    if arm.install is not None:
        python = _make_venv(workdir / "venv")
        target = f"cozempic=={RUYA_VERSION}" if arm.install == "pypi" else arm.install
        _pip_install(python, target, env=env)
        version = _cozempic_version(python, env=env)
        if arm.wire_guard:
            # Wire hooks into THIS arm's config dir only. `cozempic init` is
            # idempotent and honors CLAUDE_CONFIG_DIR.
            _run([python, "-m", "cozempic", "init"], env=env)

    return PreparedArm(arm=arm, env=env, python=python, config_dir=config_dir,
                       cozempic_version=version)


# runner(task, prepared) -> None   (drives the agent on `task` under the arm's env)
# grader(task) -> bool             (True iff the task's tests pass)
def run_build_ab(tasks: list, runner, grader,
                 arms: list[Arm], workroot: Path) -> dict:
    """Sweep arms over tasks; report per-arm resolve-rate.

    ``runner(task, prepared_arm)`` drives the agent (injected — fake in tests,
    claude -p live). ``grader(task) -> bool`` decides resolution. Each arm's env
    is prepared ONCE and reused across tasks. A crashing runner counts the task
    unresolved for that arm and never aborts the sweep.
    """
    result = {arm.name: {"resolved": 0, "total": 0, "cozempic_version": None}
              for arm in arms}
    for arm in arms:
        prepared = prepare_arm(arm, workroot / arm.name)
        result[arm.name]["cozempic_version"] = prepared.cozempic_version
        for task in tasks:
            result[arm.name]["total"] += 1
            try:
                runner(task, prepared)
                if grader(task):
                    result[arm.name]["resolved"] += 1
            except Exception as e:  # runner failure ≠ harness failure
                print(f"  [arms] runner crashed on {task} (arm={arm.name}): {e!r}",
                      file=sys.stderr)
    for arm in arms:
        t = result[arm.name]["total"]
        result[arm.name]["resolve_rate"] = (
            result[arm.name]["resolved"] / t if t else 0.0)
    return result
