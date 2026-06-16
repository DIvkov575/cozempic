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

DEFAULT_FILENAME = "dashboard.html"


# --------------------------------------------------------------------------- #
# Formatting helpers (local — keep the renderer dependency-free)              #
# --------------------------------------------------------------------------- #
def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _fmt_int(n) -> str:
    return f"{int(n):,}" if isinstance(n, (int, float)) else "0"


def _fmt_tokens(n) -> str:
    n = int(n) if isinstance(n, (int, float)) else 0
    if abs(n) >= 999_950:  # rolls 999,999 up to "1.0M" rather than "1000.0K"
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_bytes(n) -> str:
    n = float(n) if isinstance(n, (int, float)) else 0.0
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


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
            f'<span class="bar-label">{_esc(r.get(label_key, "?"))}</span>'
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
        if isinstance(p, (int, float)) and not isinstance(p, bool) and math.isfinite(p):
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
"""


def render_html(data: dict, *, generated_ts: str, source_label: str = "") -> str:
    """Render aggregate() output to a complete self-contained HTML document."""
    lt = data.get("lifetime", {}) or {}
    per_strategy = data.get("per_strategy", []) or []
    per_agent = data.get("per_agent", []) or []
    by_tier = data.get("by_tier", {}) or {}
    per_session = data.get("per_session", []) or []

    if not lt.get("prunes_total"):
        body = (
            '<section><p class="empty">No prunes recorded yet. Run '
            "<span class=\"mono\">cozempic treat --execute</span> and they'll appear here.</p></section>"
        )
    else:
        cards = "".join(
            f'<div class="card"><div class="n">{_esc(n)}</div><div class="l">{_esc(l)}</div></div>'
            for n, l in (
                (_fmt_tokens(lt.get("tokens_reclaimed", 0)), "tokens reclaimed"),
                (_fmt_bytes(lt.get("bytes_reclaimed", 0)), "bytes reclaimed"),
                (_fmt_int(lt.get("committed", 0)), "prunes applied"),
                (_fmt_int(lt.get("sessions", 0)), "sessions"),
                (f"{(lt.get('deferral_rate') or 0) * 100:.0f}%", "deferral rate"),
            )
        )
        strat_bars = _bar_rows(per_strategy, "id", "tokens_reclaimed", _fmt_tokens)
        tier_rows = [{"tier": str(k), "count": v}
                     for k, v in sorted(by_tier.items(), key=lambda kv: str(kv[0]))]
        tier_bars = _bar_rows(tier_rows, "tier", "count", _fmt_int)
        agent_rows = "".join(
            f"<tr><td>{_esc(a.get('agent'))}</td><td>{_fmt_int(a.get('prunes', 0))}</td>"
            f"<td>{_fmt_int(a.get('committed', 0))}</td><td>{_esc(_fmt_tokens(a.get('tokens_reclaimed', 0)))}</td></tr>"
            for a in per_agent
        )
        sess_rows = "".join(
            f'<tr><td class="mono">{_esc((s.get("session") or "")[:14])}</td>'
            f'<td><span class="pill">{_esc(s.get("agent"))}</span></td>'
            f"<td>{_fmt_int(s.get('prunes', 0))}</td>"
            f"<td>{_esc(_fmt_tokens(s.get('tokens_reclaimed', 0)))}</td>"
            f"<td>{_sparkline(s.get('timeline', []))}</td></tr>"
            for s in per_session[:50]
        )
        body = f"""
        <div class="cards">{cards}</div>
        <section><h2>Savings by strategy</h2>{strat_bars}</section>
        <section><h2>Prunes by tier</h2>{tier_bars}</section>
        <section><h2>By agent</h2><table>
          <tr><th>Agent</th><th>Prunes</th><th>Applied</th><th>Tokens reclaimed</th></tr>
          {agent_rows}</table></section>
        <section><h2>Sessions <span class="pill">context % over time</span></h2><table>
          <tr><th>Session</th><th>Agent</th><th>Prunes</th><th>Reclaimed</th><th>Context trend</th></tr>
          {sess_rows}</table></section>
        """

    src = f" · {_esc(source_label)}" if source_label else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cozempic — prune value</title><style>{_STYLE}</style></head>
<body><div class="wrap">
<h1>cozempic — prune value</h1>
<p class="sub">Generated {_esc(generated_ts)}{src}</p>
{body}
<p class="foot">Local-only · generated from ~/.cozempic/receipts · cozempic</p>
</div></body></html>"""


def render_dashboard(base_dir: Path | None = None, *, generated_ts: str) -> str:
    """Convenience: load receipts -> aggregate -> render HTML string."""
    from .aggregate import aggregate, load_receipts

    return render_html(
        aggregate(load_receipts(base_dir)),
        generated_ts=generated_ts,
        source_label="~/.cozempic/receipts",
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
