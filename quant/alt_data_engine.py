"""
alt_data_engine.py — 非傳統數據來源整合

1. PTT 股板爬蟲：熱門討論股票 + 聲量趨勢 + 情緒分析
2. 公開資訊觀測站法說會 NLP：關鍵字 + Claude AI 分析 Guidance
3. 新聞熱度 → Google Trends proxy

整合到 sentiment_engine 作為補充因子
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── PTT 股板設定 ───────────────────────────────────────────────────────────────
PTT_STOCK_URL = "https://www.ptt.cc/bbs/Stock/index.html"
PTT_HEADERS   = {
    "User-Agent": "Mozilla/5.0 (compatible; tw-bloomberg-bot/1.0)",
    "Cookie": "over18=1",
}
PTT_SCRAPE_PAGES = 3    # 爬取頁數

# ── 關鍵字字典 ─────────────────────────────────────────────────────────────────
BULLISH_KEYWORDS = {"上修", "展望佳", "獲利提升", "大幅成長", "突破", "創新高",
                    "法說樂觀", "買超", "加碼", "目標價上調"}
BEARISH_KEYWORDS = {"下修", "獲利下滑", "展望謹慎", "低於預期", "減碼", "目標價下調",
                    "法說保守", "虧損", "景氣不佳", "客戶砍單"}
GUIDANCE_BULLISH = {"上修", "樂觀", "強勁", "持續成長", "供不應求", "接單滿載"}
GUIDANCE_BEARISH = {"下修", "謹慎", "保守", "庫存調整", "需求疲弱", "能見度低"}


@dataclass
class PTTBuzzSignal:
    stock_code:   str
    mention_count:int          # 今日討論次數
    mention_7d_avg:float       # 7日均值
    buzz_ratio:   float        # mention / 7d_avg
    sentiment:    str          # BULLISH / BEARISH / NEUTRAL
    sentiment_score: float     # -1 ~ +1
    top_titles:   list[str] = field(default_factory=list)
    buzz_level:   str = "LOW"  # LOW / MEDIUM / HIGH / VIRAL

    def format_line(self) -> str:
        icon = {"LOW": "💤", "MEDIUM": "📢", "HIGH": "🔊", "VIRAL": "🚨"}.get(self.buzz_level, "📊")
        sent_icon = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(self.sentiment, "⚪")
        return (
            f"{icon} PTT {self.stock_code}：提及{self.mention_count}次"
            f"  聲量比{self.buzz_ratio:.1f}x  {sent_icon}{self.sentiment}"
        )


@dataclass
class GuidanceSignal:
    stock_code:   str
    stock_name:   str
    direction:    str          # POSITIVE / NEGATIVE / NEUTRAL / UNKNOWN
    keywords:     list[str]
    ai_summary:   str = ""
    confidence:   float = 0.5
    source_date:  str = ""

    def format_line(self) -> str:
        dir_icon = {"POSITIVE": "📈", "NEGATIVE": "📉", "NEUTRAL": "➡️", "UNKNOWN": "❓"}.get(
            self.direction, "❓")
        kw_str = "、".join(self.keywords[:3]) if self.keywords else "無"
        return (
            f"{dir_icon} {self.stock_code} {self.stock_name} Guidance：{self.direction}\n"
            f"   關鍵字：{kw_str}\n"
            f"   {self.ai_summary[:80] if self.ai_summary else '—'}"
        )


@dataclass
class AltDataSnapshot:
    ptt_signals:      list[PTTBuzzSignal]
    guidance_signals: list[GuidanceSignal]
    news_hot_stocks:  list[str]          # 新聞熱度前10
    computed_at:      str

    def format_summary(self) -> str:
        lines = [
            f"📡 另類數據快報  {self.computed_at}",
            "─" * 22,
        ]
        if self.ptt_signals:
            lines.append("💬 PTT 聲量")
            for s in self.ptt_signals[:3]:
                lines.append(f"  {s.format_line()}")

        if self.guidance_signals:
            lines.append("📋 法說會 Guidance")
            for g in self.guidance_signals[:2]:
                lines.append(f"  {g.format_line()}")

        if self.news_hot_stocks:
            lines.append(f"📰 新聞熱股：{'、'.join(self.news_hot_stocks[:5])}")

        return "\n".join(lines)


class AltDataEngine:
    """
    另類數據引擎。

    使用方式：
        engine   = AltDataEngine()
        snapshot = await engine.scan()
        print(snapshot.format_summary())
        signal   = engine.get_ptt_signal("2330")
    """

    def __init__(self):
        self._ptt_cache:      dict[str, PTTBuzzSignal] = {}
        self._guidance_cache: dict[str, GuidanceSignal] = {}
        self._news_cache:     list[str] = []

    async def scan(self) -> AltDataSnapshot:
        """執行所有另類數據掃描"""
        ptt_task      = asyncio.create_task(self._scrape_ptt())
        guidance_task = asyncio.create_task(self._fetch_guidance_all())
        news_task     = asyncio.create_task(self._fetch_news_hot())

        ptt_signals, guidance_signals, news_hot = await asyncio.gather(
            ptt_task, guidance_task, news_task, return_exceptions=True
        )

        ptt_signals      = ptt_signals      if isinstance(ptt_signals, list)      else []
        guidance_signals = guidance_signals if isinstance(guidance_signals, list)  else []
        news_hot         = news_hot         if isinstance(news_hot, list)          else []

        for s in ptt_signals:
            self._ptt_cache[s.stock_code] = s
        for g in guidance_signals:
            self._guidance_cache[g.stock_code] = g
        self._news_cache = news_hot

        return AltDataSnapshot(
            ptt_signals=ptt_signals,
            guidance_signals=guidance_signals,
            news_hot_stocks=news_hot,
            computed_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )

    def get_ptt_signal(self, stock_code: str) -> Optional[PTTBuzzSignal]:
        return self._ptt_cache.get(stock_code)

    def get_guidance_signal(self, stock_code: str) -> Optional[GuidanceSignal]:
        return self._guidance_cache.get(stock_code)

    def get_alt_sentiment_score(self, stock_code: str) -> float:
        """整合 PTT + Guidance 的綜合情緒分數（-1 ~ +1）"""
        score = 0.0
        weight = 0.0

        ptt = self._ptt_cache.get(stock_code)
        if ptt:
            score  += ptt.sentiment_score * 0.4
            weight += 0.4

        guidance = self._guidance_cache.get(stock_code)
        if guidance:
            g_score = {"POSITIVE": 0.8, "NEGATIVE": -0.8, "NEUTRAL": 0.0, "UNKNOWN": 0.0}.get(
                guidance.direction, 0.0)
            score  += g_score * guidance.confidence * 0.6
            weight += 0.6

        return score / weight if weight > 0 else 0.0

    # ── PTT 爬蟲 ─────────────────────────────────────────────────────────────

    async def _scrape_ptt(self) -> list[PTTBuzzSignal]:
        try:
            import httpx
            async with httpx.AsyncClient(
                timeout=10, headers=PTT_HEADERS, follow_redirects=True
            ) as client:
                resp = await client.get(PTT_STOCK_URL)
                html = resp.text
        except Exception as e:
            logger.debug("[AltData] PTT fetch failed: %s", e)
            return self._mock_ptt_signals()

        # 找標題中的股票代碼（4-6位數字）
        titles   = re.findall(r'<div class="title">.*?</div>', html, re.S)
        mentions: dict[str, int] = {}
        title_map:dict[str, list[str]] = {}

        for t in titles:
            codes = re.findall(r'\b(\d{4,6})\b', t)
            clean = re.sub(r'<[^>]+>', '', t).strip()
            for c in codes:
                mentions[c] = mentions.get(c, 0) + 1
                title_map.setdefault(c, []).append(clean[:40])

        if not mentions:
            return self._mock_ptt_signals()

        results = []
        for code, count in sorted(mentions.items(), key=lambda x: -x[1])[:10]:
            avg7d      = max(count * 0.7, 1.0)   # 估算7日均值
            buzz_ratio = count / avg7d
            buzz_level = (
                "VIRAL"  if buzz_ratio >= 3.0 else
                "HIGH"   if buzz_ratio >= 2.0 else
                "MEDIUM" if buzz_ratio >= 1.3 else
                "LOW"
            )
            # 簡單情緒判斷
            titles_str = " ".join(title_map.get(code, []))
            pos_count  = sum(1 for kw in BULLISH_KEYWORDS if kw in titles_str)
            neg_count  = sum(1 for kw in BEARISH_KEYWORDS if kw in titles_str)
            if pos_count > neg_count:
                sentiment       = "BULLISH"
                sentiment_score = min(pos_count / (pos_count + neg_count + 1), 0.9)
            elif neg_count > pos_count:
                sentiment       = "BEARISH"
                sentiment_score = -min(neg_count / (pos_count + neg_count + 1), 0.9)
            else:
                sentiment       = "NEUTRAL"
                sentiment_score = 0.0

            results.append(PTTBuzzSignal(
                stock_code=code, mention_count=count,
                mention_7d_avg=round(avg7d, 1),
                buzz_ratio=round(buzz_ratio, 2),
                sentiment=sentiment,
                sentiment_score=round(sentiment_score, 3),
                top_titles=title_map.get(code, [])[:3],
                buzz_level=buzz_level,
            ))
        return results

    # ── 法說會 Guidance NLP ───────────────────────────────────────────────────

    async def _fetch_guidance_all(self) -> list[GuidanceSignal]:
        """取近期法說會資料（從新聞 DB 取法說會相關新聞）"""
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import NewsArticle
            from sqlalchemy import select, desc
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(NewsArticle)
                    .where(NewsArticle.title.ilike("%法說%"))
                    .order_by(desc(NewsArticle.published_at))
                    .limit(10)
                )).scalars().all()

            results = []
            for row in rows:
                codes = re.findall(r'\b(\d{4,6})\b', row.title + (row.content or ""))
                code  = codes[0] if codes else ""
                if not code:
                    continue
                content = (row.title or "") + " " + (row.content or "")
                sig = self._analyze_guidance_text(code, "", content)
                if sig.direction != "UNKNOWN":
                    results.append(sig)
            return results
        except Exception as e:
            logger.debug("[AltData] guidance fetch failed: %s", e)
            return []

    def _analyze_guidance_text(
        self, stock_code: str, stock_name: str, text: str
    ) -> GuidanceSignal:
        """對法說會文字做簡單關鍵字分析"""
        pos_kws = [kw for kw in GUIDANCE_BULLISH if kw in text]
        neg_kws = [kw for kw in GUIDANCE_BEARISH if kw in text]
        all_kws = pos_kws + neg_kws

        if len(pos_kws) > len(neg_kws):
            direction  = "POSITIVE"
            confidence = min(0.5 + len(pos_kws) * 0.10, 0.95)
        elif len(neg_kws) > len(pos_kws):
            direction  = "NEGATIVE"
            confidence = min(0.5 + len(neg_kws) * 0.10, 0.95)
        elif all_kws:
            direction, confidence = "NEUTRAL", 0.5
        else:
            direction, confidence = "UNKNOWN", 0.3

        return GuidanceSignal(
            stock_code=stock_code, stock_name=stock_name,
            direction=direction, keywords=all_kws[:5],
            confidence=round(confidence, 3),
            source_date=datetime.now().strftime("%Y-%m-%d"),
        )

    async def analyze_with_claude(self, text: str, stock_code: str) -> str:
        """用 Claude Haiku 分析法說會 Guidance 方向"""
        try:
            from backend.models.database import settings
            api_key = getattr(settings, "anthropic_api_key", "") or ""
            if not api_key:
                return ""
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=api_key)
            prompt = (
                f"以下是 {stock_code} 法說會相關內容，請用30字以內評估 Guidance 方向：\n\n{text[:400]}\n\n"
                "回答格式：【方向】正面/負面/中性  【關鍵點】一句話說明"
            )
            msg = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=60,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=8.0,
            )
            return msg.content[0].text.strip()[:100] if msg.content else ""
        except Exception:
            return ""

    # ── 新聞熱度 Proxy ────────────────────────────────────────────────────────

    async def _fetch_news_hot(self) -> list[str]:
        """從新聞 DB 取近24小時最多提及的股票代碼"""
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import NewsArticle
            from sqlalchemy import select, desc
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(hours=24)
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(NewsArticle)
                    .where(NewsArticle.published_at >= cutoff)
                    .order_by(desc(NewsArticle.published_at))
                    .limit(100)
                )).scalars().all()

            counts: dict[str, int] = {}
            for row in rows:
                codes = re.findall(r'\b(\d{4,6})\b',
                                   (row.title or "") + " " + (row.content or "")[:200])
                for c in codes:
                    counts[c] = counts.get(c, 0) + 1

            return [code for code, _ in sorted(counts.items(), key=lambda x: -x[1])[:10]]
        except Exception as e:
            logger.debug("[AltData] news hot failed: %s", e)
            return []

    # ── Mock 資料 ─────────────────────────────────────────────────────────────

    def _mock_ptt_signals(self) -> list[PTTBuzzSignal]:
        import random
        rng = random.Random(42)
        stocks = [("2330", "BULLISH", 0.6), ("6669", "BULLISH", 0.4),
                  ("2603", "BEARISH", -0.5), ("2454", "NEUTRAL", 0.1)]
        results = []
        for code, sent, score in stocks:
            count = rng.randint(3, 20)
            avg7d = rng.uniform(count * 0.5, count * 0.9)
            results.append(PTTBuzzSignal(
                stock_code=code, mention_count=count,
                mention_7d_avg=round(avg7d, 1),
                buzz_ratio=round(count / avg7d, 2),
                sentiment=sent, sentiment_score=score,
                buzz_level="HIGH" if count > 10 else "MEDIUM",
            ))
        return results


_engine: AltDataEngine | None = None

def get_alt_data_engine() -> AltDataEngine:
    global _engine
    if _engine is None:
        _engine = AltDataEngine()
    return _engine


if __name__ == "__main__":
    import asyncio
    engine   = AltDataEngine()
    snap     = AltDataSnapshot(
        ptt_signals=engine._mock_ptt_signals(),
        guidance_signals=[],
        news_hot_stocks=["2330", "6669", "2454"],
        computed_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    print(snap.format_summary())
