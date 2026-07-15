"""Fixed early-checkpoint tier (150K default): env resolution + below-soft gating."""

import os

import pytest

from cozempic.guard import _checkpoint_threshold_tokens
from cozempic.tokens import DEFAULT_CHECKPOINT_TOKENS, default_token_thresholds_4tier


@pytest.fixture(autouse=True)
def _clean_env():
    prior = os.environ.pop("COZEMPIC_CHECKPOINT_TOKENS", None)
    yield
    if prior is None:
        os.environ.pop("COZEMPIC_CHECKPOINT_TOKENS", None)
    else:
        os.environ["COZEMPIC_CHECKPOINT_TOKENS"] = prior


def _resolve(context_window, env=None):
    """Mirror start_guard's checkpoint resolution: default → env → below-soft gate."""
    if env is not None:
        os.environ["COZEMPIC_CHECKPOINT_TOKENS"] = env
    soft, _hard1, _hard2 = default_token_thresholds_4tier(context_window)
    cp = _checkpoint_threshold_tokens()
    if cp is None:
        cp = DEFAULT_CHECKPOINT_TOKENS
    if not (cp and soft and cp < soft):
        cp = None
    return cp


def test_default_active_on_1m_window():
    # soft = 250K, so a fixed 150K checkpoint sits below it and is active.
    assert _resolve(1_000_000) == 150_000


def test_disabled_on_small_window():
    # soft = 50K on a 200K window; 150K is above it → checkpoint disabled.
    assert _resolve(200_000) is None


def test_env_zero_disables():
    assert _resolve(1_000_000, env="0") is None


def test_env_override_below_soft():
    assert _resolve(1_000_000, env="100000") == 100_000


def test_env_override_above_soft_is_gated_off():
    # 300K > soft 250K → gated off even though explicitly set.
    assert _resolve(1_000_000, env="300000") is None


def test_env_garbage_falls_back_to_default():
    assert _resolve(1_000_000, env="not-a-number") == 150_000


def test_env_negative_falls_back_to_default():
    assert _resolve(1_000_000, env="-5") == 150_000
