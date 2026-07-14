from cozempic.memory.insight import Insight, TrustClass


def test_insight_roundtrips_to_dict():
    ins = Insight(
        slug="use-uv-not-pip",
        title="Use uv, not pip",
        description="Project standardizes on uv for installs",
        type="feedback",
        trust_class=TrustClass.USER_DIRECTIVE,
        body="Always run `uv pip install`, never bare `pip`.",
    )
    d = ins.to_dict()
    assert d["slug"] == "use-uv-not-pip"
    assert d["trust_class"] == "user-directive"
    assert Insight.from_dict(d) == ins


def test_trust_class_values():
    assert TrustClass.USER_DIRECTIVE.value == "user-directive"
    assert TrustClass.AGENT_PROVISIONAL.value == "agent-provisional"
    assert TrustClass.WORLD_FACT.value == "world-fact"
