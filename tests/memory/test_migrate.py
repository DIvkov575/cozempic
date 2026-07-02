from cozempic.memory import migrate
from cozempic.memory.insight import TrustClass


def test_rule_becomes_user_directive_insight():
    ins = migrate._rule_to_insight("r1", "Never force-push to main")
    assert ins.trust_class is TrustClass.USER_DIRECTIVE
    assert ins.type == "feedback"
    assert "force-push" in ins.body
    assert ins.slug and " " not in ins.slug     # kebab slug


def test_migrate_persists_all_active_rules(monkeypatch):
    class _Rule:
        def __init__(self, rid, rule):
            self.id, self.rule = rid, rule

    class _Store:
        def active_rules(self):
            return [_Rule("r1", "Never force-push"), _Rule("r2", "Prefer uv")]

    monkeypatch.setattr(migrate, "load_digest_store", lambda: _Store())
    persisted = {}
    monkeypatch.setattr(migrate, "persist_insights",
                        lambda sid, items: persisted.update(n=len(items)) or [i.slug for i, _ in items])
    n = migrate.migrate_digest_rules("migration")
    assert n == 2
    assert persisted["n"] == 2


def test_migrate_count_reflects_unpartitioned(monkeypatch):
    class _Rule:
        def __init__(self, rid, rule):
            self.id, self.rule = rid, rule

    class _Store:
        def active_rules(self):
            return [_Rule("r1", "Never force-push"), _Rule("r2", "Prefer uv")]

    monkeypatch.setattr(migrate, "load_digest_store", lambda: _Store())
    # Simulate unpartitioned project: persist_insights writes nothing.
    monkeypatch.setattr(migrate, "persist_insights", lambda sid, items: [])
    assert migrate.migrate_digest_rules("x") == 0
