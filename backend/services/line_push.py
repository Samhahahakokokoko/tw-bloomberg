from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import httpx
from loguru import logger

from ..models.database import settings


PUSH_URL = "https://api.line.me/v2/bot/message/push"
MULTICAST_URL = "https://api.line.me/v2/bot/message/multicast"

# 月用量計數器（in-memory，重啟歸零）
_monthly_push_counts: dict[str, int] = defaultdict(int)

# 可重試的 LINE API 狀態碼（速率限制 / 暫時不可用）
_RETRYABLE_STATUS = {429, 502, 503}
_RETRY_DELAY = 3.0   # seconds
_MAX_RETRIES = 1     # 最多補試 1 次


def get_push_stats() -> dict[str, int]:
    """回傳各月的 push_message 累計次數（訊息則數，非對話數）"""
    return dict(_monthly_push_counts)


def _record_push(n_messages: int = 1) -> None:
    """成功推送後才計入月用量計數器。"""
    month_key = time.strftime("%Y-%m")
    _monthly_push_counts[month_key] += n_messages
    total = _monthly_push_counts[month_key]
    if total % 50 == 0:
        logger.info(f"[push_counter] {month_key}: {total} messages pushed this month")


def _mask_target(target: str) -> str:
    return f"{target[:8]}..." if target else "unknown"


def _n_messages_from_payload(payload: dict[str, Any]) -> int:
    msgs = payload.get("messages", [])
    target = payload.get("to")
    n_msgs = len(msgs) if isinstance(msgs, list) else 1
    n_users = len(target) if isinstance(target, list) else 1
    return n_msgs * n_users


async def _post_line_message(
    url: str,
    payload: dict[str, Any],
    *,
    token: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = 20,
    context: str = "line",
) -> bool:
    access_token = token or settings.line_channel_access_token
    if not access_token:
        logger.warning(f"[{context}] LINE push skipped: channel access token missing")
        return False

    headers = {"Authorization": f"Bearer {access_token}"}
    target = payload.get("to")
    target_label = f"{len(target)} users" if isinstance(target, list) else _mask_target(str(target or ""))

    owned_client = client is None
    active_client = client or httpx.AsyncClient(timeout=timeout)
    try:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await active_client.post(url, json=payload, headers=headers)
            except Exception as exc:
                logger.exception(f"[{context}] LINE push exception attempt={attempt+1}: {exc}")
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_DELAY)
                    continue
                return False

            if response.is_success:
                _record_push(_n_messages_from_payload(payload))
                return True

            if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                logger.warning(
                    f"[{context}] LINE push {response.status_code} (retrying in {_RETRY_DELAY}s) "
                    f"target={target_label}"
                )
                await asyncio.sleep(_RETRY_DELAY)
                continue

            logger.error(
                f"[{context}] LINE push failed target={target_label} "
                f"status={response.status_code} body={response.text[:500]}"
            )
            return False
        return False
    finally:
        if owned_client:
            await active_client.aclose()


async def push_line_messages(
    user_id: str,
    messages: Sequence[dict[str, Any]],
    *,
    token: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = 20,
    context: str = "line.push",
) -> bool:
    return await _post_line_message(
        PUSH_URL,
        {"to": user_id, "messages": list(messages)},
        token=token,
        client=client,
        timeout=timeout,
        context=context,
    )


async def multicast_line_messages(
    user_ids: Sequence[str],
    messages: Sequence[dict[str, Any]],
    *,
    token: str | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = 20,
    context: str = "line.multicast",
) -> bool:
    return await _post_line_message(
        MULTICAST_URL,
        {"to": list(user_ids), "messages": list(messages)},
        token=token,
        client=client,
        timeout=timeout,
        context=context,
    )
