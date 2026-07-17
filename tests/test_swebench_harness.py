"""A/B coding-task harness: run an agent on a task, grade by real test execution.

SWE-bench-shaped but Docker-free: a task is a working dir with a failing test; the
agent edits code; grading runs pytest and checks the target test passes. The agent
is INJECTED (fake in tests, `claude -p` live), and cozempic on/off is the A/B knob.
Deterministic pieces (grade, aggregate) are unit-tested; the live agent is opt-in.
"""

from __future__ import annotations

import os
import textwrap

import pytest


def _make_task(tmp_path):
    """A minimal task: add() is broken; a test asserts add(2,3)==5."""
    (tmp_path / "sol.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "test_sol.py").write_text(
        "from sol import add\ndef test_add():\n    assert add(2, 3) == 5\n")
    return tmp_path


def test_grade_fails_on_broken_code(tmp_path):
    from cozempic.bench.swebench import grade_task
    _make_task(tmp_path)
    assert grade_task(tmp_path, test_file="test_sol.py") is False


def test_grade_passes_after_fix(tmp_path):
    from cozempic.bench.swebench import grade_task
    _make_task(tmp_path)
    (tmp_path / "sol.py").write_text("def add(a, b):\n    return a + b\n")
    assert grade_task(tmp_path, test_file="test_sol.py") is True


def test_run_ab_reports_resolve_rate_per_arm(tmp_path):
    from cozempic.bench.swebench import run_ab

    # Fake agent that "fixes" the file only under the no-guard arm (to prove
    # the harness distinguishes arms by their env overlay), writing a correct sol.py.
    def fake_agent(task_dir, arm_env):
        _make_task(task_dir)
        if arm_env.get("COZEMPIC_NO_AUTO_INIT") == "1":
            (task_dir / "sol.py").write_text("def add(a,b):\n    return a+b\n")

    result = run_ab([tmp_path], agent=fake_agent, test_file="test_sol.py")
    assert result["baseline"]["resolved"] == 0
    assert result["no-guard"]["resolved"] == 1
    assert result["baseline"]["total"] == result["no-guard"]["total"] == 1


def test_run_ab_custom_arms(tmp_path):
    from cozempic.bench.swebench import run_ab

    def fake_agent(task_dir, arm_env):
        _make_task(task_dir)
        if arm_env.get("FIX") == "1":
            (task_dir / "sol.py").write_text("def add(a,b):\n    return a+b\n")

    result = run_ab([tmp_path], agent=fake_agent, test_file="test_sol.py",
                    arms={"broken": {}, "fixed": {"FIX": "1"}})
    assert result["broken"]["resolved"] == 0
    assert result["fixed"]["resolved"] == 1


def test_run_ab_handles_agent_crash(tmp_path):
    from cozempic.bench.swebench import run_ab

    def crashing_agent(task_dir, arm_env):
        _make_task(task_dir)
        raise RuntimeError("agent blew up")

    # A crashed agent counts as unresolved, never aborts the sweep.
    result = run_ab([tmp_path], agent=crashing_agent, test_file="test_sol.py")
    assert all(v["resolved"] == 0 for v in result.values())


@pytest.mark.skipif(os.environ.get("COZEMPIC_LIVE_LLM") != "1",
                    reason="live claude -p agent run — set COZEMPIC_LIVE_LLM=1")
def test_live_claude_agent_fixes_trivial_bug(tmp_path):
    from cozempic.bench.swebench import grade_task, claude_agent
    _make_task(tmp_path)
    claude_agent(tmp_path, arm_env={})
    assert grade_task(tmp_path, test_file="test_sol.py") is True
