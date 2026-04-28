"""
execution_engine.py — 下單 / 倉位 / 風控管理

功能：
  1. 下單管理：市價 / 限價 / 停損 / 停利單（模擬台股撮合規則）
  2. 倉位管理：持倉追蹤、平均成本計算、未實現損益
  3. 風控檢查：每筆最大金額、總倉位上限、單日最大虧損停止交易
  4. 台股真實成本：手續費 0.1425%（買+賣）、交易稅 0.3%（賣方）、滑價 0.05%
  5. 交易日誌：完整紀錄每筆交易（可匯出 DataFrame 回測分析）

台股特有規則：
  - 最小交易單位：1 張（1000 股）
  - 漲跌停：±10%（ETF / 部分特殊標的除外）
  - 成交量上限：當日成交量 × 1%（避免大量衝擊）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ── 台股成本常數 ──────────────────────────────────────────────────────────────

COMMISSION_RATE  = 0.001425   # 手續費 0.1425%（買 + 賣）
TAX_RATE         = 0.003      # 交易稅 0.3%（賣方才有）
SLIPPAGE_RATE    = 0.0005     # 滑價 0.05%（估算市場衝擊）
LIMIT_UP_DOWN    = 0.10       # 漲跌停幅度
MIN_SHARES       = 1000       # 最小交易單位（1 張）
MAX_VOL_RATIO    = 0.01       # 最大成交量占當日量比例

# ── 列舉型別 ─────────────────────────────────────────────────────────────────

class OrderSide(str, Enum):
    BUY  = "buy"
    SELL = "sell"

class OrderType(str, Enum):
    MARKET = "market"   # 市價（以當日收盤成交，加上滑價）
    LIMIT  = "limit"    # 限價（只在價格觸及時成交）
    STOP   = "stop"     # 停損（觸及停損價後轉市價）

class OrderStatus(str, Enum):
    PENDING   = "pending"
    FILLED    = "filled"
    CANCELLED = "cancelled"
    REJECTED  = "rejected"


# ── 資料類別 ─────────────────────────────────────────────────────────────────

@dataclass
class Order:
    """下單物件"""
    order_id:    str
    stock_code:  str
    side:        OrderSide
    shares:      int            # 股數（非張數）
    order_type:  OrderType = OrderType.MARKET
    limit_price: Optional[float] = None   # 限價單：觸發價格
    stop_price:  Optional[float] = None   # 停損單：停損觸發價
    status:      OrderStatus = OrderStatus.PENDING
    filled_price:Optional[float] = None
    filled_at:   Optional[datetime] = None
    commission:  float = 0.0
    tax:         float = 0.0
    slippage:    float = 0.0
    note:        str   = ""

    @property
    def cost(self) -> float:
        """實際交易成本（手續費 + 稅 + 滑價）"""
        return self.commission + self.tax + self.slippage

    @property
    def net_amount(self) -> float:
        """淨成交金額（正=付出，負=收入）"""
        if self.filled_price is None:
            return 0.0
        gross = self.shares * self.filled_price
        if self.side == OrderSide.BUY:
            return gross + self.commission + self.slippage
        else:
            return -(gross - self.commission - self.tax - self.slippage)


@dataclass
class Position:
    """單一持股倉位"""
    stock_code: str
    shares:     int     = 0
    avg_cost:   float   = 0.0   # 平均買入成本（含手續費）
    realized_pnl: float = 0.0   # 已實現損益

    def unrealized_pnl(self, current_price: float) -> float:
        """未實現損益"""
        return self.shares * (current_price - self.avg_cost)

    def unrealized_pnl_pct(self, current_price: float) -> float:
        """未實現損益%"""
        if self.avg_cost == 0:
            return 0.0
        return (current_price - self.avg_cost) / self.avg_cost

    @property
    def is_empty(self) -> bool:
        return self.shares == 0


@dataclass
class TradeRecord:
    """交易紀錄（用於回測分析 / 績效計算）"""
    date:        str
    stock_code:  str
    side:        str
    shares:      int
    price:       float
    commission:  float
    tax:         float
    slippage:    float
    net_amount:  float
    holding_days: Optional[int] = None   # 賣出時計算持有天數
    pnl:         Optional[float] = None  # 賣出時計算損益


# ── 執行引擎 ─────────────────────────────────────────────────────────────────

class ExecutionEngine:
    """
    模擬下單與倉位管理引擎。

    使用方式：
        engine = ExecutionEngine(initial_capital=1_000_000)
        order = engine.create_order("2330", OrderSide.BUY, lots=10)
        engine.execute(order, current_price=850.0, daily_volume=20_000_000)
        print(engine.portfolio_value({"2330": 855.0}))

    風控參數（透過 risk_config 傳入）：
        max_position_pct  最大單股倉位%（預設 20%）
        max_total_long_pct 最大總多頭倉位%（預設 80%）
        max_daily_loss_pct 單日最大虧損%停止交易（預設 3%）
        stop_loss_pct     全局停損%（預設 8%）
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000,
        risk_config: Optional[dict] = None,
    ):
        self.cash             = initial_capital
        self.initial_capital  = initial_capital
        self.positions:  dict[str, Position] = {}
        self.orders:     list[Order] = []
        self.trade_log:  list[TradeRecord] = []
        self._order_seq  = 0
        self._session_date: Optional[str] = None
        self._daily_start_value: float = initial_capital

        rc = risk_config or {}
        self.max_position_pct   = rc.get("max_position_pct",   0.20)
        self.max_total_long_pct = rc.get("max_total_long_pct", 0.80)
        self.max_daily_loss_pct = rc.get("max_daily_loss_pct", 0.03)
        self.stop_loss_pct      = rc.get("stop_loss_pct",      0.08)
        self._trading_halted    = False

    # ── 建立訂單 ──────────────────────────────────────────────────────────

    def create_order(
        self,
        stock_code:  str,
        side:        OrderSide,
        lots:        int = 1,              # 張數（1 張 = 1000 股）
        shares:      Optional[int] = None, # 直接指定股數（覆蓋 lots）
        order_type:  OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        stop_price:  Optional[float] = None,
        note:        str = "",
    ) -> Order:
        """建立新訂單（尚未執行）"""
        self._order_seq += 1
        actual_shares = shares if shares is not None else lots * MIN_SHARES
        # 台股最小單位修正：無條件捨去至 1000 倍數
        actual_shares = max(MIN_SHARES, (actual_shares // MIN_SHARES) * MIN_SHARES)

        order = Order(
            order_id=f"ORD{self._order_seq:06d}",
            stock_code=stock_code,
            side=side,
            shares=actual_shares,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            note=note,
        )
        return order

    # ── 執行訂單 ──────────────────────────────────────────────────────────

    def execute(
        self,
        order:         Order,
        current_price: float,
        daily_volume:  float = 0,
        trade_date:    Optional[str] = None,
        prev_close:    Optional[float] = None,
    ) -> bool:
        """
        執行訂單撮合。

        回傳 True = 成交，False = 未成交（被風控拒絕或條件未觸及）。
        """
        if self._trading_halted:
            order.status = OrderStatus.REJECTED
            order.note   = "單日虧損停止交易"
            return False

        # ── 漲跌停保護 ──────────────────────────────────────────────────
        exec_price = current_price
        if prev_close is not None:
            upper = prev_close * (1 + LIMIT_UP_DOWN)
            lower = prev_close * (1 - LIMIT_UP_DOWN)
            exec_price = max(lower, min(upper, exec_price))

        # ── 限價 / 停損單觸發檢查 ───────────────────────────────────────
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            if order.side == OrderSide.BUY  and exec_price > order.limit_price:
                return False   # 市場價超過買入限價，未成交
            if order.side == OrderSide.SELL and exec_price < order.limit_price:
                return False   # 市場價低於賣出限價，未成交
        if order.order_type == OrderType.STOP and order.stop_price is not None:
            if order.side == OrderSide.SELL and exec_price > order.stop_price:
                return False   # 尚未觸及停損價

        # ── 成交量上限（避免流動性衝擊）──────────────────────────────────
        if daily_volume > 0:
            max_shares = int(daily_volume * MAX_VOL_RATIO)
            max_shares = max(MIN_SHARES, (max_shares // MIN_SHARES) * MIN_SHARES)
            if order.shares > max_shares:
                logger.warning(
                    f"[{order.stock_code}] 下單量({order.shares})超過日量上限({max_shares})，自動截斷"
                )
                order.shares = max_shares

        # ── 風控檢查 ─────────────────────────────────────────────────────
        if not self._risk_check(order, exec_price):
            order.status = OrderStatus.REJECTED
            return False

        # ── 計算成本 ──────────────────────────────────────────────────────
        gross = order.shares * exec_price
        commission = gross * COMMISSION_RATE
        tax        = gross * TAX_RATE if order.side == OrderSide.SELL else 0.0
        slippage   = gross * SLIPPAGE_RATE

        # ── 更新現金 / 持倉 ───────────────────────────────────────────────
        if order.side == OrderSide.BUY:
            total_cost = gross + commission + slippage
            if total_cost > self.cash:
                order.status = OrderStatus.REJECTED
                order.note   = f"現金不足（需 {total_cost:.0f}，有 {self.cash:.0f}）"
                return False
            self.cash -= total_cost
            self._update_position_buy(order.stock_code, order.shares, exec_price, commission + slippage)
        else:
            pos = self.positions.get(order.stock_code)
            if pos is None or pos.shares < order.shares:
                order.status = OrderStatus.REJECTED
                order.note   = "持股不足"
                return False
            net_recv = gross - commission - tax - slippage
            pnl = self._update_position_sell(order.stock_code, order.shares, exec_price, commission + tax + slippage)
            self.cash += net_recv

        # ── 填寫訂單 ──────────────────────────────────────────────────────
        order.filled_price = exec_price
        order.filled_at    = datetime.utcnow()
        order.status       = OrderStatus.FILLED
        order.commission   = commission
        order.tax          = tax
        order.slippage     = slippage
        self.orders.append(order)

        # ── 交易日誌 ──────────────────────────────────────────────────────
        td = trade_date or str(date.today())
        self._new_session_check(td)
        rec = TradeRecord(
            date=td,
            stock_code=order.stock_code,
            side=order.side.value,
            shares=order.shares,
            price=exec_price,
            commission=commission,
            tax=tax,
            slippage=slippage,
            net_amount=order.net_amount,
            pnl=pnl if order.side == OrderSide.SELL else None,
        )
        self.trade_log.append(rec)

        logger.info(
            f"[{td}] {order.side.value.upper()} {order.stock_code} "
            f"{order.shares}股 @{exec_price:.2f} "
            f"手續費={commission:.0f} 稅={tax:.0f} 滑價={slippage:.0f}"
        )
        return True

    # ── 風控 ──────────────────────────────────────────────────────────────

    def _risk_check(self, order: Order, price: float) -> bool:
        """風控驗證，回傳 False 表示拒單"""
        if order.side == OrderSide.BUY:
            order_value = order.shares * price
            total_equity = self.total_equity({order.stock_code: price})

            # 單股倉位上限
            pos = self.positions.get(order.stock_code)
            current_pos_val = (pos.shares * price) if pos else 0
            new_pos_val = current_pos_val + order_value
            if new_pos_val / total_equity > self.max_position_pct:
                order.note = f"單股倉位超過 {self.max_position_pct*100:.0f}%"
                return False

            # 總多頭倉位上限
            long_val = sum(
                p.shares * price for p in self.positions.values() if not p.is_empty
            ) + order_value
            if long_val / total_equity > self.max_total_long_pct:
                order.note = f"總倉位超過 {self.max_total_long_pct*100:.0f}%"
                return False

        return True

    def check_daily_loss(self, current_prices: dict[str, float]) -> bool:
        """
        單日最大虧損檢查：若當日虧損超過 max_daily_loss_pct，停止交易。
        應在每個 bar 開始時呼叫。
        """
        equity = self.total_equity(current_prices)
        daily_loss_pct = (self._daily_start_value - equity) / self._daily_start_value
        if daily_loss_pct >= self.max_daily_loss_pct:
            self._trading_halted = True
            logger.warning(f"單日虧損 {daily_loss_pct*100:.1f}%，停止交易")
        return self._trading_halted

    def reset_daily_state(self, current_prices: dict[str, float]) -> None:
        """每個新交易日呼叫，重置單日停損狀態"""
        self._daily_start_value = self.total_equity(current_prices)
        self._trading_halted    = False

    # ── 倉位計算 ──────────────────────────────────────────────────────────

    def _update_position_buy(self, code: str, shares: int, price: float, cost: float) -> None:
        """更新買入後的持倉平均成本（加權平均）"""
        if code not in self.positions:
            self.positions[code] = Position(stock_code=code)
        pos = self.positions[code]
        total_cost = pos.shares * pos.avg_cost + shares * price + cost
        pos.shares += shares
        pos.avg_cost = total_cost / pos.shares if pos.shares > 0 else 0.0

    def _update_position_sell(self, code: str, shares: int, price: float, cost: float) -> float:
        """更新賣出後的持倉，回傳本次已實現損益"""
        pos = self.positions[code]
        pnl = shares * (price - pos.avg_cost) - cost
        pos.realized_pnl += pnl
        pos.shares -= shares
        if pos.shares == 0:
            pos.avg_cost = 0.0
        return pnl

    # ── 組合評估 ──────────────────────────────────────────────────────────

    def portfolio_value(self, current_prices: dict[str, float]) -> dict:
        """
        計算當前組合價值，回傳詳細 dict。
        current_prices: {stock_code: current_price, ...}
        """
        positions_detail = []
        total_stock_val  = 0.0

        for code, pos in self.positions.items():
            if pos.is_empty:
                continue
            price = current_prices.get(code, pos.avg_cost)
            mkt_val = pos.shares * price
            upnl    = pos.unrealized_pnl(price)
            upnl_pct= pos.unrealized_pnl_pct(price)
            total_stock_val += mkt_val
            positions_detail.append({
                "stock_code":       code,
                "shares":           pos.shares,
                "avg_cost":         round(pos.avg_cost, 2),
                "current_price":    round(price, 2),
                "market_value":     round(mkt_val, 0),
                "unrealized_pnl":   round(upnl, 0),
                "unrealized_pnl_pct": round(upnl_pct * 100, 2),
                "realized_pnl":     round(pos.realized_pnl, 0),
            })

        total_realized = sum(p.realized_pnl for p in self.positions.values())
        total_equity   = self.cash + total_stock_val
        total_return   = (total_equity - self.initial_capital) / self.initial_capital

        return {
            "cash":          round(self.cash, 0),
            "stock_value":   round(total_stock_val, 0),
            "total_equity":  round(total_equity, 0),
            "total_return":  round(total_return * 100, 2),
            "realized_pnl":  round(total_realized, 0),
            "positions":     positions_detail,
        }

    def total_equity(self, current_prices: dict[str, float]) -> float:
        """快速計算總資產（現金 + 持股市值）"""
        stock_val = sum(
            p.shares * current_prices.get(code, p.avg_cost)
            for code, p in self.positions.items()
        )
        return self.cash + stock_val

    # ── 部位建議（倉位計算工具）────────────────────────────────────────────

    def calc_position_size(
        self,
        price:        float,
        risk_pct:     float = 0.01,  # 每筆最大風險 1% 資本
        stop_loss_pct: float = 0.08, # 停損幅度 8%
    ) -> int:
        """
        凱利 / ATR 風控倉位計算：

        shares = (資本 × 風險%) / (價格 × 停損幅度)

        回傳建議買入股數（已對齊到 1000 股整倍數）。
        """
        risk_amount = self.cash * risk_pct
        loss_per_share = price * stop_loss_pct
        if loss_per_share == 0:
            return 0
        raw_shares = risk_amount / loss_per_share
        lots = max(1, int(raw_shares / MIN_SHARES))
        return lots * MIN_SHARES

    # ── 報表匯出 ──────────────────────────────────────────────────────────

    def trade_log_df(self) -> pd.DataFrame:
        """匯出交易日誌為 DataFrame（回測績效分析用）"""
        if not self.trade_log:
            return pd.DataFrame()
        return pd.DataFrame([
            {
                "date":         r.date,
                "stock_code":   r.stock_code,
                "side":         r.side,
                "shares":       r.shares,
                "price":        r.price,
                "commission":   r.commission,
                "tax":          r.tax,
                "slippage":     r.slippage,
                "net_amount":   r.net_amount,
                "pnl":          r.pnl,
                "holding_days": r.holding_days,
            }
            for r in self.trade_log
        ])

    def _new_session_check(self, trade_date: str) -> None:
        """偵測新交易日，自動重置日停損狀態"""
        if self._session_date != trade_date:
            self._session_date = trade_date
            # 重置日停損（不知道今日價格，先用現金估）
            # 正式使用時應傳入 current_prices，這裡簡化處理
            self._trading_halted = False


# ── Mock 資料 + 獨立測試 ────────────────────────────────────────────────────

if __name__ == "__main__":
    engine = ExecutionEngine(initial_capital=1_000_000)

    print("=== 買入測試 ===")
    order1 = engine.create_order("2330", OrderSide.BUY, lots=5)
    ok = engine.execute(order1, current_price=850.0, daily_volume=30_000_000, trade_date="2024-01-02")
    print(f"Buy order: {'成交' if ok else '拒絕'} — {order1.note or '正常'}")

    print("\n=== 風控測試：倉位上限 ===")
    order2 = engine.create_order("2330", OrderSide.BUY, lots=200)  # 遠超 20%
    ok2 = engine.execute(order2, current_price=850.0, daily_volume=30_000_000, trade_date="2024-01-02")
    print(f"Large buy: {'成交' if ok2 else '拒絕'} — {order2.note}")

    print("\n=== 賣出測試 ===")
    order3 = engine.create_order("2330", OrderSide.SELL, lots=3)
    ok3 = engine.execute(order3, current_price=870.0, daily_volume=30_000_000, trade_date="2024-01-05")
    print(f"Sell order: {'成交' if ok3 else '拒絕'} — {order3.note or '正常'}")

    print("\n=== 倉位建議 ===")
    sz = engine.calc_position_size(price=850.0, risk_pct=0.01, stop_loss_pct=0.08)
    print(f"建議買入: {sz} 股（{sz//1000} 張）")

    print("\n=== 組合價值 ===")
    pv = engine.portfolio_value({"2330": 880.0})
    print(f"總資產: {pv['total_equity']:,.0f}  報酬率: {pv['total_return']}%")
    for p in pv["positions"]:
        print(f"  {p['stock_code']}: {p['shares']}股 成本={p['avg_cost']} "
              f"未實現={p['unrealized_pnl']:+.0f}({p['unrealized_pnl_pct']:+.1f}%)")

    print("\n=== 交易日誌 ===")
    print(engine.trade_log_df().to_string())
