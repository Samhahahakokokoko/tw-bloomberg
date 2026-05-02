"""
callback_router.py — LINE Bot Postback 集中路由器

設計：
  - 所有 postback action 在此統一登記
  - 未知 action → 回「功能維護中」，不 crash
  - 每次 callback 都記錄到 callback_log（包括錯誤）
  - handler.py 的 _handle_postback 可直接委託此 router

使用方式（在 handler.py 中）：
    from line_webhook.callback_router import CallbackRouter
    router = CallbackRouter()
    msgs = await router.dispatch(act, params, uid)
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

# 維護中的預設回覆文字
_MAINTENANCE_MSG = "🔧 功能維護中\n此功能正在升級，敬請期待！"


class CallbackRouter:
    """
    集中管理所有 postback action 的路由。

    使用 register() 裝飾器或直接呼叫 add_route() 登記 handler。
    每個 handler 簽名：async def handler(params: dict, uid: str) -> list
    """

    def __init__(self):
        self._routes: dict[str, Callable] = {}
        self._error_count: int = 0

    def add_route(self, action: str, handler: Callable) -> None:
        self._routes[action] = handler

    def register(self, *actions: str):
        """裝飾器：@router.register("act_name")"""
        def decorator(fn: Callable) -> Callable:
            for act in actions:
                self._routes[act] = fn
            return fn
        return decorator

    async def dispatch(
        self,
        data: str,    # 原始 postback data 字串，如 "act=foo&bar=baz"
        uid:  str,
    ) -> list:
        """
        解析 data → 路由到對應 handler。
        未知 action 回「功能維護中」；任何錯誤都記錄並回安全訊息。
        """
        params = dict(urllib.parse.parse_qsl(data))
        act    = params.get("act", "")

        handler = self._routes.get(act)

        if handler is None:
            logger.info("[CallbackRouter] undefined act=%s uid=%s", act, uid[:8])
            await self._log(uid, act, params, error="undefined action")
            return [_make_maintenance_msg()]

        try:
            result = await handler(params, uid)
            return result if isinstance(result, list) else [result]
        except Exception as e:
            self._error_count += 1
            logger.error("[CallbackRouter] act=%s uid=%s error=%s", act, uid[:8], e)
            await self._log(uid, act, params, error=str(e)[:500])
            return [_make_maintenance_msg(str(e))]

    async def _log(
        self,
        uid:    str,
        action: str,
        params: dict,
        error:  str = "",
    ) -> None:
        """寫入 callback_log 表（失敗靜默）"""
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import CallbackLog
            async with AsyncSessionLocal() as db:
                db.add(CallbackLog(
                    user_id=uid,
                    action=action,
                    params=str(params)[:500],
                    error=error[:500] if error else None,
                ))
                await db.commit()
        except Exception as log_err:
            logger.debug("[CallbackRouter] log write failed: %s", log_err)

    @property
    def registered_actions(self) -> list[str]:
        return sorted(self._routes.keys())

    @property
    def error_count(self) -> int:
        return self._error_count


def _make_maintenance_msg(detail: str = ""):
    """回傳功能維護中 TextMessage（LINE SDK 物件）"""
    from linebot.v3.messaging import TextMessage
    text = _MAINTENANCE_MSG
    if detail:
        text += f"\n\n錯誤代碼：{detail[:80]}"
    return TextMessage(text=text)


# ── 全域 singleton ────────────────────────────────────────────────────────────

_global_router: CallbackRouter | None = None


def get_callback_router() -> CallbackRouter:
    global _global_router
    if _global_router is None:
        _global_router = CallbackRouter()
        _register_default_routes(_global_router)
    return _global_router


def _register_default_routes(router: CallbackRouter) -> None:
    """
    將所有已知 action 登記到 router。
    handler 函式從 handler.py 動態 import，避免循環依賴。
    """

    async def _portfolio_ai(params, uid):
        from line_webhook.handler import _cmd_ai_portfolio
        return [await _cmd_ai_portfolio(uid)]

    async def _alert_set(params, uid):
        from line_webhook.handler import _alert_guide
        return [_alert_guide()]

    async def _portfolio_view(params, uid):
        from line_webhook.handler import _cmd_portfolio
        return await _cmd_portfolio(uid)

    async def _news_refresh(params, uid):
        from line_webhook.handler import _cmd_news_feed
        return await _cmd_news_feed(uid)

    async def _screener_menu(params, uid):
        from line_webhook.handler import _flex_screen_menu
        from linebot.v3.messaging import FlexMessage
        return [_flex_screen_menu()]

    async def _more_menu(params, uid):
        from line_webhook.handler import _flex_more_menu
        return [_flex_more_menu()]

    async def _screener_run(params, uid):
        from line_webhook.handler import _cmd_report
        stype = params.get("type", "all")
        return await _cmd_report(stype, uid)

    async def _more_sub(params, uid):
        from line_webhook.handler import (
            _cmd_backtest_menu, _cmd_risk_report, _cmd_accuracy
        )
        sub = params.get("sub", "")
        if sub == "backtest": return await _cmd_backtest_menu(uid)
        if sub == "risk":     return await _cmd_risk_report(uid)
        if sub == "ranking":  return await _cmd_accuracy()
        if sub == "odd":
            from linebot.v3.messaging import TextMessage
            return [TextMessage(text="零股計算\n\n格式：/odd 預算 代碼\n例：/odd 5000 2330")]
        return [_make_maintenance_msg(f"unknown sub={sub}")]

    async def _strategy_toggle(params, uid):
        from line_webhook.handler import _cmd_strategy_toggle
        return await _cmd_strategy_toggle(params.get("name", ""), uid)

    async def _strategy_preset(params, uid):
        from line_webhook.handler import _cmd_strategy_preset
        return await _cmd_strategy_preset(params.get("preset", "balanced"), uid)

    async def _strategy_momentum(params, uid):
        from line_webhook.handler import _cmd_strategy_perf
        return await _cmd_strategy_perf("momentum", uid)

    async def _strategy_value(params, uid):
        from line_webhook.handler import _cmd_strategy_perf
        return await _cmd_strategy_perf("value", uid)

    async def _strategy_chip(params, uid):
        from line_webhook.handler import _cmd_strategy_perf
        return await _cmd_strategy_perf("chip", uid)

    async def _strategy_breakout(params, uid):
        from line_webhook.handler import _cmd_strategy_perf
        return await _cmd_strategy_perf("breakout", uid)

    async def _backtest_run(params, uid):
        from line_webhook.handler import _cmd_backtest_run
        return await _cmd_backtest_run(params.get("strategy", "momentum"), uid)

    async def _risk_optimize(params, uid):
        import asyncio
        from line_webhook.handler import _risk_optimize_bg
        asyncio.create_task(_risk_optimize_bg(uid))
        return [{"type": "text",
                 "text": "📐 計算馬可維茲最佳配置中…約需 15-30 秒，完成後自動推送"}]

    async def _recommend_detail(params, uid):
        from line_webhook.handler import _cmd_ai_stock
        code = params.get("code", "")
        if code:
            return await _cmd_ai_stock(code)
        return [_make_maintenance_msg("missing code param")]

    async def _menu_market(params, uid):
        from line_webhook.handler import qr_items, _text
        return [_text(
            "📊 市場資訊",
            qr_items(
                ("大盤指數",  "/market"),
                ("今日早報",  "/morning"),
                ("外資動向",  "/inst 2330"),
                ("市場情緒",  "/ai 今日台股市場情緒如何"),
            )
        )]

    async def _menu_ai_strategy(params, uid):
        from line_webhook.handler import qr_items, _text
        return [_text(
            "🤖 AI 策略選單",
            qr_items(
                ("今日推薦",  "/r"),
                ("動能策略",  "/report momentum"),
                ("存股策略",  "/report value"),
                ("AI族群",   "/report ai"),
                ("籌碼策略",  "/report chip"),
            )
        )]

    async def _add_holding(params, uid):
        code   = params.get("code", "")
        shares = params.get("shares", "1000")
        cost   = params.get("cost", "0")
        from line_webhook.handler import _cmd_buy
        return await _cmd_buy(code, shares, cost, uid)

    async def _del_holding(params, uid):
        from line_webhook.handler import portfolio_service, AsyncSessionLocal, _text, qr_items
        hid = int(params.get("id", 0))
        code = params.get("code", "")
        async with AsyncSessionLocal() as db:
            ok = await portfolio_service.remove_holding(db, hid, uid)
        if ok:
            return [_text(f"🗑️ {code} 已從庫存刪除", qr_items(("💼 庫存", "/p")))]
        return [_text("❌ 找不到此持股")]

    async def _edit_cost(params, uid):
        hid  = params.get("id", "")
        code = params.get("code", "")
        from line_webhook.handler import _text, qr_items
        return [_text(
            f"✏️ 修改 {code} 成本價\n\n請傳送：\n/setcost {hid} 新成本價\n例：/setcost {hid} 850",
            qr_items(("取消", "/p"))
        )]

    async def _ai_stock_analysis(params, uid):
        from line_webhook.handler import _cmd_ai_ask
        code = params.get("code", "")
        return [await _cmd_ai_ask(f"{code} 現在的技術面和基本面如何？值得持有嗎？", uid)]

    async def _mc_run(params, uid):
        """蒙地卡羅模擬（背景執行）"""
        import asyncio
        strategy = params.get("strategy", "momentum")
        asyncio.create_task(_mc_bg(strategy, uid))
        from line_webhook.handler import _text, qr_items
        return [_text("🎲 蒙地卡羅模擬啟動中...\n1000次隨機交易順序模擬\n約需 10-20 秒，完成後推送分布圖",
                      qr_items(("💼 庫存", "/p")))]

    # ── 登記所有路由 ──────────────────────────────────────────────────────────
    routes = {
        "portfolio_ai":       _portfolio_ai,
        "portfolio_view":     _portfolio_view,
        "alert_set":          _alert_set,
        "news_refresh":       _news_refresh,
        "screener_menu":      _screener_menu,
        "more_menu":          _more_menu,
        "screener":           _screener_run,
        "more":               _more_sub,
        "strategy_toggle":    _strategy_toggle,
        "strategy_preset":    _strategy_preset,
        "strategy_momentum":  _strategy_momentum,
        "strategy_value":     _strategy_value,
        "strategy_chip":      _strategy_chip,
        "strategy_breakout":  _strategy_breakout,
        "backtest_run":       _backtest_run,
        "risk_optimize":      _risk_optimize,
        "recommend_detail":   _recommend_detail,
        "menu_market":        _menu_market,
        "menu_ai_strategy":   _menu_ai_strategy,
        "add":                _add_holding,
        "del":                _del_holding,
        "editcost":           _edit_cost,
        "ai":                 _ai_stock_analysis,
        "mc_run":             _mc_run,
    }
    for act, fn in routes.items():
        router.add_route(act, fn)

    logger.info("[CallbackRouter] Registered %d routes", len(routes))


async def _mc_bg(strategy: str, uid: str) -> None:
    """背景：蒙地卡羅 + 推送圖片"""
    import httpx
    from backend.models.database import settings as line_settings

    try:
        from quant.montecarlo_engine import MonteCarloEngine
        engine = MonteCarloEngine(n_sims=1000)
        result = engine.run_mock(n_trades=60, win_rate=0.58)
        path   = engine.generate_chart(result)

        import os
        BASE_URL = os.getenv("BASE_URL", "")
        msgs = []
        if path and BASE_URL:
            img_url = f"{BASE_URL.rstrip('/')}/static/reports/{path.name}"
            msgs.append({"type": "image",
                          "originalContentUrl": img_url,
                          "previewImageUrl":    img_url})

        # 文字摘要
        r = result
        text = (
            f"🎲 蒙地卡羅結果（{r.n_sims:,}次模擬）\n"
            f"{'─'*22}\n"
            f"最大回撤（P95）：{r.max_dd_p95*100:.1f}%\n"
            f"爆倉機率：{r.bankruptcy_prob*100:.2f}%\n"
            f"損失20%機率：{r.ruin_prob_20*100:.1f}%\n"
            f"最終報酬（均值）：{r.final_return_mean*100:+.1f}%\n"
            f"夏普穩定性（σ）：{r.sharpe_std:.3f}"
        )
        msgs.append({"type": "text", "text": text})

        headers = {"Authorization": f"Bearer {line_settings.line_channel_access_token}"}
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post("https://api.line.me/v2/bot/message/push",
                         json={"to": uid, "messages": msgs[:5]},
                         headers=headers)
    except Exception as e:
        logger.error("[mc_bg] %s", e)
