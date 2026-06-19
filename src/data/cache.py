"""Disk-backed cache wrapper.

Wraps ``diskcache.Cache`` with a simple async ``cached`` decorator so the
data wrappers (yfinance, Finnhub, Stocktwits) can opt into TTL-based caching
without each rebuilding the same scaffolding.

Why diskcache (vs. in-memory) — Finnhub free tier is 60 calls/min and we
plan to share results across processes (CLI + Streamlit). On-disk + JSON
serializable means cache survives restarts and is easy to inspect.
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import diskcache

from src.config import get_settings

P = ParamSpec("P")
R = TypeVar("R")


_CACHE: diskcache.Cache | None = None


def get_cache() -> diskcache.Cache:
    global _CACHE
    if _CACHE is None:
        settings = get_settings()
        Path(settings.cache_dir).mkdir(parents=True, exist_ok=True)
        _CACHE = diskcache.Cache(settings.cache_dir)
    return _CACHE


def make_key(namespace: str, *parts: Any) -> str:
    """Stable cache key. Hashes complex args to a short fingerprint so keys
    stay sane regardless of input size."""
    normalized = json.dumps(parts, sort_keys=True, default=str)
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:16]
    return f"{namespace}:{digest}"


def cached(
    namespace: str,
    ttl_seconds: int,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator: cache async function results on disk for ``ttl_seconds``.

    Cache key is namespace + a hash of all positional + keyword args. Pass
    only JSON-serializable args; otherwise the digest will be lossy."""

    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            cache = get_cache()
            key = make_key(namespace, *args, sorted(kwargs.items()))
            hit = cache.get(key, default=_MISSING)
            if hit is not _MISSING:
                return hit  # type: ignore[return-value]
            result = await fn(*args, **kwargs)
            cache.set(key, result, expire=ttl_seconds)
            return result

        return wrapper

    return decorator


_MISSING = object()


__all__ = ["get_cache", "make_key", "cached"]
