"""籌碼分點追蹤服務

來源：FinMind BrokerTradingDetail
  URL: https://api.finmindtrade.com/api/v4/data?dataset=BrokerTradingDetail&data_id=XXXX&start_date=YYYY-MM-DD

功能：
  1. get_broker_summary(stock_code)   - 分點買超/賣超排行 + 股名
  2. get_top_brokers(stock_code)      - 向下相容舊接口
  3. track_broker(broker_name)        - 某分點最近買了哪些股
  4. detect_smart_money()             - 今日主力分點異動最大的股票
  5. fetch_bulk_broker_data()         - 批次更新自選股分點資料
  6. push_consecutive_buy_alerts()    - 連買3日警示推播（含 push_dedup）
"""
import asyncio
from datetime import date, timedelta, datetime
from loguru import logger
from sqlalchemy import select, func

from ..models.database import AsyncSessionLocal
from ..models.models import BrokerActivity
from .finmind_service import _get


async def _lookup_stock_name(stock_code: str) -> str:
    """從 TWSE cache 查股票名稱，失敗回傳空字串"""
    try:
        from .twse_service import fetch_realtime_quote
        q = await fetch_realtime_quote(stock_code)
        return q.get("name", "") or ""
    except Exception:
        return ""


# ── 資料抓取與快取 ────────────────────────────────────────────────────────────

async def fetch_broker_detail(stock_code: str, days: int = 10) -> list[dict]:
    """從 FinMind 抓取分點交易明細並快取到 DB"""
    start_date = (date.today() - timedelta(days=days + 5)).strftime("%Y-%m-%d")
    raw = await _get("BrokerTradingDetail", stock_code, start_date)

    parsed = []
    for r in raw:
        try:
            parsed.append({
                "date":        r.get("date", ""),
                "stock_code":  stock_code,
                "broker_id":   str(r.get("broker_id", "")),
                "broker_name": r.get("broker", ""),
                "buy_shares":  int(float(r.get("buy", 0) or 0)),
                "sell_shares": int(float(r.get("sell", 0) or 0)),
                "net_shares":  int(float(r.get("buy", 0) or 0)) - int(float(r.get("sell", 0) or 0)),
                "buy_price":   float(r.get("buy_price", 0) or 0),
                "sell_price":  float(r.get("sell_price", 0) or 0),
            })
        except (ValueError, TypeError):
            continue

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


async def fetch_bulk_broker_data(stock_codes: list[str], days: int = 10) -> int:
    """批次抓取多支股票分點資料（含 rate limit，回傳成功筆數）"""
    ok = 0
    for code in stock_codes:
        try:
            rows = await fetch_broker_detail(code, days)
            if rows:
                ok += 1
            await asyncio.sleep(2.5)   # FinMind 免費版 rate limit
        except Exception as e:
            logger.warning(f"[Broker] fetch {code} failed: {e}")
    logger.info(f"[Broker] bulk fetch done: {ok}/{len(stock_codes)} stocks")
    return ok


# ── 主要查詢接口 ──────────────────────────────────────────────────────────────

def _aggregate_brokers(rows) -> dict[str, dict]:
    """將 BrokerActivity rows 聚合為 broker_id → stats dict"""
    agg: dict[str, dict] = {}
    for row in rows:
        bid = row.broker_id
        if bid not in agg:
            agg[bid] = {
                "broker_id":   bid,
                "broker_name": row.broker_name,
                "buy_shares":  0,
                "sell_shares": 0,
                "net_shares":  0,
                "days_bought": 0,
                "days_sold":   0,
                "dates":       [],
            }
        agg[bid]["buy_shares"]  += row.buy_shares
        agg[bid]["sell_shares"] += row.sell_shares
        agg[bid]["net_shares"]  += row.net_shares
        if row.net_shares > 0:
            agg[bid]["days_bought"] += 1
        elif row.net_shares < 0:
            agg[bid]["days_sold"] += 1
        if row.date not in agg[bid]["dates"]:
            agg[bid]["dates"].append(row.date)
    return agg


async def get_broker_summary(stock_code: str, days: int = 10) -> dict:
    """
    取得某股票近 days 日分點彙整，含：
    - stock_name
    - top_buyers:  買超前5（net > 0，排序 desc）
    - top_sellers: 賣超前5（net < 0，排序 asc）
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
        fresh = await fetch_broker_detail(stock_code, days)
        if not fresh:
            return {"stock_code": stock_code, "stock_name": "", "top_buyers": [], "top_sellers": [], "no_data": True}
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(BrokerActivity).where(
                    BrokerActivity.stock_code == stock_code,
                    BrokerActivity.date >= cutoff,
                )
            )
            cached = r.scalars().all()

    agg = _aggregate_brokers(cached)
    all_brokers = sorted(agg.values(), key=lambda x: x["net_shares"], reverse=True)

    top_buyers  = [b for b in all_brokers if b["net_shares"] > 0][:5]
    top_sellers = [b for b in reversed(all_brokers) if b["net_shares"] < 0][:5]

    stock_name = await _lookup_stock_name(stock_code)

    return {
        "stock_code":  stock_code,
        "stock_name":  stock_name,
        "period_days": days,
        "top_buyers":  top_buyers,
        "top_sellers": top_sellers,
    }


async def get_top_brokers(stock_code: str, days: int = 10) -> dict:
    """向下相容舊接口"""
    summary = await get_broker_summary(stock_code, days)
    all_b = summary["top_buyers"] + summary["top_sellers"]
    all_b.sort(key=lambda x: x["net_shares"], reverse=True)
    return {"stock_code": stock_code, "period_days": days, "brokers": all_b}


# ── 追蹤特定分點 ──────────────────────────────────────────────────────────────

async def track_broker(broker_name: str, days: int = 5) -> dict:
    """找出特定分點近 days 日買了哪些股票（從 DB 快取查）"""
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

    stock_agg: dict[str, dict] = {}
    for row in rows:
        code = row.stock_code
        if code not in stock_agg:
            stock_agg[code] = {"stock_code": code, "stock_name": row.stock_name or "", "total_net": 0, "days": set()}
        stock_agg[code]["total_net"] += row.net_shares
        stock_agg[code]["days"].add(row.date)

    result = [
        {"stock_code": v["stock_code"], "stock_name": v["stock_name"], "net_shares": v["total_net"], "active_days": len(v["days"])}
        for v in sorted(stock_agg.values(), key=lambda x: x["total_net"], reverse=True)
    ]
    return {"broker_name": broker_name, "period_days": days, "stocks": result[:10]}


# ── 偵測聰明錢訊號 ────────────────────────────────────────────────────────────

async def detect_smart_money() -> list[dict]:
    """
    從已快取的 broker_activity 中找出最近分點異動最顯著的訊號：
    1. 特定分點連續 ≥3 日買超（含股名、累計買超張數）
    2. 多個主力分點同時進場同一檔（≥3 個不同分點）
    """
    cutoff = (date.today() - timedelta(days=10)).isoformat()

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

    # {stock_code: {broker_id: {dates: [], net_total: int, broker_name: str, stock_name: str}}}
    stock_data: dict[str, dict[str, dict]] = {}
    for row in rows:
        sc  = row.stock_code
        bid = row.broker_id
        if sc not in stock_data:
            stock_data[sc] = {}
        if bid not in stock_data[sc]:
            stock_data[sc][bid] = {
                "broker_name": row.broker_name,
                "stock_name":  row.stock_name or "",
                "dates":       [],
                "net_total":   0,
            }
        if row.date not in stock_data[sc][bid]["dates"]:
            stock_data[sc][bid]["dates"].append(row.date)
        stock_data[sc][bid]["net_total"] += row.net_shares

    signals: list[dict] = []
    recent_cutoff = (date.today() - timedelta(days=3)).isoformat()

    for sc, brokers in stock_data.items():
        # 訊號 1：連續買超 ≥3 日
        for bid, info in brokers.items():
            consec = _consecutive_days(sorted(info["dates"]))
            if consec >= 3:
                signals.append({
                    "type":        "consecutive_buy",
                    "stock_code":  sc,
                    "stock_name":  info["stock_name"],
                    "broker_name": info["broker_name"],
                    "consec_days": consec,
                    "net_total":   info["net_total"],
                    "signal_key":  f"{sc}_{bid}_consec",
                })

        # 訊號 2：多主力分點近3日進場
        active_brokers = [
            info for info in brokers.values()
            if any(d >= recent_cutoff for d in info["dates"])
        ]
        if len(active_brokers) >= 3:
            total_net = sum(b["net_total"] for b in active_brokers)
            sample_name = active_brokers[0].get("stock_name", "")
            signals.append({
                "type":          "multi_broker",
                "stock_code":    sc,
                "stock_name":    sample_name,
                "broker_count":  len(active_brokers),
                "net_total":     total_net,
                "signal_key":    f"{sc}_multi",
            })

    # 按訊號強度排序（連續天數 > 分點數）
    signals.sort(key=lambda x: (x.get("consec_days", 0), x.get("broker_count", 0)), reverse=True)
    return signals[:12]


def _consecutive_days(sorted_dates: list[str]) -> int:
    """計算最近連續天數（允許週末跳過最多3天）"""
    if not sorted_dates:
        return 0
    count = 1
    for i in range(len(sorted_dates) - 1, 0, -1):
        d1 = date.fromisoformat(sorted_dates[i])
        d0 = date.fromisoformat(sorted_dates[i - 1])
        if (d1 - d0).days <= 3:
            count += 1
        else:
            break
    return count


# ── 自動推播 ──────────────────────────────────────────────────────────────────

async def push_consecutive_buy_alerts():
    """
    偵測並推播連買3日以上的分點訊號（含 push_dedup，每個 signal_key 每天只推一次）
    """
    signals = await detect_smart_money()
    if not signals:
        return

    strong = [s for s in signals if s.get("consec_days", 0) >= 3 or s.get("broker_count", 0) >= 4]
    if not strong:
        return

    from ..models.models import Subscriber
    from .morning_report import _push_to_users
    from .push_dedup import check_and_record

    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
        subs = r.scalars().all()
    if not subs:
        return

    lines = ["🧠 聰明錢訊號", "─" * 20]
    for s in strong[:6]:
        sc   = s["stock_code"]
        name = s.get("stock_name", "")
        tag  = f"{sc} {name}".strip()
        if s["type"] == "consecutive_buy":
            net = s.get("net_total", 0)
            lines.append(f"📌 {tag}")
            lines.append(f"   {s['broker_name']} 連續{s['consec_days']}日買超 +{net:,}張")
        else:
            lines.append(f"⚡ {tag}")
            lines.append(f"   {s['broker_count']}個主力分點同步進場")

    msg = "\n".join(lines)
    signal_key = "|".join(s["signal_key"] for s in strong[:6])

    eligible = []
    for sub in subs:
        if await check_and_record(sub.line_user_id, "alert", msg + signal_key):
            eligible.append(sub.line_user_id)

    if eligible:
        await _push_to_users(eligible, msg)
        logger.info(f"[Broker] 推播 {len(strong)} 個聰明錢訊號 → {len(eligible)} 人")
    else:
        logger.info("[Broker] 聰明錢訊號已全部推送過，跳過")


async def push_smart_money_alerts():
    """向下相容：呼叫新函數"""
    await push_consecutive_buy_alerts()
