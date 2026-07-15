import subprocess
import types

from cozempic.memory import blobref, mem_bridge
from cozempic.memory.insight import Insight, TrustClass


def test_is_offload_eligible():
    big = "x" * 9000
    assert blobref.is_offload_eligible({"type": "text", "text": big}, min_bytes=8192) is True
    assert blobref.is_offload_eligible({"type": "text", "text": "short"}, min_bytes=8192) is False
    # thinking and image are NOT F8's job
    assert blobref.is_offload_eligible({"type": "thinking", "thinking": big}, min_bytes=8192) is False
    assert blobref.is_offload_eligible({"type": "image", "source": {}}, min_bytes=8192) is False
    # already a cozempic stub → not eligible
    assert blobref.is_offload_eligible({"type": "text", "text": "[cozempic asset: a — 9KB · recall s]"}, min_bytes=1) is False


def test_blob_text_of_handles_str_and_list_tool_result():
    assert blobref.blob_text_of({"type": "text", "text": "hello"}) == "hello"
    assert blobref.blob_text_of({"type": "tool_result", "content": "raw"}) == "raw"


def test_build_pointer_stub_shape():
    stub = blobref.build_pointer_stub(name="report.md", n_bytes=9216, slug="report-md-abc123")
    assert "cozempic asset" in stub
    assert "report.md" in stub
    assert "recall report-md-abc123" in stub
    assert "9" in stub  # KB shown


def test_offload_block_writes_reference_and_returns_slug(tmp_path, monkeypatch):
    part = tmp_path / "proj"; part.mkdir()
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: part)
    monkeypatch.setattr(mem_bridge, "_mymem_cmd", lambda: ["python3", "/x/mymem"])
    calls: list[list[str]] = []

    def _run(cmd, *a, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", _run)

    block = {"type": "text", "text": "y" * 9000}
    slug = blobref.offload_block(block, name="doc")
    assert slug is not None
    # the offload persisted as a `reference` insight, verbatim, via mymem save
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:3] == ["python3", "/x/mymem", "save"]
    assert cmd[cmd.index("--type") + 1] == "reference"
    assert "y" * 9000 in cmd[cmd.index("--content") + 1]   # verbatim


def test_offload_block_noop_when_unpartitioned(monkeypatch):
    monkeypatch.setattr(mem_bridge, "resolve_partition", lambda: None)
    assert blobref.offload_block({"type": "text", "text": "z" * 9000}, name="doc") is None
