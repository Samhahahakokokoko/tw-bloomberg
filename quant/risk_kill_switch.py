"""
risk_kill_switch.py — 交易訊號緊急停止開關

觸發條件（任一）：
  - 超過 50% 的股票資料 confidence < 0.6
  - FinMind API 連續失敗 > 3 次
  - TWSE API 連續失敗 > 3 次
  - DB 寫入失敗
  - 偵測到 mock 資料混入 production

Kill Switch 啟動 → 停止所有決策輸出 + 推送 LINE 警告
自動恢復：資料品質恢復正常後自動解除
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── 觸發閾值 ──────────────────────────────────────────────────────────────────
LOW_CONFIDENCE_RATIO = 0.50   # 超過 50% 股票信心不足
API_FAIL_THRESHOLD   = 3      # 連續失敗次數
AUTO_RECOVER_MINUTES = 30     # 嘗試自動恢復間隔（分鐘）


@dataclass
class KillSwitchState:
    enabled:       bool = False
    reason:        str  = ""
    triggered_at:  Optional[str] = None
    api_fail_counts: dict = field(default_factory=lambda: {
        "finmind": 0, "twse": 0, "database": 0
    })
    last_check_at: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return self.enabled

    def activate(self, reason: str):
        if not self.enabled:
            self.enabled      = True
            self.reason       = reason
            self.triggered_at = datetime.now().isoformat()
            logger.critical("[KillSwitch] ACTIVATED: %s", reason)
            _persist_state(self)
            _push_warning(reason)

    def deactivate(self, reason: str = "data_quality_restored"):
        if self.enabled:
            self.enabled      = False
            prev_reason       = self.reason
            self.reason       = ""
            self.triggered_at = None
            logger.info("[KillSwitch] DEACTIVATED (was: %s)", prev_reason)
            _persist_state(self)
            _push_recovery(reason)

    def record_api_fail(self, source: str):
        self.api_fail_counts[source] = self.api_fail_counts.get(source, 0) + 1
        count = self.api_fail_counts[source]
        if count >= API_FAIL_THRESHOLD:
            self.activate(f"{source}_api_consecutive_fail_{count}")

    def record_api_success(self, source: str):
        self.api_fail_counts[source] = 0


# ── 全域單例 ─────────────────────────────────────────────────────────────────
_state = KillSwitchState()


def get_state() -> KillSwitchState:
    return _state


def is_trading_enabled() -> bool:
    """主判斷函數：在 decision_engine 開頭呼叫"""
    return not _state.is_active


def check_and_activate(
    stocks_below_confidence: int,
    total_stocks: int,
    db_write_failed: bool = False,
    mock_in_production: bool = False,
) -> bool:
    """
    統一入口：決策前呼叫。回傳 True = 可繼續。
    """
    global _state

    if mock_in_production:
        _state.activate("mock_data_detected_in_production")
        return False

    if db_write_failed:
        _state.activate("database_write_failure")
        return False

    if total_stocks > 0:
        ratio = stocks_below_confidence / total_stocks
        if ratio >= LOW_CONFIDENCE_RATIO:
            _state.activate(
                f"low_confidence_ratio_{ratio:.0%}_{stocks_below_confidence}/{total_stocks}"
            )
            return False

    return True


def record_api_result(source: str, success: bool):
    """由各資料抓取服務呼叫，記錄 API 成功/失敗"""
    if success:
        _state.record_api_success(source)
        if _state.is_active and _state.reason.startswith(source):
            _attempt_auto_recover()
    else:
        _state.record_api_fail(source)


def _attempt_auto_recover():
    """嘗試自動恢復（若所有 API 失敗計數已歸零）"""
    all_clear = all(v == 0 for v in _state.api_fail_counts.values())
    if all_clear:
        _state.deactivate("all_api_failures_cleared")


def _persist_state(state: KillSwitchState):
    """非同步寫 DB（fire-and-forget）"""
    try:
        import asyncio

        async def _write():
            try:
                from backend.models.database import AsyncSessionLocal
                from backend.models.models import KillSwitchLog
                async with AsyncSessionLocal() as db:
                    db.add(KillSwitchLog(
                        enabled    = state.enabled,
                        reason     = state.reason,
                        triggered_at = state.triggered_at,
                    ))
                    await db.commit()
            except Exception as e:
                logger.warning("[KillSwitch] DB persist failed: %s", e)

        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_write())
    except Exception:
        pass


_KILL_MSG = """\
⛔ 系統安全模式

偵測到資料異常，已暫停交易訊號
原因：{reason}

影響：今日 AI 選股建議暫停
查詢功能：報價/庫存/新聞 正常可用

預計恢復：資料品質恢復後自動重啟"""

_RECOVER_MSG = """\
✅ 系統已恢復正常

資料品質已恢復，交易訊號重新啟動
原因：{reason}

今日 AI 選股功能已恢復"""


def _push_warning(reason: str):
    """推送 LINE 警告（fire-and-forget）"""
    _push_line(_KILL_MSG.format(reason=reason))


def _push_recovery(reason: str):
    """推送 LINE 恢復通知"""
    _push_line(_RECOVER_MSG.format(reason=reason))


def _push_line(text: str):
    try:
        import asyncio
        import httpx

        async def _send():
            try:
                from backend.models.database import settings, AsyncSessionLocal
                from backend.models.models import Subscriber
                from sqlalchemy import select
                token = settings.line_channel_access_token
                if not token:
                    return
                async with AsyncSessionLocal() as db:
                    r = await db.execute(
                        select(Subscriber).where(Subscriber.subscribed_morning == True)
                    )
                    subs = r.scalars().all()
                headers = {"Authorization": f"Bearer {token}"}
                async with httpx.AsyncClient(timeout=20) as c:
                    for sub in subs:
                        try:
                            await c.post(
                                "https://api.line.me/v2/bot/message/push",
                                json={"to": sub.line_user_id, "messages": [{"type": "text", "text": text}]},
                                headers=headers,
                            )
                        except Exception:
                            pass
            except Exception as e:
                logger.warning("[KillSwitch] LINE push failed: %s", e)

        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_send())
    except Exception:
        pass


def status_dict() -> dict:
    return {
        "kill_switch_active": _state.is_active,
        "reason":             _state.reason,
        "triggered_at":       _state.triggered_at,
        "api_fail_counts":    dict(_state.api_fail_counts),
        "trading_enabled":    is_trading_enabled(),
    }
