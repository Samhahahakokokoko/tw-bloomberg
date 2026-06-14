"""Conference Service — 法說會日程追蹤與提醒"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from loguru import logger
import asyncio
import re

_cache: list | None = None
_cache_ts: float = 0.0
_TTL = 3600  # 1 hr


async def get_conferences(days_ahead: int = 14) -> list[dict]:
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _filter_upcoming(_cache, days_ahead)

    confs = await _fetch_conferences()
    _cache = confs
    _cache_ts = now
    return _filter_upcoming(confs, days_ahead)


def _filter_upcoming(confs: list[dict], days_ahead: int) -> list[dict]:
    today = date.today()
    cutoff = today + timedelta(days=days_ahead)
    return [c for c in confs if today <= c.get("date", today) <= cutoff]


async def _fetch_conferences() -> list[dict]:
    """從多個來源抓取法說會日程"""
    import asyncio
    results = await asyncio.gather(
        _fetch_twse_conf(),
        _fetch_mops_conf(),
        return_exceptions=True,
    )
    merged: list[dict] = []
    seen: set[str] = set()
    for r in results:
        if isinstance(r, list):
            for item in r:
                key = f"{item.get('code','')}_{item.get('date','')}"
                if key not in seen:
                    seen.add(key)
                    merged.append(item)
    merged.sort(key=lambda x: x.get("date", date.today()))
    return merged


async def _fetch_twse_conf() -> list[dict]:
    """從 TWSE 公開資訊觀測站抓取法說會"""
    import httpx
    try:
        today = date.today()
        year  = today.year - 1911  # 民國年
        url   = f"https://mops.twse.com.tw/mops/web/ajax_t100sb02_1"
        params = {"encodeURIComponent": "1", "step": "1",
                  "TYPEK": "sii", "year": str(year), "month": ""}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, data=params)
        return _parse_mops_html(r.text)
    except Exception as e:
        logger.debug(f"[conf] TWSE fetch failed: {e}")
        return []


async def _fetch_mops_conf() -> list[dict]:
    """備用：從 MOPS 抓取法說會"""
    import httpx
    try:
        today = date.today()
        year  = today.year - 1911
        month = today.month
        url   = "https://mops.twse.com.tw/mops/web/ajax_t100sb02"
        params = {"encodeURIComponent": "1", "step": "1",
                  "TYPEK": "all", "year": str(year), "month": str(month)}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, data=params)
        return _parse_mops_html(r.text)
    except Exception as e:
        logger.debug(f"[conf] MOPS fetch failed: {e}")
        return _fallback_sample_conferences()


def _parse_mops_html(html: str) -> list[dict]:
    """解析 MOPS 表格 HTML"""
    import re
    confs = []
    try:
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
            if len(cells) < 5:
                continue
            # 典型欄位: 代號, 公司, 日期, 時間, 地點
            code_match = re.match(r'\d{4,6}', cells[0])
            if not code_match:
                continue
            code = code_match.group()
            name = cells[1]
            date_str = cells[2]
            # 轉換民國年為西元
            date_match = re.match(r'(\d+)/(\d+)/(\d+)', date_str)
            if not date_match:
                continue
            y, m, d = int(date_match.group(1)) + 1911, int(date_match.group(2)), int(date_match.group(3))
            try:
                conf_date = date(y, m, d)
            except ValueError:
                continue
            location = cells[4] if len(cells) > 4 else ""
            confs.append({
                "code":     code,
                "name":     name,
                "date":     conf_date,
                "time":     cells[3] if len(cells) > 3 else "",
                "location": location,
            })
    except Exception as e:
        logger.debug(f"[conf] parse error: {e}")
    return confs


def _fallback_sample_conferences() -> list[dict]:
    """無法抓取時的示範資料"""
    today = date.today()
    samples = []
    offsets = [2, 4, 7, 9, 12]
    names = [
        ("2330", "台積電"), ("2454", "聯發科"), ("2317", "鴻海"),
        ("2412", "中華電"), ("2882", "國泰金"),
    ]
    for i, (code, name) in enumerate(names):
        d = today + timedelta(days=offsets[i % len(offsets)])
        samples.append({
            "code": code, "name": name, "date": d,
            "time": "14:00", "location": "台北國際會議中心",
        })
    return samples


def format_conference_list(confs: list[dict]) -> str:
    if not confs:
        return "📋 未來 2 週無法說會資料"

    today = date.today()
    lines = [
        "📢 未來 2 週法說會日程",
        "─" * 32,
        "",
    ]
    current_date = None
    for c in confs:
        conf_date = c.get("date", today)
        delta = (conf_date - today).days
        if conf_date != current_date:
            current_date = conf_date
            day_label = {0: "今天", 1: "明天", 2: "後天"}.get(delta, "")
            lines.append(f"\n📅 {conf_date.strftime('%m/%d')} ({_weekday(conf_date)}) {day_label}")
        lines.append(
            f"  [{c['code']}] {c['name']}"
            f"  {c.get('time', '--')}"
            f"  {c.get('location', '')[:10]}"
        )
    lines += ["", "─" * 32,
              "💡 法說會前 2 天自動提醒推播"]
    return "\n".join(lines)


def _weekday(d: date) -> str:
    return ["一", "二", "三", "四", "五", "六", "日"][d.weekday()]


async def check_and_push_reminders() -> int:
    """檢查法說會並推播提醒，回傳推播數量"""
    import os
    from .line_push import push_to_admin
    today = date.today()
    confs = await get_conferences(days_ahead=3)
    pushed = 0
    for c in confs:
        delta = (c["date"] - today).days
        if delta == 2:  # 2 天前提醒
            msg = (
                f"⏰ 法說會提醒\n"
                f"{c['code']} {c['name']}\n"
                f"📅 {c['date'].strftime('%m/%d')} {c.get('time','')}\n"
                f"📍 {c.get('location','')}\n\n"
                f"輸入 /ai {c['code']} 查看 AI 分析"
            )
            try:
                await push_to_admin(msg)
                pushed += 1
            except Exception as e:
                logger.error(f"[conf] push failed: {e}")
        elif delta == 0:  # 當天摘要
            msg = (
                f"📊 法說會今日召開\n"
                f"{c['code']} {c['name']}\n"
                f"🕐 {c.get('time','--')}\n"
                f"📍 {c.get('location','')}\n\n"
                f"關注重點：營收展望、毛利率、客戶結構\n"
                f"輸入 /ai {c['code']} 取得即時分析"
            )
            try:
                await push_to_admin(msg)
                pushed += 1
            except Exception as e:
                logger.error(f"[conf] push today failed: {e}")
    return pushed
