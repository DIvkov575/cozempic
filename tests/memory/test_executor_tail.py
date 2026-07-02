from cozempic.memory import tail
from cozempic import executor


def test_apply_tail_appends_one_block(monkeypatch):
    monkeypatch.setattr(executor, "_derive_northstar", lambda msgs: "Ship it")
    monkeypatch.setattr(executor, "_derive_todos", lambda msgs: ["do x"])
    monkeypatch.setattr(executor, "_derive_directives", lambda msgs: ["never Y"])
    from cozempic.memory import stubs
    monkeypatch.setattr(stubs, "relevant_stubs", lambda q, k=7: ["p/z.md"])

    msgs = [{"role": "user", "content": "hello"}]
    out = executor.apply_memory_tail(msgs)
    tails = [m for m in out if tail.TAIL_MARKER in tail._text_of(m)]
    assert len(tails) == 1
    assert "Ship it" in tail._text_of(tails[0])


def test_apply_tail_off_switch(monkeypatch):
    monkeypatch.setenv("COZEMPIC_MEMORY_OFF", "1")
    msgs = [{"role": "user", "content": "hello"}]
    assert executor.apply_memory_tail(msgs) == msgs
