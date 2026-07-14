from cozempic.memory import stubs


def test_parse_query_rows_filters_low_scores():
    raw = "0.81  workplace/use-uv.md\n0.12  biostat/foo.md\n0.55  workplace/bar.md\n"
    rows = stubs._parse_rows(raw, min_score=0.4)
    assert rows == [("workplace/use-uv.md", 0.81), ("workplace/bar.md", 0.55)]


def test_relevant_stubs_top_k(monkeypatch):
    raw = "\n".join(f"0.9{i}  p/f{i}.md" for i in range(10))
    monkeypatch.setattr(stubs, "_query", lambda q: raw)
    out = stubs.relevant_stubs("anything", k=3)
    assert len(out) == 3
    assert all("p/f" in s for s in out)


def test_relevant_stubs_empty_on_no_backend(monkeypatch):
    monkeypatch.setattr(stubs, "_query", lambda q: "")
    assert stubs.relevant_stubs("q", k=5) == []
