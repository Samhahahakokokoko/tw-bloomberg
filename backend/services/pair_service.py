"""配對交易分析服務 — 價差 Z 值、相關係數、套利訊號

get_pair_analysis_sync(code1, code2) 為同步函式，由呼叫方透過 run_in_executor 執行。
"""

import os
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf
from loguru import logger


def get_pair_analysis_sync(code1: str, code2: str) -> str:
    """同步版本：計算兩檔股票的配對交易分析（Z 值、相關係數、套利訊號）。"""
    try:
        # ── 1. 下載 1 年股價 ──────────────────────────────────────────────────
        end_date = datetime.today()
        start_date = end_date - timedelta(days=365)
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")

        def _download_close(code: str) -> pd.Series:
            for suffix in (".TW", ".TWO"):
                ticker = f"{code}{suffix}"
                df = yf.download(ticker, start=start_str, end=end_str,
                                 progress=False, auto_adjust=True)
                if df.empty:
                    continue
                # 處理 MultiIndex 欄位（yfinance >= 0.2.x）
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                close = df["Close"].dropna()
                if not close.empty:
                    return close
            return pd.Series(dtype=float)

        close1 = _download_close(code1)
        close2 = _download_close(code2)

        if close1.empty:
            return f"❌ 配對分析失敗：無法取得 {code1} 的股價資料"
        if close2.empty:
            return f"❌ 配對分析失敗：無法取得 {code2} 的股價資料"

        # ── 2. 對齊共同交易日 ─────────────────────────────────────────────────
        combined = pd.concat([close1.rename("p1"), close2.rename("p2")], axis=1).dropna()
        if len(combined) < 20:
            return f"❌ 配對分析失敗：{code1} 與 {code2} 共同交易日不足"

        # ── 3. 價格比與 Z 值 ──────────────────────────────────────────────────
        ratio_series = combined["p1"] / combined["p2"]
        mean = float(ratio_series.mean())
        std = float(ratio_series.std())
        current_ratio = float(ratio_series.iloc[-1])
        z = (current_ratio - mean) / std if std > 0 else 0.0

        price1 = float(combined["p1"].iloc[-1])
        price2 = float(combined["p2"].iloc[-1])

        # ── 4. Pearson 相關係數（日報酬） ─────────────────────────────────────
        ret1 = combined["p1"].pct_change().dropna()
        ret2 = combined["p2"].pct_change().dropna()
        ret_df = pd.concat([ret1.rename("r1"), ret2.rename("r2")], axis=1).dropna()
        corr = float(ret_df["r1"].corr(ret_df["r2"])) if len(ret_df) >= 5 else float("nan")

        # ── 5. 相關係數評語 ───────────────────────────────────────────────────
        if pd.isna(corr):
            corr_str = "N/A"
            corr_note = "⚠️ 無法計算相關係數"
        else:
            corr_str = f"{corr:.2f}"
            corr_note = "✅ 相關性良好（> 0.7）" if corr >= 0.7 else "⚠️ 相關係數偏低，風險較高！"

        # ── 6. 套利訊號 ───────────────────────────────────────────────────────
        expected_return = abs(z) * (std / mean) * 100  # 估計收斂報酬 %

        if z > 2:
            signal = (
                f"📈 {code1} 相對高估（z=+{z:.2f}σ）\n"
                f"👉 做空 {code1}，做多 {code2}\n"
                f"預期收斂報酬 ≈ {expected_return:.1f}%，時間 1~4 週"
            )
        elif z < -2:
            signal = (
                f"📉 {code2} 相對高估（z={z:.2f}σ）\n"
                f"👉 做空 {code2}，做多 {code1}\n"
                f"預期收斂報酬 ≈ {expected_return:.1f}%，時間 1~4 週"
            )
        else:
            signal = f"⏳ 價差正常（z={z:+.2f}σ），等待 ±2σ 訊號"

        # ── 7. 組合回傳字串 ───────────────────────────────────────────────────
        return (
            f"🔄 配對交易：{code1} vs {code2}\n"
            f"──────────────────────────\n"
            f"現價：{code1}={price1:.2f} / {code2}={price2:.2f}\n"
            f"價格比：{current_ratio:.4f}\n"
            f"歷史均值：{mean:.4f} ± {std:.4f}\n"
            f"Z 值：{z:+.2f}σ\n"
            f"相關係數：{corr_str}  {corr_note}\n"
            f"──────────────────────────\n"
            f"{signal}\n"
            f"⚠️ 配對交易需同步建倉，注意流動性風險"
        )

    except Exception as e:
        logger.error(f"[pair] get_pair_analysis_sync({code1}, {code2}) 失敗: {e}")
        return f"❌ 配對分析失敗：{e}"
