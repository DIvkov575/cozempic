"""Tests for the 3-arm build comparison (none / ruya / mine)."""

import os
from pathlib import Path

import pytest

from cozempic.bench.arms import (
    Arm, default_arms, prepare_arm, run_build_ab, RUYA_VERSION,
)


def test_default_arms_shape():
    arms = default_arms(Path("/repo"))
    names = [a.name for a in arms]
    assert names == ["none", "ruya", "mine"]
    none, ruya, mine = arms
    assert none.install is None and none.wire_guard is False
    assert ruya.install == "pypi" and ruya.wire_guard is True
    assert mine.install == "/repo" and mine.wire_guard is True


def test_prepare_none_arm_no_install(tmp_path):
    """The 'none' arm installs nothing and just sets an isolated config dir."""
    prepared = prepare_arm(Arm("none", install=None, wire_guard=False), tmp_path)
    assert prepared.cozempic_version is None
    assert prepared.config_dir.exists()
    assert prepared.env["CLAUDE_CONFIG_DIR"] == str(prepared.config_dir)
    # auto-update/global-init are pinned off so the arm can't drift.
    assert prepared.env["COZEMPIC_NO_AUTO_UPDATE"] == "1"


def test_prepare_arm_applies_env_overlay(tmp_path):
    prepared = prepare_arm(
        Arm("none", install=None, wire_guard=False,
            env={"COZEMPIC_SOME_TEST_VAR": "0"}),
        tmp_path)
    assert prepared.env["COZEMPIC_SOME_TEST_VAR"] == "0"


def test_arms_get_isolated_config_dirs(tmp_path):
    """Each arm's CLAUDE_CONFIG_DIR must be distinct (no guard/hook collision)."""
    a = prepare_arm(Arm("none", install=None, wire_guard=False), tmp_path / "a")
    b = prepare_arm(Arm("none2", install=None, wire_guard=False), tmp_path / "b")
    assert a.config_dir != b.config_dir
    assert a.env["CLAUDE_CONFIG_DIR"] != b.env["CLAUDE_CONFIG_DIR"]


def test_run_build_ab_sweeps_arms_and_grades(tmp_path):
    tasks = ["t1", "t2"]
    # runner "fixes" only under the 'mine' arm; grader passes iff fixed.
    fixed = set()

    def runner(task, prepared):
        if prepared.arm.name == "mine":
            fixed.add(task)

    def grader(task):
        return task in fixed

    arms = [Arm("none", None, False), Arm("mine", None, False)]  # no real installs
    result = run_build_ab(tasks, runner, grader, arms, tmp_path)
    assert result["none"]["resolved"] == 0
    assert result["mine"]["resolved"] == 2
    assert result["mine"]["resolve_rate"] == 1.0
    assert result["none"]["total"] == result["mine"]["total"] == 2


def test_run_build_ab_isolates_runner_crash(tmp_path):
    def runner(task, prepared):
        raise RuntimeError("boom")

    def grader(task):
        return False

    arms = [Arm("none", None, False)]
    result = run_build_ab(["t1", "t2"], runner, grader, arms, tmp_path)
    # both tasks counted, none resolved, sweep completed
    assert result["none"]["total"] == 2
    assert result["none"]["resolved"] == 0


@pytest.mark.skipif(os.environ.get("COZEMPIC_LIVE_INSTALL") != "1",
                    reason="real venv+pip install — set COZEMPIC_LIVE_INSTALL=1")
def test_prepare_ruya_arm_real_install(tmp_path):
    """Opt-in: actually create a venv and pip install upstream cozempic."""
    prepared = prepare_arm(Arm("ruya", install="pypi", wire_guard=False), tmp_path)
    assert prepared.cozempic_version == RUYA_VERSION
    assert Path(prepared.python).exists()
