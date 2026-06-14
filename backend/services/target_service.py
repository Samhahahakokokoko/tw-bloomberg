"""目標價預測服務 — 技術分析 + 籌碼 + 新聞 + Claude AI

get_target_price_sync(code) 為同步函式，由呼叫方透過 run_in_executor 執行。
"""

import os
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from ta.momentum import RSIIndicator
from bs4 import BeautifulSoup
import anthropic
from dotenv import load_dotenv
from loguru import logger

from backend.utils.credit_guard import is_exhausted

load_dotenv()

_ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def get_target_price_sync(code: str) -> str:
    """同步版本：下載股價、計算指標、抓籌碼與新聞，呼叫 Claude 給出目標價預測。"""
    try:
        if is_exhausted():
            return "❌ AI 配額暫時耗盡，請稍後再試"

        # ── 1. 下載 6 個月股價 ────────────────────────────────────────────────
        end_date = datetime.today()
        start_date = end_date - timedelta(days=182)

        ticker_tw = f"{code}.TW"
        ticker_two = f"{code}.TWO"

        df = yf.download(ticker_tw, start=start_date.strftime("%Y-%m-%d"),
                         end=end_date.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
        if df.empty:
            df = yf.download(ticker_two, start=start_date.strftime("%Y-%m-%d"),
                             end=end_date.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
        if df.empty:
            return f"❌ 目標價預測失敗：無法取得 {code} 的股價資料"

        # 處理 MultiIndex 欄位（yfinance >= 0.2.x）
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        close_series = df["Close"].dropna()
        if len(close_series) < 20:
            return f"❌ 目標價預測失敗：{code} 資料筆數不足"

        close = float(close_series.iloc[-1])

        # ── 2. 技術指標計算 ───────────────────────────────────────────────────
        # RSI (14)
        rsi_indicator = RSIIndicator(close=close_series, window=14)
        rsi_series = rsi_indicator.rsi().dropna()
        rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty else float("nan")

        # MA20 / MA60
        ma20 = float(close_series.rolling(20).mean().iloc[-1]) if len(close_series) >= 20 else float("nan")
        ma60 = float(close_series.rolling(60).mean().iloc[-1]) if len(close_series) >= 60 else float("nan")

        # Support / Resistance (60-day low/high)
        tail60 = close_series.iloc[-60:] if len(close_series) >= 60 else close_series
        support = float(tail60.min())
        resistance = float(tail60.max())

        # Pivot Points R1 / S1（使用最後一根 K 棒的 high/low/close）
        last_high = float(df["High"].dropna().iloc[-1])
        last_low = float(df["Low"].dropna().iloc[-1])
        last_close_pivot = float(df["Close"].dropna().iloc[-1])
        pivot = (last_high + last_low + last_close_pivot) / 3
        r1 = 2 * pivot - last_low
        s1 = 2 * pivot - last_high

        # ── 3. FinMind 三大法人籌碼（近 30 天） ──────────────────────────────
        fm_start = (datetime.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        fm_end = datetime.today().strftime("%Y-%m-%d")
        foreign_net_lots = 0
        trust_net_lots = 0

        try:
            fm_url = "https://api.finmindtrade.com/api/v4/data"
            fm_params = {
                "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
                "data_id": code,
                "start_date": fm_start,
                "end_date": fm_end,
            }
            fm_resp = requests.get(fm_url, params=fm_params, timeout=15)
            fm_resp.raise_for_status()
            fm_data = fm_resp.json().get("data", [])

            foreign_names = {"Foreign_Investor", "Foreign_Dealer_Self"}
            trust_names = {"Investment_Trust"}

            for row in fm_data:
                name = row.get("name", "")
                buy = int(row.get("buy", 0) or 0)
                sell = int(row.get("sell", 0) or 0)
                net = buy - sell
                if name in foreign_names:
                    foreign_net_lots += net
                elif name in trust_names:
                    trust_net_lots += net
        except Exception as e:
            logger.debug(f"[target] FinMind 籌碼抓取失敗 {code}: {e}")

        # ── 4. 抓取 Yahoo 新聞標題 ────────────────────────────────────────────
        news_titles: list[str] = []
        try:
            news_url = f"https://tw.stock.yahoo.com/quote/{code}.TW/news"
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            }
            news_resp = requests.get(news_url, headers=headers, timeout=10)
            soup = BeautifulSoup(news_resp.text, "html.parser")
            h3_tags = soup.find_all("h3")
            for tag in h3_tags[:6]:
                title = tag.get_text(strip=True)
                if title:
                    news_titles.append(title)
        except Exception as e:
            logger.debug(f"[target] Yahoo 新聞抓取失敗 {code}: {e}")

        news_str = "\n".join(f"- {t}" for t in news_titles) if news_titles else "（無法取得新聞）"

        # ── 5. 組合 Claude 提示詞 ─────────────────────────────────────────────
        prompt = f"""你是一位台股分析師，請根據以下資料預測 {code} 未來 1 個月的目標價。

技術指標：
- 現價：{close:.2f} 元
- RSI(14)：{rsi:.1f}
- MA20：{ma20:.2f} 元
- MA60：{ma60:.2f} 元
- 支撐（60日低）：{support:.2f} 元
- 壓力（60日高）：{resistance:.2f} 元
- 樞紐 R1：{r1:.2f} 元 / S1：{s1:.2f} 元

近 30 日法人籌碼（千股）：
- 外資淨買賣：{foreign_net_lots:+,}
- 投信淨買賣：{trust_net_lots:+,}

最新新聞：
{news_str}

請用繁體中文，以下格式回答，總字數不超過 250 字：
🎯 樂觀目標價：X元（理由）
⚖️ 中性目標價：X元（理由）
🛡️ 悲觀目標價：X元（理由）
📊 信心分數：X/100（綜合評估）
📌 最大風險：
📌 最大催化劑："""

        # ── 6. 呼叫 Claude API ────────────────────────────────────────────────
        client = anthropic.Anthropic(api_key=_ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        ai_result = msg.content[0].text.strip()

        # ── 7. 組合回傳字串 ───────────────────────────────────────────────────
        return (
            f"🎯 {code} 1個月目標價預測\n"
            f"現價：{close:.2f} 元\n"
            f"─────\n"
            f"{ai_result}"
        )

    except Exception as e:
        logger.error(f"[target] get_target_price_sync({code}) 失敗: {e}")
        return f"❌ 目標價預測失敗：{e}"


async def get_target_price(code: str) -> str:
    """非同步包裝：供 FastAPI handler 直接 await 使用（或由 run_in_executor 呼叫 sync 版本）。"""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_target_price_sync, code)
