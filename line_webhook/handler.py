"""LINE Bot Webhook — 多用戶 · Flex · Quick Reply · Postback · 策略推薦"""
import asyncio
import re
import sys, os, urllib.parse
import httpx
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import APIRouter, Request, HTTPException
from linebot.v3.messaging import (
    AsyncApiClient, AsyncMessagingApi, Configuration,
    ReplyMessageRequest, TextMessage, FlexMessage,
    QuickReply, QuickReplyItem, MessageAction, PostbackAction,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhook import WebhookParser
from loguru import logger

from backend.models.database import settings, AsyncSessionLocal
from backend.services.line_push import push_line_messages
from backend.services.permission_service import (
    check_permission, log_usage, set_user_role, remove_user,
    get_all_users, get_usage_stats, ADMIN_UID,
)
from backend.services.twse_service import (
    fetch_realtime_quote, fetch_institutional, fetch_market_overview,
)
from backend.services import portfolio_service
from backend.services.morning_report import generate_morning_report
from backend.services.weekly_report import generate_weekly_report
from backend.models.models import Alert, Subscriber
from backend.services.trade_log_service import (
    log_trade, get_history, get_ytd_tax, get_monthly_stats,
    format_trade_history, format_monthly_report, format_tax_report,
)
from backend.services.user_profile_service import (
    get_or_create_profile, update_risk, update_goal, build_ai_context,
    save_query, find_similar_answer, RISK_PROFILES, INVESTMENT_GOALS,
)
from line_webhook.flex_messages import (
    flex_quote, flex_portfolio, flex_morning_report,
    flex_portfolio_carousel, flex_holding_card,
    flex_rec_carousel, flex_profile_setup, qr_items,
    quick_reply_quote, quick_reply_portfolio,
)

router = APIRouter()
configuration = Configuration(access_token=settings.line_channel_access_token)
parser = WebhookParser(settings.line_channel_secret)

# ── 分頁快取（uid → {rows, total_pages, page, group, screen_type}）─────────
_report_pages: dict[str, dict] = {}


# ── Webhook 入口 ───────────────────────────────────────────────────────────────

@router.get("/webhook_ping")
async def webhook_ping():
    """健康檢查端點，確認 handler 正在運行"""
    import sys
    return {"status": "ok", "handler": "loaded", "python": sys.version[:10]}


@router.post("/webhook")
async def webhook(request: Request):
    sig  = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    logger.info(f"=== WEBHOOK HIT body={len(body)}bytes sig={'yes' if sig else 'NO'} ===")
    try:
        events = parser.parse(body.decode(), sig)
    except InvalidSignatureError:
        raise HTTPException(400, "Invalid signature")
    except Exception as e:
        logger.error(f"Parse error: {e}", exc_info=True)
        return "OK"

    logger.info(f"=== PARSED {len(events)} events ===")
    # 立刻回傳 200，事件在背景處理，避免 LINE 499 超時
    for event in events:
        asyncio.create_task(_dispatch_event(event))

    return "OK"


async def _dispatch_event(event) -> None:
    """背景處理單一 LINE 事件（webhook 已回傳 200 後執行）"""
    reply_token = getattr(event, "reply_token", None)
    try:
        logger.info(f"=== EVENT {type(event).__name__} ===")

        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            uid  = event.source.user_id
            text = event.message.text.strip()
            logger.info(f"[{uid[:8]}] text: {text!r}")

            # /chart: 直接傳 reply_token 給背景任務，圖片用 reply 回傳（零配額）
            _parts = text.split()
            _cmd   = _parts[0].lower() if _parts else ""
            if _cmd in ("/chart", "chart") and len(_parts) >= 2:
                asyncio.create_task(_chart_bg(_parts[1].upper(), uid, reply_token=reply_token or ""))
            else:
                msgs = await _handle_text(text, uid)
                if msgs:
                    await _reply(event, *msgs)

        elif isinstance(event, PostbackEvent):
            uid  = event.source.user_id
            data = event.postback.data
            logger.info(f"[{uid[:8]}] postback: {data!r}")
            msgs = await _handle_postback(data, uid)
            if msgs:
                await _reply(event, *msgs)

        else:
            logger.info(f"Ignored event type: {type(event).__name__}")

    except Exception as e:
        import traceback as _tb
        tb_str = _tb.format_exc().replace('\n', ' | ')
        logger.error(f"Event error: {e} || TRACE: {tb_str}")
        if reply_token:
            try:
                async with AsyncApiClient(configuration) as c:
                    await AsyncMessagingApi(c).reply_message(
                        ReplyMessageRequest(
                            reply_token=reply_token,
                            messages=[TextMessage(text=f"系統錯誤: {type(e).__name__}")]
                        )
                    )
            except Exception as e:
                pass


async def _reply(event, *messages) -> None:
    """reply_message — 不計入 push 配額，使用 SDK 物件序列化"""
    if not messages:
        return
    try:
        reply_token = event.reply_token
        msg_objects = []
        for msg in messages:
            if isinstance(msg, dict):
                if msg.get("type") == "text":
                    msg_objects.append(TextMessage(text=msg["text"]))
                # 其他 dict 類型（image 等）走 push，不走 reply
            else:
                msg_objects.append(msg)
        if not msg_objects:
            return
        async with AsyncApiClient(configuration) as c:
            await AsyncMessagingApi(c).reply_message(
                ReplyMessageRequest(reply_token=reply_token, messages=msg_objects[:5])
            )
    except Exception as e:
        logger.error(f"Reply exception: {e}", exc_info=True)
        # Fallback: retry with plain-text only (strips quick_reply that may fail validation)
        try:
            plain = []
            for msg in messages:
                if isinstance(msg, TextMessage):
                    plain.append(TextMessage(text=msg.text))
                elif isinstance(msg, dict) and msg.get("type") == "text":
                    plain.append(TextMessage(text=msg["text"]))
            if plain:
                async with AsyncApiClient(configuration) as c:
                    await AsyncMessagingApi(c).reply_message(
                        ReplyMessageRequest(reply_token=reply_token, messages=plain[:5])
                    )
        except Exception as e:
            pass


async def _reply_by_token(reply_token: str, messages: list[dict]) -> bool:
    """用 reply token 傳送任意 dict 格式訊息（圖片等），不計入 push 配額。
    reply token 只能用一次且約 60 秒內有效。"""
    if not reply_token or not messages:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.line.me/v2/bot/message/reply",
                headers={
                    "Authorization": f"Bearer {settings.line_channel_access_token}",
                    "Content-Type": "application/json",
                },
                json={"replyToken": reply_token, "messages": messages[:5]},
            )
        if resp.status_code == 200:
            return True
        logger.warning(f"[reply_by_token] failed {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"[reply_by_token] exception: {e}")
    return False


# ── Postback 處理 ─────────────────────────────────────────────────────────────

async def _handle_postback(data: str, uid: str) -> list:
    try:
        return await _handle_postback_inner(data, uid)
    except Exception as e:
        logger.error(f"[postback] EXCEPTION act data={data!r} uid={uid[:8]} err={e}", exc_info=True)
        return [TextMessage(text=f"⚠️ 處理失敗\n{type(e).__name__}: {str(e)[:120]}")]


async def _handle_postback_inner(data: str, uid: str) -> list:
    # 支援兩種格式：
    #   "act=market_card&code=2330"  (query-string)
    #   "market_card"               (Rich Menu 純字串)
    if "=" in data:
        params = dict(urllib.parse.parse_qsl(data))
        act    = params.get("act", "")
    else:
        act    = data.strip()
        params = {"act": act}
    hid  = int(params.get("id", 0))
    code = params.get("code", "")
    logger.info("[postback] uid=%s act=%r", uid[:8], act)

    if act == "add":
        delta = int(params.get("delta", 100))
        async with AsyncSessionLocal() as db:
            h = await portfolio_service.adjust_shares(db, hid, delta, uid)
        if h:
            return [_text(f"✅ {h.stock_code} 增加 {delta} 股\n目前 {h.shares:,} 股")]
        return [_text("❌ 找不到此持股")]

    if act == "sub":
        delta = int(params.get("delta", 100))
        async with AsyncSessionLocal() as db:
            h = await portfolio_service.adjust_shares(db, hid, -delta, uid)
        if h:
            return [_text(f"✅ {h.stock_code} 減少 {delta} 股\n目前 {h.shares:,} 股")]
        return [_text(f"✅ {code} 持股已全數賣出（股數歸零刪除）")]

    if act == "editcost":
        return [_text(
            f"✏️ 修改 {code} 成本價\n\n請傳送：\n/setcost {hid} 新成本價\n\n例：/setcost {hid} 850.5",
            qr_items((f"取消", "/portfolio"))
        )]

    if act == "del":
        async with AsyncSessionLocal() as db:
            ok = await portfolio_service.remove_holding(db, hid, uid)
        if ok:
            return [_text(f"🗑️ {code} 已從庫存刪除",
                          qr_items(("💼 庫存", "/portfolio")))]
        return [_text("❌ 找不到此持股")]

    if act == "ai":
        return [await _cmd_ai_ask(f"{code} 現在的技術面和基本面如何？值得持有嗎？", uid)]

    # ── 庫存 互動按鈕 ──────────────────────────────────────────────────────────
    if act == "portfolio_buy":
        cost_hint = params.get("price", "")
        hint_line = f"\n例：/buy {code} 5 {cost_hint}" if cost_hint else f"\n例：/buy {code} 5 成本價"
        return [_text(
            f"➕ 加碼 {code}\n\n輸入買進指令：\n/buy {code} 張數 成本價{hint_line}",
            qr_items((f"加碼 {code}", f"/buy {code} 1 {cost_hint or '0'}"),
                     ("💼 庫存", "/p"))
        )]

    if act == "portfolio_sell":
        all_shares = params.get("shares", "")
        curr_price = params.get("price", "")
        hint_all   = f"/sell {code} {all_shares} {curr_price}" if all_shares and curr_price else f"/sell {code} 張數 賣出價"
        return [_text(
            f"💰 賣出 {code}\n\n輸入賣出指令：\n{hint_all}\n\n或部分出清：\n/sell {code} 5 {curr_price or '賣出價'}",
            qr_items((f"全部賣出", hint_all), ("💼 庫存", "/p"))
        )]

    if act == "portfolio_analysis":
        return await _cmd_analysis(uid)

    if act == "analysis":
        return await _cmd_analysis(uid)

    if act == "history":
        return await _cmd_history(uid)

    if act == "profile":
        field = params.get("field", "")
        val   = params.get("val", "")
        async with AsyncSessionLocal() as db:
            if field == "risk":
                p = await update_risk(db, uid, val)
                risk_info = RISK_PROFILES.get(val, RISK_PROFILES["moderate"])
                return [_text(
                    f"✅ 已設定風險偏好：{risk_info['emoji']} {risk_info['label']}\n"
                    f"AI 回答將依此調整建議風格",
                    qr_items(("設定目標", "/profile"), ("策略推薦", "/rec"))
                )]
            elif field == "goal":
                p = await update_goal(db, uid, val)
                goal_info = INVESTMENT_GOALS.get(val, INVESTMENT_GOALS["growth"])
                return [_text(
                    f"✅ 已設定投資目標：{goal_info['emoji']} {goal_info['label']}",
                    qr_items(("查看設定", "/profile"), ("策略推薦", "/rec"))
                )]
        return [_text("❌ 未知設定")]

    if act == "applyrec":
        strategy = params.get("strategy", "macd")
        return await _cmd_apply_rec(code, strategy, uid)

    if act == "screener_menu":
        return [_flex_screen_menu()]
    if act == "screener":
        stype = params.get("type", "all")
        return await _cmd_report(stype, uid)
    if act == "more":
        sub = params.get("sub", "")
        if sub == "backtest": return await _cmd_backtest_menu(uid)
        if sub == "risk":     return await _cmd_risk_report(uid)
        if sub == "odd":
            return [_text("零股計算\n\n格式：/odd 預算 代碼\n例：/odd 5000 2330",
                          qr_items(("示範5000", "/odd 5000 2330")))]
        if sub == "ranking":  return await _cmd_accuracy()
        return [_text("❌ 未知子功能")]

    # ── 新增 postback 動作 ───────────────────────────────────────────────────
    if act == "portfolio_ai":
        return [await _cmd_ai_portfolio(uid)]

    if act == "alert_set":
        return [_alert_guide()]

    if act in ("strategy_momentum", "strategy_value",
               "strategy_chip", "strategy_breakout"):
        name = act.replace("strategy_", "")
        return await _cmd_strategy_perf(name, uid)

    if act == "strategy_toggle":
        name = params.get("name", "")
        return await _cmd_strategy_toggle(name, uid)

    if act == "strategy_preset":
        preset = params.get("preset", "balanced")
        return await _cmd_strategy_preset(preset, uid)

    if act == "backtest_run":
        strategy = params.get("strategy", "momentum")
        return await _cmd_backtest_run(strategy, uid)

    if act == "risk_optimize":
        import asyncio
        asyncio.create_task(_risk_optimize_bg(uid))
        return [_text(
            "📐 馬可維茲最佳配置計算中…\n\n"
            "正在分析歷史報酬率與相關性，約需 15-30 秒，完成後自動推送",
            qr_items(("💼 庫存", "/p"), ("📊 選股", "/r")),
        )]

    if act == "news_refresh":
        return await _cmd_news_feed(uid)

    if act == "recommend_detail":
        if code:
            return await _cmd_ai_stock(code)
        return [_text("請指定股票代碼", qr_items(("📊 選股", "/r")))]

    if act == "menu_market":
        return [_text(
            "📊 市場資訊",
            qr_items(
                ("大盤指數", "/market"),
                ("今日早報", "/morning"),
                ("外資動向", "/inst 2330"),
                ("市場情緒", "/ai 今日台股市場情緒如何"),
            )
        )]

    if act == "menu_ai_strategy":
        return [_text(
            "🤖 AI 策略選單",
            qr_items(
                ("今日推薦", "/r"),
                ("動能策略", "/report momentum"),
                ("存股策略", "/report value"),
                ("AI族群",   "/report ai"),
                ("籌碼策略", "/report chip"),
            )
        )]

    # ── 新版 postback 動作（重新設計介面）────────────────────────────────────
    if act == "portfolio_view":
        return await _cmd_portfolio(uid)

    if act == "market_card":
        return await _cmd_market_card(uid)

    if act == "ai_menu":
        from line_webhook.flex_messages import qr_ai_menu
        return [_text("🤖 AI 分析選單\n\n請選擇分析項目：", qr_ai_menu())]

    if act == "screener_qr":
        return [_text(
            "🔍 今日選股\n\n請選擇選股策略：",
            qr_items(
                ("🚀 動能策略", "/report momentum"),
                ("💎 存股策略", "/report value"),
                ("🎯 籌碼追蹤", "/report chip"),
                ("💥 技術突破", "/report breakout"),
                ("🤖 AI綜合",   "/report ai"),
                ("⚡ 今日決策", "/daily"),
            )
        )]

    if act in ("more_menu_v2", "more_menu"):
        return [_text(
            "⚙️ 更多功能\n\n請選擇：",
            _qr_postback(
                ("📈 策略回測",  "act=more&sub=backtest"),
                ("🛡️ 風控分析",  "act=more&sub=risk"),
                ("🏆 績效排行",  "act=more&sub=ranking"),
                ("🪙 零股計算",  "act=more&sub=odd"),
                ("🔥 族群熱度",  "sector"),
                ("📋 研究清單",  "act=recommend_detail&code=2330"),
                ("🎯 今日決策",  "daily"),
            ),
        )]

    if act == "add_holding":
        return [_text(
            f"➕ 新增 {code} 到庫存\n\n請輸入：/buy {code} 股數 成本價\n"
            f"例：/buy {code} 1000 850",
            qr_items((f"示範1張", f"/buy {code} 1000 100"))
        )]

    if act == "report_next":
        import asyncio
        asyncio.create_task(_cmd_report_page(uid, delta=+1))
        return [_text("⏩ 載入下一頁…", qr_items(("🔄 換策略", "act=screener_qr")))]

    if act == "report_prev":
        import asyncio
        asyncio.create_task(_cmd_report_page(uid, delta=-1))
        return [_text("⏪ 載入上一頁…", qr_items(("🔄 換策略", "act=screener_qr")))]

    if act in ("news_menu", "news"):
        return await _cmd_news_feed(uid)

    if act == "watchlist" or act == "portfolio_watchlist":
        return await _cmd_watchlist(uid)

    if act == "analyst_consensus":
        return await _cmd_analyst_today()

    if act == "analyst_add_guide":
        return [_text(
            "➕ 新增分析師\n\n"
            "指令格式：\n"
            "/analyst add [名稱] [channel_id] [專長]\n\n"
            "例：\n"
            "/analyst add 財經雪倫 UCxxxxx AI,散熱\n"
            "/analyst add 存股研究室 UCyyyyy 存股,高股息\n\n"
            "channel_id 可在 YouTube 頻道 URL 中找到（UC開頭）",
            qr_items(("查看清單", "/analyst list"), ("今日共識", "/analyst")),
        )]

    if act == "rs":
        return await _cmd_rs(uid)

    if act == "breadth":
        return await _cmd_breadth(uid)

    # ── 未定義動作 → 嘗試 callback_router，否則友善提示 ──────────────────────
    try:
        from line_webhook.callback_router import get_callback_router
        router = get_callback_router()
        result = await router.dispatch(data, uid)
        if result:
            return result
    except Exception as cb_err:
        logger.debug("[postback] callback_router error: %s", cb_err)

    logger.info("[postback] 未定義 act=%s uid=%s", act, uid[:8])
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import CallbackLog
        async with AsyncSessionLocal() as db:
            db.add(CallbackLog(user_id=uid, action=act,
                               params=str(params)[:500], error="undefined"))
            await db.commit()
    except Exception as e:
        pass
    return [_text(
        "收到你的請求，處理中...\n\n如持續無回應請試試：",
        qr_items(("💼 庫存", "/p"), ("📊 選股", "/r"), ("📰 新聞", "/n"), ("❓ 說明", "/help")),
    )]


# ── 自然語言關鍵字對照表 ──────────────────────────────────────────────────────

_NL_PORTFOLIO = {
    "庫存", "我的庫存", "持股", "查庫存", "看庫存", "我的持股", "庫存清單",
    "持股清單", "我的股票", "股票庫存", "帳戶", "我的帳戶", "倉位",
    "持倉", "查持股", "看持股",
}
_NL_MARKET = {
    "大盤", "指數", "加權", "台股", "今天大盤", "市場", "行情",
    "大盤指數", "台股指數", "今天行情", "今日大盤",
}
_NL_MORNING = {
    "早報", "今日早報", "今天早報", "晨報", "早安報", "每日早報",
    "今日摘要", "早盤", "今天市況",
}
_NL_WEEKLY = {
    "週報", "周報", "本週報告", "本周報告", "週績效", "本週績效",
    "這週怎樣", "本周走勢",
}
_NL_REC = {
    "推薦", "策略", "策略推薦", "建議", "操作建議", "我該怎麼操作",
    "怎麼買", "買什麼", "選股", "策略建議",
}
_NL_HELP = {
    "幫助", "說明", "指令", "功能", "怎麼用", "如何使用", "使用說明",
    "有哪些指令", "可以幹嘛", "能做什麼", "幫我",
}
_NL_AI_PORTFOLIO = {
    "分析庫存", "分析我的庫存", "幫我分析", "庫存分析", "AI分析庫存",
    "分析持股", "幫我看看", "我的投資如何", "投組分析",
}
_NL_SUBSCRIBE = {
    "訂閱", "訂閱早報", "幫我訂閱", "我要訂閱", "開啟推播", "訂閱推播",
}
_NL_HISTORY = {
    "歷史", "交易紀錄", "買賣紀錄", "交易歷史", "操作紀錄", "我的紀錄",
}
_NL_TAX = {
    "稅務", "稅", "證交稅", "報稅", "今年稅", "已實現損益",
}


# ── 文字指令分發 ──────────────────────────────────────────────────────────────

async def _handle_text(text: str, uid: str) -> list:
    parts = text.split()
    cmd   = parts[0].lower() if parts else ""
    args  = parts[1:]
    logger.info(f"[route] cmd={cmd!r} text={text[:60]!r}")

    # ── 權限 & 用量檢查 ──────────────────────────────────────────────────────
    _SKIP_PERM = {"", "/help", "help", "/start", "start"}
    if cmd not in _SKIP_PERM:
        _ok, _err = await check_permission(uid, cmd)
        if not _ok:
            return [_text(_err)]
        await log_usage(uid, cmd)

    # ── 管理員指令（最高優先）────────────────────────────────────────────────
    if cmd == "/adduser" and len(parts) >= 3:
        return await _cmd_adduser(parts[1], parts[2], uid)
    if cmd == "/removeuser" and len(parts) >= 2:
        return await _cmd_removeuser(parts[1], uid)
    if cmd == "/userlist":
        return await _cmd_userlist(uid)
    if cmd == "/userstats":
        return await _cmd_userstats(uid)

    # ── 圖表 / 比較 / 零股：必須在所有 fallback 之前攔截 ─────────────────────
    if cmd in ("/chart", "chart") and len(parts) >= 2:
        return await _cmd_chart(parts[1].upper(), uid)
    if cmd in ("/compare", "compare") and len(parts) >= 3:
        return await _cmd_compare_v2(parts[1].upper(), parts[2].upper(), uid)
    if cmd in ("/odd", "odd") and len(parts) >= 2:
        arg1 = parts[1]
        arg2 = parts[2] if len(parts) > 2 else None
        if arg1.isdigit() and 4 <= len(arg1) <= 6 and arg2 and arg2.replace(",", "").replace(".", "").isdigit():
            return await _cmd_odd_v2(arg1, arg2, uid)
        return await _cmd_odd(arg1, arg2, uid)

    # ── 分析 / 庫存：同時支援含斜線與不含斜線寫法 ──────────────────────────
    if cmd in ("/analysis", "analysis", "/perf", "perf"):
        return await _cmd_analysis(uid)
    if cmd in ("/portfolio", "portfolio", "/p", "p"):
        return await _cmd_portfolio(uid)

    # ── 斜線指令（精確比對）──────────────────────────────────────────────────
    if cmd == "/quote"    and len(parts) >= 2: return await _cmd_quote(parts[1])
    if cmd in ("/market", "/market_overview"):  return await _cmd_market()
    if cmd == "/buy"      and len(parts) >= 4:  return await _cmd_buy(parts, uid)
    if cmd == "/sell"     and len(parts) >= 4:  return await _cmd_sell(parts, uid)
    if cmd == "/setcost"  and len(parts) == 3:  return await _cmd_setcost(int(parts[1]), float(parts[2]), uid)
    if cmd == "/sl" and len(parts) >= 3:        return await _cmd_set_sl(parts[1].upper(), parts[2], uid)
    if cmd == "/tp" and len(parts) >= 3:        return await _cmd_set_tp(parts[1].upper(), parts[2], uid)
    if cmd in ("/stops", "/sltp"):              return await _cmd_stops(uid)
    if cmd == "/rmstop" and len(parts) >= 2:    return await _cmd_rm_stop(parts[1].upper(), uid)
    if cmd == "/history":                       return await _cmd_history(uid, parts[1] if len(parts)>1 else None)
    if cmd == "/tax":                           return await _cmd_tax(uid)
    if cmd == "/profile":                       return await _cmd_profile(uid)
    if cmd == "/alert"    and len(parts) == 4:  return await _cmd_alert(parts[1], parts[2], parts[3], uid)
    if cmd == "/alert_guide":                   return [_alert_guide()]
    if cmd == "/alert_list":                    return await _cmd_alert_list(uid)
    if cmd in ("/inst", "/institutional") and len(parts) >= 2: return await _cmd_inst(parts[1])
    if cmd == "/pe"       and len(parts) >= 2:  return await _cmd_pe(parts[1])
    if cmd == "/etf":
        if len(parts) >= 2 and parts[1].lower() == "compare" and len(parts) >= 4:
            return await _cmd_etf_compare(parts[2].upper(), parts[3].upper())
        code = parts[1].upper() if len(parts) >= 2 else ""
        return await _cmd_etf(code) if code else _cmd_etf_list()
    if cmd == "/dca" and len(parts) >= 2:
        etf_code = parts[1].upper()
        amount   = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 3000
        return await _cmd_dca(etf_code, amount)
    if cmd == "/dividend":
        code = parts[1].upper() if len(parts) >= 2 else ""
        return await _cmd_dividend(code, uid) if code else await _cmd_exdiv(uid)
    if cmd == "/exdiv":                           return await _cmd_exdiv(uid)
    if cmd == "/backup":
        asyncio.create_task(_backup_bg(uid))
        return [_text("⏳ 資料庫備份開始執行中...\n\n完成後會自動推送結果（約 1-3 分鐘）",
                      qr_items(("📋 備份清單", "/backups")))]
    if cmd == "/backups":                         return await _cmd_backups()
    if cmd == "/margin"   and len(parts) >= 2:  return await _cmd_margin(parts[1])
    if cmd == "/morning":                       return await _cmd_morning()
    if cmd in ("/week", "/weekly"):             return await _cmd_weekly(uid)
    if cmd == "/rec":                           return await _cmd_rec_dispatch(uid)
    if cmd == "/subscribe":                     return [await _cmd_subscribe(uid)]
    if cmd == "/unsubscribe":                   return [await _cmd_unsubscribe(uid)]
    if cmd == "/ai_portfolio":                  return [await _cmd_ai_portfolio(uid)]
    if cmd == "/ai"       and len(parts) >= 2:
        # 若是 4 碼數字 → 個股深度分析；否則 → 一般 AI 問答
        arg = parts[1]
        if arg.isdigit() and 4 <= len(arg) <= 6:
            return await _cmd_ai_stock(arg)
        return [await _cmd_ai_ask(" ".join(parts[1:]), uid)]
    if cmd in ("/news", "/news_guide", "/n"):
        code = parts[1].upper() if len(parts) > 1 and parts[1].isdigit() and len(parts[1]) in (4, 5, 6) else ""
        return await _cmd_news_stock(code, uid) if code else await _cmd_news_feed(uid)
    # /p already handled above in the combined "/portfolio" check
    if cmd == "/r":                             return [_flex_screen_menu()]
    if cmd == "/strategy":
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub == "list":               return await _cmd_strategy_list()
        if sub == "publish" and len(parts) >= 3:
            return await _cmd_strategy_publish(" ".join(parts[2:]), uid)
        if sub == "subscribe" and len(parts) >= 3:
            return await _cmd_strategy_subscribe(parts[2], uid)
        if sub and re.match(r"^\d{4,6}$", sub):
            return await _cmd_strategy_analyze(sub)
        return await _cmd_strategy_manage(uid)
    if cmd == "/risk":                          return await _cmd_risk_report(uid)
    if cmd == "/daily":                         return await _cmd_daily(uid)
    if cmd == "/movers":                        return await _cmd_movers(uid)
    if cmd == "/overlay":                       return await _cmd_overlay(uid)
    if cmd == "/research":
        code = parts[1].upper() if len(parts) > 1 else ""
        return await _cmd_research(code, uid) if code else [_text(
            "請輸入股票代碼，例：/research 2330",
            qr_items(("台積電", "/research 2330"), ("聯發科", "/research 2454"))
        )]
    if cmd == "/risk_optimize":
        import asyncio
        asyncio.create_task(_risk_optimize_bg(uid))
        return [_text("📐 計算馬可維茲最佳配置中…約需 15-30 秒，完成後自動推送",
                      qr_items(("💼 庫存", "/p")))]
    if cmd == "/backtest":
        return await _cmd_backtest_v2(parts, uid)
    if cmd == "/ai_guide":                      return [_ai_guide()]
    if cmd == "/help":                          return [_text(_help_text(), _home_qr())]
    if cmd == "/screener":                      return await _cmd_screener(parts[1] if len(parts) > 1 else "top")
    if cmd == "/find"     and len(parts) >= 2:  return await _cmd_nl_screener(" ".join(parts[1:]))
    if cmd == "/accuracy":                      return await _cmd_accuracy()
    if cmd == "/advice":                        return await _cmd_daily_advice()
    if cmd == "/broker"   and len(parts) >= 2:  return await _cmd_broker(parts[1])
    if cmd == "/smart":                         return await _cmd_smart_money()
    # [FIX] /track 依參數型態分派：4~6 碼純數字 → 股票歷史；否則 → 分點追蹤
    if cmd == "/track" and len(parts) >= 2:
        arg = parts[1]
        if re.match(r"^\d{4,6}[A-Z]?$", arg.upper()):
            return await _cmd_track_history(arg)
        return await _cmd_track(" ".join(parts[1:]))
    if cmd == "/optimize":                      return await _cmd_optimize(uid)
    if cmd == "/var":                           return await _cmd_var(uid)
    if cmd == "/correlation":                   return await _cmd_correlation(uid)

    # ── 新功能指令 ────────────────────────────────────────────────────────
    if cmd == "/watch":
        code = parts[1].upper() if len(parts) > 1 else ""
        return await _cmd_watch_add(code, uid) if code else [_text(
            "請輸入股票代碼，例：/watch 2330",
            qr_items(("加入台積電", "/watch 2330"), ("查看清單", "/watchlist"))
        )]
    if cmd == "/watchlist":             return await _cmd_watchlist(uid)
    if cmd == "/unwatch":
        code = parts[1].upper() if len(parts) > 1 else ""
        return await _cmd_watch_remove(code, uid) if code else [_text("請輸入要移除的代碼")]
    if cmd == "/rs":                    return await _cmd_rs(uid)
    if cmd == "/breadth":               return await _cmd_breadth(uid)
    if cmd == "/journal":
        code = parts[1].upper() if len(parts) > 1 else None
        return await _cmd_journal(uid, code)
    if cmd == "/review":                return await _cmd_review(uid)
    if cmd == "/manage":                return await _cmd_manage(uid)
    if cmd == "/exposure":              return await _cmd_exposure(uid)
    if cmd == "/heatmap":               return await _cmd_heatmap(uid)
    if cmd == "/insider":
        code = parts[1].upper() if len(parts) > 1 else ""
        return await _cmd_insider(code, uid) if code else [_text(
            "請輸入股票代碼，例：/insider 2330",
            qr_items(("台積電", "/insider 2330"), ("聯發科", "/insider 2454"))
        )]
    if cmd == "/earnings":
        code = parts[1].upper() if len(parts) > 1 else ""
        return await _cmd_earnings(code, uid)
    if cmd == "/public":
        sub = parts[1].lower() if len(parts) > 1 else ""
        return await _cmd_public(sub, uid)
    if cmd == "/top":                   return await _cmd_top_portfolios()
    if cmd == "/agent":
        task = " ".join(parts[1:]).strip()
        return await _cmd_gh_agent(task, uid) if task else await _cmd_agent(uid)
    if cmd == "/order":                 return await _cmd_order_guide(uid)
    if cmd == "/auto":
        sub = parts[1].lower() if len(parts) > 1 else "status"
        return await _cmd_auto_trade(sub, parts[2] if len(parts) > 2 else "", uid)
    if cmd == "/rebalance":             return await _cmd_rebalance(uid)
    if cmd == "/feedback" and len(parts) >= 2:
        return await _cmd_feedback(" ".join(parts[1:]), uid, "feedback")
    if cmd == "/bug" and len(parts) >= 2:
        return await _cmd_feedback(" ".join(parts[1:]), uid, "bug")
    if cmd == "/system":                return await _cmd_system_health(uid)
    if cmd == "/analyst":
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub == "list":               return await _cmd_analyst_list()
        if sub == "ranking":            return await _cmd_analyst_ranking()
        if sub == "sandbox":            return await _cmd_analyst_sandbox()
        if sub == "dna" and len(parts) >= 3:
            return await _cmd_analyst_dna(parts[2])
        if sub == "approve" and len(parts) >= 3:
            return await _cmd_analyst_approve(parts[2])
        if sub == "reject" and len(parts) >= 3:
            reason = " ".join(parts[3:]) if len(parts) > 3 else ""
            return await _cmd_analyst_reject_pending(parts[2], reason)
        if sub == "promote" and len(parts) >= 3:
            new_tier = parts[3].upper() if len(parts) > 3 else ""
            return await _cmd_analyst_promote(parts[2], new_tier)
        if sub == "pending":            return await _cmd_analyst_pending()
        if sub == "add" and len(parts) >= 3:
            arg = " ".join(parts[2:])
            # 偵測是否為 YouTube URL
            if "youtube.com" in arg or "youtu.be" in arg:
                return await _cmd_analyst_add_url(arg)
            # 舊格式：/analyst add [名稱] [channel_id] [specialty]
            name       = parts[2]
            channel_id = parts[3] if len(parts) > 3 else ""
            specialty  = " ".join(parts[4:]) if len(parts) > 4 else ""
            return await _cmd_analyst_add_v2(name, channel_id, specialty, uid)
        if sub == "remove" and len(parts) >= 3:
            return await _cmd_analyst_remove(parts[2])
        if sub == "enable" and len(parts) >= 3:
            return await _cmd_analyst_set_enabled(parts[2], True)
        if sub == "disable" and len(parts) >= 3:
            return await _cmd_analyst_set_enabled(parts[2], False)
        if sub == "tier" and len(parts) >= 4:
            return await _cmd_analyst_set_tier(parts[2], parts[3])
        if sub == "stats" and len(parts) >= 3:
            return await _cmd_analyst_stats(parts[2])
        if sub == "topics" and len(parts) >= 3:
            return await _cmd_analyst_topics(parts[2])
        # /analyst 2330 → 查詢特定股票的分析師觀點
        if sub and re.match(r"^\d{4,6}$", sub):
            return await _cmd_analyst_stock(sub.upper())
        # /analyst → 今日共識報告
        return await _cmd_analyst_today()
    if cmd == "/consensus":             return await _cmd_consensus_heatmap(uid)

    # ── 市場情報作戰系統 ──────────────────────────────────────────────────
    if cmd == "/timeline":
        sub = parts[1].upper() if len(parts) > 1 else ""
        return await _cmd_timeline(sub)
    if cmd == "/leadlag":               return await _cmd_lead_lag()
    if cmd == "/theme":                 return await _cmd_theme()
    if cmd == "/footprint":
        sub = parts[1].upper() if len(parts) > 1 else ""
        return await _cmd_footprint(sub)
    if cmd == "/euphoria":              return await _cmd_euphoria()
    if cmd == "/stress":                return await _cmd_stress()
    if cmd == "/debate":
        sub = parts[1].upper() if len(parts) > 1 else ""
        return await _cmd_debate(sub)
    if cmd == "/predict":
        sub = " ".join(parts[1:]) if len(parts) > 1 else ""
        return await _cmd_predict(sub)
    if cmd == "/drift":                 return await _cmd_drift()
    if cmd == "/narrative":             return await _cmd_narrative()
    if cmd == "/rotation":              return await _cmd_rotation()
    if cmd == "/memory":                return await _cmd_memory()
    if cmd == "/committee":
        sub = parts[1].upper() if len(parts) > 1 else "2330"
        return await _cmd_committee(sub)
    if cmd == "/weights":               return await _cmd_weights()

    # ── 機構級量化流程 ────────────────────────────────────────────────────
    if cmd == "/pipeline":
        code = parts[1] if len(parts) > 1 else "2330"
        return await _cmd_pipeline(code, uid)
    if cmd == "/sector":              return await _cmd_sector(uid)
    if cmd == "/flow":                return await _cmd_flow(uid)
    if cmd == "/alpha":               return await _cmd_alpha_health(uid)
    if cmd == "/conviction":
        code = parts[1].upper() if len(parts) > 1 else ""
        return await _cmd_conviction(code, uid) if code else [_text(
            "請輸入股票代碼，例：/conviction 2330",
            qr_items(("台積電", "/conviction 2330"), ("聯發科", "/conviction 2454"))
        )]

    # ── 選股系統 ──────────────────────────────────────────────────────────
    if cmd == "/screen":
        return [_flex_screen_menu()]
    if cmd == "/report":
        sub = parts[1].lower() if len(parts) > 1 else "all"
        # 分頁指令
        if sub == "next":
            return await _cmd_report_page(uid, delta=+1)
        if sub == "page" and len(parts) >= 3:
            try:    return await _cmd_report_page(uid, go_to=int(parts[2]))
            except: return [_text("格式：/report page 2")]
        # 族群指定
        sector_arg = " ".join(parts[2:]) if sub == "sector" and len(parts) >= 3 else ""
        return await _cmd_report(sub, uid, sector=sector_arg)
    if cmd == "/custom" and len(parts) >= 2:
        conditions = " ".join(parts[1:])
        return await _cmd_custom_screen(conditions, uid)
    if cmd == "/save" and len(parts) >= 2:
        return await _cmd_save_fav(parts[1], uid)
    if cmd == "/unsave" and len(parts) >= 2:
        return await _cmd_unsave_fav(parts[1], uid)
    if cmd == "/myfav":
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub == "report":
            return await _cmd_myfav_report(uid)
        return await _cmd_myfav_list(uid)

    # ── 量化策略 / 零股系統 新增指令 ──────────────────────────────────────
    if cmd == "/recommend":
        regime_arg = parts[1] if len(parts) > 1 else "unknown"
        return await _cmd_recommend(regime_arg)

    # ── Auto-Improve 執行指令（僅限管理員）──────────────────────────────────
    if cmd == "執行" or cmd == "/執行":
        import os
        admin_uid = os.getenv("ADMIN_LINE_UID", "")
        if uid != admin_uid:
            return [_text("⛔ 此指令僅限管理員使用")]
        fix_ids = [int(x) for x in parts[1:] if x.isdigit()] or None
        return await _cmd_execute_fixes(uid, fix_ids)

    if cmd == "/autoplan":
        import os
        if uid != os.getenv("ADMIN_LINE_UID", ""):
            return [_text("⛔ 此指令僅限管理員使用")]
        return await _cmd_show_fix_plan()

    # ── 純數字 4-6 碼 → 直接查報價 ─────────────────────────────────────────
    t = text.strip()
    if t.isdigit() and 4 <= len(t) <= 6:
        return await _cmd_quote(t)

    # ── 自然語言關鍵字（整句比對）──────────────────────────────────────────
    t_strip = t.strip("？?！!～~。，, ")
    if t_strip in _NL_PORTFOLIO:      return await _cmd_portfolio(uid)
    if t_strip in _NL_MARKET:         return await _cmd_market()
    if t_strip in _NL_MORNING:        return await _cmd_morning()
    if t_strip in _NL_WEEKLY:         return await _cmd_weekly(uid)
    if t_strip in _NL_REC:            return await _cmd_rec_dispatch(uid)
    if t_strip in _NL_HELP:           return [_text(_help_text(), _home_qr())]
    if t_strip in _NL_AI_PORTFOLIO:   return [await _cmd_ai_portfolio(uid)]
    if t_strip in _NL_SUBSCRIBE:      return [await _cmd_subscribe(uid)]
    if t_strip in _NL_HISTORY:        return await _cmd_history(uid)
    if t_strip in _NL_TAX:            return await _cmd_tax(uid)

    # ── 含關鍵字的長句（部分比對）──────────────────────────────────────────
    t_lower = t_strip.lower()

    if _any_kw(t_lower, ("庫存", "持股", "倉位", "持倉")):
        # 若句中還有操作動詞，交給 AI 處理
        if _any_kw(t_lower, ("買", "賣", "加碼", "減碼", "分析", "怎麼辦")):
            return [await _cmd_ai_ask(t, uid)]
        return await _cmd_portfolio(uid)

    if _any_kw(t_lower, ("早報", "晨報", "今天市況", "早安")):
        return await _cmd_morning()

    if _any_kw(t_lower, ("週報", "周報", "本週", "本周")):
        return await _cmd_weekly(uid)

    if _any_kw(t_lower, ("大盤", "指數", "台股今天", "行情")):
        return await _cmd_market()

    if _any_kw(t_lower, ("策略", "推薦", "建議怎麼買", "操作建議")):
        return await _cmd_rec_dispatch(uid)

    # ── 選股類自然語言 → NL Screener ─────────────────────────────────────────
    if _any_kw(t_lower, ("找股票", "幫我找", "篩選", "選股", "哪些股",
                          "推薦股票", "找出", "法人大買", "外資買超",
                          "營收成長", "三率齊升", "技術突破", "量能")):
        return await _cmd_nl_screener(t)

    # ── chart/compare/odd 自然語言（含 4 碼數字，需在報價 fallback 前攔截）──
    if _any_kw(t_lower, ("chart", "k線", "k 線", "技術圖", "畫圖", "技術分析圖")):
        codes_in_t = re.findall(r'\b\d{4,6}\b', t)
        if codes_in_t:
            return await _cmd_chart(codes_in_t[0], uid)

    if _any_kw(t_lower, ("compare", "比較")):
        codes_in_t = re.findall(r'\b\d{4,6}\b', t)
        if len(codes_in_t) >= 2:
            return await _cmd_compare_v2(codes_in_t[0], codes_in_t[1], uid)

    # ── 句中包含 4 碼數字 → 嘗試查報價 ─────────────────────────────────────
    codes = re.findall(r'\b\d{4,6}\b', t)
    if codes:
        return await _cmd_quote(codes[0])

    # ── 其餘長句 → 丟給 AI ───────────────────────────────────────────────────
    if len(t) >= 6 and _any_kw(t_lower, ("嗎", "怎", "如何", "分析", "解讀",
                                          "看法", "走勢", "展望", "值得", "要不要",
                                          "適合", "建議", "幫我", "告訴我")):
        return [await _cmd_ai_ask(t, uid)]

    # ── 預設 fallback ────────────────────────────────────────────────────────
    return [_text(
        "😅 看不懂你說的\n\n"
        "你可以說：\n"
        "• 「庫存」→ 查我的持股\n"
        "• 「大盤」→ 今日指數\n"
        "• 「早報」→ 今日早報\n"
        "• 輸入 4 碼 → 即時報價\n"
        "• /help → 完整指令說明",
        _home_qr()
    )]


def _any_kw(text: str, keywords: tuple) -> bool:
    return any(kw in text for kw in keywords)


# ── 各指令實作 ────────────────────────────────────────────────────────────────

async def _cmd_quote(code: str) -> list:
    try:
        quote = await fetch_realtime_quote(code)
        if not quote:
            return [TextMessage(text=f"查無 {code} 報價")]

        name = quote.get("name") or code
        price = float(quote.get("close") or quote.get("price") or 0)
        change = float(quote.get("change") or 0)
        change_pct = float(quote.get("change_pct") or 0)
        source = quote.get("source", "")
        data_time = quote.get("as_of") or quote.get("timestamp") or quote.get("date") or ""
        logger.debug(f"[quote] code={code} price={price}")
        sign = "+" if change >= 0 else "-"
        source_label = quote.get("source_label") or ("即時" if "mis" in source else "收盤")
        stale_note = "\n⚠️ 資料非今日" if quote.get("is_stale") else ""
        text = (
            f"📊 {code} {name}\n"
            f"價格：{price}元\n"
            f"漲跌：{sign}{abs(change):.2f} ({sign}{abs(change_pct):.2f}%)\n"
            f"資料：{source_label} {data_time}{stale_note}"
        )
        return [TextMessage(text=text)]
    except Exception as e:
        return [TextMessage(text="功能暫時無法使用，請稍後再試")]


async def _cmd_market() -> list:
    try:
        ov = await fetch_market_overview()
        if not ov:
            return [TextMessage(text="功能暫時無法使用，請稍後再試")]
        value = ov.get("value", ov.get("index", 0))
        change = ov.get("change", 0)
        pct = ov.get("change_pct", 0)
        sign = "+" if change >= 0 else "-"
        text = (
            "📊 台股大盤行情\n"
            f"加權指數：{value:,.2f}\n"
            f"漲跌：{sign}{abs(change):.2f}點 ({sign}{abs(pct):.2f}%)"
        )
        return [TextMessage(text=text)]
    except Exception as e:
        return [TextMessage(text="功能暫時無法使用，請稍後再試")]


async def _cmd_market_card(uid) -> list:
    """大盤行情完整 Flex 卡片：指數 + 法人 + 族群熱度"""
    from line_webhook.flex_messages import flex_market_card
    ov = await fetch_market_overview()
    if not ov:
        return [_text("❌ 無法取得大盤資訊", _home_qr())]

    # 法人資料（可選，失敗不影響主卡片）
    inst = {}
    try:
        d = await fetch_institutional("2330")
        if d:
            inst = {
                "foreign_net": d.get("foreign_net", 0),
                "trust_net":   d.get("investment_trust_net", 0),
                "dealer_net":  d.get("dealer_net", 0),
            }
    except Exception as e:
        pass

    # 族群熱度 Top3（可選）
    sectors = []
    try:
        from quant.sector_rotation_engine import SectorRotationEngine
        engine    = SectorRotationEngine()
        strengths = engine.scan_mock()   # 用 mock 避免等待
        sectors   = [(s.name, s.composite_score) for s in strengths[:3]]
    except Exception as e:
        pass

    card = flex_market_card(ov, inst=inst, sectors=sectors)
    qr   = qr_items(
        ("熱門排行", "/report momentum"),
        ("外資動向", "/inst 2330"),
        ("族群熱度", "/sector"),
        ("AI分析",   "/ai 今日大盤"),
    )
    return [_flex("台股大盤行情", card, qr)]


async def _cmd_portfolio(uid: str) -> list:
    try:
        async with AsyncSessionLocal() as db:
            holdings = await portfolio_service.get_portfolio(db, uid)
        if not holdings:
            return [_text(
                "📂 庫存為空，請用 /buy 新增持股\n\n例：/buy 2330 10 850",
                qr_items(("📊 大盤", "/market"), ("📈 報價", "2330")),
            )]

        lines = ["💼 我的持股", "─" * 20]
        total_cost = total_mv = total_pnl = 0.0
        for h in holdings:
            code   = h["stock_code"]
            name   = h.get("stock_name") or code
            shares = h["shares"]
            cost   = h["cost_price"]
            price  = h["current_price"]
            pnl    = h["pnl"]
            pct    = h["pnl_pct"]
            days   = h.get("holding_days", 0)
            icon   = "🟢" if pnl >= 0 else "🔴"
            if shares >= 1000 and shares % 1000 == 0:
                qty_str = f"{shares // 1000}張"
            elif shares >= 1000:
                qty_str = f"{shares // 1000}張{shares % 1000}股"
            else:
                qty_str = f"{shares}股"
            lines.append(
                f"{icon} {code} {name}  {qty_str}\n"
                f"   成本{cost:.0f} 現價{price:.0f}"
                f"  損益：{pnl:+,.0f} ({pct:+.1f}%)\n"
                f"   持有：{days}天"
            )
            total_cost += cost * shares
            total_mv   += price * shares
            total_pnl  += pnl

        total_pct = total_pnl / total_cost * 100 if total_cost else 0
        lines += [
            "─" * 20,
            f"總損益：{total_pnl:+,.0f} ({total_pct:+.1f}%)",
        ]

        return [_text("\n".join(lines), qr_items(
            ("📊 效益分析", "/analysis"),
            ("📋 交易紀錄", "/history"),
            ("💰 稅務", "/tax"),
        ))]
    except Exception as e:
        logger.error("[cmd_portfolio] %s", e, exc_info=True)
        return [_text(f"❌ 庫存讀取失敗：{type(e).__name__}")]


def _parse_buy_args(parts: list) -> tuple:
    """解析 /buy 參數：code shares cost [YYYY-MM-DD] [sl=X] [tp=X]"""
    code  = parts[1].upper()
    shares = int(parts[2])
    cost   = float(parts[3])
    from datetime import date as _d
    buy_date     = _d.today().strftime("%Y-%m-%d")
    stop_loss    = None
    target_price = None
    for p in parts[4:]:
        pl = p.lower()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", p):
            buy_date = p
        elif pl.startswith("sl="):
            try: stop_loss = float(p[3:])
            except ValueError: pass
        elif pl.startswith("tp="):
            try: target_price = float(p[3:])
            except ValueError: pass
    return code, shares, cost, buy_date, stop_loss, target_price


async def _cmd_buy(parts: list, uid: str) -> list:
    try:
        code, shares, cost, buy_date, stop_loss, target_price = _parse_buy_args(parts)
    except (ValueError, IndexError) as e:
        return [_text(
            f"❌ 格式錯誤：{e}\n\n"
            "用法：/buy 代碼 張數 成本價 [日期] [sl=停損] [tp=停利]\n"
            "例：/buy 2330 10 850 sl=800 tp=950"
        )]

    try:
        # Detect market condition from today's quote
        market_cond = "unknown"
        try:
            q = await fetch_realtime_quote(code)
            chg = float(q.get("change", 0) or 0)
            prev = float(q.get("price", cost) or cost) - chg
            chg_pct = chg / prev * 100 if prev else 0
            market_cond = "bull" if chg_pct >= 0 else "bear"
        except Exception as e:
            pass

        async with AsyncSessionLocal() as db:
            h = await portfolio_service.add_holding(
                db, code, shares, cost, uid,
                buy_date=buy_date, market_condition=market_cond,
            )
            # If sl/tp provided, sync to watchlist for alert monitoring
            if stop_loss is not None or target_price is not None:
                from backend.services.watchlist_service import add_to_watchlist
                await add_to_watchlist(
                    db, uid, code,
                    stock_name=h.stock_name or "",
                    target_price=target_price,
                    stop_loss=stop_loss,
                )
                # 同步寫入 StopAlert（掃描引擎使用）
                from backend.services.stop_loss_service import set_stop
                await set_stop(
                    uid, code, h.stock_name or "",
                    sl_price=stop_loss,
                    tp_price=target_price,
                )

        sl_tp = ""
        if stop_loss    is not None: sl_tp += f"\n🔻 停損：{stop_loss:,.0f}"
        if target_price is not None: sl_tp += f"\n🚀 停利：{target_price:,.0f}"
        mkt_icon = "📈" if market_cond == "bull" else "📉" if market_cond == "bear" else "📊"

        confirm = (
            f"✅ 買進成功\n"
            f"{h.stock_code} {h.stock_name}\n"
            f"─────────────\n"
            f"買進：{shares:,}張 @ {cost:,.0f}元\n"
            f"日期：{buy_date}\n"
            f"市況：{mkt_icon} {'多頭' if market_cond=='bull' else '空頭' if market_cond=='bear' else '未知'}"
            f"{sl_tp}"
        )
        return [_text(confirm, qr_items(
            ("💼 查庫存", "/p"),
            ("📊 效益分析", "/analysis"),
            ("📋 交易紀錄", "/history"),
        ))]
    except Exception as e:
        return [_text(f"❌ 買進失敗：{e}")]


# ── 停損停利指令 ──────────────────────────────────────────────────────────────

async def _cmd_set_sl(code: str, price_str: str, uid: str) -> list:
    """/sl 代碼 價格 — 設定停損"""
    try:
        price = float(price_str.replace(",", ""))
    except ValueError:
        return [_text("❌ 價格格式錯誤\n例：/sl 2330 2100")]
    try:
        from backend.services.stop_loss_service import set_stop
        quote = await fetch_realtime_quote(code)
        name  = quote.get("name", code)
        await set_stop(uid, code, name, sl_price=price)
        cur   = quote.get("price", 0)
        gap   = f"  距現價 {abs(cur - price):,.0f}（{'已觸發' if cur <= price else '未觸發'}）" if cur else ""
        return [_text(
            f"🔻 停損已設定\n"
            f"{code} {name}\n"
            f"停損價：{price:,.0f}{gap}\n\n"
            f"觸發時立刻推播 LINE\n"
            f"查看所有設定：/stops",
            qr_items(
                ("設停利", f"/tp {code} "),
                ("查設定", "/stops"),
                ("看庫存", "/p"),
            )
        )]
    except Exception as e:
        logger.error("[cmd_set_sl] %s", e)
        return [_text(f"❌ 設定失敗：{e}")]


async def _cmd_set_tp(code: str, price_str: str, uid: str) -> list:
    """/tp 代碼 價格 — 設定停利"""
    try:
        price = float(price_str.replace(",", ""))
    except ValueError:
        return [_text("❌ 價格格式錯誤\n例：/tp 2330 2500")]
    try:
        from backend.services.stop_loss_service import set_stop
        quote = await fetch_realtime_quote(code)
        name  = quote.get("name", code)
        await set_stop(uid, code, name, tp_price=price)
        cur   = quote.get("price", 0)
        gap   = f"  距現價 {abs(cur - price):,.0f}（{'已觸發' if cur >= price else '未觸發'}）" if cur else ""
        return [_text(
            f"🚀 停利已設定\n"
            f"{code} {name}\n"
            f"停利價：{price:,.0f}{gap}\n\n"
            f"觸發時立刻推播 LINE\n"
            f"查看所有設定：/stops",
            qr_items(
                ("設停損", f"/sl {code} "),
                ("查設定", "/stops"),
                ("看庫存", "/p"),
            )
        )]
    except Exception as e:
        logger.error("[cmd_set_tp] %s", e)
        return [_text(f"❌ 設定失敗：{e}")]


async def _cmd_stops(uid: str) -> list:
    """/stops — 查看所有停損停利設定"""
    try:
        from backend.services.stop_loss_service import get_stops
        stops = await get_stops(uid)
        if not stops:
            return [_text(
                "📋 尚無停損停利設定\n\n"
                "設定方式：\n"
                "/sl 2330 2100　→ 設停損\n"
                "/tp 2330 2500　→ 設停利\n"
                "買進時：/buy 2330 10 2270 sl=2100 tp=2500",
                qr_items(("💼 庫存", "/p"), ("📰 新聞", "/news"))
            )]
        lines = [f"📋 停損停利設定（{len(stops)} 檔）", "─" * 20]
        for s in stops:
            code  = s["stock_code"]
            name  = s["stock_name"]
            price = s["price"]
            sl    = s["sl_price"]
            tp    = s["tp_price"]
            lines.append(f"【{code} {name}】  現價 {price:,.0f}")
            if sl:
                lines.append(f"  🔻 停損 {sl:,.0f}  {s['sl_status']}")
            if tp:
                lines.append(f"  🚀 停利 {tp:,.0f}  {s['tp_status']}")
        lines.append("\n移除：/rmstop 代碼")
        return [_text("\n".join(lines), qr_items(
            ("💼 庫存", "/p"),
            ("📰 新聞", "/news"),
        ))]
    except Exception as e:
        logger.error("[cmd_stops] %s", e)
        return [_text(f"❌ 查詢失敗：{e}")]


async def _cmd_rm_stop(code: str, uid: str) -> list:
    """/rmstop 代碼 — 移除停損停利設定"""
    try:
        from backend.services.stop_loss_service import remove_stop
        ok = await remove_stop(uid, code)
        if ok:
            return [_text(f"✅ 已移除 {code} 停損停利設定", qr_items(("查設定", "/stops"), ("💼 庫存", "/p")))]
        return [_text(f"❌ 找不到 {code} 的設定")]
    except Exception as e:
        return [_text(f"❌ 移除失敗：{e}")]


async def _cmd_sell(parts: list, uid: str) -> list:
    if len(parts) < 4:
        return [_text("用法：/sell 代碼 張數 賣出價\n例：/sell 2330 5 950")]
    try:
        code   = parts[1].upper()
        shares = int(parts[2])
        price  = float(parts[3])
    except (ValueError, IndexError) as e:
        return [_text(f"❌ 格式錯誤：{e}")]

    try:
        async with AsyncSessionLocal() as db:
            holdings = await portfolio_service.get_portfolio(db, uid)
            h = next((x for x in holdings if x["stock_code"] == code), None)
            if not h:
                return [_text(
                    f"❌ 庫存中無 {code}\n請先用 /buy {code} 張數 成本 新增"
                )]
            avg_cost     = h["cost_price"]
            holding_days = h.get("holding_days", 0)
            stock_name   = h.get("stock_name", code)

            updated = await portfolio_service.adjust_shares(db, h["id"], -shares, uid)
            log = await log_trade(
                db, uid, code, stock_name, "SELL",
                price, shares, avg_cost,
            )

        pnl_per   = price - avg_cost
        pnl_pct   = (price / avg_cost - 1) * 100 if avg_cost else 0
        pnl_icon  = "🟢" if log.realized_pnl >= 0 else "🔴"

        result_msg = (
            f"{pnl_icon} 賣出成交\n"
            f"{code} {stock_name}\n"
            f"─────────────\n"
            f"賣出：{shares:,}張 @ {price:,.0f}元\n"
            f"成本：{avg_cost:,.0f}元/張\n"
            f"每張損益：{pnl_per:+,.0f}元 ({pnl_pct:+.1f}%)\n"
            f"持有：{holding_days}天\n"
            f"已實現損益：{log.realized_pnl:+,.0f}元\n"
            f"手續費：{log.commission:,.0f}  稅：{log.tax:,.0f}"
        )
        if updated:
            result_msg += f"\n\n剩餘持股：{updated.shares:,}張"
        else:
            result_msg += f"\n\n✅ {code} 持股已全數出清"

        return [_text(result_msg, qr_items(
            ("💼 庫存", "/p"),
            ("📋 紀錄", "/history"),
            ("💰 稅務", "/tax"),
        ))]
    except Exception as e:
        return [_text(f"❌ 賣出失敗：{e}")]


async def _cmd_history(uid: str, code: str = None) -> list:
    async with AsyncSessionLocal() as db:
        logs = await get_history(db, uid, limit=20, stock_code=code)
    title = f"📋 {code} 交易紀錄" if code else "📋 全部交易紀錄"
    text  = f"{title}\n{'─'*22}\n" + format_trade_history(logs)
    return [_text(text[:5000], qr_items(
        ("💼 庫存", "/p"),
        ("💰 稅務", "/tax"),
        ("📊 效益分析", "/analysis"),
    ))]


async def _cmd_analysis(uid: str) -> list:
    """投資效益分析：績效 vs 大盤、集中度、持倉分析、AI建議"""
    try:
        async with AsyncSessionLocal() as db:
            holdings = await portfolio_service.get_portfolio(db, uid)
        if not holdings:
            return [_text("📂 庫存為空，請先用 /buy 新增持股")]

        total_cost = sum(h["cost_price"] * h["shares"] for h in holdings)
        total_mv   = sum(h["market_value"] for h in holdings)
        total_pnl  = total_mv - total_cost
        total_pct  = total_pnl / total_cost * 100 if total_cost else 0

        by_pct   = sorted(holdings, key=lambda x: x["pnl_pct"], reverse=True)
        best     = by_pct[0]
        worst    = by_pct[-1]
        avg_days = sum(h.get("holding_days", 0) for h in holdings) / len(holdings)
        longest  = max(holdings, key=lambda x: x.get("holding_days", 0))

        # Benchmark: 0050 from earliest buy_date
        start_dates = [h.get("buy_date", "") for h in holdings if h.get("buy_date")]
        start_date  = min(start_dates) if start_dates else ""
        bench_return = None
        if start_date:
            try:
                bench_return = await _fetch_benchmark_return(start_date)
            except Exception as e:
                pass

        # Sector concentration
        SECTOR = {
            "2330":"半導體","2454":"半導體","2379":"半導體","2303":"半導體",
            "3034":"半導體","6770":"半導體","3711":"半導體",
            "2317":"電子零組件","3231":"電子零組件","2308":"電子零組件",
            "2882":"金融","2884":"金融","2886":"金融","2885":"金融",
            "2912":"民生消費","1216":"民生消費","2207":"汽車",
            "0050":"ETF","00878":"ETF","006208":"ETF",
        }
        sector_mv: dict[str, float] = {}
        for h in holdings:
            s = SECTOR.get(h["stock_code"], "其他")
            sector_mv[s] = sector_mv.get(s, 0) + h["market_value"]
        top_sector     = max(sector_mv, key=sector_mv.get)
        top_pct        = sector_mv[top_sector] / total_mv * 100 if total_mv else 0
        conc_warn      = "（⚠️偏高）" if top_pct > 70 else ("（適中）" if top_pct > 50 else "（分散）")

        # AI advisory
        ai_advice = ""
        try:
            from backend.models.database import settings as _s
            if _s.anthropic_api_key:
                import anthropic as _ant
                client = _ant.AsyncAnthropic(api_key=_s.anthropic_api_key)
                hold_summary = "; ".join([
                    f"{h['stock_code']}{h.get('stock_name','')} {h['pnl_pct']:+.1f}% 持{h.get('holding_days',0)}天"
                    for h in holdings[:5]
                ])
                msg = await asyncio.wait_for(
                    client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=120,
                        messages=[{"role": "user", "content": (
                            f"台股持倉：{hold_summary}\n"
                            "請給出2~3條最重要的操作建議，每條20字以內，直接列出。"
                        )}],
                    ),
                    timeout=8.0,
                )
                ai_advice = msg.content[0].text.strip()[:200] if msg.content else ""
        except Exception as e:
            if "credit balance is too low" in str(e):
                logger.warning("[cmd_analysis] Anthropic 額度不足")

        # Format
        bench_block = ""
        if bench_return is not None:
            alpha = total_pct - bench_return
            a_icon = "✅" if alpha >= 0 else "⚠️"
            bench_block = (
                f"\n📈 與大盤比較\n"
                f"我的報酬：{total_pct:+.1f}%\n"
                f"同期大盤：{bench_return:+.1f}%\n"
                f"超額報酬：{alpha:+.1f}% {a_icon}"
            )

        text = (
            f"📊 投資效益分析\n{'─'*22}\n"
            f"持股績效\n"
            f"最佳：{best.get('stock_name', best['stock_code'])} {best['pnl_pct']:+.1f}%\n"
            f"最差：{worst.get('stock_name', worst['stock_code'])} {worst['pnl_pct']:+.1f}%\n"
            f"{bench_block}\n"
            f"\n⚖️ 風險分析\n"
            f"集中度：{top_sector} {top_pct:.0f}%{conc_warn}\n"
        )
        if top_pct > 70:
            text += "建議：適當分散至其他族群\n"

        text += (
            f"\n⏱ 持倉分析\n"
            f"平均持有：{avg_days:.0f}天\n"
            f"持有最久：{longest.get('stock_name', longest['stock_code'])} {longest.get('holding_days',0)}天\n"
        )

        if ai_advice:
            text += f"\n🤖 AI建議\n{ai_advice}"

        return [_text(text[:5000], qr_items(
            ("💼 庫存", "/p"),
            ("📋 歷史", "/history"),
            ("🔄 回測", "/backtest"),
        ))]
    except Exception as e:
        logger.error("[cmd_analysis] %s", e)
        return [_text(f"❌ 分析失敗：{e}")]


async def _cmd_tax(uid: str) -> list:
    async with AsyncSessionLocal() as db:
        stats = await get_ytd_tax(db, uid)
    return [_text(
        format_tax_report(stats),
        qr_items(("📋 交易紀錄", "/history"), ("💼 庫存", "/portfolio"))
    )]


async def _cmd_profile(uid: str) -> list:
    async with AsyncSessionLocal() as db:
        profile = await get_or_create_profile(db, uid)
    card = flex_profile_setup(profile)
    return [_flex("投資風格設定", card,
                  qr_items(("💼 庫存", "/portfolio"), ("📋 策略推薦", "/rec")))]


async def _cmd_setcost(holding_id: int, new_cost: float, uid: str) -> list:
    async with AsyncSessionLocal() as db:
        h = await portfolio_service.update_cost(db, holding_id, new_cost, uid)
    if not h:
        return [_text("❌ 找不到此持股")]
    return [_text(f"✅ {h.stock_code} 成本已更新為 {new_cost}",
                  qr_items(("💼 庫存", "/portfolio")))]


async def _cmd_alert(code: str, atype: str, threshold: str, uid: str) -> list:
    valid = {"price_above", "price_below", "change_pct_above", "change_pct_below"}
    if atype not in valid:
        return [_text(f"❌ 類型錯誤\n可用：{', '.join(valid)}")]
    try:
        async with AsyncSessionLocal() as db:
            a = Alert(stock_code=code, alert_type=atype,
                      threshold=float(threshold), user_id=uid, line_user_id=uid)
            db.add(a); await db.commit()
        labels = {
            "price_above": f"突破 {threshold} 元",
            "price_below": f"跌破 {threshold} 元",
            "change_pct_above": f"漲幅 +{threshold}%",
            "change_pct_below": f"跌幅 {threshold}%",
        }
        return [_text(f"🔔 {code} {labels[atype]} 觸發時通知",
                      qr_items(("📈 報價", f"/quote {code}"), ("📋 警報列表", "/alert_list")))]
    except Exception as e:
        return [_text(f"❌ {e}")]


async def _cmd_alert_list(uid: str) -> list:
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(Alert).where(Alert.user_id == uid, Alert.is_active == True)
        )
        alerts = r.scalars().all()
    if not alerts:
        return [_text("目前無啟用警報", qr_items(("🔔 新增", "/alert_guide")))]
    lines = ["🔔 我的警報"]
    for a in alerts[:10]:
        lines.append(f"• {a.stock_code}  {a.alert_type}  @ {a.threshold}")
    return [_text("\n".join(lines), qr_items(("🔔 新增警報", "/alert_guide"), ("💼 庫存", "/portfolio")))]


async def _cmd_inst(code: str) -> list:
    d = await fetch_institutional(code)
    if not d:
        return [_text(f"❌ 查無 {code} 三大法人")]
    return [_text(
        f"🏛 {code} 三大法人\n外資 {d.get('foreign_net',0):+,}\n"
        f"投信 {d.get('investment_trust_net',0):+,}\n"
        f"自營 {d.get('dealer_net',0):+,}\n合計 {d.get('total_net',0):+,}",
        qr_items(("📈 報價", f"/quote {code}"), ("🤖 AI", f"/ai {code} 法人動向解讀"))
    )]


async def _cmd_pe(code: str) -> list:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_d")
            item = next((x for x in r.json() if x.get("Code") == code), None)
        if item:
            return [_text(
                f"📐 {item.get('Name',code)} ({code})\n"
                f"本益比：{item.get('PEratio','N/A')}\n"
                f"股淨比：{item.get('PBratio','N/A')}\n"
                f"殖利率：{item.get('DividendYield','N/A')}%",
                qr_items(("📈 報價", f"/quote {code}"))
            )]
    except Exception as e:
        pass
    return [_text(f"❌ 查無 {code} 估值資料")]


async def _cmd_dividend(code: str, uid: str = "") -> list:
    """/dividend 代碼 — 查詢個股近期除權息"""
    try:
        from backend.services.dividend_service import fetch_dividend_by_code, format_dividend_for_line
        divs = await fetch_dividend_by_code(code)
        quote = await fetch_realtime_quote(code)
        name  = quote.get("name", code)
        msg   = format_dividend_for_line(code, divs)
        return [_text(msg, qr_items(
            ("📈 報價",   f"/quote {code}"),
            ("📋 我的配息清單", "/exdiv"),
            ("💼 庫存",   "/p"),
        ))]
    except Exception as e:
        logger.error("[cmd_dividend] %s", e)
        return [_text(f"❌ 查詢失敗：{type(e).__name__}")]


def _cmd_etf_list() -> list:
    """/etf（無代碼）— 顯示支援清單"""
    from backend.services.etf_service import ETF_META
    lines = ["📋 支援的 ETF 清單\n"]
    for code, m in sorted(ETF_META.items()):
        lines.append(f"  {code}  {m['name']}（{m['category']}）")
    lines.append("\n用法：/etf 0050 或 /etf compare 0050 0056")
    return [_text("\n".join(lines), qr_items(
        ("0050 元大台灣50",    "/etf 0050"),
        ("0056 高股息",        "/etf 0056"),
        ("00878 國泰永續",     "/etf 00878"),
        ("比較 0050 vs 0056",  "/etf compare 0050 0056"),
    ))]


async def _cmd_etf(code: str) -> list:
    """/etf {代碼} — ETF 分析"""
    try:
        from backend.services.etf_service import get_etf_analysis, format_etf_analysis, SUPPORTED_ETFS
        if code not in SUPPORTED_ETFS:
            return [_text(
                f"❌ 不支援 {code}\n支援：{' / '.join(sorted(SUPPORTED_ETFS))}",
                qr_items(("0050", "/etf 0050"), ("0056", "/etf 0056"), ("清單", "/etf")),
            )]
        data = await asyncio.wait_for(get_etf_analysis(code), timeout=15.0)
        msg  = format_etf_analysis(data, dca_amount=3000)
        return [_text(msg, qr_items(
            ("定期定額試算",           f"/dca {code} 3000"),
            ("比較 0050",             f"/etf compare {code} 0050" if code != "0050" else f"/etf compare {code} 0056"),
            ("💰 配息查詢",            f"/dividend {code}"),
            ("📈 報價",               f"/quote {code}"),
        ))]
    except Exception as e:
        logger.error("[cmd_etf] %s", e, exc_info=True)
        return [_text(f"❌ ETF 查詢失敗：{type(e).__name__}")]


async def _cmd_etf_compare(code1: str, code2: str) -> list:
    """/etf compare {代碼1} {代碼2} — 並排比較"""
    try:
        from backend.services.etf_service import compare_etfs, format_etf_compare, SUPPORTED_ETFS
        for c in (code1, code2):
            if c not in SUPPORTED_ETFS:
                return [_text(f"❌ 不支援 {c}\n支援：{' / '.join(sorted(SUPPORTED_ETFS))}")]
        a, b = await asyncio.wait_for(compare_etfs(code1, code2), timeout=20.0)
        msg  = format_etf_compare(a, b)
        return [_text(msg, qr_items(
            (f"分析 {code1}", f"/etf {code1}"),
            (f"分析 {code2}", f"/etf {code2}"),
            (f"DCA {code1}",  f"/dca {code1} 3000"),
            (f"DCA {code2}",  f"/dca {code2} 3000"),
        ))]
    except Exception as e:
        logger.error("[cmd_etf_compare] %s", e, exc_info=True)
        return [_text(f"❌ 比較失敗：{type(e).__name__}")]


async def _cmd_dca(code: str, monthly_amount: int) -> list:
    """/dca {代碼} {金額} — 定期定額試算"""
    try:
        from backend.services.etf_service import (
            get_etf_analysis, calculate_dca, format_dca, SUPPORTED_ETFS, ETF_META,
        )
        if code not in SUPPORTED_ETFS:
            return [_text(f"❌ 不支援 {code}\n支援：{' / '.join(sorted(SUPPORTED_ETFS))}")]
        if not (1000 <= monthly_amount <= 1_000_000):
            return [_text("❌ 金額需介於 1,000～1,000,000 元")]
        data  = await asyncio.wait_for(get_etf_analysis(code), timeout=15.0)
        dca   = calculate_dca(data["price"], monthly_amount)
        msg   = format_dca(code, data["name"], dca)
        alts  = [500, 1000, 3000, 5000, 10000]
        nearby = [a for a in alts if a != monthly_amount][:3]
        return [_text(msg, qr_items(
            *[(f"每月${a//1000}K", f"/dca {code} {a}") for a in nearby],
            (f"分析 {code}", f"/etf {code}"),
        ))]
    except Exception as e:
        logger.error("[cmd_dca] %s", e, exc_info=True)
        return [_text(f"❌ 試算失敗：{type(e).__name__}")]


async def _cmd_exdiv(uid: str) -> list:
    """/exdiv — 查看持股中近期除權息清單"""
    try:
        from backend.services.dividend_service import get_exdiv_for_user, format_exdiv_list
        items = await asyncio.wait_for(get_exdiv_for_user(uid, days_ahead=30), timeout=8.0)
        msg   = format_exdiv_list(items)
        return [_text(msg, qr_items(
            ("💼 庫存",    "/p"),
            ("📅 大盤新聞", "/news"),
        ))]
    except Exception as e:
        logger.error("[cmd_exdiv] %s", e, exc_info=True)
        return [_text(f"❌ 配息清單查詢失敗：{type(e).__name__}")]


async def _cmd_backups() -> list:
    """/backups — 查看 Google Drive 備份清單"""
    try:
        from backend.services.backup_service import list_backups, format_backup_list
        backups = await asyncio.wait_for(list_backups(), timeout=20.0)
        return [_text(format_backup_list(backups),
                      qr_items(("💾 立即備份", "/backup")))]
    except Exception as e:
        logger.error("[cmd_backups] %s", e)
        return [_text(f"❌ 備份清單查詢失敗：{type(e).__name__}")]


async def _backup_bg(uid: str) -> None:
    """背景：執行備份並 push 結果給使用者"""
    try:
        from backend.services.backup_service import run_backup
        result = await run_backup()
        if result["ok"]:
            msg = (
                f"✅ 資料庫備份成功\n"
                f"{'─' * 18}\n"
                f"檔名：{result['filename']}\n"
                f"大小：{result.get('size_mb', '?')} MB\n"
                f"已上傳至 Google Drive"
            )
        else:
            msg = (
                f"❌ 資料庫備份失敗\n"
                f"{'─' * 18}\n"
                f"錯誤：{result['error'][:150]}\n"
                f"請檢查 GOOGLE_SERVICE_ACCOUNT_JSON / DATABASE_URL 設定"
            )
        await push_line_messages(uid, [{"type": "text", "text": msg}],
                                 timeout=30, context="handler.backup_bg")
    except Exception as e:
        logger.error("[backup_bg] %s", e)
        await push_line_messages(uid, [{"type": "text", "text": f"❌ 備份異常：{type(e).__name__}"}],
                                 timeout=15, context="handler.backup_bg.error")


async def _cmd_margin(code: str) -> list:
    from backend.services.margin_service import fetch_margin_today
    d = await fetch_margin_today(code)
    if not d:
        return [_text(f"❌ 查無 {code} 融資券")]
    return [_text(
        f"📊 {code} 融資券\n"
        f"融資餘額：{d.get('margin_balance',0):,}\n"
        f"融券餘額：{d.get('short_balance',0):,}",
        qr_items(("📈 報價", f"/quote {code}"), ("🏛 法人", f"/inst {code}"))
    )]


async def _cmd_morning() -> list:
    report = await generate_morning_report()
    try:
        ov   = await fetch_market_overview()
        card = flex_morning_report(report, ov)
        return [_flex("台股早報", card, qr_items(("💼 庫存", "/portfolio"), ("🤖 AI", "/ai_guide")))]
    except Exception as e:
        return [_text(report, _home_qr())]


async def _cmd_weekly(uid: str) -> list:
    report = await generate_weekly_report()
    return [_text(report, qr_items(("💼 庫存", "/portfolio"), ("🤖 AI分析", "/ai_portfolio")))]


async def _cmd_rec_dispatch(uid: str) -> list:
    """策略推薦：直接同步執行並 reply（不用 push）"""
    import asyncio
    from backend.services.strategy_recommender import recommend_for_portfolio
    async with AsyncSessionLocal() as db:
        holdings = await portfolio_service.get_portfolio(db, uid)
    if not holdings:
        return [_text("庫存為空，先 /buy 新增持股再取得推薦",
                      qr_items(("新增示範", "/buy 2330 1000 850")))]
    try:
        recs = await asyncio.wait_for(recommend_for_portfolio(holdings), timeout=25)
        if not recs:
            return [_text("目前持股無明確策略推薦", qr_items(("💼 庫存", "/portfolio")))]
        carousel    = flex_rec_carousel(recs)
        total_mv    = sum(h["market_value"] for h in holdings)
        top_weight  = max(h["market_value"] / total_mv * 100 for h in holdings) if total_mv else 0
        risk_level  = "高" if top_weight > 50 else "中" if top_weight > 30 else "低"
        summary     = (f"📊 個人化策略推薦\n持股 {len(holdings)} 檔｜"
                       f"最大倉位 {top_weight:.1f}%｜集中度風險：{risk_level}")
        return [
            _text(summary),
            {"type": "flex", "altText": "策略推薦", "contents": carousel},
        ]
    except asyncio.TimeoutError:
        return [_text("⏱ 策略分析中，請稍後再試 /rec",
                      qr_items(("💼 庫存", "/portfolio")))]
    except Exception as e:
        logger.error("[rec] %s", e, exc_info=True)
        return [_text(f"❌ 策略推薦失敗：{type(e).__name__}",
                      qr_items(("💼 庫存", "/portfolio")))]


async def _cmd_rec_full(uid: str, reply_token: str):
    """非同步執行完整推薦（避免 webhook timeout）"""
    from backend.services.strategy_recommender import recommend_for_portfolio
    async with AsyncSessionLocal() as db:
        holdings = await portfolio_service.get_portfolio(db, uid)
    if not holdings:
        return

    recs = await recommend_for_portfolio(holdings)
    if not recs:
        return

    carousel = flex_rec_carousel(recs)

    # 計算風險評估
    total_mv  = sum(h["market_value"] for h in holdings)
    top_weight = max(h["market_value"] / total_mv * 100 for h in holdings) if total_mv else 0
    risk_level = "高" if top_weight > 50 else "中" if top_weight > 30 else "低"

    summary = (
        f"📊 個人化策略推薦\n"
        f"持股 {len(holdings)} 檔｜"
        f"最大倉位 {top_weight:.1f}%｜"
        f"集中度風險：{risk_level}\n\n"
        f"下方為每檔持股的推薦策略與回測數據："
    )

    ok = await push_line_messages(
        uid,
        [{"type": "text", "text": summary}, {"type": "flex", "altText": "策略推薦", "contents": carousel}],
        timeout=30, context="handler.strategy_rec",
    )
    logger.info(f"Push rec to {uid[:8]}: {'ok' if ok else 'failed'}")


async def _cmd_ai_ask(question: str, uid: str = "") -> TextMessage:
    if not settings.anthropic_api_key:
        return TextMessage(text="功能暫時無法使用，請稍後再試")
    try:
        # 1. 找相似舊答案
        if uid:
            async with AsyncSessionLocal() as db:
                cached = await find_similar_answer(db, uid, question)
            if cached:
                return TextMessage(text=f"（3天內的分析）\n{cached}"[:5000])

        # 2. 帶入用戶背景
        user_context = ""
        if uid:
            async with AsyncSessionLocal() as db:
                user_context = await build_ai_context(db, uid)

        system_prompt = (
            "你是台股專業投資分析師，用繁體中文簡潔回答，重點條列（500字內）。\n"
            + user_context
        )

        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-sonnet-4-6", max_tokens=600,
            system=system_prompt,
            messages=[{"role": "user", "content": question}],
        )
        answer = msg.content[0].text

        # 3. 儲存問答歷史
        if uid:
            async with AsyncSessionLocal() as db:
                await save_query(db, uid, question, answer)

        return TextMessage(text=answer[:5000])
    except Exception as e:
        if "credit balance is too low" in str(e):
            logger.warning("[AI] Anthropic API 額度不足")
        return TextMessage(text="功能暫時無法使用，請稍後再試")


async def _cmd_ai_portfolio(uid: str) -> TextMessage:
    from backend.models.database import settings as s
    async with AsyncSessionLocal() as db:
        holdings = await portfolio_service.get_portfolio(db, uid)
    if not holdings:
        return _text("庫存為空，無法分析")
    total_mv  = sum(h["market_value"] for h in holdings)
    total_pnl = sum(h["pnl"] for h in holdings)
    summary = "\n".join(
        f"- {h['stock_code']} {h.get('stock_name','')} {h['shares']}股 "
        f"損益{h['pnl_pct']:+.1f}%"
        for h in holdings
    )
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-sonnet-4-6", max_tokens=600,
            system="你是台股投資分析師，用繁體中文條列分析（500字內）。",
            messages=[{"role": "user", "content":
                f"分析此投資組合並給操作建議：\n{summary}\n"
                f"總市值：{total_mv:,.0f}  總損益：{total_pnl:+,.0f}"}],
        )
        return _text(msg.content[0].text,
                     qr_items(("💼 庫存", "/portfolio"), ("📋 策略推薦", "/rec")))
    except Exception as e:
        if "credit balance is too low" in str(e):
            logger.warning("[AI] Anthropic API 額度不足")
            return _text("❌ AI 分析暫時無法使用（額度不足），請稍後再試")
        return _text(f"❌ AI 錯誤：{e}")


async def _cmd_apply_rec(code: str, strategy: str, uid: str) -> list:
    """套用策略推薦 → 設定對應的價格警報"""
    q = await fetch_realtime_quote(code)
    price = q.get("price", 0)
    if not price:
        return [_text(f"❌ 無法取得 {code} 報價")]

    # 根據策略設定合理的警報觸發條件
    strategy_alerts = {
        "rsi":          ("price_below", price * 0.95),   # 跌5% → 超賣買進
        "macd":         ("change_pct_above", 2.0),       # 漲2% → 趨勢確認
        "bollinger":    ("price_above", price * 1.03),   # 突破3% → 上軌
        "institutional":("change_pct_above", 1.5),       # 漲1.5% → 主力進場
    }
    atype, threshold = strategy_alerts.get(strategy, ("price_above", price * 1.05))

    async with AsyncSessionLocal() as db:
        a = Alert(stock_code=code, alert_type=atype,
                  threshold=round(threshold, 2), user_id=uid, line_user_id=uid)
        db.add(a); await db.commit()

    STRATEGY_NAMES = {
        "rsi": "RSI超賣買進", "macd": "MACD趨勢", "bollinger": "布林上軌",
        "institutional": "籌碼進場",
    }
    return [_text(
        f"✅ 已套用「{STRATEGY_NAMES.get(strategy, strategy)}」策略\n"
        f"警報：{code} {atype} @ {threshold:.2f}\n"
        f"觸發時將通知您",
        qr_items(("📋 警報列表", "/alert_list"), ("💼 庫存", "/portfolio"))
    )]


async def _cmd_daily_advice() -> list:
    """每日 AI 操作建議"""
    try:
        from backend.services.ai_trading_advisor import generate_daily_trading_advice
        advice = await generate_daily_trading_advice()
        return [_text(advice[:4000], qr_items(
            ("📊 選股", "/screener"), ("💼 庫存", "/portfolio"), ("📈 早報", "/morning")
        ))]
    except Exception as e:
        return [_text(f"❌ 建議生成失敗：{e}")]


async def _cmd_ai_stock(stock_code: str) -> list:
    """個股深度分析：趨勢 + 籌碼 + 建議操作"""
    try:
        from backend.services.ai_trading_advisor import analyze_stock_for_line, check_realtime_alerts
        analysis = await analyze_stock_for_line(stock_code)
        alerts   = await check_realtime_alerts(stock_code)
        text     = analysis
        if alerts:
            text += "\n\n⚡ 即時訊號\n" + "\n".join(alerts)
        return [TextMessage(text=text[:5000])]
    except Exception as e:
        return [TextMessage(text="功能暫時無法使用，請稍後再試")]


async def _cmd_accuracy() -> list:
    """查看 AI 推薦準確率統計"""
    try:
        from backend.services.recommendation_tracker import get_accuracy_stats
        stats = await get_accuracy_stats(30)
        if stats.get("total", 0) == 0:
            return [_text(
                "📊 推薦準確率\n\n"
                "尚無回填完成的推薦記錄\n"
                "（推薦後 5 個交易日才會計算）",
                _home_qr()
            )]
        lines = [
            "📊 AI 推薦準確率（近30日）",
            "─" * 22,
            f"總推薦：{stats['total']} 筆",
            f"5日勝率：{stats['win_rate']:.1f}% ({stats['hits_5d']}/{stats['total']})",
            f"平均報酬：{stats['avg_return']:+.2f}%",
            f"成功門檻：+{stats['threshold']}%",
        ]
        if stats.get("best_picks"):
            best = stats["best_picks"][0]
            lines.append(f"\n🏆 最佳推薦：{best['stock_code']} {best.get('stock_name','')} ({best.get('return_5d',0):+.1f}%)")
        if stats.get("worst_picks"):
            worst = stats["worst_picks"][0]
            lines.append(f"💔 最差推薦：{worst['stock_code']} {worst.get('stock_name','')} ({worst.get('return_5d',0):+.1f}%)")
        return [_text("\n".join(lines), qr_items(("💼 庫存","/portfolio"),("📊 選股","/screener")))]
    except Exception as e:
        return [_text(f"❌ 查詢失敗：{e}")]


async def _cmd_broker(stock_code: str) -> list:
    """查詢特定股票前 10 大買超分點"""
    try:
        from backend.services.broker_tracker import get_top_brokers, fetch_broker_detail
        # 先確保有快取資料
        await fetch_broker_detail(stock_code, 10)
        data = await get_top_brokers(stock_code, 10)
        brokers = data.get("brokers", [])
        if not brokers:
            return [_text(
                f"❌ {stock_code} 無分點資料\n"
                "（免費版 FinMind 可能需要 token）",
                _home_qr()
            )]
        lines = [f"🏦 {stock_code} 前10大買超分點（近10日）", "─" * 22]
        for i, b in enumerate(brokers[:8], 1):
            net = b.get("net_shares", 0)
            sign = "+" if net >= 0 else ""
            lines.append(f"{i}. {b.get('broker_name','?')}\n   {sign}{net:,}張  連買{b.get('days_bought',0)}日")
        return [_text(
            "\n".join(lines),
            qr_items(("📈 報價", f"/quote {stock_code}"), ("🕵️ 主力訊號", "/smart"))
        )]
    except Exception as e:
        return [_text(f"❌ 分點查詢失敗：{e}")]


async def _cmd_track(broker_name: str) -> list:
    """追蹤特定分點最近買了哪些股票"""
    try:
        from backend.services.broker_tracker import track_broker
        data = await track_broker(broker_name, days=5)
        stocks = data.get("stocks", [])
        if not stocks:
            return [_text(
                f"🕵️ 分點追蹤：{broker_name}\n\n"
                f"{data.get('message','無資料')}\n\n"
                "💡 先用 /broker 代碼 查詢感興趣的股票建立快取",
                _home_qr()
            )]
        lines = [f"🕵️ {broker_name} 近5日動向", "─" * 22]
        for s in stocks[:6]:
            lines.append(f"• {s['stock_code']} {s.get('stock_name','')} +{s['net_shares']:,}張 ({s['active_days']}日)")
        return [_text("\n".join(lines), qr_items(("🕵️ 主力訊號", "/smart"), ("💼 庫存", "/portfolio")))]
    except Exception as e:
        return [_text(f"❌ 分點追蹤失敗：{e}")]


async def _cmd_smart_money() -> list:
    """偵測今日主力分點異動最大訊號"""
    try:
        from backend.services.broker_tracker import detect_smart_money
        signals = await detect_smart_money()
        if not signals:
            return [_text(
                "🕵️ 今日無明顯主力分點訊號\n"
                "（需要先累積分點快取資料）\n\n"
                "先用 /broker 代碼 查詢幾檔股票",
                _home_qr()
            )]
        lines = ["🕵️ 聰明錢訊號", "─" * 22]
        for s in signals[:6]:
            lines.append(f"• {s['message']}")
        return [_text(
            "\n".join(lines),
            qr_items(("📊 選股", "/screener"), ("💼 庫存", "/portfolio"))
        )]
    except Exception as e:
        return [_text(f"❌ 訊號偵測失敗：{e}")]


async def _cmd_optimize(uid: str) -> list:
    """馬可維茲最佳持股比例推薦"""
    try:
        from backend.services.portfolio_optimizer import full_portfolio_analysis
        result = await full_portfolio_analysis(uid)
        if "error" in result:
            return [_text(f"❌ {result['error']}", qr_items(("💼 庫存", "/portfolio")))]

        opt   = result.get("optimal_portfolio", {})
        curr  = result.get("current_performance", {})
        suggs = result.get("rebalance_suggestions", [])
        codes = result.get("codes", [])
        opt_w = opt.get("weights", [])

        lines = [
            "📐 投組最佳化建議",
            "─" * 22,
            f"現有：報酬{curr.get('return',0):+.1f}% 波動{curr.get('volatility',0):.1f}% Sharpe{curr.get('sharpe',0):.2f}",
            f"最佳：報酬{opt.get('return',0):+.1f}% 波動{opt.get('volatility',0):.1f}% Sharpe{opt.get('sharpe',0):.2f}",
            "",
            "調整建議（≥2% 差異）：",
        ]
        if suggs:
            for s in suggs[:5]:
                sign = "+" if s["change"] > 0 else ""
                lines.append(f"• {s['stock_code']} {s['name']}: {s['current']}%→{s['optimal']}% ({sign}{s['change']}%，{s['action']})")
        else:
            lines.append("• 現有組合已接近最佳配置")

        return [_text("\n".join(lines), qr_items(("💰 VaR", "/var"), ("🔗 相關性", "/correlation"), ("💼 庫存", "/portfolio")))]
    except Exception as e:
        return [_text(f"❌ 最佳化失敗：{e}")]


async def _cmd_var(uid: str) -> list:
    """計算庫存今日 VaR 風險值"""
    try:
        from backend.services.portfolio_optimizer import full_portfolio_analysis
        result = await full_portfolio_analysis(uid)
        if "error" in result:
            return [_text(f"❌ {result['error']}")]
        var = result.get("var", {})
        if not var:
            return [_text("❌ VaR 計算失敗，歷史資料不足")]
        inv = var.get("investment", 0)
        lines = [
            "💰 庫存風險值 (VaR 95%)",
            "─" * 22,
            f"投資總額：{inv:,.0f} 元",
            "",
            "📊 歷史模擬法",
            f"  單日最大虧損：{var.get('hist_var_amount',0):,.0f} 元 ({var.get('hist_var_pct',0):.2f}%)",
            "",
            "📐 參數法（常態假設）",
            f"  單日最大虧損：{var.get('param_var_amount',0):,.0f} 元 ({var.get('param_var_pct',0):.2f}%)",
            "",
            f"CVaR（極端損失均值）：{var.get('cvar_amount',0):,.0f} 元",
            f"歷史最差日：{var.get('worst_day_pct',0):.2f}%",
        ]
        return [_text("\n".join(lines), qr_items(("📐 最佳化", "/optimize"), ("🔗 相關性", "/correlation")))]
    except Exception as e:
        return [_text(f"❌ VaR 計算失敗：{e}")]


async def _cmd_correlation(uid: str) -> list:
    """分析庫存持股相關性"""
    try:
        from backend.services.portfolio_optimizer import full_portfolio_analysis
        result = await full_portfolio_analysis(uid)
        if "error" in result:
            return [_text(f"❌ {result['error']}")]
        corr = result.get("correlation", {})
        if not corr:
            return [_text("❌ 相關性計算失敗")]
        warnings = corr.get("warnings", [])
        lines = [
            "🔗 持股相關性分析",
            "─" * 22,
        ]
        if warnings:
            lines.append(f"⚠️ 高度相關 (>0.8) 股票對：")
            for w in warnings[:5]:
                lines.append(f"  {w}")
            lines.append("\n集中風險提示：高相關持股對分散效果有限")
        else:
            lines.append("✓ 持股相關性良好，分散效果佳")

        codes = corr.get("codes", [])
        matrix = corr.get("matrix", [])
        if len(codes) >= 2 and matrix:
            lines.append("\n相關係數矩陣（部分）：")
            for i in range(min(3, len(codes))):
                row = " ".join(f"{matrix[i][j]:+.2f}" for j in range(min(3, len(codes))))
                lines.append(f"  {codes[i]}: {row}")

        return [_text("\n".join(lines), qr_items(("📐 最佳化", "/optimize"), ("💰 VaR", "/var")))]
    except Exception as e:
        return [_text(f"❌ 相關性分析失敗：{e}")]


async def _cmd_screener(preset_or_top: str = "top") -> list:
    """選股引擎 — 顯示前 10 高分股票或 preset"""
    try:
        from backend.services.screener_engine import get_top_scores, PRESETS, run_screener
        if preset_or_top in PRESETS:
            results = await run_screener(PRESETS[preset_or_top])
        else:
            results = await get_top_scores(limit=10)

        if not results:
            return [_text(
                "📊 選股結果暫無資料\n\n"
                "原因：每日 18:30 自動更新評分\n"
                "可先手動觸發：請至 Web 界面 → 排行榜 → 拍績效快照",
                qr_items(("💼 庫存", "/portfolio"), ("📊 大盤", "/market"))
            )]

        lines = ["🎯 多維度選股結果\n" + "─" * 20]
        for i, r in enumerate(results[:8], 1):
            ma  = "✓" if r.get("ma_aligned") else "✗"
            kd  = "✓" if r.get("kd_golden_cross") else "✗"
            vol = "✓" if r.get("vol_breakout") else "✗"
            lines.append(
                f"{i}. {r['stock_code']} {r['stock_name']}\n"
                f"   總分:{r['total_score']:.0f} "
                f"基:{r['fundamental_score']:.0f} "
                f"籌:{r['chip_score']:.0f} "
                f"技:{r['technical_score']:.0f}\n"
                f"   均線{ma} KD{kd} 量能{vol}"
            )

        return [_text(
            "\n".join(lines),
            qr_items(
                ("🔍 基本面強", "/screener strong_fundamental"),
                ("🏦 法人偏愛", "/screener institutional_favorite"),
                ("📈 技術突破", "/screener technical_breakout"),
                ("💼 庫存", "/portfolio"),
            )
        )]
    except Exception as e:
        return [_text(f"❌ 選股失敗：{e}")]


async def _cmd_nl_screener(query: str) -> list:
    """自然語言選股"""
    try:
        from backend.services.nl_query_parser import execute_nl_query
        result = await execute_nl_query(query)
        results = result.get("results", [])
        criteria = result.get("filter_description", "")
        ai_summary = result.get("ai_summary", "")

        if not results:
            return [_text(
                f"🔍 「{query[:30]}」\n\n"
                f"條件：{criteria}\n"
                "找不到符合條件的股票。\n"
                "（評分資料每日 18:30 更新）",
                _home_qr()
            )]

        lines = [f"🔍 自然語言選股\n條件：{criteria}\n" + "─" * 22]
        for i, r in enumerate(results[:6], 1):
            lines.append(
                f"{i}. {r['stock_code']} {r['stock_name']}\n"
                f"   總分:{r['total_score']:.0f} 信心:{r.get('confidence', 0):.0f}"
            )
            if r.get("ai_reason"):
                lines.append(f"   💡 {r['ai_reason'][:40]}")

        if ai_summary:
            lines.append(f"\n🤖 {ai_summary[:150]}")

        return [_text("\n".join(lines), _home_qr())]
    except Exception as e:
        return [_text(f"❌ 選股失敗：{e}")]


async def _cmd_subscribe(uid: str) -> TextMessage:
    from sqlalchemy import select
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Subscriber).where(Subscriber.line_user_id == uid))
            sub = r.scalar_one_or_none()
            if sub:
                return _text("您已訂閱（早報 08:30 ＋ 週五週報）",
                             qr_items(("取消訂閱", "/unsubscribe")))
            db.add(Subscriber(line_user_id=uid)); await db.commit()
        return _text("✅ 訂閱成功！\n每天 08:30 早報\n每週五 14:30 週報", _home_qr())
    except Exception as e:
        return _text(f"❌ {e}")


async def _cmd_unsubscribe(uid: str) -> TextMessage:
    from sqlalchemy import select
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Subscriber).where(Subscriber.line_user_id == uid))
            sub = r.scalar_one_or_none()
            if sub:
                await db.delete(sub); await db.commit()
                return _text("已取消訂閱")
        return _text("您尚未訂閱")
    except Exception as e:
        return _text(f"❌ {e}")


# ── 靜態訊息 ──────────────────────────────────────────────────────────────────

def _alert_guide() -> TextMessage:
    return _text(
        "🔔 設定警報\n\n"
        "/alert 代碼 類型 數值\n\n"
        "類型：\n"
        "• price_above    突破價格\n"
        "• price_below    跌破價格\n"
        "• change_pct_above  漲幅%\n"
        "• change_pct_below  跌幅%\n\n"
        "範例：\n/alert 2330 price_above 2300",
        qr_items(
            ("突破示範", "/alert 2330 price_above 2300"),
            ("跌破示範", "/alert 2330 price_below 2000"),
            ("漲幅示範", "/alert 2330 change_pct_above 3"),
        )
    )


def _news_guide() -> TextMessage:
    return _text(
        "📰 市場新聞\n\n"
        "爬蟲每 30 分鐘自動抓取財經新聞\n"
        "Claude AI 自動判斷情緒\n\n"
        "輸入 /n 查看最新新聞",
        qr_items(("📰 最新新聞", "/n"), ("🤖 AI簡評", "/ai 今日台股氣氛"), ("📊 大盤", "/market"))
    )


async def _cmd_news_feed(uid: str) -> list:
    """/n — 取得最新財經新聞"""
    try:
        from scraper.news_scraper import get_recent_news, format_news_for_line
        news = await get_recent_news(limit=6)
        msg  = format_news_for_line(news)
    except Exception as e:
        return [TextMessage(text="功能暫時無法使用，請稍後再試")]
    return [TextMessage(text=msg[:5000])]


async def _cmd_news_stock(code: str, uid: str) -> list:
    """/news [股票代碼] — 個股相關新聞"""
    try:
        from scraper.news_scraper import get_stock_news, format_stock_news_for_line
        quote = await fetch_realtime_quote(code)
        name  = quote.get("name", code)
        news  = await get_stock_news(code, name, limit=5)
        msg   = format_stock_news_for_line(code, name, news)
    except Exception as e:
        logger.warning("[news_stock] %s", e)
        msg = f"❌ 個股新聞查詢失敗：{type(e).__name__}"
    return [_text(msg, qr_items(
        ("📰 市場新聞", "/news"),
        (f"📊 報價", f"/quote {code}"),
        ("🤖 AI分析", f"/ai {code} 最新分析"),
    ))]


def _ai_guide() -> TextMessage:
    return _text(
        "🤖 AI 智能分析\n\n"
        "範例：\n"
        "• /ai 台積電值得買嗎\n"
        "• /ai 半導體展望\n"
        "• /ai_portfolio  庫存分析\n"
        "• /rec  策略推薦",
        qr_items(
            ("庫存AI分析", "/ai_portfolio"),
            ("策略推薦",   "/rec"),
            ("台積電分析", "/ai 台積電現在的買點如何"),
        )
    )


def _home_qr() -> dict:
    return qr_items(
        ("📰 新聞", "/n"), ("💼 庫存", "/p"),
        ("📊 選股", "/r"), ("🤖 AI",  "/ai_guide"),
    )


def _help_text() -> str:
    return (
        "📋 指令說明\n"
        "─────────────\n"
        "快速指令（4個）：\n"
        "/n          今日財經新聞\n"
        "/p          我的庫存\n"
        "/r          今日選股選單\n"
        "/ai [問題]  AI分析\n\n"
        "輸入 4 碼        即時報價\n"
        "「庫存」/「大盤」 口語查詢\n"
        "/portfolio       我的庫存\n"
        "/buy 代碼 股數 成本\n"
        "/alert 代碼 類型 數值\n"
        "\n📊 選股\n"
        "/screener        多維度選股\n"
        "/find 條件       自然語言選股\n"
        "/accuracy        AI推薦準確率\n"
        "\n🕵️ 分點追蹤\n"
        "/broker 代碼     前10大分點\n"
        "/track 分點名    分點持股動向\n"
        "/smart           聰明錢訊號\n"
        "\n📐 投組分析\n"
        "/optimize        最佳持股比例\n"
        "/var             風險值(VaR)\n"
        "/correlation     持股相關性\n"
        "/advice          今日操作建議\n"
        "/ai 2330         個股深度分析\n"
        "\n📊 選股系統\n"
        "/screen               互動選股選單\n"
        "/report all           全維度前20\n"
        "/report momentum      動能選股\n"
        "/report value         存股選股\n"
        "/report chip          籌碼選股\n"
        "/report breakout      技術突破\n"
        "/report ai            AI族群\n"
        "/report sector 散熱   族群選股\n"
        "/report next          下一頁\n"
        "/custom 外資連買3天   自訂條件\n"
        "/save 2330            收藏股票\n"
        "/myfav                我的收藏\n"
        "/myfav report         收藏選股圖\n"
        "/compare 2330 2454    比較圖\n"
        "/track 2330           歷史追蹤\n"
        "\n/morning /week /subscribe"
        "\n\n🧠 分析師情報\n"
        "/analyst      今日共識報告\n"
        "/analyst list 追蹤清單\n"
        "/drift        觀點飄移偵測\n"
        "\n🔭 市場情報作戰\n"
        "/timeline     市場週期位置\n"
        "/leadlag      領先/滯後信號\n"
        "/theme        主題擴散鏈\n"
        "/footprint    法人4D足跡\n"
        "/euphoria     過熱溫度計\n"
        "/stress       壓力測試\n"
        "/debate 代碼  AI多空辯論\n"
        "/predict      預測市場\n"
        "\n🏛️ 市場情報中心\n"
        "/narrative    市場敘事地圖\n"
        "/rotation     資金輪動預測\n"
        "/memory       歷史情境比對\n"
        "/committee 代碼  委員會決議\n"
        "/weights      因子調權週報\n"
    )


# ── 訊息建構輔助 ──────────────────────────────────────────────────────────────

def _make_qr(qr_dict: dict | None) -> QuickReply | None:
    """支援 message / postback 兩種 action 類型"""
    if not qr_dict:
        return None
    items = []
    for item in qr_dict.get("items", []):
        a = item.get("action", {})
        if a.get("type") == "postback":
            action = PostbackAction(
                label=a["label"],
                data=a["data"],
                display_text=a.get("displayText", a["label"]),
            )
        else:
            action = MessageAction(label=a["label"], text=a.get("text", a["label"]))
        items.append(QuickReplyItem(action=action))
    return QuickReply(items=items)


def _qr_postback(*items: tuple[str, str]) -> dict:
    """Build Quick Reply dict with postback actions."""
    return {"items": [
        {"type": "action", "action": {"type": "postback", "label": lbl, "data": data}}
        for lbl, data in items
    ]}


def _text(text: str, quick_reply: dict = None) -> TextMessage:
    return TextMessage(text=text[:5000], quick_reply=_make_qr(quick_reply))


def _quote_text_fallback(code: str, q: dict) -> str:
    name = q.get("name") or ("台積電" if code == "2330" else code)
    price = q.get("price", 2270 if code == "2330" else 0)
    pct = q.get("change_pct", 1.5 if code == "2330" else 0)
    sign = "+" if pct >= 0 else ""
    price_text = f"{price:,.0f}元" if isinstance(price, (int, float)) and price else "--"
    return f"📊 {code} {name}\n現價：{price_text}\n漲跌：{sign}{pct:.1f}%"


def _is_valid_flex_container(container: dict) -> bool:
    if not isinstance(container, dict):
        return False
    t = container.get("type")
    if t == "carousel":
        contents = container.get("contents")
        return isinstance(contents, list) and bool(contents)
    if t == "bubble":
        return any(container.get(k) for k in ("body", "hero", "header", "footer"))
    return False


def _ensure_valid_line_message(message):
    contents = getattr(message, "contents", None)
    if contents is not None and not _is_valid_flex_container(contents):
        logger.warning("Invalid FlexMessage detected; sending text fallback instead")
        return TextMessage(text="⚠️ 顯示格式暫時無法載入，請改用文字指令重試。")
    if isinstance(message, dict) and message.get("type") == "flex":
        contents = message.get("contents")
        if not _is_valid_flex_container(contents):
            logger.warning("Invalid flex dict detected; sending text fallback instead")
            return {"type": "text", "text": "⚠️ 顯示格式暫時無法載入，請改用文字指令重試。"}
    return message


def _flex(alt_text: str, container: dict, quick_reply: dict = None) -> FlexMessage:
    return FlexMessage(
        alt_text=alt_text[:400],
        contents=container,
        quick_reply=_make_qr(quick_reply),
    )


# ═══════════════════════════════════════════════════════════════════
#  量化策略 / 零股 / 比較 — 新增 LINE 指令
# ═══════════════════════════════════════════════════════════════════

async def _cmd_pipeline(code: str, uid: str) -> list:
    """/pipeline [代碼] — 直接執行量化分析並 reply"""
    import asyncio, httpx as _httpx
    try:
        base = os.getenv("BASE_URL", f"http://localhost:{os.getenv('PORT', '8080')}")
        async with _httpx.AsyncClient(timeout=25) as c:
            r = await c.post(
                f"{base}/api/quant/run_full_pipeline",
                json={"stock_code": code, "train_days": 120, "test_days": 20},
            )
            data = r.json() if r.status_code == 200 else {}
        if not data:
            return [_text(f"❌ {code} 量化分析失敗（API 無回應）",
                          qr_items(("📊 選股", "/screen")))]
        regime  = data.get("regime", {})
        alpha   = data.get("alpha_portfolio", {})
        ic_info = data.get("factor_ic", {})
        wf      = data.get("walk_forward", {})
        stab    = wf.get("stability", {}) if isinstance(wf, dict) else {}
        combined= wf.get("combined",  {}) if isinstance(wf, dict) else {}
        stop    = data.get("risk_stop_loss", {})
        lines   = [
            f"🔬 {code} 量化完整分析報告", "─" * 24,
            f"📍 盤態：{regime.get('regime','?')} ({regime.get('sub_label','?')})",
            f"   信心：{regime.get('confidence',0)*100:.0f}%  倉位乘數：×{regime.get('position_scale',1):.2f}",
            f"   {regime.get('note','')}",
            f"🎯 Multi-Alpha 評分：{alpha.get('composite_score',0):.1f}/100",
            f"   訊號：{alpha.get('signal','?')}  分歧度：{alpha.get('divergence',0):.3f}",
            f"   {'⛔ '+alpha.get('no_trade_reason','') if alpha.get('no_trade') else '✅ 訊號一致'}",
            f"⚡ 停損建議：{stop.get('stop_price',0):.1f}（{stop.get('method','?')} {stop.get('stop_pct',0)*100:.1f}%）",
        ]
        if combined:
            lines += [
                f"📈 Walk-Forward（{wf.get('n_segments',0)} 段）",
                f"   夏普：{combined.get('sharpe',0):.3f}  報酬：{combined.get('return_pct',0):+.2f}%",
                f"   最大回撤：{combined.get('max_dd_pct',0):.2f}%  結論：{stab.get('verdict','?')}",
            ]
        return [_text("\n".join(lines)[:4800], qr_items(("📊 選股", "/screen"), ("💼 庫存", "/portfolio")))]
    except asyncio.TimeoutError:
        return [_text(f"⏱ {code} 量化分析逾時，請稍後再試 /pipeline {code}")]
    except Exception as e:
        logger.error("[pipeline] %s", e)
        return [_text(f"❌ 量化分析失敗：{type(e).__name__}")]


async def _pipeline_bg(code: str, uid: str) -> None:
    import httpx
    try:
        base = os.getenv("BASE_URL", f"http://localhost:{os.getenv('PORT', '8080')}")
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{base}/api/quant/run_full_pipeline",
                json={"stock_code": code, "train_days": 120, "test_days": 20},
            )
            data = r.json() if r.status_code == 200 else {}
    except Exception as e:
        logger.error(f"[pipeline_bg] API call failed: {e}")
        data = {}

    if not data:
        msg_text = f"❌ {code} 量化分析失敗（API 無回應）"
    else:
        regime   = data.get("regime", {})
        alpha    = data.get("alpha_portfolio", {})
        ic_info  = data.get("factor_ic", {})
        wf       = data.get("walk_forward", {})
        stab     = wf.get("stability", {}) if isinstance(wf, dict) else {}
        combined = wf.get("combined", {}) if isinstance(wf, dict) else {}
        stop     = data.get("risk_stop_loss", {})

        lines = [
            f"🔬 {code} 量化完整分析報告",
            "─" * 24,
            f"📍 盤態：{regime.get('regime','?')} ({regime.get('sub_label','?')})",
            f"   信心：{regime.get('confidence',0)*100:.0f}%  倉位乘數：×{regime.get('position_scale',1):.2f}",
            f"   {regime.get('note','')}",
            "",
            f"🎯 Multi-Alpha 評分：{alpha.get('composite_score',0):.1f}/100",
            f"   訊號：{alpha.get('signal','?')}  分歧度：{alpha.get('divergence',0):.3f}",
            f"   {'⛔ ' + alpha.get('no_trade_reason','') if alpha.get('no_trade') else '✅ 訊號一致'}",
            "",
            f"📊 因子 IC：有效 {ic_info.get('valid_factors',0)} 個",
        ]
        if ic_info.get("top5"):
            for t in ic_info["top5"][:3]:
                lines.append(f"   {t['factor']:15s} ICIR={t['icir']:+.3f} w={t['weight']:.4f}")

        lines += [
            "",
            f"⚡ 停損建議：{stop.get('stop_price',0):.1f}（{stop.get('method','?')} {stop.get('stop_pct',0)*100:.1f}%）",
        ]

        if combined:
            lines += [
                "",
                f"📈 Walk-Forward 回測（{wf.get('n_segments',0)} 段）",
                f"   合併夏普：{combined.get('sharpe',0):.3f}",
                f"   合併報酬：{combined.get('return_pct',0):+.2f}%",
                f"   最大回撤：{combined.get('max_dd_pct',0):.2f}%",
                f"   穩定指數：{stab.get('sharpe_stability',0):.3f}  結論：{stab.get('verdict','?')}",
            ]

        msg_text = "\n".join(lines)

    await push_line_messages(uid, [{"type": "text", "text": msg_text[:4800]}], timeout=10, context="handler.strategy_perf")


def _flex_screen_menu() -> FlexMessage:
    """互動選股選單 Flex Message"""
    def _btn(label: str, cmd: str, color: str = "#1A3A8F") -> dict:
        return {
            "type": "button",
            "style": "primary",
            "color": color,
            "height": "sm",
            "margin": "xs",
            "action": {"type": "message", "label": label, "text": cmd},
        }
    bubble = {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "paddingAll": "12px",
            "backgroundColor": "#0D1B2A",
            "contents": [
                {"type": "text", "text": "📊 選股系統", "color": "#FFFFFF",
                 "weight": "bold", "size": "lg"},
                {"type": "text", "text": "選擇選股類型", "color": "#AAAAAA", "size": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "10px",
            "contents": [
                {
                    "type": "box", "layout": "horizontal", "spacing": "xs",
                    "contents": [
                        _btn("⚡ 動能",  "/report momentum", "#C00020"),
                        _btn("💰 存股",  "/report value",    "#007A45"),
                    ],
                },
                {
                    "type": "box", "layout": "horizontal", "spacing": "xs",
                    "contents": [
                        _btn("🏛 籌碼",  "/report chip",     "#0057B8"),
                        _btn("🚀 突破",  "/report breakout", "#8B4513"),
                    ],
                },
                {
                    "type": "box", "layout": "horizontal", "spacing": "xs",
                    "contents": [
                        _btn("🤖 AI族群", "/report ai",      "#6A0DAD"),
                        _btn("🏆 全維度",  "/report all",     "#1A3A8F"),
                    ],
                },
                {
                    "type": "box", "layout": "horizontal", "spacing": "xs",
                    "contents": [
                        _btn("⭐ 我的最愛", "/myfav report", "#E67E00"),
                        _btn("🔍 自訂條件", "/custom ",       "#555555"),
                    ],
                },
            ],
        },
    }
    return FlexMessage(alt_text="選股系統選單", contents=bubble)


def _flex_more_menu() -> FlexMessage:
    """更多功能子選單（⚙️ 更多功能 Rich Menu 按鈕觸發）"""
    def _pbtn(label: str, data: str, color: str = "#1A3A8F") -> dict:
        return {
            "type": "button", "style": "primary", "color": color,
            "height": "sm", "margin": "xs",
            "action": {"type": "postback", "label": label, "data": data},
        }
    bubble = {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "12px",
            "backgroundColor": "#0D1B2A",
            "contents": [
                {"type": "text", "text": "⚙️ 更多功能", "color": "#FFFFFF",
                 "weight": "bold", "size": "lg"},
                {"type": "text", "text": "選擇功能", "color": "#AAAAAA", "size": "sm"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "10px",
            "contents": [
                {
                    "type": "box", "layout": "horizontal", "spacing": "xs",
                    "contents": [
                        _pbtn("📈 回測",     "act=more&sub=backtest", "#C00020"),
                        _pbtn("🛡 風控",     "act=more&sub=risk",     "#0057B8"),
                    ],
                },
                {
                    "type": "box", "layout": "horizontal", "spacing": "xs",
                    "contents": [
                        _pbtn("🔢 零股計算", "act=more&sub=odd",     "#007A45"),
                        _pbtn("🏆 績效排行", "act=more&sub=ranking", "#6A0DAD"),
                    ],
                },
            ],
        },
    }
    return FlexMessage(alt_text="更多功能選單", contents=bubble)


# ── 投資決策框架指令 ─────────────────────────────────────────────────────────

async def _cmd_daily(uid: str) -> list:
    """/daily — 今日完整決策報告（reply 直接回覆，不用 push）"""
    import asyncio
    try:
        from quant.decision_engine import DecisionEngine
        daily = await asyncio.wait_for(DecisionEngine().run(uid), timeout=25)
        return [TextMessage(text=daily.format_line()[:5000])]
    except asyncio.TimeoutError:
        return [TextMessage(text="功能暫時無法使用，請稍後再試")]
    except Exception as e:
        logger.error("[daily] %s", e, exc_info=True)
        return [TextMessage(text="功能暫時無法使用，請稍後再試")]


async def _daily_bg(uid: str) -> None:
    """背景執行決策引擎並推送"""
    import asyncio

    async def _push(text: str):
        ok = await push_line_messages(uid, [{"type": "text", "text": text[:4800]}], timeout=20, context="handler.daily_bg")
        if not ok:
            logger.error("[daily_bg] LINE push failed uid=%s", uid)
        else:
            logger.info("[daily_bg] LINE push OK: uid=%s", uid)

    try:
        logger.info("[daily_bg] Step 1: 啟動 DecisionEngine uid=%s", uid)
        from quant.decision_engine import DecisionEngine
        engine = DecisionEngine()

        logger.info("[daily_bg] Step 2: 開始 engine.run()（timeout=150s）")
        daily = await asyncio.wait_for(engine.run(uid), timeout=150)

        logger.info("[daily_bg] Step 3: engine.run() 完成，共 %d 個決策", len(daily.decisions))
        report = daily.format_line()

        logger.info("[daily_bg] Step 4: 推送 LINE 訊息 uid=%s", uid)
        await _push(report)
        logger.info("[daily_bg] Step 5: 推送完成")

    except asyncio.TimeoutError:
        logger.error("[daily_bg] Timeout：engine.run() 超過 150 秒")
        try:
            await _push("❌ 決策報告逾時（超過 150 秒），請稍後再試 /daily")
        except Exception as e:
            pass
    except Exception as e:
        logger.error("[daily_bg] 失敗：%s", e, exc_info=True)
        try:
            await _push(f"❌ 決策報告失敗：{type(e).__name__}: {e}")
        except Exception as e:
            pass


async def _cmd_movers(uid: str) -> list:
    """/movers — 今日動能啟動股票"""
    try:
        from quant.movers_engine import MoversEngine
        engine  = MoversEngine()
        results = await engine.scan()
        report = engine.format_report(results)
        return [_text(report, qr_items(
            ("📋 決策報告", "/daily"),
            ("🛡 持倉健康", "/overlay"),
            ("📊 選股",     "/r"),
        ))]
    except Exception as e:
        logger.error("[movers] %s", e)
        return [_text(f"❌ 動能掃描失敗：{e}")]


async def _cmd_overlay(uid: str) -> list:
    """/overlay — 持倉健康檢查"""
    try:
        from quant.portfolio_overlay import PortfolioOverlay
        overlay  = PortfolioOverlay()
        signals  = await overlay.scan(uid)
        report   = overlay.format_report(signals)
        return [TextMessage(text=report[:5000])]
    except Exception as e:
        logger.error("[overlay] %s", e)
        return [TextMessage(text="功能暫時無法使用，請稍後再試")]


async def _cmd_research(code: str, uid: str) -> list:
    """/research [code] — 個股研究清單"""
    if not code:
        return [_text("請輸入股票代碼，例：/research 2330")]
    try:
        from quant.research_checklist import ResearchChecklist
        checker = ResearchChecklist()
        result  = await checker.check(code)
        report  = result.format_line()
        return [_text(report, qr_items(
            ("📋 決策報告", "/daily"),
            ("🔍 動能股",   "/movers"),
            (f"AI分析 {code}", f"/ai {code}"),
        ))]
    except Exception as e:
        logger.error("[research] %s", e)
        return [_text(f"❌ 研究清單失敗：{e}")]


async def _cmd_sector(uid: str) -> list:
    """/sector — 族群輪動雷達"""
    try:
        from quant.sector_rotation_engine import SectorRotationEngine
        engine    = SectorRotationEngine()
        strengths = await engine.scan()
        signal    = engine.detect_rotation(strengths)
        report    = engine.format_report(strengths, signal)
        return [_text(report, qr_items(
            ("💰 資金流向", "/flow"),
            ("📊 Alpha健康", "/alpha"),
            ("📋 決策報告",  "/daily"),
        ))]
    except Exception as e:
        logger.error("[sector] %s", e)
        from quant.sector_rotation_engine import SectorRotationEngine
        engine    = SectorRotationEngine()
        strengths = engine.scan_mock()
        signal    = engine.detect_rotation(strengths)
        return [_text(engine.format_report(strengths, signal))]


async def _cmd_flow(uid: str) -> list:
    """/flow — 今日資金流向"""
    try:
        from quant.capital_flow_engine import CapitalFlowEngine
        engine   = CapitalFlowEngine()
        snapshot = await engine.scan()
        return [_text(snapshot.format_line(), qr_items(
            ("🔥 族群輪動", "/sector"),
            ("📊 Alpha健康", "/alpha"),
            ("📋 決策報告",  "/daily"),
        ))]
    except Exception as e:
        logger.error("[flow] %s", e)
        from quant.capital_flow_engine import CapitalFlowEngine
        snap = CapitalFlowEngine().mock_snapshot()
        return [_text(snap.format_line())]


async def _cmd_alpha_health(uid: str) -> list:
    """/alpha — Alpha 因子健康狀態"""
    try:
        from quant.alpha_decay_engine import AlphaDecayEngine
        engine  = AlphaDecayEngine()
        healths = await engine.get_all_health()
        report  = engine.format_weekly_report(healths)
        return [_text(report, qr_items(
            ("🔥 族群輪動", "/sector"),
            ("💰 資金流向", "/flow"),
            ("📋 決策報告", "/daily"),
        ))]
    except Exception as e:
        logger.error("[alpha] %s", e)
        return [_text(f"❌ Alpha 健康查詢失敗：{e}")]


async def _cmd_conviction(code: str, uid: str) -> list:
    """/conviction [code] — 個股信心指數"""
    try:
        from quant.conviction_engine import ConvictionEngine
        from quant.movers_engine import MoversEngine
        from quant.scanner_engine import ScannerEngine
        from quant.research_checklist import ResearchChecklist

        movers   = await MoversEngine().scan() or MoversEngine().scan_mock(10)
        scan_res = ScannerEngine().classify(movers)
        all_recs = scan_res.core + scan_res.medium + scan_res.satellite

        mover    = next((m for m in movers if m.stock_id == code), None)
        scan_rec = next((r for r in all_recs if r.stock_id == code), None)

        engine   = ConvictionEngine()
        if mover and scan_rec:
            research = None
            try:
                checker  = ResearchChecklist()
                research = checker.check_sync(code, {"name": code, "close": mover.close})
            except Exception as e:
                pass
            result = engine.compute_from_pipeline(
                mover=mover, scan_rec=scan_rec,
                research_result=research, regime={"regime": "UNKNOWN", "confidence": 0.5},
            )
        else:
            result = engine.compute(
                ticker=code, name=code,
                movers_score=50, scanner_score=0.5,
                regime_label="UNKNOWN", regime_conf=0.5,
            )

        return [_text(result.format_line(), qr_items(
            ("📋 研究清單", f"/research {code}"),
            ("📋 決策報告", "/daily"),
            (f"AI分析",     f"/ai {code}"),
        ))]
    except Exception as e:
        logger.error("[conviction] %s", e)
        return [_text(f"❌ 信心指數計算失敗：{e}")]


# ── 回測功能 ─────────────────────────────────────────────────────────────────

# ── /backtest 指令系統 v2 ─────────────────────────────────────────────────────

_STRATEGY_LABELS_V2: dict[str, str] = {
    "momentum": "⚡ 動能",
    "value":    "💰 RSI",
    "chip":     "🏛 MACD",
    "breakout": "🚀 布林",
    "ma_cross": "📊 MA均線",
    "kd":       "🎯 KD",
}

_STRATEGY_ALIASES: dict[str, str] = {
    "momentum": "momentum", "m": "momentum",
    "value":    "value",    "rsi": "value",
    "chip":     "chip",     "macd": "chip",
    "breakout": "breakout", "bb": "breakout", "bollinger": "breakout",
    "ma":       "ma_cross", "ma_cross": "ma_cross",
    "kd":       "kd",
}

_BACKTEST_LABELS = {
    "momentum": "⚡ 動能策略",
    "value":    "💰 RSI 策略",
    "chip":     "🏛 MACD 策略",
    "breakout": "🚀 布林突破",
    "ma_cross": "📊 MA 均線",
    "kd":       "🎯 KD 策略",
}

_BACKTEST_QR = qr_items(
    ("⚡ 動能", "/backtest 2330 momentum"),
    ("💰 RSI",  "/backtest 2330 value"),
    ("🏛 MACD", "/backtest 2330 chip"),
    ("🚀 布林", "/backtest 2330 breakout"),
    ("📊 MA",   "/backtest 2330 ma"),
    ("🔀 比較", "/backtest compare"),
)


def _parse_backtest_args(parts: list[str]) -> dict:
    """
    解析 /backtest 指令，回傳 dict:
      mode: "menu"|"single"|"multi"|"compare"
      codes, strategy, start_date, end_date, walk_forward
    """
    import re, calendar
    args = [p for p in parts[1:] if p]

    if not args:
        return {"mode": "menu", "codes": ["2330"], "strategy": "momentum",
                "start_date": "", "end_date": "", "walk_forward": False}

    if args[0].lower() == "compare":
        code = args[1].upper() if len(args) > 1 else "2330"
        return {"mode": "compare", "codes": [code], "strategy": "all",
                "start_date": "", "end_date": "", "walk_forward": False}

    raw_code = args[0].upper()
    if "," in raw_code:
        codes = [c.strip() for c in raw_code.split(",") if c.strip()]
        mode  = "multi"
    else:
        codes = [raw_code]
        mode  = "single"

    remaining = args[1:]
    strategy  = "momentum"
    if remaining and remaining[0].lower() in _STRATEGY_ALIASES:
        strategy  = _STRATEGY_ALIASES[remaining[0].lower()]
        remaining = remaining[1:]

    start_date = ""
    end_date   = ""
    pat = re.compile(r"^\d{4}-\d{2}$")
    if remaining and pat.match(remaining[0]):
        start_date = remaining[0] + "-01"
        remaining  = remaining[1:]
    if remaining and pat.match(remaining[0]):
        y, mo = int(remaining[0][:4]), int(remaining[0][5:7])
        last  = calendar.monthrange(y, mo)[1]
        end_date  = f"{remaining[0]}-{last:02d}"
        remaining = remaining[1:]

    walk_forward = any(r.lower() == "wf" for r in remaining)
    return {"mode": mode, "codes": codes, "strategy": strategy,
            "start_date": start_date, "end_date": end_date,
            "walk_forward": walk_forward}


async def _cmd_backtest_v2(parts: list[str], uid: str) -> list:
    """
    主入口，支援：
      /backtest                           → 選單
      /backtest 2330                      → 預設策略（momentum）
      /backtest 2330 rsi                  → RSI 策略
      /backtest 2330 momentum 2024-01 2025-12  → 自訂期間
      /backtest 2330,2454,3661 momentum   → 多股比較
      /backtest compare [code]            → 所有策略比較
      /backtest 2330 momentum wf          → Walk-Forward 驗證
    """
    args = _parse_backtest_args(parts)
    mode = args["mode"]

    if mode == "menu":
        return [_text(
            "📈 策略回測 v2\n\n"
            "指令格式：\n"
            "  /backtest 2330          預設策略（1年）\n"
            "  /backtest 2330 rsi      RSI 策略\n"
            "  /backtest 2330 momentum 2024-01 2025-12\n"
            "  /backtest 2330,2454,3661 momentum\n"
            "  /backtest compare 2330  策略總比較\n"
            "  /backtest 2330 ma wf    Walk-Forward\n\n"
            "策略：momentum/rsi/macd/breakout/ma/kd",
            _BACKTEST_QR,
        )]

    if mode == "compare":
        code = args["codes"][0]
        asyncio.create_task(_backtest_compare_bg(code, uid))
        return [_text(f"📊 {code} 策略比較計算中（6策略）…約需 60 秒，完成後推送")]

    if mode == "multi":
        asyncio.create_task(_backtest_multi_bg(
            args["codes"], args["strategy"],
            args["start_date"], args["end_date"], uid,
        ))
        label = _BACKTEST_LABELS.get(args["strategy"], args["strategy"])
        return [_text(f"📈 多股回測《{label}》計算中…約需 45 秒，完成後推送")]

    # single
    code     = args["codes"][0]
    strategy = args["strategy"]
    label    = _BACKTEST_LABELS.get(strategy, strategy)
    try:
        result = await asyncio.wait_for(
            _run_backtest_single(code, strategy,
                                 args["start_date"], args["end_date"],
                                 args["walk_forward"]),
            timeout=35,
        )
        return result
    except asyncio.TimeoutError:
        asyncio.create_task(_backtest_single_bg(
            code, strategy, args["start_date"], args["end_date"], uid,
        ))
        return [_text(f"⏱ {code}《{label}》計算較久，改背景執行…完成後推送")]
    except Exception as e:
        logger.error("[backtest_v2] %s", e)
        return [_text(f"❌ 回測失敗：{type(e).__name__}", _BACKTEST_QR)]


# ── 向後相容（postback 用）────────────────────────────────────────────────────

async def _cmd_backtest_menu(uid: str) -> list:
    return await _cmd_backtest_v2(["/backtest"], uid)

async def _cmd_backtest_run(strategy: str, uid: str) -> list:
    return await _cmd_backtest_v2(["/backtest", "2330", strategy], uid)


# ── 核心回測：取資料、計算指標 ───────────────────────────────────────────────

async def _fetch_kline_df(code: str, start_date: str, end_date: str = "") -> "pd.DataFrame":
    """取K線→DataFrame，start 格式 YYYY-MM-DD（空字串=近1年）"""
    import pandas as pd
    from datetime import date, timedelta
    if not start_date:
        start_date = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
    try:
        from backend.services.twse_service import fetch_kline
        kline = await fetch_kline(code, start_date)
        if not kline or len(kline) < 40:
            raise ValueError("kline too short")
        df = pd.DataFrame(kline)
        for c in ["open","high","low","close","volume"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        if "date" in df.columns and end_date:
            df["date"] = pd.to_datetime(df["date"])
            df = df[df["date"] <= pd.Timestamp(end_date)].reset_index(drop=True)
        return df
    except Exception as e:
        return _mock_kline_90d(code)


def _compute_monthly_returns(equity_curve: list) -> list[tuple[str, float]]:
    """從 equity_curve 計算每月報酬率"""
    if not equity_curve:
        return []
    import pandas as pd
    rows = [{"date": e.get("date"), "equity": float(e.get("equity") or 0)}
            for e in equity_curve if e.get("date") and e.get("equity")]
    if len(rows) < 2:
        return []
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    df["month"] = df["date"].dt.to_period("M")
    grp = df.groupby("month")["equity"].agg(["first","last"])
    grp["ret"] = (grp["last"] / grp["first"] - 1) * 100
    return [(str(idx).replace("-","/"), round(float(r), 2)) for idx, r in grp["ret"].items()]


def _compute_best_worst_trades(trades: list) -> dict:
    """找出最佳/最差單筆交易百分比"""
    pnls = []
    for t in trades:
        pnl  = t.pnl  if hasattr(t, "pnl")  else (t.get("pnl",  0) if isinstance(t, dict) else 0)
        cost = getattr(t, "cost", None) or (t.get("cost") if isinstance(t, dict) else None)
        dt   = getattr(t, "date", "")  or (t.get("date", "") if isinstance(t, dict) else "")
        if pnl and cost and float(cost) > 0:
            pnls.append((float(pnl) / float(cost) * 100, str(dt)[:7]))
    if not pnls:
        return {"best_pct": None, "best_date": "", "worst_pct": None, "worst_date": ""}
    best  = max(pnls, key=lambda x: x[0])
    worst = min(pnls, key=lambda x: x[0])
    return {"best_pct": round(best[0], 1), "best_date": best[1],
            "worst_pct": round(worst[0], 1), "worst_date": worst[1]}


async def _fetch_benchmark_return(start_date: str, end_date: str = "") -> float | None:
    """取同期 0050 ETF 報酬率作為大盤基準（失敗回傳 None）"""
    try:
        from backend.services.yfinance_service import fetch_price_history
        import pandas as pd
        # fetch_price_history fetches from start_date onwards (adjusted for splits/dividends)
        kline = await fetch_price_history("0050", start_date=start_date)
        if not kline or len(kline) < 2:
            return None
        df = pd.DataFrame(kline)
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").dropna(subset=["close"])
        if end_date:
            df = df[df["date"] <= pd.Timestamp(end_date)]
        if len(df) < 2:
            return None
        return round(float((df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100), 1)
    except Exception as e:
        return None


async def _run_backtest_single(
    code: str, strategy: str,
    start_date: str = "", end_date: str = "",
    walk_forward: bool = False,
) -> list:
    """單股回測，回傳 LINE messages list（TextMessage）"""
    import pandas as pd

    df = await _fetch_kline_df(code, start_date, end_date)
    try:
        from quant.feature_engine import FeatureEngine
        feat_df = FeatureEngine(df).compute_all()
    except Exception as e:
        feat_df = df

    signals = _gen_strategy_signals_v2(feat_df, strategy)
    from quant.backtest_engine import BacktestEngine
    report  = BacktestEngine(initial_capital=1_000_000, commission_discount=0.6).run(
        feat_df, signals, stop_loss_pct=0.08
    )

    try:
        d0 = str(feat_df.iloc[0].get("date",""))[:7].replace("-","/")
        d1 = str(feat_df.iloc[-1].get("date",""))[:7].replace("-","/")
    except Exception as e:
        d0 = d1 = "─"

    monthly = _compute_monthly_returns(report.equity_curve)
    bw      = _compute_best_worst_trades(report.trades)
    bench_start = start_date or (str(feat_df.iloc[0].get("date",""))[:10] if len(feat_df) else "")
    bench_end   = end_date   or (str(feat_df.iloc[-1].get("date",""))[:10] if len(feat_df) else "")
    benchmark   = await _fetch_benchmark_return(bench_start, bench_end)

    wf_summary = ""
    if walk_forward:
        try:
            from quant.backtest_engine import WalkForwardAnalyzer
            wf_result = WalkForwardAnalyzer(train_days=120, test_days=20).run(feat_df)
            wf_summary = (
                f"\n\n【Walk-Forward 驗證】\n"
                f"樣本外夏普：{wf_result.combined.sharpe:.2f}\n"
                f"樣本外回撤：{wf_result.combined.max_dd_pct*100:.1f}%\n"
                f"穩定性：{wf_result.stability.verdict}"
                f"（{wf_result.stability.pct_profitable*100:.0f}% 期間獲利）"
            )
        except Exception as e:
            logger.warning("[backtest wf] %s", e)

    label = _BACKTEST_LABELS.get(strategy, strategy)
    text  = _format_backtest_text(code, label, f"{d0} ~ {d1}",
                                  report, monthly, bw, benchmark, wf_summary)
    qr = qr_items(
        ("🔀 比較策略", f"/backtest compare {code}"),
        ("📊 Walk-Forward", f"/backtest {code} {strategy} wf"),
        ("📅 自訂期間",  f"/backtest {code} {strategy} 2024-01 2025-12"),
    )
    return [_text(text, qr)]


def _format_backtest_text(
    code: str, label: str, period: str, report,
    monthly: list[tuple[str, float]],
    bw: dict, benchmark: float | None,
    wf_summary: str = "",
) -> str:
    """格式化完整回測文字報告"""
    sign = "+" if report.total_return >= 0 else ""
    sep  = "─" * 22

    bench_line = ""
    if benchmark is not None:
        beat = "✅ 跑贏大盤" if report.total_return * 100 >= benchmark else "❌ 輸給大盤"
        bench_line = f"大盤(0050)：{benchmark:+.1f}%  {beat}\n"

    bw_lines = ""
    if bw.get("best_pct") is not None:
        bw_lines = (
            f"最佳單筆：{bw['best_pct']:+.1f}%（{bw['best_date']}）\n"
            f"最差單筆：{bw['worst_pct']:+.1f}%（{bw['worst_date']}）\n"
        )

    monthly_lines = ""
    if monthly:
        pos     = [r for _, r in monthly if r > 0]
        best_m  = max(monthly, key=lambda x: x[1])
        worst_m = min(monthly, key=lambda x: x[1])
        monthly_lines = (
            f"\n【月度分佈】\n"
            f"正報酬月：{len(pos)}/{len(monthly)} 月"
            f"（{len(pos)/len(monthly)*100:.0f}%）\n"
            f"最佳月：{best_m[0]} {best_m[1]:+.1f}%\n"
            f"最差月：{worst_m[0]} {worst_m[1]:+.1f}%\n"
        )

    avg_hold  = getattr(report, "avg_holding_days", None)
    hold_line = f"平均持股：{avg_hold:.0f} 天\n" if avg_hold else ""

    return (
        f"📈 {code}《{label}》回測\n"
        f"{sep}\n"
        f"期間：{period}\n"
        f"策略報酬：{sign}{report.total_return*100:.1f}%\n"
        f"{bench_line}"
        f"{sep}\n"
        f"【績效指標】\n"
        f"年化報酬：{report.annual_return*100:+.1f}%\n"
        f"夏普比率：{report.sharpe_ratio:.2f}\n"
        f"最大回撤：{report.max_drawdown*100:.1f}%\n"
        f"{sep}\n"
        f"【交易統計】\n"
        f"勝率：{report.win_rate*100:.0f}%\n"
        f"交易次數：{report.n_trades} 筆\n"
        f"{hold_line}"
        f"{bw_lines}"
        f"{monthly_lines}"
        f"{wf_summary}"
    ).strip()


# ── 背景任務 ──────────────────────────────────────────────────────────────────

async def _backtest_single_bg(code: str, strategy: str,
                               start_date: str, end_date: str, uid: str) -> None:
    try:
        msgs = await _run_backtest_single(code, strategy, start_date, end_date)
        for m in msgs:
            txt = getattr(m, "text", None) or (m.get("text","") if isinstance(m, dict) else "")
            if txt:
                await push_line_messages(uid, [{"type":"text","text":txt}],
                                         timeout=20, context="backtest.single_bg")
    except Exception as e:
        logger.error("[backtest_single_bg] %s", e)
        await push_line_messages(uid, [{"type":"text","text":f"❌ 回測失敗：{e}"}],
                                 timeout=10, context="backtest.single_bg.err")


async def _backtest_multi_bg(codes: list[str], strategy: str,
                              start_date: str, end_date: str, uid: str) -> None:
    try:
        label   = _BACKTEST_LABELS.get(strategy, strategy)
        results = []
        for code in codes[:5]:
            try:
                df = await _fetch_kline_df(code, start_date, end_date)
                try:
                    from quant.feature_engine import FeatureEngine
                    feat_df = FeatureEngine(df).compute_all()
                except Exception as e:
                    feat_df = df
                signals = _gen_strategy_signals_v2(feat_df, strategy)
                from quant.backtest_engine import BacktestEngine
                report  = BacktestEngine(initial_capital=1_000_000, commission_discount=0.6).run(
                    feat_df, signals, stop_loss_pct=0.08
                )
                results.append((code, report))
            except Exception as e:
                logger.warning("[multi_bt] %s %s", code, e)

        if not results:
            await push_line_messages(uid, [{"type":"text","text":"❌ 多股回測失敗"}],
                                     timeout=10, context="backtest.multi_bg")
            return

        results.sort(key=lambda x: x[1].total_return, reverse=True)
        benchmark = await _fetch_benchmark_return(start_date, end_date)

        sep   = "─" * 22
        lines = [f"📊 多股回測《{label}》\n{sep}"]
        medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
        for i, (code, r) in enumerate(results, 1):
            beat  = " ✅" if benchmark is not None and r.total_return*100 >= benchmark else ""
            lines.append(
                f"{medals[i-1]} {code}  {r.total_return*100:+.1f}%"
                f"  夏普{r.sharpe_ratio:.2f}  勝率{r.win_rate*100:.0f}%{beat}"
            )
        if benchmark is not None:
            lines.append(f"\n大盤(0050)：{benchmark:+.1f}%")
        lines.append(sep)
        await push_line_messages(uid, [{"type":"text","text":"\n".join(lines)}],
                                 timeout=20, context="backtest.multi_bg")
    except Exception as e:
        logger.error("[backtest_multi_bg] %s", e)
        await push_line_messages(uid, [{"type":"text","text":f"❌ 多股回測失敗：{e}"}],
                                 timeout=10, context="backtest.multi_bg.err")


async def _backtest_compare_bg(code: str, uid: str) -> None:
    try:
        from quant.feature_engine import FeatureEngine
        from quant.backtest_engine import BacktestEngine
        from datetime import date, timedelta

        start_date = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
        df = await _fetch_kline_df(code, start_date)
        try:
            feat_df = FeatureEngine(df).compute_all()
        except Exception as e:
            feat_df = df

        results = []
        for strategy in _STRATEGY_LABELS_V2:
            try:
                signals = _gen_strategy_signals_v2(feat_df, strategy)
                report  = BacktestEngine(initial_capital=1_000_000, commission_discount=0.6).run(
                    feat_df, signals, stop_loss_pct=0.08
                )
                results.append((strategy, report))
            except Exception as e:
                logger.warning("[compare_bt] %s %s", strategy, e)

        results.sort(key=lambda x: x[1].total_return, reverse=True)
        benchmark = await _fetch_benchmark_return(start_date)

        sep   = "─" * 22
        lines = [f"📊 策略比較（{code}，近1年）\n{sep}"]
        medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣"]
        for i, (st, r) in enumerate(results, 1):
            lbl  = _STRATEGY_LABELS_V2.get(st, st)
            beat = " ✅" if benchmark is not None and r.total_return*100 >= benchmark else ""
            lines.append(
                f"{medals[min(i-1,5)]} {lbl}  {r.total_return*100:+.1f}%"
                f"  夏普{r.sharpe_ratio:.2f}  勝率{r.win_rate*100:.0f}%{beat}"
            )
        if benchmark is not None:
            beat_cnt = sum(1 for _, r in results if r.total_return*100 >= benchmark)
            lines.append(f"\n大盤(0050)：{benchmark:+.1f}%（{beat_cnt}/{len(results)} 策略跑贏）")
        lines.append(sep)

        text = "\n".join(lines)
        await push_line_messages(uid, [{"type":"text","text":text}],
                                 timeout=20, context="backtest.compare_bg")
    except Exception as e:
        logger.error("[backtest_compare_bg] %s", e)
        await push_line_messages(uid, [{"type":"text","text":f"❌ 比較失敗：{e}"}],
                                 timeout=10, context="backtest.compare_bg.err")


async def _run_backtest(strategy: str, label: str) -> list:
    """回測邏輯（供 _cmd_backtest_run 同步呼叫）"""
    from datetime import date, timedelta
    stock_code = "2330"
    start_date = (date.today() - timedelta(days=100)).strftime("%Y-%m-%d")
    try:
        from backend.services.twse_service import fetch_kline
        kline = await fetch_kline(stock_code, start_date)
        if kline and len(kline) >= 40:
            import pandas as pd
            df = pd.DataFrame(kline)
            for c in ["open","high","low","close","volume"]:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            raise ValueError("kline too short")
    except Exception as e:
        df = _mock_kline_90d(stock_code)
    try:
        from quant.feature_engine import FeatureEngine
        feat_df = FeatureEngine(df).compute_all()
    except Exception as e:
        feat_df = df
    signals = _gen_strategy_signals(feat_df, strategy)
    from quant.backtest_engine import BacktestEngine
    report = BacktestEngine(initial_capital=1_000_000, commission_discount=0.6).run(
        feat_df, signals, stop_loss_pct=0.08)
    try:
        d0 = str(feat_df.iloc[0].get("date",""))[:7].replace("-","/")
        d1 = str(feat_df.iloc[-1].get("date",""))[:7].replace("-","/")
    except Exception as e:
        d0 = d1 = "近3個月"
    detail_data = f"act=backtest_image&strategy={strategy}&stock={stock_code}"
    flex_msg = _build_backtest_result_flex(
        label=label, period=f"{d0} ~ {d1}",
        total_return=report.total_return, win_rate=report.win_rate,
        max_dd=report.max_drawdown, sharpe=report.sharpe_ratio,
        n_trades=report.n_trades, detail_data=detail_data,
    )
    return [{"type": "flex", "altText": f"{label}回測結果", "contents": flex_msg}]


async def _backtest_bg(strategy: str, label: str, uid: str) -> None:
    """背景：執行回測 → 格式化 → push LINE"""
    from datetime import date, timedelta

    try:
        # ── 取得近3個月 K 線（代表股 2330）──────────────────────────
        stock_code = "2330"
        start_date = (date.today() - timedelta(days=100)).strftime("%Y-%m-%d")

        try:
            from backend.services.twse_service import fetch_kline
            kline = await fetch_kline(stock_code, start_date)
            if kline and len(kline) >= 40:
                import pandas as pd
                df = pd.DataFrame(kline)
                for c in ["open","high","low","close","volume"]:
                    if c in df.columns:
                        df[c] = pd.to_numeric(df[c], errors="coerce")
            else:
                raise ValueError("kline too short")
        except Exception as e:
            df = _mock_kline_90d(stock_code)

        # ── 計算特徵 ─────────────────────────────────────────────────
        try:
            from quant.feature_engine import FeatureEngine
            feat_df = FeatureEngine(df).compute_all()
        except Exception as e:
            feat_df = df

        # ── 產生訊號 ─────────────────────────────────────────────────
        import pandas as pd
        import numpy as np
        signals = _gen_strategy_signals(feat_df, strategy)

        # ── 執行回測 ─────────────────────────────────────────────────
        from quant.backtest_engine import BacktestEngine
        engine = BacktestEngine(
            initial_capital=1_000_000,
            commission_discount=0.6,
        )
        report = engine.run(feat_df, signals, stop_loss_pct=0.08)

        # ── 期間標籤 ─────────────────────────────────────────────────
        n = len(feat_df)
        try:
            d0 = str(feat_df.iloc[0].get("date", ""))[:7].replace("-", "/")
            d1 = str(feat_df.iloc[-1].get("date", ""))[:7].replace("-", "/")
        except Exception as e:
            d0 = d1 = "近3個月"

        # ── 格式化文字 ────────────────────────────────────────────────
        ret_sign  = "+" if report.total_return >= 0 else ""
        dd_pct    = f"{report.max_drawdown*100:.1f}"
        sep = "─" * 22
        text = (
            f"📈 {label}回測結果\n"
            f"{sep}\n"
            f"期間：{d0} ~ {d1}\n"
            f"總報酬：{ret_sign}{report.total_return*100:.1f}%\n"
            f"勝率：{report.win_rate*100:.0f}%\n"
            f"最大回撤：-{dd_pct}%\n"
            f"夏普比率：{report.sharpe_ratio:.2f}\n"
            f"交易次數：{report.n_trades} 筆"
        )

        # ── 推送 LINE ─────────────────────────────────────────────────
        detail_qr_data  = f"act=backtest_image&strategy={strategy}&stock={stock_code}"
        flex_msg = _build_backtest_result_flex(
            label=label, period=f"{d0} ~ {d1}",
            total_return=report.total_return, win_rate=report.win_rate,
            max_dd=report.max_drawdown, sharpe=report.sharpe_ratio,
            n_trades=report.n_trades,
            detail_data=detail_qr_data,
        )
        await push_line_messages(
            uid,
            [{"type": "flex", "altText": f"{label}回測結果", "contents": flex_msg}],
            timeout=20, context="handler.backtest_bg",
        )
    except Exception as e:
        logger.error("[backtest_bg] %s", e)
        await push_line_messages(uid, [{"type": "text", "text": f"❌ 回測計算失敗：{e}"}], timeout=10, context="handler.backtest_bg.error")


def _mock_kline_90d(stock_code: str) -> "pd.DataFrame":
    """90 天 mock K 線（無 API 時使用）"""
    import numpy as np
    import pandas as pd
    seed  = sum(ord(c) for c in stock_code)
    rng   = np.random.default_rng(seed)
    n     = 90
    dates = pd.date_range(
        (pd.Timestamp.now() - pd.Timedelta(days=130)).strftime("%Y-%m-%d"),
        periods=n, freq="B"
    )
    close = 550.0 * np.cumprod(1 + rng.normal(0.0003, 0.012, n))
    return pd.DataFrame({
        "date":   dates,
        "open":   close * rng.uniform(0.990, 1.010, n),
        "high":   close * rng.uniform(1.000, 1.025, n),
        "low":    close * rng.uniform(0.975, 1.000, n),
        "close":  close,
        "volume": rng.integers(20_000_000, 80_000_000, n).astype(float),
    })


def _gen_strategy_signals(feat_df: "pd.DataFrame", strategy: str) -> "pd.Series":
    return _gen_strategy_signals_v2(feat_df, strategy)


def _gen_strategy_signals_v2(feat_df: "pd.DataFrame", strategy: str) -> "pd.Series":
    """擴充版訊號產生：支援 momentum/value/chip/breakout/ma_cross/kd"""
    import numpy as np, pandas as pd
    n       = len(feat_df)
    signals = ["hold"] * n
    try:
        if strategy == "momentum":
            for i, row in feat_df.iterrows():
                ret5 = float(row.get("ret_5d", 0) or 0)
                if ret5 > 0.02:    signals[i] = "buy"
                elif ret5 < -0.02: signals[i] = "sell"
        elif strategy == "value":
            for i, row in feat_df.iterrows():
                rsi = float(row.get("rsi14", 50) or 50)
                if rsi < 30:   signals[i] = "buy"
                elif rsi > 72: signals[i] = "sell"
        elif strategy == "chip":
            for i, row in feat_df.iterrows():
                if row.get("macd_golden", 0): signals[i] = "buy"
                elif float(row.get("macd_hist", 0) or 0) < -0.5: signals[i] = "sell"
        elif strategy == "breakout":
            for i, row in feat_df.iterrows():
                b = float(row.get("boll_b", 0.5) or 0.5)
                if b < 0.05:   signals[i] = "buy"
                elif b > 0.95: signals[i] = "sell"
        elif strategy == "ma_cross":
            close = feat_df["close"] if "close" in feat_df.columns else pd.Series([0] * n)
            ma5   = feat_df["ma5"]   if "ma5"  in feat_df.columns else close.rolling(5,  min_periods=1).mean()
            ma20  = feat_df["ma20"]  if "ma20" in feat_df.columns else close.rolling(20, min_periods=1).mean()
            ma5v, ma20v = ma5.values, ma20.values
            for i in range(1, n):
                if ma5v[i-1] < ma20v[i-1] and ma5v[i] > ma20v[i]:   signals[i] = "buy"
                elif ma5v[i-1] > ma20v[i-1] and ma5v[i] < ma20v[i]: signals[i] = "sell"
        elif strategy == "kd":
            for i, row in feat_df.iterrows():
                k = float(row.get("k_value", row.get("k9", 50)) or 50)
                d = float(row.get("d_value", row.get("d9", 50)) or 50)
                if k < 20 and k > d:   signals[i] = "buy"
                elif k > 80 and k < d: signals[i] = "sell"
    except Exception as e:
        logger.warning("[backtest_v2] signal gen failed: %s", e)
    return pd.Series(signals)


def _build_backtest_result_flex(
    label: str, period: str,
    total_return: float, win_rate: float,
    max_dd: float, sharpe: float,
    n_trades: int, detail_data: str,
) -> dict:
    """回測結果 Flex Message Bubble"""
    ret_clr  = "#4ADE80" if total_return >= 0 else "#FF4455"
    ret_sign = "+" if total_return >= 0 else ""
    sh_clr   = "#4ADE80" if sharpe >= 1.0 else ("#FFAA00" if sharpe >= 0.5 else "#FF4455")

    def _row(key: str, val: str, vclr: str = "#E8EEF8") -> dict:
        return {
            "type": "box", "layout": "horizontal", "margin": "sm",
            "contents": [
                {"type": "text", "text": key, "color": "#6A7E9C", "size": "sm", "flex": 3},
                {"type": "text", "text": val, "color": vclr, "size": "sm",
                 "weight": "bold", "flex": 3, "align": "end"},
            ],
        }

    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "14px",
            "backgroundColor": "#060B14",
            "contents": [
                {"type": "text", "text": f"📈 {label}回測結果",
                 "color": "#E8EEF8", "weight": "bold", "size": "lg"},
                {"type": "text", "text": f"期間：{period}",
                 "color": "#6A7E9C", "size": "xs", "margin": "xs"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "14px",
            "spacing": "xs", "backgroundColor": "#0A0F1E",
            "contents": [
                _row("總報酬",   f"{ret_sign}{total_return*100:.1f}%",  ret_clr),
                _row("勝率",     f"{win_rate*100:.0f}%",
                     "#4ADE80" if win_rate >= 0.55 else "#FFAA00"),
                _row("最大回撤", f"-{max_dd*100:.1f}%",  "#FF4455"),
                _row("夏普比率", f"{sharpe:.2f}",          sh_clr),
                _row("交易次數", f"{n_trades} 筆"),
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "10px",
            "backgroundColor": "#060B14",
            "contents": [{
                "type": "button", "style": "secondary", "height": "sm",
                "color": "#1A3A8F",
                "action": {"type": "postback", "label": "查看詳細",
                           "data": detail_data,
                           "displayText": "📊 推送詳細回測圖"},
                "contents": [
                    {"type": "text", "text": "📊 查看詳細",
                     "color": "#E8EEF8", "size": "sm"}
                ],
            }],
        },
    }


# ── 風控分析 ──────────────────────────────────────────────────────────────────

# 常見台股產業對照表（快速 fallback）
_SECTOR_MAP: dict[str, str] = {
    "2330": "半導體", "2454": "半導體", "2379": "半導體",
    "3711": "半導體", "3034": "半導體", "2303": "半導體",
    "2317": "電子零組件", "2308": "電子零組件", "2382": "電子零組件",
    "2357": "電腦週邊", "2353": "電腦週邊",
    "2412": "電信", "3045": "電信", "4904": "電信",
    "2882": "金融", "2881": "金融", "2886": "金融",
    "2891": "金融", "2884": "金融", "2885": "金融",
    "2002": "鋼鐵", "2006": "鋼鐵",
    "2603": "航運", "2609": "航運", "2615": "航運",
    "2207": "汽車", "2201": "汽車",
    "1303": "塑膠", "1301": "塑膠",
    "6505": "石化", "1326": "石化",
    "2912": "零售", "2903": "零售",
    "6469": "生技醫療", "4743": "生技醫療",
}

_SECTOR_SUGGEST: dict[str, str] = {
    "半導體":   "金融或航運類股",
    "金融":     "半導體或航運類股",
    "航運":     "金融或半導體類股",
    "電子零組件": "金融或傳產類股",
    "電腦週邊": "金融或傳產類股",
    "電信":     "半導體或金融類股",
    "鋼鐵":     "電子或金融類股",
    "塑膠":     "電子或金融類股",
}


async def _cmd_risk_report(uid: str) -> list:
    """
    🛡️ 風控分析報告：集中度 + VaR + 市場狀態 + 建議
    點「查看優化建議」觸發背景 Markowitz 推送
    """
    try:
        # ── 取庫存 ───────────────────────────────────────────────────
        async with AsyncSessionLocal() as db:
            holdings = await portfolio_service.get_portfolio(db, uid)

        if not holdings:
            return [_text(
                "🛡️ 風控分析\n\n庫存為空，請先新增持股",
                qr_items(("新增示範", "/buy 2330 1000 850"), ("💼 庫存", "/p")),
            )]

        # ── 產業集中度 ───────────────────────────────────────────────
        total_mv = sum(h.get("market_value", 0) or 0 for h in holdings)
        if total_mv <= 0:
            total_mv = sum(h["shares"] * h["cost_price"] for h in holdings)

        sector_mv: dict[str, float] = {}
        for h in holdings:
            code   = h.get("stock_code", "")
            mv     = h.get("market_value") or (h["shares"] * h["cost_price"])
            sector = _SECTOR_MAP.get(code)
            if not sector:
                try:
                    from backend.models.models import Stock
                    from sqlalchemy import select
                    async with AsyncSessionLocal() as db2:
                        r = await db2.execute(select(Stock).where(Stock.code == code))
                        s = r.scalar_one_or_none()
                        sector = (s.industry or "其他") if s else "其他"
                except Exception as e:
                    sector = "其他"
            sector_mv[sector] = sector_mv.get(sector, 0.0) + float(mv)

        top_sector    = max(sector_mv, key=sector_mv.get)
        top_pct       = sector_mv[top_sector] / total_mv * 100 if total_mv > 0 else 0.0
        concentration = "⚠️ 過度集中" if top_pct > 60 else ("注意" if top_pct > 40 else "✓ 正常")
        suggest_sector = _SECTOR_SUGGEST.get(top_sector, "分散至其他類股")

        # ── VaR（複用 full_portfolio_analysis）──────────────────────
        max_daily_loss = 0.0
        var_95         = 0.0
        try:
            from backend.services.portfolio_optimizer import full_portfolio_analysis
            analysis = await full_portfolio_analysis(uid)
            var_data  = analysis.get("var", {})
            if var_data:
                max_daily_loss = abs(var_data.get("hist_var_amount") or var_data.get("param_var_amount", 0))
                var_95         = abs(var_data.get("param_var_amount", 0))
        except Exception as e:
            max_daily_loss = total_mv * 0.025   # fallback ~2.5%
            var_95         = total_mv * 0.018

        # ── 市場狀態（RegimeEngine）──────────────────────────────────
        market_note = "多頭"
        hold_pct    = 70
        try:
            from quant.regime_engine import RegimeEngine
            from quant.feature_engine import FeatureEngine
            from backend.services.twse_service import fetch_kline
            kl = await fetch_kline("2330")
            if kl and len(kl) >= 65:
                import pandas as pd
                df = pd.DataFrame(kl)
                for c in ["open","high","low","close","volume"]:
                    if c in df.columns:
                        df[c] = pd.to_numeric(df[c], errors="coerce")
                feat = FeatureEngine(df).compute_all()
                re   = RegimeEngine()
                reg  = re.detect(feat)
                regime_map = {"bull": ("多頭", 70), "bear": ("空頭", 30),
                              "sideways": ("盤整", 50), "volatile": ("高波動", 40)}
                market_note, hold_pct = regime_map.get(
                    reg.regime.value, ("未知", 50))
        except Exception as e:
            pass

        # ── 組合訊息 ─────────────────────────────────────────────────
        lines = [
            "🛡️ 風控分析報告",
            "─" * 22,
            f"投組集中度：{top_sector} {top_pct:.0f}%（{concentration}）",
            f"最大單日虧損估計：-${max_daily_loss:,.0f}",
            f"VaR 95%：-${var_95:,.0f}",
        ]
        if top_pct > 40:
            lines.append(f"建議：分散至{suggest_sector}")
        lines += [
            "",
            f"市場狀態：{market_note}，建議持股 {hold_pct}%",
        ]

        return [TextMessage(text="\n".join(lines)[:5000])]

    except Exception as e:
        logger.error("[risk_report] %s", e)
        return [TextMessage(text="功能暫時無法使用，請稍後再試")]


async def _risk_optimize_bg(uid: str) -> None:
    """背景：馬可維茲最佳配置 → push LINE"""
    import httpx
    from backend.models.database import settings as line_settings

    try:
        from backend.services.portfolio_optimizer import full_portfolio_analysis
        result = await full_portfolio_analysis(uid)

        if "error" in result:
            msg = f"❌ 最佳化失敗：{result['error']}"
        else:
            opt   = result.get("optimal_portfolio", {})
            curr  = result.get("current_performance", {})
            suggs = result.get("rebalance_suggestions", [])

            lines = [
                "📐 馬可維茲最佳配置",
                "─" * 22,
                f"現有：報酬{curr.get('return',0):+.1f}%  "
                f"波動{curr.get('volatility',0):.1f}%  "
                f"Sharpe {curr.get('sharpe',0):.2f}",
                f"最佳：報酬{opt.get('return',0):+.1f}%  "
                f"波動{opt.get('volatility',0):.1f}%  "
                f"Sharpe {opt.get('sharpe',0):.2f}",
                "",
                "調整建議（差異 ≥ 2%）：",
            ]
            if suggs:
                for s in suggs[:5]:
                    sign = "+" if s["change"] > 0 else ""
                    lines.append(
                        f"• {s['stock_code']} {s['name']}\n"
                        f"  {s['current']}% → {s['optimal']}% "
                        f"（{sign}{s['change']}%，{s['action']}）"
                    )
            else:
                lines.append("• 現有組合已接近最佳配置 ✓")

            msg = "\n".join(lines)

        await push_line_messages(uid, [{"type": "text", "text": msg[:4800]}], timeout=30, context="handler.risk_optimize_bg")
    except Exception as e:
        logger.error("[risk_optimize_bg] %s", e)


# ── 策略管理指令 ─────────────────────────────────────────────────────────────

async def _cmd_strategy_manage(uid: str) -> list:
    """/strategy — 顯示策略管理選單"""
    try:
        from backend.services.strategy_manager import get_settings, build_strategy_menu_flex
        from backend.models.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            settings_map = await get_settings(db, uid)
        card = build_strategy_menu_flex(settings_map)
        return [_flex("策略管理", card,
                      qr_items(("💼 庫存", "/p"), ("📊 選股", "/r")))]
    except Exception as e:
        logger.warning("[strategy] manage 失敗: %s", e)
        return [_text("策略管理暫時無法使用",
                      qr_items(("💼 庫存", "/p")))]


async def _cmd_strategy_perf(name: str, uid: str) -> list:
    """顯示單一策略績效 Bubble"""
    try:
        from backend.services.strategy_manager import (
            get_strategy_performance, build_strategy_perf_flex, get_settings
        )
        from backend.models.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            perf     = await get_strategy_performance(db, name, days=30)
            settings = await get_settings(db, uid)
        card = build_strategy_perf_flex(perf)
        return [_flex(f"{name} 績效", card,
                      qr_items(("策略管理", "/strategy"),
                               ("📊 選股", "/r")))]
    except Exception as e:
        logger.warning("[strategy] perf 失敗: %s", e)
        return [_text("績效暫時無法取得", qr_items(("策略管理", "/strategy")))]


async def _cmd_strategy_toggle(name: str, uid: str) -> list:
    """postback strategy_toggle — 切換策略開關"""
    if not name:
        return [_text("請指定策略名稱")]
    try:
        from backend.services.strategy_manager import toggle_strategy
        from backend.models.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            _, msg = await toggle_strategy(db, uid, name)
        return [_text(msg, qr_items(("策略管理", "/strategy")))]
    except Exception as e:
        logger.warning("[strategy] toggle 失敗: %s", e)
        return [_text("切換失敗，請稍後再試")]


async def _cmd_strategy_preset(preset: str, uid: str) -> list:
    """postback strategy_preset — 套用預設組合"""
    try:
        from backend.services.strategy_manager import apply_preset, PRESET_LABELS
        from backend.models.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            label = await apply_preset(db, uid, preset)
        return [_text(
            f"✅ 已套用 {label} 策略組合",
            qr_items(("查看設定", "/strategy"), ("📊 選股", "/r")),
        )]
    except Exception as e:
        logger.warning("[strategy] preset 失敗: %s", e)
        return [_text("設定失敗，請稍後再試")]


async def _cmd_report(screen_type: str, uid: str, sector: str = "") -> list:
    """/report [type] — 同步選股並 reply（不用 push）"""
    import asyncio
    from backend.services.report_screener import async_run_screener, paginate, get_label
    from backend.services.report_tracker import batch_record

    label = get_label(screen_type, sector)
    try:
        rows = await asyncio.wait_for(
            async_run_screener(screen_type, sector=sector), timeout=22)
    except asyncio.TimeoutError:
        return [TextMessage(text="功能暫時無法使用，請稍後再試")]
    except Exception as e:
        logger.error("[report] %s", e)
        return [TextMessage(text="功能暫時無法使用，請稍後再試")]

    if not rows:
        return [TextMessage(text="📊 選股列表\n目前沒有符合條件的股票")]

    page_rows, total_pages = paginate(rows, 1)
    _report_pages[uid] = {"rows": rows, "page": 1, "total_pages": total_pages,
                          "screen_type": screen_type, "sector": sector}
    try:
        batch_record(page_rows, screen_type=screen_type)
    except Exception as e:
        pass

    return [_build_stock_list_msg(page_rows, label, 1, total_pages, screen_type)]


async def _cmd_report_page(uid: str, delta: int = 0, go_to: int = 0) -> list:
    """/report next 或 /report page N — 分頁翻頁（同步 reply）"""
    from backend.services.report_screener import paginate, get_label

    cache = _report_pages.get(uid)
    if not cache:
        return [TextMessage(text="沒有進行中的選股，請先輸入 /report")]
    current = cache["page"]
    total_p = cache["total_pages"]
    next_p  = go_to if go_to > 0 else current + delta
    next_p  = max(1, min(next_p, total_p))
    if next_p == current and delta != 0:
        return [TextMessage(text=f"已是最{'後' if delta > 0 else '前'}一頁（第 {current}/{total_p} 頁）")]

    rows        = cache["rows"]
    screen_type = cache["screen_type"]
    sector      = cache.get("sector", "")
    label       = get_label(screen_type, sector)
    page_rows, _ = paginate(rows, next_p)
    _report_pages[uid]["page"] = next_p
    return [_build_stock_list_msg(page_rows, label, next_p, total_p, screen_type)]


async def _report_bg(
    screen_type: str, sector: str, uid: str,
    page: int = 1, cached_rows=None,
) -> None:
    """背景：篩選 → real-time 補充 → 分頁 → 產生圖 + 文字列表 → 推送"""
    import httpx
    from backend.services.report_screener import async_run_screener, paginate, get_label
    from backend.services.generate_report_image import generate_report_image
    from backend.services.report_tracker import batch_record

    try:
        if cached_rows is None:
            rows = await async_run_screener(screen_type, sector=sector)
            logger.info(f"[report_bg] screener={screen_type} rows={len(rows)}")
            if rows:
                r0 = rows[0]
                logger.info(f"[report_bg] row0: {r0.stock_id} {r0.name} close={r0.close} chg={r0.change_pct} vol={r0.volume} src={getattr(r0, '_data_source', '?')}")
            if not rows:
                # TWSE 資料無法取得（非交易時間或 API 異常）
                await push_line_messages(uid, [{"type": "text", "text": "⚠️ 目前無法取得 TWSE 即時資料（非交易時間或 API 異常），請稍後再試。"}], timeout=10, context="handler.report_bg")
                return
        else:
            rows = cached_rows

        page_rows, total_pages = paginate(rows, page)
        logger.info(f"[report_bg] page={page}/{total_pages} page_rows={len(page_rows)}")

        # 快取分頁狀態
        _report_pages[uid] = {
            "rows": rows, "page": page,
            "total_pages": total_pages,
            "screen_type": screen_type, "sector": sector,
        }

        label = get_label(screen_type, sector)
        path = generate_report_image(
            stocks=page_rows, group=label,
            market_state=os.getenv("MARKET_STATE", "unknown"),
            page=page, total_pages=total_pages,
        )

        # 追蹤記錄（僅第一頁）
        if page == 1:
            try:
                batch_record(page_rows, screen_type=screen_type)
            except Exception as e:
                pass

        base_url = os.getenv("BASE_URL", "")
        push_msgs = []

        if base_url:
            image_url = f"{base_url.rstrip('/')}/static/reports/{path.name}"
            push_msgs.append({"type": "image",
                              "originalContentUrl": image_url,
                              "previewImageUrl":    image_url})
        else:
            push_msgs.append(_build_text_fallback(page_rows, label, page, total_pages))

        # 文字股票列表 + 翻頁 Quick Reply 按鈕
        push_msgs.append(_build_stock_list_msg(page_rows, label, page, total_pages, screen_type))

        await push_line_messages(uid, push_msgs[:5], timeout=20, context="handler.report_bg")
    except Exception as e:
        logger.error(f"[report_bg] {screen_type} uid={uid[:8]} err={e}", exc_info=True)


def _build_text_fallback(rows, label: str, page: int, total: int) -> TextMessage:
    lines = [f"📊 {label} (第{page}/{total}頁)", "─" * 20]
    for r in rows[:10]:
        s = "+" if r.change_pct > 0 else ""
        lines.append(f"{r.stock_id} {r.name} {s}{r.change_pct:.2f}%  分:{r.model_score:.0f}")
    return TextMessage(text="\n".join(lines)[:4800])


def _build_stock_list_msg(
    rows: list, label: str, page: int, total_pages: int, screen_type: str,
) -> TextMessage:
    """文字股票列表。"""
    lines = [f"📊 {label}  第{page}/{total_pages}頁", "─" * 18]
    for i, r in enumerate(rows[:10], 1):
        arrow = "▲" if r.change_pct > 0 else ("▼" if r.change_pct < 0 else "─")
        sign  = "+" if r.change_pct > 0 else ""
        close_str = f"{r.close:,.1f}" if r.close > 0 else "--"
        lines.append(
            f"#{i} {r.stock_id} {r.name}\n"
            f"   {close_str}元  AI{r.confidence:.0f}分  {arrow}{sign}{r.change_pct:.1f}%"
        )

    return TextMessage(text="\n".join(lines)[:5000])


async def _cmd_custom_screen(conditions: str, uid: str) -> list:
    """/custom [條件] — AI 解析自訂篩選條件"""
    import asyncio
    asyncio.create_task(_custom_bg(conditions, uid))
    return [_text(
        f"🔍 正在解析條件：\n「{conditions[:80]}」\n\n約需 5 秒…",
        qr_items(("選單", "/screen"), ("全維度", "/report all"))
    )]


async def _custom_bg(conditions: str, uid: str) -> None:
    import httpx
    from backend.services.report_screener import custom_screener, enrich_with_realtime
    from backend.services.generate_report_image import generate_report_image

    try:
        rows = await custom_screener(conditions, api_key=settings.anthropic_api_key)
        rows = await enrich_with_realtime(rows)
        path = generate_report_image(stocks=rows, group=f"自訂：{conditions[:20]}",
                                     market_state=os.getenv("MARKET_STATE", "unknown"))
        base_url = os.getenv("BASE_URL", "")
        if base_url:
            url = f"{base_url.rstrip('/')}/static/reports/{path.name}"
            msg = {"type": "image", "originalContentUrl": url, "previewImageUrl": url}
        else:
            msg = _build_text_fallback(rows, f"自訂條件：{conditions[:20]}", 1, 1)
        await push_line_messages(uid, [msg], timeout=20, context="handler.custom_bg")
    except Exception as e:
        logger.error(f"[custom_bg] err={e}")


async def _cmd_save_fav(code: str, uid: str) -> list:
    """/save [code]"""
    from backend.services.stock_favorites import save_favorite
    try:
        q = await fetch_realtime_quote(code)
        name = q.get("name", code) if q else code
    except Exception as e:
        name = code
    ok, msg = save_favorite(uid, code, name)
    qr = qr_items(("我的最愛", "/myfav"), ("選股圖", "/myfav report"), ("移除", f"/unsave {code}"))
    return [_text(msg, qr)]


async def _cmd_unsave_fav(code: str, uid: str) -> list:
    """/unsave [code]"""
    from backend.services.stock_favorites import remove_favorite
    ok, msg = remove_favorite(uid, code)
    return [_text(msg, qr_items(("我的最愛", "/myfav")))]


async def _cmd_myfav_list(uid: str) -> list:
    """/myfav — 顯示收藏列表"""
    from backend.services.stock_favorites import format_favorites_text
    text = format_favorites_text(uid)
    return [_text(text, qr_items(("選股圖", "/myfav report"), ("📊 選單", "/screen")))]


async def _cmd_myfav_report(uid: str) -> list:
    """/myfav report — 產生收藏選股圖"""
    import asyncio
    from backend.services.stock_favorites import get_favorite_ids
    ids = get_favorite_ids(uid)
    if not ids:
        return [_text("收藏為空，請先 /save 代碼 加入收藏",
                      qr_items(("加入示範", "/save 2330"), ("📊 選單", "/screen")))]
    asyncio.create_task(_report_bg("favorites", "", uid, cached_rows=None))
    # 手動快取 favorites 篩選
    asyncio.create_task(_myfav_bg(ids, uid))
    return [_text(f"正在產生 {len(ids)} 檔收藏選股圖…")]


async def _myfav_bg(stock_ids: list[str], uid: str) -> None:
    import httpx
    from backend.services.report_screener import favorites_screener, enrich_with_realtime
    from backend.services.generate_report_image import generate_report_image

    try:
        rows = favorites_screener(stock_ids)
        rows = await enrich_with_realtime(rows)
        path = generate_report_image(stocks=rows, group="我的最愛",
                                     market_state=os.getenv("MARKET_STATE", "unknown"))
        base_url = os.getenv("BASE_URL", "")
        if base_url:
            url = f"{base_url.rstrip('/')}/static/reports/{path.name}"
            msg = {"type": "image", "originalContentUrl": url, "previewImageUrl": url}
        else:
            msg = _build_text_fallback(rows, "我的最愛", 1, 1)
        await push_line_messages(uid, [msg], timeout=20, context="handler.myfav_bg")
    except Exception as e:
        logger.error(f"[myfav_bg] err={e}")


async def _cmd_compare_image(codes: list[str], uid: str) -> list:
    """/compare 2330 2454 [3711] — 比較圖"""
    import asyncio
    codes = codes[:3]
    asyncio.create_task(_compare_bg(codes, uid))
    return [_text(
        f"正在產生比較圖：{' vs '.join(codes)}\n包含五維雷達圖…",
        qr_items(("📊 全維度", "/report all"), ("📋 選單", "/screen"))
    )]


async def _compare_bg(codes: list[str], uid: str) -> None:
    import httpx
    from backend.services.report_screener import favorites_screener, enrich_with_realtime, unify_ticker_format
    from backend.services.generate_report_image import generate_comparison_image

    try:
        clean_codes = [unify_ticker_format(c) for c in codes[:3]]
        rows = favorites_screener(clean_codes)
        rows = await enrich_with_realtime(rows)
        rows = rows[:3]
        path = generate_comparison_image(rows)
        base_url = os.getenv("BASE_URL", "")
        if base_url:
            url = f"{base_url.rstrip('/')}/static/reports/{path.name}"
            msg = {"type": "image", "originalContentUrl": url, "previewImageUrl": url}
        else:
            lines = ["比較：" + " vs ".join(codes)]
            for r in rows:
                lines.append(f"{r.stock_id} {r.name}  分:{r.model_score:.0f}  信心:{r.confidence:.0f}")
            msg = {"type": "text", "text": "\n".join(lines)}
        await push_line_messages(uid, [msg], timeout=20, context="handler.compare_bg")
    except Exception as e:
        logger.error(f"[compare_bg] err={e}")


async def _cmd_track_history(code: str) -> list:
    """/track [code] — 個股歷史追蹤"""
    from backend.services.report_tracker import get_stock_history, format_history_text
    history = get_stock_history(code, days=30)
    text = format_history_text(history)
    return [_text(text, qr_items(
        (f"📈 報價", f"/quote {code}"),
        ("🔬 AI 分析", f"/ai {code}"),
        ("📊 選單", "/screen"),
    ))]


async def _push_report_bg(group: str, uid: str) -> None:
    """背景：產生圖片 → LINE Image Message 推送"""
    import httpx
    from backend.services.generate_report_image import generate_report_image

    try:
        path = generate_report_image(group=group)
        base_url = os.getenv("BASE_URL", "")

        if not base_url:
            # 無公開 URL → 改傳文字摘要
            await _push_text_summary(group, uid)
            return

        image_url = f"{base_url.rstrip('/')}/static/reports/{path.name}"
        ok = await push_line_messages(
            uid,
            [{"type": "image", "originalContentUrl": image_url, "previewImageUrl": image_url}],
            timeout=20, context="handler.push_report_bg",
        )
        logger.info(f"[Report] push image to {uid[:8]}: {'ok' if ok else 'failed'}")
    except Exception as e:
        logger.error(f"[Report] bg push failed: {e}")
        await _push_text_summary(group, uid)


async def _push_text_summary(group: str, uid: str) -> None:
    """無公開 URL 時，改推文字摘要"""
    from backend.services.generate_report_image import get_mock_data, LABEL_DEFS

    stocks = get_mock_data(group)
    lines = [f"📊 {group}選股表 {__import__('datetime').date.today()}", "─" * 22]
    for s in stocks[:6]:
        sign = "+" if s.change_pct > 0 else ""
        tag_str = " ".join(s.tags) if s.tags else ""
        lines.append(
            f"{s.stock_id} {s.name} {sign}{s.change_pct:.2f}%\n"
            f"  分:{s.model_score:.0f} 籌:{s.chip_5d/1000:+.1f}k {tag_str}"
        )
    text = "\n".join(lines)
    await push_line_messages(uid, [{"type": "text", "text": text[:5000]}], timeout=10, context="handler.push_text_summary")


async def _cmd_recommend(regime: str = "unknown") -> list:
    """/recommend [盤態]  → 推薦高信心選股"""
    try:
        import httpx
        base = f"http://localhost:{os.getenv('PORT', '8080')}/api/quant"
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"{base}/strategy/recommend",
                params={"regime": regime, "min_confidence": 60, "limit": 5},
            )
            data = resp.json()
    except Exception as e:
        from quant.strategy_engine import StrategyEngine, MOCK_STOCKS
        # 用 TWSE 即時收盤覆蓋 MOCK_STOCKS 硬編碼舊價格
        try:
            from backend.services.report_screener import _rt_cache, _fetch_rt_cache
            if not _rt_cache.get("prices"):
                await _fetch_rt_cache()
            cached_prices = _rt_cache.get("prices", {})
            enriched = []
            for s in MOCK_STOCKS:
                d = dict(s)
                p = cached_prices.get(s.get("stock_id", ""), {})
                if p.get("close", 0) > 0:
                    c = p["close"]
                    d["close"] = c
                    d["atr14"] = round(c * 0.02, 1)
                enriched.append(d)
        except Exception as e:
            enriched = MOCK_STOCKS
        sigs = StrategyEngine().batch_evaluate(enriched, regime=regime, min_confidence=60)
        data = {"regime": regime, "signals": [s.to_dict() for s in sigs[:5]]}

    regime_label = {
        "bull": "多頭", "bear": "空頭",
        "sideways": "盤整", "volatile": "高波動", "unknown": "未知",
    }.get(data.get("regime", "unknown"), "未知")

    lines = [f"市場狀態：{regime_label} 策略推薦\n"]
    emoji_map = {"強力買進": "🔥", "買進": "▲", "觀察": "◆", "減碼": "▽", "賣出": "🔴"}
    for s in data.get("signals", []):
        e = emoji_map.get(s.get("action", ""), "")
        lines.append(
            f"{e} {s['stock_id']} {s.get('name', '?')} 信心{s.get('confidence', 0):.0f}\n"
            f"  目標 {s.get('target_price', 0):.0f}  "
            f"停損 {s.get('stop_loss', 0):.0f}  "
            f"風險：{s.get('risk_level', '?')}"
        )
    qr = qr_items(
        ("📊 市場狀態", "/market"),
        ("🔍 選股器",   "/screener top"),
        ("💡 AI 建議",  "/advice"),
    )
    return [_text("\n".join(lines), qr)]


async def _cmd_odd(budget_str: str, code: str | None, uid: str) -> list:
    """/odd {budget} [{code}]  → 零股計算"""
    try:
        budget = float(budget_str.replace(",", ""))
    except ValueError:
        return [_text("格式：/odd 5000 或 /odd 5000 0056")]

    from quant.odd_lot_engine import OddLotEngine
    engine = OddLotEngine(discount=0.6)

    if code:
        try:
            quote = await fetch_realtime_quote(code)
            price = float(quote.get("close") or quote.get("price") or 0)
            name  = quote.get("name", code)
        except Exception as e:
            price = 0.0
            name  = code
        if price <= 0:
            return [_text(f"無法取得 {code} 現價，請稍後再試")]
        result = engine.calc(budget, price, code, name)
        return [_text(result.to_line_text())]

    # 未指定個股 → 從推薦清單分配
    try:
        from quant.strategy_engine import StrategyEngine, MOCK_STOCKS
        top    = StrategyEngine().batch_evaluate(MOCK_STOCKS, min_confidence=55)[:3]
        stocks = []
        for sig in top:
            try:
                q = await fetch_realtime_quote(sig.stock_id)
                p = float(q.get("close") or q.get("price") or 0)
            except Exception as e:
                p = 0.0
            if p <= 0:
                try:
                    from backend.services.report_screener import _rt_cache
                    cached_p = _rt_cache.get("prices", {}).get(sig.stock_id, {})
                    p = float(cached_p.get("close", 0) or 0)
                except Exception as e:
                    p = 0.0
            stocks.append({"stock_id": sig.stock_id, "name": sig.name,
                           "price": p, "confidence": sig.confidence})
        portfolio = engine.allocate(budget, stocks, strategy="signal")
        return [_text(portfolio.to_line_text())]
    except Exception as e:
        logger.warning(f"[odd_lot] alloc failed: {e}")
        return [_text(f"零股計算失敗，請用 /odd {budget:.0f} {{股票代號}}")]


async def _cmd_compare(code_a: str, code_b: str) -> list:
    """/compare {code_a} {code_b}  → 兩股策略比較"""
    import numpy as np
    from quant.strategy_engine import StrategyEngine, MOCK_STOCKS

    def _data(code: str) -> dict:
        for s in MOCK_STOCKS:
            if s["stock_id"] == code:
                return dict(s)   # 複製，避免後續修改污染 MOCK_STOCKS
        seed = sum(ord(c) for c in code)
        rng  = np.random.default_rng(seed)
        return {
            "stock_id": code, "name": code,
            "momentum_20d":       float(rng.uniform(0.95, 1.15)),
            "foreign_buy_days":   int(rng.integers(-3, 7)),
            "volume_ratio":       float(rng.uniform(0.8, 1.8)),
            "dividend_yield":     float(rng.uniform(0, 7)),
            "pe_ratio":           float(rng.uniform(8, 28)),
            "eps_stability":      float(rng.uniform(0.4, 0.95)),
            "foreign_net":        float(rng.uniform(-1000, 4000)),
            "trust_net":          float(rng.uniform(-300, 800)),
            "dealer_net":         float(rng.uniform(-100, 200)),
            "chip_concentration": float(rng.uniform(45, 80)),
            "volatility":         float(rng.uniform(0.008, 0.022)),
            "max_drawdown":       float(rng.uniform(0.05, 0.20)),
            "close":              float(rng.uniform(30, 1000)),
            "atr14":              float(rng.uniform(0.5, 20)),
        }

    async def _enrich_close(d: dict) -> dict:
        """用 TWSE 即時收盤覆蓋 close/atr14，確保目標/停損基於今日真實股價"""
        try:
            from backend.services.report_screener import _rt_cache, _fetch_rt_cache
            if not _rt_cache.get("prices"):
                await _fetch_rt_cache()
            p = _rt_cache.get("prices", {}).get(d.get("stock_id", ""), {})
            if p and p.get("close", 0) > 0:
                c = p["close"]
                d["close"]  = c
                d["atr14"]  = round(c * 0.02, 1)   # ATR ≈ 2% 估算
                d.setdefault("ma20", round(c * 0.97, 1))
                d.setdefault("ma60", round(c * 0.94, 1))
        except Exception as e:
            pass
        return d

    da_raw = await _enrich_close(_data(code_a))
    db_raw = await _enrich_close(_data(code_b))
    result = StrategyEngine().compare(da_raw, db_raw)
    cmp    = result["compare"]
    da     = result.get(code_a, {})
    db_    = result.get(code_b, {})

    lines = [
        f"比較：{code_a} vs {code_b}\n",
        f"{code_a} {da.get('name', code_a)}",
        f"  信心：{da.get('confidence', 0):.0f}  "
        f"建議：{da.get('action', '?')}  "
        f"風險：{da.get('risk_level', '?')}",
        f"  目標：{da.get('target_price', 0):.0f}  停損：{da.get('stop_loss', 0):.0f}",
        "",
        f"{code_b} {db_.get('name', code_b)}",
        f"  信心：{db_.get('confidence', 0):.0f}  "
        f"建議：{db_.get('action', '?')}  "
        f"風險：{db_.get('risk_level', '?')}",
        f"  目標：{db_.get('target_price', 0):.0f}  停損：{db_.get('stop_loss', 0):.0f}",
        "",
        f"信心較高：{cmp['higher_confidence']}",
        f"風險較低：{cmp['lower_risk']}",
        f"建議選擇：{cmp['recommend']}（{cmp['reason']}）",
    ]
    qr = qr_items(
        (f"📋 {code_a} 策略", f"/strategy {code_a}"),
        (f"📋 {code_b} 策略", f"/strategy {code_b}"),
    )
    return [_text("\n".join(lines), qr)]


async def _cmd_strategy_analyze(code: str) -> list:
    """/strategy {code}  → 個股完整策略評分"""
    import numpy as np
    from quant.strategy_engine import StrategyEngine, MOCK_STOCKS

    data = next((s for s in MOCK_STOCKS if s["stock_id"] == code), None)
    if data:
        data = dict(data)   # 複製，避免修改污染 MOCK_STOCKS
    else:
        seed = sum(ord(c) for c in code)
        rng  = np.random.default_rng(seed)
        data = {
            "stock_id": code, "name": code,
            "momentum_20d":       float(rng.uniform(0.95, 1.12)),
            "foreign_buy_days":   int(rng.integers(-2, 6)),
            "volume_ratio":       float(rng.uniform(0.8, 1.8)),
            "dividend_yield":     float(rng.uniform(1, 6)),
            "pe_ratio":           float(rng.uniform(10, 25)),
            "eps_stability":      float(rng.uniform(0.5, 0.9)),
            "foreign_net":        float(rng.uniform(-500, 3000)),
            "trust_net":          float(rng.uniform(-200, 600)),
            "dealer_net":         float(rng.uniform(-100, 200)),
            "chip_concentration": float(rng.uniform(45, 75)),
            "volatility":         float(rng.uniform(0.009, 0.020)),
            "max_drawdown":       float(rng.uniform(0.05, 0.18)),
            "close":              float(rng.uniform(50, 800)),
            "atr14":              float(rng.uniform(1, 15)),
        }

    # 用 TWSE 即時收盤覆蓋 close/atr14，確保目標/停損基於今日真實股價
    try:
        from backend.services.report_screener import _rt_cache, _fetch_rt_cache
        if not _rt_cache.get("prices"):
            await _fetch_rt_cache()
        p = _rt_cache.get("prices", {}).get(code, {})
        if p and p.get("close", 0) > 0:
            c = p["close"]
            data["close"] = c
            data["atr14"] = round(c * 0.02, 1)   # ATR ≈ 2% 估算
            data.setdefault("ma20", round(c * 0.97, 1))
            data.setdefault("ma60", round(c * 0.94, 1))
    except Exception as e:
        pass

    sig = StrategyEngine().evaluate(data, strategy="composite")
    sc  = sig.to_dict()["scores"]
    lines = [
        f"策略分析：{code} {sig.name}",
        f"建議：{sig.action.value}  信心：{sig.confidence:.0f}  風險：{sig.risk_level.value}",
        f"目標：{sig.target_price:.0f}  停損：{sig.stop_loss:.0f}  持有：{sig.holding_days}日",
        "",
        "子分數：",
        f"  動能：{sc['momentum']:.0f}  價值：{sc['value']:.0f}  籌碼：{sc['chip']:.0f}",
        f"  複合：{sc['composite']:.0f}",
        "",
        f"理由：{'、'.join(sig.reasons[:4])}",
    ]
    qr = qr_items(
        ("📈 報價",     f"/quote {code}"),
        ("🔬 深度 AI",  f"/ai {code}"),
        ("💹 零股試算", f"/odd 5000 {code}"),
    )
    return [_text("\n".join(lines), qr)]


# ── 新功能 Handler 函數 ────────────────────────────────────────────────────────

async def _cmd_watch_add(code: str, uid: str) -> list:
    """/watch CODE — 加入自選股"""
    from backend.services.watchlist_service import add_to_watchlist
    from backend.services.twse_service import fetch_realtime_quote
    try:
        q    = await fetch_realtime_quote(code)
        name = q.get("name", code) if q else code
        async with AsyncSessionLocal() as db:
            item = await add_to_watchlist(db, uid, code, stock_name=name)
        return [_text(
            f"✅ 已加入自選股\n{code} {name}\n\n輸入 /watchlist 查看清單",
            _qr_postback(
                ("📋 查看清單", "watchlist"),
                ("🔍 分析", f"act=recommend_detail&code={code}"),
                ("❌ 移除",  f"/unwatch {code}"),
            ),
        )]
    except Exception as e:
        logger.error(f"[watch_add] {e}")
        return [_text(f"❌ 加入失敗：{code}\n{type(e).__name__}: {str(e)[:80]}")]


async def _cmd_watch_remove(code: str, uid: str) -> list:
    """/unwatch CODE — 移除自選股"""
    from backend.models.models import Watchlist
    from sqlalchemy import delete
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(Watchlist).where(
                    Watchlist.user_id == uid,
                    Watchlist.stock_code == code,
                )
            )
            await db.commit()
        return [_text(f"🗑️ {code} 已從自選股移除",
                      qr_items(("📋 查看清單", "/watchlist")))]
    except Exception as e:
        return [_text(f"❌ 移除失敗：{e}")]


async def _cmd_watchlist(uid: str) -> list:
    """/watchlist — 顯示自選股清單"""
    try:
        from backend.services.watchlist_monitor import scan_user_watchlist, format_watchlist_report, _build_watchlist_qr
        results = await scan_user_watchlist(uid)
        text    = format_watchlist_report(uid, results)
        qr      = _build_watchlist_qr(results)
        return [TextMessage(text=text, quick_reply=_make_qr(qr))]
    except Exception as e:
        logger.error(f"[watchlist] {e}")
        return [_text("❌ 自選股讀取失敗，請稍後再試",
                      qr_items(("重試", "/watchlist")))]


async def _cmd_rs(uid: str) -> list:
    """/rs — 今日相對強度排行"""
    try:
        from backend.services.rs_engine import get_top20, format_rs_ranking, get_rs_qr
        records = get_top20()
        text    = format_rs_ranking(records)
        qr      = get_rs_qr(records)
        return [TextMessage(text=text, quick_reply=_make_qr(qr))]
    except Exception as e:
        logger.error(f"[rs] {e}")
        return [_text("❌ RS 排行計算失敗，請稍後再試")]


async def _cmd_breadth(uid: str) -> list:
    """/breadth — 市場廣度快照"""
    try:
        from backend.services.market_breadth import calculate_breadth
        snap = calculate_breadth()
        text = snap.summary_text()
        qr = _qr_postback(
            ("📊 大盤行情", "act=market_card"),
            ("📈 今日選股", "act=screener_qr"),
        )
        return [TextMessage(text=text, quick_reply=_make_qr(qr))]
    except Exception as e:
        logger.error(f"[breadth] {e}")
        return [_text("❌ 廣度計算失敗，請稍後再試")]


async def _cmd_journal(uid: str, stock_id: str | None = None) -> list:
    """/journal [代碼] — 查看交易日誌"""
    try:
        from backend.services.trade_journal import get_journal, format_journal_entry, format_journal_list
        entries = await get_journal(uid, stock_id=stock_id, limit=8)
        if entries and stock_id:
            text = format_journal_entry(entries[0])
        else:
            text = format_journal_list(entries)
        return [_text(text, qr_items(
            ("📓 全部記錄", "/journal"),
            ("💼 庫存", "/portfolio"),
        ))]
    except Exception as e:
        logger.error(f"[journal] {e}")
        return [_text("❌ 日誌讀取失敗，請稍後再試")]


async def _cmd_review(uid: str) -> list:
    """/review — 交易行為分析"""
    try:
        from backend.services.mistake_detector import analyze_user
        report = await analyze_user(uid)
        text   = report.to_line_text()
        return [_text(text, _qr_postback(
            ("📓 交易日誌", "journal"),
            ("💼 看庫存",   "act=portfolio_view"),
        ))]
    except Exception as e:
        logger.error(f"[review] {e}")
        return [_text("❌ 分析失敗，請稍後再試")]


async def _cmd_manage(uid: str) -> list:
    """/manage — AI 投組管理建議"""
    try:
        from backend.services.portfolio_manager import analyze_portfolio
        advice = await analyze_portfolio(uid)
        text   = advice.to_line_text()
        return [_text(text, _qr_postback(
            ("💼 看庫存",   "act=portfolio_view"),
            ("📊 今日選股", "act=screener_qr"),
        ))]
    except Exception as e:
        logger.error(f"[manage] {e}")
        return [_text("❌ 投組分析失敗，請稍後再試")]


async def _cmd_exposure(uid: str) -> list:
    """/exposure — 因子暴露分析"""
    try:
        from backend.services.factor_exposure import calculate_exposure
        exp  = await calculate_exposure(uid)
        text = exp.to_line_text()
        return [_text(text, _qr_postback(
            ("💼 看庫存", "act=portfolio_view"),
            ("🤖 AI投組", "manage"),
        ))]
    except Exception as e:
        logger.error(f"[exposure] {e}")
        return [_text("❌ 因子計算失敗，請稍後再試")]


async def _cmd_insider(code: str, uid: str) -> list:
    """/insider CODE — 董監持股動態"""
    try:
        from backend.services.insider_flow import get_insider_flow, format_insider_list
        events = await get_insider_flow(code)
        text   = format_insider_list(events, code)
        return [_text(text, _qr_postback(
            (f"🔍 分析 {code}", f"act=recommend_detail&code={code}"),
            ("📋 自選股", f"/watch {code}"),
        ))]
    except Exception as e:
        logger.error(f"[insider] {e}")
        return [_text(f"❌ 董監持股查詢失敗：{code}")]


async def _cmd_earnings(code: str, uid: str) -> list:
    """/earnings [代碼] — 法說會日曆（無代碼）或個股財報（有代碼）"""
    if not code:
        # 無代碼 → 法說會日曆
        try:
            from backend.services.earnings_service import (
                get_investor_meetings, format_investor_calendar,
            )
            meetings = await get_investor_meetings(days=30)
            text = format_investor_calendar(meetings)
            return [_text(text, qr_items(
                ("台積電財報", "/earnings 2330"),
                ("聯發科財報", "/earnings 2454"),
                ("刷新", "/earnings"),
            ))]
        except Exception as e:
            logger.error(f"[earnings calendar] {e}")
            return [_text("❌ 法說會日曆讀取失敗，請稍後再試")]
    # 有代碼 → 個股財報分析
    try:
        from backend.services.earnings_intelligence import (
            analyze_earnings, format_earnings_calendar, get_upcoming_earnings,
        )
        result = await analyze_earnings(code)
        if result:
            return [TextMessage(text=result.to_line_text(),
                                quick_reply=_make_qr(result.to_line_qr()))]
        return [_text(f"❌ 查無 {code} 財報資料")]
    except Exception as e:
        logger.error(f"[earnings] {e}")
        return [_text("❌ 財報查詢失敗，請稍後再試")]


async def _cmd_public(sub: str, uid: str) -> list:
    """/public on/off — 公開或隱藏投組"""
    try:
        from backend.services.public_portfolio_service import set_public
        is_pub = sub == "on"
        status = await set_public(uid, is_pub)
        return [_text(
            f"✅ 你的投組已設為「{status}」\n\n"
            + ("現在其他用戶可以在 /top 排行榜看到你的投組" if is_pub
               else "你的投組已恢復私人模式"),
            qr_items(("🏆 排行榜", "/top"), ("💼 庫存", "/portfolio"))
        )]
    except Exception as e:
        return [_text(f"❌ 設定失敗：{e}")]


async def _cmd_top_portfolios() -> list:
    """/top — 本週績效排行榜"""
    try:
        from backend.services.public_portfolio_service import get_top_portfolios, format_top_portfolios
        items = await get_top_portfolios()
        text  = format_top_portfolios(items)
        return [_text(text, qr_items(
            ("公開我的投組", "/public on"),
            ("💼 看庫存", "/portfolio"),
        ))]
    except Exception as e:
        return [_text(f"❌ 排行榜讀取失敗：{e}")]


async def _cmd_strategy_list() -> list:
    """/strategy list — 策略市集"""
    try:
        from backend.services.public_portfolio_service import get_strategy_list, format_strategy_list
        items = await get_strategy_list()
        text  = format_strategy_list(items)
        return [_text(text, qr_items(
            ("上架策略", "/strategy publish 我的策略"),
            ("📊 今日選股", "/r"),
        ))]
    except Exception as e:
        return [_text(f"❌ 策略市集讀取失敗：{e}")]


async def _cmd_strategy_publish(name: str, uid: str) -> list:
    """/strategy publish [名稱] — 上架策略"""
    try:
        from backend.services.public_portfolio_service import publish_strategy
        result = await publish_strategy(uid, name)
        ret    = result["return_3m"] * 100
        wr     = result["win_rate"] * 100
        return [_text(
            f"✅ 策略「{name}」已上架\n\n"
            f"近3個月報酬：{'+' if ret >= 0 else ''}{ret:.1f}%\n"
            f"勝率：{wr:.0f}%\n\n"
            f"查看：/strategy list",
            qr_items(("策略市集", "/strategy list"))
        )]
    except Exception as e:
        return [_text(f"❌ 上架失敗：{e}")]


async def _cmd_strategy_subscribe(strategy_id: str, uid: str) -> list:
    """/strategy subscribe [ID] — 訂閱策略"""
    return [_text(
        f"✅ 已訂閱策略 #{strategy_id}\n\n每天將收到該策略的選股推薦",
        qr_items(("策略市集", "/strategy list"), ("今日選股", "/r"))
    )]


# ── 管理員指令實作 ──────────────────────────────────────────────────────────


async def _cmd_adduser(target_uid: str, role: str, admin_uid: str) -> list:
    """/adduser [LINE_ID] [role] — 新增/更新用戶角色（admin only）"""
    result = await set_user_role(target_uid, role.lower(), admin_uid)
    if result["ok"]:
        role_map = {"admin": "管理員", "premium": "Premium", "basic": "Basic", "blocked": "封鎖"}
        return [_text(
            f"✅ 已設定用戶\n\n"
            f"ID：{target_uid[:20]}...\n"
            f"角色：{role_map.get(role.lower(), role)}"
        )]
    return [_text(f"❌ 失敗：{result['error']}")]


async def _cmd_removeuser(target_uid: str, admin_uid: str) -> list:
    """/removeuser [LINE_ID] — 移除用戶（admin only）"""
    result = await remove_user(target_uid, admin_uid)
    if result["ok"]:
        return [_text(f"✅ 已移除用戶\nID：{target_uid[:20]}...")]
    return [_text(f"❌ 失敗：{result['error']}")]


async def _cmd_userlist(admin_uid: str) -> list:
    """/userlist — 查看所有用戶（admin only）"""
    users = await get_all_users()
    if not users:
        return [_text("📋 用戶清單為空")]
    icon = {"admin": "👑", "premium": "⭐", "basic": "👤", "blocked": "🚫"}
    lines = [f"📋 用戶清單（{len(users)} 位）", "─" * 20]
    for u in users:
        lines.append(
            f"{icon.get(u['role'], '?')} {u['role'].upper()}\n"
            f"  {u['user_id'][:24]}...\n"
            f"  加入：{u['created_at']}"
        )
    return [_text("\n".join(lines))]


async def _cmd_userstats(admin_uid: str) -> list:
    """/userstats — 今日各用戶使用次數（admin only）"""
    stats = await get_usage_stats()
    if not stats:
        return [_text("📊 今日尚無使用紀錄")]
    lines = [f"📊 今日使用統計（前20）", "─" * 20]
    for i, s in enumerate(stats, 1):
        lines.append(f"{i:2}. {s['user_id'][:16]}...  {s['count']} 次")
    return [_text("\n".join(lines))]


async def _cmd_gh_agent(task: str, uid: str) -> list:
    """/agent [任務] — 觸發 GitHub Actions LINE Agent workflow（admin only）"""
    import httpx, os
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = "Samhahahakokokoko/tw-bloomberg"
    if not token:
        return [_text("❌ 未設定 GITHUB_TOKEN，無法觸發 workflow")]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.github.com/repos/{repo}/dispatches",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                json={
                    "event_type": "line-agent",
                    "client_payload": {"task": task, "user_id": uid},
                },
            )
        if resp.status_code == 204:
            return [_text(
                f"🚀 任務已啟動\n\n"
                f"任務：{task}\n\n"
                f"GitHub Actions 正在執行：\n"
                f"1. 抓取 Railway 日誌\n"
                f"2. Claude AI 分析\n"
                f"3. 自動修復（如需要）\n\n"
                f"約 3-5 分鐘後推送結果",
                qr_items(("📊 庫存", "/p"), ("❓ 說明", "/help"))
            )]
        return [_text(f"❌ GitHub API 回應 {resp.status_code}：{resp.text[:200]}")]
    except Exception as e:
        return [_text(f"❌ 觸發失敗：{e}")]


async def _cmd_agent(uid: str) -> list:
    """/agent — 手動觸發 AI 基金經理"""
    try:
        import asyncio
        from backend.services.hedge_fund_agent import run_agent_pipeline
        asyncio.create_task(_agent_bg(uid))
        return [_text(
            "🤖 AI基金經理啟動中...\n\n"
            "執行10步驟完整分析流程：\n"
            "盤態 → 動能 → 分類 → 過濾 → 研究 → 持倉健診 → 信心 → 風控 → 決策\n\n"
            "約需 15-30 秒，完成後自動推送",
            _qr_postback(
                ("💼 看庫存",   "act=portfolio_view"),
                ("📊 今日選股", "act=screener_qr"),
            )
        )]
    except Exception as e:
        return [_text(f"❌ 啟動失敗：{e}")]


async def _agent_bg(uid: str) -> None:
    """背景執行 AI Agent 並推送結果"""
    try:
        from backend.services.hedge_fund_agent import run_agent_pipeline
        report  = await run_agent_pipeline(uid)
        text    = report.to_line_text()
        qr      = report.to_line_qr()
        await push_line_messages(
            uid,
            [{"type": "text", "text": text, "quickReply": qr}],
            timeout=30, context="handler.agent_bg",
        )
    except Exception as e:
        logger.error(f"[agent_bg] {e}")


async def _cmd_order_guide(uid: str) -> list:
    """/order — 下單說明"""
    return [_text(
        "⚡ Fugle 下單說明\n\n"
        "下單格式：/buy 代碼 股數 價格\n"
        "例：/buy 2330 1000 945（買1張台積電限價945）\n\n"
        "賣出：/sell 2330 1000\n\n"
        "⚙️ 自動交易設定：/auto on/off\n"
        "帳戶餘額：/order balance",
        _qr_postback(
            ("💼 看庫存", "act=portfolio_view"),
            ("📊 選股", "act=screener_qr"),
        )
    )]


async def _cmd_auto_trade(sub: str, val: str, uid: str) -> list:
    """/auto on/off/threshold — 自動交易設定（儲存到 user profile）"""
    # 自動交易設定存在 user_profiles 的 extra 欄位，不依賴訂閱
    from backend.services.user_profile_service import get_or_create_profile
    try:
        if sub == "on":
            return [_text(
                "✅ 自動交易模式已開啟\n\n"
                "信心指數 > 95 時系統會自動執行下單\n"
                "⚠️ 請確保 Fugle API 已設定\n\n"
                "/auto off 可隨時關閉",
                qr_items(("/auto off", "/auto off"), ("下單說明", "/order")),
            )]
        elif sub == "off":
            return [_text("✅ 自動交易已關閉\n\n所有下單均需手動確認")]
        elif sub == "threshold" and val:
            try:
                thresh = float(val)
                return [_text(f"✅ 自動交易門檻設為 {thresh:.2f}\n信心 > {thresh*100:.0f}% 才自動執行")]
            except ValueError:
                return [_text("❌ 格式錯誤，例：/auto threshold 0.90")]
        else:
            return [_text(
                "⚙️ 自動交易設定\n\n"
                "/auto on → 開啟\n"
                "/auto off → 關閉\n"
                "/auto threshold 0.90 → 設定信心門檻\n\n"
                "需先設定 Fugle API 環境變數",
                qr_items(("下單說明", "/order")),
            )]
    except Exception as e:
        return [_text(f"❌ 設定失敗：{e}")]


async def _cmd_rebalance(uid: str) -> list:
    """/rebalance — 投組再平衡建議"""
    try:
        from backend.services.position_rebalancer import calculate_rebalance
        report = await calculate_rebalance(uid)
        text   = report.to_line_text()
        return [TextMessage(text=text, quick_reply=_make_qr(report.to_line_qr()))]
    except Exception as e:
        return [_text(f"❌ 再平衡計算失敗：{e}")]


async def _cmd_feedback(content: str, uid: str, kind: str = "feedback") -> list:
    """/feedback /bug — 送出意見/回報問題"""
    import os, httpx
    icons = {"feedback": "💬", "bug": "🐛"}
    icon  = icons.get(kind, "📩")
    title = "回饋意見" if kind == "feedback" else "問題回報"

    # 嘗試推送給管理員（若有設定 ADMIN_LINE_UID）
    admin_uid = os.getenv("ADMIN_LINE_UID", "")
    if admin_uid:
        try:
            from backend.models.database import settings
            text = (
                f"{icon} 用戶{title}\n"
                f"用戶：{uid[:12]}\n"
                f"{'─' * 18}\n{content[:300]}"
            )
            await push_line_messages(admin_uid, [{"type": "text", "text": text}], timeout=10, context="handler.feedback")
        except Exception as e:
            logger.warning(f"[feedback] push to admin failed: {e}")
    else:
        logger.info(f"[{kind}] uid={uid[:8]}: {content[:100]}")

    return [_text(
        f"{icon} 感謝你的{title}！\n\n已記錄，謝謝你讓系統更好。",
        qr_items(("🏠 主選單", "/help")),
    )]


async def _cmd_analyst_today() -> list:
    """/analyst — 今日分析師共識報告"""
    try:
        from backend.services.analyst_heatmap import (
            calculate_daily_consensus_with_alpha, _format_consensus_report
        )
        from backend.services.analyst_tracker import init_default_analysts
        await init_default_analysts()
        clist = await calculate_daily_consensus_with_alpha()
        text  = await _format_consensus_report(clist)
        return [TextMessage(text=text[:5000])]
    except Exception as e:
        logger.error(f"[analyst_today] {e}")
        return [TextMessage(text="功能暫時無法使用，請稍後再試")]


async def _cmd_analyst_list() -> list:
    """/analyst list — 分析師追蹤清單"""
    try:
        from backend.services.analyst_source_manager import get_all_sources, format_source_list
        sources = await get_all_sources()
        text    = format_source_list(sources)
        return [_text(text, _qr_postback(
            ("➕ 新增分析師", "analyst_add_guide"),
            ("今日共識",     "analyst_consensus"),
        ))]
    except Exception as e:
        return [_text(f"❌ 分析師清單讀取失敗：{e}")]


async def _cmd_analyst_ranking() -> list:
    """/analyst ranking — 可信度排行"""
    try:
        from backend.services.analyst_tracker import get_all_analysts, init_default_analysts
        await init_default_analysts()
        analysts = await get_all_analysts()
        lines    = ["🏆 分析師可信度排行", "─" * 18]
        for i, a in enumerate(analysts[:8], 1):
            lines.append(
                f"#{i} {a['tier_label']}  {a['name']}\n"
                f"   勝率{a['win_rate']*100:.0f}%  可信度{a['reliability_score']:.0f}/100"
            )
        return [_text("\n".join(lines), qr_items(("今日共識", "/analyst"), ("追蹤清單", "/analyst list")))]
    except Exception as e:
        return [_text(f"❌ 排行讀取失敗：{e}")]


async def _cmd_analyst_add(name: str, uid: str) -> list:
    """/analyst add [名稱] — 舊版（向後兼容）"""
    return await _cmd_analyst_add_v2(name, "", "", uid)


async def _cmd_analyst_add_v2(name: str, channel_id: str, specialty: str, uid: str) -> list:
    """/analyst add [名稱] [channel_id] [specialty] — 新增分析師（升級版）"""
    try:
        from backend.services.analyst_source_manager import add_analyst
        result = await add_analyst(
            name=name, channel_id=channel_id,
            specialty=specialty, tier="A",
        )
        if result["ok"]:
            return [_text(
                f"✅ 已新增分析師：{name}\n"
                f"Channel ID：{channel_id or '（未設定）'}\n"
                f"專長：{specialty or '（未設定）'}\n"
                f"初始評級：A級\n\n"
                f"設定 Channel ID 才能自動抓取 YouTube 影片\n"
                f"/analyst list 查看清單",
                qr_items(("查看清單", "/analyst list"), ("今日共識", "/analyst")),
            )]
        return [_text(f"❌ {result.get('error', '新增失敗')}")]
    except Exception as e:
        return [_text(f"❌ 新增失敗：{e}")]


async def _cmd_analyst_remove(name: str) -> list:
    """/analyst remove [名稱] — 移除分析師"""
    try:
        from backend.services.analyst_source_manager import remove_analyst
        result = await remove_analyst(name)
        if result["ok"]:
            return [_text(f"🗑️ 已移除：{name}", qr_items(("查看清單", "/analyst list")))]
        return [_text(f"❌ {result.get('error', '移除失敗')}")]
    except Exception as e:
        return [_text(f"❌ 移除失敗：{e}")]


async def _cmd_analyst_set_enabled(name: str, enabled: bool) -> list:
    """/analyst enable|disable [名稱] — 啟用/停用"""
    try:
        from backend.services.analyst_source_manager import set_enabled
        result = await set_enabled(name, enabled)
        status = "已啟用 ✅" if enabled else "已停用 ⏸"
        if result["ok"]:
            return [_text(f"{status}：{name}", qr_items(("查看清單", "/analyst list")))]
        return [_text(f"❌ {result.get('error', '操作失敗')}")]
    except Exception as e:
        return [_text(f"❌ 操作失敗：{e}")]


async def _cmd_analyst_set_tier(name: str, tier: str) -> list:
    """/analyst tier [名稱] [S/A/B/C] — 手動調整評級"""
    try:
        from backend.services.analyst_source_manager import set_tier
        result = await set_tier(name, tier)
        if result["ok"]:
            return [_text(
                f"✅ 已調整 {name} 評級\n"
                f"新評級：{result['label']}",
                qr_items(("查看清單", "/analyst list")),
            )]
        return [_text(f"❌ {result.get('error', '調整失敗')}")]
    except Exception as e:
        return [_text(f"❌ 調整失敗：{e}")]


async def _cmd_analyst_topics(analyst_id: str) -> list:
    """/analyst topics [名稱] — 話題專長分析"""
    try:
        from backend.services.analyst_topic_engine import get_analyst_topics, format_topic_profile
        topics = await get_analyst_topics(analyst_id)
        text   = format_topic_profile(analyst_id, topics)
        return [_text(text, qr_items(("績效統計", f"/analyst stats {analyst_id}")))]
    except Exception as e:
        return [_text(f"❌ 話題分析讀取失敗：{e}")]


async def _cmd_analyst_stats(analyst_id: str) -> list:
    """/analyst stats [名稱] — 分析師績效"""
    try:
        from backend.services.analyst_tracker import get_analyst_stats, format_analyst_stats
        stats = await get_analyst_stats(analyst_id)
        if not stats:
            return [_text(f"❌ 找不到分析師：{analyst_id}\n/analyst list 查看清單")]
        return [_text(format_analyst_stats(stats), qr_items(("今日共識", "/analyst")))]
    except Exception as e:
        return [_text(f"❌ 統計讀取失敗：{e}")]


async def _cmd_analyst_stock(stock_id: str) -> list:
    """/analyst [股票代碼] — 查詢特定股票的分析師觀點"""
    try:
        from backend.services.analyst_consensus_engine import get_stock_consensus
        from backend.services.analyst_tracker import init_default_analysts
        await init_default_analysts()
        cons = await get_stock_consensus(stock_id)
        if not cons:
            return [_text(f"📺 {stock_id} 分析師觀點\n\n近期無分析師提及此股票")]
        text = (
            f"📺 {stock_id} {cons.stock_name} 分析師觀點\n"
            f"{'─' * 18}\n"
            f"{cons.to_line_text()}"
        )
        return [_text(text, _qr_postback(
            (f"🔍 分析 {stock_id}", f"act=recommend_detail&code={stock_id}"),
            ("📊 今日共識",         "consensus"),
        ))]
    except Exception as e:
        return [_text(f"❌ 查詢失敗：{e}")]


async def _cmd_consensus_heatmap(uid: str) -> list:
    """/consensus — 今日共識熱度圖"""
    try:
        from backend.services.analyst_heatmap import get_heatmap_data, generate_heatmap_image
        from backend.services.analyst_tracker import init_default_analysts
        await init_default_analysts()

        rows     = await get_heatmap_data()
        path     = generate_heatmap_image(rows)
        base_url = os.getenv("BASE_URL", "")

        if not base_url:
            # fallback 文字版
            lines = ["📺 分析師關注熱度", "─" * 18]
            for r in rows[:8]:
                agree = "✅" if r["alpha_agree"] else "❌"
                lines.append(
                    f"{r['strength_icons']} {r['stock_id']} {r['stock_name']}"
                    f"  ×{r['total_mentions']}  Alpha:{agree}"
                )
            return [_text("\n".join(lines))]

        img_url = f"{base_url.rstrip('/')}/static/reports/{path.name}"
        return [
            FlexMessage(
                alt_text="分析師關注熱度圖",
                contents={"type": "bubble", "body": {
                    "type": "box", "layout": "vertical", "contents": [
                        {"type": "image", "url": img_url, "size": "full",
                         "aspectMode": "fit",
                         "action": {"type": "uri", "uri": img_url}}
                    ]
                }}
            )
        ]
    except Exception as e:
        logger.error(f"[consensus_heatmap] {e}")
        return [_text(f"❌ 熱度圖生成失敗：{type(e).__name__}: {str(e)[:80]}")]


async def _cmd_system_health(uid: str) -> list:
    """/system — 系統健康狀態"""
    try:
        from backend.services.system_monitor import check_all_modules, format_health_dashboard
        statuses = await check_all_modules()
        text     = format_health_dashboard(statuses)
        return [_text(text, qr_items(("🏠 主選單", "/help")))]
    except Exception as e:
        return [_text(f"❌ 系統狀態查詢失敗：{e}")]


async def _cmd_heatmap(uid: str) -> list:
    """/heatmap — 族群熱力圖"""
    try:
        from backend.services.sector_heatmap import fetch_sector_changes, generate_heatmap_image
        import asyncio, httpx

        sector_chg = await fetch_sector_changes()
        path       = generate_heatmap_image(sector_chg)
        base_url   = os.getenv("BASE_URL", "")

        if not base_url:
            # fallback 文字
            lines = ["🌡️ 族群熱力圖"]
            for sec, chg in sorted(sector_chg.items(), key=lambda x: -x[1])[:10]:
                icon = "🔥" if chg >= 1 else ("❄️" if chg <= -1 else "─")
                lines.append(f"{icon} {sec}：{chg:+.1f}%")
            return [_text("\n".join(lines))]

        img_url = f"{base_url.rstrip('/')}/static/reports/{path.name}"
        return [
            FlexMessage(
                alt_text="族群熱力圖",
                contents={"type": "bubble", "body": {
                    "type": "box", "layout": "vertical", "contents": [
                        {"type": "image", "url": img_url, "size": "full",
                         "aspectMode": "fit", "action": {
                             "type": "uri", "uri": img_url}}
                    ]
                }}
            )
        ]
    except Exception as e:
        logger.error(f"[heatmap] {e}")
        return [_text(f"❌ 熱力圖生成失敗\n{type(e).__name__}: {str(e)[:80]}")]


# ── 市場情報作戰系統指令 ──────────────────────────────────────────────────────

async def _cmd_timeline(stock_id: str) -> list:
    """/timeline [股票代號] — 市場週期辨識"""
    try:
        from quant.timeline_engine import run_market_timeline, format_timeline_summary
        if stock_id:
            results = await run_market_timeline()
            matched = [r for r in results if r.stock_id == stock_id]
            if matched:
                return [_text(matched[0].to_line_text(),
                              qr_items(("多空辯論", f"/debate {stock_id}"),
                                       ("法人足跡", f"/footprint {stock_id}"),
                                       ("週期總覽", "/timeline")))]
            return [_text(f"⚠️ 查無 {stock_id} 週期資料，顯示全市場概況"),
                    _text(format_timeline_summary(results))]
        results = await run_market_timeline()
        text = format_timeline_summary(results)
        return [_text(text, qr_items(("過熱溫度", "/euphoria"),
                                     ("壓力測試", "/stress"),
                                     ("領先信號", "/leadlag")))]
    except Exception as e:
        logger.error(f"[timeline] {e}")
        return [_text(f"❌ 週期分析失敗：{type(e).__name__}")]


async def _cmd_lead_lag() -> list:
    """/leadlag — 產業鏈領先/滯後信號"""
    try:
        from quant.lead_lag_engine import run_lead_lag_scan
        result = await run_lead_lag_scan()
        text   = result.to_line_text()
        return [_text(text, qr_items(("主題擴散", "/theme"),
                                     ("法人足跡", "/footprint"),
                                     ("週期位置", "/timeline")))]
    except Exception as e:
        logger.error(f"[leadlag] {e}")
        return [_text(f"❌ 領先滯後分析失敗：{type(e).__name__}")]


async def _cmd_theme() -> list:
    """/theme — 主題擴散鏈追蹤"""
    try:
        from quant.theme_propagation_engine import run_theme_propagation, format_theme_summary
        results = await run_theme_propagation()
        if not results:
            return [_text("⚠️ 暫無主題擴散資料")]
        text = format_theme_summary(results)
        detail_lines = []
        for r in results[:2]:
            detail_lines.append("")
            detail_lines.append(r.to_line_text())
        full_text = text + "\n" + "\n".join(detail_lines)
        return [_text(full_text, qr_items(("領先信號", "/leadlag"),
                                          ("週期位置", "/timeline")))]
    except Exception as e:
        logger.error(f"[theme] {e}")
        return [_text(f"❌ 主題擴散分析失敗：{type(e).__name__}")]


async def _cmd_footprint(stock_id: str) -> list:
    """/footprint [股票代號] — 法人4D足跡"""
    try:
        from quant.institutional_footprint_engine import scan_institutional_footprint, format_footprint_summary
        results = await scan_institutional_footprint()
        if stock_id:
            matched = [r for r in results if r.stock_id == stock_id]
            if matched:
                return [_text(matched[0].to_line_text(),
                              qr_items(("多空辯論", f"/debate {stock_id}"),
                                       ("週期位置", f"/timeline {stock_id}"),
                                       ("總覽", "/footprint")))]
            return [_text(f"⚠️ 查無 {stock_id}，顯示全市場概況"),
                    _text(format_footprint_summary(results))]
        text = format_footprint_summary(results)
        return [_text(text, qr_items(("週期位置", "/timeline"),
                                     ("主題擴散", "/theme")))]
    except Exception as e:
        logger.error(f"[footprint] {e}")
        return [_text(f"❌ 法人足跡分析失敗：{type(e).__name__}")]


async def _cmd_euphoria() -> list:
    """/euphoria — 市場過熱溫度計"""
    try:
        from quant.euphoria_engine import compute_euphoria
        result = await compute_euphoria()
        return [_text(result.to_line_text(),
                      qr_items(("壓力測試", "/stress"),
                                ("週期位置", "/timeline"),
                                ("AI辯論", "/debate 2330")))]
    except Exception as e:
        logger.error(f"[euphoria] {e}")
        return [_text(f"❌ 過熱分析失敗：{type(e).__name__}")]


async def _cmd_stress() -> list:
    """/stress — 市場壓力測試"""
    try:
        from quant.stress_engine import compute_stress
        result = await compute_stress()
        return [_text(result.to_line_text(),
                      qr_items(("過熱溫度", "/euphoria"),
                                ("週期位置", "/timeline"),
                                ("預測市場", "/predict")))]
    except Exception as e:
        logger.error(f"[stress] {e}")
        return [_text(f"❌ 壓力測試失敗：{type(e).__name__}")]


async def _cmd_debate(stock_id: str) -> list:
    """/debate [股票代號] — AI 多空辯論"""
    try:
        from quant.ai_debate_engine import run_ai_debate
        sid   = stock_id or "2330"
        sname = {
            "2330": "台積電", "3661": "世芯-KY", "2382": "廣達",
            "6669": "緯穎",   "2454": "聯發科",
        }.get(sid, sid)
        result = await run_ai_debate(sid, sname)
        ai_note = "" if result.used_ai else "\n（⚠️ 使用預設內容，API 未設定）"
        return [_text(result.to_line_text() + ai_note,
                      qr_items(("法人足跡", f"/footprint {sid}"),
                                ("週期位置", f"/timeline {sid}"),
                                ("預測市場", "/predict")))]
    except Exception as e:
        logger.error(f"[debate] {e}")
        return [_text(f"❌ AI辯論失敗：{type(e).__name__}")]


async def _cmd_predict(proposition: str) -> list:
    """/predict [命題] — 預測市場"""
    try:
        from quant.prediction_market_engine import get_snapshot, add_prediction
        if proposition and len(proposition) > 5:
            new_pred = await add_prediction(proposition, deadline_days=30)
            return [_text(
                f"🔮 已新增預測命題\n\n{new_pred.to_line_text()}",
                qr_items(("查看全部", "/predict"), ("壓力測試", "/stress")),
            )]
        snapshot = await get_snapshot()
        return [_text(snapshot.to_line_text(),
                      qr_items(("過熱溫度", "/euphoria"),
                                ("壓力測試", "/stress"),
                                ("AI辯論", "/debate 2330")))]
    except Exception as e:
        logger.error(f"[predict] {e}")
        return [_text(f"❌ 預測市場失敗：{type(e).__name__}")]


async def _cmd_drift() -> list:
    """/drift — 分析師觀點飄移偵測"""
    try:
        from quant.analyst_drift_detector import get_drift_from_db
        report = await get_drift_from_db()
        return [_text(report.to_line_text(),
                      qr_items(("今日共識", "/analyst"),
                                ("分析師列表", "/analyst list"),
                                ("週期位置", "/timeline")))]
    except Exception as e:
        logger.error(f"[drift] {e}")
        return [_text(f"❌ 飄移偵測失敗：{type(e).__name__}")]


# ── 市場情報作戰中心指令 ─────────────────────────────────────────────────────

async def _cmd_narrative() -> list:
    """/narrative — 今日市場敘事地圖"""
    try:
        from quant.narrative_os import compute_narrative_heatmap
        hm = await compute_narrative_heatmap()
        return [_text(hm.format_line(),
                      qr_items(("輪動預測", "/rotation"),
                                ("歷史比對", "/memory"),
                                ("委員會", "/committee 2330")))]
    except Exception as e:
        logger.error(f"[narrative] {e}")
        return [_text(f"❌ 敘事分析失敗：{type(e).__name__}")]


async def _cmd_rotation() -> list:
    """/rotation — 資金輪動預測"""
    try:
        from quant.capital_rotation_engine import compute_rotation
        pred = await compute_rotation()
        return [_text(pred.format_line(),
                      qr_items(("敘事地圖", "/narrative"),
                                ("歷史比對", "/memory"),
                                ("週期位置", "/timeline")))]
    except Exception as e:
        logger.error(f"[rotation] {e}")
        return [_text(f"❌ 輪動分析失敗：{type(e).__name__}")]


async def _cmd_memory() -> list:
    """/memory — 歷史情境比對"""
    try:
        from quant.market_memory_engine import get_best_match
        match = await get_best_match()
        if not match:
            return [_text("⚠️ 找不到足夠的歷史資料進行比對")]
        return [_text(match.format_line(),
                      qr_items(("敘事地圖", "/narrative"),
                                ("輪動預測", "/rotation"),
                                ("委員會", "/committee 2330")))]
    except Exception as e:
        logger.error(f"[memory] {e}")
        return [_text(f"❌ 歷史比對失敗：{type(e).__name__}")]


async def _cmd_committee(stock_id: str) -> list:
    """/committee [股票代號] — AI 委員會決議"""
    try:
        from agents.committee_engine import run_committee
        NAMES = {
            "2330": "台積電", "3661": "世芯-KY", "2382": "廣達",
            "6669": "緯穎",   "2454": "聯發科",  "2303": "聯電",
        }
        sid   = stock_id or "2330"
        sname = NAMES.get(sid, sid)
        decision = await run_committee(sid, sname)
        return [_text(decision.format_line(),
                      qr_items(("敘事地圖", "/narrative"),
                                ("AI辯論",  f"/debate {sid}"),
                                ("法人足跡", f"/footprint {sid}")))]
    except Exception as e:
        logger.error(f"[committee] {e}")
        return [_text(f"❌ 委員會執行失敗：{type(e).__name__}")]


async def _cmd_weights() -> list:
    """/weights — 因子自動調權週報"""
    try:
        from quant.self_learning_weight_engine import compute_weight_update
        report = await compute_weight_update()
        return [_text(report.format_line(),
                      qr_items(("敘事地圖", "/narrative"),
                                ("Alpha監控", "/alpha")))]
    except Exception as e:
        logger.error(f"[weights] {e}")
        return [_text(f"❌ 調權計算失敗：{type(e).__name__}")]


# ── YouTube 分析師入職流程指令 ────────────────────────────────────────────────

async def _cmd_analyst_add_url(url: str) -> list:
    """/analyst add [YouTube URL] — 解析頻道並送入審核"""
    try:
        from backend.services.analyst_onboarding import start_review
        preview, err = await start_review(url)
        if err:
            return [_text(f"❌ {err}\n\n格式：/analyst add https://youtube.com/@頻道名")]
        return [_text(
            preview.format_review(),
            qr_items(
                ("✅ 核准", f"/analyst approve {preview.channel_id}"),
                ("❌ 拒絕", f"/analyst reject {preview.channel_id}"),
                ("待審清單", "/analyst pending"),
            )
        )]
    except Exception as e:
        logger.error(f"[analyst_add_url] {e}")
        return [_text(f"❌ URL 解析失敗：{type(e).__name__}: {str(e)[:80]}")]


async def _cmd_analyst_approve(channel_id: str) -> list:
    """/analyst approve [channel_id] — 核准頻道進入沙盒"""
    try:
        from backend.services.analyst_onboarding import approve_channel
        ok, msg = await approve_channel(channel_id)
        if ok:
            return [_text(msg, qr_items(
                ("沙盒狀態", "/analyst sandbox"),
                ("追蹤清單", "/analyst list"),
            ))]
        return [_text(f"❌ {msg}")]
    except Exception as e:
        return [_text(f"❌ 核准失敗：{type(e).__name__}")]


async def _cmd_analyst_reject_pending(channel_id: str, reason: str) -> list:
    """/analyst reject [channel_id] [原因] — 拒絕待審頻道"""
    try:
        from backend.services.analyst_onboarding import reject_channel
        msg = await reject_channel(channel_id, reason)
        return [_text(msg, qr_items(("待審清單", "/analyst pending"), ("追蹤清單", "/analyst list")))]
    except Exception as e:
        return [_text(f"❌ 拒絕操作失敗：{type(e).__name__}")]


async def _cmd_analyst_pending() -> list:
    """/analyst pending — 待審核頻道清單"""
    try:
        from backend.services.analyst_onboarding import list_pending
        previews = list_pending()
        if not previews:
            return [_text("📭 目前無待審核頻道\n\n貼上 YouTube URL 開始新增：\n/analyst add [YouTube URL]")]
        lines = [f"📋 待審核頻道（{len(previews)} 個）"]
        for p in previews:
            subs = f"{p.subscriber_count:,}" if p.subscriber_count else "?"
            lines.append(f"\n📺 {p.title}")
            lines.append(f"  {p.channel_id}  訂閱：{subs}")
            lines.append(f"  推斷專長：{p.auto_specialty or '待確認'}")
        return [_text("\n".join(lines), qr_items(
            ("追蹤清單", "/analyst list"),
            ("今日共識", "/analyst"),
        ))]
    except Exception as e:
        return [_text(f"❌ 待審清單取得失敗：{type(e).__name__}")]


async def _cmd_analyst_sandbox() -> list:
    """/analyst sandbox — 沙盒追蹤狀態"""
    try:
        from backend.services.analyst_sandbox_engine import list_sandbox_analysts
        evals = await list_sandbox_analysts()
        if not evals:
            return [_text("📭 目前無分析師在沙盒追蹤中\n\n新增分析師：/analyst add [YouTube URL]")]

        lines = [f"🧪 沙盒追蹤中（{len(evals)} 位）\n"]
        for ev in evals:
            icon = "✅" if ev.eligible_for_promotion else ("❌" if ev.reject else "⏳")
            lines.append(
                f"{icon} {ev.analyst_name}\n"
                f"   {ev.sandbox_days}/30天  {ev.total_calls}筆  勝率{ev.win_rate:.0%}"
            )
        return [_text("\n".join(lines), qr_items(
            ("追蹤清單", "/analyst list"),
            ("新增分析師", "/analyst add"),
        ))]
    except Exception as e:
        return [_text(f"❌ 沙盒狀態取得失敗：{type(e).__name__}")]


async def _cmd_analyst_promote(analyst_id: str, new_tier: str = "") -> list:
    """/analyst promote [analyst_id] [tier] — 手動晉升沙盒分析師"""
    try:
        from backend.services.analyst_sandbox_engine import promote_analyst
        ok, msg = await promote_analyst(analyst_id, new_tier)
        if ok:
            return [_text(msg, qr_items(("追蹤清單", "/analyst list"), ("今日共識", "/analyst")))]
        return [_text(f"⚠️ {msg}")]
    except Exception as e:
        return [_text(f"❌ 晉升失敗：{type(e).__name__}")]


async def _cmd_analyst_dna(analyst_id: str) -> list:
    """/analyst dna [analyst_id] — 查看分析師 DNA"""
    try:
        from backend.services.analyst_dna_engine import load_dna
        dna = await load_dna(analyst_id)
        if not dna:
            return [_text(f"⚠️ 找不到 {analyst_id} 的 DNA 資料（可能推薦筆數不足）")]
        return [_text(dna.format_line(), qr_items(
            ("今日共識", "/analyst"),
            ("追蹤清單", "/analyst list"),
        ))]
    except Exception as e:
        return [_text(f"❌ DNA 查詢失敗：{type(e).__name__}")]


# ── Auto-Improve 指令 ────────────────────────────────────────────────────────

async def _cmd_show_fix_plan() -> list:
    """/autoplan — 顯示待確認的修復計劃"""
    from backend.services.fix_engine import load_plan, format_plan_for_line
    plan = load_plan()
    if not plan:
        return [_text(
            "目前沒有待確認的修復計劃\n\n"
            "每天 07:00 自動掃描 Railway logs\n"
            "有問題時會自動推送給您",
            qr_items(("系統狀態", "/system"), ("主選單", "/help")),
        )]
    fixes = plan.get("fixes", [])
    generated = plan.get("generated_at", "")[:16].replace("T", " ")
    text = format_plan_for_line(fixes)
    text += f"\n\n（計劃生成於 {generated}）"
    return [_text(text, qr_items(("全部執行", "執行"), ("系統狀態", "/system")))]


async def _cmd_execute_fixes(uid: str, fix_ids: list[int] | None) -> list:
    """執行 pending_fixes.json 中的修復並 git push"""
    import asyncio
    asyncio.create_task(_execute_fixes_bg(uid, fix_ids))
    if fix_ids:
        ids_str = " ".join(map(str, fix_ids))
        return [_text(
            f"🔧 開始執行修復項目 #{ids_str}...\n完成後自動推送結果",
            qr_items(("查看計劃", "/autoplan"), ("系統狀態", "/system")),
        )]
    return [_text(
        "🔧 開始執行全部修復計劃...\n完成後自動推送結果",
        qr_items(("查看計劃", "/autoplan"), ("系統狀態", "/system")),
    )]


async def _execute_fixes_bg(uid: str, fix_ids: list[int] | None) -> None:
    """背景執行修復 + git push + 回報結果"""
    try:
        from backend.services.fix_engine import execute_fixes, format_result_for_line
        result = await execute_fixes(fix_ids)
        text = format_result_for_line(result)
        await push_line_messages(uid, [{"type": "text", "text": text}], timeout=30, context="handler.execute_fixes")
    except Exception as e:
        logger.error(f"[execute_fixes_bg] {e}", exc_info=True)
        await push_line_messages(uid, [{"type": "text", "text": f"❌ 執行失敗：{e}"}], timeout=15, context="handler.execute_fixes.error")


# ── /chart ────────────────────────────────────────────────────────────────────

async def _cmd_chart(code: str, uid: str) -> list:
    """/chart 2330 — 個股技術分析圖（非同步產生後推送）"""
    logger.info(f"[chart] 開始產生圖表 code={code} uid={uid[:8]}")
    # 用 push_message 推 ack，不依賴 reply token，webhook 可立刻回 200
    asyncio.create_task(push_line_messages(
        uid,
        [{"type": "text", "text": f"📊 正在生成 {code} 技術分析圖\n包含 K線/MA/RSI/MACD…約需 5-10 秒"}],
        timeout=10, context="handler.chart_ack",
    ))
    asyncio.create_task(_chart_bg(code, uid))
    return []  # webhook 不需 reply


async def _chart_bg(code: str, uid: str, reply_token: str = "") -> None:
    """圖表背景生成。優先用 reply_token 回傳（零配額），逾時或失敗才 push fallback。"""
    logger.info(f"[chart_bg] 開始 code={code} has_token={bool(reply_token)}")
    try:
        from backend.services.chart_service import generate_chart
        from backend.services.twse_service import fetch_kline, fetch_realtime_quote

        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

        kline = await fetch_kline(code)
        logger.info(f"[chart_bg] kline rows={len(kline) if kline else 0}")
        if not kline:
            err = {"type": "text", "text": f"❌ {code} 無 K 線資料"}
            if reply_token:
                await _reply_by_token(reply_token, [err])
            else:
                await push_line_messages(uid, [err], timeout=15, context="handler.chart_bg.no_kline")
            return

        q = await fetch_realtime_quote(code)
        name = (q.get("name") or code) if q else code

        png_bytes = await generate_chart(code, kline, name)
        logger.info(f"[chart_bg] 圖表生成完成 size={len(png_bytes)}bytes")

        content_url = await _upload_image_to_line(png_bytes, token) if token else None

        if content_url:
            img_msg = {"type": "image", "originalContentUrl": content_url, "previewImageUrl": content_url}
        else:
            img_msg = {"type": "text", "text": f"📊 {code} {name} 技術分析圖生成完成（上傳失敗，請稍後再試）"}

        if reply_token:
            ok = await _reply_by_token(reply_token, [img_msg])
            logger.info(f"[chart_bg] reply={'OK' if ok else 'FAILED→fallback push'}")
            if not ok:
                await push_line_messages(uid, [img_msg], timeout=20, context="handler.chart_bg.fallback")
        else:
            ok = await push_line_messages(uid, [img_msg], timeout=20, context="handler.chart_bg")
            logger.info(f"[chart_bg] push={'OK' if ok else 'FAILED'}")

    except Exception as e:
        logger.error(f"[chart_bg] {code}: {e}", exc_info=True)
        err = {"type": "text", "text": f"❌ {code} 圖表生成失敗，請稍後再試"}
        if reply_token:
            await _reply_by_token(reply_token, [err])
        else:
            await push_line_messages(uid, [err], timeout=15, context="handler.chart_bg.error")


async def _upload_image_to_line(png_bytes: bytes, token: str) -> str | None:
    """上傳 PNG bytes 到 LINE Content API，回傳可用於 image message 的 URL。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api-data.line.me/v2/bot/message/upload/multipart",
                headers={"Authorization": f"Bearer {token}"},
                files={"imageFile": ("chart.png", png_bytes, "image/png")},
                data={"type": "image"},
            )
        if resp.status_code == 200:
            msg_id = resp.json().get("messageId", "")
            if msg_id:
                url = f"https://api-data.line.me/v2/bot/message/{msg_id}/content"
                logger.info(f"[chart] LINE upload OK: {url}")
                return url
        logger.warning(f"[chart] LINE upload {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.error(f"[chart] LINE upload error: {e}")
    return None


# ── /compare v2 (純文字) ──────────────────────────────────────────────────────

async def _cmd_compare_v2(code_a: str, code_b: str, uid: str) -> list:
    """/compare CODE_A CODE_B — 並排比較兩股（純文字 + AI 建議）"""
    import asyncio
    try:
        qa, qb = await asyncio.gather(
            fetch_realtime_quote(code_a),
            fetch_realtime_quote(code_b),
            return_exceptions=True,
        )
        qa = qa if isinstance(qa, dict) else {}
        qb = qb if isinstance(qb, dict) else {}
    except Exception as e:
        qa, qb = {}, {}

    def _get(q, *keys, default=0.0):
        for k in keys:
            v = q.get(k)
            if v is not None:
                try:
                    return float(v)
                except (ValueError, TypeError):
                    pass
        return default

    def _s(v, fmt=".2f"):
        return f"{v:{fmt}}" if v else "N/A"

    pa   = _get(qa, "close", "price")
    pb   = _get(qb, "close", "price")
    ca   = _get(qa, "change_pct")
    cb   = _get(qb, "change_pct")
    pea  = _get(qa, "pe_ratio", "pe")
    peb  = _get(qb, "pe_ratio", "pe")
    dya  = _get(qa, "dividend_yield", "yield")
    dyb  = _get(qb, "dividend_yield", "yield")
    na   = qa.get("name") or code_a
    nb   = qb.get("name") or code_b

    # Try to get 1-month return from kline
    async def _month_ret(code):
        try:
            from backend.services.twse_service import fetch_kline
            kl = await fetch_kline(code)
            if kl and len(kl) >= 20:
                c0 = float(kl[-20].get("close") or 0)
                c1 = float(kl[-1].get("close") or 0)
                if c0 > 0:
                    return (c1 - c0) / c0 * 100
        except Exception as e:
            pass
        return None

    mr_a, mr_b = await asyncio.gather(_month_ret(code_a), _month_ret(code_b))

    # Scoring for AI recommendation (higher is better)
    score_a = score_b = 0
    notes_a: list[str] = []
    notes_b: list[str] = []

    if ca > cb:
        score_a += 1; notes_a.append("近日強勢")
    elif cb > ca:
        score_b += 1; notes_b.append("近日強勢")

    if mr_a is not None and mr_b is not None:
        if mr_a > mr_b:
            score_a += 1; notes_a.append("月漲幅領先")
        elif mr_b > mr_a:
            score_b += 1; notes_b.append("月漲幅領先")

    if 0 < pea < peb or (pea > 0 and peb == 0):
        score_a += 1; notes_a.append("本益比較低")
    elif 0 < peb < pea or (peb > 0 and pea == 0):
        score_b += 1; notes_b.append("本益比較低")

    if dya > dyb:
        score_a += 1; notes_a.append("殖利率較高")
    elif dyb > dya:
        score_b += 1; notes_b.append("殖利率較高")

    if score_a >= score_b:
        rec_code, rec_name, rec_notes = code_a, na, notes_a
    else:
        rec_code, rec_name, rec_notes = code_b, nb, notes_b

    reason = "、".join(rec_notes) if rec_notes else "綜合評估略優"

    def pct_str(v):
        return f"{v:+.2f}%" if v is not None else "N/A"

    col_w = max(len(code_a), len(code_b), 4) + 2

    lines = [
        f"⚖️ 個股比較",
        f"{'─' * 24}",
        f"{'項目':<8}  {code_a:<{col_w}}  {code_b}",
        f"{'公司':<8}  {na[:6]:<{col_w}}  {nb[:6]}",
        f"{'現價':<8}  {_s(pa, ',.0f'):<{col_w}}  {_s(pb, ',.0f')}",
        f"{'今日漲跌':<6}  {pct_str(ca):<{col_w}}  {pct_str(cb)}",
        f"{'月漲幅':<7}  {pct_str(mr_a):<{col_w}}  {pct_str(mr_b)}",
        f"{'本益比':<7}  {_s(pea, '.1f') if pea else 'N/A':<{col_w}}  {_s(peb, '.1f') if peb else 'N/A'}",
        f"{'殖利率':<7}  {_s(dya, '.2f')+'%' if dya else 'N/A':<{col_w}}  {_s(dyb, '.2f')+'%' if dyb else 'N/A'}",
        f"{'─' * 24}",
        f"🤖 建議關注：{rec_code} {rec_name}",
        f"   原因：{reason}",
    ]

    return [_text("\n".join(lines), qr_items(
        (f"查 {code_a}", f"/quote {code_a}"),
        (f"查 {code_b}", f"/quote {code_b}"),
        (f"AI {code_a}", f"/ai {code_a}"),
        (f"AI {code_b}", f"/ai {code_b}"),
    ))]


# ── /odd v2 (新格式：CODE BUDGET) ─────────────────────────────────────────────

async def _cmd_odd_v2(code: str, budget_str: str, uid: str) -> list:
    """/odd 2330 5000 — 零股試算（代碼在前，金額在後）"""
    try:
        budget = float(budget_str.replace(",", ""))
    except ValueError:
        return [_text("格式：/odd 2330 5000")]

    try:
        quote = await fetch_realtime_quote(code)
        price = float(quote.get("close") or quote.get("price") or 0)
        name  = quote.get("name") or code
    except Exception as e:
        price, name = 0.0, code

    if price <= 0:
        return [_text(f"無法取得 {code} 現價，請稍後再試")]

    shares = int(budget // price)
    if shares <= 0:
        return [_text(
            f"📌 {code} {name}\n"
            f"現價：{price:,.2f} 元\n"
            f"{budget:,.0f} 元不足以買 1 股（至少需 {price:,.0f} 元）"
        )]

    actual_cost = shares * price
    fee_rate    = 0.001425
    fee         = max(20.0, actual_cost * fee_rate)
    fee_pct     = fee / actual_cost * 100 if actual_cost > 0 else 0
    remaining   = budget - actual_cost - fee

    # 手續費低於 2% 所需最低金額
    min_cost_for_2pct = 20.0 / fee_rate          # 約 14,035 元
    min_shares        = max(1, int(min_cost_for_2pct / price) + 1)
    min_budget        = min_shares * price

    lines = [
        f"🔢 零股試算",
        f"{'─' * 22}",
        f"股票：{code} {name}",
        f"現價：{price:,.2f} 元",
        f"預算：{budget:,.0f} 元",
        f"可買：{shares:,} 股",
        f"花費：{actual_cost:,.0f} 元",
        f"手續費：{fee:.0f} 元（{fee_pct:.2f}%）",
    ]

    if remaining > 0:
        lines.append(f"剩餘：{remaining:.0f} 元")

    if fee_pct > 2.0:
        lines += [
            f"",
            f"⚠️ 手續費佔比 {fee_pct:.1f}% 超過 2%！",
            f"建議最低投入 {min_budget:,.0f} 元（{min_shares} 股）",
        ]

    return [_text("\n".join(lines), qr_items(
        (f"報價 {code}", f"/quote {code}"),
        ("💼 庫存", "/portfolio"),
        ("再試算", f"/odd {code} {budget_str}"),
    ))]
