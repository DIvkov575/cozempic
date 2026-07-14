# src/cozempic/memory/extract.py
"""Turn a message span into atomic, source-trust-tagged Insights.

Pluggable: `backend` is any `str -> str` (default: claude_cli.run_claude).
Conservative: the prompt demands original wording be preserved, not paraphrased.
"""

from __future__ import annotations

import json
from typing import Callable

from .claude_cli import run_claude
from .insight import Insight, TrustClass

Backend = Callable[[str], str]

_PROMPT_TEMPLATE = """\
Decompose the SESSION SPAN below into atomic, standalone insights — one fact per unit.
PRESERVE the original wording of each fact; quote/lift it, do NOT paraphrase into a lossy
summary. Compress only where clearly safe.

Classify each insight's trust_class:
- "user-directive": user intent / preference / correction. Keep verbatim.
- "agent-provisional": a model-generated claim. Include ONLY if corroborated by a tool
  result, an artifact, or explicit user confirmation in the span.
- "world-fact": a factual assertion. Include it, but the caller will drop user-only ones.

Do NOT emit an insight whose slug is in ALREADY-KNOWN (dedup): {existing}

Return ONLY a JSON array; each element:
{{"slug": "kebab-case", "title": "...", "description": "one dense line",
  "type": "user|feedback|project|reference", "trust_class": "...", "body": "..."}}

SESSION SPAN:
{span}
"""


def build_prompt(span_text: str, existing_slugs: list[str]) -> str:
    existing = ", ".join(existing_slugs) if existing_slugs else "(none)"
    return _PROMPT_TEMPLATE.format(existing=existing, span=span_text)


def extract_insights(
    span_text: str,
    existing_slugs: list[str],
    backend: Backend = run_claude,
) -> list[Insight]:
    raw = backend(build_prompt(span_text, existing_slugs))
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    out: list[Insight] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            tc = TrustClass(item["trust_class"])
        except (KeyError, ValueError):
            continue
        if tc is TrustClass.WORLD_FACT:
            continue  # never persist a world-fact through this path
        try:
            out.append(Insight(
                slug=str(item["slug"]),
                title=str(item["title"]),
                description=str(item["description"]),
                type=str(item["type"]),
                trust_class=tc,
                body=str(item["body"]),
            ))
        except KeyError:
            continue
    return out
