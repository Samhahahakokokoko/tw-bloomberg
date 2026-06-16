"""AI Natural QA Service — 升級版自然語言投資問答（整合全系統數據）"""
from __future__ import annotations

import time
from loguru import logger
import re

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 300  # 5 分鐘（問答快取較短）


async def get_ai_natural_answer(question: str, uid: str) -> str:
    """整合系統數據後呼叫 Claude 回答台股自然語言問題"""
    from ..models.database import settings, AsyncSessionLocal
    from . import portfolio_service

    if not settings.anthropic_api_key:
        return "❌ AI 功能未配置，請聯繫管理員"

    # 快取相同問題
    cache_key = f"{uid}:{question[:50]}"
    now = time.time()
    if cache_key in _cache and now - _cache_ts.get(cache_key, 0) < _TTL:
        return _cache[cache_key]

    # ── 1. 蒐集系統數據作為上下文 ──────────────────────────────────────────
    context_parts = []

    # 市場大盤
    try:
        market_ctx = await _get_market_snapshot()
        context_parts.append(f"【今日市場】\n{market_ctx}")
    except Exception as e:
        pass

    # 用戶持倉
    try:
        async with AsyncSessionLocal() as db:
            holdings = await portfolio_service.get_portfolio(db, uid)
        if holdings:
            h_lines = [f"- {h['stock_code']} {h.get('stock_name','')} "
                       f"{h['shares']}股 損益{h.get('pnl_pct',0):+.1f}%"
                       for h in holdings[:8]]
            context_parts.append("【用戶持倉】\n" + "\n".join(h_lines))
    except Exception as e:
        pass

    # 恐慌貪婪指數
    try:
        from .feargreed_service import get_feargreed
        fg = await get_feargreed()
        score = fg.get("composite_score", 50)
        label = fg.get("label", "中性")
        context_parts.append(f"【恐慌貪婪指數】{score}/100（{label}）")
    except Exception as e:
        pass

    # 問題中提到的個股資訊
    import re
    codes_in_q = re.findall(r'\b(\d{4,5})\b', question)
    for code in codes_in_q[:2]:
        try:
            import httpx
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(url, params={"interval": "1d", "range": "5d"},
                                headers={"User-Agent": "Mozilla/5.0"})
            q_data = r.json()["chart"]["result"][0]["indicators"]["quote"][0]
            closes = [x for x in q_data.get("close", []) if x is not None]
            if len(closes) >= 2:
                chg = (closes[-1] - closes[-2]) / closes[-2] * 100
                context_parts.append(
                    f"【{code} 今日資料】現價={closes[-1]:.1f} 漲跌={chg:+.1f}%"
                )
        except Exception as e:
            pass

    # 問題中的族群關鍵字 → 提供族群資訊
    sector_keywords = {
        "AI": ["2330", "2454", "2382", "6669"],
        "半導體": ["2330", "2303", "2454", "3443"],
        "金融": ["2881", "2882", "2886", "5880"],
        "航運": ["2609", "2615"],
        "電子": ["2317", "2357", "4938", "2379"],
    }
    for kw, sector_codes in sector_keywords.items():
        if kw in question:
            context_parts.append(f"【{kw}族群主要成分】{' / '.join(sector_codes)}")
            break

    system_context = "\n\n".join(context_parts)

    # ── 2. 建構 Claude 請求 ────────────────────────────────────────────────
    system_prompt = f"""你是台股專業投資分析師，精通技術分析、基本面分析和總經研判。
用繁體中文回答，重點條列，500字內。語氣專業但簡潔。

以下是系統即時數據供你參考：
{system_context}

回答原則：
1. 結合上方系統數據來回答，而非只靠通用知識
2. 給出具體建議而非模糊答案
3. 指出主要風險和注意事項
4. 如問到自選股/持倉，結合用戶的實際持股分析"""

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=700,
            system=system_prompt,
            messages=[{"role": "user", "content": question}],
        )
        answer = msg.content[0].text

        _cache[cache_key] = answer
        _cache_ts[cache_key] = now
        return answer
    except Exception as e:
        if "credit balance is too low" in str(e).lower():
            logger.warning("[ai_natural_qa] Anthropic credit 耗盡")
        logger.error(f"[ai_natural_qa] error: {e}")
        return f"❌ AI 分析暫時無法使用：{str(e)[:100]}"


async def _get_market_snapshot() -> str:
    import httpx
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(url, params={"interval": "1d", "range": "3d"},
                            headers={"User-Agent": "Mozilla/5.0"})
        closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", [])
        closes = [x for x in closes if x is not None]
        if len(closes) >= 2:
            chg = (closes[-1] - closes[-2]) / closes[-2] * 100
            return f"台股加權指數 {closes[-1]:.0f}，今日{chg:+.1f}%"
    except Exception as e:
        pass
    return "台股資料暫時無法取得"


# 常見問題快速路由（不需 AI）
_QUICK_ANSWERS = {
    "市場今天怎麼樣": "使用 /market 查看今日大盤",
    "早報": "使用 /morning 查看今日早報",
    "選股": "使用 /screener 智慧選股",
    "持倉": "使用 /portfolio 查看持倉",
}


def get_quick_answer(question: str) -> str | None:
    for key, val in _QUICK_ANSWERS.items():
        if key in question:
            return val
    return None
