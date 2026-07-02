"""One-shot migration: existing behavioral-digest active rules -> user-directive memories."""

from __future__ import annotations

import re

from ..digest import load_digest_store
from .insight import Insight, TrustClass
from .ledger import span_hash
from .mem_bridge import persist_insights


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:60] or "rule").rstrip("-")


def _rule_to_insight(rule_id: str, rule_text: str) -> Insight:
    return Insight(
        slug=_slugify(rule_text),
        title=rule_text[:60],
        description=rule_text[:120],
        type="feedback",
        trust_class=TrustClass.USER_DIRECTIVE,
        body=rule_text,
    )


def migrate_digest_rules(session_id: str) -> int:
    """Persist all active digest rules as memories. Returns count persisted."""
    store = load_digest_store()
    rules = store.active_rules()
    if not rules:
        return 0
    items = []
    for r in rules:
        ins = _rule_to_insight(r.id, r.rule)
        items.append((ins, span_hash([{"migrated_rule": r.id, "text": r.rule}])))
    written = persist_insights(session_id, items)
    return len(written)
