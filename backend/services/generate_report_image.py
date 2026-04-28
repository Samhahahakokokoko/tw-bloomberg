"""
generate_report_image.py — 族群連動選股表圖片產生器

輸出一張包含完整選股資訊的 PNG 圖片：
  - 表頭：「族群連動｜預計進場日 YYYY-MM-DD」
  - 欄位：股票（代碼+名稱+標籤）、盤面、籌碼、基本面、模型
  - 台股色彩慣例：漲紅、跌綠

標籤自動計算（compute_labels）：
  ★週核  週線 KD > 50 且 MA20 向上
  ☆高連  連續 5 日收紅
  •新資金  成交量創 20 日新高
  ▲同族  同族群平均漲幅 > 3%
  ◎達標  收盤突破目標價
  ■高頻  日內波動 > 3%

使用方式：
    path = generate_report_image(stocks, group="AI族群", target_date=date.today())
    await push_report_image(path, user_ids=["Uxxxx"])
"""

from __future__ import annotations

import os
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import numpy as np

# Matplotlib（必要）
try:
    import matplotlib
    matplotlib.use("Agg")   # 無頭模式，不需 display
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.patches import FancyBboxPatch
    _MPL_OK = True
except ImportError:
    _MPL_OK = False

# Pillow（選用，可做更精細排版）
try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

# 靜態資源目錄
STATIC_DIR = Path(os.getenv("STATIC_DIR", "./static/reports"))
STATIC_DIR.mkdir(parents=True, exist_ok=True)


# ── 常數 ─────────────────────────────────────────────────────────────────────

# 台股色彩：漲→紅、跌→綠（與歐美相反）
COLOR_UP       = "#FF3333"      # 漲，亮紅
COLOR_UP_BG    = "#FFE8E8"      # 漲，淡紅背景
COLOR_DOWN     = "#00AA44"      # 跌，深綠
COLOR_DOWN_BG  = "#E8F8EE"      # 跌，淡綠背景
COLOR_NEUTRAL  = "#444444"
COLOR_HEADER   = "#1A1A2E"      # 深藍黑表頭
COLOR_SUB_HDR  = "#16213E"      # 欄位標題
COLOR_MODEL_HIGH_BG = "#E8F0FF" # 模型分數高 → 淡藍背景
COLOR_CHIP_POS = "#FF3333"      # 籌碼正
COLOR_CHIP_NEG = "#00AA44"      # 籌碼負
COLOR_ROW_ODD  = "#FAFAFA"
COLOR_ROW_EVEN = "#FFFFFF"
COLOR_BORDER   = "#DDDDDD"
COLOR_LABEL_BG = "#F0F0F0"

# 標籤定義
LABEL_DEFS = {
    "week_core":  "★週核",
    "high_cons":  "☆高連",
    "new_money":  "•新資金",
    "same_group": "▲同族",
    "target":     "◎達標",
    "high_freq":  "■高頻",
}

# 欄位群組
COL_GROUPS = [
    ("股票", 1),
    ("盤面", 3),
    ("籌碼", 2),
    ("基本面", 2),
    ("模型", 2),
]

# 欄位明細（key, header, width_ratio）
COLUMNS = [
    ("label",      "代碼/名稱",   2.2),
    ("close",      "收盤",        0.8),
    ("change_pct", "漲跌%",       0.8),
    ("volume_k",   "量(千)",      0.9),
    ("chip_5d",    "5日法人",     1.0),
    ("chip_20d",   "20日法人",    1.0),
    ("rev_yoy",    "營收YoY",     0.9),
    ("rev_mom",    "營收MoM",     0.9),
    ("eps_growth", "EPS成長",     0.9),
    ("model_score","基礎分",      0.8),
    ("day_rank",   "日排名",      0.8),
]


# ── 資料類別 ─────────────────────────────────────────────────────────────────

@dataclass
class StockRow:
    stock_id:    str
    name:        str
    close:       float
    change_pct:  float      # % 例: +2.5
    volume:      float      # 張
    chip_5d:     float      # 5日法人買賣（張，正=買）
    chip_20d:    float      # 20日法人買賣（張）
    rev_yoy:     float      # 營收年增率（%）
    rev_mom:     float      # 營收月增率（%）
    eps_growth:  float      # EPS成長率（%）
    model_score: float      # 模型基礎分（0~100）
    day_rank:    int        # 今日排名
    target_price: float = 0.0     # 目標價（達標用）
    # 技術指標（標籤計算用）
    kd_weekly:   float = 50.0     # 週線KD值
    ma20_slope:  float = 0.0      # MA20斜率（正=向上）
    consec_up:   int   = 0        # 連續收紅天數
    vol_20d_max: float = 0.0      # 20日最大成交量
    intraday_range: float = 0.0   # 日內振幅（%）
    group_avg_change: float = 0.0 # 同族群平均漲幅（%）
    # 自動計算標籤
    tags: list[str] = field(default_factory=list)

    def volume_k(self) -> float:
        return self.volume / 1000


# ── 標籤自動計算 ─────────────────────────────────────────────────────────────

def compute_labels(row: StockRow) -> list[str]:
    """依技術條件自動計算該股適用的標籤"""
    tags: list[str] = []

    # ★週核：週線 KD > 50 且 MA20 向上
    if row.kd_weekly > 50 and row.ma20_slope > 0:
        tags.append(LABEL_DEFS["week_core"])

    # ☆高連：連續 5 日收紅
    if row.consec_up >= 5:
        tags.append(LABEL_DEFS["high_cons"])

    # •新資金：成交量創 20 日新高
    if row.vol_20d_max > 0 and row.volume >= row.vol_20d_max:
        tags.append(LABEL_DEFS["new_money"])

    # ▲同族：同族群平均漲幅 > 3%
    if row.group_avg_change > 3.0:
        tags.append(LABEL_DEFS["same_group"])

    # ◎達標：收盤突破目標價
    if row.target_price > 0 and row.close >= row.target_price:
        tags.append(LABEL_DEFS["target"])

    # ■高頻：日內波動 > 3%
    if row.intraday_range > 3.0:
        tags.append(LABEL_DEFS["high_freq"])

    return tags


# ── 範例資料（模擬，實際應從 DB / API 取得）─────────────────────────────────

_AI_STOCKS: list[dict] = [
    dict(stock_id="2330", name="台積電", close=850.0, change_pct=+2.3,
         volume=35000, chip_5d=+8500, chip_20d=+32000,
         rev_yoy=+28.5, rev_mom=+5.2, eps_growth=+32.1,
         model_score=88, day_rank=1,
         target_price=880, kd_weekly=72, ma20_slope=+1.2,
         consec_up=6, vol_20d_max=34000, intraday_range=2.8, group_avg_change=3.5),
    dict(stock_id="2454", name="聯發科", close=1180.0, change_pct=+3.1,
         volume=18000, chip_5d=+3200, chip_20d=+15000,
         rev_yoy=+22.0, rev_mom=+8.1, eps_growth=+25.0,
         model_score=85, day_rank=2,
         target_price=1150, kd_weekly=68, ma20_slope=+0.9,
         consec_up=3, vol_20d_max=20000, intraday_range=3.8, group_avg_change=3.5),
    dict(stock_id="6770", name="力積電", close=52.8, change_pct=+1.8,
         volume=22000, chip_5d=+1200, chip_20d=+4500,
         rev_yoy=+15.2, rev_mom=+3.0, eps_growth=+18.5,
         model_score=74, day_rank=3,
         target_price=50.0, kd_weekly=61, ma20_slope=+0.5,
         consec_up=5, vol_20d_max=21000, intraday_range=2.2, group_avg_change=3.5),
    dict(stock_id="2303", name="聯電", close=46.5, change_pct=-0.5,
         volume=28000, chip_5d=-800, chip_20d=+2000,
         rev_yoy=+8.0, rev_mom=-1.5, eps_growth=+5.0,
         model_score=62, day_rank=8,
         target_price=48.0, kd_weekly=55, ma20_slope=+0.1,
         consec_up=2, vol_20d_max=27000, intraday_range=1.8, group_avg_change=3.5),
    dict(stock_id="3711", name="日月光投", close=152.0, change_pct=+1.2,
         volume=9500, chip_5d=+550, chip_20d=+3200,
         rev_yoy=+12.0, rev_mom=+2.8, eps_growth=+14.2,
         model_score=71, day_rank=5,
         target_price=155, kd_weekly=64, ma20_slope=+0.7,
         consec_up=4, vol_20d_max=9000, intraday_range=1.5, group_avg_change=3.5),
]

_COOLING_STOCKS: list[dict] = [
    dict(stock_id="6415", name="矽力-KY", close=1350.0, change_pct=+4.2,
         volume=5200, chip_5d=+2100, chip_20d=+8500,
         rev_yoy=+35.0, rev_mom=+9.5, eps_growth=+40.0,
         model_score=92, day_rank=1,
         target_price=1300, kd_weekly=78, ma20_slope=+1.5,
         consec_up=7, vol_20d_max=5100, intraday_range=4.5, group_avg_change=4.2),
    dict(stock_id="3552", name="同亨", close=285.0, change_pct=+3.8,
         volume=3200, chip_5d=+800, chip_20d=+3000,
         rev_yoy=+28.0, rev_mom=+6.0, eps_growth=+33.0,
         model_score=87, day_rank=2,
         target_price=280, kd_weekly=73, ma20_slope=+1.1,
         consec_up=6, vol_20d_max=3100, intraday_range=3.2, group_avg_change=4.2),
    dict(stock_id="3450", name="聯鈞", close=95.5, change_pct=+2.5,
         volume=4800, chip_5d=+400, chip_20d=+1800,
         rev_yoy=+18.5, rev_mom=+4.0, eps_growth=+22.0,
         model_score=78, day_rank=4,
         target_price=95.0, kd_weekly=65, ma20_slope=+0.8,
         consec_up=5, vol_20d_max=4700, intraday_range=2.8, group_avg_change=4.2),
    dict(stock_id="8299", name="群聯", close=535.0, change_pct=-0.3,
         volume=2800, chip_5d=-200, chip_20d=+1200,
         rev_yoy=+10.0, rev_mom=-0.8, eps_growth=+8.5,
         model_score=65, day_rank=9,
         target_price=550, kd_weekly=52, ma20_slope=+0.2,
         consec_up=1, vol_20d_max=2900, intraday_range=1.5, group_avg_change=4.2),
    dict(stock_id="6269", name="台郡", close=128.0, change_pct=+1.8,
         volume=3500, chip_5d=+320, chip_20d=+1500,
         rev_yoy=+14.0, rev_mom=+3.2, eps_growth=+16.0,
         model_score=72, day_rank=6,
         target_price=125, kd_weekly=60, ma20_slope=+0.6,
         consec_up=4, vol_20d_max=3400, intraday_range=2.1, group_avg_change=4.2),
]

_GENERAL_STOCKS: list[dict] = _AI_STOCKS + _COOLING_STOCKS[:3]


def build_stock_rows(raw_list: list[dict]) -> list[StockRow]:
    """將 dict 列表轉換為 StockRow 並自動計算標籤"""
    rows: list[StockRow] = []
    for d in raw_list:
        row = StockRow(**{k: d[k] for k in StockRow.__dataclass_fields__ if k in d})
        row.tags = compute_labels(row)
        rows.append(row)
    return rows


def get_mock_data(group: str) -> list[StockRow]:
    """取得指定族群的模擬資料"""
    group_lower = group.lower()
    if "ai" in group_lower or "人工智慧" in group_lower:
        return build_stock_rows(_AI_STOCKS)
    elif "散熱" in group_lower or "cooling" in group_lower:
        return build_stock_rows(_COOLING_STOCKS)
    else:
        return build_stock_rows(_GENERAL_STOCKS)


# ── 圖片產生（Matplotlib）────────────────────────────────────────────────────

def _pct_color(val: float, is_text: bool = False) -> str:
    """漲跌幅對應顏色（台股：紅漲綠跌）"""
    if val > 0:
        return COLOR_UP if is_text else COLOR_UP_BG
    elif val < 0:
        return COLOR_DOWN if is_text else COLOR_DOWN_BG
    return COLOR_NEUTRAL if is_text else "#F5F5F5"


def _chip_color(val: float) -> str:
    """籌碼正負對應文字顏色"""
    return COLOR_CHIP_POS if val > 0 else (COLOR_CHIP_NEG if val < 0 else COLOR_NEUTRAL)


def generate_report_image(
    stocks: Optional[list[StockRow]] = None,
    group: str = "族群連動",
    target_date: Optional[date] = None,
    output_path: Optional[Path] = None,
) -> Path:
    """
    產生選股表圖片，回傳圖片 Path。

    stocks:      StockRow 列表（為 None 時自動取模擬資料）
    group:       族群名稱（顯示於表頭 & 決定模擬資料類別）
    target_date: 預計進場日（預設今日）
    output_path: 儲存路徑（預設 STATIC_DIR/report_{timestamp}.png）
    """
    if not _MPL_OK:
        raise RuntimeError("matplotlib 未安裝，請執行 pip install matplotlib")

    if stocks is None:
        stocks = get_mock_data(group)

    if target_date is None:
        target_date = date.today()

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = STATIC_DIR / f"report_{ts}.png"

    n_rows = len(stocks)
    n_cols = len(COLUMNS)

    # ── 畫布設定 ─────────────────────────────────────────────────────────────
    fig_w = 14.0
    row_h = 0.52
    header_h = 0.9
    col_header_h = 0.45
    group_header_h = 0.35
    fig_h = header_h + group_header_h + col_header_h + n_rows * row_h + 0.3

    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.axis("off")
    fig.patch.set_facecolor("#FFFFFF")

    # ── 欄位 x 座標計算 ────────────────────────────────────────────────────
    total_ratio = sum(c[2] for c in COLUMNS)
    x_positions: list[float] = []
    x_widths: list[float] = []
    x = 0.0
    for _, _, ratio in COLUMNS:
        w = ratio / total_ratio * fig_w
        x_positions.append(x)
        x_widths.append(w)
        x += w

    # ── 表頭（最頂部深色大標題）─────────────────────────────────────────────
    y_top = fig_h
    header_rect = FancyBboxPatch(
        (0, y_top - header_h), fig_w, header_h,
        boxstyle="square,pad=0", linewidth=0,
        facecolor=COLOR_HEADER, zorder=2,
    )
    ax.add_patch(header_rect)
    date_str = target_date.strftime("%Y-%m-%d")
    ax.text(
        fig_w / 2, y_top - header_h / 2,
        f"族群連動  |  {group}  |  預計進場日 {date_str}",
        ha="center", va="center", fontsize=13, fontweight="bold",
        color="white", zorder=3,
        fontfamily="DejaVu Sans",
    )

    # 右上角產生時間
    gen_time = datetime.now().strftime("%m/%d %H:%M")
    ax.text(fig_w - 0.2, y_top - 0.22, f"產生：{gen_time}",
            ha="right", va="center", fontsize=7, color="#AAAAAA", zorder=3)

    # ── 欄群組標題（盤面/籌碼/基本面/模型）──────────────────────────────────
    y_grp = y_top - header_h
    grp_colors = ["#16213E", "#1A2744", "#162338", "#1C2B44", "#162244"]
    col_idx = 0
    for gi, (grp_name, span) in enumerate(COL_GROUPS):
        x_start = x_positions[col_idx]
        x_end   = x_positions[col_idx + span - 1] + x_widths[col_idx + span - 1]
        rect = FancyBboxPatch(
            (x_start, y_grp - group_header_h), x_end - x_start, group_header_h,
            boxstyle="square,pad=0", linewidth=0,
            facecolor=grp_colors[gi % len(grp_colors)], zorder=2,
        )
        ax.add_patch(rect)
        ax.plot([x_start, x_start], [y_grp - group_header_h, y_grp],
                color="#333355", linewidth=0.5, zorder=3)
        ax.text(
            (x_start + x_end) / 2, y_grp - group_header_h / 2,
            grp_name, ha="center", va="center",
            fontsize=8, fontweight="bold", color="white", zorder=3,
        )
        col_idx += span

    # ── 欄位標題 ───────────────────────────────────────────────────────────
    y_col = y_grp - group_header_h
    for ci, (key, header, _) in enumerate(COLUMNS):
        xc = x_positions[ci]
        wc = x_widths[ci]
        rect = FancyBboxPatch(
            (xc, y_col - col_header_h), wc, col_header_h,
            boxstyle="square,pad=0", linewidth=0.3,
            edgecolor=COLOR_BORDER,
            facecolor="#2C3E50", zorder=2,
        )
        ax.add_patch(rect)
        ax.text(
            xc + wc / 2, y_col - col_header_h / 2, header,
            ha="center", va="center", fontsize=7.5, fontweight="bold",
            color="#E0E0E0", zorder=3,
        )

    # ── 資料列 ────────────────────────────────────────────────────────────
    y_data_start = y_col - col_header_h
    for ri, row in enumerate(stocks):
        y_row = y_data_start - ri * row_h
        row_bg = COLOR_ROW_ODD if ri % 2 == 0 else COLOR_ROW_EVEN

        # 整行底色
        bg_rect = FancyBboxPatch(
            (0, y_row - row_h), fig_w, row_h,
            boxstyle="square,pad=0", linewidth=0,
            facecolor=row_bg, zorder=1,
        )
        ax.add_patch(bg_rect)

        for ci, (key, _, _) in enumerate(COLUMNS):
            xc = x_positions[ci]
            wc = x_widths[ci]
            y_center = y_row - row_h / 2

            # ─ 股票欄（代碼 + 名稱 + 標籤）─────────────────────────────
            if key == "label":
                ax.text(xc + 0.12, y_center + 0.08,
                        f"{row.stock_id}  {row.name}",
                        ha="left", va="center", fontsize=8, fontweight="bold",
                        color="#1A1A1A", zorder=3)
                if row.tags:
                    tag_str = "  ".join(row.tags)
                    ax.text(xc + 0.12, y_center - 0.11,
                            tag_str, ha="left", va="center",
                            fontsize=6.5, color="#555555", zorder=3)

            # ─ 收盤價 ─────────────────────────────────────────────────
            elif key == "close":
                ax.text(xc + wc / 2, y_center,
                        f"{row.close:,.1f}",
                        ha="center", va="center", fontsize=8,
                        color="#1A1A1A", zorder=3)

            # ─ 漲跌幅（背景上色）──────────────────────────────────────
            elif key == "change_pct":
                cell_bg = _pct_color(row.change_pct, is_text=False)
                cell_fg = _pct_color(row.change_pct, is_text=True)
                cr = FancyBboxPatch(
                    (xc + 0.03, y_row - row_h + 0.04), wc - 0.06, row_h - 0.08,
                    boxstyle="round,pad=0.02", linewidth=0,
                    facecolor=cell_bg, zorder=2,
                )
                ax.add_patch(cr)
                sign = "+" if row.change_pct > 0 else ""
                ax.text(xc + wc / 2, y_center,
                        f"{sign}{row.change_pct:.2f}%",
                        ha="center", va="center", fontsize=8, fontweight="bold",
                        color=cell_fg, zorder=3)

            # ─ 成交量 ─────────────────────────────────────────────────
            elif key == "volume_k":
                ax.text(xc + wc / 2, y_center,
                        f"{row.volume_k():,.0f}",
                        ha="center", va="center", fontsize=7.5,
                        color="#444444", zorder=3)

            # ─ 籌碼（5日 / 20日法人）──────────────────────────────────
            elif key in ("chip_5d", "chip_20d"):
                val = row.chip_5d if key == "chip_5d" else row.chip_20d
                fg  = _chip_color(val)
                sign = "+" if val > 0 else ""
                ax.text(xc + wc / 2, y_center,
                        f"{sign}{val/1000:+.1f}k",
                        ha="center", va="center", fontsize=7.5,
                        fontweight="bold", color=fg, zorder=3)

            # ─ 基本面（YoY / MoM / EPS）───────────────────────────────
            elif key in ("rev_yoy", "rev_mom", "eps_growth"):
                val = {
                    "rev_yoy": row.rev_yoy,
                    "rev_mom": row.rev_mom,
                    "eps_growth": row.eps_growth,
                }[key]
                fg = _pct_color(val, is_text=True)
                sign = "+" if val > 0 else ""
                ax.text(xc + wc / 2, y_center,
                        f"{sign}{val:.1f}%",
                        ha="center", va="center", fontsize=7.5,
                        color=fg, zorder=3)

            # ─ 模型分數（高→藍色背景）─────────────────────────────────
            elif key == "model_score":
                score = row.model_score
                bg = COLOR_MODEL_HIGH_BG if score >= 80 else row_bg
                fg = "#1A3A8F" if score >= 80 else ("#555555" if score >= 60 else "#999999")
                if score >= 80:
                    sr = FancyBboxPatch(
                        (xc + 0.05, y_row - row_h + 0.05), wc - 0.10, row_h - 0.10,
                        boxstyle="round,pad=0.02", linewidth=0,
                        facecolor=bg, zorder=2,
                    )
                    ax.add_patch(sr)
                ax.text(xc + wc / 2, y_center,
                        f"{score:.0f}",
                        ha="center", va="center", fontsize=8, fontweight="bold",
                        color=fg, zorder=3)

            # ─ 日排名 ─────────────────────────────────────────────────
            elif key == "day_rank":
                rank_fg = "#C00000" if row.day_rank <= 3 else "#444444"
                ax.text(xc + wc / 2, y_center,
                        f"#{row.day_rank}",
                        ha="center", va="center", fontsize=8,
                        color=rank_fg, fontweight="bold" if row.day_rank <= 3 else "normal",
                        zorder=3)

            # 格線
            ax.plot([xc, xc], [y_row - row_h, y_row],
                    color=COLOR_BORDER, linewidth=0.3, zorder=4)

        # 行底線
        ax.plot([0, fig_w], [y_row - row_h, y_row - row_h],
                color=COLOR_BORDER, linewidth=0.3, zorder=4)

    # ── 圖例 ─────────────────────────────────────────────────────────────
    legend_y = y_data_start - n_rows * row_h - 0.05
    legend_items = [
        (COLOR_UP, "漲紅"),
        (COLOR_DOWN, "跌綠"),
        ("#1A3A8F", "模型高分"),
        (COLOR_CHIP_POS, "籌碼正"),
        (COLOR_CHIP_NEG, "籌碼負"),
    ]
    lx = 0.2
    for color, label in legend_items:
        patch = mpatches.Patch(facecolor=color, label=label)
        ax.text(lx, legend_y, f"■ {label}", ha="left", va="top",
                fontsize=7, color=color)
        lx += 2.0

    ax.text(fig_w - 0.2, legend_y,
            "★週核 ☆高連 •新資金 ▲同族 ◎達標 ■高頻",
            ha="right", va="top", fontsize=7, color="#666666")

    plt.tight_layout(pad=0)
    fig.savefig(str(output_path), dpi=150, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)

    return output_path


# ── LINE Image 推送 ───────────────────────────────────────────────────────────

async def push_report_image(
    image_path: Path,
    user_ids: list[str],
    access_token: str,
    base_url: str,
    alt_text: str = "族群連動選股表",
) -> None:
    """
    將圖片以 LINE Image Message 推送給指定用戶。

    image_path:   本地圖片路徑
    user_ids:     LINE user ID 列表
    access_token: LINE Channel Access Token
    base_url:     伺服器公開 base URL，例 https://tw-bloomberg.railway.app
    alt_text:     替代文字
    """
    import httpx

    # 構建圖片公開 URL（FastAPI StaticFiles 掛載在 /static）
    rel = image_path.relative_to(STATIC_DIR.parent) if STATIC_DIR.parent in image_path.parents else image_path.name
    image_url = f"{base_url.rstrip('/')}/static/reports/{image_path.name}"
    preview_url = image_url   # 同張圖作為預覽

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    message = {
        "type": "image",
        "originalContentUrl": image_url,
        "previewImageUrl": preview_url,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        for uid in user_ids:
            payload = {"to": uid, "messages": [message]}
            try:
                resp = await client.post(
                    "https://api.line.me/v2/bot/message/push",
                    json=payload,
                    headers=headers,
                )
                if resp.status_code == 200:
                    pass  # 成功
                else:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"[Report] push to {uid[:8]} failed: {resp.status_code} {resp.text[:200]}"
                    )
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"[Report] push error for {uid[:8]}: {e}")


async def generate_and_push(
    group: str = "族群連動",
    target_date: Optional[date] = None,
    user_ids: Optional[list[str]] = None,
    access_token: str = "",
    base_url: str = "",
    stocks: Optional[list[StockRow]] = None,
) -> Path:
    """
    一次性：產生圖片 + 推送給所有訂閱者。
    回傳圖片路徑。
    """
    if target_date is None:
        target_date = date.today()

    path = generate_report_image(stocks=stocks, group=group, target_date=target_date)

    if user_ids and access_token and base_url:
        await push_report_image(
            image_path=path,
            user_ids=user_ids,
            access_token=access_token,
            base_url=base_url,
            alt_text=f"族群連動｜{group}",
        )

    return path


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from pathlib import Path
    import sys

    print("=== 產生族群連動選股表 ===")

    groups = ["AI族群", "散熱族群", "一般"]
    for group in groups:
        path = generate_report_image(group=group)
        print(f"[{group}] 圖片已儲存: {path}")

    print("\n=== 標籤計算測試 ===")
    test_row = StockRow(
        stock_id="2330", name="台積電", close=900, change_pct=2.5,
        volume=36000, chip_5d=9000, chip_20d=35000,
        rev_yoy=30, rev_mom=6, eps_growth=35,
        model_score=91, day_rank=1,
        target_price=880, kd_weekly=75, ma20_slope=1.5,
        consec_up=6, vol_20d_max=35000, intraday_range=4.0, group_avg_change=4.5,
    )
    tags = compute_labels(test_row)
    print(f"  2330 標籤: {tags}")
    print("  預期觸發: ★週核 ☆高連 •新資金 ▲同族 ◎達標 ■高頻")
