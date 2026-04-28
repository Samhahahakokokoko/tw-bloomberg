"""
generate_report_image.py — 族群連動選股表圖片產生器（v2）

升級特性：
  - 7 種選股類型（momentum/value/chip/breakout/ai/sector/all）
  - 分頁支援（每頁最多 20 檔）
  - 右上角市場狀態標示（多頭/空頭/盤整）
  - 底部大盤漲跌資訊列
  - 信心指數條（每檔股票視覺化）
  - 模型分數星級顯示（★★★★☆）
  - 比較圖：並排 + 雷達圖

使用方式：
    from backend.services.report_screener import run_screener, paginate
    rows = run_screener("momentum")
    page_rows, total_pages = paginate(rows, page=1)
    path = generate_report_image(page_rows, group="動能選股",
                                  page=1, total_pages=total_pages,
                                  market_state="bull")
    path = generate_comparison_image([row1, row2, row3])
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from backend.services.report_screener import StockRow

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.patches import FancyBboxPatch
    _MPL_OK = True
except ImportError:
    _MPL_OK = False

STATIC_DIR = Path(os.getenv("STATIC_DIR", "./static/reports"))
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# ── 色彩系統 ──────────────────────────────────────────────────────────────────

COLOR_UP        = "#E8001E"      # 漲，深紅
COLOR_UP_BG     = "#FFF0F0"
COLOR_DOWN      = "#008A5E"      # 跌，深綠
COLOR_DOWN_BG   = "#EEF9F4"
COLOR_NEUTRAL   = "#555555"
COLOR_HEADER    = "#0D1B2A"      # 深海軍藍表頭
COLOR_SUB_HDR   = "#1B2A3B"
COLOR_COL_HDR   = "#253545"
COLOR_MODEL_HI  = "#EBF2FF"      # 高分藍底
COLOR_MODEL_TXT = "#1A3A8F"
COLOR_CHIP_POS  = "#C00020"
COLOR_CHIP_NEG  = "#007A45"
COLOR_ROW_ODD   = "#F8F9FA"
COLOR_ROW_EVEN  = "#FFFFFF"
COLOR_BORDER    = "#D0D5DD"
COLOR_BAR_BG    = "#EEEEEE"

# 市場狀態 badge 顏色
REGIME_COLORS = {
    "bull":     ("#E8001E", "多頭"),
    "bear":     ("#008A5E", "空頭"),
    "sideways": ("#E67E00", "盤整"),
    "volatile": ("#8B00DD", "高波動"),
    "unknown":  ("#666666", "未知"),
}

# 欄位定義：(key, header, width_ratio, align)
COLUMNS = [
    ("label",       "代碼/名稱",     2.1, "left"),
    ("close",       "收盤",          0.75, "center"),
    ("change_pct",  "漲跌%",         0.75, "center"),
    ("volume_k",    "量(千)",        0.80, "center"),
    ("chip_5d",     "5日法人",       0.90, "center"),
    ("chip_20d",    "20日法人",      0.90, "center"),
    ("rev_yoy",     "YoY%",          0.75, "center"),
    ("eps_growth",  "EPS↑%",         0.75, "center"),
    ("confidence",  "信心條",        1.10, "center"),
    ("stars",       "評級",          0.90, "center"),
]

COL_GROUPS = [
    ("股票",   1),
    ("盤面",   3),
    ("籌碼",   2),
    ("基本面", 2),
    ("模型",   2),
]


# ── 輔助函式 ──────────────────────────────────────────────────────────────────

def _pct_txt(val: float) -> str:
    return COLOR_UP if val > 0 else (COLOR_DOWN if val < 0 else COLOR_NEUTRAL)

def _pct_bg(val: float) -> str:
    return COLOR_UP_BG if val > 0 else (COLOR_DOWN_BG if val < 0 else "#F5F5F5")

def _chip_txt(val: float) -> str:
    return COLOR_CHIP_POS if val > 0 else (COLOR_CHIP_NEG if val < 0 else COLOR_NEUTRAL)

def _stars(score: float) -> str:
    n = round(max(0, min(5, score / 20)))
    return "★" * n + "☆" * (5 - n)


# ── 主圖產生 ─────────────────────────────────────────────────────────────────

def generate_report_image(
    stocks: list,                        # list[StockRow]
    group:  str = "選股結果",
    target_date: Optional[date] = None,
    market_state: str = "unknown",
    market_index: float = 0.0,           # 大盤指數收盤
    market_change: float = 0.0,          # 大盤漲跌點
    market_change_pct: float = 0.0,      # 大盤漲跌%
    page: int = 1,
    total_pages: int = 1,
    output_path: Optional[Path] = None,
) -> Path:
    """產生選股表圖片，回傳 Path"""
    if not _MPL_OK:
        raise RuntimeError("matplotlib 未安裝，請執行 pip install matplotlib")

    if target_date is None:
        target_date = date.today()

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = STATIC_DIR / f"report_{ts}.png"

    n_rows = len(stocks)
    n_cols = len(COLUMNS)

    fig_w    = 14.0
    row_h    = 0.54
    hdr_h    = 0.88     # 表頭
    grp_h    = 0.32     # 欄群組
    col_h    = 0.40     # 欄標題
    footer_h = 0.36     # 底部大盤列
    fig_h    = hdr_h + grp_h + col_h + n_rows * row_h + footer_h + 0.10

    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.axis("off")
    fig.patch.set_facecolor("#FFFFFF")

    # ── x 座標計算 ───────────────────────────────────────────────────────────
    total_r = sum(c[2] for c in COLUMNS)
    xpos, xwid = [], []
    x = 0.0
    for _, _, r, _ in COLUMNS:
        w = r / total_r * fig_w
        xpos.append(x); xwid.append(w); x += w

    y = fig_h  # 從頂部往下繪製

    # ── 表頭 ─────────────────────────────────────────────────────────────────
    ax.add_patch(FancyBboxPatch((0, y - hdr_h), fig_w, hdr_h,
                                boxstyle="square,pad=0", linewidth=0,
                                facecolor=COLOR_HEADER, zorder=2))
    date_str = target_date.strftime("%Y-%m-%d")
    ax.text(fig_w / 2, y - hdr_h / 2,
            f"族群連動選股  ▏  {group}  ▏  {date_str}",
            ha="center", va="center", fontsize=12.5, fontweight="bold",
            color="white", zorder=3)

    # 右上角：市場狀態 badge
    reg_color, reg_label = REGIME_COLORS.get(market_state, REGIME_COLORS["unknown"])
    bw, bh = 1.1, 0.30
    ax.add_patch(FancyBboxPatch((fig_w - bw - 0.15, y - 0.15 - bh), bw, bh,
                                boxstyle="round,pad=0.04", linewidth=0,
                                facecolor=reg_color, zorder=4))
    ax.text(fig_w - 0.15 - bw / 2, y - 0.15 - bh / 2,
            reg_label, ha="center", va="center",
            fontsize=8, fontweight="bold", color="white", zorder=5)

    # 左上角：分頁資訊
    if total_pages > 1:
        ax.text(0.15, y - 0.18,
                f"第 {page} 頁 / 共 {total_pages} 頁   /report next 下一頁",
                ha="left", va="center", fontsize=7.5, color="#AAAAAA", zorder=3)

    y -= hdr_h

    # ── 欄群組標題 ───────────────────────────────────────────────────────────
    grp_bgs = ["#162535", "#1D2F3F", "#152535", "#1A2C3C", "#162030"]
    ci = 0
    for gi, (gname, span) in enumerate(COL_GROUPS):
        xs = xpos[ci]; xe = xpos[ci + span - 1] + xwid[ci + span - 1]
        ax.add_patch(FancyBboxPatch((xs, y - grp_h), xe - xs, grp_h,
                                    boxstyle="square,pad=0", linewidth=0,
                                    facecolor=grp_bgs[gi % len(grp_bgs)], zorder=2))
        ax.plot([xs, xs], [y - grp_h, y], color="#334466", linewidth=0.5, zorder=3)
        ax.text((xs + xe) / 2, y - grp_h / 2, gname,
                ha="center", va="center", fontsize=8, fontweight="bold",
                color="white", zorder=3)
        ci += span
    y -= grp_h

    # ── 欄標題 ───────────────────────────────────────────────────────────────
    for ci_, (key, hdr, _, _) in enumerate(COLUMNS):
        ax.add_patch(FancyBboxPatch((xpos[ci_], y - col_h), xwid[ci_], col_h,
                                    boxstyle="square,pad=0", linewidth=0.3,
                                    edgecolor=COLOR_BORDER, facecolor=COLOR_COL_HDR, zorder=2))
        ax.text(xpos[ci_] + xwid[ci_] / 2, y - col_h / 2, hdr,
                ha="center", va="center", fontsize=7.5, fontweight="bold",
                color="#D8E0E8", zorder=3)
    y -= col_h

    # ── 資料列 ───────────────────────────────────────────────────────────────
    for ri, row in enumerate(stocks):
        row_bg = COLOR_ROW_ODD if ri % 2 == 0 else COLOR_ROW_EVEN
        ry = y - ri * row_h
        ax.add_patch(FancyBboxPatch((0, ry - row_h), fig_w, row_h,
                                    boxstyle="square,pad=0", linewidth=0,
                                    facecolor=row_bg, zorder=1))

        for ci_, (key, _, _, align) in enumerate(COLUMNS):
            cx = xpos[ci_]; cw = xwid[ci_]
            cy = ry - row_h / 2
            ha = "left" if align == "left" else "center"
            ox = cx + 0.12 if align == "left" else cx + cw / 2

            # ── 股票欄 ──────────────────────────────────────────────────────
            if key == "label":
                ax.text(ox, cy + 0.09,
                        f"{row.stock_id}  {row.name}",
                        ha=ha, va="center", fontsize=8, fontweight="bold",
                        color="#1A1A2E", zorder=3)
                if row.tags:
                    ax.text(ox, cy - 0.12, "  ".join(row.tags),
                            ha=ha, va="center", fontsize=6, color="#666688", zorder=3)

            # ── 收盤 ─────────────────────────────────────────────────────────
            elif key == "close":
                ax.text(cx + cw / 2, cy, f"{row.close:,.1f}",
                        ha="center", va="center", fontsize=8, color="#1A1A1A", zorder=3)

            # ── 漲跌% ────────────────────────────────────────────────────────
            elif key == "change_pct":
                ax.add_patch(FancyBboxPatch(
                    (cx + 0.04, ry - row_h + 0.05), cw - 0.08, row_h - 0.10,
                    boxstyle="round,pad=0.02", linewidth=0,
                    facecolor=_pct_bg(row.change_pct), zorder=2))
                s = "+" if row.change_pct > 0 else ""
                ax.text(cx + cw / 2, cy, f"{s}{row.change_pct:.2f}%",
                        ha="center", va="center", fontsize=8, fontweight="bold",
                        color=_pct_txt(row.change_pct), zorder=3)

            # ── 量（千）──────────────────────────────────────────────────────
            elif key == "volume_k":
                ax.text(cx + cw / 2, cy, f"{row.volume / 1000:,.0f}",
                        ha="center", va="center", fontsize=7.5, color="#444444", zorder=3)

            # ── 籌碼 ─────────────────────────────────────────────────────────
            elif key in ("chip_5d", "chip_20d"):
                val = row.chip_5d if key == "chip_5d" else row.chip_20d
                ax.text(cx + cw / 2, cy,
                        f"{val/1000:+.1f}k",
                        ha="center", va="center", fontsize=7.5, fontweight="bold",
                        color=_chip_txt(val), zorder=3)

            # ── 基本面 ───────────────────────────────────────────────────────
            elif key in ("rev_yoy", "eps_growth"):
                val = row.rev_yoy if key == "rev_yoy" else row.eps_growth
                s = "+" if val > 0 else ""
                ax.text(cx + cw / 2, cy, f"{s}{val:.1f}%",
                        ha="center", va="center", fontsize=7.5,
                        color=_pct_txt(val), zorder=3)

            # ── 信心條 ───────────────────────────────────────────────────────
            elif key == "confidence":
                bar_x = cx + 0.08
                bar_w = cw - 0.16
                bar_h = 0.12
                # 灰色底
                ax.add_patch(FancyBboxPatch(
                    (bar_x, cy - bar_h / 2), bar_w, bar_h,
                    boxstyle="round,pad=0.01", linewidth=0, facecolor=COLOR_BAR_BG, zorder=2))
                # 填色（依信心高低變色）
                conf = max(0, min(100, row.confidence))
                bar_color = (
                    "#E8001E" if conf >= 80 else
                    "#E67E00" if conf >= 60 else
                    "#666666"
                )
                ax.add_patch(FancyBboxPatch(
                    (bar_x, cy - bar_h / 2), bar_w * conf / 100, bar_h,
                    boxstyle="round,pad=0.01", linewidth=0, facecolor=bar_color, zorder=3))
                ax.text(cx + cw / 2, cy + 0.13, f"{conf:.0f}",
                        ha="center", va="center", fontsize=6.5, color="#333333", zorder=3)

            # ── 星級 ─────────────────────────────────────────────────────────
            elif key == "stars":
                score = row.model_score
                if score >= 80:
                    ax.add_patch(FancyBboxPatch(
                        (cx + 0.05, ry - row_h + 0.06), cw - 0.10, row_h - 0.12,
                        boxstyle="round,pad=0.02", linewidth=0,
                        facecolor=COLOR_MODEL_HI, zorder=2))
                fg = COLOR_MODEL_TXT if score >= 80 else ("#555555" if score >= 60 else "#AAAAAA")
                ax.text(cx + cw / 2, cy, _stars(score),
                        ha="center", va="center", fontsize=8,
                        color=fg, zorder=3)

            # 格線
            ax.plot([cx, cx], [ry - row_h, ry], color=COLOR_BORDER, linewidth=0.3, zorder=4)

        # 行底線
        ax.plot([0, fig_w], [ry - row_h, ry - row_h], color=COLOR_BORDER, linewidth=0.3, zorder=4)

    # ── 底部大盤資訊列 ────────────────────────────────────────────────────────
    y_foot = y - n_rows * row_h
    ax.add_patch(FancyBboxPatch((0, y_foot - footer_h), fig_w, footer_h,
                                boxstyle="square,pad=0", linewidth=0,
                                facecolor="#1A1A2E", zorder=2))
    if market_index > 0:
        s = "+" if market_change >= 0 else ""
        mkt_color = COLOR_UP if market_change >= 0 else COLOR_DOWN
        ax.text(0.2, y_foot - footer_h / 2,
                f"加權指數  {market_index:,.2f}  {s}{market_change:.2f} ({s}{market_change_pct:.2f}%)",
                ha="left", va="center", fontsize=8.5, fontweight="bold",
                color=mkt_color, zorder=3)
    else:
        ax.text(0.2, y_foot - footer_h / 2,
                "★週核 ☆高連 •新資金 ▲同族 ◎達標 ■高頻",
                ha="left", va="center", fontsize=8, color="#AAAAAA", zorder=3)

    # 右下：產生時間 + /report next 提示
    gen_time = datetime.now().strftime("%m/%d %H:%M")
    hint = f"/report next 下一頁  " if total_pages > 1 else ""
    ax.text(fig_w - 0.15, y_foot - footer_h / 2,
            f"{hint}產生：{gen_time}",
            ha="right", va="center", fontsize=7, color="#888888", zorder=3)

    plt.tight_layout(pad=0)
    fig.savefig(str(output_path), dpi=150, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    return output_path


# ── 比較圖（3 股並排 + 雷達圖）──────────────────────────────────────────────

def generate_comparison_image(
    stocks: list,            # list[StockRow]，最多 3 筆
    output_path: Optional[Path] = None,
) -> Path:
    """產生多股並排比較圖（右側含雷達圖），回傳 Path"""
    if not _MPL_OK:
        raise RuntimeError("matplotlib 未安裝")

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = STATIC_DIR / f"compare_{ts}.png"

    stocks = stocks[:3]
    n = len(stocks)

    # ── 圖面佈局：左側比較表 + 右側雷達圖 ─────────────────────────────────
    fig = plt.figure(figsize=(14, 7))
    ax_table = fig.add_axes([0.01, 0.05, 0.55, 0.90])
    ax_radar = fig.add_axes([0.58, 0.08, 0.40, 0.82], projection="polar")
    fig.patch.set_facecolor("#FFFFFF")

    # ── 左側：比較表 ────────────────────────────────────────────────────────
    ax_table.set_xlim(0, 10); ax_table.set_ylim(0, 18); ax_table.axis("off")

    # 表頭
    ax_table.add_patch(FancyBboxPatch((0, 16.5), 10, 1.4,
                                      boxstyle="square,pad=0", linewidth=0,
                                      facecolor=COLOR_HEADER))
    ax_table.text(5, 17.2, "多股比較分析", ha="center", va="center",
                  fontsize=12, fontweight="bold", color="white")
    ax_table.text(9.8, 17.8, datetime.now().strftime("%m/%d"),
                  ha="right", va="center", fontsize=7, color="#AAAAAA")

    # 欄標題（股票名稱）
    col_w = 9.0 / max(n, 1)
    for i, row in enumerate(stocks):
        cx = 1.0 + i * col_w + col_w / 2
        bg = [COLOR_UP_BG, COLOR_MODEL_HI, "#FFF8E8"][i % 3]
        ax_table.add_patch(FancyBboxPatch(
            (1.0 + i * col_w, 15.3), col_w, 1.0,
            boxstyle="round,pad=0.05", linewidth=0, facecolor=bg))
        ax_table.text(cx, 15.85,
                      f"{row.stock_id}\n{row.name}",
                      ha="center", va="center", fontsize=9, fontweight="bold",
                      color="#1A1A2E")

    # 指標列
    METRICS = [
        ("收盤價",     lambda r: f"{r.close:,.1f}",              None),
        ("漲跌幅",     lambda r: f"{r.change_pct:+.2f}%",        lambda r: r.change_pct),
        ("成交量(千)", lambda r: f"{r.volume/1000:,.0f}",         None),
        ("5日法人",    lambda r: f"{r.chip_5d/1000:+.1f}k",      lambda r: r.chip_5d),
        ("20日法人",   lambda r: f"{r.chip_20d/1000:+.1f}k",     lambda r: r.chip_20d),
        ("外資連買",   lambda r: f"{r.foreign_buy_days:+d}天",   lambda r: r.foreign_buy_days),
        ("營收YoY",    lambda r: f"{r.rev_yoy:+.1f}%",           lambda r: r.rev_yoy),
        ("EPS成長",    lambda r: f"{r.eps_growth:+.1f}%",        lambda r: r.eps_growth),
        ("殖利率",     lambda r: f"{r.dividend_yield:.1f}%",      lambda r: r.dividend_yield),
        ("本益比",     lambda r: f"{r.pe_ratio:.1f}",            None),
        ("信心指數",   lambda r: f"{r.confidence:.0f}",          lambda r: r.confidence),
        ("模型評分",   lambda r: f"{r.model_score:.0f}  {_stars(r.model_score)}", lambda r: r.model_score),
        ("動能分",     lambda r: f"{r.momentum_score:.0f}",       lambda r: r.momentum_score),
        ("價值分",     lambda r: f"{r.value_score:.0f}",          lambda r: r.value_score),
    ]

    row_h = 14.8 / max(len(METRICS) + 1, 1)
    for mi, (label, fmt, color_fn) in enumerate(METRICS):
        y0 = 15.0 - (mi + 1) * row_h
        row_bg = "#F8F9FA" if mi % 2 == 0 else "#FFFFFF"
        ax_table.add_patch(FancyBboxPatch((0, y0), 10, row_h,
                                          boxstyle="square,pad=0", linewidth=0,
                                          facecolor=row_bg))
        # 指標名
        ax_table.text(0.5, y0 + row_h / 2, label,
                      ha="left", va="center", fontsize=8, color="#555555")

        # 各股數值
        vals = [color_fn(row) if color_fn else None for row in stocks] if stocks else []
        best_idx = None
        if vals and all(v is not None for v in vals):
            best_idx = int(np.argmax(vals))

        for i, row in enumerate(stocks):
            cx = 1.0 + i * col_w + col_w / 2
            val_str = fmt(row)
            is_best = (i == best_idx) and n > 1
            fg = "#C00020" if is_best else "#1A1A1A"
            fw = "bold" if is_best else "normal"
            ax_table.text(cx, y0 + row_h / 2, val_str,
                          ha="center", va="center", fontsize=8,
                          color=fg, fontweight=fw)

        ax_table.plot([0, 10], [y0, y0], color=COLOR_BORDER, linewidth=0.3)

    # ── 右側：雷達圖 ────────────────────────────────────────────────────────
    dimensions = ["動能", "價值", "籌碼", "技術", "基本面"]
    n_dim = len(dimensions)
    angles = np.linspace(0, 2 * np.pi, n_dim, endpoint=False).tolist()
    angles += angles[:1]   # 閉合

    colors = ["#E8001E", "#1A3A8F", "#E67E00"][:n]
    ax_radar.set_ylim(0, 100)
    ax_radar.set_xticks(angles[:-1])
    ax_radar.set_xticklabels(dimensions, fontsize=9, color="#333333")
    ax_radar.set_yticks([20, 40, 60, 80, 100])
    ax_radar.set_yticklabels(["20", "40", "60", "80", "100"], fontsize=6, color="#AAAAAA")
    ax_radar.grid(color="#DDDDDD", linewidth=0.5)
    ax_radar.set_facecolor("#FAFAFA")

    legend_handles = []
    for i, (row, color) in enumerate(zip(stocks, colors)):
        vals = [
            row.momentum_score,
            row.value_score,
            row.chip_score_v,
            row.tech_score,
            row.fundamental_score,
        ]
        vals += vals[:1]
        ax_radar.plot(angles, vals, color=color, linewidth=2, zorder=3)
        ax_radar.fill(angles, vals, color=color, alpha=0.12, zorder=2)
        legend_handles.append(
            mpatches.Patch(facecolor=color, label=f"{row.stock_id} {row.name}")
        )

    ax_radar.legend(handles=legend_handles,
                    loc="upper right", bbox_to_anchor=(1.35, 1.15),
                    fontsize=8.5, framealpha=0.8)
    ax_radar.set_title("五維雷達圖", fontsize=10, fontweight="bold",
                        color="#1A1A2E", pad=18)

    fig.savefig(str(output_path), dpi=150, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)
    return output_path


# ── LINE 推送 ────────────────────────────────────────────────────────────────

async def push_report_image(
    image_path: Path,
    user_ids: list[str],
    access_token: str,
    base_url: str,
    alt_text: str = "族群連動選股表",
) -> None:
    import httpx
    image_url = f"{base_url.rstrip('/')}/static/reports/{image_path.name}"
    headers = {"Authorization": f"Bearer {access_token}"}
    message = {
        "type": "image",
        "originalContentUrl": image_url,
        "previewImageUrl":    image_url,
    }
    async with httpx.AsyncClient(timeout=15) as c:
        for uid in user_ids:
            try:
                r = await c.post("https://api.line.me/v2/bot/message/push",
                                 json={"to": uid, "messages": [message]},
                                 headers=headers)
                if r.status_code != 200:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"[Report] push to {uid[:8]} HTTP {r.status_code}")
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"[Report] push error: {e}")


async def generate_and_push(
    group: str = "族群連動",
    target_date=None,
    user_ids: list[str] = None,
    access_token: str = "",
    base_url: str = "",
    stocks: list = None,
    market_state: str = "unknown",
    page: int = 1,
    total_pages: int = 1,
) -> Path:
    """一次性產生 + 推送"""
    if stocks is None:
        from backend.services.report_screener import all_screener, paginate
        all_rows = all_screener()
        stocks, total_pages = paginate(all_rows, page)
    path = generate_report_image(
        stocks=stocks, group=group, target_date=target_date,
        market_state=market_state, page=page, total_pages=total_pages,
    )
    if user_ids and access_token and base_url:
        await push_report_image(path, user_ids, access_token, base_url, alt_text=group)
    return path


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from backend.services.report_screener import (
        momentum_screener, value_screener, chip_screener,
        ai_screener, all_screener, paginate,
    )
    print("=== 產生各類選股圖 ===")
    for fn, name in [
        (momentum_screener, "動能選股"),
        (value_screener,    "存股選股"),
        (chip_screener,     "籌碼選股"),
        (ai_screener,       "AI族群"),
        (all_screener,      "全維度"),
    ]:
        rows = fn()
        page_rows, total = paginate(rows, page=1)
        path = generate_report_image(page_rows, group=name,
                                      market_state="bull",
                                      market_index=21500, market_change=+152.3,
                                      market_change_pct=+0.71,
                                      page=1, total_pages=total)
        print(f"  [{name}] {path}")

    print("\n=== 比較圖 ===")
    all_rows = all_screener()
    path = generate_comparison_image(all_rows[:3])
    print(f"  比較圖: {path}")
