"""
generate_report_image.py — 族群連動選股表圖片產生器 v3（深色高對比版）

設計規格：
  - 深色背景（#0A0F1E）+ 白字高對比
  - 字體尺寸：欄標題 10pt / 資料列 9.5pt（DPI=150 → ~21/20px）
  - 欄位：股票 / 價格(元) / 漲跌(%) / 成交量(張) / 籌碼 / 基本面 / 模型
  - 漲跌加 ▲▼ 箭頭
  - 數值加單位（元、張、%）
  - 底部圖例說明
  - 支援分頁 / 市場狀態 badge / 信心條 / 星級

顏色慣例（台股）：漲 → 紅，跌 → 綠
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import numpy as np

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from backend.services.report_screener import StockRow

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import matplotlib.font_manager as fm
    from matplotlib.patches import FancyBboxPatch
    font_path = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    if os.path.exists(font_path):
        prop = fm.FontProperties(fname=font_path)
        plt.rcParams["font.family"] = prop.get_name()
        plt.rcParams["axes.unicode_minus"] = False
    _MPL_OK = True
except ImportError:
    _MPL_OK = False

STATIC_DIR = Path(os.getenv("STATIC_DIR", "./static/reports"))
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# ── 深色高對比配色 ────────────────────────────────────────────────────────────

BG_MAIN      = "#0A0F1E"   # 主背景（深海軍藍黑）
BG_HEADER    = "#060B14"   # 表頭（更深）
BG_GRP_HDR   = "#0F1A2E"   # 欄群組標題
BG_COL_HDR   = "#131F35"   # 欄標題
BG_ROW_ODD   = "#0D1525"   # 奇數列
BG_ROW_EVEN  = "#0A1020"   # 偶數列
BG_BADGE     = "#1A2840"   # badge 背景

# 文字顏色
TXT_WHITE    = "#E8EEF8"   # 主文字
TXT_MUTED    = "#6A7E9C"   # 次要文字
TXT_HDR      = "#8AADCE"   # 欄標題

# 漲跌色
CLR_UP       = "#FF4455"   # 漲（紅）
CLR_UP_DIM   = "#3A1520"   # 漲背景（暗紅）
CLR_DOWN     = "#22DD88"   # 跌（綠）
CLR_DOWN_DIM = "#0A2A1A"   # 跌背景（暗綠）
CLR_NEUTRAL  = "#7A8FA8"

# 籌碼
CLR_CHIP_POS = "#FF4455"
CLR_CHIP_NEG = "#22DD88"

# 模型高分
CLR_MODEL_HI = "#4A90E2"
CLR_MODEL_BG = "#0D1E38"

# 格線
CLR_BORDER   = "#1C2E48"

# 市場狀態顏色
REGIME_CLR = {
    "bull":     ("#FF4455", "多頭"),
    "bear":     ("#22DD88", "空頭"),
    "sideways": ("#FFAA00", "盤整"),
    "volatile": ("#BB66FF", "高波動"),
    "unknown":  ("#6A7E9C", "未知"),
}

# ── 欄位定義 ─────────────────────────────────────────────────────────────────
# (key, header, width_ratio)
COLUMNS = [
    ("label",      "股票代碼/名稱",  2.10),
    ("close",      "收盤價(元)",     0.85),
    ("change_pct", "漲跌幅",         0.85),
    ("volume_k",   "成交量(張)",     0.90),
    ("chip_5d",    "法人5日買賣",    1.05),
    ("chip_20d",   "法人20日買賣",   1.05),
    ("rev_yoy",    "年營收成長",     0.85),
    ("eps_growth", "EPS成長",        0.78),
    ("confidence", "AI綜合評分",     1.10),
    ("stars",      "信心★",          0.82),
]

COL_GROUPS = [
    ("股票",        1),
    ("盤面行情",    3),
    ("法人籌碼",    2),
    ("基本面",      2),
    ("AI評分",      2),
]

# 底部圖例說明
LEGEND_TEXT = (
    "📖 欄位說明：法人籌碼=外資+投信買賣超（紅=買超/綠=賣超）"
    " | 基本面=年營收/EPS年增率 | AI綜合=系統綜合評分0~100 | 信心★=建議力道"
)


# ── 輔助函式 ──────────────────────────────────────────────────────────────────

def _arrow(val: float) -> str:
    return "▲" if val > 0 else ("▼" if val < 0 else "─")

def _pct_clr(val: float) -> str:
    return CLR_UP if val > 0 else (CLR_DOWN if val < 0 else CLR_NEUTRAL)

def _pct_bg(val: float) -> str:
    return CLR_UP_DIM if val > 0 else (CLR_DOWN_DIM if val < 0 else BG_ROW_ODD)

def _chip_clr(val: float) -> str:
    return CLR_CHIP_POS if val > 0 else (CLR_CHIP_NEG if val < 0 else CLR_NEUTRAL)

def _stars(score: float) -> str:
    n = round(max(0, min(5, score / 20)))
    return "★" * n + "☆" * (5 - n)

def _fmt_chip(val: float, days: int = 0) -> str:
    """格式化法人買賣超，顯示 +5,300張(3天) 格式"""
    abs_v = abs(val)
    sign  = "+" if val >= 0 else "-"
    if abs_v >= 1000:
        s = f"{sign}{abs_v/1000:.1f}K張"
    else:
        s = f"{sign}{abs_v:.0f}張"
    if days:
        s += f"({days}天)"
    return s

def _fmt_pct(val: float) -> str:
    arrow = _arrow(val)
    return f"{arrow}{abs(val):.1f}%"


# ── 主圖產生 ─────────────────────────────────────────────────────────────────

def generate_report_image(
    stocks: list,
    group: str = "選股結果",
    target_date: Optional[date] = None,
    market_state: str = "unknown",
    market_index: float = 0.0,
    market_change: float = 0.0,
    market_change_pct: float = 0.0,
    page: int = 1,
    total_pages: int = 1,
    output_path: Optional[Path] = None,
) -> Path:
    if not _MPL_OK:
        raise RuntimeError("matplotlib 未安裝")

    if target_date is None:
        target_date = date.today()
    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = STATIC_DIR / f"report_{ts}.png"

    n_rows = len(stocks)
    logger.info(f"[generate_report_image] group={group!r} n_rows={n_rows} page={page}/{total_pages}")
    if n_rows > 0:
        r0 = stocks[0]
        logger.info(f"[generate_report_image] row0: {r0.stock_id} {r0.name} close={r0.close} vol={r0.volume} chg={r0.change_pct}")
    elif n_rows == 0:
        logger.warning(f"[generate_report_image] ⚠️ stocks 清單為空！group={group!r}")

    fig_w  = 16.0       # 稍微加寬，讓字體更舒適

    # 行高加大以容納更大字體
    row_h     = 0.62
    hdr_h     = 0.95
    grp_h     = 0.36
    col_h     = 0.46
    footer_h  = 0.42
    legend_h  = 0.28
    fig_h     = hdr_h + grp_h + col_h + n_rows * row_h + footer_h + legend_h + 0.12

    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.axis("off")
    fig.patch.set_facecolor(BG_MAIN)

    # ── x 座標 ──────────────────────────────────────────────────────────────
    total_r = sum(c[2] for c in COLUMNS)
    xpos, xwid = [], []
    x = 0.0
    for _, _, r in COLUMNS:
        w = r / total_r * fig_w
        xpos.append(x); xwid.append(w); x += w

    y = fig_h  # 從頂部往下

    # ── 表頭 ─────────────────────────────────────────────────────────────────
    ax.add_patch(FancyBboxPatch((0, y - hdr_h), fig_w, hdr_h,
                                boxstyle="square,pad=0", lw=0, facecolor=BG_HEADER))
    date_str = target_date.strftime("%Y-%m-%d")
    ax.text(fig_w / 2, y - hdr_h / 2,
            f"族群連動選股  ▏  {group}  ▏  {date_str}",
            ha="center", va="center", fontsize=13, fontweight="bold",
            color=TXT_WHITE)

    # 市場狀態 badge（右上角）
    reg_clr, reg_lbl = REGIME_CLR.get(market_state, REGIME_CLR["unknown"])
    bw, bh = 1.15, 0.32
    ax.add_patch(FancyBboxPatch((fig_w - bw - 0.15, y - 0.15 - bh), bw, bh,
                                boxstyle="round,pad=0.04", lw=0, facecolor=reg_clr))
    ax.text(fig_w - 0.15 - bw / 2, y - 0.15 - bh / 2, reg_lbl,
            ha="center", va="center", fontsize=9, fontweight="bold", color="white")

    # 分頁資訊（左上）
    if total_pages > 1:
        ax.text(0.18, y - 0.2,
                f"第 {page} 頁 / 共 {total_pages} 頁   /report next 下一頁",
                ha="left", va="center", fontsize=8, color=TXT_MUTED)
    y -= hdr_h

    # ── 欄群組標題 ─────────────────────────────────────────────────────────
    ci = 0
    for gi, (gname, span) in enumerate(COL_GROUPS):
        xs = xpos[ci]; xe = xpos[ci + span - 1] + xwid[ci + span - 1]
        ax.add_patch(FancyBboxPatch((xs, y - grp_h), xe - xs, grp_h,
                                    boxstyle="square,pad=0", lw=0, facecolor=BG_GRP_HDR))
        ax.plot([xs, xs], [y - grp_h, y], color=CLR_BORDER, lw=0.7)
        ax.text((xs + xe) / 2, y - grp_h / 2, gname,
                ha="center", va="center", fontsize=9, fontweight="bold", color=TXT_HDR)
        ci += span
    y -= grp_h

    # ── 欄標題 ───────────────────────────────────────────────────────────────
    for ci_, (key, hdr, _) in enumerate(COLUMNS):
        ax.add_patch(FancyBboxPatch((xpos[ci_], y - col_h), xwid[ci_], col_h,
                                    boxstyle="square,pad=0", lw=0.4,
                                    edgecolor=CLR_BORDER, facecolor=BG_COL_HDR))
        ax.text(xpos[ci_] + xwid[ci_] / 2, y - col_h / 2, hdr,
                ha="center", va="center", fontsize=10, fontweight="bold", color=TXT_HDR)
    y -= col_h

    # ── 資料列 ───────────────────────────────────────────────────────────────
    for ri, row in enumerate(stocks):
        row_bg = BG_ROW_ODD if ri % 2 == 0 else BG_ROW_EVEN
        ry = y - ri * row_h

        ax.add_patch(FancyBboxPatch((0, ry - row_h), fig_w, row_h,
                                    boxstyle="square,pad=0", lw=0, facecolor=row_bg))

        for ci_, (key, _, _) in enumerate(COLUMNS):
            cx = xpos[ci_]; cw = xwid[ci_]
            cy = ry - row_h / 2

            # ── 股票（代碼 + 名稱 + 標籤）──────────────────────────────
            if key == "label":
                ax.text(cx + 0.12, cy + 0.10,
                        f"{row.stock_id}  {row.name}",
                        ha="left", va="center", fontsize=9.5, fontweight="bold",
                        color=TXT_WHITE)
                if row.tags:
                    ax.text(cx + 0.12, cy - 0.13,
                            "  ".join(row.tags),
                            ha="left", va="center", fontsize=7.5, color=TXT_MUTED)

            # ── 價格（元）──────────────────────────────────────────────
            elif key == "close":
                close_str = f"{row.close:,.1f}" if (row.close or 0) > 0 else "--"
                ax.text(cx + cw / 2, cy,
                        close_str,
                        ha="center", va="center", fontsize=9.5, color=TXT_WHITE)

            # ── 漲跌 ▲▼ ─────────────────────────────────────────────────
            elif key == "change_pct":
                cell_bg = _pct_bg(row.change_pct)
                ax.add_patch(FancyBboxPatch(
                    (cx + 0.05, ry - row_h + 0.06), cw - 0.10, row_h - 0.12,
                    boxstyle="round,pad=0.02", lw=0, facecolor=cell_bg))
                ax.text(cx + cw / 2, cy,
                        _fmt_pct(row.change_pct),
                        ha="center", va="center", fontsize=9.5, fontweight="bold",
                        color=_pct_clr(row.change_pct))

            # ── 成交量（張）────────────────────────────────────────────
            elif key == "volume_k":
                vol_k = (row.volume or 0) / 1000
                vol_str = f"{vol_k:,.0f}" if vol_k > 0 else "--"
                ax.text(cx + cw / 2, cy,
                        vol_str,
                        ha="center", va="center", fontsize=9, color=TXT_WHITE)

            # ── 籌碼（法人買賣超，含連買天數）────────────────────────────
            elif key in ("chip_5d", "chip_20d"):
                val  = row.chip_5d if key == "chip_5d" else row.chip_20d
                days = getattr(row, "foreign_buy_days", 0) or 0
                chip_str = _fmt_chip(val, abs(int(days)) if days else 0) if val != 0 else "--"
                ax.text(cx + cw / 2, cy,
                        chip_str,
                        ha="center", va="center", fontsize=8.0, fontweight="bold",
                        color=_chip_clr(val) if val != 0 else TXT_MUTED)

            # ── 基本面（年營收 / EPS 成長率）────────────────────────────
            elif key in ("rev_yoy", "eps_growth"):
                val      = row.rev_yoy if key == "rev_yoy" else row.eps_growth
                lbl_pre  = "年增" if key == "rev_yoy" else "EPS"
                fund_str = f"{lbl_pre}{_arrow(val)}{abs(val):.0f}%" if val != 0 else "--"
                ax.text(cx + cw / 2, cy + 0.08,
                        fund_str,
                        ha="center", va="center", fontsize=8.5,
                        color=_pct_clr(val) if val != 0 else TXT_MUTED)

            # ── AI綜合評分（分數 + 進度條）────────────────────────────
            elif key == "confidence":
                conf    = max(0, min(100, row.confidence))
                bar_clr = (CLR_UP if conf >= 75 else
                           "#FFAA00" if conf >= 55 else TXT_MUTED)
                bx  = cx + 0.10
                bw_ = cw - 0.20
                bh_ = 0.12
                ax.add_patch(FancyBboxPatch(
                    (bx, cy - bh_ / 2), bw_, bh_,
                    boxstyle="round,pad=0.01", lw=0, facecolor="#1C2E48"))
                ax.add_patch(FancyBboxPatch(
                    (bx, cy - bh_ / 2), bw_ * conf / 100, bh_,
                    boxstyle="round,pad=0.01", lw=0, facecolor=bar_clr))
                # 評分數字（上方）
                ax.text(cx + cw / 2, cy + 0.17,
                        f"AI {conf:.0f}分",
                        ha="center", va="center", fontsize=8.5,
                        color=bar_clr, fontweight="bold")

            # ── 星級 ────────────────────────────────────────────────────
            elif key == "stars":
                score = row.model_score
                if score >= 80:
                    ax.add_patch(FancyBboxPatch(
                        (cx + 0.06, ry - row_h + 0.07), cw - 0.12, row_h - 0.14,
                        boxstyle="round,pad=0.02", lw=0, facecolor=CLR_MODEL_BG))
                fg = CLR_MODEL_HI if score >= 80 else (TXT_WHITE if score >= 60 else TXT_MUTED)
                ax.text(cx + cw / 2, cy, _stars(score),
                        ha="center", va="center", fontsize=9, color=fg)

            # 格線
            ax.plot([cx, cx], [ry - row_h, ry],
                    color=CLR_BORDER, lw=0.35)

        ax.plot([0, fig_w], [ry - row_h, ry - row_h],
                color=CLR_BORDER, lw=0.35)

    # ── 底部資訊列 ────────────────────────────────────────────────────────────
    y_foot = y - n_rows * row_h
    ax.add_patch(FancyBboxPatch((0, y_foot - footer_h), fig_w, footer_h,
                                boxstyle="square,pad=0", lw=0, facecolor=BG_HEADER))

    # 大盤資訊（左側，有資料才顯示）
    if market_index > 0:
        s    = "+" if market_change >= 0 else ""
        mclr = CLR_UP if market_change >= 0 else CLR_DOWN
        arr  = "▲" if market_change >= 0 else "▼"
        ax.text(0.2, y_foot - footer_h / 2,
                f"加權指數  {market_index:,.2f}  {arr}{s}{market_change:.2f} ({s}{market_change_pct:.2f}%)",
                ha="left", va="center", fontsize=9, fontweight="bold", color=mclr)

    # 產生時間（右側）
    gen_time = datetime.now().strftime("%m/%d %H:%M")
    ax.text(fig_w - 0.15, y_foot - footer_h / 2,
            f"產生：{gen_time}",
            ha="right", va="center", fontsize=8, color=TXT_MUTED)

    # 標籤說明列（底部固定，始終顯示）
    y_legend = y_foot - footer_h
    ax.add_patch(FancyBboxPatch((0, y_legend - legend_h), fig_w, legend_h,
                                boxstyle="square,pad=0", lw=0, facecolor=BG_MAIN))
    ax.text(fig_w / 2, y_legend - legend_h / 2, LEGEND_TEXT,
            ha="center", va="center", fontsize=7.5, color=TXT_MUTED)

    plt.tight_layout(pad=0)
    fig.savefig(str(output_path), dpi=150, bbox_inches="tight",
                facecolor=BG_MAIN, edgecolor="none")
    plt.close(fig)
    return output_path


# ── 比較圖 ────────────────────────────────────────────────────────────────────

def generate_comparison_image(
    stocks: list,
    output_path: Optional[Path] = None,
) -> Path:
    if not _MPL_OK:
        raise RuntimeError("matplotlib 未安裝")
    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = STATIC_DIR / f"compare_{ts}.png"

    stocks = stocks[:3]
    n = len(stocks)

    fig = plt.figure(figsize=(15, 7.5))
    ax_t = fig.add_axes([0.01, 0.05, 0.56, 0.90])
    ax_r = fig.add_axes([0.59, 0.08, 0.39, 0.82], projection="polar")
    fig.patch.set_facecolor(BG_MAIN)

    ax_t.set_xlim(0, 10); ax_t.set_ylim(0, 18); ax_t.axis("off")
    ax_t.add_patch(FancyBboxPatch((0, 16.5), 10, 1.4,
                                  boxstyle="square,pad=0", lw=0, facecolor=BG_HEADER))
    ax_t.text(5, 17.2, "多股比較分析",
              ha="center", va="center", fontsize=13, fontweight="bold", color=TXT_WHITE)

    col_w = 9.0 / max(n, 1)
    col_colors = [CLR_UP, CLR_MODEL_HI, "#FFAA00"][:n]

    for i, row in enumerate(stocks):
        cx = 1.0 + i * col_w + col_w / 2
        ax_t.add_patch(FancyBboxPatch(
            (1.0 + i * col_w, 15.3), col_w, 1.0,
            boxstyle="round,pad=0.05", lw=1, edgecolor=col_colors[i],
            facecolor=BG_GRP_HDR))
        ax_t.text(cx, 15.82, f"{row.stock_id}\n{row.name}",
                  ha="center", va="center", fontsize=9.5, fontweight="bold", color=TXT_WHITE)

    METRICS = [
        ("收盤價(元)",    lambda r: f"{r.close:,.1f}",          None),
        ("漲跌(%)",       lambda r: _fmt_pct(r.change_pct),     lambda r: r.change_pct),
        ("成交量(千張)",  lambda r: f"{r.volume/1000:,.0f}",    None),
        ("5日法人(張)",   lambda r: _fmt_chip(r.chip_5d),       lambda r: r.chip_5d),
        ("20日法人(張)",  lambda r: _fmt_chip(r.chip_20d),      lambda r: r.chip_20d),
        ("外資連買(日)",  lambda r: f"{r.foreign_buy_days:+d}日", lambda r: r.foreign_buy_days),
        ("營收YoY(%)",    lambda r: f"{_arrow(r.rev_yoy)}{abs(r.rev_yoy):.1f}%", lambda r: r.rev_yoy),
        ("EPS成長(%)",    lambda r: f"{_arrow(r.eps_growth)}{abs(r.eps_growth):.1f}%", lambda r: r.eps_growth),
        ("殖利率(%)",     lambda r: f"{r.dividend_yield:.1f}%", lambda r: r.dividend_yield),
        ("本益比",        lambda r: f"{r.pe_ratio:.1f}",        None),
        ("信心指數",      lambda r: f"{r.confidence:.0f}",      lambda r: r.confidence),
        ("模型評分",      lambda r: f"{r.model_score:.0f}  {_stars(r.model_score)}", lambda r: r.model_score),
        ("動能分",        lambda r: f"{r.momentum_score:.0f}",  lambda r: r.momentum_score),
        ("價值分",        lambda r: f"{r.value_score:.0f}",     lambda r: r.value_score),
    ]

    rh = 14.8 / max(len(METRICS) + 1, 1)
    for mi, (label, fmt, cfn) in enumerate(METRICS):
        y0   = 15.0 - (mi + 1) * rh
        rbg  = BG_ROW_ODD if mi % 2 == 0 else BG_ROW_EVEN
        ax_t.add_patch(FancyBboxPatch((0, y0), 10, rh,
                                      boxstyle="square,pad=0", lw=0, facecolor=rbg))
        ax_t.text(0.5, y0 + rh / 2, label,
                  ha="left", va="center", fontsize=9, color=TXT_MUTED)

        vals    = [cfn(r) if cfn else None for r in stocks]
        best_i  = (int(np.argmax(vals)) if all(v is not None for v in vals) and vals
                   else None)
        for i, row in enumerate(stocks):
            cx   = 1.0 + i * col_w + col_w / 2
            s    = fmt(row)
            is_b = (i == best_i) and n > 1
            fg   = col_colors[i] if is_b else TXT_WHITE
            ax_t.text(cx, y0 + rh / 2, s,
                      ha="center", va="center", fontsize=9,
                      color=fg, fontweight="bold" if is_b else "normal")

        ax_t.plot([0, 10], [y0, y0], color=CLR_BORDER, lw=0.4)

    # 雷達圖（深色）
    dims   = ["動能", "價值", "籌碼", "技術", "基本面"]
    angles = np.linspace(0, 2 * np.pi, len(dims), endpoint=False).tolist()
    angles += angles[:1]

    ax_r.set_facecolor(BG_ROW_ODD)
    ax_r.set_ylim(0, 100)
    ax_r.set_xticks(angles[:-1])
    ax_r.set_xticklabels(dims, fontsize=9.5, color=TXT_WHITE)
    ax_r.set_yticks([20, 40, 60, 80, 100])
    ax_r.set_yticklabels(["", "", "", "", ""], fontsize=0)
    ax_r.grid(color=CLR_BORDER, linewidth=0.8)
    ax_r.spines["polar"].set_color(CLR_BORDER)

    handles = []
    for i, (row, clr) in enumerate(zip(stocks, col_colors)):
        v = [row.momentum_score, row.value_score, row.chip_score_v,
             row.tech_score, row.fundamental_score]
        v += v[:1]
        ax_r.plot(angles, v, color=clr, lw=2.5)
        ax_r.fill(angles, v, color=clr, alpha=0.15)
        handles.append(mpatches.Patch(facecolor=clr, label=f"{row.stock_id} {row.name}"))

    ax_r.legend(handles=handles, loc="upper right", bbox_to_anchor=(1.40, 1.18),
                fontsize=9, framealpha=0.2,
                labelcolor=TXT_WHITE, facecolor=BG_GRP_HDR, edgecolor=CLR_BORDER)
    ax_r.set_title("五維雷達圖", fontsize=11, fontweight="bold", color=TXT_WHITE, pad=20)

    fig.savefig(str(output_path), dpi=150, bbox_inches="tight",
                facecolor=BG_MAIN, edgecolor="none")
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
    headers   = {"Authorization": f"Bearer {access_token}"}
    message   = {"type": "image",
                  "originalContentUrl": image_url,
                  "previewImageUrl":    image_url}
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
