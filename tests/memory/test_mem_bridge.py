"""mem_bridge now persists via the `mymem save` CLI (deterministic, no LLM)."""

import subprocess
import types

from cozempic.memory import mem_bridge
from cozempic.memory.insight import Insight, TrustClass


def _mk_insight(slug="use-uv"):
    return Insight(slug, "Use uv", "std on uv", "feedback",
                   TrustClass.USER_DIRECTIVE, "Always `uv pip install`.")


def _fake_run_ok(recorder):
    """Return a subprocess.run stand-in that records argv and reports success."""
    def _run(cmd, *a, **kw):
        recorder.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return _run


def test_persist_noop_when_not_partitioned(monkeypatch):
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: None)
    assert mem_bridge.persist_insights("sess1", [_mk_insight()]) == []


def test_persist_noop_when_tool_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: tmp_path / "myproj")
    monkeypatch.setattr(mem_bridge, "_mymem_cmd", lambda: None)  # tool not installed
    assert mem_bridge.persist_insights("sess1", [_mk_insight()]) == []


def test_persist_invokes_mymem_save_with_expected_args(tmp_path, monkeypatch):
    part = tmp_path / "myproj"
    part.mkdir()
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: part)
    monkeypatch.setattr(mem_bridge, "_mymem_cmd", lambda: ["python3", "/x/mymem"])
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_run_ok(calls))

    slugs = mem_bridge.persist_insights("sess1", [_mk_insight()])
    assert slugs == ["use-uv"]
    assert len(calls) == 1
    cmd = calls[0]
    # routed through `mymem save` with the resolved partition NAME + insight fields
    assert cmd[:3] == ["python3", "/x/mymem", "save"]
    assert "use-uv" in cmd
    assert cmd[cmd.index("--partition") + 1] == "myproj"
    assert cmd[cmd.index("--type") + 1] == "feedback"
    assert cmd[cmd.index("--evidence") + 1] == "sess1"
    # BOTH network git ops suppressed — persist runs on the prune hot path.
    # --no-pull is the critical one (default pull is a ~590ms network round-trip).
    assert "--no-pull" in cmd
    assert "--no-push" in cmd
    assert "Always `uv pip install`." in cmd[cmd.index("--content") + 1]


def test_persist_skips_failed_saves(tmp_path, monkeypatch):
    part = tmp_path / "myproj"
    part.mkdir()
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: part)
    monkeypatch.setattr(mem_bridge, "_mymem_cmd", lambda: ["python3", "/x/mymem"])

    def _run_fail(cmd, *a, **kw):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
    monkeypatch.setattr(subprocess, "run", _run_fail)

    assert mem_bridge.persist_insights("sess1", [_mk_insight()]) == []


def test_persist_survives_subprocess_error(tmp_path, monkeypatch):
    part = tmp_path / "myproj"
    part.mkdir()
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: part)
    monkeypatch.setattr(mem_bridge, "_mymem_cmd", lambda: ["python3", "/x/mymem"])

    def _run_raise(cmd, *a, **kw):
        raise OSError("no such file")
    monkeypatch.setattr(subprocess, "run", _run_raise)

    # a memory glitch must never abort a prune → returns [], no raise
    assert mem_bridge.persist_insights("sess1", [_mk_insight()]) == []


def test_partition_name_derives_dir_name(tmp_path, monkeypatch):
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: tmp_path / "workplace")
    assert mem_bridge._partition_name() == "workplace"
