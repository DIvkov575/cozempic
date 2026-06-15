"""Shared pytest fixtures.

Hermetic ``~/.claude``: point ``CLAUDE_CONFIG_DIR`` at a fresh temp dir for every
test so nothing reads the developer's real Claude config — in particular the
active-transcript store (``cozempic-active-sessions.json``) that
``find_current_session`` consults as Strategy 1. Without this, those tests
non-deterministically pick up the live session's real record (they patch
``get_projects_dir`` but not the active store), which is exactly the latent
non-hermetic-test leak the 1.8.30 detection work introduced.

A test that needs a specific config dir still overrides this — its own
``monkeypatch.setenv`` / ``patch("...get_claude_dir")`` wins.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_claude_config_dir(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("claude_home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    yield
