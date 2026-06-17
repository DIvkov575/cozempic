"""Prune-value dashboard — D2+ of the dashboard build path.

``aggregate`` turns the local receipt log (see :mod:`cozempic.receipts`) into the
derived views the dashboard renders. Agent-agnostic: it reads the receipt schema,
never any agent internals, so Codex sessions appear automatically once a Codex
adapter emits receipts.
"""

from .aggregate import aggregate, load_receipts
from .lifetime import load_lifetime
from .render import dashboard_path, render_dashboard, render_html, write_dashboard

__all__ = [
    "aggregate",
    "load_receipts",
    "load_lifetime",
    "render_html",
    "render_dashboard",
    "write_dashboard",
    "dashboard_path",
]
