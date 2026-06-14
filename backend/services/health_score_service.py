"""股票健康評分服務

多維度健康評分（0-100）：
  技術面 30% — RSI / MACD / 均線排列
  籌碼面 30% — 外資 / 投信淨買賣（FinMind）
  趨勢   20% — 5/20/60 日報酬
  新聞   20% — Claude Haiku 情緒分析

使用方式：
    from backend.services.health_score_service import get_stock_health_score
    text = await get_stock_health_score("2330")
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from loguru import logger

from ..utils.credit_guard import is_exhausted as _credit_exhausted, mark_exhausted as _mark_credit_exhausted


# ── 技術指標輔助 ──────────────────────────────────────────────────────────────

def _calc_rsi(closes: list[float], period: int = 14) -> float:
    """計算 RSI(period)"""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _calc_ema(closes: list[float], period: int) -> list[float]:
    """計算 EMA"""
    if not closes:
        return []
    k = 2 / (period + 1)
    ema = [closes[0]]
    for price in closes[1:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def _calc_macd(closes: list[float]) -> tuple[float, bool]:
    """
    計算 MACD diff 與是否上升。
    回傳 (diff_value, is_rising)
    """
    if len(closes) < 26:
        return 0.0, False
    ema12 = _calc_ema(closes, 12)
    ema26 = _calc_ema(closes, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    if len(macd_line) < 2:
        return macd_line[-1] if macd_line else 0.0, False
    return macd_line[-1], macd_line[-1] > macd_line[-2]


def _calc_ma(closes: list[float], period: int) -> float | None:
    """計算簡單移動平均"""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


# ── 同步主核心 ────────────────────────────────────────────────────────────────

def get_stock_health_score_sync(code: str) -> str:
    """
    計算股票健康評分並回傳格式化文字。
    所有 I/O 均為同步（yfinance、requests）。
    """
    import requests

    # ── 1. 下載 6 個月 K 線（yfinance） ───────────────────────────────────────
    closes: list[float] = []
    current_close = 0.0
    try:
        import yfinance as yf
        start = (date.today() - timedelta(days=186)).strftime("%Y-%m-%d")
        for suffix in (".TW", ".TWO"):
            ticker_str = f"{code}{suffix}"
            df = yf.Ticker(ticker_str).history(start=start, interval="1d", auto_adjust=True)
            if df is not None and not df.empty:
                closes = [float(v) for v in df["Close"].dropna().tolist() if float(v) > 0]
                if closes:
                    current_close = closes[-1]
                    break
    except Exception as e:
        logger.warning(f"[health_score] yfinance {code}: {e}")

    if len(closes) < 30:
        return f"⚠️ {code} 無法取得足夠歷史資料，請確認股票代碼是否正確。"

    # ── 2. 技術面分數 (30%) ────────────────────────────────────────────────────
    rsi = _calc_rsi(closes)
    macd_diff, macd_rising = _calc_macd(closes)
    ma5  = _calc_ma(closes, 5)
    ma20 = _calc_ma(closes, 20)
    ma60 = _calc_ma(closes, 60)

    # RSI 分數
    if rsi < 30:
        rsi_s = 85
    elif rsi <= 60:
        rsi_s = 75
    elif rsi <= 70:
        rsi_s = 60
    else:
        rsi_s = 30

    # MACD 分數
    if macd_diff > 0 and macd_rising:
        macd_s = 85
    elif macd_diff > 0:
        macd_s = 65
    elif macd_diff < 0 and not macd_rising:
        macd_s = 20
    else:
        macd_s = 40

    # 均線排列分數
    price = current_close
    if ma5 and ma20 and ma60:
        if price > ma5 > ma20 > ma60:
            ma_s, ma_label = 90, "多頭排列"
        elif price < ma5 < ma20 < ma60:
            ma_s, ma_label = 15, "空頭排列"
        else:
            ma_s, ma_label = 55, "均線混排"
    else:
        ma_s, ma_label = 55, "均線混排"

    tech_s = round((rsi_s + macd_s + ma_s) / 3)

    # ── 3. 籌碼面分數 (30%) ────────────────────────────────────────────────────
    f_net = 0
    t_net = 0
    chip_s = 50  # 預設中性
    try:
        start_40 = (date.today() - timedelta(days=40)).strftime("%Y-%m-%d")
        params = {
            "dataset":    "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id":    code,
            "start_date": start_40,
        }
        # 嘗試注入 token
        try:
            from ..models.database import settings as _settings
            if getattr(_settings, "finmind_token", ""):
                params["token"] = _settings.finmind_token
        except Exception:
            pass

        resp = requests.get(
            "https://api.finmindtrade.com/api/v4/data",
            params=params,
            timeout=20,
        )
        if resp.status_code == 200:
            payload = resp.json()
            if payload.get("status") == 200:
                for row in payload.get("data", []):
                    name = row.get("name", "")
                    buy  = int(float(row.get("buy",  0) or 0))
                    sell = int(float(row.get("sell", 0) or 0))
                    net  = (buy - sell) // 1000  # 股 → 張
                    if name in ("Foreign_Investor", "Foreign_Dealer_Self"):
                        f_net += net
                    elif name == "Investment_Trust":
                        t_net += net

        # 外資分數
        if f_net > 5000:
            f_s = 85
        elif f_net > 0:
            f_s = 70
        elif f_net > -5000:
            f_s = 35
        else:
            f_s = 15

        # 投信分數
        if t_net > 1000:
            t_s = 90
        elif t_net > 0:
            t_s = 70
        elif t_net > -500:
            t_s = 40
        else:
            t_s = 20

        chip_s = round((f_s + t_s) / 2)
    except Exception as e:
        logger.warning(f"[health_score] chip FinMind {code}: {e}")

    # ── 4. 趨勢分數 (20%) ─────────────────────────────────────────────────────
    r5 = r20 = r60 = 0.0
    trend_s = 45
    try:
        if len(closes) >= 6:
            r5 = (closes[-1] / closes[-6] - 1) * 100
        if len(closes) >= 21:
            r20 = (closes[-1] / closes[-21] - 1) * 100
        if len(closes) >= 61:
            r60 = (closes[-1] / closes[-61] - 1) * 100

        positive_count = sum(1 for r in (r5, r20, r60) if r > 0)
        trend_s_map = {0: 25, 1: 45, 2: 65, 3: 90}
        trend_s = trend_s_map[positive_count]
    except Exception as e:
        logger.warning(f"[health_score] trend {code}: {e}")

    # ── 5. 新聞情緒分數 (20%) ─────────────────────────────────────────────────
    news_s = 50
    news_label = "中性"
    try:
        import re
        news_resp = requests.get(
            f"https://tw.stock.yahoo.com/quote/{code}.TW/news",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        h3_tags = re.findall(r"<h3[^>]*>(.*?)</h3>", news_resp.text, re.DOTALL)
        clean_titles = []
        for tag in h3_tags[:5]:
            text = re.sub(r"<[^>]+>", "", tag).strip()
            if text:
                clean_titles.append(text)

        if clean_titles and not _credit_exhausted():
            news_text = "\n".join(clean_titles)
            try:
                from ..models.database import settings as _settings
                api_key = getattr(_settings, "anthropic_api_key", "") or ""
            except Exception:
                api_key = ""

            if api_key:
                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
                prompt = (
                    f"以下是台股{code}新聞標題，整體情緒分數"
                    f"（0=極負面，50=中性，100=極正面），只回傳一個整數：\n{news_text}"
                )
                msg = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=10,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw_score = (msg.content[0].text if msg.content else "50").strip()
                # 解析整數並夾限 0-100
                import re as _re
                digits = _re.findall(r"\d+", raw_score)
                if digits:
                    news_s = max(0, min(100, int(digits[0])))
    except Exception as e:
        err_str = str(e)
        if "credit balance is too low" in err_str or "402" in err_str:
            _mark_credit_exhausted()
            logger.warning(f"[health_score] Anthropic credit 耗盡")
        else:
            logger.warning(f"[health_score] news score {code}: {e}")
        news_s = 50

    if news_s >= 70:
        news_label = "正面"
    elif news_s >= 45:
        news_label = "中性"
    else:
        news_label = "負面"

    # ── 6. 加權總分 ───────────────────────────────────────────────────────────
    total = round(tech_s * 0.3 + chip_s * 0.3 + trend_s * 0.2 + news_s * 0.2)

    # 評級
    if total >= 80:
        rating = "🟢 優秀"
    elif total >= 65:
        rating = "🟡 良好"
    elif total >= 50:
        rating = "🟠 普通"
    else:
        rating = "🔴 偏弱"

    # 進度條
    bar = "█" * (total // 10) + "░" * (10 - total // 10)

    # MACD 方向箭頭
    macd_arrow = "↑" if macd_rising else "↓"

    # ── 7. 優缺點判斷 ─────────────────────────────────────────────────────────
    if chip_s >= 70:
        pros = "✅ 強勢籌碼支撐"
    elif trend_s >= 65:
        pros = "✅ 趨勢向上一致"
    else:
        pros = "✅ RSI 健康區間"

    if chip_s < 50:
        cons = "⚠️ 籌碼偏弱"
    elif trend_s < 50:
        cons = "⚠️ 趨勢偏弱"
    elif news_s < 40:
        cons = "⚠️ 新聞偏負面"
    else:
        cons = "⚠️ 技術面轉弱"

    return (
        f"💊 {code} 健康評分\n"
        f"現價：{current_close:.2f} 元\n"
        f"──────────────────────────\n"
        f"總分：{total}/100 {rating}\n"
        f"[{bar}]\n"
        f"──────────────────────────\n"
        f"📊 技術面 (30%)：{tech_s} 分\n"
        f"   └ RSI={rsi:.1f}, MACD={macd_arrow}, {ma_label}\n"
        f"🏦 籌碼面 (30%)：{chip_s} 分\n"
        f"   └ 外資{'買' if f_net > 0 else '賣'}{abs(f_net):,}張，"
        f"投信{'買' if t_net > 0 else '賣'}{abs(t_net):,}張\n"
        f"📈 趨勢   (20%)：{trend_s} 分\n"
        f"   └ 5日{r5:+.1f}% / 20日{r20:+.1f}% / 60日{r60:+.1f}%\n"
        f"📰 新聞   (20%)：{news_s} 分\n"
        f"   └ {news_label}新聞情緒\n"
        f"──────────────────────────\n"
        f"{pros}\n"
        f"{cons}"
    )


# ── 公開 async 包裝 ───────────────────────────────────────────────────────────

async def get_stock_health_score(code: str) -> str:
    """
    非同步包裝：在 executor 中執行同步的健康評分計算，避免阻塞 event loop。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_stock_health_score_sync, code)
