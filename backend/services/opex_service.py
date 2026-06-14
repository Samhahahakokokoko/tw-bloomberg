"""台指選擇權結算日服務

每月第三個週三為台指選擇權結算日。
提供查詢、提醒與排程警示功能。
"""
from datetime import date, timedelta


# ── 核心計算 ──────────────────────────────────────────────────────────────────

def get_third_wednesday(year: int, month: int) -> date:
    """每月第三個週三"""
    d = date(year, month, 1)
    days_to_wed = (2 - d.weekday()) % 7  # Mon=0, Wed=2
    first_wed = d + timedelta(days=days_to_wed)
    return first_wed + timedelta(weeks=2)


def get_next_opex(from_date: date = None) -> date:
    """取得下一個台指選擇權結算日"""
    if from_date is None:
        from_date = date.today()
    year, month = from_date.year, from_date.month
    opex = get_third_wednesday(year, month)
    if opex <= from_date:
        month = month % 12 + 1
        year = year + (1 if month == 1 else 0)
        opex = get_third_wednesday(year, month)
    return opex


# ── 公開 API ──────────────────────────────────────────────────────────────────

def get_opex_info() -> str:
    """查詢下次結算日 - /opex"""
    today = date.today()
    next_opex = get_next_opex(today)
    days = (next_opex - today).days

    if days == 0:
        return "🔔 今日為台指選擇權結算日！\n日期：{next_opex}\n⚠️ 注意今日盤中劇烈波動！".format(next_opex=next_opex)
    status = "⚠️ 即將結算，注意波動！" if days <= 3 else "✅ 距結算尚有餘裕"
    return (
        f"📅 台指選擇權結算日\n"
        f"下次結算：{next_opex}（第三個週三）\n"
        f"距今：{days} 天\n"
        f"{status}"
    )


def check_opex_alert() -> str | None:
    """排程用：前3天或結算日當天才返回提醒文字，其他時候返回 None"""
    today = date.today()
    next_opex = get_next_opex(today)
    days = (next_opex - today).days

    if days == 0:
        return (
            f"⚠️【結算日警告】今日為台指選擇權結算日！\n"
            f"日期：{next_opex}\n"
            f"盤中可能劇烈波動，建議保守操作！"
        )
    if 1 <= days <= 3:
        return (
            f"🔔【結算日提醒】距台指選擇權結算日剩 {days} 天\n"
            f"結算日：{next_opex}\n"
            f"請提前規劃倉位，避免受結算波動影響！"
        )
    return None
