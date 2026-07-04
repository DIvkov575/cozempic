# tests/memory/test_ledger_block.py
from cozempic.memory import ledger


def test_record_block_and_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    block = {"type": "thinking", "thinking": "long reasoning here"}
    assert ledger.is_block_captured("s1", block) is False
    ledger.record_block("s1", block, "decision-slug")
    assert ledger.is_block_captured("s1", block) is True
    assert ledger.slug_for_block("s1", block) == "decision-slug"


def test_block_and_message_namespaces_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    # A block captured for distillation must NOT make a message-hash lookup hit.
    block = {"type": "thinking", "thinking": "x"}
    ledger.record_block("s1", block, "slug-a")
    # recoverability would look up span_hash([msg]); a different dict shape → different hash
    msg = {"role": "assistant", "content": [block]}
    assert ledger.is_captured("s1", ledger.span_hash([msg])) is False
