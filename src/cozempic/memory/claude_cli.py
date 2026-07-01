"""Default extraction backend: the `claude -p` CLI. Pluggable — extract.py accepts
any callable `str -> str`, so a different model or a stub can replace this."""

from __future__ import annotations

import subprocess

_TIMEOUT_S = 120


def _strip_fences(text: str) -> str:
    """Remove a surrounding ```json ... ``` (or bare ```) fence if present."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def run_claude(prompt: str) -> str:
    """Run `claude -p <prompt>`; return de-fenced stdout, or "" on any failure."""
    try:
        cp = subprocess.run(
            ["claude", "-p", prompt],
            input="",
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if cp.returncode != 0:
        return ""
    return _strip_fences(cp.stdout)
