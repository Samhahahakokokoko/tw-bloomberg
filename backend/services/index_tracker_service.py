"""Index Tracker Service — 指數成分股追蹤（台灣50 / 中型100 / 電子）"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 900  # 15 min during trading hours

# 指數成分股資料庫（主要成分）
_INDEX_DB: dict[str, dict] = {
    "台灣50": {
        "name": "台灣50（0050）",
        "ticker": "^TWII",
        "etf": "0050.TW",
        "components": [
            ("2330", "台積電",  30.0),
            ("2317", "鴻海",    5.2),
            ("2454", "聯發科",  4.8),
            ("2382", "廣達",    3.5),
            ("2308", "台達電",  2.8),
            ("2881", "富邦金",  2.5),
            ("2882", "國泰金",  2.4),
            ("3008", "大立光",  1.8),
            ("2412", "中華電",  1.7),
            ("2886", "兆豐金",  1.6),
            ("2303", "聯電",    1.5),
            ("2379", "瑞昱",    1.3),
            ("2357", "華碩",    1.2),
            ("4938", "和碩",    1.1),
            ("2345", "智邦",    1.0),
        ],
    },
    "中型100": {
        "name": "台灣中型100（0051）",
        "ticker": "^TAIEX",
        "etf": "0051.TW",
        "components": [
            ("2376", "技嘉",  3.2),
            ("3443", "創意",  2.8),
            ("6669", "緯穎",  2.5),
            ("2337", "旺宏",  2.0),
            ("3037", "欣興",  1.8),
            ("6505", "台塑化", 1.7),
            ("2002", "中鋼",  1.6),
            ("1301", "台塑",  1.5),
            ("2609", "陽明",  1.4),
            ("2615", "萬海",  1.3),
        ],
    },
    "0050": {
        "name": "台灣50（0050）",
        "ticker": "^TWII",
        "etf": "0050.TW",
        "components": [
            ("2330", "台積電",  30.0),
            ("2317", "鴻海",    5.2),
            ("2454", "聯發科",  4.8),
            ("2382", "廣達",    3.5),
            ("2308", "台達電",  2.8),
            ("2881", "富邦金",  2.5),
            ("2882", "國泰金",  2.4),
            ("3008", "大立光",  1.8),
            ("2412", "中華電",  1.7),
            ("2303", "聯電",    1.5),
        ],
    },
    "電子": {
        "name": "電子族群",
        "ticker": "^TAIEX",
        "etf": "0053.TW",
        "components": [
            ("2330", "台積電",  28.0),
            ("2317", "鴻海",    8.0),
            ("2454", "聯發科",  6.0),
            ("2382", "廣達",    4.0),
            ("2308", "台達電",  3.5),
            ("3008", "大立光",  2.5),
            ("2303", "聯電",    2.0),
            ("2379", "瑞昱",    1.8),
            ("2357", "華碩",    1.5),
            ("4938", "和碩",    1.3),
        ],
    },
}
_ALIAS: dict[str, str] = {
    "台灣50": "台灣50", "台灣 50": "台灣50", "tw50": "台灣50",
    "0050": "0050", "50etf": "台灣50",
    "中型": "中型100", "中型100": "中型100",
    "電子": "電子", "電子股": "電子",
}


def _resolve_index(query: str) -> str | None:
    q = query.strip()
    if q in _INDEX_DB:
        return q
    return _ALIAS.get(q) or _ALIAS.get(q.lower())


async def get_index_tracker(query: str) -> dict:
    key = _resolve_index(query)
    if not key:
        return {"error": f"找不到指數：{query}\n支援：台灣50 / 中型100 / 電子"}

    cache_key = key
    now = time.time()
    if cache_key in _cache and now - _cache_ts.get(cache_key, 0) < _TTL:
        return _cache[cache_key]

    result = await _fetch_index(key)
    _cache[cache_key] = result
    _cache_ts[cache_key] = now
    return result


async def _fetch_index(key: str) -> dict:
    import asyncio, httpx
    info   = _INDEX_DB[key]
    comps  = info["components"]

    async def fetch_one(code: str):
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(url, params={"interval": "1d", "range": "3d"},
                                headers={"User-Agent": "Mozilla/5.0"})
            q      = r.json()["chart"]["result"][0]["indicators"]["quote"][0]
            closes = [x for x in q.get("close", []) if x is not None]
            if len(closes) >= 2:
                chg = (closes[-1] - closes[-2]) / closes[-2] * 100
                return {"price": closes[-1], "chg_pct": round(chg, 2)}
        except Exception:
            pass
        return {"price": 0.0, "chg_pct": 0.0}

    tasks   = [fetch_one(c[0]) for c in comps]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    components_data = []
    index_impact    = 0.0

    for (code, name, weight), res in zip(comps, results):
        if isinstance(res, dict):
            chg    = res.get("chg_pct", 0.0)
            price  = res.get("price", 0.0)
            impact = chg * weight / 100
            index_impact += impact
            components_data.append({
                "code":   code,
                "name":   name,
                "weight": weight,
                "price":  price,
                "chg_pct": chg,
                "impact":  round(impact, 4),
            })
        else:
            components_data.append({
                "code": code, "name": name, "weight": weight,
                "price": 0.0, "chg_pct": 0.0, "impact": 0.0,
            })

    components_data.sort(key=lambda x: x["impact"], reverse=True)
    top_contributors = components_data[:3]
    top_drags        = sorted(components_data, key=lambda x: x["impact"])[:3]

    bull_cnt = sum(1 for c in components_data if c["chg_pct"] > 0)
    bear_cnt = sum(1 for c in components_data if c["chg_pct"] < 0)
    chip_dir = "偏多" if bull_cnt > bear_cnt + 2 else ("偏空" if bear_cnt > bull_cnt + 2 else "分歧")

    verdict = _gen_verdict(index_impact, chip_dir, bull_cnt, bear_cnt, top_contributors)

    return {
        "key":          key,
        "name":         info["name"],
        "components":   components_data,
        "index_impact": round(index_impact, 2),
        "bull_cnt":     bull_cnt,
        "bear_cnt":     bear_cnt,
        "chip_dir":     chip_dir,
        "top_contrib":  top_contributors,
        "top_drags":    top_drags,
        "verdict":      verdict,
        "updated_at":   time.strftime("%Y-%m-%d %H:%M"),
    }


def _gen_verdict(impact: float, chip_dir: str, bull: int, bear: int, leaders: list) -> str:
    if impact > 0.3 and chip_dir == "偏多":
        lead = "、".join(c["name"] for c in leaders[:2])
        return f"指數今日由 {lead} 等個股帶動走強，成分股多頭格局，後市偏正向。"
    elif impact < -0.3 and chip_dir == "偏空":
        return f"指數今日承壓下跌，成分股空頭居多（{bear}跌/{bull}漲），注意系統性風險。"
    else:
        return f"成分股漲跌互見（{bull}漲/{bear}跌），指數整體震盪，個股分化明顯，建議精選操作。"


def format_index_report(data: dict) -> str:
    if data.get("error"):
        return f"❌ {data['error']}"

    name    = data.get("name", "")
    comps   = data.get("components", [])
    impact  = data.get("index_impact", 0.0)
    bull    = data.get("bull_cnt", 0)
    bear    = data.get("bear_cnt", 0)
    chip    = data.get("chip_dir", "─")
    verdict = data.get("verdict", "")
    updated = data.get("updated_at", "")

    lines = [
        f"📊 指數成分股追蹤  {name}",
        "─" * 32, "",
        f"加權貢獻：{impact:+.2f}%  📈{bull}漲 / 📉{bear}跌",
        f"籌碼方向：{chip}",
        f"更新：{updated}",
        "",
        "前5大成分股今日表現：",
    ]

    for c in comps[:10]:
        chg  = c["chg_pct"]
        icon = "📈" if chg > 0 else ("📉" if chg < 0 else "⬜")
        lines.append(
            f"  {icon} {c['name']:6s}({c['code']}) "
            f"{chg:+.1f}%  佔比{c['weight']:.1f}%  貢獻{c['impact']:+.3f}%"
        )

    top3 = data.get("top_contrib", [])
    drag3 = data.get("top_drags", [])

    if top3:
        lines.append("")
        lines.append("🔥 最大拉升：" + "  ".join(f"{c['name']}{c['chg_pct']:+.1f}%" for c in top3))
    if drag3:
        lines.append("⬇️  最大拖累：" + "  ".join(f"{c['name']}{c['chg_pct']:+.1f}%" for c in drag3 if c['chg_pct'] < 0))

    lines += [
        "",
        "─" * 28,
        "🤖 AI 指數走勢分析",
        verdict,
        "",
        "輸入 /index 中型100 | /index 電子 | /feargreed",
    ]
    return "\n".join(lines)
