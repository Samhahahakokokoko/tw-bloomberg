"""財報提醒服務

台股財報公布時間規律：
  Q1 (1-3月)：5月15日前
  Q2 (4-6月)：8月14日前
  Q3 (7-9月)：11月14日前
  年報 (全年)：3月31日前

功能：
  - 使用者可手動新增財報提醒（指定股票+預計公布日）
  - 每日早上自動檢查，提前 N 天發送 LINE 提醒
  - 公布後可記錄實際 EPS 與市場預期
  - 從 TWSE OpenAPI 抓最新 EPS（估值參考）
"""
import httpx
from datetime import datetime, date, timedelta
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..models.database import AsyncSessionLocal
from ..models.models import EarningsReminder
from .twse_service import fetch_realtime_quote


# ── 台股標準財報期別估算 ─────────────────────────────────────────────────────

def _estimate_announce_date(period: str) -> str:
    """根據財報期別估算公布截止日（回傳 YYYY-MM-DD）"""
    period = period.upper().strip()
    now = datetime.now()
    year = now.year

    # 解析年份
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
    # 若未填公布日，嘗試自動估算
    if not announce_date and period:
        announce_date = _estimate_announce_date(period)

    # 查股票名稱
    stock_name = ""
    try:
        q = await fetch_realtime_quote(stock_code)
        stock_name = q.get("name", "")
    except Exception:
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
        # 計算距離公布日天數
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
    """
    從 TWSE OpenAPI 抓最新 EPS 資料。
    端點：openapi.twse.com.tw/v1/opendata/t187ap06_L
    """
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap06_L"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            # 找對應的股票代碼
            matches = [x for x in data if x.get("公司代號", "") == stock_code]
            if not matches:
                return {}
            # 取最新一筆（通常最後一筆是最新）
            latest = matches[-1]
            return {
                "stock_code":   stock_code,
                "stock_name":   latest.get("公司名稱", ""),
                "year":         latest.get("年度", ""),
                "season":       latest.get("季別", ""),
                "eps":          _safe_float(latest.get("基本每股盈餘", "")),
                "revenue":      latest.get("營業收入", ""),
                "net_income":   latest.get("本期淨利", ""),
            }
    except Exception as e:
        logger.error(f"Fetch latest EPS error {stock_code}: {e}")
    return {}


# ── 每日定時檢查並推播 ─────────────────────────────────────────────────────────

async def check_and_push_reminders():
    """
    每日 08:00 執行：
    - 找出 announce_date 在今天或未來 N 天內的提醒
    - 推播 LINE 通知
    """
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
        # 提前 remind_days_before 天或當天
        if 0 <= days_left <= (r.remind_days_before or 3):
            await _push_earnings_reminder(r, days_left)
            # 當天才標記已提醒（避免只有 3 天前提醒完就不再提）
            if days_left == 0:
                async with AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(EarningsReminder).where(EarningsReminder.id == r.id)
                    )
                    rec = result.scalar_one_or_none()
                    if rec:
                        rec.is_reminded = True
                        await db.commit()


async def _push_earnings_reminder(r: EarningsReminder, days_left: int):
    from ..models.database import settings
    from ..models.models import Subscriber
    from .morning_report import _push_to_users

    if days_left == 0:
        timing = "今天"
    elif days_left == 1:
        timing = "明天"
    else:
        timing = f"{days_left} 天後"

    eps_info = ""
    if r.expected_eps:
        eps_info = f"\n市場預期 EPS：{r.expected_eps}"

    msg = (
        f"📊 財報提醒\n"
        f"{r.stock_code} {r.stock_name or ''}\n"
        f"📅 {r.period or ''} 財報預計 {timing}（{r.announce_date}）公布{eps_info}\n"
        f"請留意最新財報數據！"
    )

    # 優先推到 reminder 指定的 line_user_id
    line_ids: list[str] = []
    if r.line_user_id:
        line_ids.append(r.line_user_id)
    elif r.user_id:
        # 用 user_id 找 Subscriber 取 LINE ID
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Subscriber).where(Subscriber.line_user_id == r.user_id)
            )
            sub = result.scalar_one_or_none()
            if sub:
                line_ids.append(sub.line_user_id)

    if line_ids and settings.line_channel_access_token:
        await _push_to_users(line_ids, msg)
        logger.info(f"Earnings reminder pushed: {r.stock_code} {r.period}")
    else:
        logger.info(f"Earnings reminder (no LINE): {r.stock_code} {r.period} — {days_left}d left")


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
