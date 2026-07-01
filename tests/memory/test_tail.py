from cozempic.memory import tail


def test_build_tail_message_has_marker_and_sections():
    msg = tail.build_tail_message(
        northstar="Ship the memory overhaul",
        todos=["wire scheduler", "retire digest"],
        directives=["never auto-commit mymemories"],
        stubs=["workplace/use-uv.md"],
    )
    text = tail._text_of(msg)
    assert tail.TAIL_MARKER in text
    assert "Ship the memory overhaul" in text
    assert "wire scheduler" in text
    assert "never auto-commit mymemories" in text
    assert "workplace/use-uv.md" in text
    assert msg["role"] == "user"


def test_strip_prior_tail_removes_only_marked():
    keep = {"role": "user", "content": "real message"}
    old = tail.build_tail_message("goal", [], [], [])
    result = tail.strip_prior_tail([keep, old])
    assert result == [keep]


def test_compose_is_idempotent():
    base = [{"role": "user", "content": "hi"}]
    once = tail.compose_tail(base, northstar="G", todos=[], directives=[], stubs=[])
    twice = tail.compose_tail(once, northstar="G", todos=[], directives=[], stubs=[])
    # exactly one tail block after either 1 or 2 composes
    assert sum(1 for m in once if tail.TAIL_MARKER in tail._text_of(m)) == 1
    assert sum(1 for m in twice if tail.TAIL_MARKER in tail._text_of(m)) == 1
    assert len(once) == len(twice)
