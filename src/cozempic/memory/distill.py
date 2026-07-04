"""F7 worker half — distill a thinking block into its decision points (LLM, pluggable)."""

from __future__ import annotations

from typing import Callable

from .claude_cli import run_claude

Backend = Callable[[str], str]

_PROMPT = """\
Below is a model's internal reasoning (thinking) block. Extract ONLY the decision points —
the conclusions and choices it actually reached (what was decided, chosen, or ruled out and
why), in at most a few terse lines. Preserve the wording of each decision; do not add
commentary or restate the deliberation. Output plain text, no preamble.

THINKING:
{text}
"""


def build_distill_prompt(text: str) -> str:
    return _PROMPT.format(text=text)


def distill_thinking(text: str, backend: Backend = run_claude) -> str | None:
    """Return decision-point text, or None if input/backend is empty."""
    if not text.strip():
        return None
    out = backend(build_distill_prompt(text)).strip()
    return out or None
