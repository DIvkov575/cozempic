# Cozempic Memory Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace cozempic's regex behavioral-digest with a pluggable, early/background insight extractor that persists atomic memories to the `mymemories` repo, injects only compact stubs, anchors a northstar/todo tail block, and lets prune strategies aggressively evict spans once their content is capture-confirmed.

**Architecture:** Six decoupled units behind clean interfaces. `extract.py` turns a message span into source-trust-tagged `Insight` objects (default backend `claude_cli.py`, pluggable). `mem_bridge.py` persists any `Insight` to a `mymemories` partition and records `{span_hash → slug}` in a per-session **bridge ledger** under `~/.cozempic/bridge/`. A background scheduler fires extraction *early* (below the prune threshold), off the hook critical path. `tail.py` composes a marker-tagged tail block (northstar/todos/directives/stubs). A new `recoverability` strategy consults the ledger so capture-confirmed spans become droppable. `digest.py` regex extraction is retired with a one-shot migration.

**Tech Stack:** Python 3.11, stdlib only (`subprocess`, `json`, `hashlib`, `pathlib`), pytest. External deliverables (verified): `mymemories-tool` `embed.py {update|query <text>}` at `~/workplace/mymemories-tool`; `MEM_HOME=~/workplace/mymemories`; fact-file `format.md` schema (frontmatter `name`/`description`/`type` + body + `MEMORY.md` index line); `claude -p <prompt>` CLI. Existing cozempic primitives reused: `@strategy` registry, `StrategyResult`/`PruneAction`/`Message` types (`src/cozempic/types.py`), `save_messages` (rewrites/reorders/appends JSONL), `digest._sanitize_for_injection`, `digest._get_memdir`/`sync_to_memdir`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/cozempic/memory/__init__.py` | Package marker for the new memory subsystem. |
| `src/cozempic/memory/insight.py` | `Insight` dataclass + `TrustClass` — the shared currency between extractor and bridge. |
| `src/cozempic/memory/claude_cli.py` | Default LLM backend: `run_claude(prompt) -> str`, fence-stripping JSON parse. |
| `src/cozempic/memory/extract.py` | `extract_insights(span_text, existing_slugs, backend) -> list[Insight]`. Pluggable; conservative (preserve wording). |
| `src/cozempic/memory/mem_bridge.py` | Partition resolve; write `Insight` → fact file + `MEMORY.md`; `embed.py update`; ledger read/write. |
| `src/cozempic/memory/ledger.py` | Bridge ledger: `{span_hash → slug}` per session under `~/.cozempic/bridge/`. |
| `src/cozempic/memory/schedule.py` | `maybe_consolidate(...)` — early/background detached extraction, debounced. |
| `src/cozempic/memory/tail.py` | Compose the marker-tagged tail block; strip prior block (idempotent). |
| `src/cozempic/memory/stubs.py` | `relevant_stubs(query) -> list[str]` via `embed.py query`, score-filtered top-K. |
| `src/cozempic/strategies/recoverability.py` | New `@strategy` that marks capture-confirmed spans for removal. |
| `tests/memory/test_*.py` | One test module per unit above. |

**Span hashing (shared contract, used by ledger + strategy + scheduler):** a "span" is a contiguous run of messages. Its hash is `hashlib.sha256("\n".join(<canonical-json of each msg dict>).encode()).hexdigest()[:16]`. Defined once in `ledger.py` as `span_hash(msgs: list[dict]) -> str` and imported everywhere.

---

### Task 1: Memory package + `Insight` type

**Files:**
- Create: `src/cozempic/memory/__init__.py`
- Create: `src/cozempic/memory/insight.py`
- Create: `tests/memory/__init__.py`
- Test: `tests/memory/test_insight.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_insight.py
from cozempic.memory.insight import Insight, TrustClass


def test_insight_roundtrips_to_dict():
    ins = Insight(
        slug="use-uv-not-pip",
        title="Use uv, not pip",
        description="Project standardizes on uv for installs",
        type="feedback",
        trust_class=TrustClass.USER_DIRECTIVE,
        body="Always run `uv pip install`, never bare `pip`.",
    )
    d = ins.to_dict()
    assert d["slug"] == "use-uv-not-pip"
    assert d["trust_class"] == "user-directive"
    assert Insight.from_dict(d) == ins


def test_trust_class_values():
    assert TrustClass.USER_DIRECTIVE.value == "user-directive"
    assert TrustClass.AGENT_PROVISIONAL.value == "agent-provisional"
    assert TrustClass.WORLD_FACT.value == "world-fact"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/memory/test_insight.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cozempic.memory'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/cozempic/memory/__init__.py
"""Claim-based memory subsystem: extract, persist, inject, gate pruning."""
```

```python
# src/cozempic/memory/insight.py
"""The Insight — shared currency between the extractor and the persistence bridge."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TrustClass(str, Enum):
    USER_DIRECTIVE = "user-directive"      # intent/preference/correction — keep verbatim
    AGENT_PROVISIONAL = "agent-provisional"  # model claim — keep only if corroborated
    WORLD_FACT = "world-fact"              # user-asserted fact — never ground truth


@dataclass(frozen=True)
class Insight:
    slug: str          # kebab-case; unique within partition; the [[link]] target
    title: str         # human title for MEMORY.md
    description: str    # one dense line, used for index + recall relevance
    type: str          # user | feedback | project | reference (format.md)
    trust_class: TrustClass
    body: str          # the fact; preserve original wording

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "type": self.type,
            "trust_class": self.trust_class.value,
            "body": self.body,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Insight":
        return cls(
            slug=d["slug"],
            title=d["title"],
            description=d["description"],
            type=d["type"],
            trust_class=TrustClass(d["trust_class"]),
            body=d["body"],
        )
```

```python
# tests/memory/__init__.py
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/memory/test_insight.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cozempic/memory/__init__.py src/cozempic/memory/insight.py tests/memory/__init__.py tests/memory/test_insight.py
git commit -m "feat(memory): Insight type + TrustClass"
```

---

### Task 2: Bridge ledger + span hashing

**Files:**
- Create: `src/cozempic/memory/ledger.py`
- Test: `tests/memory/test_ledger.py`

The ledger records which spans have been durably captured. `span_hash` lives here because ledger, scheduler, and the recoverability strategy all need the identical hash. Stored at `~/.cozempic/bridge/<session_id>.json` as `{span_hash: slug}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_ledger.py
from cozempic.memory import ledger


def test_span_hash_is_stable_and_order_sensitive():
    a = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    assert ledger.span_hash(a) == ledger.span_hash(list(a))          # stable
    assert ledger.span_hash(a) != ledger.span_hash(list(reversed(a)))  # order matters
    assert len(ledger.span_hash(a)) == 16


def test_record_and_confirm(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    msgs = [{"role": "user", "content": "always use uv"}]
    h = ledger.span_hash(msgs)
    assert ledger.is_captured("sess1", h) is False
    ledger.record("sess1", h, "use-uv-not-pip")
    assert ledger.is_captured("sess1", h) is True
    assert ledger.slug_for("sess1", h) == "use-uv-not-pip"


def test_ledger_isolated_per_session(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    ledger.record("sessA", "deadbeefdeadbeef", "slug-a")
    assert ledger.is_captured("sessB", "deadbeefdeadbeef") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/memory/test_ledger.py -v`
Expected: FAIL with `AttributeError: module 'cozempic.memory.ledger' has no attribute 'span_hash'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/cozempic/memory/ledger.py
"""Bridge ledger: which message spans have been durably captured as memories.

`{span_hash -> slug}` per session at ~/.cozempic/bridge/<session_id>.json.
This is the capture-confirmation source the recoverability strategy gates on.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

BRIDGE_DIR = Path.home() / ".cozempic" / "bridge"


def span_hash(msgs: list[dict]) -> str:
    """Stable, order-sensitive 16-hex hash of a contiguous message span."""
    canonical = "\n".join(
        json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for m in msgs
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _path(session_id: str) -> Path:
    safe = session_id.replace("/", "_")
    return BRIDGE_DIR / f"{safe}.json"


def _load(session_id: str) -> dict:
    p = _path(session_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def record(session_id: str, span_h: str, slug: str) -> None:
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    data = _load(session_id)
    data[span_h] = slug
    _path(session_id).write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_captured(session_id: str, span_h: str) -> bool:
    return span_h in _load(session_id)


def slug_for(session_id: str, span_h: str) -> str | None:
    return _load(session_id).get(span_h)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/memory/test_ledger.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cozempic/memory/ledger.py tests/memory/test_ledger.py
git commit -m "feat(memory): bridge ledger + stable span hashing"
```

---

### Task 3: `claude_cli` backend

**Files:**
- Create: `src/cozempic/memory/claude_cli.py`
- Test: `tests/memory/test_claude_cli.py`

Thin wrapper over `claude -p`. Must strip ```` ```json ```` fences (the CLI wraps output). No network in tests — `subprocess.run` is monkeypatched.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_claude_cli.py
import json
from cozempic.memory import claude_cli


def test_strip_fences_plain():
    assert claude_cli._strip_fences('{"a": 1}') == '{"a": 1}'


def test_strip_fences_json_block():
    raw = '```json\n{"a": 1}\n```'
    assert json.loads(claude_cli._strip_fences(raw)) == {"a": 1}


def test_run_claude_invokes_cli(monkeypatch):
    calls = {}

    class _CP:
        stdout = '```json\n[]\n```'
        returncode = 0

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["input"] = kw.get("input")
        return _CP()

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    out = claude_cli.run_claude("PROMPT-TEXT")
    assert calls["cmd"][0] == "claude"
    assert "-p" in calls["cmd"]
    assert out == "[]"


def test_run_claude_returns_empty_on_failure(monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("claude not installed")

    monkeypatch.setattr(claude_cli.subprocess, "run", boom)
    assert claude_cli.run_claude("x") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/memory/test_claude_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cozempic.memory.claude_cli'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/cozempic/memory/claude_cli.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/memory/test_claude_cli.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cozempic/memory/claude_cli.py tests/memory/test_claude_cli.py
git commit -m "feat(memory): claude -p backend with fence-stripping"
```

---

### Task 4: Insight extractor (pluggable, conservative)

**Files:**
- Create: `src/cozempic/memory/extract.py`
- Test: `tests/memory/test_extract.py`

`extract_insights` takes span text, the existing partition slugs (for dedup), and a `backend` callable (default `claude_cli.run_claude`). It builds the conservative prompt, parses the JSON array, drops `world-fact`, and returns `Insight` objects. Backend is injected so tests never call the CLI.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_extract.py
import json
from cozempic.memory.extract import extract_insights, build_prompt
from cozempic.memory.insight import TrustClass


def test_prompt_is_conservative_and_lists_existing():
    p = build_prompt("SPAN", existing_slugs=["old-slug"])
    assert "SPAN" in p
    assert "preserv" in p.lower()          # preserve original wording
    assert "old-slug" in p                  # dedup context
    assert "world-fact" in p                # trust taxonomy present


def test_extract_parses_and_drops_world_facts():
    payload = json.dumps([
        {"slug": "use-uv", "title": "Use uv", "description": "d",
         "type": "feedback", "trust_class": "user-directive", "body": "use `uv`"},
        {"slug": "earth-round", "title": "Earth", "description": "d",
         "type": "reference", "trust_class": "world-fact", "body": "earth is round"},
    ])
    got = extract_insights("span", existing_slugs=[], backend=lambda _p: payload)
    assert [i.slug for i in got] == ["use-uv"]           # world-fact dropped
    assert got[0].trust_class is TrustClass.USER_DIRECTIVE


def test_extract_handles_garbage_backend_output():
    assert extract_insights("span", existing_slugs=[], backend=lambda _p: "not json") == []
    assert extract_insights("span", existing_slugs=[], backend=lambda _p: "") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/memory/test_extract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cozempic.memory.extract'`

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/memory/test_extract.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cozempic/memory/extract.py tests/memory/test_extract.py
git commit -m "feat(memory): pluggable conservative insight extractor"
```

---

### Task 5: mymemories bridge (persist + partition resolve)

**Files:**
- Create: `src/cozempic/memory/mem_bridge.py`
- Test: `tests/memory/test_mem_bridge.py`

Resolves the current project's `mymemories` partition via the symlink at
`<CLAUDE_CONFIG_DIR>/projects/<mangled-cwd>/memory` (the tool's own convention). Writes each
`Insight` as `<partition>/<slug>.md` per `format.md`, appends a `MEMORY.md` index line, runs
`embed.py update`, and records the span in the ledger. No auto-commit. If the project isn't
partitioned, `persist_insights` returns `[]` (no-op).

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_mem_bridge.py
from pathlib import Path
from cozempic.memory import mem_bridge, ledger
from cozempic.memory.insight import Insight, TrustClass


def _mk_insight(slug="use-uv"):
    return Insight(slug, "Use uv", "std on uv", "feedback",
                   TrustClass.USER_DIRECTIVE, "Always `uv pip install`.")


def test_write_fact_file_follows_format(tmp_path):
    part = tmp_path / "myproj"
    part.mkdir()
    mem_bridge._write_fact_file(part, _mk_insight())
    text = (part / "use-uv.md").read_text()
    assert text.startswith("---")
    assert "name: use-uv" in text
    assert "type: feedback" in text
    assert "Always `uv pip install`." in text


def test_append_memory_index_line(tmp_path):
    part = tmp_path / "myproj"
    part.mkdir()
    (part / "MEMORY.md").write_text("# Memories\n")
    mem_bridge._append_index_line(part, _mk_insight())
    assert "- [Use uv](use-uv.md) — std on uv" in (part / "MEMORY.md").read_text()


def test_persist_noop_when_not_partitioned(tmp_path, monkeypatch):
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: None)
    got = mem_bridge.persist_insights("sess1", [(_mk_insight(), "spanhash0000aaaa")])
    assert got == []


def test_persist_writes_and_records_ledger(tmp_path, monkeypatch):
    part = tmp_path / "myproj"
    part.mkdir()
    (part / "MEMORY.md").write_text("# Memories\n")
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: part)
    monkeypatch.setattr(mem_bridge, "_reindex", lambda: None)   # no embed.py in test
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path / "bridge")

    slugs = mem_bridge.persist_insights("sess1", [(_mk_insight(), "spanhash0000aaaa")])
    assert slugs == ["use-uv"]
    assert (part / "use-uv.md").exists()
    assert ledger.is_captured("sess1", "spanhash0000aaaa")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/memory/test_mem_bridge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cozempic.memory.mem_bridge'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/cozempic/memory/mem_bridge.py
"""Persist Insights to the mymemories repo and record capture in the ledger.

Standalone stage: `insight -> slug`. Knows nothing about how insights were extracted.
Partition resolution reuses the mymemories-tool symlink convention. No auto-commit.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import ledger
from .insight import Insight

TOOL_DIR = Path(os.path.expanduser("~/workplace/mymemories-tool"))
MEM_HOME = Path(os.environ.get("MEM_HOME", os.path.expanduser("~/workplace/mymemories")))


def _mangled_cwd() -> str:
    """Claude Code mangles the cwd to a dir name by replacing '/' with '-'."""
    return os.getcwd().replace("/", "-")


def _claude_projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
    return Path(base) / "projects"


def resolve_partition() -> Path | None:
    """Return the mymemories partition dir for the cwd, or None if not installed."""
    link = _claude_projects_dir() / _mangled_cwd() / "memory"
    if not link.is_symlink():
        return None
    target = link.resolve()
    try:
        target.relative_to(MEM_HOME.resolve())
    except ValueError:
        return None
    return target if target.is_dir() else None


def _write_fact_file(partition: Path, ins: Insight) -> None:
    fm = (
        "---\n"
        f"name: {ins.slug}\n"
        f"description: {ins.description}\n"
        f"type: {ins.type}\n"
        "---\n\n"
    )
    (partition / f"{ins.slug}.md").write_text(fm + ins.body.rstrip() + "\n", encoding="utf-8")


def _append_index_line(partition: Path, ins: Insight) -> None:
    idx = partition / "MEMORY.md"
    line = f"- [{ins.title}]({ins.slug}.md) — {ins.description}\n"
    prior = idx.read_text(encoding="utf-8") if idx.exists() else "# Memories\n"
    if line not in prior:
        if not prior.endswith("\n"):
            prior += "\n"
        idx.write_text(prior + line, encoding="utf-8")


def _reindex() -> None:
    """Best-effort incremental embedding index update. Never raises."""
    embed = TOOL_DIR / "embed.py"
    if not embed.exists():
        return
    try:
        subprocess.run(["python3", str(embed), "update"], capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        pass


def persist_insights(session_id: str, items: list[tuple[Insight, str]]) -> list[str]:
    """Write each (insight, span_hash); record ledger; reindex once. Returns slugs written.

    No-op (returns []) if the project isn't partitioned into mymemories.
    """
    partition = resolve_partition()
    if partition is None:
        return []
    written: list[str] = []
    for ins, span_h in items:
        _write_fact_file(partition, ins)
        _append_index_line(partition, ins)
        ledger.record(session_id, span_h, ins.slug)
        written.append(ins.slug)
    if written:
        _reindex()
    return written
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/memory/test_mem_bridge.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cozempic/memory/mem_bridge.py tests/memory/test_mem_bridge.py
git commit -m "feat(memory): mymemories persistence bridge + partition resolve"
```

---

### Task 6: Relevant-stub lookup

**Files:**
- Create: `src/cozempic/memory/stubs.py`
- Test: `tests/memory/test_stubs.py`

Queries `embed.py query <text>`, parses `score  partition/file.md` rows, keeps `score > 0.4`,
returns top-K compact stub strings. Reuses `digest._sanitize_for_injection` on each stub
(untrusted-text injection guard). The `embed.py` call is injected for tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_stubs.py
from cozempic.memory import stubs


def test_parse_query_rows_filters_low_scores():
    raw = "0.81  workplace/use-uv.md\n0.12  biostat/foo.md\n0.55  workplace/bar.md\n"
    rows = stubs._parse_rows(raw, min_score=0.4)
    assert rows == [("workplace/use-uv.md", 0.81), ("workplace/bar.md", 0.55)]


def test_relevant_stubs_top_k(monkeypatch):
    raw = "\n".join(f"0.9{i}  p/f{i}.md" for i in range(10))
    monkeypatch.setattr(stubs, "_query", lambda q: raw)
    out = stubs.relevant_stubs("anything", k=3)
    assert len(out) == 3
    assert all("p/f" in s for s in out)


def test_relevant_stubs_empty_on_no_backend(monkeypatch):
    monkeypatch.setattr(stubs, "_query", lambda q: "")
    assert stubs.relevant_stubs("q", k=5) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/memory/test_stubs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cozempic.memory.stubs'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/cozempic/memory/stubs.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/memory/test_stubs.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cozempic/memory/stubs.py tests/memory/test_stubs.py
git commit -m "feat(memory): relevant-stub lookup via embedding index"
```

---

### Task 7: Tail composer

**Files:**
- Create: `src/cozempic/memory/tail.py`
- Test: `tests/memory/test_tail.py`

Builds ONE tail message dict tagged with a marker so the next prune replaces it. `strip_prior_tail` removes any earlier marked block; `build_tail_message` composes northstar/todos/directives/stubs; `compose_tail` = strip + append (idempotent). Marker mirrors the existing `__cozempic_behavioral_digest__` convention.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_tail.py
from cozempic.memory import tail


def test_build_tail_message_has_marker_and_sections():
    msg = tail.build_tail_message(
        northstar="Ship the memory overhaul",
        todos=["wire scheduler", "retire digest"],
        directives=["never auto-commit mymemories"],
        stubs=["workplace/use-uv.md"],
    )
    text = tail._text_of(msg)
    assert tail.TAIL_MARKER in text
    assert "Ship the memory overhaul" in text
    assert "wire scheduler" in text
    assert "never auto-commit mymemories" in text
    assert "workplace/use-uv.md" in text
    assert msg["role"] == "user"


def test_strip_prior_tail_removes_only_marked():
    keep = {"role": "user", "content": "real message"}
    old = tail.build_tail_message("goal", [], [], [])
    result = tail.strip_prior_tail([keep, old])
    assert result == [keep]


def test_compose_is_idempotent():
    base = [{"role": "user", "content": "hi"}]
    once = tail.compose_tail(base, northstar="G", todos=[], directives=[], stubs=[])
    twice = tail.compose_tail(once, northstar="G", todos=[], directives=[], stubs=[])
    # exactly one tail block after either 1 or 2 composes
    assert sum(1 for m in once if tail.TAIL_MARKER in tail._text_of(m)) == 1
    assert sum(1 for m in twice if tail.TAIL_MARKER in tail._text_of(m)) == 1
    assert len(once) == len(twice)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/memory/test_tail.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cozempic.memory.tail'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/cozempic/memory/tail.py
"""Compose the end-of-conversation tail block: northstar, todos, directives, stubs.

Placed last (highest-adherence position). Marker-tagged so each prune replaces rather
than appends — idempotent.
"""

from __future__ import annotations

TAIL_MARKER = "__cozempic_northstar_tail__"


def _text_of(msg: dict) -> str:
    c = msg.get("content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(
            b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def build_tail_message(
    northstar: str,
    todos: list[str],
    directives: list[str],
    stubs: list[str],
) -> dict:
    lines = [TAIL_MARKER, "# Current focus (regenerated each prune)"]
    if northstar:
        lines += ["", "## Northstar", northstar]
    if todos:
        lines += ["", "## Open todos", *[f"- {t}" for t in todos]]
    if directives:
        lines += ["", "## Standing directives", *[f"- {d}" for d in directives]]
    if stubs:
        lines += ["", "## Relevant memories (use /recall to load)",
                  *[f"- {s}" for s in stubs]]
    return {"role": "user", "content": "\n".join(lines)}


def strip_prior_tail(messages: list[dict]) -> list[dict]:
    return [m for m in messages if TAIL_MARKER not in _text_of(m)]


def compose_tail(
    messages: list[dict],
    northstar: str,
    todos: list[str],
    directives: list[str],
    stubs: list[str],
) -> list[dict]:
    cleaned = strip_prior_tail(messages)
    return cleaned + [build_tail_message(northstar, todos, directives, stubs)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/memory/test_tail.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cozempic/memory/tail.py tests/memory/test_tail.py
git commit -m "feat(memory): idempotent northstar/todo tail composer"
```

---

### Task 8: Early/background consolidation scheduler

**Files:**
- Create: `src/cozempic/memory/schedule.py`
- Test: `tests/memory/test_schedule.py`

`maybe_consolidate(session_id, span_msgs, fraction)` decides whether to fire. It fires *early*
(when `fraction >= LOW_WATER` and below the prune threshold), debounces via a marker file, and
launches a **detached** worker (`consolidate_worker`) so the caller never blocks. The worker
(tested directly, synchronously) runs extract → persist. `_spawn` is injected for tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_schedule.py
from cozempic.memory import schedule, ledger
from cozempic.memory.insight import Insight, TrustClass


def test_fires_at_low_water_not_before(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "BRIDGE_DIR", tmp_path)
    spawned = []
    monkeypatch.setattr(schedule, "_spawn", lambda sid, msgs: spawned.append(sid))

    schedule.maybe_consolidate("s1", [{"role": "user", "content": "x"}], fraction=0.10)
    assert spawned == []                       # below low-water: no fire

    schedule.maybe_consolidate("s1", [{"role": "user", "content": "x"}], fraction=0.35)
    assert spawned == ["s1"]                    # at/above low-water: fires


def test_debounced_within_window(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "BRIDGE_DIR", tmp_path)
    spawned = []
    monkeypatch.setattr(schedule, "_spawn", lambda sid, msgs: spawned.append(sid))
    schedule.maybe_consolidate("s1", [{"role": "user", "content": "x"}], fraction=0.5)
    schedule.maybe_consolidate("s1", [{"role": "user", "content": "x"}], fraction=0.5)
    assert spawned == ["s1"]                    # second call debounced


def test_worker_extracts_and_persists(tmp_path, monkeypatch):
    payload_ins = [Insight("s", "T", "d", "feedback", TrustClass.USER_DIRECTIVE, "b")]
    monkeypatch.setattr(schedule, "extract_insights", lambda text, slugs: payload_ins)
    captured = {}
    monkeypatch.setattr(schedule, "persist_insights",
                        lambda sid, items: captured.update(sid=sid, n=len(items)) or ["s"])
    schedule.consolidate_worker("s1", [{"role": "user", "content": "always use uv"}])
    assert captured == {"sid": "s1", "n": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/memory/test_schedule.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cozempic.memory.schedule'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/cozempic/memory/schedule.py
"""Early, background consolidation. Fires ahead of the prune threshold, off the critical
path, debounced. The hook that calls maybe_consolidate() never blocks on extraction.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import ledger
from .extract import extract_insights
from .ledger import span_hash
from .mem_bridge import persist_insights

BRIDGE_DIR = ledger.BRIDGE_DIR

# Fire consolidation once context reaches this fraction of the window — deliberately
# BELOW the prune threshold so memories are captured before pruning is warranted.
LOW_WATER = 0.30
_DEBOUNCE_S = 300


def _marker(session_id: str) -> Path:
    return BRIDGE_DIR / f"{session_id.replace('/', '_')}.consolidated"


def _recently_fired(session_id: str) -> bool:
    m = _marker(session_id)
    if not m.exists():
        return False
    try:
        return (time.time() - m.stat().st_mtime) < _DEBOUNCE_S
    except OSError:
        return False


def _touch(session_id: str) -> None:
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    _marker(session_id).write_text("", encoding="utf-8")


def _existing_slugs() -> list[str]:
    from .mem_bridge import resolve_partition
    part = resolve_partition()
    if part is None:
        return []
    return [p.stem for p in part.glob("*.md") if p.name != "MEMORY.md"]


def _span_text(msgs: list[dict]) -> str:
    out = []
    for m in msgs:
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(b.get("text", "") for b in c
                         if isinstance(b, dict) and b.get("type") == "text")
        out.append(f"{m.get('role', '?')}: {c}")
    return "\n".join(out)


def consolidate_worker(session_id: str, span_msgs: list[dict]) -> None:
    """Synchronous work unit: extract → persist. Run directly (worker) or via _spawn."""
    insights = extract_insights(_span_text(span_msgs), _existing_slugs())
    if not insights:
        return
    items = [(ins, span_hash(span_msgs)) for ins in insights]
    persist_insights(session_id, items)


def _spawn(session_id: str, span_msgs: list[dict]) -> None:
    """Launch a detached worker process; return immediately."""
    payload = json.dumps({"session_id": session_id, "msgs": span_msgs})
    subprocess.Popen(
        [sys.executable, "-m", "cozempic.memory.schedule", "--worker"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    ).stdin.write(payload.encode())  # fire-and-forget


def maybe_consolidate(session_id: str, span_msgs: list[dict], fraction: float) -> bool:
    """Fire background consolidation if at/above low-water and not debounced.

    Returns True if a worker was spawned. Never blocks on extraction.
    """
    if fraction < LOW_WATER:
        return False
    if _recently_fired(session_id):
        return False
    _touch(session_id)
    _spawn(session_id, span_msgs)
    return True


if __name__ == "__main__":  # detached worker entrypoint
    if "--worker" in sys.argv:
        data = json.loads(sys.stdin.read() or "{}")
        if data:
            consolidate_worker(data["session_id"], data["msgs"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/memory/test_schedule.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cozempic/memory/schedule.py tests/memory/test_schedule.py
git commit -m "feat(memory): early/background consolidation scheduler"
```

---

### Task 9: Recoverability-gated prune strategy

**Files:**
- Create: `src/cozempic/strategies/recoverability.py`
- Modify: `src/cozempic/strategies/__init__.py` (import the new module so `@strategy` registers)
- Modify: `src/cozempic/registry.py` (add `"recoverability"` to the `aggressive` prescription, first)
- Test: `tests/memory/test_recoverability.py`

The strategy needs the session id. Strategies receive `config`; the executor already threads a
`config` dict. This strategy reads `config["session_id"]` (absent → no-op, safe) and removes any
message whose singleton span hash is capture-confirmed in the ledger. It follows the exact
`@strategy` + `StrategyResult`/`PruneAction` contract used by `strategies/standard.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_recoverability.py
from cozempic.strategies import recoverability
from cozempic.memory import ledger


def _msg(idx, text):
    d = {"role": "user", "content": text}
    import json
    return (idx, d, len(json.dumps(d)))


def test_removes_only_captured_spans(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    m0 = _msg(0, "captured fact")
    m1 = _msg(1, "uncaptured fact")
    ledger.record("s1", ledger.span_hash([m0[1]]), "some-slug")

    result = recoverability.strategy_recoverability(
        [m0, m1], {"session_id": "s1"}
    )
    removed_idx = [a.line_index for a in result.actions if a.action == "remove"]
    assert removed_idx == [0]
    assert result.messages_removed == 1


def test_noop_without_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "BRIDGE_DIR", tmp_path)
    result = recoverability.strategy_recoverability([_msg(0, "x")], {})
    assert result.actions == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/memory/test_recoverability.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cozempic.strategies.recoverability'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/cozempic/strategies/recoverability.py
"""Recoverability-gated pruning: drop spans whose content is durably captured.

A message is removable once its content lives as a memory (ledger has its span hash).
Uncaptured messages are untouched — other strategies decide their fate.
"""

from __future__ import annotations

from ..memory import ledger
from ..registry import strategy
from ..types import Message, PruneAction, StrategyResult


@strategy("recoverability", "Drop spans already captured as durable memories",
          "aggressive", "10-40%")
def strategy_recoverability(messages: list[Message], config: dict) -> StrategyResult:
    session_id = config.get("session_id", "")
    actions: list[PruneAction] = []
    total_orig = sum(b for _, _, b in messages)

    if session_id:
        for idx, msg, size in messages:
            if ledger.is_captured(session_id, ledger.span_hash([msg])):
                actions.append(PruneAction(
                    line_index=idx,
                    action="remove",
                    reason="captured as durable memory (recoverable via /recall)",
                    original_bytes=size,
                    pruned_bytes=0,
                ))

    removed = len(actions)
    pruned_bytes = sum(a.original_bytes for a in actions)
    return StrategyResult(
        strategy_name="recoverability",
        actions=actions,
        original_bytes=total_orig,
        pruned_bytes=pruned_bytes,
        messages_affected=removed,
        messages_removed=removed,
        messages_replaced=0,
        summary=f"Removed {removed} capture-confirmed message(s)",
    )
```

- [ ] **Step 4: Register the strategy**

In `src/cozempic/strategies/__init__.py`, add an import so the decorator runs on package load. Read the file first; append alongside the existing strategy-module imports:

```python
from . import recoverability  # noqa: F401  (registers @strategy)
```

In `src/cozempic/registry.py`, add `"recoverability"` as the FIRST entry of the `"aggressive"` list (drop captured spans before other strategies run):

```python
    "aggressive": [
        "recoverability",
        "compact-summary-collapse",
        # ... rest unchanged ...
    ],
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/memory/test_recoverability.py -v`
Expected: PASS (2 passed)

Run: `python -m pytest tests/ -k "registry or strategy" -v`
Expected: PASS (no regression from the new registration)

- [ ] **Step 6: Commit**

```bash
git add src/cozempic/strategies/recoverability.py src/cozempic/strategies/__init__.py src/cozempic/registry.py tests/memory/test_recoverability.py
git commit -m "feat(strategies): recoverability-gated pruning"
```

---

### Task 10: Wire scheduler into the guard/nudge path

**Files:**
- Modify: `src/cozempic/cli.py` (`cmd_nudge`, near the existing threshold checks around line 1231)
- Test: `tests/memory/test_nudge_consolidation.py`

`cmd_nudge` already computes a context fraction and fires at 25/55/80%. Add an early
consolidation call there: it's the existing place that knows the fraction and the session, and
runs frequently. Guard behind `COZEMPIC_MEMORY_OFF`.

- [ ] **Step 1: Read the current `cmd_nudge` to find the fraction variable**

Run: `sed -n '1231,1330p' src/cozempic/cli.py`
Note the variable holding the context fraction and the session-id/transcript variable in scope.

- [ ] **Step 2: Write the failing test**

```python
# tests/memory/test_nudge_consolidation.py
import cozempic.cli as cli
from cozempic.memory import schedule


def test_nudge_fires_consolidation(monkeypatch):
    calls = {}
    monkeypatch.setattr(schedule, "maybe_consolidate",
                        lambda sid, msgs, fraction: calls.update(sid=sid, f=fraction) or True)
    cli._maybe_memory_consolidate("sess-xyz", [{"role": "user", "content": "x"}], 0.5)
    assert calls == {"sid": "sess-xyz", "f": 0.5}


def test_nudge_consolidation_off_switch(monkeypatch):
    monkeypatch.setenv("COZEMPIC_MEMORY_OFF", "1")
    called = []
    monkeypatch.setattr(schedule, "maybe_consolidate",
                        lambda *a, **k: called.append(1))
    cli._maybe_memory_consolidate("s", [{"role": "user", "content": "x"}], 0.5)
    assert called == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/memory/test_nudge_consolidation.py -v`
Expected: FAIL with `AttributeError: module 'cozempic.cli' has no attribute '_maybe_memory_consolidate'`

- [ ] **Step 4: Add the helper and call it**

Add this helper to `src/cozempic/cli.py` (module scope):

```python
def _maybe_memory_consolidate(session_id: str, messages: list[dict], fraction: float) -> None:
    """Early/background memory consolidation. Never raises into the hook."""
    import os
    from ._validation import parse_env_bool
    if parse_env_bool("COZEMPIC_MEMORY_OFF", default=False, warn=False):
        return
    try:
        from .memory import schedule
        schedule.maybe_consolidate(session_id, messages, fraction)
    except Exception:
        pass  # memory is best-effort; must never break the nudge hook
```

Then, inside `cmd_nudge`, after the fraction is computed and messages are loaded, add one call (use the fraction + session-id variable names found in Step 1):

```python
    _maybe_memory_consolidate(session_id, [m for _, m, _ in messages], fraction)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/memory/test_nudge_consolidation.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add src/cozempic/cli.py tests/memory/test_nudge_consolidation.py
git commit -m "feat(cli): fire early memory consolidation from nudge path"
```

---

### Task 11: Wire tail composer + stub injection into prune output

**Files:**
- Modify: `src/cozempic/executor.py` (post-strategy, before `save_messages`)
- Test: `tests/memory/test_executor_tail.py`

After strategies produce the pruned message list and before it's written, append the tail block.
Northstar/todos/directives derivation is intentionally minimal for v1 (see Step 4); stubs come
from `stubs.relevant_stubs`. Guarded by `COZEMPIC_MEMORY_OFF`.

- [ ] **Step 1: Read the executor's write path**

Run: `sed -n '1,120p' src/cozempic/executor.py`
Identify the function that returns/writes the final pruned `list[Message]` and the variable holding it.

- [ ] **Step 2: Write the failing test**

```python
# tests/memory/test_executor_tail.py
from cozempic.memory import tail
from cozempic import executor


def test_apply_tail_appends_one_block(monkeypatch):
    monkeypatch.setattr(executor, "_derive_northstar", lambda msgs: "Ship it")
    monkeypatch.setattr(executor, "_derive_todos", lambda msgs: ["do x"])
    monkeypatch.setattr(executor, "_derive_directives", lambda msgs: ["never Y"])
    from cozempic.memory import stubs
    monkeypatch.setattr(stubs, "relevant_stubs", lambda q, k=7: ["p/z.md"])

    msgs = [{"role": "user", "content": "hello"}]
    out = executor.apply_memory_tail(msgs)
    tails = [m for m in out if tail.TAIL_MARKER in tail._text_of(m)]
    assert len(tails) == 1
    assert "Ship it" in tail._text_of(tails[0])


def test_apply_tail_off_switch(monkeypatch):
    monkeypatch.setenv("COZEMPIC_MEMORY_OFF", "1")
    msgs = [{"role": "user", "content": "hello"}]
    assert executor.apply_memory_tail(msgs) == msgs
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/memory/test_executor_tail.py -v`
Expected: FAIL with `AttributeError: module 'cozempic.executor' has no attribute 'apply_memory_tail'`

- [ ] **Step 4: Implement the derivations + apply function**

Add to `src/cozempic/executor.py`:

```python
def _derive_northstar(messages: list[dict]) -> str:
    """v1: the first substantive user message is the stated goal."""
    from .memory.tail import _text_of
    for m in messages:
        if m.get("role") == "user":
            t = _text_of(m).strip()
            if len(t) > 20 and "__cozempic" not in t:
                return t.splitlines()[0][:200]
    return ""


def _derive_todos(messages: list[dict]) -> list[str]:
    """v1: pull the latest TodoWrite tool input's pending/in-progress items, if present."""
    todos: list[str] = []
    for m in reversed(messages):
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "TodoWrite":
                for item in b.get("input", {}).get("todos", []):
                    if item.get("status") in ("pending", "in_progress"):
                        todos.append(item.get("content", "")[:120])
                if todos:
                    return todos[:10]
    return todos


def _derive_directives(messages: list[dict]) -> list[str]:
    """v1: user-directive memories aren't in-window; use CLAUDE.md critical lines.

    Reuse the same enforcement-marker scan the digest injection already uses.
    """
    from pathlib import Path
    out: list[str] = []
    for candidate in ("CLAUDE.md", ".claude/CLAUDE.md"):
        p = Path(candidate)
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if any(kw in s.upper() for kw in
                       ("MUST NEVER", "NEVER ", "MUST ALWAYS", "CRITICAL:", "IMPORTANT:")):
                    if len(s) > 10 and not s.startswith("#"):
                        out.append(s[:120])
                        if len(out) >= 8:
                            return out
        except OSError:
            pass
    return out


def apply_memory_tail(messages: list[dict]) -> list[dict]:
    """Append the regenerated northstar/todo/directives/stubs tail block.

    Best-effort and guarded by COZEMPIC_MEMORY_OFF. Returns messages unchanged on opt-out
    or any error.
    """
    import os
    from ._validation import parse_env_bool
    if parse_env_bool("COZEMPIC_MEMORY_OFF", default=False, warn=False):
        return messages
    try:
        from .memory import tail, stubs
        northstar = _derive_northstar(messages)
        todos = _derive_todos(messages)
        directives = _derive_directives(messages)
        stub_list = stubs.relevant_stubs(northstar or "current session", k=7)
        return tail.compose_tail(messages, northstar, todos, directives, stub_list)
    except Exception:
        return messages
```

- [ ] **Step 5: Call `apply_memory_tail` in the write path**

In the function found in Step 1, immediately before the pruned dicts are handed to
`save_messages`, transform the plain-dict list through `apply_memory_tail`. If the executor
holds `Message` tuples, map to dicts, apply, and (since the tail is appended at the end with
fresh indices) re-wrap — follow the existing pattern in that function for constructing the
list passed to `save_messages`. Show the actual edit in the diff; do not leave it abstract.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/memory/test_executor_tail.py tests/test_executor.py -v`
Expected: PASS (existing executor tests still green + 2 new)

- [ ] **Step 7: Commit**

```bash
git add src/cozempic/executor.py tests/memory/test_executor_tail.py
git commit -m "feat(executor): append northstar/todo/stub tail block on prune"
```

---

### Task 12: Retire regex extraction + one-shot migration

**Files:**
- Modify: `src/cozempic/digest.py` (remove `extract_corrections` and its heuristic helpers; keep `_sanitize_for_injection`, `load_digest_store`, `_get_memdir`, injection plumbing)
- Modify: `src/cozempic/cli.py` (`cmd_digest`: `update` action now no-ops with a deprecation note; add a `migrate` action)
- Create: `src/cozempic/memory/migrate.py`
- Test: `tests/memory/test_migrate.py`

Existing active digest rules become `user-directive` memories once, then the JSON is retired.
`extract_corrections` (the regex path) is deleted; the update hook stops calling it.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_migrate.py
from cozempic.memory import migrate
from cozempic.memory.insight import TrustClass


def test_rule_becomes_user_directive_insight():
    ins = migrate._rule_to_insight("r1", "Never force-push to main")
    assert ins.trust_class is TrustClass.USER_DIRECTIVE
    assert ins.type == "feedback"
    assert "force-push" in ins.body
    assert ins.slug and " " not in ins.slug     # kebab slug


def test_migrate_persists_all_active_rules(monkeypatch):
    class _Rule:
        def __init__(self, rid, rule):
            self.id, self.rule = rid, rule

    class _Store:
        def active_rules(self):
            return [_Rule("r1", "Never force-push"), _Rule("r2", "Prefer uv")]

    monkeypatch.setattr(migrate, "load_digest_store", lambda: _Store())
    persisted = {}
    monkeypatch.setattr(migrate, "persist_insights",
                        lambda sid, items: persisted.update(n=len(items)) or [i.slug for i, _ in items])
    n = migrate.migrate_digest_rules("migration")
    assert n == 2
    assert persisted["n"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/memory/test_migrate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cozempic.memory.migrate'`

- [ ] **Step 3: Write the migration module**

```python
# src/cozempic/memory/migrate.py
"""One-shot migration: existing behavioral-digest active rules -> user-directive memories."""

from __future__ import annotations

import re

from ..digest import load_digest_store
from .insight import Insight, TrustClass
from .ledger import span_hash
from .mem_bridge import persist_insights


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:60] or "rule").rstrip("-")


def _rule_to_insight(rule_id: str, rule_text: str) -> Insight:
    return Insight(
        slug=_slugify(rule_text),
        title=rule_text[:60],
        description=rule_text[:120],
        type="feedback",
        trust_class=TrustClass.USER_DIRECTIVE,
        body=rule_text,
    )


def migrate_digest_rules(session_id: str) -> int:
    """Persist all active digest rules as memories. Returns count persisted."""
    store = load_digest_store()
    rules = store.active_rules()
    if not rules:
        return 0
    items = []
    for r in rules:
        ins = _rule_to_insight(r.id, r.rule)
        items.append((ins, span_hash([{"migrated_rule": r.id, "text": r.rule}])))
    persist_insights(session_id, items)
    return len(items)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/memory/test_migrate.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Remove the regex extractor and stop calling it**

In `src/cozempic/digest.py`: delete `extract_corrections` (line ~416) and the heuristic-only
helpers it solely uses (`_to_prohibition`, `_infer_scope`, `classify_turn`, `_is_system_noise` —
verify no other caller with `grep -n "_to_prohibition\|classify_turn\|_infer_scope\|extract_corrections\|_is_system_noise" src/cozempic/`). Keep everything the injector still uses.

In `src/cozempic/cli.py` `cmd_digest`: change the `update` branch to print a one-line
deprecation (`"digest update is retired; memory is now extracted via the memory subsystem"`)
and add a `migrate` branch calling `migrate.migrate_digest_rules(...)`. Add `"migrate"` to the
action `choices` in the argparse setup (search `choices=["show", "update"`).

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS. `test_digest.py` cases that asserted regex extraction behavior will fail — delete or rewrite those specific cases to assert the new deprecation/no-op (they test removed behavior). Do not weaken unrelated assertions.

- [ ] **Step 7: Commit**

```bash
git add src/cozempic/digest.py src/cozempic/cli.py src/cozempic/memory/migrate.py tests/memory/test_migrate.py tests/test_digest.py
git commit -m "refactor(digest): retire regex extraction; migrate rules to memories"
```

---

### Task 13: Full-suite green + smoke check

**Files:** none (verification task)

- [ ] **Step 1: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: all pass. Investigate and fix any failure at root cause — do not skip tests.

- [ ] **Step 2: Import smoke check**

Run: `python -c "import cozempic.memory.extract, cozempic.memory.mem_bridge, cozempic.memory.schedule, cozempic.memory.tail, cozempic.memory.stubs, cozempic.strategies.recoverability; print('imports ok')"`
Expected: `imports ok`

- [ ] **Step 3: Registry smoke check**

Run: `python -c "from cozempic.registry import PRESCRIPTIONS, STRATEGIES; assert 'recoverability' in STRATEGIES; assert PRESCRIPTIONS['aggressive'][0]=='recoverability'; print('registry ok')"`
Expected: `registry ok`

- [ ] **Step 4: Commit any fixes**

```bash
git add -A
git commit -m "test: full-suite green for memory overhaul"
```

---

## Self-Review Notes

- **Spec coverage:** F1 → Tasks 3,4; F1a (early/background) → Task 8 + 10; F2 → Task 5; F3 → Tasks 6,11; F4 → Tasks 7,11; F5 → Task 4 (trust taxonomy, world-fact drop) + Task 5 (verbatim body); F6 → Tasks 2,9. Digest teardown → Task 12.
- **Interface consistency:** `Insight`/`TrustClass` (Task 1) used unchanged in 4,5,8,12. `span_hash` defined once (Task 2), imported in 5,8,9,12. `persist_insights(session_id, list[(Insight, span_hash)]) -> list[str]` signature identical across 5,8,12. `TAIL_MARKER`/`_text_of`/`compose_tail` (Task 7) reused in 11.
- **Grounding gaps flagged for the implementer:** Tasks 10 and 11 begin with a "read the current code" step because the exact fraction/session-id variable in `cmd_nudge` and the write-path variable in `executor.py` must be read from source, not assumed. The wiring code is fully specified; only the local variable names are to be confirmed on site.
- **Opt-out:** every wired-in behavior (10, 11) honors `COZEMPIC_MEMORY_OFF` and is exception-swallowing so memory work can never break pruning or hooks.
