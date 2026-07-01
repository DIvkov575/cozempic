"""Find memories relevant to a session via the offline embedding index, as compact stubs.

Bodies never enter the window here — only `partition/slug` pointers. Full text is pulled
on demand by the user via /recall.
"""

from __future__ import annotations

import subprocess

from ..digest import _sanitize_for_injection
from .mem_bridge import TOOL_DIR

_MIN_SCORE = 0.4


def _query(text: str) -> str:
    embed = TOOL_DIR / "embed.py"
    if not embed.exists():
        return ""
    try:
        cp = subprocess.run(
            ["python3", str(embed), "query", text],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return cp.stdout if cp.returncode == 0 else ""


def _parse_rows(raw: str, min_score: float = _MIN_SCORE) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            score = float(parts[0])
        except ValueError:
            continue
        if score >= min_score:
            rows.append((parts[1], score))
    return rows


def relevant_stubs(query: str, k: int = 7) -> list[str]:
    """Return up to k sanitized `partition/slug` stub strings for the query."""
    rows = _parse_rows(_query(query))
    rows.sort(key=lambda r: r[1], reverse=True)
    return [_sanitize_for_injection(path, limit=200) for path, _ in rows[:k]]
