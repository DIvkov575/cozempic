import cozempic.cli as cli
from cozempic.memory import schedule


def test_nudge_fires_consolidation(monkeypatch):
    calls = {}
    monkeypatch.setattr(schedule, "maybe_consolidate",
                        lambda sid, msgs, fraction: calls.update(sid=sid, f=fraction) or True)
    cli._maybe_memory_consolidate("sess-xyz", [{"role": "user", "content": "x"}], 0.5)
    assert calls == {"sid": "sess-xyz", "f": 0.5}


def test_nudge_consolidation_off_switch(monkeypatch):
    monkeypatch.setenv("COZEMPIC_MEMORY_OFF", "1")
    called = []
    monkeypatch.setattr(schedule, "maybe_consolidate",
                        lambda *a, **k: called.append(1))
    cli._maybe_memory_consolidate("s", [{"role": "user", "content": "x"}], 0.5)
    assert called == []


def test_treat_threads_session_id_into_prune_config(monkeypatch, tmp_path):
    """Part B: cmd_treat must pass a config containing the session_id into
    run_prescription so the recoverability strategy is not inert."""
    import argparse

    session_path = tmp_path / "abc123.jsonl"
    session_path.write_text(
        '{"type":"user","message":{"content":"hi"},"uuid":"a","sessionId":"abc123"}\n'
    )

    captured = {}

    def fake_run_prescription(messages, strategy_names, config, *a, **k):
        captured["config"] = config
        return messages, []

    monkeypatch.setattr(cli, "resolve_session", lambda *a, **k: session_path)
    monkeypatch.setattr(cli, "run_prescription", fake_run_prescription)

    args = argparse.Namespace(
        session=str(session_path),
        project=None,
        rx="standard",
        execute=False,
        thinking_mode=None,
        protect_pattern=None,
    )
    cli.cmd_treat(args)

    assert captured["config"].get("session_id") == "abc123"
