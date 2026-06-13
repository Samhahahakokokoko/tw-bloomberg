"""除權息服務 — TWSE 除權息資料 + DB 同步 + LINE 提醒"""
from __future__ import annotations

import calendar
import httpx
from datetime import datetime, date, timedelta
from loguru import logger


# ── TWSE API 抓取 ──────────────────────────────────────────────────────────────

async def fetch_upcoming_dividends(days_ahead: int = 60) -> list[dict]:
    """抓近期除權息日期（OpenAPI → 傳統 TWSE 雙重 fallback）"""
    # ── Try 1: OpenAPI endpoint ──────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get("https://openapi.twse.com.tw/v1/exchangeReport/TWT49U")
            ct = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "json" in ct:
                data = resp.json()
                if isinstance(data, list) and data:
                    results = []
                    for item in data:
                        ex_date = _tw_date(item.get("ExRightDate", "") or item.get("ExDividendDate", ""))
                        if not ex_date:
                            continue
                        results.append({
                            "stock_code":            item.get("Code", ""),
                            "stock_name":            item.get("Name", ""),
                            "ex_dividend_date":      ex_date,
                            "ex_dividend_ref_price": _f(item.get("ExRightReferencePrice")),
                            "cash_dividend":         _f(item.get("CashDividend")),
                            "stock_dividend":        _f(item.get("StockDividend")),
                            "total_dividend":        _f(item.get("TotalDividend")),
                        })
                    if results:
                        logger.info(f"[dividend] openapi: {len(results)} records")
                        return results
    except Exception as e:
        logger.warning(f"[dividend] openapi failed: {e}")

    # ── Try 2: Traditional TWSE endpoint (按月查詢) ──────────────────────────
    return await _fetch_twse_traditional(days_ahead)


async def _fetch_twse_traditional(days_ahead: int = 60) -> list[dict]:
    """TWSE 傳統端點 fallback：按月份分批查詢 TWT49U"""
    today = date.today()
    months: dict[tuple, tuple] = {}
    for offset in range(0, days_ahead + 1, 15):
        d = today + timedelta(days=offset)
        key = (d.year, d.month)
        if key not in months:
            last = calendar.monthrange(d.year, d.month)[1]
            months[key] = (f"{d.year}{d.month:02d}01", f"{d.year}{d.month:02d}{last:02d}")

    results: list[dict] = []
    seen: set[tuple] = set()

    for (year, month), (start, end) in sorted(months.items()):
        url = (
            f"https://www.twse.com.tw/exchangeReport/TWT49U"
            f"?response=json&strDate={start}&endDate={end}"
        )
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(url)
                data = resp.json()
            if data.get("stat") != "OK":
                continue
            fields = data.get("fields", [])
            col = {f: i for i, f in enumerate(fields)}
            for row in data.get("data", []):
                item = _parse_traditional_row(row, col)
                if item:
                    key = (item["stock_code"], item["ex_dividend_date"])
                    if key not in seen:
                        seen.add(key)
                        results.append(item)
        except Exception as e:
            logger.warning(f"[dividend] traditional {year}/{month} error: {e}")

    logger.info(f"[dividend] traditional fallback: {len(results)} records")
    return results


def _parse_traditional_row(row: list, col: dict) -> dict | None:
    """解析 TWSE 傳統格式（fields 對應動態列索引）"""
    if not row:
        return None

    def gc(*names: str, pos: int | None = None) -> str:
        for n in names:
            if n in col and col[n] < len(row):
                return str(row[col[n]]).strip()
        if pos is not None and pos < len(row):
            return str(row[pos]).strip()
        return ""

    code  = gc("股票代號", "代號", "證券代號", pos=0)
    name  = gc("股票名稱", "名稱", "證券名稱", pos=1)
    # prefer 除息日（配息），fallback to 除權日（配股）
    ex_raw = gc("除息日", "除權日", "除權息日", pos=2)
    ex_date = _tw_date(ex_raw)
    if not ex_date or not code:
        return None

    cash  = _f(gc("現金股利", "每股現金股利", pos=5))
    stock = _f(gc("股票股利", "每股股票股利", pos=6))
    ref   = _f(gc("除權息參考價", "填息參考價", "除息參考價", pos=7))

    return {
        "stock_code":            code,
        "stock_name":            name,
        "ex_dividend_date":      ex_date,
        "ex_dividend_ref_price": ref,
        "cash_dividend":         cash,
        "stock_dividend":        stock,
        "total_dividend":        cash + stock,
    }


async def fetch_dividend_by_code(stock_code: str) -> list[dict]:
    """查單一股票的近期除權息"""
    # 先查 DB，fallback 到 API
    try:
        from ..models.database import AsyncSessionLocal
        from ..models.models import DividendCalendar
        from sqlalchemy import select

        today = date.today().isoformat()
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(DividendCalendar)
                .where(
                    DividendCalendar.stock_code == stock_code,
                    DividendCalendar.ex_date    >= today,
                )
                .order_by(DividendCalendar.ex_date)
                .limit(5)
            )
            rows = r.scalars().all()
            if rows:
                return [_row_to_dict(row) for row in rows]
    except Exception as e:
        logger.warning(f"[dividend] DB query failed, falling back to API: {e}")

    all_divs = await fetch_upcoming_dividends()
    return [d for d in all_divs if d["stock_code"] == stock_code]


async def fetch_historical_dividends(stock_code: str) -> list[dict]:
    """近年配息紀錄（使用 TWSE 年度除權息資料）"""
    year = datetime.now().year
    results = []
    for y in [year, year - 1]:
        url = f"https://www.twse.com.tw/exchangeReport/TWT49U?response=json&strDate={y}0101&endDate={y}1231"
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url)
                data = resp.json()
                for row in data.get("data", []):
                    if len(row) > 1 and row[0] == stock_code:
                        results.append({
                            "stock_code": stock_code,
                            "date": row[1] if len(row) > 1 else "",
                            "cash": _f(row[5]) if len(row) > 5 else 0,
                            "stock": _f(row[6]) if len(row) > 6 else 0,
                        })
        except Exception as e:
            logger.error(f"Historical dividend error: {e}")
    return results


# ── DB 同步 ────────────────────────────────────────────────────────────────────

async def sync_to_db() -> int:
    """從 TWSE 抓取除權息資料並同步到 dividend_calendar，回傳新增筆數"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import DividendCalendar
    from sqlalchemy import select

    items = await fetch_upcoming_dividends()
    if not items:
        return 0

    saved = 0
    async with AsyncSessionLocal() as db:
        for item in items:
            code    = item["stock_code"]
            ex_date = item["ex_dividend_date"]
            if not code or not ex_date:
                continue
            try:
                r = await db.execute(
                    select(DividendCalendar).where(
                        DividendCalendar.stock_code == code,
                        DividendCalendar.ex_date    == ex_date,
                    )
                )
                row = r.scalar_one_or_none()
                if row:
                    row.cash_dividend  = item["cash_dividend"]
                    row.stock_dividend = item["stock_dividend"]
                    row.ref_price      = item["ex_dividend_ref_price"]
                    row.stock_name     = item["stock_name"]
                    row.updated_at     = datetime.utcnow()
                else:
                    db.add(DividendCalendar(
                        stock_code     = code,
                        stock_name     = item["stock_name"],
                        ex_date        = ex_date,
                        cash_dividend  = item["cash_dividend"],
                        stock_dividend = item["stock_dividend"],
                        ref_price      = item["ex_dividend_ref_price"],
                    ))
                    saved += 1
            except Exception as e:
                logger.warning(f"[dividend] sync row error {code}: {e}")
        await db.commit()

    logger.info(f"[dividend] sync_to_db: {saved} new / {len(items)} total")
    return saved


# ── 持股提醒掃描 ───────────────────────────────────────────────────────────────

async def scan_dividend_reminders() -> int:
    """
    掃描所有持股，找出 7 天內或 1 天內的除權息日，
    對尚未通知的用戶推播 LINE。
    回傳推播次數。
    """
    from ..models.database import AsyncSessionLocal
    from ..models.models import DividendCalendar, DividendNotification, Portfolio
    from ..services.line_push import push_line_messages
    from sqlalchemy import select, or_

    today      = date.today()
    today_str  = today.isoformat()
    d7_str     = (today + timedelta(days=7)).isoformat()
    d1_str     = (today + timedelta(days=1)).isoformat()
    pushed     = 0

    try:
        # 1. 找出 7 天內的除權息
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(DividendCalendar).where(
                    DividendCalendar.ex_date > today_str,
                    DividendCalendar.ex_date <= d7_str,
                )
            )
            upcoming = r.scalars().all()

        if not upcoming:
            return 0

        # 2. 找出所有持股
        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Portfolio))
            all_holdings = r.scalars().all()

        # 3. 對每個 (用戶, 股票, 除權息日) 判斷通知時機
        async with AsyncSessionLocal() as db:
            for div in upcoming:
                ex_date = div.ex_date
                days_left = (date.fromisoformat(ex_date) - today).days

                # 7天提醒：days_left 5-7（週末/假日落差容錯）
                # 1天提醒：days_left 1-2（週五→週一 gap 補足）
                if 5 <= days_left <= 7:
                    remind_type = 7
                elif 1 <= days_left <= 2:
                    remind_type = 1
                else:
                    continue

                # 找持有此股的用戶
                holders = [h for h in all_holdings if h.stock_code == div.stock_code]
                for holding in holders:
                    uid = holding.user_id
                    if not uid:
                        continue

                    # 已通知過？
                    r2 = await db.execute(
                        select(DividendNotification).where(
                            DividendNotification.user_id     == uid,
                            DividendNotification.stock_code  == div.stock_code,
                            DividendNotification.ex_date     == ex_date,
                            DividendNotification.days_before == remind_type,
                        )
                    )
                    if r2.scalar_one_or_none():
                        continue

                    # 計算預計配息
                    lots   = holding.shares // 1000
                    odd    = holding.shares % 1000
                    shares = holding.shares
                    cash_div = div.cash_dividend or 0
                    stock_div = div.stock_dividend or 0
                    estimated_cash = round(cash_div * shares, 0)

                    msg = _build_reminder(
                        code       = div.stock_code,
                        name       = div.stock_name or div.stock_code,
                        ex_date    = ex_date,
                        days_left  = days_left,
                        cash_div   = cash_div,
                        stock_div  = stock_div,
                        shares     = shares,
                        est_cash   = estimated_cash,
                    )

                    qr = {"items": [
                        {"type": "action", "action": {
                            "type": "message", "label": "📋 查除息清單", "text": "/exdiv",
                        }},
                        {"type": "action", "action": {
                            "type": "message", "label": "💼 看庫存", "text": "/p",
                        }},
                        {"type": "action", "action": {
                            "type": "message", "label": f"💰 {div.stock_code}除息",
                            "text": f"/dividend {div.stock_code}",
                        }},
                        {"type": "action", "action": {
                            "type": "message", "label": "🔍 AI分析",
                            "text": f"/ai {div.stock_code} 除息值不值得參與？",
                        }},
                    ]}
                    await push_line_messages(
                        uid,
                        [{"type": "text", "text": msg, "quickReply": qr}],
                        timeout=10, context="dividend_reminder",
                    )

                    # 記錄已通知
                    db.add(DividendNotification(
                        user_id     = uid,
                        stock_code  = div.stock_code,
                        ex_date     = ex_date,
                        days_before = remind_type,
                    ))
                    await db.commit()
                    pushed += 1
                    logger.info(
                        "[dividend] reminded uid=%s %s ex=%s days=%d",
                        uid[:8], div.stock_code, ex_date, days_left,
                    )

    except Exception as e:
        logger.error("[dividend] scan_dividend_reminders failed: {}", e)

    return pushed


# ── 持股除權息清單（/exdiv 用）─────────────────────────────────────────────────

async def get_exdiv_for_user(user_id: str, days_ahead: int = 30) -> list[dict]:
    """取得用戶持股中近期除權息清單"""
    from ..models.database import AsyncSessionLocal
    from ..models.models import DividendCalendar, Portfolio
    from sqlalchemy import select

    today    = date.today().isoformat()
    deadline = (date.today() + timedelta(days=days_ahead)).isoformat()

    try:
        async with AsyncSessionLocal() as db:
            # 用戶持股
            r = await db.execute(
                select(Portfolio).where(Portfolio.user_id == user_id)
            )
            holdings = {h.stock_code: h for h in r.scalars().all()}

            if not holdings:
                return []

            # 近期除權息
            r2 = await db.execute(
                select(DividendCalendar).where(
                    DividendCalendar.stock_code.in_(list(holdings.keys())),
                    DividendCalendar.ex_date > today,
                    DividendCalendar.ex_date <= deadline,
                ).order_by(DividendCalendar.ex_date)
            )
            divs = r2.scalars().all()

        result = []
        for div in divs:
            h = holdings.get(div.stock_code)
            if not h:
                continue
            shares   = h.shares
            cash_div = div.cash_dividend or 0
            est_cash = round(cash_div * shares, 0)
            days_left = (date.fromisoformat(div.ex_date) - date.today()).days
            result.append({
                "stock_code":   div.stock_code,
                "stock_name":   div.stock_name or div.stock_code,
                "ex_date":      div.ex_date,
                "days_left":    days_left,
                "cash_div":     cash_div,
                "stock_div":    div.stock_dividend or 0,
                "shares":       shares,
                "est_cash":     est_cash,
                "cost_price":   h.cost_price,
            })
        return result
    except Exception as e:
        logger.warning(f"[dividend] get_exdiv_for_user failed: {e}")
        return []


# ── 訊息格式 ──────────────────────────────────────────────────────────────────

def _build_reminder(
    code: str, name: str, ex_date: str, days_left: int,
    cash_div: float, stock_div: float,
    shares: int, est_cash: float,
) -> str:
    lots = shares // 1000
    odd  = shares % 1000
    qty  = f"{lots}張" if odd == 0 else f"{lots}張{odd}股"
    if lots == 0:
        qty = f"{shares}股"

    stock_line = f"\n每股配股：{stock_div:.2f}股" if stock_div else ""
    return (
        f"📅 除權息提醒\n"
        f"{code} {name}\n"
        f"─────────────\n"
        f"除息日：{ex_date}（還有{days_left}天）\n"
        f"每股配息：{cash_div:.2f}元{stock_line}\n"
        f"你持有：{qty}\n"
        f"預計配息：${est_cash:,.0f}\n"
        f"─────────────\n"
        f"注意：除息後股價會下調\n"
        f"建議：評估是否參與除息\n\n"
        f"查庫存：/p　查個股：/dividend {code}"
    )


def format_dividend_for_line(code: str, divs: list[dict]) -> str:
    """格式化個股除權息查詢結果"""
    if not divs:
        return f"❌ 查無 {code} 近期除權息資料"

    today = date.today()
    lines = [f"💰 {code} 除權息資料", "─" * 18]
    for d in divs[:4]:
        ex_date = d.get("ex_dividend_date") or d.get("ex_date", "")
        cash    = d.get("cash_dividend", 0) or d.get("cash_div", 0)
        stock   = d.get("stock_dividend", 0) or d.get("stock_div", 0)
        ref     = d.get("ex_dividend_ref_price") or d.get("ref_price", 0)
        # 計算距離天數
        try:
            days_left = (date.fromisoformat(ex_date) - today).days
            day_str = f"（還有{days_left}天）" if days_left > 0 else "（今日）" if days_left == 0 else f"（{abs(days_left)}天前）"
        except Exception as e:
            day_str = ""
        cash_str  = f"現金：{cash:.2f}元" if cash else ""
        stock_str = f"　股票：{stock:.2f}股" if stock else ""
        ref_str   = f"\n　填息參考：{ref:.0f}" if ref else ""
        lines.append(f"📅 {ex_date}{day_str}")
        if cash_str or stock_str:
            lines.append(f"　{cash_str}{stock_str}{ref_str}")
    return "\n".join(lines)


def format_exdiv_list(items: list[dict]) -> str:
    """格式化持股除權息清單"""
    if not items:
        return (
            "📋 近30天持股中無除權息\n\n"
            "查個股：/dividend 2330"
        )
    lines = [f"📋 持股除權息清單（{len(items)} 檔）", "─" * 18]
    for it in items:
        qty = it["shares"] // 1000
        odd = it["shares"] % 1000
        qty_str = f"{qty}張" if odd == 0 else f"{qty}張{odd}股"
        if qty == 0:
            qty_str = f"{it['shares']}股"
        cash = it["cash_div"]
        est  = it["est_cash"]
        lines.append(
            f"📅 {it['stock_code']} {it['stock_name']}\n"
            f"   除息：{it['ex_date']}（{it['days_left']}天後）\n"
            f"   配息：{cash:.2f}元／股　持有：{qty_str}\n"
            f"   預計：${est:,.0f}"
        )
    return "\n".join(lines)


# ── 工具函式 ──────────────────────────────────────────────────────────────────

def _tw_date(s: str) -> str:
    """民國日期轉西元 → YYYY-MM-DD（支援 1140301 / 114/03/01 / 114-03-01）"""
    try:
        s = str(s).strip()
        if not s:
            return s
        # slash/dash separated: 114/03/01 or 114-03-01
        for sep in ("/", "-"):
            if sep in s:
                parts = s.split(sep)
                if len(parts) == 3 and len(parts[0]) in (3, 4):
                    y = int(parts[0]) + 1911
                    return f"{y}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
        # compact 7-digit: 1140301
        digits = s.replace("/", "").replace("-", "")
        if len(digits) == 7 and digits.isdigit():
            y = int(digits[:3]) + 1911
            return f"{y}-{digits[3:5]}-{digits[5:7]}"
    except Exception as e:
        pass
    return s


def _f(v) -> float:
    try:
        return float(str(v).replace(",", "") or 0)
    except Exception as e:
        return 0.0


def _row_to_dict(row) -> dict:
    return {
        "stock_code":           row.stock_code,
        "stock_name":           row.stock_name,
        "ex_dividend_date":     row.ex_date,
        "cash_dividend":        row.cash_dividend,
        "stock_dividend":       row.stock_dividend,
        "ex_dividend_ref_price": row.ref_price,
        "total_dividend":       (row.cash_dividend or 0) + (row.stock_dividend or 0),
    }
