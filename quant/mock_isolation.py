"""
mock_isolation.py — Mock / Live 資料隔離層

環境變數控制：
  ENV=production  → 完全禁止 mock data（會 raise）
  ENV=staging     → 允許 mock，但強制標記
  ENV=development → 允許 mock（預設）

Railway 生產環境請設定 ENV=production
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ── 環境判斷 ──────────────────────────────────────────────────────────────────
ENV = os.getenv("ENV", "development").lower()
IS_PRODUCTION = ENV == "production"
IS_STAGING    = ENV == "staging"
IS_DEV        = not (IS_PRODUCTION or IS_STAGING)


class ProductionMockDataError(RuntimeError):
    """當 production 環境偵測到 mock 資料時拋出"""
    pass


def assert_no_mock(data: Any, context: str = ""):
    """
    在 production 環境中，若資料帶有 is_mock=True 則立即 raise。
    staging/dev 環境只記錄警告。
    """
    is_mock = False

    if hasattr(data, "is_mock"):
        is_mock = bool(data.is_mock)
    elif isinstance(data, dict):
        is_mock = bool(data.get("is_mock", False))
    elif isinstance(data, list):
        is_mock = any(
            (hasattr(d, "is_mock") and d.is_mock) or
            (isinstance(d, dict) and d.get("is_mock", False))
            for d in data
        )

    if not is_mock:
        return

    msg = f"[MockIsolation] Mock data detected in {context or 'unknown'} (ENV={ENV})"

    if IS_PRODUCTION:
        logger.critical(msg)
        raise ProductionMockDataError(msg)

    if IS_STAGING:
        logger.warning(msg + " [STAGING: allowed but flagged]")
    else:
        logger.debug(msg + " [DEV: allowed]")


def check_mock_list(items: list, context: str = "") -> tuple[int, int]:
    """
    掃描列表，回傳 (total, mock_count)。
    若 production 且 mock_count > 0 → raise。
    """
    mock_count = sum(
        1 for i in items
        if (hasattr(i, "is_mock") and i.is_mock) or
           (isinstance(i, dict) and i.get("is_mock", False))
    )

    if mock_count > 0:
        msg = f"[MockIsolation] {mock_count}/{len(items)} mock items in {context}"
        if IS_PRODUCTION:
            logger.critical(msg)
            raise ProductionMockDataError(msg)
        logger.warning(msg)

    return len(items), mock_count


def wrap_with_mock_check(fn):
    """
    裝飾器：在 production 環境中，函數回傳值若含 mock 則 raise。
    用於 decision_engine 等關鍵函數。
    """
    import functools

    @functools.wraps(fn)
    async def async_wrapper(*args, **kwargs):
        result = await fn(*args, **kwargs)
        if IS_PRODUCTION:
            assert_no_mock(result, context=fn.__name__)
        return result

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        if IS_PRODUCTION:
            assert_no_mock(result, context=fn.__name__)
        return result

    import asyncio
    if asyncio.iscoroutinefunction(fn):
        return async_wrapper
    return sync_wrapper


def env_info() -> dict:
    """回傳目前環境資訊（供 /api/data-status 使用）"""
    return {
        "env":            ENV,
        "is_production":  IS_PRODUCTION,
        "is_staging":     IS_STAGING,
        "mock_allowed":   not IS_PRODUCTION,
    }
