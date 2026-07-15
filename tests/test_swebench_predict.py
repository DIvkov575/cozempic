"""Prediction generation for the real SWE-bench A/B.

An agent edits a repo checkout; we capture `git diff` as a SWE-bench prediction
(model_patch) that the official harness grades in Finch/Docker. Deterministic
pieces (prediction dict shape, diff capture) are unit-tested; the live claude
agent is opt-in.
"""

from __future__ import annotations

import subprocess


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _make_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "m.py").write_text("def f():\n    return 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def test_capture_diff_empty_when_no_change(tmp_path):
    from cozempic.bench.swebench_predict import capture_diff
    _make_repo(tmp_path)
    assert capture_diff(tmp_path) == ""


def test_capture_diff_after_edit(tmp_path):
    from cozempic.bench.swebench_predict import capture_diff
    _make_repo(tmp_path)
    (tmp_path / "m.py").write_text("def f():\n    return 2\n")
    diff = capture_diff(tmp_path)
    assert "return 2" in diff and "m.py" in diff


def test_make_prediction_shape():
    from cozempic.bench.swebench_predict import make_prediction
    pred = make_prediction("astropy__astropy-1", "cozempic-on", "DIFFTEXT")
    assert pred == {
        "instance_id": "astropy__astropy-1",
        "model_name_or_path": "cozempic-on",
        "model_patch": "DIFFTEXT",
    }


def test_generate_prediction_uses_injected_agent(tmp_path):
    from cozempic.bench.swebench_predict import generate_prediction
    _make_repo(tmp_path)

    def fake_agent(repo_dir, arm_env):
        (repo_dir / "m.py").write_text("def f():\n    return 42\n")

    pred = generate_prediction("inst-1", tmp_path, fake_agent, arm_env={},
                               model_name="baseline")
    assert pred["instance_id"] == "inst-1"
    assert "return 42" in pred["model_patch"]
    assert pred["model_name_or_path"] == "baseline"
