"""Fugle Trading API 整合 — 富果證券下單服務"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

import httpx
from loguru import logger

FUGLE_API_KEY    = os.getenv("FUGLE_API_KEY", "")
FUGLE_API_SECRET = os.getenv("FUGLE_API_SECRET", "")
FUGLE_ACCOUNT    = os.getenv("FUGLE_ACCOUNT", "")

MAX_ORDER_PCT  = 0.20   # 單筆最大 20% 總資金
MAX_DAILY_ORDERS = 10   # 每日上限
TRADING_HOURS  = (time(9, 0), time(13, 30))


def is_trading_hours() -> bool:
    """是否在交易時段"""
    now = datetime.now().time()
    return TRADING_HOURS[0] <= now <= TRADING_HOURS[1]


@dataclass
class OrderRequest:
    stock_id:   str
    stock_name: str
    action:     str         # buy / sell
    order_type: str         # limit / market
    price:      float
    shares:     int
    uid:        str
    confidence: float = 0.0
    auto_exec:  bool  = False

    @property
    def amount(self) -> float:
        return self.price * self.shares

    def confirm_text(self) -> str:
        act   = "買進" if self.action == "buy" else "賣出"
        otype = "限價" if self.order_type == "limit" else "市價"
        return (
            f"⚡ 下單確認\n"
            f"{act} {self.stock_id} {self.stock_name}\n"
            f"數量：{self.shares:,}股\n"
            f"價格：${self.price:,.0f}（{otype}）\n"
            f"金額：${self.amount:,.0f}"
        )

    def confirm_qr(self) -> dict:
        import json
        order_data = json.dumps({
            "act": "order_confirm",
            "stock": self.stock_id,
            "action": self.action,
            "price": self.price,
            "shares": self.shares,
        })
        return {"items": [
            {"type": "action", "action": {
                "type": "postback",
                "label": "✅ 確認下單",
                "data": order_data,
                "displayText": "確認下單"}},
            {"type": "action", "action": {
                "type": "postback",
                "label": "❌ 取消",
                "data": "act=order_cancel",
                "displayText": "取消下單"}},
        ]}


async def place_order(req: OrderRequest) -> dict:
    """執行下單（先驗證，再呼叫 Fugle API）"""
    # 安全檢查
    if not is_trading_hours() and not req.auto_exec:
        return {"ok": False, "error": "非交易時段（09:00-13:30）"}

    if not FUGLE_API_KEY:
        return {"ok": False, "error": "Fugle API 未設定，請先設定環境變數"}

    # 每日下單次數檢查
    count = await _get_today_order_count(req.uid)
    if count >= MAX_DAILY_ORDERS:
        return {"ok": False, "error": f"今日下單已達上限 {MAX_DAILY_ORDERS} 筆"}

    try:
        sdk = _get_fugle_sdk()
        if sdk is None:
            return {"ok": False, "error": "Fugle SDK 未安裝，請執行 pip install fugle-trade"}

        result = await _execute_order(sdk, req)
        await _save_order(req, result)
        return result

    except Exception as e:
        logger.error(f"[fugle] place_order failed: {e}")
        return {"ok": False, "error": str(e)[:100]}


def _get_fugle_sdk():
    """嘗試載入 Fugle SDK"""
    try:
        from fugle_trade.sdk import SDK
        sdk = SDK()
        sdk.login(FUGLE_API_KEY, FUGLE_API_SECRET)
        return sdk
    except ImportError:
        logger.warning("[fugle] fugle-trade not installed")
        return None
    except Exception as e:
        logger.error(f"[fugle] SDK init failed: {e}")
        return None


async def _execute_order(sdk, req: OrderRequest) -> dict:
    """實際呼叫 Fugle API 下單"""
    try:
        buy_sell = "B" if req.action == "buy" else "S"
        price    = 0 if req.order_type == "market" else req.price
        result   = sdk.order(
            buy_sell = buy_sell,
            stock_no = req.stock_id,
            price    = price,
            quantity = req.shares // 1000,  # 張
        )
        order_id = result.get("ordNo", "")
        logger.info(f"[fugle] order placed: {order_id} {req.stock_id} {req.shares}股")
        return {"ok": True, "order_id": order_id, "status": "submitted"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


async def _get_today_order_count(uid: str) -> int:
    try:
        from ..models.database import AsyncSessionLocal
        from ..models.models import TradingOrder
        from sqlalchemy import select, func
        from datetime import date
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(func.count()).select_from(TradingOrder)
                .where(TradingOrder.user_id == uid)
                .where(func.date(TradingOrder.created_at) == date.today())
            )
            return r.scalar() or 0
    except Exception:
        return 0


async def _save_order(req: OrderRequest, result: dict):
    try:
        from ..models.database import AsyncSessionLocal
        from ..models.models import TradingOrder
        async with AsyncSessionLocal() as db:
            order = TradingOrder(
                user_id       = req.uid,
                stock_id      = req.stock_id,
                stock_name    = req.stock_name,
                action        = req.action,
                order_type    = req.order_type,
                price         = req.price,
                shares        = req.shares,
                status        = "executed" if result.get("ok") else "failed",
                fugle_order_id = result.get("order_id", ""),
                confidence    = req.confidence,
                auto_executed = req.auto_exec,
            )
            db.add(order)
            await db.commit()
    except Exception as e:
        logger.debug(f"[fugle] save_order failed: {e}")


async def get_account_balance() -> dict:
    """查詢帳戶餘額"""
    sdk = _get_fugle_sdk()
    if sdk is None:
        return {"cash": 0, "market_value": 0, "total": 0, "error": "SDK not available"}
    try:
        balance = sdk.get_balance()
        return {
            "cash":         balance.get("availableMoney", 0),
            "market_value": balance.get("stockValue", 0),
            "total":        balance.get("totalAssets", 0),
        }
    except Exception as e:
        return {"error": str(e)[:80]}


async def get_fugle_holdings() -> list[dict]:
    """查詢 Fugle 庫存"""
    sdk = _get_fugle_sdk()
    if sdk is None:
        return []
    try:
        holdings = sdk.get_holdings()
        return [
            {
                "stock_id": h.get("stockNo", ""),
                "name":     h.get("stockName", ""),
                "shares":   h.get("quantity", 0) * 1000,
                "cost":     h.get("costPrice", 0),
                "price":    h.get("currentPrice", 0),
            }
            for h in holdings
        ]
    except Exception as e:
        logger.warning(f"[fugle] get_holdings failed: {e}")
        return []


def check_auto_trade(confidence: float, threshold: float = 0.95) -> bool:
    """檢查信心指數是否達到自動交易門檻（純工具版，無訂閱限制）"""
    return confidence >= threshold
