"""
stock_favorites.py — 用戶自選股收藏管理

使用 JSON 檔案（per-user）儲存，路徑：./data/favorites/{uid}.json
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

FAV_DIR = Path(os.getenv("FAV_DIR", "./data/favorites"))
FAV_DIR.mkdir(parents=True, exist_ok=True)

MAX_FAVORITES = 30


def _path(uid: str) -> Path:
    safe = uid.replace("/", "_").replace("\\", "_")
    return FAV_DIR / f"{safe}.json"


def _load(uid: str) -> dict:
    p = _path(uid)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"stocks": []}


def _save(uid: str, data: dict) -> None:
    _path(uid).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_favorite(uid: str, stock_id: str, name: str = "") -> tuple[bool, str]:
    """
    新增一檔到收藏。
    回傳 (成功, 訊息)。
    """
    data = _load(uid)
    stocks = data["stocks"]
    existing = next((s for s in stocks if s["stock_id"] == stock_id), None)
    if existing:
        return False, f"{stock_id} {existing.get('name', '')} 已在收藏中"
    if len(stocks) >= MAX_FAVORITES:
        return False, f"收藏上限 {MAX_FAVORITES} 檔，請先移除部分"
    stocks.append({
        "stock_id": stock_id,
        "name":     name or stock_id,
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    data["stocks"] = stocks
    _save(uid, data)
    return True, f"✅ {stock_id} {name} 已加入收藏"


def remove_favorite(uid: str, stock_id: str) -> tuple[bool, str]:
    """移除收藏。回傳 (成功, 訊息)。"""
    data  = _load(uid)
    before = len(data["stocks"])
    data["stocks"] = [s for s in data["stocks"] if s["stock_id"] != stock_id]
    if len(data["stocks"]) == before:
        return False, f"{stock_id} 不在收藏中"
    _save(uid, data)
    return True, f"🗑️ {stock_id} 已從收藏移除"


def get_favorites(uid: str) -> list[dict]:
    """回傳收藏列表 [{"stock_id": ..., "name": ..., "added_at": ...}]"""
    return _load(uid)["stocks"]


def get_favorite_ids(uid: str) -> list[str]:
    return [s["stock_id"] for s in get_favorites(uid)]


def clear_favorites(uid: str) -> None:
    _save(uid, {"stocks": []})


def format_favorites_text(uid: str) -> str:
    favs = get_favorites(uid)
    if not favs:
        return "📭 收藏為空\n\n用 /save 代碼 加入收藏"
    lines = [f"⭐ 我的最愛（{len(favs)} 檔）", "─" * 20]
    for i, s in enumerate(favs, 1):
        lines.append(f"{i:2d}. {s['stock_id']}  {s.get('name', '')}  ({s.get('added_at', '')})")
    lines.append("\n/myfav report → 產生收藏選股圖\n/unsave 代碼  → 移除收藏")
    return "\n".join(lines)
