"""
slippage_engine.py — 真實成交滑價模擬引擎

滑價規則（依成交量分層）：
  成交量 < 500 張   → 滑價 0.30%（低流動性）
  成交量 500-2000 張 → 滑價 0.15%（中流動性）
  成交量 > 2000 張   → 滑價 0.05%（高流動性）
  漲跌停時：無法成交（回傳 can_fill=False）

整合方式：
  在 BacktestEngine 的成交邏輯中，先呼叫 SlippageEngine.fill()
  取得 fill_price，再以此價格計算損益。

  # 例：BacktestEngine 改造
  slip = SlippageEngine()
  result = slip.fill("buy", price=close, volume_k=vol/1000, prev_close=prev_c)
  if result.can_fill:
      actual_price = result.fill_price
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

LIMIT_BAND = 0.10   # 台股漲跌停幅度 10%


@dataclass
class FillResult:
    """成交結果"""
    can_fill:     bool
    fill_price:   Optional[float]   # 含滑價的成交價；無法成交時為 None
    slippage_pct: float             # 滑價率（例 0.003 = 0.3%）
    slippage_amt: float             # 每股滑價金額
    reason:       str


class SlippageEngine:
    """
    真實成交滑價模擬引擎。

    使用方式：
        engine = SlippageEngine()
        result = engine.fill("buy", price=100.0, volume_k=1200, prev_close=99.0)
        if result.can_fill:
            actual_price = result.fill_price   # 含滑價成交價
        else:
            # 漲停買不到 / 跌停賣不掉
            pass

    volume_k: 當日成交量（張，1張=1000股）
    """

    # (上限張數 or None, 滑價率, 說明)
    TIERS = [
        (500,   0.0030, "低流動性 < 500 張"),
        (2000,  0.0015, "中流動性 500-2000 張"),
        (None,  0.0005, "高流動性 > 2000 張"),
    ]

    def __init__(
        self,
        limit_band:    float = LIMIT_BAND,
        custom_tiers:  Optional[list] = None,
    ):
        self.limit_band = limit_band
        self.tiers      = custom_tiers or self.TIERS

    def slippage_rate(self, volume_k: float) -> tuple[float, str]:
        """依成交量（張）回傳 (slip_rate, 說明)"""
        for threshold, rate, label in self.tiers:
            if threshold is None or volume_k < threshold:
                return rate, label
        return self.tiers[-1][1], self.tiers[-1][2]

    def is_limit_up(self, price: float, prev_close: float) -> bool:
        if prev_close <= 0:
            return False
        return price >= prev_close * (1 + self.limit_band - 0.001)

    def is_limit_down(self, price: float, prev_close: float) -> bool:
        if prev_close <= 0:
            return False
        return price <= prev_close * (1 - self.limit_band + 0.001)

    def fill(
        self,
        side:       str,         # "buy" 或 "sell"
        price:      float,       # 委託價格
        volume_k:   float,       # 當日成交量（張）
        prev_close: float = 0.0, # 前日收盤（0 = 不做漲跌停檢查）
    ) -> FillResult:
        """
        計算含滑價的成交價。

        漲停時 buy 訂單無法成交；跌停時 sell 訂單無法成交。
        """
        if prev_close > 0:
            if side == "buy" and self.is_limit_up(price, prev_close):
                return FillResult(
                    can_fill=False, fill_price=None,
                    slippage_pct=0.0, slippage_amt=0.0,
                    reason="漲停，買單無法成交",
                )
            if side == "sell" and self.is_limit_down(price, prev_close):
                return FillResult(
                    can_fill=False, fill_price=None,
                    slippage_pct=0.0, slippage_amt=0.0,
                    reason="跌停，賣單無法成交",
                )

        slip_rate, label = self.slippage_rate(volume_k)

        # 買進往上滑（成本增加）；賣出往下滑（收益減少）
        if side == "buy":
            fill_price = price * (1 + slip_rate)
        else:
            fill_price = price * (1 - slip_rate)

        return FillResult(
            can_fill=True,
            fill_price=round(fill_price, 2),
            slippage_pct=slip_rate,
            slippage_amt=round(abs(fill_price - price), 4),
            reason=label,
        )

    def batch_fill(
        self,
        side:        str,
        prices:      list[float],
        volumes_k:   list[float],
        prev_closes: Optional[list[float]] = None,
    ) -> list[FillResult]:
        """批次計算（用於向量化回測）"""
        if prev_closes is None:
            prev_closes = [0.0] * len(prices)
        return [
            self.fill(side, p, v, pc)
            for p, v, pc in zip(prices, volumes_k, prev_closes)
        ]

    def calc_slippage_cost(
        self,
        price:     float,
        shares:    int,
        volume_k:  float,
        side:      str = "buy",
    ) -> float:
        """便利函式：回傳滑價總成本（元）"""
        r = self.fill(side, price, volume_k)
        if not r.can_fill:
            return 0.0
        return r.slippage_amt * shares


_global_slippage: Optional[SlippageEngine] = None


def get_slippage_engine() -> SlippageEngine:
    global _global_slippage
    if _global_slippage is None:
        _global_slippage = SlippageEngine()
    return _global_slippage


# ── Mock data + 獨立測試 ───────────────────────────────────────────────────

if __name__ == "__main__":
    engine = SlippageEngine()

    print("=== 滑價模擬測試 ===")
    cases = [
        ("buy",  100.0,  200,   99.0,  "低流動性買"),
        ("buy",  100.0, 1000,   99.0,  "中流動性買"),
        ("buy",  100.0, 5000,   99.0,  "高流動性買"),
        ("sell", 100.0, 5000,   99.0,  "高流動性賣"),
        ("buy",  109.9, 1000,  100.0,  "漲停買（應失敗）"),
        ("sell",  90.0, 1000,  100.0,  "跌停賣（應失敗）"),
        ("buy",  100.0,  100,    0.0,  "無前收（不檢查停板）"),
    ]
    for side, price, vol, prev, desc in cases:
        r = engine.fill(side, price, float(vol), prev)
        ok = "✅" if r.can_fill else "⛔"
        print(f"{ok} {desc:20s} fill={r.fill_price}  "
              f"slip={r.slippage_pct*100:.2f}%  {r.reason}")
