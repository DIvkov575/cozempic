"""Static-HTML dashboard renderer — D3 of the dashboard build path.

Turns the D2 aggregate() views into ONE self-contained HTML document:
  * zero runtime deps, no external URLs/CDNs (works offline, honors zero-dep);
  * server-side rendered — charts are CSS bars + inline SVG sparklines, so it
    needs no client JavaScript to display;
  * every dynamic value HTML-escaped (defense-in-depth, though data is local +
    hashed + capped at the contract boundary).

``render_html`` is pure (views in, HTML string out). ``write_dashboard`` does the
I/O (atomic write) and is what the D4 ``cozempic dashboard`` command calls.
"""

from __future__ import annotations

import html
import math
import os
import tempfile
from pathlib import Path

from .._constants import _MAX_RECEIPT_INT

DEFAULT_FILENAME = "dashboard.html"


# --------------------------------------------------------------------------- #
# Formatting helpers (local — keep the renderer dependency-free)              #
# --------------------------------------------------------------------------- #
def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _is_finite_num(v) -> bool:
    """True iff v is a non-bool finite int or float — safe for :.Nf formatting."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _fmt_int(n) -> str:
    """Format an integer with thousands separators.

    Guards NaN/inf (ValueError/OverflowError on int()) and huge ints
    (clamp to _MAX_RECEIPT_INT so f-string stays finite).
    """
    if not isinstance(n, (int, float)) or isinstance(n, bool):
        return "0"
    try:
        v = int(n)
    except (ValueError, OverflowError):
        return "0"
    # Bound magnitude (display length) but PRESERVE sign, consistent with
    # _fmt_tokens/_fmt_bytes which keep the sign of in-range values.
    v = max(-_MAX_RECEIPT_INT, min(v, _MAX_RECEIPT_INT))
    return f"{v:,}"


def _fmt_tokens(n) -> str:
    """Format a token count as "1.2M" / "1.2K" / raw integer string.

    Clamps magnitude to _MAX_RECEIPT_INT before float division to prevent
    OverflowError on corrupt huge-int values.  Sign is preserved for
    in-range values (negative reclaimed is unusual but must not crash).
    """
    if not isinstance(n, (int, float)) or isinstance(n, bool):
        return "0"
    try:
        v = int(n)
    except (ValueError, OverflowError):
        return "0"
    sign = -1 if v < 0 else 1
    mag = min(abs(v), _MAX_RECEIPT_INT)
    if mag >= 999_950:  # rolls 999,999 up to "1.0M" rather than "1000.0K"
        return f"{sign * mag / 1_000_000:.1f}M"
    if mag >= 1_000:
        return f"{sign * mag / 1_000:.1f}K"
    return str(sign * mag)


def _fmt_bytes(n) -> str:
    """Format a byte count as "1.2 GB" / "1.2 MB" / "1.2 KB" / "N B".

    Clamps integer magnitude to _MAX_RECEIPT_INT BEFORE float conversion
    (mirroring _fmt_tokens) so huge ints (>= 10**309 overflow float) are
    clamped to a finite value rather than hitting the OverflowError path.
    Sign is preserved for in-range values.
    """
    if not isinstance(n, (int, float)) or isinstance(n, bool):
        return "0 B"
    try:
        i = int(n)
    except (ValueError, OverflowError):
        return "0 B"
    sign = -1 if i < 0 else 1
    mag = min(abs(i), _MAX_RECEIPT_INT)  # clamp int magnitude first, like _fmt_tokens
    v = float(mag)  # _MAX_RECEIPT_INT=10**15 is well within float range; always finite
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            sv = sign * v
            return f"{sv:.0f} {unit}" if unit == "B" else f"{sv:.1f} {unit}"
        v /= 1024
    return "0 B"  # unreachable — loop always returns; kept for type-checker


def _pretty_label(slug) -> str:
    """Title Case a strategy/tier slug for display (tool-output-trim -> Tool Output Trim)."""
    s = str(slug if slug not in (None, "") else "?").replace("_", "-")
    return " ".join(w.capitalize() for w in s.split("-")) or "?"


def _bar_rows(rows, label_key, value_key, fmt) -> str:
    """CSS horizontal bars; widths relative to the max value (guards div-by-zero)."""
    if not rows:
        return '<p class="empty">No data.</p>'
    # Floor the scaling basis at 0 so an all-negative dataset renders empty bars
    # (0% width) rather than every bar at 100%.
    max_v = max([(r.get(value_key, 0) or 0) for r in rows] + [0]) or 1
    out = []
    for r in rows:
        v = r.get(value_key, 0) or 0
        pct = max(0.0, min(100.0, v / max_v * 100))
        out.append(
            '<div class="bar-row">'
            f'<span class="bar-label">{_esc(_pretty_label(r.get(label_key, "?")))}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{pct:.1f}%"></span></span>'
            f'<span class="bar-val">{_esc(fmt(v))}</span>'
            "</div>"
        )
    return "\n".join(out)


def _sparkline(timeline) -> str:
    """Inline-SVG sparkline of context_pct_after over a session's timeline."""
    vals = []
    for e in timeline:
        p = e.get("context_pct_after")
        if _is_finite_num(p):
            vals.append(p)
    if len(vals) < 2:
        return '<span class="spark-na">—</span>'
    vals = vals[-60:]  # cap: a high-volume session can't emit thousands of coords
    w, h, pad = 120, 24, 2
    scale = max(vals)
    if scale <= 0:
        scale = 1
    step = (w - 2 * pad) / (len(vals) - 1)
    coords = []
    for i, p in enumerate(vals):
        t = min(1.0, max(0.0, p / scale))  # clamp into the viewBox
        coords.append(f"{pad + i * step:.1f},{h - pad - t * (h - 2 * pad):.1f}")
    return (
        f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" '
        f'points="{" ".join(coords)}"/></svg>'
    )


_STYLE = """
:root{--bg:#0f1117;--card:#1a1d27;--fg:#e6e8ee;--mut:#8b90a0;--acc:#5b8cff;--ok:#3fb950;--warn:#d29922}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:32px 20px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mut);font-size:13px;margin:0 0 24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:28px}
.card{background:var(--card);border-radius:10px;padding:16px}
.card .n{font-size:24px;font-weight:600}.card .l{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
section{background:var(--card);border-radius:10px;padding:18px;margin-bottom:18px}
section h2{font-size:14px;margin:0 0 14px;color:var(--fg)}
.bar-row{display:grid;grid-template-columns:160px 1fr 90px;align-items:center;gap:10px;margin:6px 0}
.bar-label{color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-track{background:#262a36;border-radius:5px;height:14px;overflow:hidden}
.bar-fill{display:block;height:100%;background:var(--acc);border-radius:5px}
.bar-val{text-align:right;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #262a36}
th{color:var(--mut);font-weight:500;font-size:12px}td{font-variant-numeric:tabular-nums}
.spark{color:var(--acc);vertical-align:middle}.spark-na,.mono{color:var(--mut)}.mono{font-family:ui-monospace,monospace}
.empty{color:var(--mut)}.foot{color:var(--mut);font-size:12px;margin-top:24px;text-align:center}
.pill{font-size:11px;padding:1px 7px;border-radius:10px;background:#262a36;color:var(--mut)}
.lifetime{background:linear-gradient(135deg,#1a2740,#1a1d27);border:1px solid #2b3a55;margin-bottom:24px}
.lt-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:18px}
.lt-cell .lt-n{font-size:26px;font-weight:700;color:#7aa2ff}
.lt-cell .lt-l{color:var(--mut);font-size:12px}.lt-since{color:var(--mut);font-size:12px;margin-top:12px}
.lt-title{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#7aa2ff;font-weight:700;margin:0 0 14px}
.sub2{color:var(--mut);font-size:12px;margin:-4px 0 14px}
.foot code{color:var(--acc);font-family:ui-monospace,monospace}
"""


def _lifetime_band(ledger: dict | None) -> str:
    """Header band of the user's TRUE lifetime totals (from the savings ledger)."""
    if not ledger or not ledger.get("tokens_saved"):
        return ""
    chips = [(_fmt_tokens(ledger["tokens_saved"]), "Tokens Reclaimed")]
    if ledger.get("prune_count"):
        chips.append((_fmt_int(ledger["prune_count"]), "Prunes Applied"))
    if ledger.get("turns_gained"):
        chips.append((f"~{_fmt_int(ledger['turns_gained'])}", "Est. Extra Turns"))
    _rate = ledger.get("savings_rate_pct")
    if _is_finite_num(_rate):
        # saved/processed (processed is cumulative-with-overlap) — NOT a per-prune average
        chips.append((f"{_rate:.1f}%", "Reclaimed of Processed"))
    _mult = ledger.get("session_multiplier_x")
    if _is_finite_num(_mult):
        chips.append((f"{_mult:.2f}×", "Longer Per Pruned Session"))
    cells = "".join(
        f'<div class="lt-cell"><div class="lt-n">{_esc(n)}</div>'
        f'<div class="lt-l">{_esc(label)}</div></div>'
        for n, label in chips
    )
    since = f" since {_esc(ledger['since'])}" if ledger.get("since") else ""
    return (
        '<section class="lifetime"><div class="lt-title">Lifetime</div>'
        f'<div class="lt-row">{cells}</div>'
        f'<div class="lt-since">Running totals from ~/.cozempic_savings.json{since}</div></section>'
    )


def render_html(data: dict, *, generated_ts: str, source_label: str = "",
                ledger: dict | None = None) -> str:
    """Render aggregate() output to a complete self-contained HTML document."""
    lt = data.get("lifetime", {}) or {}
    per_strategy = data.get("per_strategy", []) or []
    per_agent = data.get("per_agent", []) or []
    by_tier = data.get("by_tier", {}) or {}
    per_session = data.get("per_session", []) or []

    # Per-prune DETAIL (from receipts) — the lifetime band above is the single
    # headline, so this section is the breakdown the ledger can't show (which
    # strategies, tiers, sessions), NOT a second set of summary cards.
    _RECORDED_INTRO = (
        '<div class="sub2"><b style="color:var(--fg)">Recorded Prunes</b> — per-prune '
        "detail from receipts (~/.cozempic/receipts), separate from the all-time totals "
        "above; fills in as cozempic prunes.</div>"
    )
    if not lt.get("prunes_total"):
        body = (
            _RECORDED_INTRO +
            '<section><p class="empty">No prunes recorded yet. Run '
            "<span class=\"mono\">cozempic treat --execute</span> and they'll appear here.</p></section>"
        )
    else:
        strat_bars = _bar_rows(per_strategy, "id", "tokens_reclaimed", _fmt_tokens)
        tier_rows = [{"tier": str(k), "count": v}
                     for k, v in sorted(by_tier.items(), key=lambda kv: str(kv[0]))]
        tier_bars = _bar_rows(tier_rows, "tier", "count", _fmt_int)
        agent_rows = "".join(
            f"<tr><td>{_esc(_pretty_label(a.get('agent')))}</td><td>{_fmt_int(a.get('prunes', 0))}</td>"
            f"<td>{_fmt_int(a.get('committed', 0))}</td><td>{_esc(_fmt_tokens(a.get('tokens_reclaimed', 0)))}</td></tr>"
            for a in per_agent
        )
        _SESS_CAP = 50
        sess_rows = "".join(
            f'<tr><td class="mono">{_esc((s.get("session") or "")[:14])}</td>'
            f'<td><span class="pill">{_esc(_pretty_label(s.get("agent")))}</span></td>'
            f"<td>{_fmt_int(s.get('prunes', 0))}</td>"
            f"<td>{_esc(_fmt_tokens(s.get('tokens_reclaimed', 0)))}</td>"
            f"<td>{_sparkline(s.get('timeline', []))}</td></tr>"
            for s in per_session[:_SESS_CAP]
        )
        sess_more = (
            f'<div class="sub2">Showing the {_SESS_CAP} most-recently-active of '
            f"{_fmt_int(len(per_session))} sessions.</div>"
            if len(per_session) > _SESS_CAP else ""
        )
        body = f"""
        {_RECORDED_INTRO}
        <section><h2>Savings by Strategy</h2>{strat_bars}</section>
        <section><h2>Prunes by Tier</h2>{tier_bars}</section>
        <section><h2>By Agent</h2><table>
          <tr><th>Agent</th><th>Prunes</th><th>Applied</th><th>Tokens Reclaimed</th></tr>
          {agent_rows}</table></section>
        <section><h2>Sessions <span class="pill">Context % Over Time</span></h2><table>
          <tr><th>Session</th><th>Agent</th><th>Prunes</th><th>Reclaimed</th><th>Context Trend</th></tr>
          {sess_rows}</table>{sess_more}</section>
        """

    src = f" · {_esc(source_label)}" if source_label else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cozempic Dashboard</title><style>{_STYLE}</style></head>
<body><div class="wrap">
<h1>Cozempic Dashboard</h1>
<p class="sub">Generated {_esc(generated_ts)}{src}</p>
{_lifetime_band(ledger)}
{body}
<p class="foot">Local-only · ~/.cozempic · regenerate by running <code>cozempic dashboard</code></p>
</div></body></html>"""


def render_dashboard(base_dir: Path | None = None, *, generated_ts: str) -> str:
    """Convenience: load receipts + lifetime ledger -> aggregate -> render HTML."""
    from .aggregate import aggregate, load_receipts
    from .lifetime import load_lifetime

    return render_html(
        aggregate(load_receipts(base_dir)),
        generated_ts=generated_ts,
        source_label="~/.cozempic/receipts",
        ledger=load_lifetime(),
    )


def dashboard_path(base_dir: Path | None = None) -> Path:
    base = base_dir if base_dir is not None else (Path.home() / ".cozempic")
    return Path(base) / DEFAULT_FILENAME


def write_dashboard(html_str: str, *, base_dir: Path | None = None) -> Path:
    """Atomically write the HTML to ~/.cozempic/dashboard.html; return the path."""
    path = dashboard_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".dashboard-", suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html_str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
