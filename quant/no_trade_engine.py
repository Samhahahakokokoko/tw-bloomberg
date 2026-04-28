"""
no_trade_engine.py — 不交易條件過濾引擎

在送出實際下單前，NoTradeEngine 執行多層過濾，
任一條件觸發即阻止交易並說明原因。

過濾層（依優先順序）：
  1. 市場停市         — 台股法定假日、颱風停市
  2. 除息/除權日       — 除息前後 1 日保護（避免填息風險）
  3. 漲跌停鎖定        — 股價已觸及漲停（追高風險）或跌停（流動性不足）
  4. 極端跳空         — 開盤跳空 > 閾值，執行價格不確定性過大
  5. 低流動性         — 成交量 < N 日均量 × 閾值（難以成交）
  6. 高波動過濾        — 近期波動率超標（ATR / 收盤 > 閾值）
  7. 集成訊號衝突      — EnsembleEngine 分數落在中性區間（不明確）
  8. 盤態禁止         — 空頭市場下禁止做多，或多頭市場下禁止做空
  9. 重大事件日        — 財報發布日、聯準會決議日（外部注入）
  10. 部位已達上限      — 同標的已持有，不重複加碼

輸出：
  NoTradeDecision(trade_ok=True/False, reasons=[...], filters_triggered=[...])

使用方式：
    engine = NoTradeEngine()

    decision = engine.check(
        date="2026-04-28",
        stock_id="2330",
        close=850.0,
        open_price=855.0,
        volume=12000000,
        avg_volume_5d=8000000,
        atr14=18.0,
        ensemble_score=52.0,        # 來自 EnsembleEngine
        regime="bull",
        action="buy",               # 預計方向
        is_ex_div_day=False,
        is_event_day=False,
        current_position=0,         # 目前持有張數
        max_position=10,
        limit_up=False,
        limit_down=False,
    )
    if decision.trade_ok:
        # 送出委託
        ...
    else:
        print(decision.reasons)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── 參數常數 ──────────────────────────────────────────────────────────────────

# 極端跳空閾值（開盤 vs 昨收漲跌幅）
GAP_UP_THRESHOLD   =  0.03    # 跳空 > +3% → 追高風險
GAP_DOWN_THRESHOLD = -0.03    # 跳空 < -3% → 跳空恐慌

# 低流動性：成交量低於 5 日均量 × 此比例
LOW_VOLUME_RATIO = 0.30

# 高波動：ATR14 / close > 此比例
HIGH_VOL_RATIO = 0.05         # 5% 為高波動

# 集成訊號中性區間（不明確 → 不交易）
ENSEMBLE_NEUTRAL_LOW  = 42.0
ENSEMBLE_NEUTRAL_HIGH = 58.0

# 台股法定假日（每年更新）：格式 "MM-DD"
TW_FIXED_HOLIDAYS = {
    "01-01",  # 元旦
    "02-28",  # 和平紀念日
    "04-04",  # 兒童節
    "04-05",  # 清明節（通常）
    "05-01",  # 勞動節
    "10-10",  # 國慶日
    "12-25",  # 聖誕節（非假日，但示範）
}

# 已知盤態 × 方向 禁止組合
REGIME_ACTION_BLOCK: dict[str, set[str]] = {
    "bear":   {"buy"},        # 空頭禁止做多
    "volatile": set(),         # 波動盤：不自動禁止，但後面有波動過濾
}


# ── 資料類別 ─────────────────────────────────────────────────────────────────

@dataclass
class NoTradeDecision:
    """不交易決策結果"""
    trade_ok:          bool
    filters_triggered: list[str]       # 觸發的過濾層名稱
    reasons:           list[str]       # 人類可讀的原因說明
    score:             float = 0.0     # 通過層數比例（0~1，越高越安全）

    def to_dict(self) -> dict:
        return {
            "trade_ok":          self.trade_ok,
            "score":             round(self.score, 2),
            "filters_triggered": self.filters_triggered,
            "reasons":           self.reasons,
        }


@dataclass
class TradeCheckInput:
    """集中所有檢查參數（方便擴充）"""
    date:             str            # "YYYY-MM-DD"
    stock_id:         str
    action:           str            # "buy" / "sell"
    close:            float          # 昨日收盤
    open_price:       float          # 今日開盤（可為 close 替代）
    volume:           float          # 今日成交量（張）
    avg_volume_5d:    float          # 近 5 日均量（張）
    atr14:            float          # ATR14
    ensemble_score:   float          # EnsembleEngine 輸出（0~100）
    regime:           str            # "bull"/"bear"/"sideways"/"volatile"/"unknown"
    is_ex_div_day:    bool = False   # 是否為除息/除權日（±1日）
    is_event_day:     bool = False   # 是否為重大事件日
    limit_up:         bool = False   # 是否漲停
    limit_down:       bool = False   # 是否跌停
    current_position: int  = 0       # 目前持有張數（0 = 未持有）
    max_position:     int  = 10      # 單標的最大持倉張數
    typhoon_day:      bool = False   # 是否颱風停市


# ── 不交易引擎主體 ────────────────────────────────────────────────────────────

class NoTradeEngine:
    """
    不交易條件過濾引擎。

    所有過濾層皆為 bool check：
      True  → 此層通過（不觸發禁止）
      False → 此層阻止交易（加入 filters_triggered）

    trade_ok = 所有層皆通過（True）。
    """

    def __init__(
        self,
        gap_up_threshold:   float = GAP_UP_THRESHOLD,
        gap_down_threshold: float = GAP_DOWN_THRESHOLD,
        low_volume_ratio:   float = LOW_VOLUME_RATIO,
        high_vol_ratio:     float = HIGH_VOL_RATIO,
        ensemble_neutral_low:  float = ENSEMBLE_NEUTRAL_LOW,
        ensemble_neutral_high: float = ENSEMBLE_NEUTRAL_HIGH,
    ):
        self.gap_up_threshold    = gap_up_threshold
        self.gap_down_threshold  = gap_down_threshold
        self.low_volume_ratio    = low_volume_ratio
        self.high_vol_ratio      = high_vol_ratio
        self.ensemble_neutral_low  = ensemble_neutral_low
        self.ensemble_neutral_high = ensemble_neutral_high

        # 外部可注入的停市日曆（date str set）
        self._extra_holidays: set[str] = set()
        self._extra_event_days: set[str] = set()

    # ── 公用方法 ──────────────────────────────────────────────────────────

    def add_holidays(self, dates: list[str]) -> None:
        """注入額外停市日（格式 'YYYY-MM-DD'）"""
        self._extra_holidays.update(dates)

    def add_event_days(self, dates: list[str]) -> None:
        """注入重大事件日（格式 'YYYY-MM-DD'）"""
        self._extra_event_days.update(dates)

    def check(self, inp: "TradeCheckInput | None" = None, **kwargs) -> NoTradeDecision:
        """
        執行所有過濾層，回傳 NoTradeDecision。

        可用 TradeCheckInput 物件或關鍵字參數傳入：
            engine.check(inp=inp_obj)
            engine.check(date="2026-04-28", stock_id="2330", action="buy", ...)
        """
        if inp is None:
            inp = TradeCheckInput(**kwargs)

        triggered:  list[str] = []
        reasons:    list[str] = []
        total_filters = 10

        def _fail(layer: str, reason: str) -> None:
            triggered.append(layer)
            reasons.append(reason)
            logger.info(f"[NoTrade] {inp.stock_id} {layer}: {reason}")

        # ── 1. 市場停市 ───────────────────────────────────────────────
        if self._is_market_closed(inp.date, inp.typhoon_day):
            _fail("market_closed", f"{inp.date} 市場停市（假日或颱風）")

        # ── 2. 除息/除權日 ─────────────────────────────────────────────
        if inp.is_ex_div_day:
            _fail("ex_div_day", f"{inp.stock_id} 除息/除權日保護，跳過交易")

        # ── 3. 漲跌停鎖定 ─────────────────────────────────────────────
        if inp.action == "buy" and inp.limit_up:
            _fail("limit_up", f"{inp.stock_id} 已漲停，掛買追高風險過高")
        if inp.action == "sell" and inp.limit_down:
            _fail("limit_down", f"{inp.stock_id} 已跌停，掛賣流動性不足")

        # ── 4. 極端跳空 ───────────────────────────────────────────────
        gap = self._calc_gap(inp.open_price, inp.close)
        if inp.action == "buy" and gap > self.gap_up_threshold:
            _fail("gap_up", f"跳空上漲 {gap*100:.1f}% > {self.gap_up_threshold*100:.0f}%，追高風險")
        if inp.action == "sell" and gap < self.gap_down_threshold:
            _fail("gap_down", f"跳空下跌 {gap*100:.1f}% < {self.gap_down_threshold*100:.0f}%，恐慌賣出")

        # ── 5. 低流動性 ───────────────────────────────────────────────
        if inp.avg_volume_5d > 0:
            vol_ratio = inp.volume / inp.avg_volume_5d
            if vol_ratio < self.low_volume_ratio:
                _fail("low_liquidity",
                      f"成交量 {inp.volume:.0f} 僅 {vol_ratio*100:.0f}% 均量，流動性不足")

        # ── 6. 高波動過濾 ─────────────────────────────────────────────
        if inp.close > 0:
            atr_ratio = inp.atr14 / inp.close
            if atr_ratio > self.high_vol_ratio:
                _fail("high_volatility",
                      f"ATR/收盤={atr_ratio*100:.1f}% > {self.high_vol_ratio*100:.0f}%，波動過高")

        # ── 7. 集成訊號不明確 ─────────────────────────────────────────
        if self.ensemble_neutral_low <= inp.ensemble_score <= self.ensemble_neutral_high:
            _fail("ensemble_neutral",
                  f"集成分={inp.ensemble_score:.1f} 落在中性區間"
                  f"[{self.ensemble_neutral_low},{self.ensemble_neutral_high}]，訊號不明確")

        # ── 8. 盤態禁止 ───────────────────────────────────────────────
        blocked_actions = REGIME_ACTION_BLOCK.get(inp.regime, set())
        if inp.action in blocked_actions:
            _fail("regime_block",
                  f"{inp.regime} 盤態下禁止 {inp.action} 操作")

        # ── 9. 重大事件日 ─────────────────────────────────────────────
        if inp.is_event_day or inp.date in self._extra_event_days:
            _fail("event_day", f"{inp.date} 為重大事件日，降低不確定性")

        # ── 10. 部位上限 ──────────────────────────────────────────────
        if inp.action == "buy" and inp.current_position >= inp.max_position:
            _fail("position_limit",
                  f"{inp.stock_id} 目前持有 {inp.current_position} 張，已達上限 {inp.max_position} 張")

        # ── 彙整結果 ─────────────────────────────────────────────────
        trade_ok = len(triggered) == 0
        score = 1.0 - len(triggered) / total_filters

        if trade_ok:
            logger.debug(f"[NoTrade] {inp.stock_id} {inp.action} 通過所有過濾")

        return NoTradeDecision(
            trade_ok=trade_ok,
            filters_triggered=triggered,
            reasons=reasons,
            score=round(score, 2),
        )

    # ── 批次檢查 ──────────────────────────────────────────────────────────

    def batch_check(self, inputs: list[TradeCheckInput]) -> list[NoTradeDecision]:
        """批次過濾，同時回傳所有結果"""
        return [self.check(inp) for inp in inputs]

    def filter_tradeable(self, inputs: list[TradeCheckInput]) -> list[TradeCheckInput]:
        """過濾出可交易的標的清單"""
        return [inp for inp in inputs if self.check(inp).trade_ok]

    # ── 內部工具 ──────────────────────────────────────────────────────────

    def _is_market_closed(self, date_str: str, typhoon_day: bool) -> bool:
        """判斷是否為停市日"""
        if typhoon_day:
            return True
        if date_str in self._extra_holidays:
            return True
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            # 台股週末停市
            if d.weekday() >= 5:
                return True
            # 固定假日（MM-DD 格式）
            mmdd = d.strftime("%m-%d")
            if mmdd in TW_FIXED_HOLIDAYS:
                return True
        except ValueError:
            pass
        return False

    @staticmethod
    def _calc_gap(open_price: float, prev_close: float) -> float:
        """計算跳空幅度（相對昨收）"""
        if prev_close <= 0:
            return 0.0
        return (open_price - prev_close) / prev_close

    # ── 快速建構器（從 dict 輸入）────────────────────────────────────────

    @classmethod
    def from_market_data(
        cls,
        stock_id: str,
        date: str,
        action: str,
        market_data: dict,
        ensemble_score: float,
        regime: str,
        current_position: int = 0,
        max_position: int = 10,
    ) -> "tuple[NoTradeEngine, TradeCheckInput]":
        """
        從市場資料 dict 快速建構引擎 + 輸入。

        market_data 預期欄位：
          close, open_price, volume, avg_volume_5d, atr14,
          limit_up (bool), limit_down (bool),
          is_ex_div_day (bool), is_event_day (bool)
        """
        engine = cls()
        inp = TradeCheckInput(
            date=date,
            stock_id=stock_id,
            action=action,
            close=float(market_data.get("close", 100)),
            open_price=float(market_data.get("open_price", market_data.get("close", 100))),
            volume=float(market_data.get("volume", 0)),
            avg_volume_5d=float(market_data.get("avg_volume_5d", 1)),
            atr14=float(market_data.get("atr14", 0)),
            ensemble_score=ensemble_score,
            regime=regime,
            is_ex_div_day=bool(market_data.get("is_ex_div_day", False)),
            is_event_day=bool(market_data.get("is_event_day", False)),
            limit_up=bool(market_data.get("limit_up", False)),
            limit_down=bool(market_data.get("limit_down", False)),
            current_position=current_position,
            max_position=max_position,
        )
        return engine, inp


# ── 全域單例 ─────────────────────────────────────────────────────────────────

_global_no_trade: Optional[NoTradeEngine] = None

def get_no_trade_engine() -> NoTradeEngine:
    global _global_no_trade
    if _global_no_trade is None:
        _global_no_trade = NoTradeEngine()
    return _global_no_trade


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    engine = NoTradeEngine()

    print("=== NoTradeEngine 過濾測試 ===\n")

    test_cases = [
        {
            "label": "正常交易日（應通過）",
            "inp": TradeCheckInput(
                date="2026-04-28", stock_id="2330", action="buy",
                close=850.0, open_price=852.0, volume=15000, avg_volume_5d=10000,
                atr14=15.0, ensemble_score=72.0, regime="bull",
            ),
        },
        {
            "label": "漲停不追高（應阻擋）",
            "inp": TradeCheckInput(
                date="2026-04-28", stock_id="2454", action="buy",
                close=1150.0, open_price=1265.0, volume=20000, avg_volume_5d=10000,
                atr14=25.0, ensemble_score=78.0, regime="bull",
                limit_up=True,
            ),
        },
        {
            "label": "空頭盤態做多（應阻擋）",
            "inp": TradeCheckInput(
                date="2026-04-28", stock_id="0050", action="buy",
                close=120.0, open_price=120.5, volume=8000, avg_volume_5d=9000,
                atr14=2.0, ensemble_score=62.0, regime="bear",
            ),
        },
        {
            "label": "訊號中性（應阻擋）",
            "inp": TradeCheckInput(
                date="2026-04-28", stock_id="2317", action="buy",
                close=118.0, open_price=118.5, volume=9000, avg_volume_5d=8500,
                atr14=2.5, ensemble_score=50.0, regime="sideways",
            ),
        },
        {
            "label": "低流動性（應阻擋）",
            "inp": TradeCheckInput(
                date="2026-04-28", stock_id="6666", action="buy",
                close=30.0, open_price=30.1, volume=200, avg_volume_5d=5000,
                atr14=0.5, ensemble_score=68.0, regime="bull",
            ),
        },
        {
            "label": "除息日（應阻擋）",
            "inp": TradeCheckInput(
                date="2026-07-20", stock_id="2330", action="buy",
                close=900.0, open_price=901.0, volume=12000, avg_volume_5d=10000,
                atr14=18.0, ensemble_score=70.0, regime="bull",
                is_ex_div_day=True,
            ),
        },
        {
            "label": "週末停市（應阻擋）",
            "inp": TradeCheckInput(
                date="2026-04-25", stock_id="2330", action="buy",  # 週六
                close=850.0, open_price=850.0, volume=0, avg_volume_5d=10000,
                atr14=15.0, ensemble_score=70.0, regime="bull",
            ),
        },
        {
            "label": "超大跳空（應阻擋）",
            "inp": TradeCheckInput(
                date="2026-04-28", stock_id="2330", action="buy",
                close=850.0, open_price=880.0, volume=15000, avg_volume_5d=10000,
                atr14=15.0, ensemble_score=68.0, regime="bull",
            ),
        },
        {
            "label": "部位上限（應阻擋）",
            "inp": TradeCheckInput(
                date="2026-04-28", stock_id="2330", action="buy",
                close=850.0, open_price=852.0, volume=12000, avg_volume_5d=10000,
                atr14=15.0, ensemble_score=71.0, regime="bull",
                current_position=10, max_position=10,
            ),
        },
    ]

    for tc in test_cases:
        decision = engine.check(inp=tc["inp"])
        status = "通過" if decision.trade_ok else "阻擋"
        print(f"[{status}] {tc['label']}")
        if not decision.trade_ok:
            for r in decision.reasons:
                print(f"       ⛔ {r}")
        print(f"       過濾分數: {decision.score:.2f}\n")
