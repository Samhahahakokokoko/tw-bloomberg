"""LINE Bot Webhook — 多用戶 · Flex · Quick Reply · Postback · 策略推薦"""
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
    params = dict(urllib.parse.parse_qsl(data))
    act  = params.get("act", "")
    hid  = int(params.get("id", 0))
    code = params.get("code", "")

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

    return [_text("未知操作", qr_items(("💼 庫存", "/portfolio")))]


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
    if cmd == "/ai"       and len(parts) >= 2:  return [await _cmd_ai_ask(" ".join(parts[1:]), uid)]
    if cmd in ("/news", "/news_guide"):         return [_news_guide()]
    if cmd == "/ai_guide":                      return [_ai_guide()]
    if cmd == "/help":                          return [_text(_help_text(), _home_qr())]

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

    # ── 句中包含 4 碼數字 → 嘗試查報價 ─────────────────────────────────────
    import re
    codes = re.findall(r'\b\d{4,6}\b', t)
    if codes:
        return await _cmd_quote(codes[0])

    # ── 其餘長句 → 丟給 AI ───────────────────────────────────────────────────
    # 若文字夠長且像是問句，直接問 AI
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
    ov = await fetch_market_overview()
    if not ov:
        return [_text("❌ 無法取得大盤資訊", _home_qr())]
    arr = "▲" if ov["change"] >= 0 else "▼"
    return [_text(
        f"📊 加權指數\n{ov['value']:,.2f}\n{arr}{abs(ov['change']):.2f} ({ov['change_pct']:+.2f}%)",
        qr_items(("💼 庫存", "/portfolio"), ("🤖 AI", "/ai 今日大盤氣氛"), ("📰 新聞", "/news_guide"))
    )]


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
        "完整版請前往 Web 界面",
        qr_items(("🤖 AI簡評", "/ai 今日台股氣氛"), ("📊 大盤", "/market"))
    )


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
        ("📊 大盤", "/market"), ("💼 庫存", "/portfolio"),
        ("🤖 AI",  "/ai_guide"), ("📋 推薦", "/rec"),
    )


def _help_text() -> str:
    return (
        "📋 指令說明\n"
        "─────────────\n"
        "輸入 4 碼代碼  即時報價\n"
        "/portfolio     我的庫存（互動卡片）\n"
        "/buy 代碼 股數 成本\n"
        "/setcost ID 新成本\n"
        "/alert 代碼 類型 數值\n"
        "/rec           策略推薦（含回測）\n"
        "/ai_portfolio  AI庫存分析\n"
        "/morning       今日早報\n"
        "/week          本週週報\n"
        "/pe /dividend /margin\n"
        "/subscribe     訂閱自動推播"
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
