"""統一非同步重試裝飾器

用法：
    from backend.utils.retry import retry

    @retry(max_attempts=3, delay=2.0)
    async def fetch_data(url: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

錯誤策略：
    - 429 Too Many Requests  → 等待 delay_429（預設 10s）再重試
    - 502 / 503              → 等待 delay_50x（預設 5s）再重試
    - 連線逾時 / 網路錯誤     → 等待 delay（預設 2s）再重試
    - 404 Not Found          → 不重試，直接 raise
    - 其餘例外               → 等待 delay 後重試
    - 超過 max_attempts      → raise 最後一個例外
"""
from __future__ import annotations

import asyncio
import functools
import json
from typing import Callable

import httpx
from loguru import logger


def retry(
    max_attempts: int = 3,
    delay: float = 2.0,
    *,
    delay_429: float = 10.0,
    delay_50x: float = 5.0,
) -> Callable:
    """
    Async retry decorator.

    Args:
        max_attempts: 最多嘗試次數（含第一次，預設 3）
        delay:        一般錯誤等待秒數（預設 2s）
        delay_429:    429 Rate Limit 等待秒數（預設 10s）
        delay_50x:    502/503 等待秒數（預設 5s）
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)

                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status in (404, 422):
                        raise                          # 404/422 → 無此資源/無效請求，不重試
                    if attempt >= max_attempts:
                        raise
                    wait = (
                        delay_429 if status == 429
                        else delay_50x if status in (502, 503)
                        else delay
                    )
                    logger.warning(
                        "[retry] {} HTTP {} — 第{}次重試，等待{:.0f}s",
                        func.__name__, status, attempt, wait,
                    )
                    await asyncio.sleep(wait)
                    last_exc = exc

                except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
                    if attempt >= max_attempts:
                        raise
                    logger.warning(
                        "[retry] {} 連線逾時/失敗 — 第{}次重試，等待{:.0f}s",
                        func.__name__, attempt, delay,
                    )
                    await asyncio.sleep(delay)
                    last_exc = exc

                except json.JSONDecodeError:
                    raise  # server returned non-JSON (e.g. HTML error page) — retrying won't help

                except Exception as exc:
                    if attempt >= max_attempts:
                        raise
                    logger.warning(
                        "[retry] {} 第{}次重試 ({})",
                        func.__name__, attempt, type(exc).__name__,
                    )
                    await asyncio.sleep(delay)
                    last_exc = exc

            # unreachable: loop always raises or returns above
            if last_exc is not None:
                raise last_exc
        return wrapper
    return decorator
