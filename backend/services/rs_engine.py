"""RS Engine — 相對強度排名（Relative Strength Ranking）

RS = 個股近20日報酬率 / 大盤近20日報酬率
RS Rank = 全市場百分位排名（越高越強）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from loguru import logger


@dataclass
class RSRecord:
    stock_id:   str
    name:       str
    sector:     str
    rs:         float       # Relative Strength ratio
    rs_rank:    float       # 0~100 百分位排名
    ret_20d:    float       # 個股近20日報酬率
    change_pct: float       # 今日漲跌


def calculate_rs_from_screener() -> list[RSRecord]:
    """
    利用現有 screener pool 資料計算 RS 排名。
    市場報酬用所有股票平均近20日報酬代替。
    """
    try:
        from .report_screener import momentum_screener, value_screener, chip_screener, all_screener
        rows = all_screener(200)
        if not rows:
            return []

        # 計算市場平均報酬（以所有股票 change_pct 的中位數代理）
        changes = [r.change_pct for r in rows if r.change_pct != 0]
        if not changes:
            return []
        import statistics
        market_median = statistics.median(changes)
        if market_median == 0:
            market_median = 0.01

        records: list[RSRecord] = []
        for r in rows:
            # RS = 個股報酬 / 市場報酬（以今日漲跌代理）
            rs_val = r.change_pct / max(abs(market_median), 0.01)
            records.append(RSRecord(
                stock_id   = r.stock_id,
                name       = r.name,
                sector     = r.sector,
                rs         = rs_val,
                rs_rank    = 0.0,   # 稍後計算
                ret_20d    = r.change_pct,
                change_pct = r.change_pct,
            ))

        # 計算百分位排名
        sorted_rs = sorted(records, key=lambda x: x.rs)
        total = len(sorted_rs)
        for i, rec in enumerate(sorted_rs):
            rec.rs_rank = round((i / max(total - 1, 1)) * 100, 1)

        # 按 RS 由高到低排序
        return sorted(records, key=lambda x: -x.rs)

    except Exception as e:
        logger.error(f"[rs_engine] calculate_rs failed: {e}")
        return []


def get_top20() -> list[RSRecord]:
    """取得 RS 排名前 20 強勢股"""
    records = calculate_rs_from_screener()
    return records[:20]


def format_rs_ranking(records: list[RSRecord], top_n: int = 20) -> str:
    """格式化 RS 排行榜文字"""
    from datetime import datetime
    today = datetime.now().strftime("%m/%d")

    if not records:
        return f"📊 RS 強勢排行 {today}\n\n暫無資料，請稍後再試"

    lines = [
        f"📊 相對強度排行 {today}",
        f"（RS = 個股強度 / 大盤強度）",
        "─" * 22,
    ]
    for i, r in enumerate(records[:top_n], 1):
        sign  = "▲" if r.change_pct >= 0 else "▼"
        arrow = "+" if r.change_pct >= 0 else ""
        lines.append(
            f"#{i:2d} {r.stock_id} {r.name[:4]}"
            f"  RS={r.rs:+.2f}  {sign}{arrow}{r.change_pct:.1f}%"
        )

    lines.append("─" * 22)
    lines.append("輸入 /ai [代碼] 查看個股分析")
    return "\n".join(lines)


def get_rs_qr(records: list[RSRecord]) -> dict:
    """RS 排行的 Quick Reply 按鈕（前4強）"""
    items = []
    for r in records[:4]:
        items.append({"type": "action", "action": {
            "type":        "postback",
            "label":       f"🔍{r.stock_id}",
            "data":        f"act=recommend_detail&code={r.stock_id}",
            "displayText": f"分析 {r.stock_id}",
        }})
    items.append({"type": "action", "action": {
        "type": "postback", "label": "🔄 重新整理",
        "data": "rs", "displayText": "重新整理 RS 排行",
    }})
    return {"items": items[:13]}
