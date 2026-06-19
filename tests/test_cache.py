"""Cache primitive tests — no network."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import diskcache
import pytest

import src.data.cache as cache_mod
from src.data.cache import cached, make_key


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch):
    """Each test gets a fresh diskcache directory; never shared across tests."""
    with tempfile.TemporaryDirectory() as tmp:
        c = diskcache.Cache(tmp)
        monkeypatch.setattr(cache_mod, "_CACHE", c)
        yield c
        c.close()


def test_make_key_is_stable():
    a = make_key("ns", "AAPL", 5, {"period": "6mo"})
    b = make_key("ns", "AAPL", 5, {"period": "6mo"})
    assert a == b
    assert a.startswith("ns:")


def test_make_key_changes_with_args():
    a = make_key("ns", "AAPL")
    b = make_key("ns", "MSFT")
    assert a != b


async def test_cached_returns_value(_isolated_cache):
    calls = {"n": 0}

    @cached(namespace="t", ttl_seconds=60)
    async def fetch(x: int) -> int:
        calls["n"] += 1
        return x * 2

    assert await fetch(3) == 6
    assert await fetch(3) == 6
    assert calls["n"] == 1, "second call should hit cache"


async def test_cached_distinguishes_args(_isolated_cache):
    @cached(namespace="t", ttl_seconds=60)
    async def fetch(x: int) -> int:
        return x * 2

    assert await fetch(3) == 6
    assert await fetch(4) == 8


async def test_cached_respects_ttl(_isolated_cache):
    calls = {"n": 0}

    @cached(namespace="t", ttl_seconds=1)
    async def fetch() -> str:
        calls["n"] += 1
        return f"v{calls['n']}"

    assert await fetch() == "v1"
    await asyncio.sleep(1.1)
    assert await fetch() == "v2", "expired entry must be re-fetched"


async def test_kwargs_normalize_into_key(_isolated_cache):
    calls = {"n": 0}

    @cached(namespace="t", ttl_seconds=60)
    async def fetch(*, ticker: str, period: str = "1mo") -> str:
        calls["n"] += 1
        return f"{ticker}:{period}"

    await fetch(ticker="AAPL", period="6mo")
    await fetch(period="6mo", ticker="AAPL")  # same kwargs, different order
    assert calls["n"] == 1, "kwargs order must not affect cache key"
