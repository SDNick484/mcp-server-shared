"""Tests for ratelimit.py.

Shared by the sibling servers along with ratelimit.py; only the package name
in the import below differs between repos. Change it in mcp-server-shared, not
in a copy, then sync.
"""

from __future__ import annotations

import math
import time

import pytest

from __PKG__.ratelimit import KeyedTokenBucket, RateLimitExceeded, TokenBucket, refusal


def test_bucket_init_validation():
    with pytest.raises(ValueError, match="capacity"):
        TokenBucket(0, 1.0)
    with pytest.raises(ValueError, match="capacity"):
        TokenBucket(-5, 1.0)
    with pytest.raises(ValueError, match="rate"):
        TokenBucket(5, 0)
    with pytest.raises(ValueError, match="rate"):
        TokenBucket(5, -1.0)
    with pytest.raises(ValueError, match="capacity must be positive and finite"):
        TokenBucket(float("inf"), 1.0)
    with pytest.raises(ValueError, match="capacity must be positive and finite"):
        TokenBucket(float("nan"), 1.0)
    with pytest.raises(ValueError, match="rate must be positive and finite"):
        TokenBucket(5.0, float("inf"))
    with pytest.raises(ValueError, match="rate must be positive and finite"):
        TokenBucket(5.0, float("nan"))


def test_keyed_bucket_init_validation():
    with pytest.raises(ValueError, match="capacity must be positive and finite"):
        KeyedTokenBucket(0, 1.0)
    with pytest.raises(ValueError, match="capacity must be positive and finite"):
        KeyedTokenBucket(float("inf"), 1.0)
    with pytest.raises(ValueError, match="capacity must be positive and finite"):
        KeyedTokenBucket(float("nan"), 1.0)
    with pytest.raises(ValueError, match="rate must be positive and finite"):
        KeyedTokenBucket(5.0, 0)
    with pytest.raises(ValueError, match="rate must be positive and finite"):
        KeyedTokenBucket(5.0, float("inf"))
    with pytest.raises(ValueError, match="rate must be positive and finite"):
        KeyedTokenBucket(5.0, float("nan"))


def test_bucket_starts_full():
    b = TokenBucket(10.0, 1.0)
    assert b.available() == 10.0
    assert b.wait_time(1.0) == 0.0
    assert b.wait_time(10.0) == 0.0


def test_wait_time_beyond_capacity_is_infinite():
    b = TokenBucket(5.0, 1.0)
    assert b.wait_time(6.0) == float("inf")


def test_take_non_blocking():
    b = TokenBucket(2.0, 1.0)
    assert b.take(1.0) is True
    assert b.take(1.0) is True
    assert b.take(1.0) is False  # empty
    assert b.take(0.0) is True  # zero always succeeds
    with pytest.raises(ValueError):
        b.take(-1.0)
    with pytest.raises(ValueError, match="cannot take negative"):
        b.take(float("nan"))


def test_refill_over_time():
    b = TokenBucket(2.0, 10.0)  # 10 tokens/sec
    assert b.take(2.0) is True
    assert b.take(1.0) is False
    time.sleep(0.12)  # ~1.2 tokens refilled
    assert b.available() >= 1.0
    assert b.take(1.0) is True


def test_refill_caps_at_capacity():
    b = TokenBucket(2.0, 100.0)
    time.sleep(0.05)
    assert b.available() == 2.0


def test_refund():
    b = TokenBucket(2.0, 1.0)
    b.take(2.0)
    assert b.available() < 0.5
    b.refund(1.0)
    assert b.available() >= 1.0
    # Refunding past capacity is capped
    b.refund(10.0)
    assert b.available() == 2.0
    with pytest.raises(ValueError):
        b.refund(-1.0)
    with pytest.raises(ValueError, match="cannot refund negative or non-finite"):
        b.refund(float("inf"))
    with pytest.raises(ValueError, match="cannot refund negative or non-finite"):
        b.refund(float("nan"))


@pytest.mark.anyio
async def test_acquire_immediate():
    b = TokenBucket(5.0, 1.0)
    waited = await b.acquire(2.0)
    assert waited == 0.0
    assert b.available() <= 3.01


@pytest.mark.anyio
async def test_acquire_waits():
    b = TokenBucket(1.0, 20.0)  # 20 tokens/sec = 50ms per token
    b.take(1.0)
    waited = await b.acquire(1.0)
    assert waited > 0.03
    assert waited < 0.2


@pytest.mark.anyio
async def test_acquire_timeout():
    b = TokenBucket(1.0, 0.1)  # 1 token every 10s
    b.take(1.0)
    with pytest.raises(RateLimitExceeded):
        await b.acquire(1.0, timeout=0.05)


@pytest.mark.anyio
async def test_acquire_beyond_capacity_raises_value_error():
    b = TokenBucket(2.0, 1.0)
    with pytest.raises(ValueError, match="exceeds bucket capacity"):
        await b.acquire(3.0)
    with pytest.raises(ValueError, match="cannot take negative"):
        await b.acquire(-1.0)
    with pytest.raises(ValueError, match="cannot take negative"):
        await b.acquire(float("nan"))


@pytest.mark.anyio
async def test_acquire_zero_returns_zero():
    b = TokenBucket(2.0, 1.0)
    b.take(2.0)
    assert await b.acquire(0.0) == 0.0


@pytest.mark.anyio
async def test_reserve_context_manager_success():
    b = TokenBucket(2.0, 1.0)
    async with b.reserve(1.0) as waited:
        assert waited == 0.0
    assert b.available() <= 1.01


@pytest.mark.anyio
async def test_reserve_context_manager_refund_on_error():
    b = TokenBucket(2.0, 1.0)
    with pytest.raises(RuntimeError):
        async with b.reserve(1.0, refund_on_error=True):
            raise RuntimeError("hardware call failed")
    # Tokens were refunded
    assert b.available() >= 1.9


@pytest.mark.anyio
async def test_reserve_context_manager_no_refund_on_error():
    b = TokenBucket(2.0, 0.01)  # very slow refill
    with pytest.raises(RuntimeError):
        async with b.reserve(1.0, refund_on_error=False):
            raise RuntimeError("hardware call failed")
    # Tokens were NOT refunded
    assert b.available() < 1.1


def test_keyed_bucket_independent():
    kb = KeyedTokenBucket(2.0, 1.0)
    assert kb.take("tv1", 2.0) is True
    assert kb.take("tv1", 1.0) is False
    assert kb.take("tv2", 2.0) is True  # independent


@pytest.mark.anyio
async def test_keyed_bucket_acquire_and_refund():
    kb = KeyedTokenBucket(2.0, 10.0)
    waited = await kb.acquire("h1", 1.0)
    assert waited == 0.0
    assert kb.available("h1") <= 1.05
    kb.refund("h1", 1.0)
    assert kb.available("h1") >= 1.95


@pytest.mark.anyio
async def test_keyed_reserve():
    kb = KeyedTokenBucket(2.0, 10.0)
    async with kb.reserve("tv1", 1.0) as waited:
        assert waited == 0.0
    assert kb.available("tv1") <= 1.05


def test_refusal_messages():
    msg = refusal("volume_up", 1.234)
    assert "volume_up" in msg
    assert "1.2s" in msg
    assert "retry after" in msg

    inf = refusal("bulk_send", float("inf"))
    assert "bulk_send" in msg
    assert "exceeds bucket capacity" in inf
