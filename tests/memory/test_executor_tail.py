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


def test_derivations_read_nested_message_shape():
    # Real Claude Code transcript records nest role/content under "message" and
    # carry the kind in top-level "type". Derivations MUST read that shape — a
    # plain top-level content read would always yield empty (defeating the feature).
    msgs = [
        {"type": "user",
         "message": {"role": "user",
                     "content": "Build the memory overhaul feature end to end"}},
        {"type": "assistant",
         "message": {"role": "assistant",
                     "content": [
                         {"type": "tool_use", "name": "TodoWrite",
                          "input": {"todos": [
                              {"content": "wire scheduler", "status": "pending"},
                              {"content": "done thing", "status": "completed"},
                          ]}},
                     ]}},
    ]
    northstar = executor._derive_northstar(msgs)
    assert northstar
    assert "memory overhaul" in northstar

    todos = executor._derive_todos(msgs)
    assert todos == ["wire scheduler"]
