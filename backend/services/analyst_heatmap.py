"""Analyst Heatmap — 分析師關注熱度圖生成與推送"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from loguru import logger

STATIC_DIR = Path(os.getenv("STATIC_DIR", "./static/reports"))
STATIC_DIR.mkdir(parents=True, exist_ok=True)


async def get_heatmap_data(top_n: int = 15) -> list[dict]:
    """取得熱度圖所需資料（近7日被提及股票 + Alpha系統一致性）"""
    from .analyst_consensus_engine import calculate_daily_consensus
    from .report_screener import all_screener

    consensus_list = await calculate_daily_consensus(days=7)
    screener_rows  = all_screener(200)
    alpha_codes    = {r.stock_id for r in screener_rows if r.confidence >= 65}

    rows = []
    for c in consensus_list[:top_n]:
        alpha_agree = c.stock_id in alpha_codes
        rows.append({
            "stock_id":        c.stock_id,
            "stock_name":      c.stock_name[:4],
            "total_mentions":  c.total_analysts,
            "consensus_score": c.consensus_score,
            "high_cred":       c.high_cred_count,
            "strength_icons":  c.strength_icons,
            "alpha_agree":     alpha_agree,
            "is_divergent":    c.is_divergent,
        })
    return rows


def generate_heatmap_image(rows: list[dict]) -> Path:
    """生成分析師熱度圖 PNG"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.patches import FancyBboxPatch

        if not rows:
            rows = [{"stock_id": "N/A", "stock_name": "無資料", "total_mentions": 0,
                     "consensus_score": 0, "high_cred": 0, "strength_icons": "─",
                     "alpha_agree": False, "is_divergent": False}]

        n_rows  = len(rows)
        fig_w   = 14.0
        row_h   = 0.55
        hdr_h   = 0.8
        fig_h   = hdr_h + n_rows * row_h + 0.4

        BG     = "#0A0F1E"
        SURF   = "#0F1629"
        BORDER = "#1E3A5F"
        WHITE  = "#E0F0FF"
        MUTED  = "#7090B0"
        RED    = "#FF4455"
        GREEN  = "#22DD88"
        ORANGE = "#FF9933"
        GREY   = "#445566"

        # 欄位定義 (key, header, width_ratio)
        COLS = [
            ("stock",     "股票",        2.0),
            ("mentions",  "提及次數",    1.0),
            ("sentiment", "平均情緒",    1.2),
            ("high_cred", "高可信分析師", 1.4),
            ("alpha",     "Alpha一致",   1.2),
        ]
        total_r  = sum(c[2] for c in COLS)
        xpos     = []
        xwid     = []
        x        = 0.0
        for _, _, r in COLS:
            w = r / total_r * fig_w
            xpos.append(x); xwid.append(w); x += w

        fig, ax = plt.subplots(figsize=(fig_w, fig_h))
        ax.set_xlim(0, fig_w)
        ax.set_ylim(0, fig_h)
        ax.axis("off")
        fig.patch.set_facecolor(BG)

        today = datetime.now().strftime("%Y-%m-%d")
        ax.add_patch(FancyBboxPatch((0, fig_h - hdr_h), fig_w, hdr_h,
                                    boxstyle="square,pad=0", lw=0, facecolor="#060B14"))
        ax.text(fig_w / 2, fig_h - hdr_h / 2,
                f"📺 分析師關注熱度圖  {today}  （近7日）",
                ha="center", va="center", fontsize=13, fontweight="bold", color=WHITE)

        # 欄標題
        y_col = fig_h - hdr_h
        for ci, (_, hdr, _) in enumerate(COLS):
            ax.add_patch(FancyBboxPatch((xpos[ci], y_col - 0.36), xwid[ci], 0.36,
                                        boxstyle="square,pad=0", lw=0.3,
                                        edgecolor=BORDER, facecolor="#131F35"))
            ax.text(xpos[ci] + xwid[ci] / 2, y_col - 0.18, hdr,
                    ha="center", va="center", fontsize=9.5, fontweight="bold", color="#8AADCE")
        y = y_col - 0.36

        for ri, row in enumerate(rows):
            ry     = y - ri * row_h
            bg     = SURF if ri % 2 == 0 else BG

            # 行背景色（依強度）
            if row["consensus_score"] >= 75 and row["alpha_agree"]:
                bg = "#1A0A08"   # 深紅（強力）
            elif row["consensus_score"] >= 55 and row["alpha_agree"]:
                bg = "#1A1008"   # 橙紅
            elif row["is_divergent"]:
                bg = "#101825"   # 藍灰（高分歧）
            ax.add_patch(FancyBboxPatch((0, ry - row_h), fig_w, row_h,
                                        boxstyle="square,pad=0", lw=0, facecolor=bg))

            for ci, (key, _, _) in enumerate(COLS):
                cx = xpos[ci]; cw = xwid[ci]
                cy = ry - row_h / 2
                ax.plot([cx, cx], [ry - row_h, ry], color=BORDER, lw=0.3)

                if key == "stock":
                    ax.text(cx + 0.15, cy + 0.08,
                            f"{row['stock_id']}  {row['stock_name']}",
                            ha="left", va="center", fontsize=9.5, fontweight="bold", color=WHITE)
                    if row["is_divergent"]:
                        ax.text(cx + 0.15, cy - 0.12, "高分歧",
                                ha="left", va="center", fontsize=7.5, color=ORANGE)

                elif key == "mentions":
                    ax.text(cx + cw / 2, cy,
                            str(row["total_mentions"]),
                            ha="center", va="center", fontsize=11, fontweight="bold", color=WHITE)

                elif key == "sentiment":
                    icons = row["strength_icons"]
                    ax.text(cx + cw / 2, cy, icons,
                            ha="center", va="center", fontsize=11)

                elif key == "high_cred":
                    n    = row["high_cred"]
                    clr  = RED if n >= 3 else (ORANGE if n >= 2 else MUTED)
                    ax.text(cx + cw / 2, cy,
                            f"{'⭐' * min(n, 5)}" if n > 0 else "─",
                            ha="center", va="center", fontsize=9, color=clr)

                elif key == "alpha":
                    agree = row["alpha_agree"]
                    ax.text(cx + cw / 2, cy,
                            "✅" if agree else "❌",
                            ha="center", va="center", fontsize=11)

            ax.plot([0, fig_w], [ry - row_h, ry - row_h], color=BORDER, lw=0.25)

        # 圖例
        y_foot = y - n_rows * row_h
        ax.text(0.2, y_foot - 0.18,
                "■ 深紅=高共識+Alpha一致  ■ 橙紅=中共識  ■ 灰=高分歧  ✅=Alpha系統確認",
                ha="left", va="center", fontsize=7, color=MUTED)

        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = STATIC_DIR / f"analyst_heatmap_{ts}.png"
        plt.tight_layout(pad=0)
        plt.savefig(path, dpi=130, bbox_inches="tight", facecolor=BG)
        plt.close(fig)
        logger.info(f"[analyst_heatmap] generated: {path}")
        return path

    except ImportError:
        raise RuntimeError("matplotlib 未安裝")


async def push_consensus_report():
    """每日 20:00 推送分析師共識報告給所有訂閱者"""
    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from sqlalchemy import select
    import httpx

    consensus_list = await calculate_daily_consensus_with_alpha()
    if not consensus_list:
        logger.info("[analyst_heatmap] no consensus data, skip push")
        return

    text = await _format_consensus_report(consensus_list)
    qr   = {"items": [
        {"type": "action", "action": {
            "type": "message", "label": "📺 共識報告", "text": "/consensus"}},
        {"type": "action", "action": {
            "type": "message", "label": "🏆 分析師排行", "text": "/analyst ranking"}},
        {"type": "action", "action": {
            "type": "postback", "label": "📊 今日選股",
            "data": "act=screener_qr", "displayText": "今日選股"}},
    ]}

    async with AsyncSessionLocal() as db:
        r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
        subs = r.scalars().all()

    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        for sub in subs:
            try:
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": sub.line_user_id, "messages": [
                        {"type": "text", "text": text, "quickReply": qr}
                    ]},
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"[analyst_heatmap] push failed: {e}")
    logger.info(f"[analyst_heatmap] pushed to {len(subs)} subscribers")


async def calculate_daily_consensus_with_alpha() -> list:
    """取得結合 Alpha 系統的共識列表"""
    from .analyst_consensus_engine import calculate_daily_consensus
    from .report_screener import all_screener

    consensus_list = await calculate_daily_consensus(days=7)
    screener_rows  = all_screener(200)
    alpha_codes    = {r.stock_id: r for r in screener_rows if r.confidence >= 65}

    for c in consensus_list:
        c.alpha_agree  = c.stock_id in alpha_codes
        row = alpha_codes.get(c.stock_id)
        c.alpha_score  = row.confidence if row else 0
        c.inst_flow    = (row.chip_5d or 0) > 0 if row else False
        c.rs_rank      = getattr(row, "day_rank", 99) if row else 99

    return consensus_list


async def _format_consensus_report(consensus_list: list) -> str:
    """格式化每日共識推送文字"""
    today = datetime.now().strftime("%m/%d")
    lines = [f"📺 今日分析師共識 TOP5  {today}", "─" * 22]

    top5   = [c for c in consensus_list if c.consensus_score >= 50][:5]
    diverg = [c for c in consensus_list if c.is_divergent][:2]

    for c in top5:
        lines.append("")
        lines.append(c.to_line_text())
        if hasattr(c, "inst_flow") and c.inst_flow:
            lines.append("外資確認：連買4日 ✅")
        if hasattr(c, "alpha_agree") and c.alpha_agree:
            lines.append("Alpha系統：買進訊號 ✅")
        # 三重確認
        if (c.consensus_score >= 80
                and getattr(c, "inst_flow", False)
                and getattr(c, "alpha_agree", False)):
            lines.append(f"→ 三重確認，信心指數 {min(int(c.consensus_score), 95)}")

    if diverg:
        lines.append("")
        lines.append("⚠️ 高分歧股（建議觀望）：")
        for d in diverg:
            lines.append(f"  {d.stock_id} {d.stock_name}  看多{d.bullish_count}位 vs 看空{d.bearish_count}位")

    return "\n".join(lines)
