import json
from cozempic.memory import claude_cli


def test_strip_fences_plain():
    assert claude_cli._strip_fences('{"a": 1}') == '{"a": 1}'


def test_strip_fences_json_block():
    raw = '```json\n{"a": 1}\n```'
    assert json.loads(claude_cli._strip_fences(raw)) == {"a": 1}


def test_run_claude_invokes_cli(monkeypatch):
    calls = {}

    class _CP:
        stdout = '```json\n[]\n```'
        returncode = 0

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["input"] = kw.get("input")
        return _CP()

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    out = claude_cli.run_claude("PROMPT-TEXT")
    assert calls["cmd"][0] == "claude"
    assert "-p" in calls["cmd"]
    assert out == "[]"


def test_run_claude_returns_empty_on_failure(monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("claude not installed")

    monkeypatch.setattr(claude_cli.subprocess, "run", boom)
    assert claude_cli.run_claude("x") == ""
