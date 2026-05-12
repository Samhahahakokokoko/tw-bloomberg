"""Flex Message 模板 — 報價卡片、庫存表格、早報排版"""
from typing import Optional


# ── 通用色彩 ─────────────────────────────────────────────────────────────────
C_BG      = "#0f1629"
C_SURFACE = "#162040"
C_BORDER  = "#1e3a5f"
C_ACCENT  = "#00d4ff"
C_GREEN   = "#00e676"
C_RED     = "#ff5252"
C_YELLOW  = "#ffd740"
C_WHITE   = "#e0f0ff"
C_MUTED   = "#7090b0"


# ── Quick Reply 組件 ──────────────────────────────────────────────────────────

def qr_items(*items: tuple[str, str]) -> dict:
    """建立 Quick Reply，items = [(label, text), ...]"""
    return {
        "items": [
            {
                "type": "action",
                "action": {"type": "message", "label": label[:20], "text": text},
            }
            for label, text in items[:13]   # LINE 上限 13 個
        ]
    }


def quick_reply_quote(code: str, price: float) -> dict:
    return qr_items(
        ("➕ 加入庫存", f"/buy {code} 1000 {price:.0f}"),
        ("🔔 設警報",   f"/alert {code} price_above {price*1.05:.0f}"),
        ("🤖 AI分析",   f"/ai {code} 目前值得買進嗎？"),
        ("📊 K線",      f"/kline {code}"),
        ("🏛 法人",     f"/inst {code}"),
    )


def quick_reply_portfolio() -> dict:
    return qr_items(
        ("🤖 AI建議",  "/ai_portfolio"),
        ("🛡️ 風控分析", "/risk"),
        ("📋 週報",    "/week"),
        ("🔔 新增警報", "/alert_guide"),
        ("📊 大盤",    "/market"),
    )


def quick_reply_after_alert(code: str) -> dict:
    return qr_items(
        ("📈 查報價", f"/quote {code}"),
        ("💼 看庫存", "/portfolio"),
        ("🔕 刪警報", "/alert_list"),
    )


# ── 報價 Flex Card ─────────────────────────────────────────────────────────────

def flex_quote(q: dict) -> dict:
    code    = q.get("code", "")
    name    = q.get("name", code)
    price   = q.get("price", 0)
    change  = q.get("change", 0)
    pct     = q.get("change_pct", 0)
    high    = q.get("high", 0)
    low     = q.get("low", 0)
    volume  = q.get("volume", 0)
    open_p  = q.get("open", 0)

    is_up   = change >= 0
    chg_clr = C_GREEN if is_up else C_RED
    arrow   = "▲" if is_up else "▼"
    pct_str = f"{pct:+.2f}%"

    return {
        "type": "bubble",
        "size": "kilo",
        "styles": {
            "header": {"backgroundColor": C_BG},
            "body":   {"backgroundColor": C_BG},
            "footer": {"backgroundColor": C_SURFACE},
        },
        "header": {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "box",
                    "layout": "vertical",
                    "flex": 1,
                    "contents": [
                        {"type": "text", "text": name, "size": "xl",
                         "weight": "bold", "color": C_WHITE},
                        {"type": "text", "text": code, "size": "sm", "color": C_ACCENT},
                    ],
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "alignItems": "flex-end",
                    "contents": [
                        {"type": "text", "text": f"{price:,.2f}",
                         "size": "xxl", "weight": "bold", "color": chg_clr},
                        {"type": "text", "text": f"{arrow} {abs(change):.2f}  {pct_str}",
                         "size": "sm", "color": chg_clr},
                    ],
                },
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "spacing": "sm",
            "contents": [
                _separator(),
                _row_4cols("開盤", f"{open_p}", "最高", f"{high}",
                           C_WHITE, C_GREEN),
                _row_4cols("成交量", f"{volume:,}張", "最低", f"{low}",
                           C_WHITE, C_RED),
                _separator(),
            ],
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "12px",
            "spacing": "sm",
            "contents": [
                _footer_btn("加入庫存", f"/buy {code} 1000 {price:.0f}", C_ACCENT),
                _footer_btn("設警報",   f"/alert {code} price_above {price*1.05:.0f}", C_YELLOW),
                _footer_btn("AI分析",   f"/ai {code} 值得買嗎", "#8866ff"),
            ],
        },
    }


# ── 庫存 Flex Table ───────────────────────────────────────────────────────────

def flex_portfolio(holdings: list[dict]) -> dict:
    total_mv  = sum(h["market_value"] for h in holdings)
    total_pnl = sum(h["pnl"] for h in holdings)
    total_pct = total_pnl / (total_mv - total_pnl) * 100 if (total_mv - total_pnl) else 0

    rows = []
    for h in holdings:
        is_up = h["pnl"] >= 0
        clr   = C_GREEN if is_up else C_RED
        rows.append({
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "8px",
            "borderWidth": "1px",
            "borderColor": C_BORDER,
            "contents": [
                {
                    "type": "box", "layout": "vertical", "flex": 2,
                    "contents": [
                        {"type": "text", "text": h["stock_code"],
                         "size": "sm", "weight": "bold", "color": C_ACCENT},
                        {"type": "text", "text": h.get("stock_name", ""),
                         "size": "xxs", "color": C_MUTED},
                    ],
                },
                {
                    "type": "box", "layout": "vertical", "flex": 2,
                    "alignItems": "center",
                    "contents": [
                        {"type": "text", "text": f"{h['current_price']:,.0f}",
                         "size": "sm", "color": C_WHITE},
                        {"type": "text", "text": f"{h['shares']:,}股",
                         "size": "xxs", "color": C_MUTED},
                    ],
                },
                {
                    "type": "box", "layout": "vertical", "flex": 3,
                    "alignItems": "flex-end",
                    "contents": [
                        {"type": "text", "text": f"{h['pnl']:+,.0f}",
                         "size": "sm", "weight": "bold", "color": clr},
                        {"type": "text", "text": f"{h['pnl_pct']:+.1f}%",
                         "size": "xxs", "color": clr},
                    ],
                },
            ],
        })

    summary_clr = C_GREEN if total_pnl >= 0 else C_RED
    return {
        "type": "bubble",
        "size": "mega",
        "styles": {
            "header": {"backgroundColor": C_BG},
            "body":   {"backgroundColor": C_BG},
            "footer": {"backgroundColor": C_SURFACE},
        },
        "header": {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "我的庫存", "size": "xl",
                 "weight": "bold", "color": C_WHITE, "flex": 1},
                {
                    "type": "box", "layout": "vertical", "alignItems": "flex-end",
                    "contents": [
                        {"type": "text", "text": f"{total_mv:,.0f}",
                         "size": "lg", "weight": "bold", "color": C_WHITE},
                        {"type": "text", "text": f"損益 {total_pnl:+,.0f}  ({total_pct:+.1f}%)",
                         "size": "sm", "color": summary_clr},
                    ],
                },
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "12px",
            "spacing": "none",
            "contents": [
                {
                    "type": "box",
                    "layout": "horizontal",
                    "paddingBottom": "6px",
                    "contents": [
                        {"type": "text", "text": "代碼/名稱", "flex": 2,
                         "size": "xxs", "color": C_MUTED},
                        {"type": "text", "text": "現價/股數", "flex": 2,
                         "size": "xxs", "color": C_MUTED, "align": "center"},
                        {"type": "text", "text": "損益",     "flex": 3,
                         "size": "xxs", "color": C_MUTED, "align": "end"},
                    ],
                },
                *rows,
            ],
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "12px",
            "spacing": "sm",
            "contents": [
                _footer_btn("AI 分析", "/ai_portfolio", C_ACCENT),
                _footer_btn("週報",    "/week",         C_YELLOW),
            ],
        },
    }


# ── 早報 Flex ─────────────────────────────────────────────────────────────────

def flex_morning_report(report_text: str, overview: dict) -> dict:
    value     = overview.get("value", 0)
    change    = overview.get("change", 0)
    pct       = overview.get("change_pct", 0)
    is_up     = change >= 0
    mkt_clr   = C_GREEN if is_up else C_RED
    arrow     = "▲" if is_up else "▼"

    # 拆出各段落
    sections  = _parse_report_sections(report_text)

    body_items = [
        # 大盤指數 Hero
        {
            "type": "box",
            "layout": "horizontal",
            "backgroundColor": C_SURFACE,
            "paddingAll": "14px",
            "cornerRadius": "8px",
            "contents": [
                {
                    "type": "box", "layout": "vertical", "flex": 1,
                    "contents": [
                        {"type": "text", "text": "加權指數 TAIEX",
                         "size": "sm", "color": C_MUTED},
                        {"type": "text", "text": f"{value:,.2f}",
                         "size": "xxl", "weight": "bold", "color": C_WHITE},
                    ],
                },
                {
                    "type": "box", "layout": "vertical", "alignItems": "flex-end",
                    "contents": [
                        {"type": "text", "text": f"{arrow} {abs(change):.2f}",
                         "size": "xl", "weight": "bold", "color": mkt_clr},
                        {"type": "text", "text": f"{pct:+.2f}%",
                         "size": "md", "color": mkt_clr},
                    ],
                },
            ],
        },
        {"type": "separator", "margin": "md", "color": C_BORDER},
    ]

    # 加入各段落
    for title, content in sections.items():
        if not content:
            continue
        body_items.append({
            "type": "box", "layout": "vertical",
            "paddingTop": "10px", "paddingBottom": "4px",
            "contents": [
                {"type": "text", "text": title, "size": "sm",
                 "weight": "bold", "color": C_ACCENT},
                {"type": "text", "text": content, "size": "sm",
                 "color": C_WHITE, "wrap": True, "maxLines": 6},
            ],
        })
        body_items.append({"type": "separator", "margin": "sm", "color": C_BORDER})

    return {
        "type": "bubble",
        "size": "mega",
        "styles": {
            "header": {"backgroundColor": C_BG},
            "body":   {"backgroundColor": C_BG},
            "footer": {"backgroundColor": C_SURFACE},
        },
        "header": {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "box", "layout": "vertical", "flex": 1,
                    "contents": [
                        {"type": "text", "text": "台股早報",
                         "size": "xl", "weight": "bold", "color": C_WHITE},
                        {"type": "text", "text": "TW Bloomberg Terminal",
                         "size": "xs", "color": C_ACCENT},
                    ],
                },
                {"type": "text", "text": _today_str(),
                 "size": "sm", "color": C_MUTED, "align": "end"},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "14px",
            "spacing": "sm",
            "contents": body_items,
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "12px",
            "spacing": "sm",
            "contents": [
                _footer_btn("我的庫存",  "/portfolio",    C_ACCENT),
                _footer_btn("AI 分析",   "/ai_guide",     "#8866ff"),
                _footer_btn("更多功能",  "/help",         C_MUTED),
            ],
        },
    }


# ── Alert Flex ────────────────────────────────────────────────────────────────

def flex_alert_triggered(code: str, name: str, trigger_msg: str, price: float) -> dict:
    return {
        "type": "bubble",
        "size": "kilo",
        "styles": {
            "header": {"backgroundColor": "#2a0a0a"},
            "body":   {"backgroundColor": C_BG},
        },
        "header": {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "⚠️  警報觸發", "size": "lg",
                 "weight": "bold", "color": C_RED},
                {"type": "text", "text": f"{code} {name}", "size": "sm",
                 "color": C_MUTED, "align": "end"},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": trigger_msg, "size": "md",
                 "color": C_WHITE, "wrap": True},
                {"type": "separator", "margin": "md", "color": C_BORDER},
                _row_2cols("目前價格", f"{price:,.2f}", C_WHITE),
            ],
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "12px",
            "spacing": "sm",
            "contents": [
                _footer_btn("查看報價",  f"/quote {code}", C_ACCENT),
                _footer_btn("AI 建議",   f"/ai {code} 是否該操作？", "#8866ff"),
            ],
        },
    }


# ── 輔助函式 ──────────────────────────────────────────────────────────────────

def _separator() -> dict:
    return {"type": "separator", "margin": "sm", "color": C_BORDER}


def _row_4cols(l1, v1, l2, v2, c1=None, c2=None) -> dict:
    return {
        "type": "box", "layout": "horizontal", "paddingAll": "6px",
        "contents": [
            {"type": "text", "text": l1, "flex": 1, "size": "sm", "color": C_MUTED},
            {"type": "text", "text": str(v1), "flex": 1, "size": "sm",
             "color": c1 or C_WHITE, "align": "center"},
            {"type": "text", "text": l2, "flex": 1, "size": "sm",
             "color": C_MUTED, "align": "center"},
            {"type": "text", "text": str(v2), "flex": 1, "size": "sm",
             "color": c2 or C_WHITE, "align": "end"},
        ],
    }


def _row_2cols(label, value, color=None) -> dict:
    return {
        "type": "box", "layout": "horizontal", "paddingAll": "6px",
        "contents": [
            {"type": "text", "text": label, "size": "sm", "color": C_MUTED, "flex": 1},
            {"type": "text", "text": str(value), "size": "sm",
             "color": color or C_WHITE, "align": "end", "flex": 2},
        ],
    }


def _footer_btn(label: str, text: str, color: str) -> dict:
    return {
        "type": "button",
        "action": {"type": "message", "label": label[:20], "text": text},
        "style": "primary",
        "color": color,
        "height": "sm",
        "flex": 1,
    }


def _today_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%m/%d  %H:%M")


# ── Postback 按鈕輔助 ─────────────────────────────────────────────────────────

def _postback_btn(label: str, data: str, color: str, style: str = "primary") -> dict:
    return {
        "type": "button",
        "action": {"type": "postback", "label": label[:20], "data": data},
        "style": style,
        "color": color,
        "height": "sm",
        "flex": 1,
    }


# ── 互動式單股庫存卡片（Postback 操作）────────────────────────────────────────

def flex_holding_card(h: dict) -> dict:
    """單筆持股的互動卡片，含 +100/-100/修改成本/刪除 按鈕"""
    is_up  = h["pnl"] >= 0
    clr    = C_GREEN if is_up else C_RED
    arrow  = "▲" if is_up else "▼"
    hid    = h["id"]
    code   = h["stock_code"]
    w_pct  = h.get("weight_pct", 0)

    return {
        "type": "bubble",
        "size": "kilo",
        "styles": {
            "header": {"backgroundColor": C_BG},
            "body":   {"backgroundColor": C_BG},
            "footer": {"backgroundColor": C_SURFACE},
        },
        "header": {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "14px",
            "contents": [
                {
                    "type": "box", "layout": "vertical", "flex": 1,
                    "contents": [
                        {"type": "text", "text": h.get("stock_name", code),
                         "size": "lg", "weight": "bold", "color": C_WHITE},
                        {"type": "text", "text": code,
                         "size": "xs", "color": C_ACCENT},
                    ],
                },
                {
                    "type": "box", "layout": "vertical", "alignItems": "flex-end",
                    "contents": [
                        {"type": "text", "text": f"{h['current_price']:,.0f}",
                         "size": "xl", "weight": "bold", "color": clr},
                        {"type": "text", "text": f"{arrow}{abs(h['pnl_pct']):.1f}%",
                         "size": "sm", "color": clr},
                    ],
                },
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "14px",
            "spacing": "sm",
            "contents": [
                # 損益 Hero
                {
                    "type": "box",
                    "layout": "horizontal",
                    "backgroundColor": C_SURFACE,
                    "paddingAll": "12px",
                    "cornerRadius": "8px",
                    "contents": [
                        {
                            "type": "box", "layout": "vertical", "flex": 1,
                            "contents": [
                                {"type": "text", "text": "未實現損益",
                                 "size": "xxs", "color": C_MUTED},
                                {"type": "text",
                                 "text": f"{arrow} {abs(h['pnl']):,.0f}",
                                 "size": "xl", "weight": "bold", "color": clr},
                            ],
                        },
                        {
                            "type": "box", "layout": "vertical",
                            "alignItems": "flex-end",
                            "contents": [
                                {"type": "text", "text": "市值",
                                 "size": "xxs", "color": C_MUTED},
                                {"type": "text",
                                 "text": f"{h['market_value']:,.0f}",
                                 "size": "md", "color": C_WHITE},
                            ],
                        },
                    ],
                },
                _separator(),
                _row_4cols("持股數", f"{h['shares']:,}股",
                           "成本價", f"{h['cost_price']:,.1f}"),
                _row_4cols("倉位佔比", f"{w_pct:.1f}%",
                           "總成本",  f"{h['cost_price']*h['shares']:,.0f}"),
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "10px",
            "spacing": "sm",
            "contents": [
                # 股數增減
                {
                    "type": "box", "layout": "horizontal", "spacing": "sm",
                    "contents": [
                        _postback_btn("+100股", f"act=add&id={hid}&delta=100&code={code}", C_GREEN),
                        _postback_btn("-100股", f"act=sub&id={hid}&delta=100&code={code}", C_RED),
                        _postback_btn("+1000股",f"act=add&id={hid}&delta=1000&code={code}", "#228844"),
                    ],
                },
                # 管理操作
                {
                    "type": "box", "layout": "horizontal", "spacing": "sm",
                    "contents": [
                        _postback_btn("✏️修改成本", f"act=editcost&id={hid}&code={code}", C_YELLOW),
                        _postback_btn("🤖AI分析",  f"act=ai&id={hid}&code={code}", "#8866ff"),
                        _postback_btn("🗑️刪除",   f"act=del&id={hid}&code={code}", "#884422"),
                    ],
                },
            ],
        },
    }


def flex_portfolio_carousel(holdings: list[dict]) -> dict:
    """庫存 Carousel — 每筆持股一張互動卡片 + 總覽卡片"""
    total_mv  = sum(h["market_value"] for h in holdings)
    total_pnl = sum(h["pnl"] for h in holdings)
    total_cost = total_mv - total_pnl
    pnl_pct   = total_pnl / total_cost * 100 if total_cost else 0

    # 加入權重百分比
    enriched = [
        {**h, "weight_pct": h["market_value"] / total_mv * 100 if total_mv else 0}
        for h in holdings
    ]

    # 總覽卡片
    summary_clr = C_GREEN if total_pnl >= 0 else C_RED
    summary_card = {
        "type": "bubble",
        "size": "kilo",
        "styles": {
            "header": {"backgroundColor": "#0a1428"},
            "body":   {"backgroundColor": C_BG},
            "footer": {"backgroundColor": C_SURFACE},
        },
        "header": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "庫存總覽",
                 "size": "xl", "weight": "bold", "color": C_WHITE},
                {"type": "text", "text": f"共 {len(holdings)} 檔持股",
                 "size": "xs", "color": C_MUTED},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "14px",
            "spacing": "sm",
            "contents": [
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": C_SURFACE,
                    "paddingAll": "14px",
                    "cornerRadius": "10px",
                    "contents": [
                        {"type": "text", "text": "總市值",
                         "size": "sm", "color": C_MUTED},
                        {"type": "text", "text": f"{total_mv:,.0f}",
                         "size": "xxl", "weight": "bold", "color": C_WHITE},
                        {"type": "text",
                         "text": f"{'▲' if total_pnl>=0 else '▼'} {abs(total_pnl):,.0f}  ({pnl_pct:+.1f}%)",
                         "size": "md", "color": summary_clr, "margin": "sm"},
                    ],
                },
                _separator(),
                # 持股列表摘要
                *[
                    {
                        "type": "box", "layout": "horizontal",
                        "paddingAll": "4px",
                        "contents": [
                            {"type": "text", "text": h["stock_code"],
                             "flex": 1, "size": "sm", "color": C_ACCENT},
                            {"type": "text", "text": h.get("stock_name", ""),
                             "flex": 2, "size": "sm", "color": C_WHITE},
                            {"type": "text",
                             "text": f"{h['pnl_pct']:+.1f}%",
                             "flex": 1, "size": "sm", "align": "end",
                             "color": C_GREEN if h["pnl"] >= 0 else C_RED},
                        ],
                    }
                    for h in enriched[:6]
                ],
            ],
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "10px",
            "spacing": "sm",
            "contents": [
                _footer_btn("AI分析",  "/ai_portfolio", C_ACCENT),
                _footer_btn("策略推薦", "/rec",          "#8866ff"),
                _footer_btn("週報",    "/week",         C_YELLOW),
            ],
        },
    }

    bubbles = [summary_card] + [flex_holding_card(h) for h in enriched[:9]]
    return {"type": "carousel", "contents": bubbles}


# ── 策略推薦 Carousel ─────────────────────────────────────────────────────────

def flex_strategy_card(rec: dict) -> dict:
    """單股策略推薦卡片"""
    code     = rec["stock_code"]
    name     = rec.get("stock_name", code)
    strategy = rec["strategy"]
    reason   = rec["reason"]
    bt       = rec.get("backtest", {})
    ret      = bt.get("total_return", 0)
    wr       = bt.get("win_rate", 0)
    dd       = bt.get("max_drawdown", 0)
    sharpe   = bt.get("sharpe_ratio", 0)
    ret_clr  = C_GREEN if ret >= 0 else C_RED

    STRATEGY_LABELS = {
        "ma_cross":     "MA 均線交叉",
        "rsi":          "RSI 超買超賣",
        "macd":         "MACD 策略",
        "kd":           "KD 指標",
        "bollinger":    "布林通道",
        "pvd":          "價量背離",
        "institutional":"外資籌碼面",
    }
    strategy_label = STRATEGY_LABELS.get(strategy, strategy)

    return {
        "type": "bubble",
        "size": "kilo",
        "styles": {
            "header": {"backgroundColor": "#0d1a33"},
            "body":   {"backgroundColor": C_BG},
            "footer": {"backgroundColor": C_SURFACE},
        },
        "header": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "14px",
            "contents": [
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": f"{name} ({code})",
                         "size": "md", "weight": "bold", "color": C_WHITE, "flex": 1},
                        {"type": "text", "text": "推薦策略",
                         "size": "xxs", "color": C_ACCENT, "align": "end"},
                    ],
                },
                {"type": "text", "text": strategy_label,
                 "size": "xxl", "weight": "bold", "color": C_ACCENT,
                 "margin": "sm"},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "14px",
            "spacing": "sm",
            "contents": [
                # 推薦原因
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": C_SURFACE,
                    "paddingAll": "10px",
                    "cornerRadius": "8px",
                    "contents": [
                        {"type": "text", "text": "推薦原因",
                         "size": "xxs", "color": C_MUTED},
                        {"type": "text", "text": reason,
                         "size": "sm", "color": C_WHITE, "wrap": True},
                    ],
                },
                _separator(),
                # 回測數據
                {"type": "text", "text": "近期回測數據",
                 "size": "xs", "color": C_MUTED, "margin": "sm"},
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        _stat_box("總報酬", f"{ret:+.1f}%", ret_clr),
                        _stat_box("勝率",   f"{wr:.0f}%",  C_WHITE),
                        _stat_box("最大回撤",f"{dd:.1f}%", C_RED),
                        _stat_box("夏普",   f"{sharpe:.2f}", C_YELLOW),
                    ],
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "10px",
            "spacing": "sm",
            "contents": [
                _postback_btn(
                    "套用策略警報",
                    f"act=applyrec&code={code}&strategy={strategy}",
                    C_ACCENT,
                ),
                _footer_btn("查看報價", f"/quote {code}", C_MUTED),
            ],
        },
    }


def flex_rec_carousel(recs: list[dict]) -> dict:
    """策略推薦 Carousel"""
    if not recs:
        return {"type": "bubble", "body": {"type": "box", "layout": "vertical",
                "contents": [{"type": "text", "text": "無推薦資料", "color": C_MUTED}]}}
    return {"type": "carousel", "contents": [flex_strategy_card(r) for r in recs[:10]]}


def _stat_box(label: str, value: str, color: str) -> dict:
    return {
        "type": "box",
        "layout": "vertical",
        "flex": 1,
        "alignItems": "center",
        "contents": [
            {"type": "text", "text": value, "size": "sm",
             "weight": "bold", "color": color, "align": "center"},
            {"type": "text", "text": label, "size": "xxs",
             "color": C_MUTED, "align": "center"},
        ],
    }


# ── 用戶設定 Flex ────────────────────────────────────────────────────────────

def flex_profile_setup(profile) -> dict:
    """用戶投資風格設定卡片"""
    from backend.services.user_profile_service import RISK_PROFILES, INVESTMENT_GOALS
    risk    = RISK_PROFILES.get(profile.risk_tolerance, RISK_PROFILES["moderate"])
    goal    = INVESTMENT_GOALS.get(profile.investment_goal, INVESTMENT_GOALS["growth"])
    inds    = profile.preferred_industries or "尚未設定"

    return {
        "type": "bubble",
        "size": "mega",
        "styles": {
            "header": {"backgroundColor": "#0d1a33"},
            "body":   {"backgroundColor": C_BG},
            "footer": {"backgroundColor": C_SURFACE},
        },
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "投資風格設定",
                 "size": "xl", "weight": "bold", "color": C_WHITE},
                {"type": "text", "text": "個人化 AI 分析的依據",
                 "size": "xs", "color": C_MUTED},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "14px", "spacing": "md",
            "contents": [
                # 目前設定
                {
                    "type": "box", "layout": "vertical",
                    "backgroundColor": C_SURFACE, "paddingAll": "12px",
                    "cornerRadius": "8px",
                    "contents": [
                        _row_2cols("風險偏好", f"{risk['emoji']} {risk['label']}", risk["color"]),
                        _row_2cols("投資目標", f"{goal['emoji']} {goal['label']}", C_WHITE),
                        _row_2cols("偏好產業", inds[:30], C_MUTED),
                    ],
                },
                {"type": "separator", "color": C_BORDER},
                # 風險選擇
                {"type": "text", "text": "選擇風險偏好",
                 "size": "sm", "weight": "bold", "color": C_ACCENT},
                {
                    "type": "box", "layout": "horizontal", "spacing": "sm",
                    "contents": [
                        _postback_btn("🛡️保守", "act=profile&field=risk&val=conservative", "#4488ff"),
                        _postback_btn("⚖️穩健", "act=profile&field=risk&val=moderate",     "#44cc88"),
                        _postback_btn("🚀積極", "act=profile&field=risk&val=aggressive",    "#ff5544"),
                    ],
                },
                # 投資目標
                {"type": "text", "text": "選擇投資目標",
                 "size": "sm", "weight": "bold", "color": C_ACCENT},
                {
                    "type": "box", "layout": "horizontal", "spacing": "sm",
                    "contents": [
                        _postback_btn("💰存股收息", "act=profile&field=goal&val=income",      "#ffa040"),
                        _postback_btn("📈資本成長", "act=profile&field=goal&val=growth",      C_GREEN),
                        _postback_btn("⚡波段操作", "act=profile&field=goal&val=speculation", C_RED),
                    ],
                },
            ],
        },
        "footer": {
            "type": "box", "layout": "horizontal", "paddingAll": "10px", "spacing": "sm",
            "contents": [
                _footer_btn("查看庫存",  "/portfolio",    C_ACCENT),
                _footer_btn("策略推薦",  "/rec",          "#8866ff"),
            ],
        },
    }


# ── 大盤行情 Flex Card（重新設計版）─────────────────────────────────────────

def flex_market_card(ov: dict, inst: dict = None, sectors: list = None) -> dict:
    """
    大盤行情完整卡片：指數 + 法人 + 族群熱度 Top3
    ov: fetch_market_overview 回傳
    inst: {foreign_net, trust_net, dealer_net} 法人合計（億）
    sectors: list of (name, score) 族群熱度
    """
    value   = ov.get("value", 0)
    change  = ov.get("change", 0)
    pct     = ov.get("change_pct", 0)
    volume  = ov.get("volume", 0)
    is_up   = change >= 0
    mkt_clr = C_GREEN if is_up else C_RED
    arrow   = "▲" if is_up else "▼"
    state   = "多頭 🟢" if pct > 0.5 else ("空頭 🔴" if pct < -0.5 else "盤整 🟡")

    body_contents = [
        # 指數主展示
        {
            "type": "box", "layout": "vertical",
            "backgroundColor": C_SURFACE, "paddingAll": "14px", "cornerRadius": "10px",
            "contents": [
                {"type": "text", "text": "加權指數  TAIEX",
                 "size": "xs", "color": C_MUTED},
                {"type": "text", "text": f"{value:,.2f}",
                 "size": "3xl", "weight": "bold", "color": C_WHITE},
                {
                    "type": "box", "layout": "horizontal", "marginTop": "4px",
                    "contents": [
                        {"type": "text",
                         "text": f"{arrow} {abs(change):.2f}點  ({pct:+.2f}%)",
                         "size": "sm", "color": mkt_clr, "flex": 1},
                        {"type": "text", "text": state,
                         "size": "sm", "color": mkt_clr, "align": "end"},
                    ],
                },
                {"type": "text",
                 "text": f"成交量  {volume/100_000_000:.0f} 億元" if volume > 0 else "",
                 "size": "xs", "color": C_MUTED, "margin": "sm"},
            ],
        },
    ]

    # 三大法人
    if inst:
        f_net = inst.get("foreign_net", 0)
        t_net = inst.get("trust_net",   0)
        d_net = inst.get("dealer_net",  0)

        def _inst_row(label, val):
            c   = C_GREEN if val >= 0 else C_RED
            s   = "+" if val >= 0 else ""
            txt = f"{s}{val/100_000_000:.1f}億" if abs(val) >= 1_000_000 else f"{s}{val:,.0f}"
            return {
                "type": "box", "layout": "horizontal", "paddingAll": "4px",
                "contents": [
                    {"type": "text", "text": label,  "size": "sm", "color": C_MUTED, "flex": 1},
                    {"type": "text", "text": txt, "size": "sm", "color": c,
                     "align": "end", "flex": 2, "weight": "bold"},
                ],
            }

        body_contents += [
            {"type": "separator", "margin": "md", "color": C_BORDER},
            {"type": "text", "text": "三大法人買賣超",
             "size": "xs", "color": C_MUTED, "margin": "sm"},
            _inst_row("外資", f_net),
            _inst_row("投信", t_net),
            _inst_row("自營商", d_net),
        ]

    # 族群熱度 Top3
    if sectors:
        top3 = sectors[:3]
        heat_str = "  ".join(f"🔥{s[0]}" for s in top3)
        body_contents += [
            {"type": "separator", "margin": "md", "color": C_BORDER},
            {"type": "text", "text": "族群熱度 Top3",
             "size": "xs", "color": C_MUTED, "margin": "sm"},
            {"type": "text", "text": heat_str,
             "size": "sm", "color": C_WHITE, "wrap": True},
        ]

    return {
        "type": "bubble", "size": "mega",
        "styles": {
            "header": {"backgroundColor": "#0a1020"},
            "body":   {"backgroundColor": C_BG},
            "footer": {"backgroundColor": C_SURFACE},
        },
        "header": {
            "type": "box", "layout": "horizontal", "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "📊 台股大盤即時行情",
                 "size": "lg", "weight": "bold", "color": C_WHITE, "flex": 1},
                {"type": "text", "text": _today_str(),
                 "size": "xs", "color": C_MUTED, "align": "end"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "14px", "spacing": "sm",
            "contents": body_contents,
        },
        "footer": {
            "type": "box", "layout": "horizontal",
            "paddingAll": "10px", "spacing": "sm",
            "contents": [
                _footer_btn("熱門排行", "/report momentum", C_ACCENT),
                _footer_btn("外資動向", "/inst 2330",       C_YELLOW),
                _footer_btn("族群熱度", "/sector",          "#8866ff"),
            ],
        },
    }


# ── AI 個股分析 Flex Card ────────────────────────────────────────────────────

def flex_ai_stock_analysis(
    code: str,
    name: str,
    conviction: float,     # 0~1
    regime_label: str,
    signals: dict,         # {tech, chip, fundamental, sentiment}
    action: str,           # buy/hold/sell/watch
    target: float,
    stop: float,
    position_pct: float,
    ai_text: str = "",
) -> dict:
    """AI 個股分析完整卡片"""
    cv_pct    = conviction * 100
    cv_color  = C_GREEN if cv_pct >= 70 else (C_YELLOW if cv_pct >= 50 else C_RED)
    act_zh    = {"buy": "建議買進 🟢", "hold": "建議持有 🟡",
                 "sell": "建議賣出 🔴", "watch": "觀察等待 👁"}.get(action, action)
    act_color = {"buy": C_GREEN, "hold": C_YELLOW,
                 "sell": C_RED,  "watch": C_MUTED}.get(action, C_WHITE)

    stars = int(conviction * 5)
    star_str = "★" * stars + "☆" * (5 - stars)

    def _signal_row(icon, label, value, color=None):
        clr = color or (C_GREEN if "↑" in value else (C_RED if "↓" in value else C_WHITE))
        return {
            "type": "box", "layout": "horizontal", "paddingAll": "5px",
            "contents": [
                {"type": "text", "text": f"{icon} {label}",
                 "size": "sm", "color": C_MUTED, "flex": 2},
                {"type": "text", "text": value, "size": "sm",
                 "color": clr, "align": "end", "flex": 3, "weight": "bold"},
            ],
        }

    return {
        "type": "bubble", "size": "mega",
        "styles": {
            "header": {"backgroundColor": "#0d1428"},
            "body":   {"backgroundColor": C_BG},
            "footer": {"backgroundColor": C_SURFACE},
        },
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "14px",
            "contents": [
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {
                            "type": "box", "layout": "vertical", "flex": 1,
                            "contents": [
                                {"type": "text", "text": name,
                                 "size": "xl", "weight": "bold", "color": C_WHITE},
                                {"type": "text", "text": f"{code}  {regime_label}",
                                 "size": "xs", "color": C_ACCENT},
                            ],
                        },
                        {
                            "type": "box", "layout": "vertical", "alignItems": "flex-end",
                            "contents": [
                                {"type": "text",
                                 "text": f"信心 {cv_pct:.0f}/100",
                                 "size": "md", "weight": "bold", "color": cv_color},
                                {"type": "text", "text": star_str,
                                 "size": "sm", "color": cv_color},
                            ],
                        },
                    ],
                },
            ],
        },
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "14px", "spacing": "sm",
            "contents": [
                # 四維信號
                _signal_row("📈", "技術面", signals.get("tech", "─")),
                _signal_row("🏛", "籌碼面", signals.get("chip", "─")),
                _signal_row("📊", "基本面", signals.get("fundamental", "─")),
                _signal_row("💬", "情緒面", signals.get("sentiment", "─")),
                {"type": "separator", "margin": "md", "color": C_BORDER},
                # 決策
                {
                    "type": "box", "layout": "vertical",
                    "backgroundColor": C_SURFACE, "paddingAll": "12px",
                    "cornerRadius": "8px",
                    "contents": [
                        {"type": "text", "text": "AI 建議",
                         "size": "xxs", "color": C_MUTED},
                        {"type": "text", "text": act_zh,
                         "size": "lg", "weight": "bold", "color": act_color},
                        {
                            "type": "box", "layout": "horizontal", "marginTop": "8px",
                            "contents": [
                                {"type": "text",
                                 "text": f"目標 {target:.0f}元" if target > 0 else "目標 ─",
                                 "size": "sm", "color": C_GREEN, "flex": 1},
                                {"type": "text",
                                 "text": f"停損 {stop:.0f}元" if stop > 0 else "停損 ─",
                                 "size": "sm", "color": C_RED, "flex": 1, "align": "center"},
                                {"type": "text",
                                 "text": f"倉位 {position_pct:.0f}%",
                                 "size": "sm", "color": C_ACCENT, "flex": 1, "align": "end"},
                            ],
                        },
                    ],
                },
                # AI 摘要文字
                *([] if not ai_text else [
                    {"type": "separator", "margin": "sm", "color": C_BORDER},
                    {"type": "text", "text": ai_text[:120],
                     "size": "xs", "color": C_MUTED, "wrap": True},
                ]),
            ],
        },
        "footer": {
            "type": "box", "layout": "horizontal",
            "paddingAll": "10px", "spacing": "sm",
            "contents": [
                _postback_btn("加入庫存", f"act=add_holding&code={code}", C_GREEN),
                _footer_btn("設停損警報", f"/alert {code} price_below {stop:.0f}", C_RED),
                _footer_btn("研究清單",   f"/research {code}",                     "#8866ff"),
            ],
        },
    }


# ── 更多功能選單 Flex Card ────────────────────────────────────────────────────

def flex_more_menu_v2() -> dict:
    """更多功能 2×5 格子選單"""
    def _btn(icon, label, data_or_text: str, is_postback: bool = False) -> dict:
        if is_postback:
            action = {"type": "postback", "label": f"{icon} {label}", "data": data_or_text}
        else:
            action = {"type": "message", "label": f"{icon} {label}", "text": data_or_text}
        return {
            "type": "button", "action": action,
            "style": "secondary", "height": "sm",
            "color": C_SURFACE,
            "margin": "xs",
        }

    rows = [
        [("📈", "策略回測",  "act=more&sub=backtest", True),
         ("🛡️", "風控分析",  "act=more&sub=risk",     True)],
        [("🪙", "零股計算",  "/odd 5000 2330",         False),
         ("🏆", "績效排行",  "/accuracy",              False)],
        [("🔥", "族群熱度",  "/sector",                False),
         ("💰", "資金流向",  "/flow",                  False)],
        [("🔔", "我的警報",  "/alert_list",            False),
         ("⚡", "Alpha狀態", "/alpha",                 False)],
        [("📋", "研究清單",  "/research 2330",          False),
         ("🎯", "今日決策",  "/daily",                 False)],
    ]

    row_boxes = []
    for row in rows:
        row_boxes.append({
            "type": "box", "layout": "horizontal", "spacing": "sm",
            "contents": [_btn(icon, label, action, is_pb)
                         for icon, label, action, is_pb in row],
        })

    return {
        "type": "bubble", "size": "mega",
        "styles": {
            "header": {"backgroundColor": "#0a1020"},
            "body":   {"backgroundColor": C_BG},
        },
        "header": {
            "type": "box", "layout": "horizontal", "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "⚙️ 更多功能",
                 "size": "lg", "weight": "bold", "color": C_WHITE},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "12px", "spacing": "sm",
            "contents": row_boxes,
        },
    }


# ── AI 功能選單 Quick Reply ───────────────────────────────────────────────────

def qr_ai_menu() -> dict:
    return qr_items(
        ("🎯 今日決策",  "/daily"),
        ("🛡️ 持倉健診",  "/overlay"),
        ("🔥 族群輪動",  "/sector"),
        ("💬 市場情緒",  "/ai 今日台股市場情緒"),
        ("🔬 個股分析",  "/ai 2330"),
        ("📋 研究清單",  "/research 2330"),
        ("⚡ Alpha狀態", "/alpha"),
    )


def _parse_report_sections(text: str) -> dict:
    """把早報文字拆成幾個段落"""
    result = {}
    current_key = None
    current_lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        # 識別段落標題（含 emoji 或 ─）
        if any(stripped.startswith(x) for x in ("🏦", "📈", "📉", "🏛", "🤖")):
            if current_key:
                result[current_key] = "\n".join(current_lines).strip()
            current_key  = stripped
            current_lines = []
        elif current_key:
            current_lines.append(stripped)
    if current_key and current_lines:
        result[current_key] = "\n".join(current_lines).strip()
    return result
