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


def test_spawn_closes_stdin_after_write(monkeypatch):
    events = []

    class _FakeStdin:
        def write(self, b):
            events.append(("write", b))

        def close(self):
            events.append(("close",))

    class _FakeProc:
        stdin = _FakeStdin()

    monkeypatch.setattr(schedule.subprocess, "Popen", lambda *a, **k: _FakeProc())
    schedule._spawn("s1", [{"role": "user", "content": "x"}])
    assert [e[0] for e in events] == ["write", "close"]
