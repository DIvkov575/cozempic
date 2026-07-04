from cozempic.memory.distill import distill_thinking, build_distill_prompt


def test_prompt_asks_for_decision_points():
    p = build_distill_prompt("I considered A, rejected B, chose C because fast")
    assert "decision" in p.lower()
    assert "chose C" in p  # source text embedded


def test_distill_returns_text_from_backend():
    out = distill_thinking("reasoning...", backend=lambda _p: "Decision: chose C (fast).")
    assert out == "Decision: chose C (fast)."


def test_distill_empty_on_blank_backend():
    assert distill_thinking("reasoning...", backend=lambda _p: "") is None
    assert distill_thinking("", backend=lambda _p: "anything") is None


def test_worker_distills_and_records_block(tmp_path, monkeypatch):
    from cozempic.memory import schedule, ledger
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    monkeypatch.setattr(schedule, "distill_thinking", lambda t: "Decision: X")
    captured = {}
    monkeypatch.setattr(schedule, "persist_insights", lambda sid, ins: (captured.update(n=len(ins)) or ["decision-slug"]))
    block = {"type": "thinking", "thinking": "z" * 600}
    msg = {"type": "assistant", "message": {"role": "assistant", "content": [block]}}
    schedule._distill_thinking_blocks("s1", [msg])
    assert captured.get("n") == 1
    assert ledger.is_block_captured("s1", block)
