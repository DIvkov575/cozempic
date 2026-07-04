"""Compose the end-of-conversation tail block: northstar, todos, directives, stubs.

Placed last (highest-adherence position). Marker-tagged so each prune replaces rather
than appends — idempotent.
"""

from __future__ import annotations

from ..digest import _sanitize_for_injection

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
    assets: list[str] | None = None,
) -> dict:
    lines = [TAIL_MARKER, "# Current focus (regenerated each prune)"]
    if northstar:
        lines += ["", "## Northstar", _sanitize_for_injection(northstar, limit=200)]
    if todos:
        lines += ["", "## Open todos",
                  *[f"- {_sanitize_for_injection(t, limit=200)}" for t in todos]]
    if directives:
        lines += ["", "## Standing directives",
                  *[f"- {_sanitize_for_injection(d, limit=200)}" for d in directives]]
    if stubs:
        lines += ["", "## Relevant memories (use /recall to load)",
                  *[f"- {_sanitize_for_injection(s, limit=200)}" for s in stubs]]
    if assets:
        lines += ["", "## Offloaded assets (recall to load)",
                  *[f"- {_sanitize_for_injection(a, limit=200)}" for a in assets]]
    return {"role": "user", "content": "\n".join(lines)}


def strip_prior_tail(messages: list[dict]) -> list[dict]:
    return [m for m in messages if TAIL_MARKER not in _text_of(m)]


def compose_tail(
    messages: list[dict],
    northstar: str,
    todos: list[str],
    directives: list[str],
    stubs: list[str],
    assets: list[str] | None = None,
) -> list[dict]:
    cleaned = strip_prior_tail(messages)
    return cleaned + [build_tail_message(northstar, todos, directives, stubs, assets)]
