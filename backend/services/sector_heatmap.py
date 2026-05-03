"""Sector Heatmap — 族群熱力圖圖片生成"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from loguru import logger

STATIC_DIR = Path(os.getenv("STATIC_DIR", "./static/reports"))
STATIC_DIR.mkdir(parents=True, exist_ok=True)

SECTOR_GRID = [
    ["半導體", "IC設計", "晶圓代工", "封測"],
    ["AI Server", "散熱/電源", "PCB", "連接器"],
    ["電動車", "電池", "充電樁", "車用IC"],
    ["金融", "航運", "傳產", "鋼鐵"],
    ["生技", "電商", "光學", "通訊"],
]

# 各族群的典型代表股（用來抓漲跌資料）
SECTOR_REPR: dict[str, list[str]] = {
    "半導體":    ["2330", "2303"],
    "IC設計":    ["2454", "3034"],
    "晶圓代工":  ["2330", "5347"],
    "封測":      ["2408", "3711"],
    "AI Server": ["3231", "6669"],
    "散熱/電源": ["3443", "1590"],
    "PCB":       ["2382", "3037"],
    "連接器":    ["2492", "3045"],
    "電動車":    ["1590", "6223"],
    "電池":      ["1513", "5285"],
    "充電樁":    ["8277", "6153"],
    "車用IC":    ["4927", "8081"],
    "金融":      ["2882", "2886"],
    "航運":      ["2603", "2609"],
    "傳產":      ["1301", "1326"],
    "鋼鐵":      ["2002", "2006"],
    "生技":      ["4743", "6547"],
    "電商":      ["3711", "8024"],
    "光學":      ["3008", "6669"],
    "通訊":      ["2412", "4904"],
}


async def fetch_sector_changes() -> dict[str, float]:
    """抓各族群今日平均漲跌幅"""
    sector_chg: dict[str, float] = {}
    try:
        from .twse_service import fetch_realtime_quote
        for sector, codes in SECTOR_REPR.items():
            changes = []
            for code in codes[:2]:
                try:
                    q   = await fetch_realtime_quote(code)
                    chg = q.get("change_pct", 0) if q else 0
                    if chg is not None:
                        changes.append(float(chg))
                except Exception:
                    pass
            sector_chg[sector] = sum(changes) / len(changes) if changes else 0.0
    except Exception as e:
        logger.warning(f"[heatmap] fetch_sector_changes failed: {e}")
    return sector_chg


def _change_to_color(chg: float) -> tuple[str, str]:
    """漲跌 → 背景色 + 文字色"""
    if chg >= 3.0:
        return "#CC0022", "#FFFFFF"   # 深紅
    elif chg >= 1.0:
        return "#FF4455", "#FFFFFF"   # 淺紅
    elif chg <= -3.0:
        return "#006633", "#FFFFFF"   # 深綠
    elif chg <= -1.0:
        return "#22CC66", "#FFFFFF"   # 淺綠
    else:
        return "#1A2A40", "#AACCEE"   # 中性深藍


def generate_heatmap_image(sector_changes: dict[str, float]) -> Path:
    """用 matplotlib 生成族群熱力圖"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch

        rows = len(SECTOR_GRID)
        cols = len(SECTOR_GRID[0])
        cell_w, cell_h = 3.2, 1.4
        fig_w = cell_w * cols + 0.4
        fig_h = cell_h * rows + 1.0

        fig, ax = plt.subplots(figsize=(fig_w, fig_h))
        ax.set_xlim(0, fig_w)
        ax.set_ylim(0, fig_h)
        ax.axis("off")
        fig.patch.set_facecolor("#0A0F1E")

        # 標題
        today = datetime.now().strftime("%Y-%m-%d")
        ax.text(fig_w / 2, fig_h - 0.4, f"族群熱力圖  {today}",
                ha="center", va="center", fontsize=13, fontweight="bold",
                color="#E0F0FF")

        # 圖例說明
        legend = "■ 深紅>+3%  ■ 淺紅+1%  ■ 中性  ■ 淺綠-1%  ■ 深綠<-3%"
        ax.text(fig_w / 2, 0.25, legend,
                ha="center", va="center", fontsize=7, color="#7090B0")

        for ri, row in enumerate(SECTOR_GRID):
            for ci, sector in enumerate(row):
                x = 0.2 + ci * cell_w
                y = fig_h - 1.0 - ri * cell_h - cell_h + 0.1
                chg = sector_changes.get(sector, 0.0)
                bg_clr, txt_clr = _change_to_color(chg)

                ax.add_patch(FancyBboxPatch(
                    (x, y), cell_w - 0.15, cell_h - 0.15,
                    boxstyle="round,pad=0.04", lw=0,
                    facecolor=bg_clr,
                ))
                ax.text(x + (cell_w - 0.15) / 2, y + (cell_h - 0.15) * 0.65,
                        sector, ha="center", va="center",
                        fontsize=9, fontweight="bold", color=txt_clr)

                sign  = "+" if chg >= 0 else ""
                ax.text(x + (cell_w - 0.15) / 2, y + (cell_h - 0.15) * 0.28,
                        f"{sign}{chg:.1f}%", ha="center", va="center",
                        fontsize=8, color=txt_clr)

        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = STATIC_DIR / f"heatmap_{ts}.png"
        plt.tight_layout(pad=0)
        plt.savefig(path, dpi=130, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        logger.info(f"[heatmap] generated: {path}")
        return path

    except ImportError:
        raise RuntimeError("matplotlib 未安裝")


async def push_heatmap():
    """生成熱力圖 → 推送給所有訂閱者"""
    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from sqlalchemy import select
    import httpx

    async with AsyncSessionLocal() as db:
        r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
        subs = r.scalars().all()

    if not subs:
        return

    sector_changes = await fetch_sector_changes()
    path           = generate_heatmap_image(sector_changes)
    base_url       = os.getenv("BASE_URL", "")

    if not base_url:
        logger.warning("[heatmap] BASE_URL not set, skipping push")
        return

    image_url = f"{base_url.rstrip('/')}/static/reports/{path.name}"
    headers   = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    msgs = [
        {"type": "image",
         "originalContentUrl": image_url,
         "previewImageUrl":    image_url},
        {"type": "text", "text": "🌡️ 族群熱力圖\n深紅=強勢  深綠=弱勢\n輸入 /heatmap 隨時查看",
         "quickReply": {"items": [
             {"type": "action", "action": {
                 "type": "postback", "label": "📊 今日選股",
                 "data": "act=screener_qr", "displayText": "今日選股"}},
             {"type": "action", "action": {
                 "type": "postback", "label": "📈 大盤行情",
                 "data": "act=market_card", "displayText": "大盤行情"}},
         ]}},
    ]

    async with httpx.AsyncClient(timeout=20) as c:
        for sub in subs:
            try:
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": sub.line_user_id, "messages": msgs},
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"[heatmap] push failed: {e}")

    logger.info(f"[heatmap] pushed to {len(subs)} subscribers")
