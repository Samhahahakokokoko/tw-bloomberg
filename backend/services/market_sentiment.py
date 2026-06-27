"""Market Sentiment Index — composite 0-100 score"""
import time
from datetime import date
from loguru import logger

_cache: dict = {}
_cache_ts: float = 0.0
_TTL = 1800  # 30 min

# 跨快取週期追蹤「前次分數」（用於當日比較）
_prev_score: int | None = None
_prev_date: str = ""


async def get_sentiment_score() -> dict:
    """Composite market sentiment 0-100"""
    global _cache, _cache_ts, _prev_score, _prev_date
    if _cache and time.time() - _cache_ts < _TTL:
        return _cache

    # 儲存上一次分數供比較
    old_score = _cache.get("score") if _cache else None
    old_date  = _cache.get("_date", "") if _cache else ""
    today_str = date.today().isoformat()

    score = 50  # neutral baseline
    factors = {}
    raw = {}  # 原始數值，供組成 reasons

    # Factor 1: TAIEX change_pct (weight: 35)
    try:
        from .twse_service import fetch_market_overview
        ov = await fetch_market_overview()
        pct = float(ov.get("change_pct", 0) or 0)
        delta = max(-20, min(20, pct * 13.3))
        score += delta
        factors["taiex"] = f"大盤 {pct:+.2f}%"
        raw["taiex_pct"] = pct
    except Exception as e:
        logger.debug(f"[sentiment] taiex factor skip: {e}")

    # Factor 2: Institutional net — foreign investor net
    try:
        import httpx, json as _json
        url = "https://www.twse.com.tw/fund/BFI82U?response=json&type=day"
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(url)
            data = _json.loads(resp.content)
        rows = data.get("data", [])

        def _n(v):
            try: return int(str(v).replace(",", "").replace("+", "") or 0)
            except (ValueError, TypeError): return 0

        foreign_net = None
        for r in rows:
            if len(r) >= 4 and "外資" in str(r[0]) and "陸資" in str(r[0]):
                foreign_net = _n(r[3])
                break
        if foreign_net is None:
            total_row = next((r for r in rows if "合計" in str(r[0])), None)
            if total_row and len(total_row) >= 4:
                foreign_net = _n(total_row[3])

        if foreign_net is not None:
            delta = max(-15, min(15, foreign_net / 1e9 * 1.5))
            score += delta
            sign = "+" if foreign_net >= 0 else ""
            factors["institutional"] = f"外資{sign}{foreign_net/1e8:.1f}億"
            raw["foreign_net_bil"] = foreign_net / 1e8
    except Exception as e:
        logger.debug(f"[sentiment] institutional factor skip: {e}")

    # Factor 3: Margin balance change
    try:
        import httpx, json as _json2
        url2 = "https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&type=ALL"
        async with httpx.AsyncClient(timeout=10) as c:
            raw2 = await c.get(url2)
        data2 = _json2.loads(raw2.content)
        tables = data2.get("tables", [])
        margin_chg_bil = 0.0
        if tables:
            rows2 = tables[0].get("data", [])
            for r2 in rows2:
                if len(r2) >= 6:
                    try:
                        prev = int(str(r2[4]).replace(",", "") or 0)
                        today = int(str(r2[5]).replace(",", "") or 0)
                        if prev > 1e6:
                            margin_chg_bil = (today - prev) / 1e6
                            break
                    except Exception as e:
                        continue
        delta = max(-8, min(4, -margin_chg_bil))
        score += delta
        sign = "+" if margin_chg_bil >= 0 else ""
        factors["margin"] = f"融資{sign}{margin_chg_bil:.1f}億"
        raw["margin_chg_bil"] = margin_chg_bil
    except Exception as e:
        logger.debug(f"[sentiment] margin factor skip: {e}")

    # Factor 4: Advance/Decline breadth (weight: ±12)
    try:
        from .report_screener import _rt_cache
        prices = _rt_cache.get("prices", {})
        if prices:
            up   = sum(1 for v in prices.values() if float(v.get("change_pct", 0) or 0) > 0)
            down = sum(1 for v in prices.values() if float(v.get("change_pct", 0) or 0) < 0)
            total_bd = up + down
            if total_bd >= 100:
                ratio = up / total_bd
                delta = max(-12, min(12, (ratio - 0.5) * 24))
                score += delta
                factors["breadth"] = f"漲{up}跌{down}（{ratio*100:.0f}%上漲）"
                raw["breadth_up"] = up
                raw["breadth_down"] = down
                raw["breadth_ratio"] = ratio
    except Exception as e:
        logger.debug(f"[sentiment] breadth factor skip: {e}")

    score = max(0, min(100, round(score)))

    # ── 狀態描述（5 段）──────────────────────────────────────────────────
    if score >= 80:
        label = "極度樂觀"
        icon = "🔥"
        advice = "注意過熱，適時減碼"
        state_desc = "急漲過熱 — 市場情緒過熱，追高風險高，建議逢高減碼"
    elif score >= 65:
        label = "偏多"
        icon = "📈"
        advice = "可積極操作"
        state_desc = "穩定上漲 — 多頭格局穩定，可順勢操作強勢個股"
    elif score >= 50:
        label = "偏多"
        icon = "📈"
        advice = "可積極操作"
        state_desc = "偏多震盪 — 多方佔優但力道有限，選股比擇時更重要"
    elif score >= 40:
        label = "中性"
        icon = "↔️"
        advice = "謹慎觀望"
        state_desc = "區間整理 — 市場方向不明，震盪格局，輕倉等待突破"
    elif score >= 25:
        label = "偏空"
        icon = "📉"
        advice = "減少持倉"
        state_desc = "轉弱警示 — 多頭動能衰退，注意下行風險，逢彈減碼"
    else:
        label = "極度恐慌"
        icon = "🔻"
        advice = "留意反彈機會"
        state_desc = "恐慌超賣 — 市場恐慌情緒升溫，留意技術面超賣反彈訊號"

    # ── 具體原因句子（2-3 條）────────────────────────────────────────────
    reasons: list[str] = []
    taiex_pct = raw.get("taiex_pct")
    if taiex_pct is not None:
        if taiex_pct > 1.0:
            reasons.append(f"大盤今日強勢上漲 {taiex_pct:+.2f}%，多頭動能強勁")
        elif taiex_pct > 0.3:
            reasons.append(f"大盤小幅上漲 {taiex_pct:+.2f}%，市場氣氛偏多")
        elif taiex_pct < -1.0:
            reasons.append(f"大盤今日大跌 {taiex_pct:+.2f}%，空頭壓力沉重")
        elif taiex_pct < -0.3:
            reasons.append(f"大盤小幅下跌 {taiex_pct:+.2f}%，市場氣氛偏空")
        else:
            reasons.append(f"大盤漲跌幅 {taiex_pct:+.2f}%，整體方向不明")

    fn = raw.get("foreign_net_bil")
    if fn is not None:
        if fn > 5:
            reasons.append(f"外資今日大幅買超 {fn:.1f} 億，法人態度積極")
        elif fn > 1:
            reasons.append(f"外資今日買超 {fn:.1f} 億，籌碼偏多")
        elif fn < -5:
            reasons.append(f"外資今日大幅賣超 {abs(fn):.1f} 億，法人撤資")
        elif fn < -1:
            reasons.append(f"外資今日賣超 {abs(fn):.1f} 億，籌碼偏空")
        else:
            reasons.append(f"外資近乎中立（{fn:+.1f} 億），無明顯方向")

    mc = raw.get("margin_chg_bil")
    if mc is not None and len(reasons) < 3:
        if mc > 1.0:
            reasons.append(f"融資今日快速擴張 {mc:+.1f} 億，追高情緒濃厚（警示）")
        elif mc < -0.5:
            reasons.append(f"融資今日收縮 {mc:.1f} 億，籌碼換手健康")
        else:
            reasons.append(f"融資水位穩定（{mc:+.1f} 億），過熱風險低")

    br = raw.get("breadth_ratio")
    if br is not None and len(reasons) < 3:
        up_cnt = raw.get("breadth_up", 0)
        dn_cnt = raw.get("breadth_down", 0)
        if br >= 0.65:
            reasons.append(f"市場廣度強健：上漲 {up_cnt} 家（{br*100:.0f}%），多頭擴散")
        elif br <= 0.35:
            reasons.append(f"市場廣度疲弱：下跌 {dn_cnt} 家（{(1-br)*100:.0f}%），空頭擴散")
        else:
            reasons.append(f"漲跌家數約略均衡：漲 {up_cnt} / 跌 {dn_cnt}")

    # ── 與前次比較 ────────────────────────────────────────────────────────
    vs_yesterday: str | None = None
    if old_score is not None:
        diff = score - old_score
        if old_date != today_str:
            # 跨日：這是真正的「昨日比較」
            if diff > 5:
                vs_yesterday = f"較昨日 +{diff} 分，情緒明顯改善 📈"
            elif diff > 0:
                vs_yesterday = f"較昨日 +{diff} 分，情緒小幅樂觀"
            elif diff < -5:
                vs_yesterday = f"較昨日 {diff} 分，情緒明顯轉保守 📉"
            elif diff < 0:
                vs_yesterday = f"較昨日 {diff} 分，情緒略趨保守"
            else:
                vs_yesterday = "與昨日持平"
        else:
            # 同日：與上次快取比較
            if abs(diff) >= 3:
                direction = "上升" if diff > 0 else "下降"
                vs_yesterday = f"較上次更新 {direction} {abs(diff)} 分"

    result = {
        "score":       score,
        "label":       label,
        "icon":        icon,
        "advice":      advice,
        "state_desc":  state_desc,
        "reasons":     reasons,
        "vs_yesterday": vs_yesterday,
        "factors":     factors,
        "_date":       today_str,
    }
    _cache = result
    _cache_ts = time.time()
    return result


def format_sentiment(data: dict) -> str:
    score = data["score"]
    bar_filled = round(score / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    lines = [
        "📊 大盤情緒指數",
        "─" * 22,
        f"{data['icon']} {data['label']}  {score}/100",
        f"[{bar}]",
        "",
        f"📌 {data.get('state_desc', data['label'])}",
    ]

    # 具體原因
    reasons = data.get("reasons", [])
    if reasons:
        lines.append("")
        lines.append("📋 判斷依據：")
        for r in reasons[:3]:
            lines.append(f"  · {r}")

    # 與前次/昨日比較
    vs = data.get("vs_yesterday")
    if vs:
        lines.append("")
        lines.append(f"🔄 {vs}")

    # 倉位建議
    lines.append("")
    if score >= 70:
        position = "💼 建議倉位：滿倉（90%+）"
    elif score >= 50:
        position = "💼 建議倉位：七成倉（70%）"
    elif score >= 30:
        position = "💼 建議倉位：五成倉（50%）"
    else:
        position = "💼 建議倉位：三成倉或空手（≤30%）"
    lines.append(position)
    lines.append(f"💡 建議：{data['advice']}")

    return "\n".join(lines)
