"""透明度服務 — 展開 AI 判斷依據

/why sentiment  → 情緒分數逐步計算過程
/why {code}     → 股票健康評分各維度明細 + 假設情境
"""
from __future__ import annotations
import asyncio
from loguru import logger


# ── /why sentiment ────────────────────────────────────────────────────────────

async def get_sentiment_why() -> str:
    """展開情緒分數的逐步計算過程"""
    score = 50.0
    steps: list[tuple[str, float, str]] = []  # (label, delta, detail)

    # Factor 1: TAIEX change_pct (±20)
    try:
        from .twse_service import fetch_market_overview
        ov = await fetch_market_overview()
        pct = float(ov.get("change_pct", 0) or 0)
        delta = max(-20.0, min(20.0, pct * 13.3))
        score += delta
        sign = "+" if pct >= 0 else ""
        steps.append(("加權指數", delta, f"{sign}{pct:.2f}%"))
    except Exception as e:
        logger.debug(f"[why_sentiment] taiex: {e}")

    # Factor 2: Foreign institutional net buy (±15)
    try:
        import httpx
        import json as _json
        url = "https://www.twse.com.tw/fund/BFI82U?response=json&type=day"
        async with httpx.AsyncClient(timeout=10) as c:
            raw = await c.get(url)
            data = _json.loads(raw.content)
        rows = data.get("data", [])

        def _n(v: object) -> int:
            try:
                return int(str(v).replace(",", "").replace("+", "") or 0)
            except Exception:
                return 0

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
            delta = max(-15.0, min(15.0, foreign_net / 1e9 * 1.5))
            score += delta
            sign = "+" if foreign_net >= 0 else ""
            steps.append(("外資淨買", delta, f"{sign}{foreign_net / 1e8:.1f}億"))
    except Exception as e:
        logger.debug(f"[why_sentiment] institutional: {e}")

    # Factor 3: Margin balance change — rising margin = overheated (-8~+4)
    try:
        import httpx
        import json as _json2
        url2 = "https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&type=ALL"
        async with httpx.AsyncClient(timeout=10) as c:
            raw2 = await c.get(url2)
        data2 = _json2.loads(raw2.content)
        tables = data2.get("tables", [])
        margin_chg_bil = 0.0
        if tables:
            for r2 in tables[0].get("data", []):
                if len(r2) >= 6:
                    try:
                        prev = int(str(r2[4]).replace(",", "") or 0)
                        today = int(str(r2[5]).replace(",", "") or 0)
                        if prev > 1_000_000:
                            margin_chg_bil = (today - prev) / 1e6
                            break
                    except Exception:
                        continue
        delta = max(-8.0, min(4.0, -margin_chg_bil))
        score += delta
        sign = "+" if margin_chg_bil >= 0 else ""
        direction = "上升" if margin_chg_bil > 0.05 else ("下降" if margin_chg_bil < -0.05 else "持平")
        steps.append(("融資餘額", delta, f"{sign}{margin_chg_bil:.1f}億({direction})"))
    except Exception as e:
        logger.debug(f"[why_sentiment] margin: {e}")

    # Factor 4: Advance/decline breadth (±12)
    try:
        from .report_screener import _rt_cache
        prices = _rt_cache.get("prices", {})
        if prices:
            up = sum(1 for v in prices.values() if float(v.get("change_pct", 0) or 0) > 0)
            down = sum(1 for v in prices.values() if float(v.get("change_pct", 0) or 0) < 0)
            total_bd = up + down
            if total_bd >= 100:
                ratio = up / total_bd
                delta = max(-12.0, min(12.0, (ratio - 0.5) * 24))
                score += delta
                steps.append(("漲跌家數", delta, f"漲{up}跌{down}({ratio * 100:.0f}%漲)"))
    except Exception as e:
        logger.debug(f"[why_sentiment] breadth: {e}")

    score_int = max(0, min(100, round(score)))

    if score_int >= 80:
        level_icon, level = "🔥", "極度樂觀"
    elif score_int >= 60:
        level_icon, level = "📈", "偏多"
    elif score_int >= 40:
        level_icon, level = "↔️", "中性"
    elif score_int >= 20:
        level_icon, level = "📉", "偏空"
    else:
        level_icon, level = "🔻", "極度恐慌"

    lines = ["🔍 情緒分數推算過程", "─" * 18, "起始：50（中性基準）"]
    for label, delta, detail in steps:
        sign = "+" if delta >= 0 else ""
        lines.append(f"{sign}{delta:.1f}（{label}：{detail}）")
    lines += [
        "─" * 18,
        f"合計：{score_int}/100  {level_icon}{level}",
        "",
        "各項上限：加權±20｜外資±15｜融資-8~+4｜廣度±12",
    ]
    return "\n".join(lines)


# ── /why {code} ───────────────────────────────────────────────────────────────

def _get_stock_detail_sync(code: str) -> dict:
    """同步執行健康評分，回傳所有中間值供透明度展示"""
    import requests
    from datetime import date, timedelta

    result: dict = {"code": code, "error": None}

    # 1. 下載 K 線
    closes: list[float] = []
    current_close = 0.0
    try:
        import yfinance as yf
        start = (date.today() - timedelta(days=186)).strftime("%Y-%m-%d")
        for suffix in (".TW", ".TWO"):
            df = yf.Ticker(f"{code}{suffix}").history(start=start, interval="1d", auto_adjust=True)
            if df is not None and not df.empty:
                closes = [float(v) for v in df["Close"].dropna().tolist() if float(v) > 0]
                if closes:
                    current_close = closes[-1]
                    break
    except Exception as e:
        logger.warning(f"[why_stock] yfinance {code}: {e}")

    if len(closes) < 30:
        result["error"] = f"⚠️ {code} 無法取得足夠歷史資料，請確認代號是否正確。"
        return result

    # 2. 技術面 (30%)
    from .health_score_service import _calc_rsi, _calc_macd, _calc_ma
    rsi = _calc_rsi(closes)
    macd_diff, macd_rising = _calc_macd(closes)
    ma5 = _calc_ma(closes, 5)
    ma20 = _calc_ma(closes, 20)
    ma60 = _calc_ma(closes, 60)

    if rsi < 30:
        rsi_s, rsi_label = 85, "超賣"
    elif rsi <= 60:
        rsi_s, rsi_label = 75, "健康"
    elif rsi <= 70:
        rsi_s, rsi_label = 60, "偏高"
    else:
        rsi_s, rsi_label = 30, "超買"

    if macd_diff > 0 and macd_rising:
        macd_s, macd_label = 85, "金叉向上"
    elif macd_diff > 0:
        macd_s, macd_label = 65, "正值趨緩"
    elif macd_diff < 0 and not macd_rising:
        macd_s, macd_label = 20, "死叉向下"
    else:
        macd_s, macd_label = 40, "負值回升"

    if ma5 and ma20 and ma60:
        if current_close > ma5 > ma20 > ma60:
            ma_s, ma_label = 90, "多頭排列"
        elif current_close < ma5 < ma20 < ma60:
            ma_s, ma_label = 15, "空頭排列"
        else:
            ma_s, ma_label = 55, "均線混排"
    else:
        ma_s, ma_label = 55, "均線混排"

    tech_s = round((rsi_s + macd_s + ma_s) / 3)

    # 3. 籌碼面 (30%)
    f_net = 0
    t_net = 0
    f_s = 50
    t_s = 50
    chip_s = 50
    try:
        start_40 = (date.today() - timedelta(days=40)).strftime("%Y-%m-%d")
        params: dict = {
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id": code,
            "start_date": start_40,
        }
        try:
            from ..models.database import settings as _settings
            if getattr(_settings, "finmind_token", ""):
                params["token"] = _settings.finmind_token
        except Exception:
            pass
        resp = requests.get("https://api.finmindtrade.com/api/v4/data", params=params, timeout=20)
        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("status") == 200:
                for row in payload.get("data", []):
                    name = row.get("name", "")
                    buy = int(float(row.get("buy", 0) or 0))
                    sell = int(float(row.get("sell", 0) or 0))
                    net = (buy - sell) // 1000
                    if name in ("Foreign_Investor", "Foreign_Dealer_Self"):
                        f_net += net
                    elif name == "Investment_Trust":
                        t_net += net
        f_s = 85 if f_net > 5000 else (70 if f_net > 0 else (35 if f_net > -5000 else 15))
        t_s = 90 if t_net > 1000 else (70 if t_net > 0 else (40 if t_net > -500 else 20))
        chip_s = round((f_s + t_s) / 2)
    except Exception as e:
        logger.warning(f"[why_stock] chip {code}: {e}")

    # 4. 趨勢 (20%)
    r5 = r20 = r60 = 0.0
    trend_s = 45
    positive_count = 0
    try:
        if len(closes) >= 6:
            r5 = (closes[-1] / closes[-6] - 1) * 100
        if len(closes) >= 21:
            r20 = (closes[-1] / closes[-21] - 1) * 100
        if len(closes) >= 61:
            r60 = (closes[-1] / closes[-61] - 1) * 100
        positive_count = sum(1 for r in (r5, r20, r60) if r > 0)
        trend_s = {0: 25, 1: 45, 2: 65, 3: 90}[positive_count]
    except Exception as e:
        logger.warning(f"[why_stock] trend {code}: {e}")

    # 5. 新聞 (20%)
    news_s = 50
    news_label = "中性"
    news_titles: list[str] = []
    try:
        import re as _re
        news_resp = requests.get(
            f"https://tw.stock.yahoo.com/quote/{code}.TW/news",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        for tag in _re.findall(r"<h3[^>]*>(.*?)</h3>", news_resp.text, _re.DOTALL)[:5]:
            text = _re.sub(r"<[^>]+>", "", tag).strip()
            if text:
                news_titles.append(text)

        if news_titles:
            try:
                from ..utils.credit_guard import is_exhausted as _credit_ex, mark_exhausted as _mark_ex
                from ..models.database import settings as _settings
                api_key = getattr(_settings, "anthropic_api_key", "") or ""
            except Exception:
                _credit_ex = lambda: True
                api_key = ""

            if api_key and not _credit_ex():
                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
                prompt = (
                    f"以下是台股{code}新聞標題，整體情緒分數"
                    f"（0=極負面，50=中性，100=極正面），只回傳一個整數：\n"
                    + "\n".join(news_titles)
                )
                msg = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=10,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = (msg.content[0].text if msg.content else "50").strip()
                digits = _re.findall(r"\d+", raw)
                if digits:
                    news_s = max(0, min(100, int(digits[0])))

        if news_s >= 70:
            news_label = "正面"
        elif news_s >= 45:
            news_label = "中性"
        else:
            news_label = "負面"
    except Exception as e:
        logger.warning(f"[why_stock] news {code}: {e}")

    # 6. 總分
    total = round(tech_s * 0.3 + chip_s * 0.3 + trend_s * 0.2 + news_s * 0.2)

    result.update({
        "price": current_close,
        "rsi": rsi, "rsi_s": rsi_s, "rsi_label": rsi_label,
        "macd_rising": macd_rising, "macd_diff": macd_diff,
        "macd_s": macd_s, "macd_label": macd_label,
        "ma_s": ma_s, "ma_label": ma_label,
        "tech_s": tech_s,
        "f_net": f_net, "f_s": f_s,
        "t_net": t_net, "t_s": t_s,
        "chip_s": chip_s,
        "r5": r5, "r20": r20, "r60": r60,
        "positive_count": positive_count, "trend_s": trend_s,
        "news_s": news_s, "news_label": news_label,
        "news_titles": news_titles[:3],
        "total": total,
    })
    return result


def _counter_factual(d: dict) -> list[str]:
    """生成一個最有意義的假設情境"""
    total = d["total"]
    tech_s = d["tech_s"]
    chip_s = d["chip_s"]
    trend_s = d["trend_s"]
    news_s = d["news_s"]

    # Priority 1: flip foreign net direction
    if d["f_net"] > 0:
        new_f_s = 15
        new_chip_s = round((new_f_s + d["t_s"]) / 2)
        new_total = round(tech_s * 0.3 + new_chip_s * 0.3 + trend_s * 0.2 + news_s * 0.2)
        return [
            f"若外資轉賣超 >5000張",
            f"籌碼分 {chip_s}→{new_chip_s}，總分 {total}→{new_total}（{new_total - total:+d}）",
        ]
    if d["f_net"] < -5000:
        new_f_s = 85
        new_chip_s = round((new_f_s + d["t_s"]) / 2)
        new_total = round(tech_s * 0.3 + new_chip_s * 0.3 + trend_s * 0.2 + news_s * 0.2)
        return [
            f"若外資轉買超 >5000張",
            f"籌碼分 {chip_s}→{new_chip_s}，總分 {total}→{new_total}（{new_total - total:+d}）",
        ]

    # Priority 2: RSI extremes
    if d["rsi"] > 70:
        new_rsi_s = 75
        new_tech_s = round((new_rsi_s + d["macd_s"] + d["ma_s"]) / 3)
        new_total = round(new_tech_s * 0.3 + chip_s * 0.3 + trend_s * 0.2 + news_s * 0.2)
        return [
            f"若RSI回落 60以下（超買解除）",
            f"技術分 {tech_s}→{new_tech_s}，總分 {total}→{new_total}（{new_total - total:+d}）",
        ]
    if d["rsi"] < 30:
        new_rsi_s = 75
        new_tech_s = round((new_rsi_s + d["macd_s"] + d["ma_s"]) / 3)
        new_total = round(new_tech_s * 0.3 + chip_s * 0.3 + trend_s * 0.2 + news_s * 0.2)
        return [
            f"若RSI回升至健康區（30~60）",
            f"技術分 {tech_s}→{new_tech_s}，總分 {total}→{new_total}（{new_total - total:+d}）",
        ]

    # Priority 3: MACD flip
    if d["macd_rising"] and d["macd_diff"] > 0:
        new_macd_s = 20
        new_tech_s = round((d["rsi_s"] + new_macd_s + d["ma_s"]) / 3)
        new_total = round(new_tech_s * 0.3 + chip_s * 0.3 + trend_s * 0.2 + news_s * 0.2)
        return [
            f"若MACD死叉向下",
            f"技術分 {tech_s}→{new_tech_s}，總分 {total}→{new_total}（{new_total - total:+d}）",
        ]
    if not d["macd_rising"] and d["macd_diff"] < 0:
        new_macd_s = 85
        new_tech_s = round((d["rsi_s"] + new_macd_s + d["ma_s"]) / 3)
        new_total = round(new_tech_s * 0.3 + chip_s * 0.3 + trend_s * 0.2 + news_s * 0.2)
        return [
            f"若MACD金叉翻正",
            f"技術分 {tech_s}→{new_tech_s}，總分 {total}→{new_total}（{new_total - total:+d}）",
        ]

    return []


def _format_stock_why(d: dict) -> str:
    """格式化股票評分透明度報告"""
    if d.get("error"):
        return d["error"]

    total = d["total"]
    if total >= 80:
        rating = "🟢 優秀"
    elif total >= 65:
        rating = "🟡 良好"
    elif total >= 50:
        rating = "🟠 普通"
    else:
        rating = "🔴 偏弱"

    macd_arrow = "↑" if d["macd_rising"] else "↓"
    f_dir = "買超" if d["f_net"] >= 0 else "賣超"
    t_dir = "買超" if d["t_net"] >= 0 else "賣超"

    lines = [
        f"🔍 {d['code']} 評分依據",
        "─" * 18,
        f"📊 技術面(30%)：{d['tech_s']}分",
        f"  RSI {d['rsi']:.1f}（{d['rsi_label']}，{d['rsi_s']}分）",
        f"  MACD {macd_arrow}（{d['macd_label']}，{d['macd_s']}分）",
        f"  均線{d['ma_label']}（{d['ma_s']}分）",
        f"🏦 籌碼面(30%)：{d['chip_s']}分",
        f"  外資{f_dir} {abs(d['f_net']):,}張（{d['f_s']}分）",
        f"  投信{t_dir} {abs(d['t_net']):,}張（{d['t_s']}分）",
        f"📈 趨勢(20%)：{d['trend_s']}分",
        f"  5日{d['r5']:+.1f}% 20日{d['r20']:+.1f}% 60日{d['r60']:+.1f}%",
        f"  {d['positive_count']}/3項正報酬",
        f"📰 新聞(20%)：{d['news_s']}分（{d['news_label']}）",
        "─" * 18,
        f"加權總分：{total}/100  {rating}",
    ]

    cf = _counter_factual(d)
    if cf:
        lines += ["", "💡 假設情境"] + cf

    return "\n".join(lines)


async def get_stock_why(code: str) -> str:
    """非同步包裝：執行股票評分透明度分析"""
    loop = asyncio.get_running_loop()
    d = await loop.run_in_executor(None, _get_stock_detail_sync, code)
    return _format_stock_why(d)
