from cozempic.memory import ledger


def test_span_hash_is_stable_and_order_sensitive():
    a = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert ledger.span_hash(a) == ledger.span_hash(list(a))          # stable
    assert ledger.span_hash(a) != ledger.span_hash(list(reversed(a)))  # order matters
    assert len(ledger.span_hash(a)) == 16


def test_record_and_confirm(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    msgs = [{"role": "user", "content": "always use uv"}]
    h = ledger.span_hash(msgs)
    assert ledger.is_captured("sess1", h) is False
    ledger.record("sess1", h, "use-uv-not-pip")
    assert ledger.is_captured("sess1", h) is True
    assert ledger.slug_for("sess1", h) == "use-uv-not-pip"


def test_ledger_isolated_per_session(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    ledger.record("sessA", "deadbeefdeadbeef", "slug-a")
    assert ledger.is_captured("sessB", "deadbeefdeadbeef") is False
