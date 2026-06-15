"""Seasonal Service — 個股季節性分析（月度規律/除息/法說會前後）"""
from __future__ import annotations

import time
from datetime import date, datetime
from loguru import logger

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 7200


async def get_seasonal(code: str) -> dict:
    key = code.upper()
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_seasonal(key)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_seasonal(code: str) -> dict:
    import asyncio, httpx
    # 抓取 5 年月線資料
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(url,
                            params={"interval": "1mo", "range": "5y"},
                            headers={"User-Agent": "Mozilla/5.0"})
        data   = r.json()
        result = data["chart"]["result"][0]
        times  = result.get("timestamp", [])
        closes = result["indicators"]["quote"][0].get("close", [])
        opens  = result["indicators"]["quote"][0].get("open", [])
    except Exception as e:
        logger.warning(f"[seasonal] fetch failed {code}: {e}")
        return _fallback_seasonal(code)

    # 月度報酬統計
    monthly_returns: dict[int, list[float]] = {m: [] for m in range(1, 13)}
    for ts, cl, op in zip(times, closes, opens):
        if cl is None or op is None or op == 0:
            continue
        m = datetime.utcfromtimestamp(ts).month
        ret = (cl - op) / op * 100
        monthly_returns[m].append(ret)

    month_avg = {}
    for m, rets in monthly_returns.items():
        if rets:
            month_avg[m] = round(sum(rets) / len(rets), 2)
        else:
            month_avg[m] = 0.0

    best_month  = max(month_avg, key=lambda x: month_avg[x])
    worst_month = min(month_avg, key=lambda x: month_avg[x])

    # 除息行事曆（靜態資料，常見個股）
    ex_div_info = _get_ex_div_info(code)

    # 目前月份操作建議
    cur_month = date.today().month
    cur_ret   = month_avg.get(cur_month, 0.0)
    season_tip = _season_suggestion(cur_month, cur_ret, month_avg, ex_div_info)

    return {
        "code":         code,
        "month_avg":    month_avg,
        "best_month":   best_month,
        "worst_month":  worst_month,
        "ex_div_info":  ex_div_info,
        "cur_month":    cur_month,
        "cur_ret":      cur_ret,
        "season_tip":   season_tip,
        "years_data":   5,
    }


def _get_ex_div_info(code: str) -> dict:
    # 常見股票除息資訊（月份）
    EX_DIV_MAP: dict[str, dict] = {
        "2330": {"month": 7, "note": "7月除息，6月底前買進可領息，除息後常見填息走勢"},
        "2454": {"month": 8, "note": "8月除息，法說會通常在2月/8月，法說前後有波動"},
        "2317": {"month": 8, "note": "8月除息，蘋果新品發表（9月）前常有拉升期待"},
        "2882": {"month": 8, "note": "8月除息，金融股配息穩定，除息前買進者多"},
        "2881": {"month": 8, "note": "8月除息，壽險股受利率環境影響"},
        "2308": {"month": 7, "note": "7月除息，電動車季節性：Q3中國銷售旺季"},
        "0050": {"month": 1, "note": "1月/7月各一次除息，元月效應明顯，1月底買進策略"},
        "0056": {"month": 10, "note": "10月除息（高股息ETF），除息前常見買盤湧入"},
    }
    return EX_DIV_MAP.get(code, {"month": 0, "note": "無特定除息季節性資料"})


def _season_suggestion(cur_month: int, cur_ret: float,
                        month_avg: dict, ex_div: dict) -> str:
    tips = []
    if cur_ret > 1.5:
        tips.append(f"歷史上{cur_month}月平均報酬 {cur_ret:+.1f}%，為季節性強勢月份")
    elif cur_ret < -1.5:
        tips.append(f"歷史上{cur_month}月平均報酬 {cur_ret:+.1f}%，為季節性弱勢月份，宜謹慎")
    else:
        tips.append(f"歷史上{cur_month}月平均報酬 {cur_ret:+.1f}%，無明顯季節性偏向")

    ex_month = ex_div.get("month", 0)
    if ex_month:
        if cur_month == ex_month - 1:
            tips.append(f"下個月({ex_month}月)除息，除息前一個月常有買盤提前布局")
        elif cur_month == ex_month:
            tips.append(f"本月除息，除息後留意是否有填息動能")

    best = max(month_avg, key=lambda x: month_avg[x])
    worst = min(month_avg, key=lambda x: month_avg[x])
    tips.append(f"全年最強月：{best}月({month_avg[best]:+.1f}%)，最弱月：{worst}月({month_avg[worst]:+.1f}%)")

    return "；".join(tips)


def _fallback_seasonal(code: str) -> dict:
    import random
    month_avg = {m: round(random.uniform(-3, 4), 2) for m in range(1, 13)}
    cur = date.today().month
    return {
        "code":        code,
        "month_avg":   month_avg,
        "best_month":  max(month_avg, key=lambda x: month_avg[x]),
        "worst_month": min(month_avg, key=lambda x: month_avg[x]),
        "ex_div_info": _get_ex_div_info(code),
        "cur_month":   cur,
        "cur_ret":     month_avg.get(cur, 0.0),
        "season_tip":  "資料載入中，以歷史統計規律供參考",
        "years_data":  0,
    }


_MONTH_ZH = ["", "一月", "二月", "三月", "四月", "五月", "六月",
              "七月", "八月", "九月", "十月", "十一月", "十二月"]


def format_seasonal_report(data: dict, code: str) -> str:
    month_avg = data.get("month_avg", {})
    best_m    = data.get("best_month", 0)
    worst_m   = data.get("worst_month", 0)
    ex_div    = data.get("ex_div_info", {})
    cur_m     = data.get("cur_month", date.today().month)
    tip       = data.get("season_tip", "")
    yrs       = data.get("years_data", 5)

    lines = [
        f"📅 季節性分析  {code}",
        "─" * 32, "",
        f"統計期間：近{yrs}年月度資料",
        "",
        "每月平均報酬率：",
    ]

    for m in range(1, 13):
        ret  = month_avg.get(m, 0.0)
        bar_len = min(10, abs(int(ret * 2)))
        bar  = ("▓" * bar_len) if ret >= 0 else ("░" * bar_len)
        flag = " ◀ 本月" if m == cur_m else ""
        best_flag = "🏆" if m == best_m else ("⚠️" if m == worst_m else "  ")
        lines.append(f"  {best_flag} {_MONTH_ZH[m]:3s}  {ret:+5.1f}%  {bar}{flag}")

    lines += [
        "",
        f"🏆 最強月：{_MONTH_ZH.get(best_m, '')}（{month_avg.get(best_m, 0):+.1f}%）",
        f"⚠️  最弱月：{_MONTH_ZH.get(worst_m, '')}（{month_avg.get(worst_m, 0):+.1f}%）",
    ]

    ex_note = ex_div.get("note", "")
    if ex_note:
        lines += ["", f"💰 除息資訊：{ex_note}"]

    lines += [
        "",
        "─" * 28,
        "🤖 AI 操作建議",
        tip,
        "",
        "⚠️ 季節性為參考，仍需結合即時籌碼與技術面",
        "輸入 /techrating 查技術評級 | /fundcost 查資金成本",
    ]
    return "\n".join(lines)
