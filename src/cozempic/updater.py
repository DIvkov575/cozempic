"""Auto-update: check PyPI once per day and upgrade in-place if a newer version is available."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import __version__

_PYPI_URL = "https://pypi.org/pypi/cozempic/json"
_COUNTER_URL = "https://cozempic-counters.counterapi-ruya.workers.dev/counter/auto_updates/up"
_INSTALL_COUNTER_URL = "https://cozempic-counters.counterapi-ruya.workers.dev/counter/installs/up"
_CHECK_INTERVAL = 86400  # 24 hours
_CACHE_FILE = Path.home() / ".cozempic_update_check"
_INSTALL_SENTINEL = Path.home() / ".cozempic_installed"


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0,)


def _get_latest_version() -> str | None:
    try:
        req = Request(_PYPI_URL, headers={"User-Agent": f"cozempic/{__version__}"})
        with urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read())
        return data["info"]["version"]
    except Exception:
        return None


def _should_check() -> bool:
    try:
        if _CACHE_FILE.exists():
            last = float(_CACHE_FILE.read_text().strip())
            if time.time() - last < _CHECK_INTERVAL:
                return False
    except Exception:
        pass
    return True


def _mark_checked() -> None:
    try:
        _CACHE_FILE.write_text(str(time.time()))
    except Exception:
        pass


def _install_method() -> str:
    """Best-effort detection of HOW cozempic was installed, so we pick an upgrade
    mechanism that actually works. Homebrew kegs and `uv tool` installs cannot be
    upgraded by pip — the running binary never moves — which is why a brew/uvx
    install silently stays behind on the pip-based auto-updater.

    Returns one of: "brew", "uv-tool", "pipx", "pip".
    """
    try:
        path = str(Path(__file__).resolve()).replace("\\", "/").lower()
    except Exception:
        path = ""
    if "/cellar/cozempic/" in path:          # Homebrew keg (any prefix)
        return "brew"
    if "/uv/tools/cozempic/" in path:        # `uv tool install cozempic`
        return "uv-tool"
    if "/pipx/venvs/cozempic/" in path:      # `pipx install cozempic`
        return "pipx"
    return "pip"


def _upgrade_hint(method: str | None = None) -> str:
    """The correct manual upgrade command for the detected install method."""
    return {
        # Fully-qualified so it doesn't trip Homebrew's untrusted-tap gate on
        # upgrade (a bare `brew upgrade cozempic` must load the whole non-official
        # tap → "Refusing to load formula … from untrusted tap"); the qualified
        # name trusts just this formula inline.
        "brew": "brew upgrade Ruya-AI/cozempic/cozempic",
        "uv-tool": "uv tool upgrade cozempic",
        "pipx": "pipx upgrade cozempic",
    }.get(method or _install_method(), "pip install --upgrade cozempic")


def _do_upgrade(latest: str) -> bool:
    """Upgrade cozempic using the mechanism that matches the install method.

    brew is intentionally NOT auto-run (it needs a tap refresh and can be slow /
    interactive — wrong to fire from a SessionStart hook); the caller surfaces an
    accurate `brew upgrade cozempic` hint instead. uv-tool/pipx get their proper
    upgrade command. pip/uv-pip envs use the in-place install chain.
    """
    method = _install_method()
    if method == "brew":
        return False  # can't safely auto-upgrade a keg; caller prints the hint
    if method == "uv-tool":
        if shutil.which("uv"):
            try:
                r = subprocess.run(["uv", "tool", "upgrade", "cozempic"],
                                   capture_output=True, timeout=120)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False
    if method == "pipx":
        if shutil.which("pipx"):
            try:
                r = subprocess.run(["pipx", "upgrade", "cozempic"],
                                   capture_output=True, timeout=120)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False
    # method == "pip": in-place install into the managed env.
    # Try uv pip install first (works in uv-managed environments)
    if shutil.which("uv"):
        try:
            result = subprocess.run(
                ["uv", "pip", "install", f"cozempic=={latest}", "--quiet"],
                capture_output=True,
                timeout=60,
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

    # Try pip via sys.executable (works in pip-managed venvs)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", f"cozempic=={latest}",
             "--quiet", "--disable-pip-version-check"],
            capture_output=True,
            timeout=60,
        )
        if result.returncode == 0:
            return True
    except Exception:
        pass

    # Try bare pip (works when pip is on PATH but not in current venv)
    if shutil.which("pip"):
        try:
            result = subprocess.run(
                ["pip", "install", f"cozempic=={latest}",
                 "--quiet", "--disable-pip-version-check"],
                capture_output=True,
                timeout=60,
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

    return False


def ping_install_if_new() -> None:
    """Ping counters once per installed version.

    Re-pings when the version in the sentinel doesn't match the running version.
    If the sentinel existed with a DIFFERENT version (upgrade, not first install),
    also pings the auto-update counter — this catches upgrades from the SessionStart
    hook and npm install.js which bypass the Python auto-updater.
    """
    try:
        is_upgrade = False
        if _INSTALL_SENTINEL.exists():
            old_version = _INSTALL_SENTINEL.read_text().strip()
            if old_version == __version__:
                return
            # Sentinel exists with different version = upgrade (not first install)
            is_upgrade = bool(old_version)
        _INSTALL_SENTINEL.write_text(__version__)
        if os.environ.get("COZEMPIC_NO_TELEMETRY"):
            return
        urlopen(Request(_INSTALL_COUNTER_URL, headers={"User-Agent": f"cozempic/{__version__}"}), timeout=3)
        if is_upgrade:
            urlopen(Request(_COUNTER_URL, headers={"User-Agent": f"cozempic/{__version__}"}), timeout=3)
    except Exception:
        pass


def maybe_auto_update(force: bool = False, silent: bool = False) -> None:
    """Check PyPI and auto-update cozempic if a newer version is available.

    Throttled to one check per 24 hours. No-ops silently on network failures.

    Args:
        force: Bypass the TTY check (for guard daemon and MCP server startup).
        silent: Suppress all output (required for MCP context where stdout is the protocol stream).

    Set COZEMPIC_NO_AUTO_UPDATE=1 to disable all automatic upgrade behaviour.
    """
    if os.environ.get("COZEMPIC_NO_AUTO_UPDATE"):
        return
    # Removed TTY check — auto-update should work from hooks, daemons, and CLI.
    # The 24h throttle and silent mode are sufficient controls.
    if not _should_check():
        return

    _mark_checked()

    latest = _get_latest_version()
    if latest is None:
        return
    if _version_tuple(latest) <= _version_tuple(__version__):
        return

    method = _install_method()
    # Homebrew kegs can't be auto-upgraded in place — don't claim we're "updating".
    if method == "brew":
        if not silent:
            print(f"  Cozempic: v{latest} available — run: {_upgrade_hint('brew')} "
                  f"(Homebrew installs don't auto-update).", flush=True)
        return

    if not silent:
        print(f"  Cozempic: updating {__version__} → {latest}...", flush=True)
    if _do_upgrade(latest):
        if not os.environ.get("COZEMPIC_NO_TELEMETRY"):
            try:
                urlopen(Request(_COUNTER_URL, headers={"User-Agent": f"cozempic/{latest}"}), timeout=3)
            except Exception:
                pass
        if not silent:
            # The current Python process is still running v{__version__} code —
            # new code is active on next invocation. Say so explicitly so
            # users don't think the upgrade failed when --version still prints
            # the old number.
            print(f"  Cozempic: updated to v{latest} — active on next run (this process still v{__version__}).", flush=True)
    else:
        if not silent:
            print(f"  Cozempic: auto-update failed. Run: {_upgrade_hint(method)}", flush=True)
