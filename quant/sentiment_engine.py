"""
sentiment_engine.py — 族群情緒 + Buzz Score 引擎

資料來源：
  1. 現有 news_articles DB（已爬取的財經新聞）
  2. PTT 股板爬蟲（/bbs/Stock/index.html）—— 輕量 HTTP 爬取，無瀏覽器依賴
  3. 關鍵字對應族群（SECTOR_KEYWORDS）

輸出格式：
  {
    "sector":    "AI Server",
    "sentiment": 0.82,         # -1 ~ +1
    "buzz":      "HIGH",       # LOW / MEDIUM / HIGH / VIRAL
    "signal":    "BULLISH",    # BULLISH / BEARISH / NEUTRAL
    "mentions":  142,
  }
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── 族群 → 關鍵字對照 ──────────────────────────────────────────────────────

SECTOR_KEYWORDS: dict[str, list[str]] = {
    "AI Server":    ["AI", "輝達", "NVIDIA", "GB200", "CoWoS", "HBM", "算力", "AI伺服器", "散熱"],
    "半導體":       ["台積電", "TSMC", "晶圓", "先進封裝", "2nm", "3nm", "CoWoS", "矽智財"],
    "電動車":       ["特斯拉", "Tesla", "比亞迪", "BYD", "電池", "電動車", "充電樁", "EV"],
    "金融":         ["升息", "降息", "FED", "央行", "殖利率", "銀行", "保險", "ETF"],
    "航運":         ["長榮", "陽明", "萬海", "貨櫃", "運費", "BDI", "散裝"],
    "傳產鋼鐵":     ["中鋼", "燦坤", "鋼鐵", "銅", "原物料", "油價"],
    "生技醫療":     ["新藥", "臨床試驗", "FDA", "生技", "醫療器材", "癌症"],
    "電信":         ["5G", "中華電", "台哥大", "遠傳", "頻寬", "雲端"],
    "消費零售":     ["百貨", "零售", "電商", "momo", "蝦皮", "節慶消費"],
    "太陽能":       ["太陽能", "綠能", "風電", "離岸風電", "儲能"],
}

BUZZ_THRESHOLDS = {"LOW": 10, "MEDIUM": 30, "HIGH": 80, "VIRAL": 200}

SENTIMENT_POSITIVE_WORDS = {
    "大漲", "強勢", "突破", "創高", "多頭", "買超", "強買", "跳空", "轉強",
    "上攻", "噴出", "飆漲", "加碼", "佈局", "長紅", "主力介入", "外資買",
}
SENTIMENT_NEGATIVE_WORDS = {
    "大跌", "崩跌", "賣超", "外資賣", "跌破", "悲觀", "拋售", "熊市",
    "下殺", "爆量下跌", "恐慌", "止損", "停損", "轉弱", "打壓",
}


@dataclass
class SectorSentiment:
    sector:    str
    sentiment: float      # -1 ~ +1（加權平均）
    buzz:      str        # LOW/MEDIUM/HIGH/VIRAL
    signal:    str        # BULLISH/BEARISH/NEUTRAL
    mentions:  int
    pos_count: int
    neg_count: int
    top_titles: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sector":     self.sector,
            "sentiment":  round(self.sentiment, 3),
            "buzz":       self.buzz,
            "signal":     self.signal,
            "mentions":   self.mentions,
            "pos_count":  self.pos_count,
            "neg_count":  self.neg_count,
            "top_titles": self.top_titles[:3],
        }


class SentimentEngine:
    """
    族群情緒引擎：結合 DB 新聞 + PTT 爬蟲計算每族群的情緒分數與 Buzz。

    使用方式：
        engine = SentimentEngine()
        results = await engine.analyze_all(days=3)
        for r in results:
            print(r.to_dict())
    """

    def __init__(self, ptt_timeout: float = 10.0):
        self.ptt_timeout = ptt_timeout

    # ── 主入口 ──────────────────────────────────────────────────────────────

    async def analyze_all(self, days: int = 3) -> list[SectorSentiment]:
        """分析所有族群情緒，回傳列表（依 buzz 排序）"""
        # Step 1: 從 DB 取近 N 日新聞
        news_items = await self._fetch_db_news(days)

        # Step 2: 從 PTT 取近期文章標題
        ptt_items  = await self._fetch_ptt_titles()

        all_texts = news_items + ptt_items

        # Step 3: 計算各族群
        results: list[SectorSentiment] = []
        for sector, keywords in SECTOR_KEYWORDS.items():
            sr = self._score_sector(sector, keywords, all_texts)
            results.append(sr)

        # 依 buzz 程度排序（VIRAL > HIGH > MEDIUM > LOW）
        buzz_order = {"VIRAL": 3, "HIGH": 2, "MEDIUM": 1, "LOW": 0}
        results.sort(key=lambda r: (buzz_order.get(r.buzz, 0), abs(r.sentiment)), reverse=True)
        return results

    async def analyze_sector(self, sector: str, days: int = 3) -> Optional[SectorSentiment]:
        """分析單一族群"""
        if sector not in SECTOR_KEYWORDS:
            return None
        news_items = await self._fetch_db_news(days)
        ptt_items  = await self._fetch_ptt_titles()
        return self._score_sector(sector, SECTOR_KEYWORDS[sector], news_items + ptt_items)

    # ── 資料來源 1：DB 新聞 ──────────────────────────────────────────────────

    async def _fetch_db_news(self, days: int = 3) -> list[str]:
        """從 news_articles 取近 N 日標題 + 摘要"""
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import NewsArticle
            from sqlalchemy import select
            cutoff = datetime.utcnow() - timedelta(days=days)
            async with AsyncSessionLocal() as db:
                r = await db.execute(
                    select(NewsArticle.title, NewsArticle.content)
                    .where(NewsArticle.created_at >= cutoff)
                    .limit(200)
                )
                rows = r.fetchall()
            return [f"{row[0]} {(row[1] or '')[:100]}" for row in rows]
        except Exception as e:
            logger.debug("[Sentiment] DB news fetch failed: %s", e)
            return []

    # ── 資料來源 2：PTT 股板 ─────────────────────────────────────────────────

    async def _fetch_ptt_titles(self) -> list[str]:
        """
        爬取 PTT Stock 板最新文章標題（輕量 HTTP，不需 Selenium）。
        失敗時靜默回傳空 list。
        """
        titles: list[str] = []
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; TW-Bloomberg/1.0)",
                "Cookie":     "over18=1",
            }
            async with httpx.AsyncClient(
                timeout=self.ptt_timeout,
                headers=headers,
                follow_redirects=True,
            ) as client:
                # 取最新 2 頁
                for page_url in [
                    "https://www.ptt.cc/bbs/Stock/index.html",
                    "https://www.ptt.cc/bbs/Stock/index1.html",
                ]:
                    try:
                        r = await client.get(page_url)
                        if r.status_code != 200:
                            continue
                        # 擷取文章標題（.title a 的文字）
                        found = re.findall(
                            r'<div class="title">\s*(?:<a[^>]+>)?([^<\n]+)(?:</a>)?',
                            r.text
                        )
                        titles.extend([t.strip() for t in found if t.strip() and t.strip() != "(本文已被刪除)"])
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("[Sentiment] PTT fetch failed: %s", e)
        return titles[:100]

    # ── 評分邏輯 ────────────────────────────────────────────────────────────

    def _score_sector(
        self,
        sector:   str,
        keywords: list[str],
        texts:    list[str],
    ) -> SectorSentiment:
        """計算單族群的情緒分數與 Buzz"""
        matched_texts:   list[str] = []
        pos_count = neg_count = 0

        for text in texts:
            upper = text.upper()
            # 任一關鍵字出現即算命中
            if any(kw.upper() in upper for kw in keywords):
                matched_texts.append(text)
                pos = sum(1 for w in SENTIMENT_POSITIVE_WORDS if w in text)
                neg = sum(1 for w in SENTIMENT_NEGATIVE_WORDS if w in text)
                pos_count += pos
                neg_count += neg

        mentions = len(matched_texts)

        # Buzz 等級
        buzz = "LOW"
        for level in ("VIRAL", "HIGH", "MEDIUM"):
            if mentions >= BUZZ_THRESHOLDS[level]:
                buzz = level
                break

        # 情緒分數 (-1 ~ +1)
        total_sentiment = pos_count + neg_count
        if total_sentiment > 0:
            sentiment = (pos_count - neg_count) / total_sentiment
        else:
            sentiment = 0.0

        # 訊號
        if sentiment >= 0.3 and buzz in ("HIGH", "VIRAL"):
            signal = "BULLISH"
        elif sentiment <= -0.3:
            signal = "BEARISH"
        else:
            signal = "NEUTRAL"

        # 取前3則代表標題
        top_titles = [t[:60] for t in matched_texts[:3]]

        return SectorSentiment(
            sector=sector,
            sentiment=round(sentiment, 4),
            buzz=buzz,
            signal=signal,
            mentions=mentions,
            pos_count=pos_count,
            neg_count=neg_count,
            top_titles=top_titles,
        )

    def analyze_from_texts(
        self,
        sector:  str,
        texts:   list[str],
    ) -> Optional[SectorSentiment]:
        """直接傳入文字列表分析（同步，供 pipeline 使用）"""
        if sector not in SECTOR_KEYWORDS:
            return None
        return self._score_sector(sector, SECTOR_KEYWORDS[sector], texts)

    def get_top_bullish(self, results: list[SectorSentiment], n: int = 3) -> list[SectorSentiment]:
        return sorted(
            [r for r in results if r.signal == "BULLISH"],
            key=lambda r: r.sentiment * (1 + (len(r.top_titles) / 10)),
            reverse=True,
        )[:n]

    def get_top_bearish(self, results: list[SectorSentiment], n: int = 3) -> list[SectorSentiment]:
        return sorted(
            [r for r in results if r.signal == "BEARISH"],
            key=lambda r: r.sentiment,
        )[:n]

    def format_for_line(self, results: list[SectorSentiment], top_n: int = 5) -> str:
        """格式化為 LINE 訊息"""
        if not results:
            return "📊 暫無族群情緒資料"
        lines = [f"📊 族群情緒排行（{datetime.now().strftime('%m/%d %H:%M')}）", "─" * 22]
        buzz_icons = {"VIRAL": "🔥", "HIGH": "📈", "MEDIUM": "📊", "LOW": "😴"}
        signal_icons = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}
        for r in results[:top_n]:
            bi = buzz_icons.get(r.buzz, "📊")
            si = signal_icons.get(r.signal, "🟡")
            lines.append(
                f"{bi}{si} {r.sector:10s} "
                f"情緒{r.sentiment:+.2f}  提及{r.mentions}次  {r.buzz}"
            )
        return "\n".join(lines)


_global_sentiment: Optional[SentimentEngine] = None


def get_sentiment_engine() -> SentimentEngine:
    global _global_sentiment
    if _global_sentiment is None:
        _global_sentiment = SentimentEngine()
    return _global_sentiment


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def _test():
        engine = SentimentEngine()

        # 測試：用 mock 文字
        mock_texts = [
            "台積電 CoWoS 需求爆炸，AI 伺服器訂單大增，外資買超強勢",
            "NVIDIA GB200 供應緊張，散熱族群受益，長紅突破",
            "長榮航運運費下滑，賣超壓力大，轉弱跌破支撐",
            "生技新藥臨床試驗失敗，崩跌恐慌賣壓",
        ] * 15   # 乘15模擬足夠文本量

        print("=== Sector Sentiment 測試 ===")
        for sector in ["AI Server", "半導體", "航運", "生技醫療"]:
            r = engine.analyze_from_texts(sector, mock_texts)
            if r:
                print(f"  {r.sector:12s} sentiment={r.sentiment:+.3f}  "
                      f"buzz={r.buzz:8s}  signal={r.signal}  mentions={r.mentions}")

    asyncio.run(_test())
