"""Price Alert Zone Service — 自選股支撐/壓力預警（每天最多推一次）"""
from __future__ import annotations

import json
import os
import time
from datetime import date
from loguru import logger

_ZONE_FILE = os.path.join(os.path.dirname(__file__), "../../data/price_zones.json")
_ALERT_LOG_FILE = os.path.join(os.path.dirname(__file__), "../../data/alert_log.json")

THRESHOLD_PCT = 0.03  # 3%


def _load_json(path: str, default) -> dict | list:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def set_price_zone(uid: str, code: str, support: float, resistance: float) -> dict:
    """設定股價警戒區間"""
    zones: dict = _load_json(_ZONE_FILE, {})
    if uid not in zones:
        zones[uid] = {}
    zones[uid][code] = {
        "support":    support,
        "resistance": resistance,
        "set_at":     time.strftime("%Y-%m-%d"),
    }
    _save_json(_ZONE_FILE, zones)
    return zones[uid][code]


def get_user_zones(uid: str) -> dict[str, dict]:
    zones: dict = _load_json(_ZONE_FILE, {})
    return zones.get(uid, {})


def remove_zone(uid: str, code: str) -> bool:
    zones: dict = _load_json(_ZONE_FILE, {})
    if uid in zones and code in zones[uid]:
        del zones[uid][code]
        _save_json(_ZONE_FILE, zones)
        return True
    return False


def _was_alerted_today(uid: str, code: str, kind: str) -> bool:
    log: dict = _load_json(_ALERT_LOG_FILE, {})
    today = date.today().isoformat()
    key = f"{uid}:{code}:{kind}"
    return log.get(key) == today


def _mark_alerted(uid: str, code: str, kind: str) -> None:
    log: dict = _load_json(_ALERT_LOG_FILE, {})
    today = date.today().isoformat()
    # 清除舊日記錄
    log = {k: v for k, v in log.items() if v == today}
    log[f"{uid}:{code}:{kind}"] = today
    _save_json(_ALERT_LOG_FILE, log)


async def scan_and_alert() -> int:
    """掃描所有用戶的警戒區間，推播符合條件的預警，回傳推播數"""
    import asyncio
    from backend.services.twse_service import fetch_realtime_quote
    from backend.services.line_push import push_to_user

    zones: dict = _load_json(_ZONE_FILE, {})
    pushed = 0

    for uid, user_zones in zones.items():
        for code, zone in user_zones.items():
            support    = zone.get("support", 0)
            resistance = zone.get("resistance", 0)
            if support <= 0 and resistance <= 0:
                continue

            try:
                quote = await fetch_realtime_quote(code)
            except Exception as e:
                continue
            price = float(quote.get("close") or quote.get("price") or 0)
            if price <= 0:
                continue
            name = quote.get("name", code)

            # 支撐預警：現價在支撐上方 3% 內
            if support > 0 and price <= support * (1 + THRESHOLD_PCT):
                if not _was_alerted_today(uid, code, "support"):
                    pct = (price - support) / support * 100
                    msg = (
                        f"⚠️ 股價預警 — 接近支撐\n"
                        f"{code} {name}\n"
                        f"現價：{price:,.1f}\n"
                        f"支撐位：{support:,.1f}（距離 {pct:+.1f}%）\n\n"
                        f"💡 留意是否跌破支撐，可考慮設停損\n"
                        f"輸入 /cost {code} 查看主力成本分析"
                    )
                    try:
                        await push_to_user(uid, msg)
                        _mark_alerted(uid, code, "support")
                        pushed += 1
                    except Exception as e:
                        logger.error(f"[zone_alert] support push: {e}")

            # 壓力預警：現價在壓力下方 3% 內
            if resistance > 0 and price >= resistance * (1 - THRESHOLD_PCT):
                if not _was_alerted_today(uid, code, "resistance"):
                    pct = (resistance - price) / resistance * 100
                    msg = (
                        f"🚨 股價預警 — 接近壓力\n"
                        f"{code} {name}\n"
                        f"現價：{price:,.1f}\n"
                        f"壓力位：{resistance:,.1f}（距離 {pct:.1f}%）\n\n"
                        f"💡 留意能否突破壓力，突破後可加碼\n"
                        f"輸入 /rating {code} 查看最新評級"
                    )
                    try:
                        await push_to_user(uid, msg)
                        _mark_alerted(uid, code, "resistance")
                        pushed += 1
                    except Exception as e:
                        logger.error(f"[zone_alert] resistance push: {e}")

    return pushed


def format_zones_report(uid: str) -> str:
    zones = get_user_zones(uid)
    if not zones:
        return (
            "📋 尚未設定任何警戒區間\n\n"
            "格式：/watch add 代碼 支撐價 壓力價\n"
            "例：/watch add 2330 800 1000\n\n"
            "系統在股價接近支撐/壓力時（3% 範圍內）推播提醒，每天最多一次"
        )
    lines = [
        "📋 股價警戒區間設定",
        "─" * 28,
        "",
    ]
    for code, z in zones.items():
        lines += [
            f"📌 {code}",
            f"  支撐：{z.get('support', 0):,.0f}",
            f"  壓力：{z.get('resistance', 0):,.0f}",
            f"  設定：{z.get('set_at', '')}",
            "",
        ]
    lines += [
        "─" * 28,
        "移除：/watch del 代碼",
        "觸發距離：±3%",
        "每日最多推播一次",
    ]
    return "\n".join(lines)
