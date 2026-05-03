"""LINE Bot Webhook — 多用戶 · Flex · Quick Reply · Postback · 策略推薦"""
import re
import sys, os, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import APIRouter, Request, HTTPException
from linebot.v3.messaging import (
    AsyncApiClient, AsyncMessagingApi, Configuration,
    ReplyMessageRequest, TextMessage, FlexMessage,
    QuickReply, QuickReplyItem, MessageAction,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhook import WebhookParser
from loguru import logger

from backend.models.database import settings, AsyncSessionLocal
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

@router.post("/webhook")
async def webhook(request: Request):
    sig  = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        events = parser.parse(body.decode(), sig)
    except InvalidSignatureError:
        raise HTTPException(400, "Invalid signature")
    except Exception as e:
        logger.error(f"Parse error: {e}")
        return "OK"

    for event in events:
        try:
            if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
                uid  = event.source.user_id
                text = event.message.text.strip()
                logger.info(f"[{uid[:8]}] text: {text!r}")
                msgs = await _handle_text(text, uid)
                await _reply(event.reply_token, msgs)

            elif isinstance(event, PostbackEvent):
                uid  = event.source.user_id
                data = event.postback.data
                logger.info(f"[{uid[:8]}] postback: {data!r}")
                msgs = await _handle_postback(data, uid)
                await _reply(event.reply_token, msgs)

        except Exception as e:
            logger.error(f"Event error: {e}")

    return "OK"


async def _reply(token: str, messages: list):
    try:
        async with AsyncApiClient(configuration) as client:
            api = AsyncMessagingApi(client)
            await api.reply_message(
                ReplyMessageRequest(reply_token=token, messages=messages[:5])
            )
    except Exception as e:
        logger.error(f"Reply error: {e}")


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
    if act == "more_menu":
        return [_flex_more_menu()]
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

    if act == "more_menu_v2":
        from line_webhook.flex_messages import flex_more_menu_v2
        return [_flex("更多功能", flex_more_menu_v2())]

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
    except Exception:
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

    # ── 斜線指令（精確比對）──────────────────────────────────────────────────
    if cmd == "/quote"    and len(parts) >= 2: return await _cmd_quote(parts[1])
    if cmd in ("/market", "/market_overview"):  return await _cmd_market()
    if cmd == "/portfolio":                     return await _cmd_portfolio(uid)
    if cmd == "/buy"      and len(parts) == 4:  return await _cmd_buy(parts[1], parts[2], parts[3], uid)
    if cmd == "/sell"     and len(parts) == 4:  return await _cmd_sell(parts[1], parts[2], parts[3], uid)
    if cmd == "/setcost"  and len(parts) == 3:  return await _cmd_setcost(int(parts[1]), float(parts[2]), uid)
    if cmd == "/history":                       return await _cmd_history(uid, parts[1] if len(parts)>1 else None)
    if cmd == "/tax":                           return await _cmd_tax(uid)
    if cmd == "/profile":                       return await _cmd_profile(uid)
    if cmd == "/alert"    and len(parts) == 4:  return await _cmd_alert(parts[1], parts[2], parts[3], uid)
    if cmd == "/alert_guide":                   return [_alert_guide()]
    if cmd == "/alert_list":                    return await _cmd_alert_list(uid)
    if cmd in ("/inst", "/institutional") and len(parts) >= 2: return await _cmd_inst(parts[1])
    if cmd == "/pe"       and len(parts) >= 2:  return await _cmd_pe(parts[1])
    if cmd == "/dividend" and len(parts) >= 2:  return await _cmd_dividend(parts[1])
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
    if cmd in ("/news", "/news_guide"):         return [_news_guide()]
    if cmd == "/n":                             return await _cmd_news_feed(uid)
    if cmd == "/p":                             return await _cmd_portfolio(uid)
    if cmd == "/r":                             return [_flex_screen_menu()]
    if cmd == "/strategy":                      return await _cmd_strategy_manage(uid)
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
        strategy = parts[1].lower() if len(parts) > 1 else ""
        if strategy in ("momentum", "value", "chip", "breakout"):
            return await _cmd_backtest_run(strategy, uid)
        return await _cmd_backtest_menu(uid)
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
    if cmd == "/odd" and len(parts) >= 2:
        # /odd 5000 2330  或  /odd 5000（預設推薦組合）
        budget_str = parts[1]
        code_arg   = parts[2] if len(parts) > 2 else None
        return await _cmd_odd(budget_str, code_arg, uid)
    if cmd == "/compare" and len(parts) >= 3:
        codes = parts[1:]   # 支援 2~3 支股票
        return await _cmd_compare_image(codes, uid)
    if cmd == "/strategy" and len(parts) >= 2:
        return await _cmd_strategy_analyze(parts[1])
    if cmd == "/track" and len(parts) >= 2:
        return await _cmd_track_history(parts[1])

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

    # ── 句中包含 4 碼數字 → 嘗試查報價 ─────────────────────────────────────
    import re
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
    q = await fetch_realtime_quote(code)
    if not q:
        return [_text(f"❌ 查無 {code}", _home_qr())]
    card = flex_quote(q)
    qr   = quick_reply_quote(code, q.get("price", 0))
    return [_flex(f"{q.get('name', code)} 報價", card, qr)]


async def _cmd_market() -> list:
    return await _cmd_market_card(None)


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
    except Exception:
        pass

    # 族群熱度 Top3（可選）
    sectors = []
    try:
        from quant.sector_rotation_engine import SectorRotationEngine
        engine    = SectorRotationEngine()
        strengths = engine.scan_mock()   # 用 mock 避免等待
        sectors   = [(s.name, s.composite_score) for s in strengths[:3]]
    except Exception:
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
    async with AsyncSessionLocal() as db:
        holdings = await portfolio_service.get_portfolio(db, uid)
    if not holdings:
        return [_text(
            "📂 庫存為空\n\n輸入 /buy 代碼 股數 成本 新增持股\n例：/buy 2330 1000 850",
            qr_items(("➕ 新增示範", "/buy 2330 1000 850"), ("📊 大盤", "/market"))
        )]
    carousel = flex_portfolio_carousel(holdings)
    qr       = quick_reply_portfolio()
    return [_flex("我的庫存", carousel, qr)]


async def _cmd_buy(code: str, shares_str: str, cost_str: str, uid: str) -> list:
    try:
        shares = int(shares_str); cost = float(cost_str)
        async with AsyncSessionLocal() as db:
            h = await portfolio_service.add_holding(db, code, shares, cost, uid)
        return [_text(
            f"✅ {h.stock_code} {h.stock_name}\n{shares:,}股 @ {cost}\n已加入庫存",
            qr_items(("💼 查庫存", "/portfolio"), ("🔔 設警報", f"/alert {code} price_above {int(cost*1.1)}"))
        )]
    except Exception as e:
        return [_text(f"❌ 失敗：{e}")]


async def _cmd_sell(code: str, shares_str: str, price_str: str, uid: str) -> list:
    try:
        shares = int(shares_str); price = float(price_str)
        async with AsyncSessionLocal() as db:
            holdings = await portfolio_service.get_portfolio(db, uid)
            h = next((h for h in holdings if h["stock_code"] == code), None)
            if not h:
                return [_text(f"❌ 庫存中無 {code}，請先 /buy 新增")]
            avg_cost = h["cost_price"]
            # 更新庫存
            updated = await portfolio_service.adjust_shares(db, h["id"], -shares, uid)
            # 記錄交易日誌
            log = await log_trade(
                db, uid, code, h.get("stock_name", ""), "SELL",
                price, shares, avg_cost,
            )
        pnl_str = f"{log.realized_pnl:+,.0f}"
        return [_text(
            f"🔴 賣出成交\n{code} {shares:,}股 @{price}\n"
            f"已實現損益：{pnl_str}\n"
            f"手續費：{log.commission:,.0f}  稅：{log.tax:,.0f}",
            qr_items(("💼 庫存", "/portfolio"), ("📋 紀錄", "/history"), ("💰 稅務", "/tax"))
        )]
    except Exception as e:
        return [_text(f"❌ 賣出失敗：{e}")]


async def _cmd_history(uid: str, code: str = None) -> list:
    async with AsyncSessionLocal() as db:
        logs = await get_history(db, uid, limit=15, stock_code=code)
    text = format_trade_history(logs)
    return [_text(text, qr_items(("💰 稅務", "/tax"), ("💼 庫存", "/portfolio")))]


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
    except Exception:
        pass
    return [_text(f"❌ 查無 {code} 估值資料")]


async def _cmd_dividend(code: str) -> list:
    from backend.services.dividend_service import fetch_dividend_by_code
    divs = await fetch_dividend_by_code(code)
    if not divs:
        return [_text(f"❌ 查無 {code} 除權息資料")]
    lines = [f"💰 {code} 除權息"]
    for d in divs[:4]:
        lines.append(f"日期：{d.get('ex_dividend_date','')}\n現金：{d.get('cash_dividend',0)}")
    return [_text("\n".join(lines), qr_items(("📈 報價", f"/quote {code}")))]


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
    except Exception:
        return [_text(report, _home_qr())]


async def _cmd_weekly(uid: str) -> list:
    report = await generate_weekly_report()
    return [_text(report, qr_items(("💼 庫存", "/portfolio"), ("🤖 AI分析", "/ai_portfolio")))]


async def _cmd_rec_dispatch(uid: str) -> list:
    """先回 ACK，然後背景跑分析再 push"""
    async with AsyncSessionLocal() as db:
        holdings = await portfolio_service.get_portfolio(db, uid)
    if not holdings:
        return [_text("庫存為空，先 /buy 新增持股再取得推薦",
                      qr_items(("新增示範", "/buy 2330 1000 850")))]
    import asyncio
    asyncio.create_task(_cmd_rec_full(uid, ""))
    return [_text(
        f"正在分析 {len(holdings)} 檔持股，約需 10-20 秒…\n\n"
        "分析完成後將自動推送策略推薦卡片",
        qr_items(("💼 庫存", "/portfolio"), ("🤖 AI分析", "/ai_portfolio"))
    )]


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

    import httpx
    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    payload = {
        "to": uid,
        "messages": [
            {"type": "text", "text": summary},
            {"type": "flex", "altText": "策略推薦", "contents": carousel},
        ],
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.line.me/v2/bot/message/push",
                         json=payload, headers=headers)
        logger.info(f"Push rec to {uid[:8]}: {r.status_code}")


async def _cmd_ai_ask(question: str, uid: str = "") -> TextMessage:
    if not settings.anthropic_api_key:
        return _text("❌ 未設定 API Key")
    try:
        # 1. 找相似舊答案
        if uid:
            async with AsyncSessionLocal() as db:
                cached = await find_similar_answer(db, uid, question)
            if cached:
                return _text(f"（3天內的分析）\n{cached}\n\n輸入問題重新查詢可獲得最新分析", _home_qr())

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

        return _text(answer, _home_qr())
    except Exception as e:
        return _text(f"❌ AI 錯誤：{e}")


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
        return [_text(text[:4800], qr_items(
            ("📈 報價",   f"/quote {stock_code}"),
            ("🏛 分點",   f"/broker {stock_code}"),
            ("💼 庫存",   "/portfolio"),
        ))]
    except Exception as e:
        return [_text(f"❌ 個股分析失敗：{e}")]


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
    except Exception:
        msg = "📰 今日暫無新聞"
    return [_text(msg, qr_items(
        ("🔄 更新", "/n"), ("🤖 AI簡評", "/ai 今日台股氣氛"), ("📊 大盤", "/market"),
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
    )


# ── 訊息建構輔助 ──────────────────────────────────────────────────────────────

def _make_qr(qr_dict: dict | None) -> QuickReply | None:
    if not qr_dict:
        return None
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(
            label=item["action"]["label"],
            text=item["action"]["text"],
        ))
        for item in qr_dict.get("items", [])
    ])


def _text(text: str, quick_reply: dict = None) -> TextMessage:
    return TextMessage(text=text[:5000], quick_reply=_make_qr(quick_reply))


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
    """/pipeline [代碼] — 觸發完整量化分析流程"""
    import asyncio
    asyncio.create_task(_pipeline_bg(code, uid))
    return [_text(
        f"🔬 啟動 {code} 機構級量化分析...\n\n"
        "流程：因子IC → 動態加權 → 盤態偵測 → Multi-Alpha → Walk-Forward\n"
        "約需 15-30 秒，完成後自動推送報告",
        qr_items(("📊 選股", "/screen"), ("💼 庫存", "/portfolio"))
    )]


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

    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post("https://api.line.me/v2/bot/message/push",
                     json={"to": uid, "messages": [{"type": "text", "text": msg_text[:4800]}]},
                     headers=headers)


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
    """/daily — 今日完整決策報告（0~5 個操作建議）"""
    import asyncio
    asyncio.create_task(_daily_bg(uid))
    return [_text(
        "📋 今日決策報告產生中...\n\n"
        "整合動能掃描 → 三層分類 → 垃圾清洗 → 研究清單 → 持倉健康\n"
        "約需 5-15 秒，完成後自動推送",
        qr_items(("🔍 動能股", "/movers"), ("🛡 持倉健康", "/overlay"), ("💼 庫存", "/p")),
    )]


async def _daily_bg(uid: str) -> None:
    """背景執行決策引擎並推送"""
    try:
        from quant.decision_engine import DecisionEngine
        engine = DecisionEngine()
        daily  = await engine.run(uid)
        report = daily.format_line()
        headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
        async with __import__("httpx").AsyncClient(timeout=20) as c:
            await c.post(
                "https://api.line.me/v2/bot/message/push",
                json={"to": uid, "messages": [{"type": "text", "text": report[:4800]}]},
                headers=headers,
            )
    except Exception as e:
        logger.error("[daily_bg] %s", e)


async def _cmd_movers(uid: str) -> list:
    """/movers — 今日動能啟動股票"""
    try:
        from quant.movers_engine import MoversEngine
        engine  = MoversEngine()
        results = await engine.scan()
        if not results:
            results = engine.scan_mock()
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
        return [_text(report, qr_items(
            ("📋 決策報告", "/daily"),
            ("💼 庫存",     "/p"),
            ("🔍 動能股",   "/movers"),
        ))]
    except Exception as e:
        logger.error("[overlay] %s", e)
        return [_text(f"❌ 持倉健康檢查失敗：{e}",
                      qr_items(("💼 庫存", "/p")))]


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
            except Exception:
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

_BACKTEST_LABELS = {
    "momentum": "⚡ 動能策略",
    "value":    "💰 存股策略",
    "chip":     "🏛 籌碼策略",
    "breakout": "🚀 突破策略",
}

_BACKTEST_QR = qr_items(
    ("⚡ 動能策略", "/backtest momentum"),
    ("💰 存股策略", "/backtest value"),
    ("🏛 籌碼策略", "/backtest chip"),
    ("🚀 突破策略", "/backtest breakout"),
)


async def _cmd_backtest_menu(uid: str) -> list:
    """點「📈 回測」→ 顯示 4 策略 Quick Reply"""
    return [_text(
        "📈 策略回測\n\n選擇策略查看近3個月回測績效：\n"
        "（以台積電 2330 為代表股模擬）",
        _BACKTEST_QR,
    )]


async def _cmd_backtest_run(strategy: str, uid: str) -> list:
    """選擇策略 → ACK + 背景計算 + 推送結果"""
    if strategy not in _BACKTEST_LABELS:
        return [_text("❌ 未知策略", _BACKTEST_QR)]
    label = _BACKTEST_LABELS[strategy]
    import asyncio
    asyncio.create_task(_backtest_bg(strategy, label, uid))
    return [_text(
        f"📈 {label}回測計算中...\n\n期間：近3個月\n約需 5-10 秒，完成後自動推送結果",
        _BACKTEST_QR,
    )]


async def _backtest_bg(strategy: str, label: str, uid: str) -> None:
    """背景：執行回測 → 格式化 → push LINE"""
    import asyncio, httpx
    from datetime import date, timedelta
    from backend.models.database import settings as line_settings

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
        except Exception:
            df = _mock_kline_90d(stock_code)

        # ── 計算特徵 ─────────────────────────────────────────────────
        try:
            from quant.feature_engine import FeatureEngine
            feat_df = FeatureEngine(df).compute_all()
        except Exception:
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
        except Exception:
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
        headers = {"Authorization": f"Bearer {line_settings.line_channel_access_token}"}
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post(
                "https://api.line.me/v2/bot/message/push",
                json={"to": uid, "messages": [
                    {"type": "flex", "altText": f"{label}回測結果", "contents": flex_msg},
                ]},
                headers=headers,
            )
    except Exception as e:
        logger.error("[backtest_bg] %s", e)
        try:
            headers = {"Authorization": f"Bearer {line_settings.line_channel_access_token}"}
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": uid, "messages": [{"type": "text",
                        "text": f"❌ 回測計算失敗：{e}"}]},
                    headers=headers,
                )
        except Exception:
            pass


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
    """根據策略名稱產生 buy/sell/hold 訊號"""
    import numpy as np, pandas as pd
    n       = len(feat_df)
    signals = ["hold"] * n

    try:
        if strategy == "momentum":
            for i, row in feat_df.iterrows():
                ret5 = row.get("ret_5d", np.nan)
                if np.isnan(float(ret5 or 0)): continue
                if float(ret5) > 0.02:  signals[i] = "buy"
                elif float(ret5) < -0.02: signals[i] = "sell"

        elif strategy == "value":   # RSI 均值回歸（長線存股）
            for i, row in feat_df.iterrows():
                rsi = float(row.get("rsi14", np.nan) or 50)
                if np.isnan(rsi): continue
                if rsi < 30:  signals[i] = "buy"
                elif rsi > 72: signals[i] = "sell"

        elif strategy == "chip":    # MACD 黃金交叉
            for i, row in feat_df.iterrows():
                golden = row.get("macd_golden", 0)
                hist   = float(row.get("macd_hist", 0) or 0)
                if golden:           signals[i] = "buy"
                elif hist < -0.5:    signals[i] = "sell"

        elif strategy == "breakout":  # 布林突破
            for i, row in feat_df.iterrows():
                b = float(row.get("boll_b", 0.5) or 0.5)
                if np.isnan(b): continue
                if b < 0.05:    signals[i] = "buy"
                elif b > 0.95:  signals[i] = "sell"
    except Exception as e:
        logger.warning("[backtest] signal gen failed: %s", e)

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
                except Exception:
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
        except Exception:
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
        except Exception:
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

        return [_text(
            "\n".join(lines),
            qr_items(
                ("查看優化建議", "/risk_optimize"),
                ("📐 相關性",    "/correlation"),
                ("💼 庫存",      "/p"),
            ),
        )]

    except Exception as e:
        logger.error("[risk_report] %s", e)
        return [_text(f"❌ 風控分析失敗：{e}",
                      qr_items(("💼 庫存", "/p")))]


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

        headers = {"Authorization": f"Bearer {line_settings.line_channel_access_token}"}
        async with httpx.AsyncClient(timeout=30) as c:
            await c.post(
                "https://api.line.me/v2/bot/message/push",
                json={"to": uid, "messages": [{"type": "text", "text": msg[:4800]}]},
                headers=headers,
            )
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
    """/report [type] — 觸發後立即回 ACK，背景產生圖"""
    import asyncio
    from backend.services.report_screener import run_screener, paginate, get_label, ScreenerType

    label = get_label(screen_type, sector)
    asyncio.create_task(_report_bg(screen_type, sector, uid, page=1))

    qr = qr_items(
        ("動能", "/report momentum"), ("存股", "/report value"),
        ("籌碼", "/report chip"),     ("突破", "/report breakout"),
    )
    return [_text(f"📊 正在產生「{label}」選股表…\n5秒後自動推送（共最多20檔）\n\n"
                  f"/report next → 下一頁  /screen → 選單", qr)]


async def _cmd_report_page(uid: str, delta: int = 0, go_to: int = 0) -> list:
    """/report next 或 /report page N — 分頁翻頁"""
    import asyncio
    cache = _report_pages.get(uid)
    if not cache:
        return [_text("沒有進行中的選股，請先輸入 /report [類型]",
                      qr_items(("選單", "/screen")))]
    current  = cache["page"]
    total_p  = cache["total_pages"]
    next_p   = go_to if go_to > 0 else current + delta
    next_p   = max(1, min(next_p, total_p))
    if next_p == current and delta != 0:
        return [_text(f"已是最{'後' if delta > 0 else '前'}一頁（第 {current}/{total_p} 頁）",
                      qr_items(("重新選股", "/screen")))]
    asyncio.create_task(_report_bg(
        cache["screen_type"], cache.get("sector", ""),
        uid, page=next_p, cached_rows=cache["rows"],
    ))
    return [_text(f"正在載入第 {next_p}/{total_p} 頁…")]


async def _report_bg(
    screen_type: str, sector: str, uid: str,
    page: int = 1, cached_rows=None,
) -> None:
    """背景：篩選 → real-time 補充 → 分頁 → 產生圖 + 文字列表 → 推送"""
    import httpx
    from backend.services.report_screener import run_screener, paginate, get_label, enrich_with_realtime
    from backend.services.generate_report_image import generate_report_image
    from backend.services.report_tracker import batch_record

    try:
        if cached_rows is None:
            rows = run_screener(screen_type, sector=sector)
            logger.info(f"[report_bg] screener={screen_type} rows={len(rows)}")
            if rows:
                r0 = rows[0]
                logger.info(f"[report_bg] row0: {r0.stock_id} {r0.name} close={r0.close} chg={r0.change_pct} vol={r0.volume}")
            try:
                rows = await enrich_with_realtime(rows)
                logger.info(f"[report_bg] after enrich: rows={len(rows)} close0={rows[0].close if rows else 'N/A'}")
            except Exception as e:
                logger.warning(f"[report_bg] enrich failed (using pool data): {e}")
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
            except Exception:
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

        async with httpx.AsyncClient(timeout=20) as c:
            await c.post("https://api.line.me/v2/bot/message/push",
                         json={"to": uid, "messages": push_msgs[:5]},
                         headers={"Authorization": f"Bearer {settings.line_channel_access_token}"})
    except Exception as e:
        logger.error(f"[report_bg] {screen_type} uid={uid[:8]} err={e}", exc_info=True)


def _build_text_fallback(rows, label: str, page: int, total: int) -> dict:
    lines = [f"📊 {label} (第{page}/{total}頁)", "─" * 20]
    for r in rows[:10]:
        s = "+" if r.change_pct > 0 else ""
        lines.append(f"{r.stock_id} {r.name} {s}{r.change_pct:.2f}%  分:{r.model_score:.0f}")
    return {"type": "text", "text": "\n".join(lines)[:4800]}


def _build_stock_list_msg(
    rows: list, label: str, page: int, total_pages: int, screen_type: str,
) -> dict:
    """文字股票列表 + 翻頁 + 個股分析 Quick Reply 按鈕"""
    lines = [f"📊 {label}  第{page}/{total_pages}頁", "─" * 18]
    for i, r in enumerate(rows[:10], 1):
        arrow = "▲" if r.change_pct > 0 else ("▼" if r.change_pct < 0 else "─")
        sign  = "+" if r.change_pct > 0 else ""
        close_str = f"{r.close:,.1f}" if r.close > 0 else "--"
        lines.append(
            f"#{i} {r.stock_id} {r.name}\n"
            f"   {close_str}元  AI{r.confidence:.0f}分  {arrow}{sign}{r.change_pct:.1f}%"
        )

    qr_list = []
    if page > 1:
        qr_list.append({
            "type": "action",
            "action": {"type": "postback", "label": "⬅️ 上一頁",
                       "data": "act=report_prev", "displayText": "上一頁"},
        })
    if page < total_pages:
        qr_list.append({
            "type": "action",
            "action": {"type": "postback", "label": "➡️ 下一頁",
                       "data": "act=report_next", "displayText": "下一頁"},
        })
    qr_list.append({
        "type": "action",
        "action": {"type": "postback", "label": "🔄 換策略",
                   "data": "act=screener_qr", "displayText": "換策略"},
    })
    qr_list.append({
        "type": "action",
        "action": {"type": "postback", "label": "🏠 主選單",
                   "data": "act=market_card", "displayText": "主選單"},
    })
    for r in rows[:4]:
        qr_list.append({
            "type": "action",
            "action": {
                "type": "postback",
                "label": f"🔍{r.stock_id}",
                "data": f"act=recommend_detail&code={r.stock_id}",
                "displayText": f"分析 {r.stock_id}",
            },
        })

    return {
        "type": "text",
        "text": "\n".join(lines)[:4800],
        "quickReply": {"items": qr_list[:13]},
    }


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
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post("https://api.line.me/v2/bot/message/push",
                         json={"to": uid, "messages": [msg]},
                         headers={"Authorization": f"Bearer {settings.line_channel_access_token}"})
    except Exception as e:
        logger.error(f"[custom_bg] err={e}")


async def _cmd_save_fav(code: str, uid: str) -> list:
    """/save [code]"""
    from backend.services.stock_favorites import save_favorite
    try:
        q = await fetch_realtime_quote(code)
        name = q.get("name", code) if q else code
    except Exception:
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
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post("https://api.line.me/v2/bot/message/push",
                         json={"to": uid, "messages": [msg]},
                         headers={"Authorization": f"Bearer {settings.line_channel_access_token}"})
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
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post("https://api.line.me/v2/bot/message/push",
                         json={"to": uid, "messages": [msg]},
                         headers={"Authorization": f"Bearer {settings.line_channel_access_token}"})
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
        headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
        payload = {
            "to": uid,
            "messages": [
                {
                    "type": "image",
                    "originalContentUrl": image_url,
                    "previewImageUrl": image_url,
                }
            ],
        }
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://api.line.me/v2/bot/message/push",
                             json=payload, headers=headers)
            logger.info(f"[Report] push image to {uid[:8]}: {r.status_code}")
    except Exception as e:
        logger.error(f"[Report] bg push failed: {e}")
        await _push_text_summary(group, uid)


async def _push_text_summary(group: str, uid: str) -> None:
    """無公開 URL 時，改推文字摘要"""
    import httpx
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
    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    payload = {"to": uid, "messages": [{"type": "text", "text": text[:5000]}]}
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post("https://api.line.me/v2/bot/message/push",
                     json=payload, headers=headers)


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
    except Exception:
        from quant.strategy_engine import StrategyEngine, MOCK_STOCKS
        sigs = StrategyEngine().batch_evaluate(MOCK_STOCKS, regime=regime, min_confidence=60)
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
        except Exception:
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
            except Exception:
                p = 0.0
            if p <= 0:
                m = next((x for x in MOCK_STOCKS if x["stock_id"] == sig.stock_id), None)
                p = m.get("close", 100) if m else 100.0
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
                return s
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

    result = StrategyEngine().compare(_data(code_a), _data(code_b))
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
    if not data:
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
