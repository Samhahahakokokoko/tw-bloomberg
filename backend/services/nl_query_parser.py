"""自然語言查詢解析器

用 Claude 將自然語言選股條件解析為結構化的 ScreenerFilter，
再交給 screener_engine 執行。

範例輸入：
  "幫我找營收連三個月成長且法人大買的股票"
  "找殖利率超過5%且外資買超的金融股"
  "哪些散熱股技術面突破"
"""
import json
import re
from loguru import logger
from .screener_engine import ScreenerFilter, run_screener
from .ai_decision_agent import generate_nl_recommendation


# 自然語言 → 篩選條件的 prompt
_PARSE_PROMPT = """你是台股選股助理，請將以下選股描述解析為 JSON 格式的篩選條件。

支援的欄位（所有欄位都是可選的）：
{
  "revenue_yoy_min": 數字,         // 月營收年增率最低 %
  "gross_margin_min": 數字,        // 毛利率最低 %
  "three_margins_up": true/false,  // 三率齊升（毛利率/營益率/淨利率同步上升）
  "eps_growth_qtrs_min": 數字,     // 連續 EPS 成長最少季數
  "foreign_consec_buy_min": 數字,  // 外資連續買超最少天
  "trust_consec_buy_min": 數字,    // 投信連續買超最少天
  "dual_signal": true/false,       // 外資+投信雙強訊號
  "ma_aligned": true/false,        // 均線多頭排列 (5MA>20MA>60MA)
  "kd_golden_cross": true/false,   // KD黃金交叉
  "vol_breakout": true/false,      // 成交量突破20日均量1.5倍
  "bb_breakout": true/false,       // 布林通道突破上軌
  "fundamental_score_min": 數字,   // 基本面評分最低 (0-100)
  "chip_score_min": 數字,          // 籌碼面評分最低 (0-100)
  "technical_score_min": 數字,     // 技術面評分最低 (0-100)
  "total_score_min": 數字,         // 總評分最低 (0-100)
  "sort_by": "total_score|fundamental_score|chip_score|technical_score",
  "limit": 數字                    // 回傳筆數（預設20）
}

只輸出 JSON，不要其他文字。

用戶查詢："""


async def parse_nl_to_filter(query: str) -> ScreenerFilter:
    """
    用 Claude 解析自然語言 → ScreenerFilter
    如果 Claude 不可用，嘗試簡單的關鍵字比對作 fallback
    """
    from ..models.database import settings
    if settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            msg = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{
                    "role": "user",
                    "content": _PARSE_PROMPT + query,
                }],
            )
            raw = msg.content[0].text.strip()
            # 提取 JSON
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                data = json.loads(m.group())
                return _dict_to_filter(data)
        except Exception as e:
            logger.error(f"[NLParser] Claude parse error: {e}")

    # Fallback：關鍵字比對
    return _keyword_fallback(query)


def _dict_to_filter(d: dict) -> ScreenerFilter:
    return ScreenerFilter(
        revenue_yoy_min       = d.get("revenue_yoy_min"),
        gross_margin_min      = d.get("gross_margin_min"),
        three_margins_up      = d.get("three_margins_up"),
        eps_growth_qtrs_min   = d.get("eps_growth_qtrs_min"),
        foreign_consec_buy_min= d.get("foreign_consec_buy_min"),
        trust_consec_buy_min  = d.get("trust_consec_buy_min"),
        dual_signal           = d.get("dual_signal"),
        ma_aligned            = d.get("ma_aligned"),
        kd_golden_cross       = d.get("kd_golden_cross"),
        vol_breakout          = d.get("vol_breakout"),
        bb_breakout           = d.get("bb_breakout"),
        fundamental_score_min = d.get("fundamental_score_min"),
        chip_score_min        = d.get("chip_score_min"),
        technical_score_min   = d.get("technical_score_min"),
        total_score_min       = d.get("total_score_min"),
        sort_by               = d.get("sort_by", "total_score"),
        limit                 = int(d.get("limit", 20)),
    )


def _keyword_fallback(query: str) -> ScreenerFilter:
    """無 Claude 時的關鍵字 fallback"""
    f = ScreenerFilter()
    q = query.lower()

    if any(kw in q for kw in ("營收", "revenue", "成長")):
        f.revenue_yoy_min = 15
    if any(kw in q for kw in ("三率", "三率齊升")):
        f.three_margins_up = True
    if any(kw in q for kw in ("外資", "法人", "主力")):
        f.foreign_consec_buy_min = 2
    if any(kw in q for kw in ("投信", "作帳")):
        f.trust_consec_buy_min = 1
    if any(kw in q for kw in ("雙強", "外資投信")):
        f.dual_signal = True
    if any(kw in q for kw in ("均線", "多頭排列", "排列")):
        f.ma_aligned = True
    if any(kw in q for kw in ("kd", "黃金交叉", "交叉")):
        f.kd_golden_cross = True
    if any(kw in q for kw in ("量", "爆量", "突破")):
        f.vol_breakout = True
    if any(kw in q for kw in ("布林", "上軌")):
        f.bb_breakout = True

    # 若無任何條件，設基本門檻
    if not any([
        f.revenue_yoy_min, f.three_margins_up, f.foreign_consec_buy_min,
        f.dual_signal, f.ma_aligned, f.vol_breakout,
    ]):
        f.total_score_min = 60

    return f


async def execute_nl_query(query: str) -> dict:
    """
    完整執行流程：解析 → 篩選 → AI 總結
    回傳 {results, filter_description, ai_summary}
    """
    screener_filter = await parse_nl_to_filter(query)
    results = await run_screener(screener_filter)

    # 產生 AI 總結
    ai_summary = await generate_nl_recommendation(query, results)

    # 篩選條件描述（給前端顯示）
    desc_parts = []
    if screener_filter.revenue_yoy_min:
        desc_parts.append(f"營收 YoY ≥ {screener_filter.revenue_yoy_min}%")
    if screener_filter.three_margins_up:
        desc_parts.append("三率齊升")
    if screener_filter.foreign_consec_buy_min:
        desc_parts.append(f"外資連買 ≥ {screener_filter.foreign_consec_buy_min}日")
    if screener_filter.dual_signal:
        desc_parts.append("外資+投信雙強")
    if screener_filter.ma_aligned:
        desc_parts.append("均線多頭排列")
    if screener_filter.kd_golden_cross:
        desc_parts.append("KD黃金交叉")
    if screener_filter.vol_breakout:
        desc_parts.append("量能突破")
    if screener_filter.total_score_min:
        desc_parts.append(f"總分 ≥ {screener_filter.total_score_min}")

    return {
        "query":              query,
        "filter_description": " + ".join(desc_parts) if desc_parts else "綜合評分",
        "results":            results,
        "result_count":       len(results),
        "ai_summary":         ai_summary,
    }
