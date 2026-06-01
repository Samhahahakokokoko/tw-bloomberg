"""
chart_service.py — K 線技術分析圖表產生器（深色主題）

功能：
  - 日 K 線（紅漲綠跌）+ MA5 / MA20 / MA60
  - 成交量長條圖
  - RSI(14) 含超買超賣區域
  - MACD（含柱狀圖）
  - 深色背景、中文字體自動偵測
  - 全程 in-memory（BytesIO），不寫入本地檔案系統
"""

from __future__ import annotations

import io
import logging
import os
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # 必須在 pyplot import 之前，避免 Tkinter/GUI 依賴
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import asyncio

logger = logging.getLogger(__name__)

# ── 字體設定（Linux 路徑優先；找不到則用系統預設）────────────────────────────

_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKtc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # 備援（無 CJK，但不會亂碼）
]

_font_loaded = False
for _fp in _FONT_CANDIDATES:
    if os.path.exists(_fp):
        try:
            import matplotlib.font_manager as fm
            _prop = fm.FontProperties(fname=_fp)
            plt.rcParams["font.family"] = _prop.get_name()
            plt.rcParams["axes.unicode_minus"] = False
            _font_loaded = True
            logger.info(f"[chart_service] 載入字體: {_fp}")
            break
        except Exception as _e:
            logger.warning(f"[chart_service] 字體載入失敗 {_fp}: {_e}")

if not _font_loaded:
    plt.rcParams["axes.unicode_minus"] = False
    logger.warning("[chart_service] 未找到 CJK 字體，中文可能顯示方塊")

# ── 顏色配置（深色主題）─────────────────────────────────────────────────────────

_STYLE = {
    "fig_facecolor":  "#1a1a2e",
    "ax_facecolor":   "#16213e",
    "text_color":     "#cccccc",
    "grid_color":     "#333355",
    "grid_lw":        0.3,
    "up_color":       "#e74c3c",   # 漲（紅）
    "down_color":     "#2ecc71",   # 跌（綠）
    "ma5_color":      "#f1c40f",   # MA5 黃
    "ma20_color":     "#3498db",   # MA20 藍
    "ma60_color":     "#e91e63",   # MA60 粉紅
    "rsi_color":      "#9b59b6",   # RSI 紫
    "macd_color":     "#3498db",   # MACD 藍
    "signal_color":   "#e67e22",   # Signal 橘
    "hist_pos_color": "#2ecc71",   # 柱正值綠
    "hist_neg_color": "#e74c3c",   # 柱負值紅
    "overbought_clr": "#e74c3c",   # 超買線紅
    "oversold_clr":   "#2ecc71",   # 超賣線綠
    "spine_color":    "#333355",
}


# ── 技術指標計算（純 numpy，不依賴 pandas）──────────────────────────────────────

def _sma(arr: np.ndarray, period: int) -> np.ndarray:
    """Simple Moving Average；前 (period-1) 筆填 NaN"""
    out = np.full(len(arr), np.nan)
    if len(arr) < period:
        return out
    for i in range(period - 1, len(arr)):
        out[i] = arr[i - period + 1 : i + 1].mean()
    return out


def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average"""
    out = np.full(len(arr), np.nan)
    if len(arr) < period:
        return out
    # 以前 period 個值的 SMA 作為起始
    out[period - 1] = arr[:period].mean()
    k = 2.0 / (period + 1)
    for i in range(period, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def _rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """RSI(period)"""
    out = np.full(len(closes), np.nan)
    if len(closes) < period + 1:
        return out
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    # 初始平均
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def _macd(
    closes: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    回傳 (macd_line, signal_line, histogram)
    """
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line = ema_fast - ema_slow
    # signal 是 macd_line 的 EMA；先把 NaN 填 0 再算（只在有效範圍計算）
    valid_mask = ~np.isnan(macd_line)
    sig_line = np.full(len(closes), np.nan)
    if valid_mask.sum() >= signal:
        valid_idx = np.where(valid_mask)[0]
        macd_valid = macd_line[valid_mask]
        sig_valid = np.full(len(macd_valid), np.nan)
        sig_valid[signal - 1] = macd_valid[:signal].mean()
        k = 2.0 / (signal + 1)
        for i in range(signal, len(macd_valid)):
            sig_valid[i] = macd_valid[i] * k + sig_valid[i - 1] * (1 - k)
        for local_i, global_i in enumerate(valid_idx):
            sig_line[global_i] = sig_valid[local_i]
    histogram = macd_line - sig_line
    return macd_line, sig_line, histogram


# ── 日期字串解析 ─────────────────────────────────────────────────────────────────

def _parse_date_label(date_str: str) -> str:
    """
    接受 '113/05/31'（民國年）或 '2024/05/31' 格式，
    回傳 'MM/DD' 供 X 軸顯示。
    """
    try:
        parts = date_str.replace("-", "/").split("/")
        if len(parts) == 3:
            year_part = int(parts[0])
            # 民國年（< 200）轉西元
            if year_part < 200:
                year_part += 1911
            return f"{int(parts[1]):02d}/{int(parts[2]):02d}"
    except Exception as e:
        pass
    return date_str[-5:] if len(date_str) >= 5 else date_str


# ── 繪圖輔助：設定 Axes 深色樣式 ────────────────────────────────────────────────

def _style_ax(ax: plt.Axes) -> None:
    s = _STYLE
    ax.set_facecolor(s["ax_facecolor"])
    ax.tick_params(colors=s["text_color"], labelsize=8)
    ax.yaxis.label.set_color(s["text_color"])
    ax.xaxis.label.set_color(s["text_color"])
    for spine in ax.spines.values():
        spine.set_edgecolor(s["spine_color"])
    ax.grid(color=s["grid_color"], linewidth=s["grid_lw"], linestyle="--", alpha=0.7)
    ax.set_axisbelow(True)


# ── 主函式 ────────────────────────────────────────────────────────────────────────

async def generate_chart(
    stock_code: str,
    kline_data: list[dict],
    name: str = "",
) -> bytes:
    """產生 K 線技術分析圖表，回傳 PNG bytes（全程 in-memory，不寫檔）。"""
    import asyncio
    return await asyncio.to_thread(_generate_chart_sync, stock_code, kline_data, name)


def _generate_chart_sync(
    stock_code: str,
    kline_data: list[dict],
    name: str = "",
) -> bytes:
    s = _STYLE

    # ── 資料驗證與預處理 ────────────────────────────────────────────────────────
    if not kline_data:
        logger.warning(f"[chart_service] {stock_code} kline_data 為空，產生空白圖")
        return _placeholder_bytes(stock_code, "無資料")

    # 過濾並確保欄位型別
    rows: list[dict] = []
    for row in kline_data:
        try:
            rows.append({
                "date":   str(row.get("date", "")),
                "open":   float(row.get("open") or 0),
                "high":   float(row.get("high") or 0),
                "low":    float(row.get("low") or 0),
                "close":  float(row.get("close") or 0),
                "volume": float(row.get("volume") or 0),
            })
        except (TypeError, ValueError) as e:
            logger.debug(f"[chart_service] 跳過無效行: {row} — {e}")

    if not rows:
        logger.warning(f"[chart_service] {stock_code} 有效資料為空")
        return _placeholder_bytes(stock_code, "資料解析失敗")

    n = len(rows)
    x_idx = np.arange(n)

    dates   = [_parse_date_label(r["date"]) for r in rows]
    opens   = np.array([r["open"]   for r in rows], dtype=float)
    highs   = np.array([r["high"]   for r in rows], dtype=float)
    lows    = np.array([r["low"]    for r in rows], dtype=float)
    closes  = np.array([r["close"]  for r in rows], dtype=float)
    volumes = np.array([r["volume"] for r in rows], dtype=float)

    # ── 技術指標 ────────────────────────────────────────────────────────────────
    ma5  = _sma(closes, 5)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)

    rsi_vals              = _rsi(closes, 14)
    macd_line, sig_line, histogram = _macd(closes, 12, 26, 9)

    # ── 建立圖表（4 subplots，高度比例 4:1.5:1.5:1.5）─────────────────────────
    fig, axes = plt.subplots(
        4, 1,
        figsize=(12, 10),
        dpi=120,
        gridspec_kw={"height_ratios": [4, 1.5, 1.5, 1.5]},
        sharex=True,
    )
    fig.patch.set_facecolor(s["fig_facecolor"])
    fig.subplots_adjust(hspace=0.08, left=0.07, right=0.97, top=0.93, bottom=0.08)

    ax_kline, ax_vol, ax_rsi, ax_macd = axes

    # ── 標題 ─────────────────────────────────────────────────────────────────────
    title_str = f"{stock_code}  {name}  K線技術分析圖" if name else f"{stock_code}  K線技術分析圖"
    fig.suptitle(title_str, color=s["text_color"], fontsize=13, fontweight="bold", y=0.97)

    # ── 1. K 線圖 ───────────────────────────────────────────────────────────────
    _style_ax(ax_kline)
    ax_kline.set_ylabel("價格（元）", color=s["text_color"], fontsize=9)

    candle_w = 0.6  # 蠟燭寬度（相對 x 單位）
    up_mask   = closes >= opens
    down_mask = ~up_mask

    # 漲（紅）蠟燭
    if up_mask.any():
        up_x = x_idx[up_mask]
        up_o = opens[up_mask]
        up_c = closes[up_mask]
        up_h = highs[up_mask]
        up_l = lows[up_mask]
        # 實體
        ax_kline.bar(
            up_x,
            up_c - up_o,
            bottom=up_o,
            width=candle_w,
            color=s["up_color"],
            edgecolor=s["up_color"],
            linewidth=0.3,
            zorder=3,
        )
        # 上影線
        ax_kline.vlines(up_x, up_c, up_h, color=s["up_color"], linewidth=0.8, zorder=3)
        # 下影線
        ax_kline.vlines(up_x, up_l, up_o, color=s["up_color"], linewidth=0.8, zorder=3)

    # 跌（綠）蠟燭
    if down_mask.any():
        dn_x = x_idx[down_mask]
        dn_o = opens[down_mask]
        dn_c = closes[down_mask]
        dn_h = highs[down_mask]
        dn_l = lows[down_mask]
        ax_kline.bar(
            dn_x,
            dn_o - dn_c,
            bottom=dn_c,
            width=candle_w,
            color=s["down_color"],
            edgecolor=s["down_color"],
            linewidth=0.3,
            zorder=3,
        )
        ax_kline.vlines(dn_x, dn_c, dn_h, color=s["down_color"], linewidth=0.8, zorder=3)
        ax_kline.vlines(dn_x, dn_l, dn_o, color=s["down_color"], linewidth=0.8, zorder=3)

    # 均線
    _plot_ma(ax_kline, x_idx, ma5,  s["ma5_color"],  "MA5",  1.2)
    _plot_ma(ax_kline, x_idx, ma20, s["ma20_color"], "MA20", 1.2)
    _plot_ma(ax_kline, x_idx, ma60, s["ma60_color"], "MA60", 1.2)

    ax_kline.legend(
        loc="upper left",
        fontsize=8,
        framealpha=0.3,
        facecolor=s["ax_facecolor"],
        edgecolor=s["spine_color"],
        labelcolor=s["text_color"],
    )

    # Y 軸自動範圍加一點 padding
    price_min = np.nanmin(lows)
    price_max = np.nanmax(highs)
    price_pad = (price_max - price_min) * 0.05 if price_max > price_min else 1.0
    ax_kline.set_ylim(price_min - price_pad, price_max + price_pad * 3)

    # ── 2. 成交量 ───────────────────────────────────────────────────────────────
    _style_ax(ax_vol)
    ax_vol.set_ylabel("成交量", color=s["text_color"], fontsize=9)

    vol_colors = np.where(up_mask, s["up_color"], s["down_color"])
    total_vol = volumes.sum()
    if total_vol > 0:
        ax_vol.bar(x_idx, volumes, width=candle_w, color=vol_colors, zorder=3)
        # Y 軸以萬張或千張顯示
        max_vol = volumes.max()
        if max_vol >= 1e6:
            ax_vol.yaxis.set_major_formatter(
                mticker.FuncFormatter(lambda v, _: f"{v/1e6:.1f}M")
            )
        elif max_vol >= 1e3:
            ax_vol.yaxis.set_major_formatter(
                mticker.FuncFormatter(lambda v, _: f"{v/1e3:.0f}K")
            )
    else:
        # 全零成交量：顯示提示文字
        ax_vol.text(
            0.5, 0.5, "成交量資料不足",
            transform=ax_vol.transAxes,
            ha="center", va="center",
            color=s["text_color"], fontsize=9, alpha=0.6,
        )

    # ── 3. RSI(14) ──────────────────────────────────────────────────────────────
    _style_ax(ax_rsi)
    ax_rsi.set_ylabel("RSI(14)", color=s["text_color"], fontsize=9)
    ax_rsi.set_ylim(0, 100)

    valid_rsi = ~np.isnan(rsi_vals)
    if valid_rsi.any():
        ax_rsi.plot(
            x_idx[valid_rsi], rsi_vals[valid_rsi],
            color=s["rsi_color"], linewidth=1.2, zorder=3,
        )
        # 超買超賣水平線
        ax_rsi.axhline(70, color=s["overbought_clr"], linewidth=0.8,
                       linestyle="--", alpha=0.8, zorder=2)
        ax_rsi.axhline(30, color=s["oversold_clr"],   linewidth=0.8,
                       linestyle="--", alpha=0.8, zorder=2)
        # 超買區填色
        ax_rsi.fill_between(
            x_idx[valid_rsi], 70, rsi_vals[valid_rsi],
            where=(rsi_vals[valid_rsi] >= 70),
            color=s["overbought_clr"], alpha=0.15, zorder=1,
        )
        # 超賣區填色
        ax_rsi.fill_between(
            x_idx[valid_rsi], rsi_vals[valid_rsi], 30,
            where=(rsi_vals[valid_rsi] <= 30),
            color=s["oversold_clr"], alpha=0.15, zorder=1,
        )
        # 70/30 標籤
        ax_rsi.text(
            n - 1, 71, "70",
            color=s["overbought_clr"], fontsize=7, va="bottom", ha="right",
        )
        ax_rsi.text(
            n - 1, 29, "30",
            color=s["oversold_clr"], fontsize=7, va="top", ha="right",
        )
    else:
        ax_rsi.text(
            0.5, 0.5, "RSI 資料不足",
            transform=ax_rsi.transAxes,
            ha="center", va="center",
            color=s["text_color"], fontsize=9, alpha=0.6,
        )

    # ── 4. MACD ──────────────────────────────────────────────────────────────────
    _style_ax(ax_macd)
    ax_macd.set_ylabel("MACD", color=s["text_color"], fontsize=9)

    valid_macd = ~np.isnan(macd_line)
    if valid_macd.any():
        # 柱狀圖
        hist_colors = np.where(
            histogram >= 0,
            s["hist_pos_color"],
            s["hist_neg_color"],
        )
        valid_hist = ~np.isnan(histogram)
        if valid_hist.any():
            ax_macd.bar(
                x_idx[valid_hist], histogram[valid_hist],
                width=candle_w, color=hist_colors[valid_hist],
                zorder=3, alpha=0.7,
            )
        # MACD 線
        ax_macd.plot(
            x_idx[valid_macd], macd_line[valid_macd],
            color=s["macd_color"], linewidth=1.0, label="MACD", zorder=4,
        )
        # Signal 線
        valid_sig = ~np.isnan(sig_line)
        if valid_sig.any():
            ax_macd.plot(
                x_idx[valid_sig], sig_line[valid_sig],
                color=s["signal_color"], linewidth=1.0, label="Signal", zorder=4,
            )
        # 零軸
        ax_macd.axhline(0, color=s["spine_color"], linewidth=0.6, linestyle="-", zorder=2)
        ax_macd.legend(
            loc="upper left",
            fontsize=7,
            framealpha=0.3,
            facecolor=s["ax_facecolor"],
            edgecolor=s["spine_color"],
            labelcolor=s["text_color"],
        )
    else:
        ax_macd.text(
            0.5, 0.5, "MACD 資料不足",
            transform=ax_macd.transAxes,
            ha="center", va="center",
            color=s["text_color"], fontsize=9, alpha=0.6,
        )

    # ── X 軸刻度：每隔一段顯示日期，旋轉 30° ─────────────────────────────────
    _set_xaxis(ax_macd, x_idx, dates, n)

    # ── 輸出為 bytes（in-memory，不寫檔）────────────────────────────────────────
    buf = io.BytesIO()
    try:
        fig.savefig(
            buf,
            format="png",
            dpi=120,
            bbox_inches="tight",
            facecolor=s["fig_facecolor"],
            edgecolor="none",
        )
        logger.info(f"[chart_service] {stock_code} 圖表已生成（{buf.tell()} bytes）")
    except Exception as e:
        logger.error(f"[chart_service] 生成失敗: {e}")
        raise
    finally:
        plt.close(fig)

    return buf.getvalue()


# ── 內部輔助函式 ─────────────────────────────────────────────────────────────────

def _plot_ma(
    ax: plt.Axes,
    x: np.ndarray,
    ma: np.ndarray,
    color: str,
    label: str,
    lw: float = 1.0,
) -> None:
    """繪製移動平均線（跳過 NaN 開頭）"""
    valid = ~np.isnan(ma)
    if valid.any():
        ax.plot(x[valid], ma[valid], color=color, linewidth=lw, label=label, zorder=4)


def _set_xaxis(
    ax: plt.Axes,
    x_idx: np.ndarray,
    dates: list[str],
    n: int,
) -> None:
    """設定 X 軸刻度（自動選取合理間隔，旋轉 30°）"""
    if n == 0:
        return
    # 目標顯示 8～12 個刻度
    target = max(1, min(12, n // 5))
    step   = max(1, n // target)
    tick_positions = x_idx[::step]
    tick_labels    = [dates[i] for i in tick_positions]

    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, rotation=30, ha="right", fontsize=7,
                       color=_STYLE["text_color"])
    ax.set_xlim(-0.5, n - 0.5)


def _placeholder_bytes(stock_code: str, msg: str) -> bytes:
    """回傳帶提示訊息的空白深色佔位圖 bytes"""
    s = _STYLE
    fig, ax = plt.subplots(figsize=(12, 10), dpi=120)
    fig.patch.set_facecolor(s["fig_facecolor"])
    ax.set_facecolor(s["ax_facecolor"])
    ax.axis("off")
    ax.text(
        0.5, 0.5,
        f"{stock_code}\n{msg}",
        transform=ax.transAxes,
        ha="center", va="center",
        color=s["text_color"], fontsize=18, alpha=0.7,
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor=s["fig_facecolor"], edgecolor="none")
    plt.close(fig)
    return buf.getvalue()
