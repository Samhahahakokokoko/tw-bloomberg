"""AI 日報交易建議服務

每日 19:30 產生結構化操作建議：
  市場狀態：多頭/空頭/盤整
  買進：XXX（原因）
  觀察：XXX
  避免：XXX

個股分析 /ai {code}：
  趨勢分析 + 籌碼動向 + 具體操作建議
"""
from __future__ import annotations
from datetime import date
from loguru import logger
from ..models.database import settings


# ── 日報格式化 ────────────────────────────────────────────────────────────────

async def generate_daily_trading_advice() -> str:
    """
    組合每日 AI 交易建議，格式：
    市場狀態：...
    買進：... （原因）
    觀察：...
    避免：...
    """
    from .screener_engine import get_top_scores, run_screener, ScreenerFilter

    # 1. 偵測市場盤態
    regime_info = await _get_regime()
    regime      = regime_info.get("current", "unknown")
    regime_label= {"bull": "多頭 📈", "bear": "空頭 📉", "sideways": "盤整 ↔️"}.get(regime, "未知")

    # 2. 取今日高分股
    top = await get_top_scores(limit=20)
    buy_candidates = top[:3] if top else []

    # 3. 取弱勢股（低分）for 避免清單
    avoid_results = await run_screener(ScreenerFilter(
        total_score_min=0, sort_by="total_score", limit=5
    ))
    avoid_candidates = [r for r in avoid_results if r["total_score"] < 30]

    lines = [
        f"📊 今日操作建議 {date.today().strftime('%m/%d')}",
        "─" * 24,
        f"市場狀態：{regime_label}",
        "",
    ]

    # 買進建議
    if buy_candidates:
        lines.append("🟢 買進候選：")
        for s in buy_candidates:
            reasons = []
            if s.get("foreign_consec_buy", 0) >= 3:
                reasons.append(f"外資連買{s['foreign_consec_buy']}日")
            if s.get("three_margins_up"):
                reasons.append("三率齊升")
            if s.get("ma_aligned"):
                reasons.append("均線多頭")
            if s.get("kd_golden_cross"):
                reasons.append("KD交叉")
            reason_str = "、".join(reasons) if reasons else "綜合評分佳"
            lines.append(f"  {s['stock_code']} {s.get('stock_name','')}（{reason_str}）")
    else:
        lines.append("🟢 買進候選：暫無明確訊號，建議觀望")

    lines.append("")

    # 觀察清單（中等評分）
    observe = [s for s in top if 50 <= s["total_score"] < 70][:3]
    if observe:
        lines.append("🟡 觀察清單：")
        for s in observe:
            lines.append(f"  {s['stock_code']} {s.get('stock_name','')}（評分{s['total_score']}，尚未突破）")
    else:
        lines.append("🟡 觀察清單：評分中等個股尚無明確催化劑")

    lines.append("")

    # 避免清單
    if avoid_candidates:
        lines.append("🔴 暫時避免：")
        for s in avoid_candidates[:3]:
            lines.append(f"  {s['stock_code']} {s.get('stock_name','')}（評分偏低{s['total_score']}）")
    elif regime == "bear":
        lines.append("🔴 暫時避免：市場空頭，建議降低持倉比重")
    else:
        lines.append("🔴 暫時避免：無特別警示標的")

    # AI 總結
    body = "\n".join(lines)
    ai_comment = await _ai_daily_comment(body, regime, buy_candidates)
    if ai_comment:
        lines.append(f"\n🤖 AI 總結\n{ai_comment}")

    return "\n".join(lines)


# ── 個股深度分析 ──────────────────────────────────────────────────────────────

async def analyze_stock_for_line(stock_code: str) -> str:
    """
    /ai {code} 的回覆格式：
    [代碼 名稱]
    趨勢：...
    籌碼：...
    技術：...
    建議：...
    """
    from .twse_service import fetch_realtime_quote, fetch_kline
    from .chip_service import fetch_chip_history
    from .screener_engine import get_stock_score
    from .indicator_engine import calc_mas, calc_kd, detect_kd_cross

    # 報價
    try:
        q = await fetch_realtime_quote(stock_code)
    except Exception:
        q = {}

    name  = q.get("name", stock_code)
    price = q.get("price", 0)
    chg   = q.get("change_pct", 0)

    # 技術指標
    trend_desc = "未知"
    tech_desc  = ""
    try:
        kline  = await fetch_kline(stock_code)
        closes = [float(k["close"]) for k in kline if k.get("close")]
        highs  = [float(k["high"])  for k in kline if k.get("high")]
        lows   = [float(k["low"])   for k in kline if k.get("low")]
        if len(closes) >= 20:
            mas = calc_mas(closes, [5, 20, 60])
            ma5, ma20, ma60 = mas.get(5), mas.get(20), mas.get(60)
            golden_cross, k, d = detect_kd_cross(highs, lows, closes)

            if ma5 and ma20 and ma60:
                if closes[-1] > ma5 > ma20 > ma60:
                    trend_desc = "多頭排列 📈 均線向上"
                elif closes[-1] < ma5 < ma20:
                    trend_desc = "空頭排列 📉 均線向下"
                else:
                    trend_desc = "盤整震盪 ↔️ 均線糾結"

            tech_parts = []
            if golden_cross:  tech_parts.append("KD黃金交叉")
            elif k < 20:      tech_parts.append(f"KD超賣({k:.0f})")
            elif k > 80:      tech_parts.append(f"KD超買({k:.0f})")
            if ma5:           tech_parts.append(f"MA5={ma5:.1f}")
            if ma20:          tech_parts.append(f"MA20={ma20:.1f}")
            tech_desc = "、".join(tech_parts) if tech_parts else "技術中性"
    except Exception as e:
        logger.error(f"[Advisor] tech error {stock_code}: {e}")

    # 籌碼
    chip_desc = "籌碼資料不足"
    try:
        chips = await fetch_chip_history(stock_code, 10)
        if chips:
            foreign_net_5 = sum(c.get("foreign_net", 0) for c in chips[-5:])
            trust_net_5   = sum(c.get("trust_net", 0)   for c in chips[-5:])
            consec = 0
            for c in reversed(chips):
                if c.get("foreign_net", 0) > 0:
                    consec += 1
                else:
                    break
            parts = []
            if consec >= 3:    parts.append(f"外資連買{consec}日")
            elif foreign_net_5 > 0: parts.append(f"外資近5日+{foreign_net_5:,}張")
            elif foreign_net_5 < 0: parts.append(f"外資近5日{foreign_net_5:,}張 ⚠️")
            if trust_net_5 > 0: parts.append(f"投信淨買+{trust_net_5:,}張")
            chip_desc = "、".join(parts) if parts else "法人無明顯動向"
    except Exception as e:
        logger.error(f"[Advisor] chip error {stock_code}: {e}")

    # 評分
    score_desc = ""
    try:
        score = await get_stock_score(stock_code)
        if score:
            score_desc = (
                f"總分{score['total_score']:.0f} "
                f"(基:{score['fundamental_score']:.0f} "
                f"籌:{score['chip_score']:.0f} "
                f"技:{score['technical_score']:.0f})"
            )
    except Exception:
        pass

    # 操作建議（AI 生成）
    suggestion = await _ai_stock_advice(
        stock_code, name, price, chg, trend_desc, chip_desc, tech_desc, score_desc
    )

    sign = "▲" if chg >= 0 else "▼"
    return (
        f"[{stock_code} {name}]\n"
        f"現價：{price}  {sign}{abs(chg):.2f}%\n"
        f"\n📈 趨勢\n{trend_desc}\n"
        f"\n🏛 籌碼\n{chip_desc}\n"
        f"\n📐 技術\n{tech_desc}\n"
        + (f"\n📊 AI評分\n{score_desc}\n" if score_desc else "")
        + f"\n💡 建議\n{suggestion}"
    )


# ── 即時警報偵測 ──────────────────────────────────────────────────────────────

async def check_realtime_alerts(stock_code: str) -> list[str]:
    """
    偵測爆量、外資連買、突破新高訊號
    供每 5 分鐘排程或用戶查詢使用
    """
    alerts = []
    try:
        from .twse_service import fetch_realtime_quote, fetch_kline
        from .chip_service import fetch_chip_history

        q = await fetch_realtime_quote(stock_code)
        price  = q.get("price", 0)
        volume = q.get("volume", 0)

        kline = await fetch_kline(stock_code)
        if len(kline) >= 21:
            closes  = [float(k["close"])  for k in kline]
            volumes = [int(k["volume"])   for k in kline]
            avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 0

            # 爆量警報
            if avg_vol > 0 and volume >= avg_vol * 2.5:
                alerts.append(f"⚡ {stock_code} 爆量！成交量為均量 {volume/avg_vol:.1f}x")

            # 52週新高
            high_52w = max(closes[-min(252, len(closes)):])
            if price >= high_52w * 0.99:
                alerts.append(f"🏆 {stock_code} 突破近年高點 {high_52w:.1f}")

        chips = await fetch_chip_history(stock_code, 5)
        consec = 0
        for c in reversed(chips):
            if c.get("foreign_net", 0) > 0:
                consec += 1
            else:
                break
        if consec >= 3:
            alerts.append(f"🏛 {stock_code} 外資連買 {consec} 日！")

    except Exception as e:
        logger.error(f"[Advisor] realtime alert error {stock_code}: {e}")

    return alerts


# ── 私有輔助函式 ──────────────────────────────────────────────────────────────

async def _get_regime() -> dict:
    try:
        from backtest.market_regime import get_market_regime
        return await get_market_regime()
    except Exception:
        return {"current": "unknown"}


async def _ai_daily_comment(body: str, regime: str, candidates: list) -> str:
    if not settings.anthropic_api_key:
        return ""
    try:
        import anthropic
        regime_tips = {
            "bull":     "適合追漲動能，可放大持倉",
            "bear":     "以防禦為主，控制持倉比重",
            "sideways": "適合波段操作，設好停損",
        }
        tip = regime_tips.get(regime, "")
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": (
                    f"今日市場盤態：{regime}（{tip}）\n"
                    f"選股摘要：{body[:400]}\n\n"
                    "請用繁體中文寫2句今日操作心法，語氣簡潔有力。"
                ),
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.error(f"[Advisor] AI daily comment error: {e}")
        return ""


async def _ai_stock_advice(code, name, price, chg, trend, chip, tech, score) -> str:
    if not settings.anthropic_api_key:
        # 無 API 時的規則建議
        if "多頭" in trend and "連買" in chip:
            return "多頭籌碼雙強，可考慮逢低布局，注意設好停損。"
        elif "空頭" in trend:
            return "目前偏空，建議觀望或減碼，等待趨勢轉折訊號。"
        return "技術中性，建議等待更明確突破訊號再操作。"
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"{code} {name} 現價{price} 漲跌{chg:+.1f}%\n"
                    f"趨勢：{trend}\n籌碼：{chip}\n技術：{tech}\n{score}\n\n"
                    "請用30字內繁體中文給出具體操作建議（含進場條件或停損參考）。"
                ),
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.error(f"[Advisor] AI stock advice error: {e}")
        return "建議結合個人風險承受度操作。"
