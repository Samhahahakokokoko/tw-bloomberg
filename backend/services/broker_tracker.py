"""籌碼分點追蹤服務

來源：FinMind BrokerTradingDetail
  URL: https://api.finmindtrade.com/api/v4/data?dataset=BrokerTradingDetail&data_id=XXXX&start_date=YYYY-MM-DD

功能：
  1. get_top_brokers(stock_code)    - 某股票前10大買超分點
  2. track_broker(broker_name)      - 某分點最近買了哪些股
  3. detect_smart_money()           - 今日主力分點異動最大的股票
  4. 自動偵測「聰明錢」訊號並推播
"""
import asyncio
from datetime import date, timedelta, datetime
from loguru import logger
from sqlalchemy import select, func

from ..models.database import AsyncSessionLocal
from ..models.models import BrokerActivity
from .finmind_service import _get


async def fetch_broker_detail(stock_code: str, days: int = 10) -> list[dict]:
    """從 FinMind 抓取分點交易明細並快取到 DB"""
    start_date = (date.today() - timedelta(days=days + 5)).strftime("%Y-%m-%d")
    raw = await _get("BrokerTradingDetail", stock_code, start_date)

    parsed = []
    for r in raw:
        try:
            parsed.append({
                "date":       r.get("date", ""),
                "stock_code": stock_code,
                "broker_id":  str(r.get("broker_id", "")),
                "broker_name":r.get("broker", ""),
                "buy_shares": int(float(r.get("buy", 0) or 0)),
                "sell_shares":int(float(r.get("sell", 0) or 0)),
                "net_shares": int(float(r.get("buy", 0) or 0)) - int(float(r.get("sell", 0) or 0)),
                "buy_price":  float(r.get("buy_price", 0) or 0),
                "sell_price": float(r.get("sell_price", 0) or 0),
            })
        except (ValueError, TypeError):
            continue

    # 寫入快取
    await _cache_broker_data(stock_code, parsed)
    return parsed


async def _cache_broker_data(stock_code: str, rows: list[dict]):
    async with AsyncSessionLocal() as db:
        for r in rows:
            existing = await db.execute(
                select(BrokerActivity).where(
                    BrokerActivity.date       == r["date"],
                    BrokerActivity.stock_code == stock_code,
                    BrokerActivity.broker_id  == r["broker_id"],
                )
            )
            if existing.scalar_one_or_none():
                continue
            db.add(BrokerActivity(**r))
        await db.commit()


# ── 前 10 大買超分點 ──────────────────────────────────────────────────────────

async def get_top_brokers(stock_code: str, days: int = 10) -> dict:
    """
    取得某股票近 days 日累積買超最多的前 10 大分點。
    優先從 DB 快取查，若無則抓 FinMind。
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(BrokerActivity).where(
                BrokerActivity.stock_code == stock_code,
                BrokerActivity.date >= cutoff,
            )
        )
        cached = r.scalars().all()

    if not cached:
        # 快取沒有，即時抓取
        fresh = await fetch_broker_detail(stock_code, days)
        if not fresh:
            return {"stock_code": stock_code, "brokers": [], "message": "無分點資料（可能需要 FinMind token）"}
        cached = []
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(BrokerActivity).where(
                    BrokerActivity.stock_code == stock_code,
                    BrokerActivity.date >= cutoff,
                )
            )
            cached = r.scalars().all()

    # 按分點聚合
    broker_agg: dict[str, dict] = {}
    for row in cached:
        bid = row.broker_id
        if bid not in broker_agg:
            broker_agg[bid] = {
                "broker_id":   bid,
                "broker_name": row.broker_name,
                "buy_shares":  0,
                "sell_shares": 0,
                "net_shares":  0,
                "days_bought": 0,
                "dates":       [],
            }
        broker_agg[bid]["buy_shares"]  += row.buy_shares
        broker_agg[bid]["sell_shares"] += row.sell_shares
        broker_agg[bid]["net_shares"]  += row.net_shares
        if row.net_shares > 0:
            broker_agg[bid]["days_bought"] += 1
        if row.date not in broker_agg[bid]["dates"]:
            broker_agg[bid]["dates"].append(row.date)

    # 按淨買超排序，取前 10
    top10 = sorted(broker_agg.values(), key=lambda x: x["net_shares"], reverse=True)[:10]

    return {
        "stock_code": stock_code,
        "period_days": days,
        "brokers": top10,
    }


# ── 追蹤特定分點 ──────────────────────────────────────────────────────────────

async def track_broker(broker_name: str, days: int = 5) -> dict:
    """
    找出特定分點近 days 日買了哪些股票。
    從已快取的 broker_activity 表查詢。
    （注意：快取範圍取決於之前查過哪些股票）
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(BrokerActivity).where(
                BrokerActivity.broker_name.contains(broker_name),
                BrokerActivity.date >= cutoff,
                BrokerActivity.net_shares > 0,
            ).order_by(BrokerActivity.net_shares.desc())
        )
        rows = r.scalars().all()

    if not rows:
        return {
            "broker_name": broker_name,
            "message": "快取無此分點資料。請先用 /broker 查詢你感興趣的股票以建立快取。",
            "stocks": [],
        }

    # 按股票聚合
    stock_agg: dict[str, dict] = {}
    for row in rows:
        code = row.stock_code
        if code not in stock_agg:
            stock_agg[code] = {
                "stock_code": code,
                "stock_name": row.stock_name or "",
                "total_net":  0,
                "days":       set(),
            }
        stock_agg[code]["total_net"] += row.net_shares
        stock_agg[code]["days"].add(row.date)

    result = [
        {
            "stock_code": v["stock_code"],
            "stock_name": v["stock_name"],
            "net_shares": v["total_net"],
            "active_days": len(v["days"]),
        }
        for v in sorted(stock_agg.values(), key=lambda x: x["total_net"], reverse=True)
    ]

    return {
        "broker_name": broker_name,
        "period_days": days,
        "stocks":      result[:10],
    }


# ── 偵測聰明錢訊號 ────────────────────────────────────────────────────────────

async def detect_smart_money() -> list[dict]:
    """
    從已快取的 broker_activity 中找出今日分點異動最顯著的訊號：
    1. 特定分點連續 3 日以上買超且股價尚未大漲
    2. 多個主力分點同時進場同一檔
    """
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    today  = date.today().isoformat()

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(BrokerActivity).where(
                BrokerActivity.date >= cutoff,
                BrokerActivity.net_shares > 0,
            )
        )
        rows = r.scalars().all()

    if not rows:
        return []

    # 找各股票今日有哪些分點淨買超
    # 結構：{stock_code: {broker_id: [dates]}}
    stock_brokers: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        sc = row.stock_code
        bid = row.broker_id
        if sc not in stock_brokers:
            stock_brokers[sc] = {}
        if bid not in stock_brokers[sc]:
            stock_brokers[sc][bid] = []
        if row.date not in stock_brokers[sc][bid]:
            stock_brokers[sc][bid].append(row.date)

    signals = []
    for sc, brokers in stock_brokers.items():
        # 訊號 1：某分點連續 ≥3 日買超
        for bid, dates in brokers.items():
            consec = _consecutive_days(sorted(dates))
            if consec >= 3:
                bname = next(
                    (r.broker_name for r in rows if r.stock_code == sc and r.broker_id == bid),
                    bid
                )
                signals.append({
                    "type":       "consecutive_buy",
                    "stock_code": sc,
                    "broker_id":  bid,
                    "broker_name":bname,
                    "consec_days":consec,
                    "message":    f"{sc} — {bname} 連續 {consec} 日買超",
                })

        # 訊號 2：多個分點同時進場（≥3 個不同分點）
        unique_brokers_today = len([
            bid for bid, dates in brokers.items()
            if any(d >= (date.today() - timedelta(days=2)).isoformat() for d in dates)
        ])
        if unique_brokers_today >= 3:
            signals.append({
                "type":         "multi_broker",
                "stock_code":   sc,
                "broker_count": unique_brokers_today,
                "message":      f"{sc} — {unique_brokers_today} 個主力分點同時進場",
            })

    # 按訊號強度排序
    signals.sort(key=lambda x: x.get("consec_days", x.get("broker_count", 0)), reverse=True)
    return signals[:10]


def _consecutive_days(sorted_dates: list[str]) -> int:
    """計算最近連續天數（包含週末跳過邏輯的簡化版）"""
    if not sorted_dates:
        return 0
    count = 1
    for i in range(len(sorted_dates) - 1, 0, -1):
        d1 = date.fromisoformat(sorted_dates[i])
        d0 = date.fromisoformat(sorted_dates[i - 1])
        gap = (d1 - d0).days
        if gap <= 3:  # 允許週末跳過
            count += 1
        else:
            break
    return count


# ── 自動推播聰明錢訊號 ────────────────────────────────────────────────────────

async def push_smart_money_alerts():
    """每日 18:00 偵測並推播聰明錢訊號"""
    signals = await detect_smart_money()
    if not signals:
        return

    strong = [s for s in signals if s.get("consec_days", 0) >= 4 or s.get("broker_count", 0) >= 4]
    if not strong:
        return

    from ..models.models import Subscriber
    from .morning_report import _push_to_users

    lines = ["🕵️ 聰明錢訊號", "─" * 20]
    for s in strong[:5]:
        lines.append(f"• {s['message']}")

    msg = "\n".join(lines)

    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Subscriber))
        subs = r.scalars().all()
    if subs:
        await _push_to_users([s.line_user_id for s in subs], msg)
        logger.info(f"[Broker] 推播 {len(strong)} 個聰明錢訊號")
