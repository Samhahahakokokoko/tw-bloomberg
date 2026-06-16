"""Contrarian Service — 市場反向指標追蹤"""
from __future__ import annotations

import time
from loguru import logger
import asyncio

_cache: dict = {}
_cache_ts: float = 0.0
_TTL = 1800  # 30 分鐘


async def get_contrarian() -> dict:
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _cache
    result = await _fetch_contrarian()
    _cache = result
    _cache_ts = now
    return result


async def _fetch_contrarian() -> dict:
    import httpx, asyncio

    # ── 指標1：散戶情緒（融資融券比 + 散戶買賣超）──────────────────────────
    async def fetch_retail_sentiment():
        try:
            # 用 TWII 近期走勢 + 成交量大小作為散戶情緒代理
            url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(url, params={"interval": "1d", "range": "1mo"},
                                headers={"User-Agent": "Mozilla/5.0"})
            data   = r.json()["chart"]["result"][0]["indicators"]["quote"][0]
            closes = [x for x in data.get("close", []) if x is not None]
            vols   = [x for x in data.get("volume", []) if x is not None]
            if len(closes) < 10:
                return {"score": 50, "signal": "中性", "desc": "資料不足"}

            # 散戶情緒：近5日漲多量縮 → 散戶樂觀（反向看空）；跌多量縮 → 散戶悲觀（反向看多）
            recent_ret = (closes[-1] - closes[-5]) / closes[-5] * 100 if closes[-5] else 0
            vol_ratio  = sum(vols[-3:]) / sum(vols[-10:-7]) if sum(vols[-10:-7]) > 0 else 1
            # 散戶樂觀指數：上漲+量縮=散戶追高（高分=看空信號）
            if recent_ret > 5 and vol_ratio < 0.9:
                score, signal = 80, "散戶追高（反向看空）"
                desc = f"大盤近5日漲{recent_ret:.1f}%但量縮，散戶可能過度樂觀"
            elif recent_ret < -5 and vol_ratio < 0.9:
                score, signal = 20, "散戶恐慌（反向看多）"
                desc = f"大盤近5日跌{recent_ret:.1f}%且量縮，散戶極度悲觀"
            elif recent_ret > 2:
                score, signal = 65, "偏樂觀"
                desc = f"大盤近5日上漲{recent_ret:.1f}%，市場氣氛偏正向"
            elif recent_ret < -2:
                score, signal = 35, "偏悲觀"
                desc = f"大盤近5日下跌{recent_ret:.1f}%，市場氣氛轉弱"
            else:
                score, signal = 50, "中性"
                desc = f"大盤近5日漲跌{recent_ret:.1f}%，情緒中性"
            return {"score": score, "signal": signal, "desc": desc}
        except Exception as e:
            logger.debug(f"[contrarian] retail: {e}")
            return {"score": 50, "signal": "中性", "desc": "無法取得資料"}

    # ── 指標2：媒體封面效應（新聞關注度代理）─────────────────────────────────
    async def fetch_media_effect():
        try:
            # 抓台積電相關新聞量（高度媒體關注往往是高點信號）
            url = "https://query1.finance.yahoo.com/v1/finance/search"
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(url, params={"q": "台股 2330 台積電 創新高", "newsCount": 10},
                                headers={"User-Agent": "Mozilla/5.0"})
            news = r.json().get("news", [])
            # 新聞中包含「創新高」「破紀錄」「瘋狂」等詞彙的數量
            hype_keywords = ["新高", "歷史", "破紀錄", "瘋狂", "狂飆", "暴漲", "飆股"]
            hype_cnt = sum(
                1 for n in news
                if any(kw in n.get("title", "") for kw in hype_keywords)
            )
            if hype_cnt >= 3:
                score, signal = 75, "媒體高度追捧（高點警示）"
                desc = f"近期新聞中有 {hype_cnt} 則含「創新高/暴漲」字眼，媒體封面效應警示"
            elif hype_cnt >= 1:
                score, signal = 55, "媒體輕度追捧"
                desc = f"近期有 {hype_cnt} 則新聞提及強勢字眼，尚在正常範圍"
            else:
                score, signal = 30, "媒體關注不足（可能是逢低機會）"
                desc = "近期媒體對台股關注低，反向往往是進場良機"
            return {"score": score, "signal": signal, "desc": desc, "hype_cnt": hype_cnt}
        except Exception as e:
            logger.debug(f"[contrarian] media: {e}")
            return {"score": 50, "signal": "中性", "desc": "無法取得資料", "hype_cnt": 0}

    # ── 指標3：分析師一致性（估計分析師過度看多/看空）────────────────────────
    async def fetch_analyst_consensus():
        try:
            # 用 2330 分析師目標價 vs 現價的差距作為代理
            url = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/2330.TW"
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(url, params={"modules": "financialData,recommendationTrend"},
                                headers={"User-Agent": "Mozilla/5.0"})
            fd = r.json()["quoteSummary"]["result"][0].get("financialData", {})
            price  = fd.get("currentPrice", {}).get("raw", 0)
            target = fd.get("targetMeanPrice", {}).get("raw", 0)
            if price and target:
                upside = (target - price) / price * 100
                # 若分析師目標價上漲空間 < 5%，代表已全面看多（反向警示）
                if upside < 5:
                    score, signal = 80, "分析師一致看多（警示）"
                    desc = f"分析師目標價僅較現價高 {upside:.1f}%，上調空間有限，過度共識"
                elif upside > 30:
                    score, signal = 25, "分析師目標價偏保守（潛在空間大）"
                    desc = f"分析師目標價較現價高 {upside:.1f}%，市場尚未充分定價"
                else:
                    score, signal = 50, "分析師看法適中"
                    desc = f"分析師目標價較現價高 {upside:.1f}%，共識合理"
                return {"score": score, "signal": signal, "desc": desc, "upside": round(upside, 1)}
            return {"score": 50, "signal": "中性", "desc": "無法取得分析師資料", "upside": 0}
        except Exception as e:
            logger.debug(f"[contrarian] analyst: {e}")
            return {"score": 50, "signal": "中性", "desc": "無法取得分析師資料", "upside": 0}

    # ── 指標4：恐慌貪婪（複用 feargreed 概念）────────────────────────────────
    async def fetch_vix_proxy():
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX"
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(url, params={"interval": "1d", "range": "1mo"},
                                headers={"User-Agent": "Mozilla/5.0"})
            closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", [])
            closes = [x for x in closes if x is not None]
            if closes:
                vix = closes[-1]
                if vix > 30:
                    score, signal = 20, "VIX 極度恐慌（反向看多）"
                    desc = f"VIX={vix:.1f}，市場極度恐慌，歷史上往往是最佳買點"
                elif vix > 20:
                    score, signal = 40, "VIX 偏高（市場緊張）"
                    desc = f"VIX={vix:.1f}，市場情緒偏緊張，逢跌可考慮布局"
                elif vix < 13:
                    score, signal = 80, "VIX 極低（過度自滿警示）"
                    desc = f"VIX={vix:.1f}，市場過度自信，需提高警覺"
                else:
                    score, signal = 50, "VIX 正常"
                    desc = f"VIX={vix:.1f}，市場情緒正常"
                return {"score": score, "signal": signal, "desc": desc, "vix": round(vix, 1)}
            return {"score": 50, "signal": "中性", "desc": "無法取得 VIX", "vix": 0}
        except Exception as e:
            logger.debug(f"[contrarian] vix: {e}")
            return {"score": 50, "signal": "中性", "desc": "無法取得 VIX", "vix": 0}

    retail, media, analyst, vix_data = await asyncio.gather(
        fetch_retail_sentiment(), fetch_media_effect(),
        fetch_analyst_consensus(), fetch_vix_proxy(),
        return_exceptions=True
    )

    def safe(d, default):
        return d if isinstance(d, dict) else default

    retail   = safe(retail,   {"score": 50, "signal": "中性", "desc": ""})
    media    = safe(media,    {"score": 50, "signal": "中性", "desc": ""})
    analyst  = safe(analyst,  {"score": 50, "signal": "中性", "desc": ""})
    vix_data = safe(vix_data, {"score": 50, "signal": "中性", "desc": ""})

    # 綜合反向指標分數（越高 = 越多人樂觀 = 反向越危險）
    composite = (
        retail.get("score", 50)   * 0.30 +
        media.get("score", 50)    * 0.20 +
        analyst.get("score", 50)  * 0.25 +
        vix_data.get("score", 50) * 0.25
    )
    composite = round(composite, 1)

    if composite >= 70:
        overall_signal = "🔴 反向指標看空（市場過度樂觀，需謹慎）"
        overall_action = "建議降低持倉至 3-5 成，等待回調再加碼"
    elif composite >= 55:
        overall_signal = "🟡 輕度反向訊號（偏多但需觀察）"
        overall_action = "維持持倉，注意設定移動停損保護獲利"
    elif composite <= 30:
        overall_signal = "🟢 強烈反向看多（市場極度悲觀，逆向布局機會）"
        overall_action = "市場情緒悲觀至極，往往是絕佳買點，建議分批建倉"
    elif composite <= 45:
        overall_signal = "🟢 輕度反向看多（偏空但可開始觀察買點）"
        overall_action = "市場偏悲觀，開始關注強勢個股的支撐區"
    else:
        overall_signal = "⬜ 反向指標中性（無明顯信號）"
        overall_action = "市場情緒中性，依個股基本面操作即可"

    return {
        "composite":      composite,
        "overall_signal": overall_signal,
        "overall_action": overall_action,
        "retail":         retail,
        "media":          media,
        "analyst":        analyst,
        "vix":            vix_data,
        "updated_at":     time.strftime("%Y-%m-%d %H:%M"),
    }


def format_contrarian_report(data: dict) -> str:
    if data.get("error"):
        return f"❌ {data['error']}"

    comp    = data.get("composite", 50)
    sig     = data.get("overall_signal", "")
    action  = data.get("overall_action", "")
    retail  = data.get("retail", {})
    media   = data.get("media", {})
    analyst = data.get("analyst", {})
    vix_d   = data.get("vix", {})
    updated = data.get("updated_at", "")

    def score_bar(score):
        filled = int(score / 10)
        return "█" * filled + "░" * (10 - filled)

    lines = [
        "🔁 市場反向指標追蹤",
        "─" * 32, "",
        f"綜合反向指標：{comp:.0f}/100",
        f"{sig}",
        f"更新：{updated}",
        "",
        f"（0=極度悲觀/最佳買點  100=極度樂觀/賣點）",
        "",
        "── 細項指標 ──",
        "",
    ]

    for label, d in [("散戶情緒", retail), ("媒體封面", media),
                     ("分析師共識", analyst), ("VIX恐慌", vix_d)]:
        sc  = d.get("score", 50)
        bar = score_bar(sc)
        lines += [
            f"  {label}：[{bar}] {sc}/100",
            f"  → {d.get('signal', '')}",
            f"     {d.get('desc', '')}",
            "",
        ]

    lines += [
        "─" * 28,
        "🤖 AI 反向操作建議",
        action,
        "",
        "📌 反向指標使用提醒：",
        "  • 反向指標為情緒輔助工具，需配合技術面確認",
        "  • 極值信號（<20 或 >80）最具參考價值",
        "  • 市場可以長期非理性，需設定進出場條件",
        "",
        "輸入 /feargreed 恐慌貪婪 | /chiphealth 籌碼健康 | /wisdom 每日智慧",
    ]
    return "\n".join(lines)
