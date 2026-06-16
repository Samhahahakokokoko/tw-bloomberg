"""Daily QA Service — 每日投資思考問答（08:00 自動推播）"""
from __future__ import annotations

import os
import time
from datetime import date
from loguru import logger

_cache: dict = {}
_cache_ts: float = 0.0
_TTL = 3600 * 6  # 6 小時（一天內不重複生成）

# 問題題庫（輪替使用）
_QUESTION_POOL: list[dict] = [
    {
        "q1": "今天最需要關注的風險是什麼？（系統性？個股？政策？）",
        "q2": "如果大盤下跌 3%，你的應對計畫是？持有、加碼、還是停損？",
        "q3": "今天有沒有比昨天更了解市場？你學到了什麼新觀點？",
    },
    {
        "q1": "你目前持股中，哪一檔最讓你不安心？為什麼？",
        "q2": "市場上現在最多人在討論什麼題材？你是否過度追逐熱門？",
        "q3": "如果今天完全不看盤，你對持股還有信心嗎？",
    },
    {
        "q1": "你上次賣出的理由是什麼？現在回頭看，是對的決策嗎？",
        "q2": "你的投資組合中，哪檔股票你最有把握？根據是什麼？",
        "q3": "今天有什麼資訊讓你改變了對某支股票的看法？",
    },
    {
        "q1": "現在市場是在恐懼還是貪婪？你是反向操作還是順勢？",
        "q2": "你的停損紀律有沒有被情緒影響？上次停損有沒有執行？",
        "q3": "一個月後，你希望自己現在做了什麼投資決策？",
    },
    {
        "q1": "你的投資組合有沒有過度集中在單一族群或題材？",
        "q2": "如果法人今天反向操作（你多他空），你的依據是什麼？",
        "q3": "你最近有沒有因為朋友的推薦或社群訊息改變了操作計畫？",
    },
    {
        "q1": "今天最讓你興奮的投資機會是什麼？你有沒有因興奮而失去理性？",
        "q2": "如果你的持股現在漲了 30%，你的賣出計畫是什麼？",
        "q3": "你有沒有在等待「完美進場點」而錯過了機會？",
    },
    {
        "q1": "你的資金配置是否反映了你真正的風險承受度？",
        "q2": "今天的市場給了你什麼教訓？哪個觀念需要修正？",
        "q3": "如果你現在手上沒有任何持股，你會買什麼？為什麼？",
    },
]


async def get_daily_qa() -> dict:
    global _cache, _cache_ts
    now = time.time()
    today = date.today().isoformat()

    if _cache and _cache.get("date") == today and now - _cache_ts < _TTL:
        return _cache

    result = await _build_qa()
    _cache = result
    _cache_ts = now
    return result


async def _build_qa() -> dict:
    today = date.today()
    day_of_year = today.timetuple().tm_yday
    q_set = _QUESTION_POOL[day_of_year % len(_QUESTION_POOL)]

    # 取得市場背景（用於 AI 脈絡）
    market_context = await _get_market_context()

    # 根據市場狀況延伸問題
    context_q = _gen_context_question(market_context)

    return {
        "date":    today.isoformat(),
        "q1":      q_set["q1"],
        "q2":      q_set["q2"],
        "q3":      q_set["q3"],
        "bonus_q": context_q,
        "market":  market_context,
    }


async def _get_market_context() -> dict:
    import httpx
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(url, params={"interval": "1d", "range": "5d"},
                            headers={"User-Agent": "Mozilla/5.0"})
        res    = r.json()["chart"]["result"][0]
        closes = [x for x in res["indicators"]["quote"][0].get("close", []) if x is not None]
        if len(closes) >= 2:
            chg = (closes[-1] - closes[-2]) / closes[-2] * 100
            return {"twii": round(closes[-1], 0), "chg_pct": round(chg, 2)}
    except Exception as e:
        pass
    return {"twii": 0, "chg_pct": 0.0}


def _gen_context_question(market: dict) -> str:
    chg = market.get("chg_pct", 0)
    if chg > 2:
        return f"🔥 大盤今日強漲 {chg:.1f}%，你有沒有因為市場大漲而衝動追高？追高前有沒有確認基本面支撐？"
    elif chg < -2:
        return f"📉 大盤今日重跌 {chg:.1f}%，你有沒有按計畫執行停損？還是因恐慌而過度賣出？"
    elif chg > 0.5:
        return "市場今日偏多，你有沒有借此機會審視持股是否仍符合原本的投資邏輯？"
    elif chg < -0.5:
        return "市場今日偏弱，你的現金比例是否在舒適範圍？是否需要調整持倉結構？"
    else:
        return "市場今日震盪整理，盤整期間你如何區分「好的整理」和「需要止損的下跌」？"


def format_qa_report(data: dict) -> str:
    today  = data.get("date", date.today().isoformat())
    market = data.get("market", {})
    twii   = market.get("twii", 0)
    chg    = market.get("chg_pct", 0.0)
    chg_icon = "📈" if chg >= 0 else "📉"

    lines = [
        "🧠 每日投資思考問答",
        f"── {today} ──────────────────",
        "",
        f"大盤參考：{twii:.0f}  {chg_icon}{chg:+.1f}%",
        "",
        "今天請靜下來思考以下問題：",
        "",
        f"❓ Q1  {data.get('q1', '')}",
        "",
        f"❓ Q2  {data.get('q2', '')}",
        "",
        f"❓ Q3  {data.get('q3', '')}",
        "",
        "─" * 28,
        f"💡 今日加碼題",
        f"   {data.get('bonus_q', '')}",
        "",
        "📝 建議：把答案寫進投資日記 /journal add",
        "回答這些問題能幫助你在情緒化市場中保持理性",
    ]
    return "\n".join(lines)


async def push_daily_qa() -> bool:
    """每日 08:00 排程推播"""
    import os
    from .line_push import push_line_messages
    admin_uid = os.getenv("ADMIN_LINE_UID", "")
    if not admin_uid:
        return False
    try:
        data = await get_daily_qa()
        text = format_qa_report(data)
        ok = await push_line_messages(
            admin_uid,
            [{"type": "text", "text": text[:4000]}],
            context="daily_qa.push",
        )
        logger.info(f"[daily_qa] pushed: {ok}")
        return ok
    except Exception as e:
        logger.error(f"[daily_qa] push error: {e}")
        return False
