"""財報提醒服務

台股財報公布時間規律：
  Q1 (1-3月)：5月15日前
  Q2 (4-6月)：8月14日前
  Q3 (7-9月)：11月14日前
  年報 (全年)：3月31日前（次年）

功能：
  - sync_portfolio_reminders(line_user_id) — 自動同步持股財報提醒
  - check_and_push_reminders()            — 14日 / 3日兩階段提醒推播
  - fetch_latest_eps(stock_code)          — TWSE OpenAPI 最新 EPS
  - get_portfolio_earnings_calendar()     — 持股財報日曆（供 /earnings 指令）
  - get_stock_earnings_info(code)         — 個股財報資訊（供 /earnings CODE 指令）
  - CRUD: add_reminder / list_reminders / delete_reminder / update_actual_eps
"""
import httpx
from datetime import datetime, date, timedelta
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import re
from ..models.database import AsyncSessionLocal
from ..models.models import EarningsReminder
from .twse_service import fetch_realtime_quote


# ── 台股標準財報期別估算 ─────────────────────────────────────────────────────

def _estimate_announce_date(period: str) -> str:
    """根據財報期別估算公布截止日（回傳 YYYY-MM-DD）"""
    period = period.upper().strip()
    now = datetime.now()
    year = now.year

    for y in range(year - 1, year + 2):
        if str(y) in period:
            year = y
            break

    if "Q1" in period:
        return f"{year}-05-15"
    elif "Q2" in period or "H1" in period:
        return f"{year}-08-14"
    elif "Q3" in period:
        return f"{year}-11-14"
    elif "Q4" in period or "ANNUAL" in period or "年報" in period:
        return f"{year + 1}-03-31"
    return ""


def _upcoming_quarters() -> list[tuple[str, str]]:
    """回傳未來尚未截止的財報期別 [(period, announce_date), ...]"""
    today = date.today()
    year = today.year
    quarters = [
        (f"{year}Q1", f"{year}-05-15"),
        (f"{year}Q2", f"{year}-08-14"),
        (f"{year}Q3", f"{year}-11-14"),
        (f"{year}Q4", f"{year + 1}-03-31"),
        (f"{year + 1}Q1", f"{year + 1}-05-15"),
    ]
    return [(p, d) for p, d in quarters if d >= today.isoformat()][:2]


# ── 自動同步持股財報提醒 ──────────────────────────────────────────────────────

async def sync_portfolio_reminders(line_user_id: str) -> int:
    """
    根據使用者目前持股，自動建立（或補建）未截止季度的財報提醒。
    回傳新建數量。
    """
    from ..models.models import Portfolio
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(Portfolio).where(Portfolio.user_id == line_user_id)
        )
        holdings = r.scalars().all()

    if not holdings:
        return 0

    quarters = _upcoming_quarters()
    created = 0
    for holding in holdings:
        for period, ann_date in quarters:
            async with AsyncSessionLocal() as db:
                existing = await db.execute(
                    select(EarningsReminder).where(
                        EarningsReminder.user_id == line_user_id,
                        EarningsReminder.stock_code == holding.stock_code,
                        EarningsReminder.period == period,
                    )
                )
                if existing.scalar_one_or_none():
                    continue
                db.add(EarningsReminder(
                    user_id=line_user_id,
                    line_user_id=line_user_id,
                    stock_code=holding.stock_code,
                    stock_name=holding.stock_name or "",
                    period=period,
                    announce_date=ann_date,
                    remind_days_before=14,
                    is_reminded=False,
                ))
                await db.commit()
                created += 1

    return created


# ── CRUD ────────────────────────────────────────────────────────────────────

async def add_reminder(
    db: AsyncSession,
    user_id: str,
    stock_code: str,
    period: str,
    announce_date: str = "",
    remind_days_before: int = 3,
    line_user_id: str = "",
    expected_eps: float | None = None,
) -> dict:
    if not announce_date and period:
        announce_date = _estimate_announce_date(period)

    stock_name = ""
    try:
        q = await fetch_realtime_quote(stock_code)
        stock_name = q.get("name", "")
    except Exception as e:
        pass

    reminder = EarningsReminder(
        user_id=user_id,
        line_user_id=line_user_id,
        stock_code=stock_code,
        stock_name=stock_name,
        period=period,
        announce_date=announce_date,
        remind_days_before=remind_days_before,
        expected_eps=expected_eps,
    )
    db.add(reminder)
    await db.commit()
    await db.refresh(reminder)
    return _to_dict(reminder)


async def list_reminders(db: AsyncSession, user_id: str = "") -> list[dict]:
    q = select(EarningsReminder)
    if user_id is not None:
        q = q.where(EarningsReminder.user_id == user_id)
    q = q.order_by(EarningsReminder.announce_date)
    result = await db.execute(q)
    reminders = result.scalars().all()

    today = date.today()
    output = []
    for r in reminders:
        d = _to_dict(r)
        if r.announce_date:
            try:
                ann = date.fromisoformat(r.announce_date)
                d["days_until"] = (ann - today).days
                d["is_overdue"] = ann < today
            except ValueError:
                d["days_until"] = None
                d["is_overdue"] = False
        else:
            d["days_until"] = None
            d["is_overdue"] = False
        output.append(d)
    return output


async def delete_reminder(db: AsyncSession, reminder_id: int, user_id: str = "") -> bool:
    q = select(EarningsReminder).where(EarningsReminder.id == reminder_id)
    result = await db.execute(q)
    r = result.scalar_one_or_none()
    if not r:
        return False
    await db.delete(r)
    await db.commit()
    return True


async def update_actual_eps(db: AsyncSession, reminder_id: int, actual_eps: float) -> dict | None:
    result = await db.execute(
        select(EarningsReminder).where(EarningsReminder.id == reminder_id)
    )
    r = result.scalar_one_or_none()
    if not r:
        return None
    r.actual_eps = actual_eps
    await db.commit()
    await db.refresh(r)
    return _to_dict(r)


# ── 自動抓最新 EPS（TWSE OpenAPI）─────────────────────────────────────────────

async def fetch_latest_eps(stock_code: str) -> dict:
    """從 TWSE OpenAPI 抓最新 EPS 資料"""
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap06_L"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            matches = [x for x in data if x.get("公司代號", "") == stock_code]
            if not matches:
                return {}
            latest = matches[-1]
            return {
                "stock_code": stock_code,
                "stock_name": latest.get("公司名稱", ""),
                "year":       latest.get("年度", ""),
                "season":     latest.get("季別", ""),
                "eps":        _safe_float(latest.get("基本每股盈餘", "")),
                "revenue":    latest.get("營業收入", ""),
                "net_income": latest.get("本期淨利", ""),
            }
    except Exception as e:
        logger.error(f"Fetch latest EPS error {stock_code}: {e}")
    return {}


# ── 每日定時檢查並推播（14日 + 3日兩階段）─────────────────────────────────────

async def check_and_push_reminders():
    """
    每日 08:15 執行：
    - days_left ≤ 14 → 14日前提醒（每個 stock+period 只推一次）
    - days_left ≤ 3  → 3日前二次提醒（每個 stock+period 只推一次）
    - 使用 push_dedup period_key 避免重複推播
    """
    from .push_dedup import check_and_record

    today = date.today()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(EarningsReminder).where(EarningsReminder.is_reminded == False)
        )
        reminders = result.scalars().all()

    for r in reminders:
        if not r.announce_date:
            continue
        try:
            ann_date = date.fromisoformat(r.announce_date)
        except ValueError:
            continue

        days_left = (ann_date - today).days

        if days_left < 0:
            # 截止日已過，標記完成
            async with AsyncSessionLocal() as db:
                rec = (await db.execute(
                    select(EarningsReminder).where(EarningsReminder.id == r.id)
                )).scalar_one_or_none()
                if rec:
                    rec.is_reminded = True
                    await db.commit()
            continue

        line_id = r.line_user_id or r.user_id
        if not line_id:
            continue

        # 3日里程碑（優先，比 14 日更緊急）
        if days_left <= 3:
            pk = f"{r.stock_code}_{r.period}_3d"
            if await check_and_record(line_id, "earnings_3d", pk, period_key=pk):
                await _push_earnings_reminder(r, days_left, milestone="3d")
            # 當天截止時標記完成
            if days_left == 0:
                async with AsyncSessionLocal() as db:
                    rec = (await db.execute(
                        select(EarningsReminder).where(EarningsReminder.id == r.id)
                    )).scalar_one_or_none()
                    if rec:
                        rec.is_reminded = True
                        await db.commit()

        # 14日里程碑
        elif days_left <= 14:
            pk = f"{r.stock_code}_{r.period}_14d"
            if await check_and_record(line_id, "earnings_14d", pk, period_key=pk):
                await _push_earnings_reminder(r, days_left, milestone="14d")


async def _push_earnings_reminder(r: EarningsReminder, days_left: int, milestone: str = ""):
    from ..models.database import settings
    from .morning_report import _push_to_users

    eps_info = await fetch_latest_eps(r.stock_code)
    eps_line = ""
    if eps_info.get("eps") is not None:
        season_label = f"{eps_info.get('year', '')}Q{eps_info.get('season', '')}" if eps_info.get('season') else ""
        eps_line = f"上季 EPS：{eps_info['eps']:.2f} 元"
        if season_label:
            eps_line = f"{season_label} EPS：{eps_info['eps']:.2f} 元"

    if milestone == "3d":
        header = f"⚠️ 財報截止 {days_left} 天前"
        advice = "請確認是否已充分評估財報風險"
    else:
        header = "📊 財報提醒（14天前預告）"
        advice = "建議：財報前留意法說會消息"

    lines = [
        header,
        f"{r.stock_code} {r.stock_name or ''}".strip(),
        f"📅 {r.period} 截止：{r.announce_date}（還有 {days_left} 天）",
    ]
    if eps_line:
        lines.append(eps_line)
    lines.append(advice)
    msg = "\n".join(lines)

    line_ids = []
    if r.line_user_id:
        line_ids.append(r.line_user_id)
    elif r.user_id:
        from ..models.models import Subscriber
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Subscriber).where(Subscriber.line_user_id == r.user_id)
            )
            sub = result.scalar_one_or_none()
            if sub:
                line_ids.append(sub.line_user_id)

    if line_ids and settings.line_channel_access_token:
        await _push_to_users(line_ids, msg)
        logger.info(f"[earnings] {milestone or '?'} reminder pushed: {r.stock_code} {r.period} ({days_left}d left)")
    else:
        logger.info(f"[earnings] {milestone} reminder (no LINE): {r.stock_code} {r.period}")


# ── 持股財報日曆（供 /earnings 無代碼時使用）──────────────────────────────────

async def get_portfolio_earnings_calendar(line_user_id: str) -> str:
    """
    同步持股提醒後，回傳格式化的財報日曆文字。
    """
    await sync_portfolio_reminders(line_user_id)

    today = date.today()
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(EarningsReminder)
            .where(
                EarningsReminder.user_id == line_user_id,
                EarningsReminder.is_reminded == False,
            )
            .order_by(EarningsReminder.announce_date)
        )
        reminders = r.scalars().all()

    if not reminders:
        return (
            "📅 我的持股財報日曆\n"
            "─" * 20 + "\n"
            "目前無持股財報提醒\n\n"
            "輸入 /portfolio 查看持股\n"
            "輸入 /earnings 2330 查看個股財報"
        )

    lines = ["📅 我的持股財報日曆", "─" * 20]
    for r in reminders:
        try:
            ann = date.fromisoformat(r.announce_date)
            days_left = (ann - today).days
        except ValueError:
            days_left = None

        if days_left is not None and days_left < 0:
            continue

        name = r.stock_name or ""
        code_name = f"{r.stock_code} {name}".strip()
        period_short = r.period or ""

        if days_left is not None:
            if days_left == 0:
                urgency = "🔴 今天截止"
            elif days_left <= 3:
                urgency = f"⚠️ 還有 {days_left} 天"
            elif days_left <= 14:
                urgency = f"📌 還有 {days_left} 天"
            else:
                urgency = f"還有 {days_left} 天"
        else:
            urgency = ""

        date_str = r.announce_date or ""
        lines.append(f"\n{code_name}")
        lines.append(f"   {period_short} 截止 {date_str}  {urgency}")

    lines.append("\n─" * 20)
    lines.append("輸入 /earnings [代碼] 查看個股財報詳情")
    return "\n".join(lines)


# ── 個股財報資訊（供 /earnings CODE 時使用）────────────────────────────────────

async def get_stock_earnings_info(stock_code: str) -> str:
    """回傳個股下一次財報截止日 + 最新 EPS 資訊"""
    today = date.today()
    quarters = _upcoming_quarters()

    lines = []

    # 基本報價查股名
    stock_name = ""
    try:
        q = await fetch_realtime_quote(stock_code)
        stock_name = q.get("name", "") or ""
    except Exception as e:
        pass

    title = f"📊 {stock_code} {stock_name} 財報資訊".strip()
    lines.append(title)
    lines.append("─" * 20)

    # 下一個財報截止日
    if quarters:
        next_period, next_date = quarters[0]
        try:
            ann = date.fromisoformat(next_date)
            days_left = (ann - today).days
        except ValueError:
            days_left = None

        lines.append(f"📅 {next_period} 財報截止：{next_date}")
        if days_left is not None:
            if days_left < 0:
                lines.append("  （截止日已過）")
            else:
                lines.append(f"  （還有 {days_left} 天）")

    # 最新 EPS
    eps_info = await fetch_latest_eps(stock_code)
    if eps_info.get("eps") is not None:
        season = eps_info.get("season", "")
        yr = eps_info.get("year", "")
        season_label = f"{yr}Q{season}" if season else str(yr)
        lines.append(f"\n上季 EPS：{eps_info['eps']:.2f} 元  ({season_label})")

    if len(quarters) > 1:
        p2, d2 = quarters[1]
        lines.append(f"\n下下季：{p2} 截止 {d2}")

    lines.append("\n建議：財報前14天及3天前系統將自動推送提醒")
    lines.append("輸入 /earnings 查看所有持股財報日曆")
    return "\n".join(lines)


# ── 輔助函式 ─────────────────────────────────────────────────────────────────

def _to_dict(r: EarningsReminder) -> dict:
    return {
        "id":                r.id,
        "user_id":           r.user_id,
        "stock_code":        r.stock_code,
        "stock_name":        r.stock_name or "",
        "period":            r.period or "",
        "announce_date":     r.announce_date or "",
        "remind_days_before":r.remind_days_before,
        "is_reminded":       r.is_reminded,
        "actual_eps":        r.actual_eps,
        "expected_eps":      r.expected_eps,
        "line_user_id":      r.line_user_id or "",
        "created_at":        r.created_at.isoformat() if r.created_at else "",
    }


def _safe_float(val) -> float | None:
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return None


# ── 法說會日曆（MOPS 公開資訊觀測站）────────────────────────────────────────

async def get_investor_meetings(days: int = 30) -> list[dict]:
    """抓取 MOPS 法說會資訊（未來 N 天）"""
    now = date.today()
    end = now + timedelta(days=days)
    b_date = now.strftime("%Y/%m/%d")
    e_date = end.strftime("%Y/%m/%d")

    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            resp = await client.post(
                "https://mops.twse.com.tw/mops/web/ajax_t05st27",
                data={
                    "encodeURIComponent": "1",
                    "step": "1",
                    "firstin": "1",
                    "b_date": b_date,
                    "e_date": e_date,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Mozilla/5.0",
                },
            )
        if resp.status_code != 200:
            logger.warning(f"MOPS investor meeting fetch: HTTP {resp.status_code}")
            return []
        return _parse_mops_meetings(resp.text)
    except Exception as e:
        logger.error(f"Investor meetings fetch error: {e}")
        return []


def _parse_mops_meetings(html: str) -> list[dict]:
    meetings: list[dict] = []
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)
    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
        cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        cells = [c.replace('&nbsp;', ' ').replace('&amp;', '&').strip() for c in cells]
        if len(cells) >= 3 and re.match(r'\d{4}/\d{2}/\d{2}', cells[0]):
            meetings.append({
                "date":     cells[0],
                "code":     cells[1] if len(cells) > 1 else "",
                "name":     cells[2] if len(cells) > 2 else "",
                "time":     cells[3] if len(cells) > 3 else "",
                "location": cells[4] if len(cells) > 4 else "",
            })
    return meetings


def format_investor_calendar(meetings: list[dict]) -> str:
    if not meetings:
        return (
            "📅 法說會日曆（未來 30 天）\n"
            "─" * 22 + "\n"
            "暫無法說會資料\n\n"
            "資料來源：公開資訊觀測站"
        )

    lines = [
        "📅 法說會日曆（未來 30 天）",
        f"共 {len(meetings)} 場",
        "─" * 22,
    ]
    for m in meetings[:15]:
        d    = m.get("date", "")[-5:]
        code = m.get("code", "")
        name = m.get("name", "")[:8]
        t    = m.get("time", "")[:5]
        lines.append(f"{d}  {code} {name}  {t}".rstrip())

    if len(meetings) > 15:
        lines.append(f"...另有 {len(meetings) - 15} 場")
    lines.append("\n資料來源：MOPS 公開資訊觀測站")
    return "\n".join(lines)


async def push_weekly_investor_meetings():
    """每週一推送本週法說會給所有訂閱者"""
    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from sqlalchemy import select
    from .morning_report import _push_to_users

    meetings = await get_investor_meetings(days=7)
    text = format_investor_calendar(meetings)

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(Subscriber).where(Subscriber.subscribed_morning == True)
        )
        subs = r.scalars().all()

    if not subs:
        return
    await _push_to_users([s.line_user_id for s in subs], text)
    logger.info(f"[earnings] weekly investor meetings pushed to {len(subs)} subscribers")
