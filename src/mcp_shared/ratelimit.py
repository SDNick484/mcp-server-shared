"""Per-target leaky/token bucket rate limiting.

Shared by the sibling servers, byte for byte. Change it in mcp-server-shared,
not in a copy, then sync (`python scripts/sync_shared.py --update`).

Why rate-limit in hardware servers: consumer devices and their bridges (Onkyo
eISCP, Harmony hubs, WebOS TV websocket, Pixelcade) will drop commands or drop
the connection if an LLM calls them in a tight loop.

Each bucket tracks "tokens". Taking an action costs tokens; tokens leak back in
over time up to a capacity. If a call needs more tokens than are available, it
can sleep until enough have leaked back (or raise refusal if it would wait too
long).
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class RateLimitExceeded(Exception):
    """Raised when an operation would exceed the configured wait threshold."""


def refusal(what: str, wait: float) -> str:
    """Format an LLM-friendly message for a rate-limit refusal.

    Use this when returning structured errors or raising exceptions the model
    will see: it tells the model how long to wait and what failed.
    """
    if wait == float("inf"):
        return f"rate limit exceeded for {what}: requested amount exceeds bucket capacity; reduce count or split requests"
    return f"rate limit exceeded for {what}: retry after {wait:.1f}s"


class TokenBucket:
    """A thread-safe-ish (asyncio single-loop) token bucket rate limiter.

    Capacity is the burst size; rate is the tokens refilled per second.
    Initially full.
    """

    def __init__(self, capacity: float, rate: float) -> None:
        if capacity <= 0 or not math.isfinite(capacity):
            raise ValueError(f"capacity must be positive and finite, got {capacity}")
        if rate <= 0 or not math.isfinite(rate):
            raise ValueError(f"rate must be positive and finite, got {rate}")
        self.capacity = float(capacity)
        self.rate = float(rate)
        self.tokens = float(capacity)
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        elapsed = now - self.last_update
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_update = now

    def available(self) -> float:
        """Current tokens available without waiting."""
        now = time.monotonic()
        self._refill(now)
        return self.tokens

    def wait_time(self, n: float = 1.0) -> float:
        """How many seconds until `n` tokens would be available.

        Returns 0.0 if already available. Returns infinity if `n > capacity`.
        """
        if n > self.capacity:
            return float("inf")
        avail = self.available()
        if n <= avail:
            return 0.0
        return (n - avail) / self.rate

    def take(self, n: float = 1.0) -> bool:
        """Non-blocking: take `n` tokens if available, returning True.

        Returns False and takes nothing if fewer than `n` tokens are available.
        """
        if n < 0 or math.isnan(n):
            raise ValueError(f"cannot take negative tokens: {n}")
        if n == 0:
            return True
        now = time.monotonic()
        self._refill(now)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    async def acquire(self, n: float = 1.0, timeout: float | None = None) -> float:
        """Wait until `n` tokens are available, take them, and return seconds waited.

        If `timeout` is given and the wait would exceed it, raises
        RateLimitExceeded without consuming any tokens.

        Raises ValueError if `n > capacity` (cannot ever be satisfied).
        """
        if n < 0 or math.isnan(n):
            raise ValueError(f"cannot take negative tokens: {n}")
        if n > self.capacity:
            raise ValueError(f"requested {n} tokens exceeds bucket capacity {self.capacity}")
        if n == 0:
            return 0.0

        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                self._refill(now)
                if self.tokens >= n:
                    self.tokens -= n
                    return waited
                wait = (n - self.tokens) / self.rate

            if timeout is not None and waited + wait > timeout:
                raise RateLimitExceeded(f"would have to wait {wait:.2f}s, exceeding timeout of {timeout:.2f}s")

            # Sleep outside the lock so other tasks can inspect/try
            sleep_for = max(0.001, wait)
            await asyncio.sleep(sleep_for)
            waited += sleep_for

    def refund(self, n: float = 1.0) -> None:
        """Put tokens back (e.g. after a failed request that didn't hit hardware).

        Capped at capacity.
        """
        if n < 0 or not math.isfinite(n):
            raise ValueError(f"cannot refund negative or non-finite tokens: {n}")
        now = time.monotonic()
        self._refill(now)
        self.tokens = min(self.capacity, self.tokens + n)

    @asynccontextmanager
    async def reserve(
        self, n: float = 1.0, timeout: float | None = None, refund_on_error: bool = False
    ) -> AsyncIterator[float]:
        """Context manager that acquires `n` tokens, optionally refunding on exception.

        Yields the seconds waited.

        Example:
            async with bucket.reserve(1.0, timeout=2.0, refund_on_error=True):
                await hardware.send(cmd)
        """
        waited = await self.acquire(n, timeout=timeout)
        try:
            yield waited
        except Exception:
            if refund_on_error:
                self.refund(n)
            raise


class KeyedTokenBucket:
    """A map of token buckets by key (e.g. hub name, receiver IP, TV address).

    Buckets are created on first access using a factory (capacity, rate).
    """

    def __init__(self, capacity: float, rate: float) -> None:
        if capacity <= 0 or not math.isfinite(capacity):
            raise ValueError(f"capacity must be positive and finite, got {capacity}")
        if rate <= 0 or not math.isfinite(rate):
            raise ValueError(f"rate must be positive and finite, got {rate}")
        self.capacity = float(capacity)
        self.rate = float(rate)
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()

    def get(self, key: str) -> TokenBucket:
        """Get or create the bucket for `key`."""
        b = self._buckets.get(key)
        if b is None:
            b = TokenBucket(self.capacity, self.rate)
            self._buckets[key] = b
        return b

    def available(self, key: str) -> float:
        return self.get(key).available()

    def wait_time(self, key: str, n: float = 1.0) -> float:
        return self.get(key).wait_time(n)

    def take(self, key: str, n: float = 1.0) -> bool:
        return self.get(key).take(n)

    async def acquire(self, key: str, n: float = 1.0, timeout: float | None = None) -> float:
        return await self.get(key).acquire(n, timeout)

    def refund(self, key: str, n: float = 1.0) -> None:
        self.get(key).refund(n)

    @asynccontextmanager
    async def reserve(
        self, key: str, n: float = 1.0, timeout: float | None = None, refund_on_error: bool = False
    ) -> AsyncIterator[float]:
        """``TokenBucket.reserve`` on ``key``'s bucket."""
        async with self.get(key).reserve(n, timeout=timeout, refund_on_error=refund_on_error) as waited:
            yield waited
