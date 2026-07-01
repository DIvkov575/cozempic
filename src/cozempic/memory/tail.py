"""Compose the end-of-conversation tail block: northstar, todos, directives, stubs.

Placed last (highest-adherence position). Marker-tagged so each prune replaces rather
than appends — idempotent.
"""

from __future__ import annotations

TAIL_MARKER = "__cozempic_northstar_tail__"


def _text_of(msg: dict) -> str:
    c = msg.get("content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(
            b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def build_tail_message(
    northstar: str,
    todos: list[str],
    directives: list[str],
    stubs: list[str],
) -> dict:
    lines = [TAIL_MARKER, "# Current focus (regenerated each prune)"]
    if northstar:
        lines += ["", "## Northstar", northstar]
    if todos:
        lines += ["", "## Open todos", *[f"- {t}" for t in todos]]
    if directives:
        lines += ["", "## Standing directives", *[f"- {d}" for d in directives]]
    if stubs:
        lines += ["", "## Relevant memories (use /recall to load)",
                  *[f"- {s}" for s in stubs]]
    return {"role": "user", "content": "\n".join(lines)}


def strip_prior_tail(messages: list[dict]) -> list[dict]:
    return [m for m in messages if TAIL_MARKER not in _text_of(m)]


def compose_tail(
    messages: list[dict],
    northstar: str,
    todos: list[str],
    directives: list[str],
    stubs: list[str],
) -> list[dict]:
    cleaned = strip_prior_tail(messages)
    return cleaned + [build_tail_message(northstar, todos, directives, stubs)]
