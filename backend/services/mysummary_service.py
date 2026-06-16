"""mysummary_service.py — 個人投資總結（Claude AI 個人化分析）"""
from __future__ import annotations

from loguru import logger


# ── 主要資料收集 ──────────────────────────────────────────────────────────────

async def get_mysummary(uid: str) -> dict:
    """
    收集用戶投資組合、日誌、自選股資料，回傳彙整 dict 供 Claude 分析。
    不使用快取（每次呼叫皆取最新資料）。
    """
    import asyncio

    # 1. Portfolio holdings
    holdings: list[dict] = []
    try:
        from ..models.database import AsyncSessionLocal
        from . import portfolio_service
        async with AsyncSessionLocal() as db:
            holdings = await portfolio_service.get_portfolio(db, uid)
    except Exception as e:
        logger.warning("[mysummary] portfolio fetch failed uid={}: {}", uid[:8], e)

    # 2. Journal entries
    journals: list[dict] = []
    try:
        from .trade_journal import get_journal
        journals = await get_journal(uid, limit=10)
    except Exception as e:
        try:
            from .diary_service import get_recent_entries
            raw = await get_recent_entries(uid, n=10)
            journals = raw if isinstance(raw, list) else []
        except Exception as e2:
            logger.debug("[mysummary] journal fetch failed uid={}: {}", uid[:8], e2)

    # 3. Watchlist / favorites
    watchlist: list[dict] = []
    try:
        from .stock_favorites import get_favorites
        watchlist = get_favorites(uid)
    except Exception as e:
        logger.debug("[mysummary] watchlist fetch failed uid={}: {}", uid[:8], e)

    return {
        "uid":           uid,
        "holdings":      holdings,
        "journal_count": len(journals),
        "journals":      journals,
        "watchlist":     watchlist,
    }


# ── Claude 分析 ───────────────────────────────────────────────────────────────

async def generate_mysummary_report(uid: str) -> str:
    """
    取得用戶資料後交給 Claude claude-sonnet-4-6 做個人化投資總結。
    若 API 不可用則回退規則式分析。
    """
    user_data = await get_mysummary(uid)
    holdings  = user_data["holdings"]
    journals  = user_data["journals"]
    watchlist = user_data["watchlist"]

    # ── 嘗試 Claude ──────────────────────────────────────────────────────────
    try:
        from ..models.database import settings
        from ..utils.credit_guard import is_exhausted as _credit_exhausted, mark_exhausted as _mark_exhausted

        if not settings.anthropic_api_key or _credit_exhausted():
            raise RuntimeError("Claude API 不可用")

        import anthropic as _ant

        # Build portfolio summary text
        if holdings:
            hold_lines = []
            total_pnl = 0.0
            for h in holdings:
                pnl_pct = h.get("pnl_pct", 0)
                pnl     = h.get("pnl", 0)
                total_pnl += pnl
                hold_lines.append(
                    f"  {h['stock_code']} {h.get('stock_name', '')} "
                    f"持股 {h.get('shares', 0)} 股 "
                    f"成本 {h.get('cost_price', 0):.1f} "
                    f"現價 {h.get('current_price', 0):.1f} "
                    f"損益 {pnl_pct:+.1f}% ({pnl:+,.0f}元) "
                    f"持有 {h.get('holding_days', 0)} 天"
                )
            portfolio_text = (
                f"投資組合（共 {len(holdings)} 檔，總損益 {total_pnl:+,.0f} 元）：\n"
                + "\n".join(hold_lines)
            )
        else:
            portfolio_text = "投資組合：目前無持股"

        # Build journal summary text
        if journals:
            journal_lines = []
            for j in journals[:5]:
                action = j.get("action") or j.get("type") or "記錄"
                code   = j.get("stock_code") or j.get("stock_id") or ""
                note   = j.get("note") or j.get("content") or j.get("reason") or ""
                date_  = j.get("date") or j.get("created_at") or ""
                journal_lines.append(f"  [{date_}] {action} {code} — {note[:50]}")
            journal_text = (
                f"最近 {len(journals)} 筆交易日誌：\n" + "\n".join(journal_lines)
            )
        else:
            journal_text = "交易日誌：無記錄"

        # Build watchlist text
        if watchlist:
            wl_text = "自選股：" + "、".join(
                f"{s.get('stock_id', '')} {s.get('name', '')}" for s in watchlist[:10]
            )
        else:
            wl_text = "自選股：無"

        user_message = (
            f"{portfolio_text}\n\n"
            f"{journal_text}\n\n"
            f"{wl_text}\n\n"
            "請根據以上資料分析：\n"
            "1. 投資風格（集中/分散、長線/短線）\n"
            "2. 最擅長的操作類型\n"
            "3. 最需要改進的地方\n"
            "4. 量身定制的 3 條投資建議\n"
            "回答請在 600 字內，分段清晰。"
        )

        client = _ant.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=(
                "你是專業投資顧問，根據用戶的實際交易記錄和持倉，"
                "提供客觀且個人化的投資總結。"
                "請用繁體中文回答，分段清晰，600字內。"
            ),
            messages=[{"role": "user", "content": user_message}],
        )
        ai_text = msg.content[0].text.strip() if msg.content else ""
        if not ai_text:
            raise RuntimeError("Claude 回傳空白")

        return format_mysummary_report({"uid": uid, "ai_report": ai_text})

    except Exception as e:
        err_str = str(e)
        if "credit balance is too low" in err_str:
            try:
                from ..utils.credit_guard import mark_exhausted as _me
                _me()
            except Exception as e:
                pass
            logger.warning("[mysummary] Anthropic credit 耗盡 uid={}", uid[:8])
        else:
            logger.info("[mysummary] Claude fallback uid={}: {}", uid[:8], err_str[:80])
        # Fallback to rule-based
        fallback = _fallback_summary(holdings, watchlist)
        return format_mysummary_report({"uid": uid, "ai_report": fallback})


# ── 規則式備用分析 ────────────────────────────────────────────────────────────

def _fallback_summary(holdings: list[dict], watchlist: list[dict]) -> str:
    """當 Claude 無法使用時，以規則式邏輯產生投資總結。"""
    if not holdings:
        return (
            "目前無持倉記錄。\n\n"
            "建議：先建立自己的持倉後，本功能可提供個人化投資風格分析。\n"
            "可使用 /buy 代碼 股數 成本 記錄持倉。"
        )

    # Calculate basic stats
    total_value = sum(h.get("market_value", 0) for h in holdings)
    total_pnl   = sum(h.get("pnl", 0) for h in holdings)
    total_cost  = sum(h.get("cost_price", 0) * h.get("shares", 0) for h in holdings)
    avg_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    winners = [h for h in holdings if h.get("pnl_pct", 0) > 0]
    losers  = [h for h in holdings if h.get("pnl_pct", 0) < 0]

    avg_holding_days = (
        sum(h.get("holding_days", 0) for h in holdings) / len(holdings)
        if holdings else 0
    )

    # Style detection
    if avg_holding_days < 10:
        style = "短線交易者"
    elif avg_holding_days < 60:
        style = "波段操作者"
    else:
        style = "中長線投資者"

    if len(holdings) <= 3:
        concentration = "高度集中型（持股 3 檔以內）"
    elif len(holdings) <= 8:
        concentration = "適度分散型（持股 4-8 檔）"
    else:
        concentration = "高度分散型（持股超過 8 檔）"

    # Win rate
    win_rate = len(winners) / len(holdings) * 100 if holdings else 0

    parts = [
        f"【投資風格】{style}，{concentration}",
        f"  平均持有天數：{avg_holding_days:.0f} 天",
        "",
        f"【當前損益】總資產 {total_value:,.0f} 元，整體損益 {avg_pnl_pct:+.1f}%",
        f"  獲利 {len(winners)} 檔 / 虧損 {len(losers)} 檔（勝率 {win_rate:.0f}%）",
    ]

    if winners:
        best = max(winners, key=lambda h: h.get("pnl_pct", 0))
        parts.append(
            f"  最佳持股：{best['stock_code']} {best.get('stock_name', '')} "
            f"（+{best['pnl_pct']:.1f}%）"
        )
    if losers:
        worst = min(losers, key=lambda h: h.get("pnl_pct", 0))
        parts.append(
            f"  最差持股：{worst['stock_code']} {worst.get('stock_name', '')} "
            f"（{worst['pnl_pct']:.1f}%）"
        )

    parts += [
        "",
        "【個人化建議】",
        "1. " + (
            "持股集中，建議適度分散降低個股風險" if len(holdings) <= 3
            else "持股多元，注意追蹤每一檔的基本面變化"
        ),
        "2. " + (
            "短線操作頻繁，注意交易成本累積" if avg_holding_days < 10
            else "持倉時間充裕，可配合財報週期決策"
        ),
        "3. " + (
            f"勝率 {win_rate:.0f}%，建議設定明確停損規則" if win_rate < 50
            else f"勝率 {win_rate:.0f}%，表現良好，持續維持紀律"
        ),
    ]

    if watchlist:
        parts += [
            "",
            f"【自選股】追蹤中 {len(watchlist)} 檔：" +
            "、".join(s.get("stock_id", "") for s in watchlist[:5]),
        ]

    return "\n".join(parts)


# ── 報告格式化 ────────────────────────────────────────────────────────────────

def format_mysummary_report(data: dict) -> str:
    ai_report = data.get("ai_report", "")
    lines = [
        "📊 我的投資總結",
        "─" * 28,
        "",
        ai_report,
        "",
        "─" * 28,
        "💡 /p 查看持倉  /history 交易記錄  /watchlist 自選股",
    ]
    return "\n".join(lines)
