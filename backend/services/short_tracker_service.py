"""Short Tracker Service — 融券餘額追蹤 / 軋空潛力分析"""
from __future__ import annotations

import time
from loguru import logger
import asyncio

_cache: dict = {}
_cache_ts: float = 0.0
_TTL = 1800  # 30 分鐘


async def get_short_tracker() -> dict:
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _cache
    result = await _fetch_short_data()
    _cache = result
    _cache_ts = now
    return result


async def _fetch_short_data() -> dict:
    import httpx, asyncio

    # 監測標的清單（融券較多或短空壓力明顯的個股）
    _WATCH_LIST = [
        ("2330", "台積電"),  ("2317", "鴻海"),   ("2454", "聯發科"),
        ("2382", "廣達"),    ("2308", "台達電"),  ("3008", "大立光"),
        ("2303", "聯電"),    ("2412", "中華電"),  ("6669", "緯穎"),
        ("3443", "創意"),    ("2379", "瑞昱"),    ("2357", "華碩"),
        ("2609", "陽明"),    ("2615", "萬海"),    ("2376", "技嘉"),
        ("2881", "富邦金"),  ("2882", "國泰金"),  ("2886", "兆豐金"),
        ("2337", "旺宏"),    ("3037", "欣興"),
    ]

    async def fetch_one(code: str, name: str):
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(url, params={"interval": "1d", "range": "10d"},
                                headers={"User-Agent": "Mozilla/5.0"})
            q = r.json()["chart"]["result"][0]["indicators"]["quote"][0]
            closes = [x for x in q.get("close", []) if x is not None]
            vols   = [x for x in q.get("volume", []) if x is not None]

            if len(closes) < 2:
                return None

            price    = closes[-1]
            chg_pct  = (closes[-1] - closes[-2]) / closes[-2] * 100
            vol_now  = vols[-1] if vols else 0
            vol_avg  = sum(vols[-5:]) / min(5, len(vols)) if vols else 1

            # 估算短空壓力（用價格動能反向 + 成交量異常）
            # 連跌天數
            down_days = 0
            for i in range(len(closes) - 1, 0, -1):
                if closes[i] < closes[i - 1]:
                    down_days += 1
                else:
                    break

            # 估算融券張數（實際需 TWSE API；此處用統計模型近似）
            # 融券比 ≈ (成交量異常倍數) × (連跌天數) × 基礎比率
            base_ratio = 0.015  # 平均 1.5% 融券比
            vol_mult   = vol_now / vol_avg if vol_avg else 1.0
            short_ratio = min(base_ratio * vol_mult * (1 + down_days * 0.1), 0.12)
            short_ratio = max(short_ratio, 0.003)  # 最少 0.3%

            # 軋空潛力評分（0-100）
            # 融券比高 + 股價反彈 + 成交量放大 → 軋空潛力高
            squeeze_score = 0
            squeeze_score += min(short_ratio * 500, 40)   # 融券比貢獻
            squeeze_score += min((chg_pct if chg_pct > 0 else 0) * 5, 25)  # 反彈力道
            squeeze_score += min((vol_mult - 1) * 20, 25) if vol_mult > 1 else 0  # 量能貢獻
            squeeze_score = min(round(squeeze_score), 100)

            # 評級
            if squeeze_score >= 70:
                rating, level = "🔴 高軋空潛力", "high"
            elif squeeze_score >= 45:
                rating, level = "🟡 中軋空潛力", "medium"
            else:
                rating, level = "🟢 低軋空壓力", "low"

            return {
                "code":          code,
                "name":          name,
                "price":         round(price, 1),
                "chg_pct":       round(chg_pct, 2),
                "down_days":     down_days,
                "short_ratio":   round(short_ratio * 100, 2),
                "vol_ratio":     round(vol_mult, 1),
                "squeeze_score": squeeze_score,
                "rating":        rating,
                "level":         level,
            }
        except Exception as e:
            logger.debug(f"[short] fetch {code}: {e}")
            return None

    tasks   = [fetch_one(c, n) for c, n in _WATCH_LIST]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    valid = [r for r in results if isinstance(r, dict)]
    valid.sort(key=lambda x: x["squeeze_score"], reverse=True)

    top10_squeeze = valid[:10]
    high_short    = sorted(valid, key=lambda x: x["short_ratio"], reverse=True)[:5]

    # 市場整體短空氛圍
    avg_score = sum(v["squeeze_score"] for v in valid) / len(valid) if valid else 50
    bear_pct  = sum(1 for v in valid if v["chg_pct"] < 0) / len(valid) * 100 if valid else 50
    if avg_score > 65:
        market_tone = "🔴 空頭壓力大，多股存在軋空風險"
    elif avg_score > 45:
        market_tone = "🟡 部分個股有軋空潛力，需個別追蹤"
    else:
        market_tone = "🟢 整體空頭壓力輕，市場偏多頭格局"

    return {
        "top_squeeze":   top10_squeeze,
        "high_short":    high_short,
        "market_tone":   market_tone,
        "avg_score":     round(avg_score, 1),
        "bear_pct":      round(bear_pct, 1),
        "total_watched": len(valid),
        "updated_at":    time.strftime("%Y-%m-%d %H:%M"),
    }


def format_short_report(data: dict) -> str:
    if data.get("error"):
        return f"❌ {data['error']}"

    top      = data.get("top_squeeze", [])
    high_sh  = data.get("high_short", [])
    tone     = data.get("market_tone", "")
    avg_sc   = data.get("avg_score", 50)
    updated  = data.get("updated_at", "")

    lines = [
        "🩳 融券追蹤  軋空潛力分析",
        "─" * 32, "",
        f"市場短空氛圍：{tone}",
        f"平均軋空評分：{avg_sc:.0f}/100  更新：{updated}",
        "",
        "🔥 軋空潛力 TOP 5：",
    ]

    for i, s in enumerate(top[:5], 1):
        chg_icon = "📈" if s["chg_pct"] >= 0 else "📉"
        lines.append(
            f"  {i}. {s['name']}({s['code']})  評分:{s['squeeze_score']}"
            f"  融券估:{s['short_ratio']:.1f}%  {chg_icon}{s['chg_pct']:+.1f}%"
        )

    lines += [
        "",
        "📊 融券比估算最高 TOP 5：",
    ]

    for s in high_sh:
        chg_icon = "📈" if s["chg_pct"] >= 0 else "📉"
        lines.append(
            f"  {s['name']}({s['code']})  融券估:{s['short_ratio']:.1f}%  "
            f"量比:{s['vol_ratio']:.1f}x  {chg_icon}{s['chg_pct']:+.1f}%"
        )

    lines += [
        "",
        "─" * 28,
        "🤖 AI 做空解析",
        _gen_short_verdict(data),
        "",
        "⚠️ 融券數據為統計估算，實際請參考 TWSE 融資融券報告",
        "輸入 /margin CODE 查個股融資明細 | /feargreed 查恐慌指數",
    ]
    return "\n".join(lines)


def _gen_short_verdict(data: dict) -> str:
    top     = data.get("top_squeeze", [])
    avg_sc  = data.get("avg_score", 50)
    bear_pct = data.get("bear_pct", 50)

    if not top:
        return "目前無軋空標的資料。"

    top1 = top[0]
    if avg_sc > 65:
        return (
            f"市場空頭壓力整體偏高（平均評分 {avg_sc:.0f}），"
            f"最具軋空潛力為 {top1['name']}（評分 {top1['squeeze_score']}）。"
            f"若市場反彈，融券回補力道將放大漲幅，短線留意追高風險。"
        )
    elif top1["squeeze_score"] >= 60:
        return (
            f"{top1['name']} 融券比估 {top1['short_ratio']:.1f}%、量比 {top1['vol_ratio']:.1f}x，"
            f"一旦觸發回補訊號，軋空行情可能快速升溫。"
            f"整體市場空頭比例 {bear_pct:.0f}%，操作上注意設定停損。"
        )
    else:
        return (
            f"目前市場空頭壓力較輕（平均評分 {avg_sc:.0f}），"
            f"整體以多頭格局為主（{100-bear_pct:.0f}% 個股上漲），"
            f"融券回補效應有限，建議以基本面選股為主。"
        )
