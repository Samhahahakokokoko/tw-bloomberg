"""
report_tracker.py — 選股歷史追蹤

記錄每次選股系統選中哪些股票，並追蹤後續績效。
使用獨立的 SQLite（./data/report_history.db）。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.getenv("REPORT_HISTORY_DB", "./data/report_history.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def _conn():
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS appearances (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id     TEXT    NOT NULL,
            stock_name   TEXT    NOT NULL DEFAULT '',
            screen_type  TEXT    NOT NULL,
            appear_date  TEXT    NOT NULL,
            model_score  REAL    DEFAULT 0,
            close_price  REAL    DEFAULT 0,
            change_pct   REAL    DEFAULT 0,
            rank_pos     INTEGER DEFAULT 99,
            created_at   TEXT    DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS ix_app_stock ON appearances(stock_id);
        CREATE INDEX IF NOT EXISTS ix_app_date  ON appearances(appear_date);

        CREATE TABLE IF NOT EXISTS price_followup (
            appearance_id INTEGER REFERENCES appearances(id),
            days_after    INTEGER NOT NULL,
            price         REAL,
            return_pct    REAL,
            recorded_at   TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (appearance_id, days_after)
        );
        """)


# ── 寫入 ─────────────────────────────────────────────────────────────────────

def record_appearance(
    stock_id:    str,
    stock_name:  str,
    screen_type: str,
    model_score: float = 0.0,
    close_price: float = 0.0,
    change_pct:  float = 0.0,
    rank_pos:    int   = 99,
    appear_date: Optional[date] = None,
) -> int:
    """記錄一次被選中事件，回傳 appearance_id"""
    if appear_date is None:
        appear_date = date.today()
    init_db()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO appearances
               (stock_id, stock_name, screen_type, appear_date, model_score, close_price, change_pct, rank_pos)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (stock_id, stock_name, screen_type, str(appear_date),
             model_score, close_price, change_pct, rank_pos),
        )
        return cur.lastrowid


def batch_record(rows, screen_type: str, appear_date: Optional[date] = None) -> None:
    """批次記錄一組選股結果"""
    for i, row in enumerate(rows, 1):
        record_appearance(
            stock_id=row.stock_id,
            stock_name=row.name,
            screen_type=screen_type,
            model_score=row.model_score,
            close_price=row.close,
            change_pct=row.change_pct,
            rank_pos=i,
            appear_date=appear_date,
        )


# ── 查詢 ─────────────────────────────────────────────────────────────────────

def get_stock_history(stock_id: str, days: int = 30) -> dict:
    """
    查詢某檔股票過去 N 天的被選中紀錄。

    回傳 dict：
      total_appearances: int
      screen_types: {"momentum": 5, "chip": 3, ...}
      timeline: [{"date": ..., "screen_type": ..., "model_score": ..., "change_pct": ...}]
      avg_score: float
      best_score: float
    """
    init_db()
    since = (date.today() - timedelta(days=days)).isoformat()
    with _conn() as con:
        rows = con.execute(
            """SELECT stock_id, stock_name, screen_type, appear_date,
                      model_score, close_price, change_pct, rank_pos
               FROM appearances
               WHERE stock_id = ? AND appear_date >= ?
               ORDER BY appear_date DESC""",
            (stock_id, since),
        ).fetchall()

    if not rows:
        return {
            "stock_id": stock_id,
            "days":     days,
            "total_appearances": 0,
            "screen_types": {},
            "timeline": [],
            "avg_score": 0.0,
            "best_score": 0.0,
            "avg_change": 0.0,
        }

    screen_types: dict[str, int] = {}
    timeline: list[dict] = []
    scores: list[float] = []
    changes: list[float] = []

    for r in rows:
        st = r["screen_type"]
        screen_types[st] = screen_types.get(st, 0) + 1
        timeline.append({
            "date":        r["appear_date"],
            "screen_type": st,
            "model_score": r["model_score"],
            "change_pct":  r["change_pct"],
            "rank":        r["rank_pos"],
        })
        scores.append(r["model_score"])
        changes.append(r["change_pct"])

    return {
        "stock_id":          stock_id,
        "stock_name":        rows[0]["stock_name"] if rows else stock_id,
        "days":              days,
        "total_appearances": len(rows),
        "screen_types":      screen_types,
        "timeline":          timeline[:10],
        "avg_score":         round(sum(scores) / len(scores), 1) if scores else 0.0,
        "best_score":        round(max(scores), 1) if scores else 0.0,
        "avg_change":        round(sum(changes) / len(changes), 2) if changes else 0.0,
    }


def format_history_text(history: dict) -> str:
    """格式化 get_stock_history() 結果為 LINE 文字"""
    sid  = history["stock_id"]
    name = history.get("stock_name", sid)
    n    = history["total_appearances"]
    days = history["days"]

    if n == 0:
        return (
            f"📊 {sid} {name} 過去{days}天追蹤\n\n"
            f"未曾出現在任何選股結果中\n\n"
            "系統每日 08:30 / 19:30 自動更新"
        )

    lines = [
        f"📊 {sid} {name} 近{days}天被選中 {n} 次",
        "─" * 22,
    ]

    # 出現類型統計
    if history["screen_types"]:
        lines.append("出現在：")
        type_labels = {
            "momentum": "動能", "value": "存股", "chip": "籌碼",
            "breakout": "突破", "ai": "AI族群", "all": "全維度",
        }
        for st, cnt in sorted(history["screen_types"].items(), key=lambda x: -x[1]):
            label = type_labels.get(st, st)
            lines.append(f"  {label}: {cnt} 次")

    lines.append(f"平均分數: {history['avg_score']:.1f}  最高分: {history['best_score']:.1f}")
    lines.append(f"平均漲跌: {history['avg_change']:+.2f}%")

    # 最近 5 次
    if history["timeline"]:
        lines.append("\n最近 5 次出現：")
        type_labels = {
            "momentum": "動能", "value": "存股", "chip": "籌碼",
            "breakout": "突破", "ai": "AI族群", "all": "全維度",
        }
        for r in history["timeline"][:5]:
            label = type_labels.get(r["screen_type"], r["screen_type"])
            lines.append(
                f"  {r['date']}  [{label}]  "
                f"分={r['model_score']:.0f}  "
                f"漲跌={r['change_pct']:+.1f}%  "
                f"排#{r['rank']}"
            )

    return "\n".join(lines)
