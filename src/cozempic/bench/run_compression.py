"""CLI: run the Tier-1 compression benchmark over a corpus.

Usage:
    python -m cozempic.bench.run_compression [--corpus DIR ...] [--limit N] [--json]

Defaults to the repo fixtures if no corpus is given. Point --corpus at
~/.claude/projects to benchmark real sessions.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .compression import run_corpus, format_summary, as_json


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
    ap = argparse.ArgumentParser(description="Tier-1 offline compression benchmark")
    ap.add_argument("--corpus", action="append", default=[],
                    help="dir or .jsonl file (repeatable); default: repo fixtures")
    ap.add_argument("--limit", type=int, default=None, help="max sessions")
    ap.add_argument("--json", action="store_true", help="emit JSON summary")
    args = ap.parse_args(argv)

    corpus = args.corpus or [str(Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "sessions")]
    paths = _gather(corpus)
    if not paths:
        print(f"No .jsonl sessions found under: {corpus}", file=sys.stderr)
        return 2

    _results, summary = run_corpus(paths, limit=args.limit)
    print(as_json(summary) if args.json else format_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
