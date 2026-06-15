"""Supply Chain Service — 產業供應鏈分析"""
from __future__ import annotations

import time
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 7200  # 2hr — supply chain structure changes slowly

# 供應鏈資料庫（name/alias → {upstream, downstream, sector}）
_SUPPLY_DB: dict[str, dict] = {
    "2330": {
        "name": "台積電",
        "aliases": ["tsmc", "台積", "台積電"],
        "sector": "半導體代工",
        "upstream": [
            {"code": "3037", "name": "欣興電子", "role": "IC載板"},
            {"code": "2337", "name": "旺宏電子", "role": "特殊記憶體"},
            {"code": "4994", "name": "傳奇", "role": "光阻劑"},
            {"code": "3443", "name": "創意電子", "role": "IC設計服務"},
            {"code": "6669", "name": "緯穎", "role": "封裝測試"},
        ],
        "downstream": [
            {"name": "Apple", "code": "AAPL", "role": "最大客戶(A系列/M系列晶片)"},
            {"name": "NVIDIA", "code": "NVDA", "role": "AI GPU (H100/B200)"},
            {"name": "AMD", "code": "AMD", "role": "CPU/GPU"},
            {"name": "Qualcomm", "code": "QCOM", "role": "手機處理器"},
            {"code": "2454", "name": "聯發科", "role": "手機SoC"},
        ],
        "impact_rules": {
            "positive": "台積電利多→上游設備材料廠受惠，AI客戶(輝達/AMD)需求增加",
            "negative": "台積電利空→下游IC設計股(聯發科/2454)短期承壓，封測廠跟跌",
        },
    },
    "2454": {
        "name": "聯發科",
        "aliases": ["mediatek", "聯發科", "mtk"],
        "sector": "IC設計",
        "upstream": [
            {"code": "2330", "name": "台積電", "role": "主要晶圓代工"},
            {"code": "2308", "name": "台達電", "role": "電源供應"},
            {"code": "3037", "name": "欣興電子", "role": "IC載板"},
        ],
        "downstream": [
            {"name": "Samsung", "role": "手機/電視晶片"},
            {"name": "小米", "role": "高端手機SoC"},
            {"name": "OPPO/vivo", "role": "中階手機"},
            {"code": "2317", "name": "鴻海", "role": "代工組裝"},
        ],
        "impact_rules": {
            "positive": "聯發科利多→台積電訂單增加，載板需求上揚",
            "negative": "聯發科利空→手機鏈(鴻海/和碩)短期利空",
        },
    },
    "2317": {
        "name": "鴻海",
        "aliases": ["foxconn", "鴻海", "hon hai"],
        "sector": "電子代工",
        "upstream": [
            {"code": "2330", "name": "台積電", "role": "晶片供應"},
            {"code": "2382", "name": "廣達", "role": "競品/合作"},
            {"code": "3008", "name": "大立光", "role": "鏡頭模組"},
        ],
        "downstream": [
            {"name": "Apple", "code": "AAPL", "role": "iPhone主要代工廠"},
            {"name": "NVIDIA", "code": "NVDA", "role": "AI伺服器組裝"},
            {"name": "Sony", "role": "PS5代工"},
        ],
        "impact_rules": {
            "positive": "鴻海利多→AI伺服器需求，帶動散熱/連接器供應商",
            "negative": "鴻海利空→iPhone代工下修，小米/OPPO轉單效應",
        },
    },
    "2382": {
        "name": "廣達",
        "aliases": ["廣達", "quanta"],
        "sector": "伺服器/NB代工",
        "upstream": [
            {"code": "2330", "name": "台積電", "role": "AI晶片來源"},
            {"code": "3443", "name": "創意電子", "role": "IC設計支援"},
            {"code": "6669", "name": "緯穎", "role": "競品/供應夥伴"},
        ],
        "downstream": [
            {"name": "NVIDIA", "code": "NVDA", "role": "DGX AI伺服器"},
            {"name": "Google", "role": "TPU伺服器"},
            {"name": "Meta", "role": "AI基礎設施"},
            {"name": "Microsoft", "role": "Azure伺服器"},
        ],
        "impact_rules": {
            "positive": "廣達利多→AI伺服器需求，散熱(雙鴻/奇鋐)及電源大廠受惠",
            "negative": "廣達利空→雲端資本支出下修，AI題材退潮",
        },
    },
    "2308": {
        "name": "台達電",
        "aliases": ["台達電", "delta"],
        "sector": "電源/散熱",
        "upstream": [
            {"name": "原物料廠商", "role": "銅/鋼鐵等原材料"},
        ],
        "downstream": [
            {"code": "2382", "name": "廣達", "role": "伺服器電源"},
            {"code": "2317", "name": "鴻海", "role": "電源模組"},
            {"name": "Tesla", "role": "EV充電設備"},
            {"name": "Volkswagen", "role": "EV充電站"},
        ],
        "impact_rules": {
            "positive": "台達電利多→AI伺服器電源需求，EV充電市場擴大",
            "negative": "台達電利空→EV需求下修，伺服器電源競爭加劇",
        },
    },
    "2881": {
        "name": "富邦金",
        "aliases": ["富邦金", "fubon"],
        "sector": "金融",
        "upstream": [{"name": "資本市場", "role": "利率/匯率環境"}],
        "downstream": [{"name": "壽險保戶", "role": "保費收入"}, {"name": "企業客戶", "role": "放款"}],
        "impact_rules": {
            "positive": "富邦金利多→金融股跟漲，壽險族群受惠",
            "negative": "富邦金利空→金融股普跌，注意匯損風險",
        },
    },
    "0050": {
        "name": "元大台灣50",
        "aliases": ["0050", "元大50", "台灣50"],
        "sector": "ETF",
        "upstream": [{"name": "成份股", "role": "台股前50大市值股"}],
        "downstream": [{"name": "投資人", "role": "ETF持有者"}],
        "impact_rules": {
            "positive": "0050 ETF定期定額買入，帶動成份股需求",
            "negative": "市場大跌，0050成份股全面承壓",
        },
    },
}


def _resolve_code(query: str) -> str | None:
    q = query.strip().upper()
    if q in _SUPPLY_DB:
        return q
    q_lower = query.strip().lower()
    for code, info in _SUPPLY_DB.items():
        aliases = [a.lower() for a in info.get("aliases", [])]
        if q_lower in aliases or q_lower == info["name"].lower():
            return code
    return None


async def get_supply_chain(query: str) -> dict:
    code = _resolve_code(query)
    if not code:
        return {"error": f"找不到供應鏈資料：{query}\n支援：台積電/聯發科/鴻海/廣達/台達電/富邦金/0050"}

    key = code
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]

    result = await _fetch_supply_chain(code)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_supply_chain(code: str) -> dict:
    import asyncio, httpx
    info = _SUPPLY_DB.get(code, {})

    # 抓取目標股票最新新聞標題（用於AI影響分析）
    latest_news: list[str] = []
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{code}.TW"
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url, params={"modules": "assetProfile,price"},
                            headers={"User-Agent": "Mozilla/5.0"})
        jdata = r.json()
        price_data = jdata.get("quoteSummary", {}).get("result", [{}])[0].get("price", {})
        current_price = price_data.get("regularMarketPrice", {}).get("raw", 0)
        chg_pct = price_data.get("regularMarketChangePercent", {}).get("raw", 0) * 100
    except Exception:
        current_price = 0.0
        chg_pct = 0.0

    # 影響分析（規則式）
    if chg_pct > 3:
        impact_key = "positive"
        event = f"今日大漲 {chg_pct:.1f}%"
    elif chg_pct < -3:
        impact_key = "negative"
        event = f"今日大跌 {chg_pct:.1f}%"
    else:
        impact_key = "positive"
        event = f"今日 {chg_pct:+.1f}%，無重大異動"

    impact_text = info.get("impact_rules", {}).get(impact_key, "")

    return {
        "code":          code,
        "name":          info.get("name", code),
        "sector":        info.get("sector", ""),
        "current_price": round(current_price, 2),
        "chg_pct":       round(chg_pct, 2),
        "upstream":      info.get("upstream", []),
        "downstream":    info.get("downstream", []),
        "event":         event,
        "impact":        impact_text,
        "impact_key":    impact_key,
    }


def format_supply_chain_report(data: dict) -> str:
    if data.get("error"):
        return f"❌ {data['error']}"

    name    = data["name"]
    code    = data["code"]
    sector  = data["sector"]
    price   = data.get("current_price", 0)
    chg     = data.get("chg_pct", 0)
    chg_icon = "📈" if chg >= 0 else "📉"
    event   = data.get("event", "")
    impact  = data.get("impact", "")
    ups     = data.get("upstream", [])
    downs   = data.get("downstream", [])

    lines = [
        f"🔗 供應鏈分析  {name}（{code}）",
        "─" * 32, "",
        f"產業：{sector}",
        f"今日：{price:.2f} {chg_icon}{chg:+.1f}%",
        f"事件：{event}",
        "",
        "⬆️ 上游供應商：",
    ]
    for u in ups:
        cstr = f"（{u.get('code','')}）" if u.get("code") else ""
        lines.append(f"  • {u.get('name','')} {cstr} — {u.get('role','')}")

    lines += ["", "⬇️ 下游客戶："]
    for d in downs:
        cstr = f"（{d.get('code','')}）" if d.get("code") else ""
        lines.append(f"  • {d.get('name','')} {cstr} — {d.get('role','')}")

    if impact:
        impact_icon = "📈" if data.get("impact_key") == "positive" else "📉"
        lines += [
            "",
            "─" * 28,
            f"🤖 AI 供應鏈影響分析",
            f"{impact_icon} {impact}",
        ]

    lines += [
        "",
        "輸入 /supply [公司名] 查其他供應鏈",
        "支援：台積電/聯發科/鴻海/廣達/台達電",
    ]
    return "\n".join(lines)
