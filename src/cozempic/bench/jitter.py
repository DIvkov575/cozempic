"""Curve replay: measure context JITTER (reload frequency) of a prune policy.

The user's objective is stability, not maximum compression: keep usage under a
target (~700K) while minimizing how often the live context is rewritten (each
rewrite = a reload = "jitter"). Only reloads move the model's context — the gentle
tiers are read-only (#106) — so jitter == reload count.

The growth curve is the REAL one: every assistant turn's Claude Code transcript
carries a ``usage`` block whose components sum to the API-reported cumulative
context size at that turn. Walking those snapshots in order IS the session's true
token-growth curve — no char-estimate reconstruction needed (and summing per-message
estimates is WRONG here: usage is cumulative, not additive — a late message reports
the whole running total, not its own tokens).

We replay that curve against a candidate policy:

  * a single reload threshold (e.g. 680K)
  * when the curve crosses it, model the reload as dropping the live level to the
    policy's depth target and continuing from there (subsequent snapshots are
    shifted down by the amount reclaimed).

and report, per policy: number of reloads, peak tokens reached, and mean post-prune
level. No agent runs, no LLM, no live session — cheap and deterministic.

CAVEAT (stated, not hidden): a saved session is ONE realized transcript. A real
reload changes what the agent sees and thus what it does next, so a replayed count
is an estimate for *comparing* policies, not an absolute prediction. It answers
"which candidate curve churns least on my historical growth" — the tuning question.
Sessions that already autocompacted mid-run show that as a real drop in the curve.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


def _usage_total(msg: dict) -> int | None:
    """Cumulative context tokens reported by one assistant turn, or None.

    Sums the four usage components (input + cache_create + cache_read + output) —
    the same total cozempic's exact estimator uses. Synthetic/zero-usage messages
    and non-assistant lines return None.
    """
    inner = msg.get("message", msg) if isinstance(msg, dict) else {}
    if not isinstance(inner, dict):
        return None
    u = inner.get("usage")
    if not isinstance(u, dict):
        return None
    total = sum(int(u.get(k, 0) or 0) for k in
                ("input_tokens", "cache_creation_input_tokens",
                 "cache_read_input_tokens", "output_tokens"))
    return total or None


def _growth_curve(path: Path) -> list[int]:
    """The session's real token-growth curve: cumulative usage per assistant turn."""
    curve: list[int] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return curve
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        t = _usage_total(msg)
        if t is not None:
            curve.append(t)
    return curve


def probe_peak_tokens(config_dir: Path) -> int | None:
    """Peak context tokens across ALL transcripts under an arm's CLAUDE_CONFIG_DIR.

    After a headless `claude -p` run, the session JSONL(s) live under
    ``<config_dir>/projects/**/*.jsonl``. Read each one's growth curve and return
    the max cumulative-usage level any turn reached — how much context the agent
    actually accumulated. None if no transcript / no usage. This is what shows
    WHETHER a task stressed context and whether a cozempic arm held it lower.
    """
    projects = config_dir / "projects"
    if not projects.is_dir():
        return None
    peak = 0
    for jsonl in projects.rglob("*.jsonl"):
        curve = _growth_curve(jsonl)
        if curve:
            peak = max(peak, max(curve))
    return peak or None


def probe_reload_count(tmp_dir: Path) -> int:
    """Count guard THRESHOLD firings from /tmp guard logs. 0 if none (e.g. 'none' arm)."""
    import glob
    total = 0
    for log in glob.glob(str(tmp_dir / "cozempic_guard_*.log")):
        try:
            total += Path(log).read_text(encoding="utf-8", errors="replace").count("THRESHOLD")
        except OSError:
            continue
    return total


@dataclass
class Policy:
    """A single-reload prune policy to evaluate against real growth curves.

    reload_at:    context level at which a reload fires.
    depth_target: level the reload drops context down to (the deep-prune runway).
    """
    name: str
    reload_at: int
    depth_target: int


@dataclass
class SessionJitter:
    path: str
    natural_peak: int          # highest real context level the session actually hit
    reloads: int               # times the policy would rewrite context (== jitter)
    peak_tokens: int           # highest level reached under the policy (post-offset)
    post_prune_levels: list[int] = field(default_factory=list)

    @property
    def mean_runway_floor(self) -> float:
        return (sum(self.post_prune_levels) / len(self.post_prune_levels)
                if self.post_prune_levels else 0.0)


def replay_session(path: Path, policy: Policy, **_ignored) -> SessionJitter | None:
    """Replay the session's REAL growth curve against `policy`. None if no curve.

    Walk the per-turn cumulative-usage snapshots. Track a running ``offset`` = total
    tokens reclaimed by reloads so far; the effective live level at each snapshot is
    ``raw - offset``. When the effective level crosses ``reload_at``, model a reload
    that drops it to ``depth_target`` by increasing ``offset`` accordingly, and count
    one reload. Peak is the max effective level seen.
    """
    curve = _growth_curve(path)
    if not curve:
        return None
    natural_peak = max(curve)

    reloads = 0
    peak = 0
    levels: list[int] = []
    offset = 0
    for raw in curve:
        eff = raw - offset
        if eff < 0:
            # A real autocompact already happened in the transcript (curve dropped);
            # rebase so we don't carry a stale offset past a real reset.
            offset = raw - (levels[-1] if levels else 0)
            eff = raw - offset
        peak = max(peak, eff)
        if eff >= policy.reload_at:
            reloads += 1
            offset += eff - policy.depth_target   # drop the live level to the target
            levels.append(policy.depth_target)
    return SessionJitter(path=str(path), natural_peak=natural_peak,
                         reloads=reloads, peak_tokens=peak, post_prune_levels=levels)


@dataclass
class PolicySummary:
    policy: str
    reload_at: int
    depth_target: int
    sessions_evaluated: int
    sessions_that_reload: int        # sessions big enough to trigger ≥1 reload
    total_reloads: int
    mean_reloads_per_reloading_session: float
    max_peak_tokens: int
    over_700k_sessions: int          # sessions whose peak still exceeded 700K
    mean_runway_floor: float


def summarize_policy(policy: Policy, results: list[SessionJitter]) -> PolicySummary:
    results = [r for r in results if r is not None]
    reloaders = [r for r in results if r.reloads > 0]
    total_reloads = sum(r.reloads for r in results)
    all_levels = [lvl for r in results for lvl in r.post_prune_levels]
    return PolicySummary(
        policy=policy.name,
        reload_at=policy.reload_at,
        depth_target=policy.depth_target,
        sessions_evaluated=len(results),
        sessions_that_reload=len(reloaders),
        total_reloads=total_reloads,
        mean_reloads_per_reloading_session=(
            sum(r.reloads for r in reloaders) / len(reloaders) if reloaders else 0.0),
        max_peak_tokens=max((r.peak_tokens for r in results), default=0),
        over_700k_sessions=sum(1 for r in results if r.peak_tokens > 700_000),
        mean_runway_floor=(sum(all_levels) / len(all_levels) if all_levels else 0.0),
    )


def sweep(paths: list[Path], policies: list[Policy], limit: int | None = None,
          step: int = 25) -> list[PolicySummary]:
    if limit is not None:
        paths = paths[:limit]
    summaries = []
    for policy in policies:
        results = [replay_session(p, policy, step=step) for p in paths]
        summaries.append(summarize_policy(policy, results))
    return summaries


def format_sweep(summaries: list[PolicySummary]) -> str:
    lines = [
        "Cozempic jitter sweep — reloads vs peak usage per candidate curve",
        "=" * 66,
        f"{'policy':<16}{'reload@':>9}{'depth':>8}{'reloaders':>10}"
        f"{'reloads':>9}{'mean/rl':>8}{'peak':>10}{'>700k':>7}",
    ]
    for s in summaries:
        lines.append(
            f"{s.policy:<16}{s.reload_at:>9,}{s.depth_target:>8,}"
            f"{s.sessions_that_reload:>10}{s.total_reloads:>9}"
            f"{s.mean_reloads_per_reloading_session:>8.2f}"
            f"{s.max_peak_tokens:>10,}{s.over_700k_sessions:>7}")
    lines.append("")
    lines.append("Lower reloads = less jitter. peak/>700k show whether the curve holds the target.")
    return "\n".join(lines)
