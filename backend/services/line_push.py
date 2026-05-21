from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx
from loguru import logger

from ..models.database import settings


PUSH_URL = "https://api.line.me/v2/bot/message/push"
MULTICAST_URL = "https://api.line.me/v2/bot/message/multicast"


def _mask_target(target: str) -> str:
    return f"{target[:8]}..." if target else "unknown"


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

    owned_client = client is None
    active_client = client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await active_client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.is_success:
            return True

        target = payload.get("to")
        if isinstance(target, list):
            target_label = f"{len(target)} users"
        else:
            target_label = _mask_target(str(target or ""))
        logger.error(
            f"[{context}] LINE push failed target={target_label} "
            f"status={response.status_code} body={response.text[:500]}"
        )
        return False
    except Exception as exc:
        logger.exception(f"[{context}] LINE push exception: {exc}")
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
