# tests/memory/test_extract.py
import json
from cozempic.memory.extract import extract_insights, build_prompt
from cozempic.memory.insight import TrustClass


def test_prompt_is_conservative_and_lists_existing():
    p = build_prompt("SPAN", existing_slugs=["old-slug"])
    assert "SPAN" in p
    assert "preserv" in p.lower()          # preserve original wording
    assert "old-slug" in p                  # dedup context
    assert "world-fact" in p                # trust taxonomy present


def test_extract_parses_and_drops_world_facts():
    payload = json.dumps([
        {"slug": "use-uv", "title": "Use uv", "description": "d",
         "type": "feedback", "trust_class": "user-directive", "body": "use `uv`"},
        {"slug": "earth-round", "title": "Earth", "description": "d",
         "type": "reference", "trust_class": "world-fact", "body": "earth is round"},
    ])
    got = extract_insights("span", existing_slugs=[], backend=lambda _p: payload)
    assert [i.slug for i in got] == ["use-uv"]           # world-fact dropped
    assert got[0].trust_class is TrustClass.USER_DIRECTIVE


def test_extract_handles_garbage_backend_output():
    assert extract_insights("span", existing_slugs=[], backend=lambda _p: "not json") == []
    assert extract_insights("span", existing_slugs=[], backend=lambda _p: "") == []
