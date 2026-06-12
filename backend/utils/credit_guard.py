"""Shared Anthropic API credit circuit breaker.

One module-level flag for all AI services — the first 402 propagates instantly
so subsequent services skip their API calls without needing their own round-trip.
"""
import time

_exhausted_at: float = 0.0   # 0 = credits OK


def is_exhausted() -> bool:
    return _exhausted_at > 0.0


def mark_exhausted() -> None:
    global _exhausted_at
    _exhausted_at = time.time()


def reset() -> None:
    """Call when credits are restored (new billing period / top-up)."""
    global _exhausted_at
    _exhausted_at = 0.0
