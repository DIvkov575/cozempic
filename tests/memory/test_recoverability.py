from cozempic.strategies import recoverability
from cozempic.memory import ledger


def _msg(idx, text):
    d = {"role": "user", "content": text}
    import json
    return (idx, d, len(json.dumps(d)))


def test_removes_only_captured_spans(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    m0 = _msg(0, "captured fact")
    m1 = _msg(1, "uncaptured fact")
    ledger.record("s1", ledger.span_hash([m0[1]]), "some-slug")

    result = recoverability.strategy_recoverability(
        [m0, m1], {"session_id": "s1"}
    )
    removed_idx = [a.line_index for a in result.actions if a.action == "remove"]
    assert removed_idx == [0]
    assert result.messages_removed == 1


def test_noop_without_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    result = recoverability.strategy_recoverability([_msg(0, "x")], {})
    assert result.actions == []


def test_skips_protected_even_if_captured(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    # A protected message: is_protected() returns True for isVisibleInTranscriptOnly.
    d = {"role": "user", "content": "protected but captured", "isVisibleInTranscriptOnly": True}
    import json
    m0 = (0, d, len(json.dumps(d)))
    # Record its span as captured — normally this would make it removable.
    ledger.record("s1", ledger.span_hash([d]), "some-slug")

    result = recoverability.strategy_recoverability([m0], {"session_id": "s1"})
    assert result.actions == []
    assert result.messages_removed == 0
