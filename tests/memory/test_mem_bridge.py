from pathlib import Path
from cozempic.memory import mem_bridge
from cozempic.memory.insight import Insight, TrustClass


def _mk_insight(slug="use-uv"):
    return Insight(slug, "Use uv", "std on uv", "feedback",
                   TrustClass.USER_DIRECTIVE, "Always `uv pip install`.")


def test_write_fact_file_follows_format(tmp_path):
    part = tmp_path / "myproj"
    part.mkdir()
    mem_bridge._write_fact_file(part, _mk_insight())
    text = (part / "use-uv.md").read_text()
    assert text.startswith("---")
    assert "name: use-uv" in text
    assert "type: feedback" in text
    assert "Always `uv pip install`." in text


def test_append_memory_index_line(tmp_path):
    part = tmp_path / "myproj"
    part.mkdir()
    (part / "MEMORY.md").write_text("# Memories\n")
    mem_bridge._append_index_line(part, _mk_insight())
    assert "- [Use uv](use-uv.md) — std on uv" in (part / "MEMORY.md").read_text()


def test_persist_noop_when_not_partitioned(tmp_path, monkeypatch):
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: None)
    got = mem_bridge.persist_insights("sess1", [_mk_insight()])
    assert got == []


def test_persist_writes_fact_file(tmp_path, monkeypatch):
    part = tmp_path / "myproj"
    part.mkdir()
    (part / "MEMORY.md").write_text("# Memories\n")
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: part)
    monkeypatch.setattr(mem_bridge, "_reindex", lambda: None)   # no embed.py in test

    slugs = mem_bridge.persist_insights("sess1", [_mk_insight()])
    assert slugs == ["use-uv"]
    assert (part / "use-uv.md").exists()
    # persist no longer records the ledger — that is ledger.record_span's job.
