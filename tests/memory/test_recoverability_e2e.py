"""End-to-end regression for C1: per-message span-capture must let recoverability fire.

This is the test the original bug missed. The write side used to record ONE whole-span
hash for an N-message span, while recoverability reads per-message hashes — so with N>1
messages (the production case: cmd_nudge passes the whole transcript) nothing ever matched.
Here we record via ledger.record_span (the fixed, per-message write path) over a 3-message
span and assert recoverability marks ALL 3 messages removable.
"""

from cozempic.memory import ledger
from cozempic.strategies.recoverability import strategy_recoverability


def _span():
    return [
        {"role": "user", "content": "always use uv, never pip"},
        {"role": "assistant", "content": "understood, uv only"},
        {"role": "user", "content": "and prefer ruff for linting"},
    ]


def _as_messages(msgs):
    # Message = (line_index, message_dict, byte_size)
    return [(i, m, 100) for i, m in enumerate(msgs)]


def test_multi_message_span_all_recoverable_after_record_span(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    sid = "e2e-sess"
    span = _span()

    # WRITE side (fixed): record per-message capture for the whole 3-message span.
    ledger.record_span(sid, span, "slug")

    # READ side: recoverability over the same 3 messages.
    result = strategy_recoverability(_as_messages(span), {"session_id": sid})

    assert result.messages_removed == 3
    assert {a.line_index for a in result.actions} == {0, 1, 2}
    assert all(a.action == "remove" for a in result.actions)


def test_multi_message_span_none_recoverable_without_record_span(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    # Fresh session, nothing recorded — nothing is recoverable.
    result = strategy_recoverability(_as_messages(_span()), {"session_id": "fresh-sess"})
    assert result.messages_removed == 0
    assert result.actions == []
