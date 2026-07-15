"""CLI: sweep candidate prune curves for jitter (reload count) vs peak usage.

Usage:
    python -m cozempic.bench.run_jitter --corpus ~/.claude/projects --limit 200
      [--reload-at 680000] [--depths 350000 450000 550000] [--rx aggressive]

Prints, per (reload_at × depth) policy: how many sessions reload, total reloads,
mean reloads per reloading session, peak tokens, and how many still exceed 700K.
Pick the depth with the fewest reloads that keeps peak under the target.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .jitter import Policy, sweep, format_sweep


def _gather(corpus_dirs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for d in corpus_dirs:
        base = Path(os.path.expanduser(d))
        if base.is_file() and base.suffix == ".jsonl":
            paths.append(base)
        elif base.is_dir():
            paths.extend(sorted(base.rglob("*.jsonl")))
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Jitter sweep over candidate prune curves")
    ap.add_argument("--corpus", action="append", default=[],
                    help="dir or .jsonl (repeatable); default: repo fixtures")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--reload-at", type=int, default=680_000)
    ap.add_argument("--depths", type=int, nargs="+", default=[350_000, 450_000, 550_000])
    args = ap.parse_args(argv)

    corpus = args.corpus or [str(Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "sessions")]
    paths = _gather(corpus)
    if not paths:
        print(f"No .jsonl sessions under {corpus}")
        return 2

    policies = [Policy(name=f"reload{args.reload_at // 1000}k-depth{d // 1000}k",
                       reload_at=args.reload_at, depth_target=d)
                for d in args.depths]
    summaries = sweep(paths, policies, limit=args.limit)
    print(format_sweep(summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
