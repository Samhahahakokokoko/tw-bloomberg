"""
odd_lot_engine.py — 台股零股投資計算引擎

台灣零股規則（2020年起可盤中交易）：
  - 最小單位：1 股（不限張數）
  - 手續費：max(20, price × shares × 0.001425 × discount)
  - 交易稅：price × shares × 0.003（賣方才有）
  - 撮合：每分鐘一次（盤中零股，09:00~13:30）
  - 漲跌停：與整股相同（±10%）

本引擎提供：
  1. 單股零股計算（shares, fee, break_even, min_profit_pct）
  2. 預算分配（多股零股組合建議）
  3. 定期定額試算（每月固定金額）
  4. 損益試算（輸入目標價，計算獲利）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── 台股成本常數 ──────────────────────────────────────────────────────────────

COMMISSION_RATE = 0.001425   # 手續費基本費率
MIN_COMMISSION  = 20.0       # 最低手續費（元）
TAX_RATE        = 0.003      # 交易稅（賣方，每次）
DEFAULT_DISCOUNT = 0.6       # 網路下單折扣（6折）


# ── 資料類別 ─────────────────────────────────────────────────────────────────

@dataclass
class OddLotResult:
    """單次零股計算結果"""
    stock_id:        str
    name:            str
    price:           float
    shares:          int            # 可買股數
    budget_used:     float          # 實際花費（含手續費）
    budget_left:     float          # 剩餘預算
    buy_fee:         float          # 買入手續費
    fee_rate_pct:    float          # 手續費佔成交金額比例（%）
    break_even_price: float         # 損益兩平價（含買賣雙邊成本）
    min_profit_pct:  float          # 最小獲利幅度（%）
    # 試算
    target_pnl:      Optional[float] = None  # 若有 target_price，預估損益
    target_price:    Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "stock_id":         self.stock_id,
            "name":             self.name,
            "price":            self.price,
            "shares":           self.shares,
            "budget_used":      round(self.budget_used, 0),
            "budget_left":      round(self.budget_left, 0),
            "buy_fee":          round(self.buy_fee, 0),
            "fee_rate_pct":     round(self.fee_rate_pct, 2),
            "break_even_price": round(self.break_even_price, 2),
            "min_profit_pct":   round(self.min_profit_pct, 2),
            "target_pnl":       round(self.target_pnl, 0) if self.target_pnl is not None else None,
            "target_price":     self.target_price,
        }

    def to_line_text(self) -> str:
        lines = [
            f"零股計算：{self.stock_id} {self.name}",
            f"現價：{self.price:.2f}  可買：{self.shares} 股",
            f"花費：{self.budget_used:.0f} 元  餘額：{self.budget_left:.0f} 元",
            f"手續費：{self.buy_fee:.0f} 元（{self.fee_rate_pct:.2f}%）",
            f"損平價：{self.break_even_price:.2f}  最小獲利：{self.min_profit_pct:.2f}%",
        ]
        if self.target_pnl is not None:
            emoji = "+" if self.target_pnl >= 0 else ""
            lines.append(f"目標價 {self.target_price:.0f} 預估損益：{emoji}{self.target_pnl:.0f} 元")
        return "\n".join(lines)


@dataclass
class PortfolioAllocation:
    """預算分配到多檔零股的結果"""
    total_budget:   float
    allocations:    list[dict]    # 每檔的 OddLotResult.to_dict()
    total_cost:     float
    total_fee:      float
    remaining:      float

    def to_line_text(self) -> str:
        lines = [f"零股組合建議（預算 {self.total_budget:.0f} 元）："]
        for a in self.allocations:
            if a["shares"] > 0:
                lines.append(
                    f"  {a['stock_id']} {a['name']}：{a['shares']} 股 "
                    f"花費 {a['budget_used']:.0f} 元"
                )
        lines.append(f"  手續費合計：{self.total_fee:.0f} 元")
        lines.append(f"  剩餘：{self.remaining:.0f} 元")
        return "\n".join(lines)


@dataclass
class DCAResult:
    """定期定額試算結果"""
    stock_id:       str
    name:           str
    monthly_budget: float
    price:          float
    months:         int
    shares_per_month: int
    total_shares:   int
    total_cost:     float       # 含所有手續費
    total_fee:      float
    avg_cost:       float       # 平均成本
    current_value:  float       # 以當前價計算的市值
    unrealized_pnl: float

    def to_dict(self) -> dict:
        return {
            "stock_id":         self.stock_id,
            "name":             self.name,
            "monthly_budget":   self.monthly_budget,
            "months":           self.months,
            "shares_per_month": self.shares_per_month,
            "total_shares":     self.total_shares,
            "total_cost":       round(self.total_cost, 0),
            "total_fee":        round(self.total_fee, 0),
            "avg_cost":         round(self.avg_cost, 2),
            "current_value":    round(self.current_value, 0),
            "unrealized_pnl":   round(self.unrealized_pnl, 0),
            "return_pct":       round(self.unrealized_pnl / self.total_cost * 100, 2)
                                if self.total_cost > 0 else 0,
        }


# ── 零股引擎 ─────────────────────────────────────────────────────────────────

class OddLotEngine:
    """
    台股零股投資計算引擎。

    使用方式：
        engine = OddLotEngine(discount=0.6)

        # 單股計算
        result = engine.calc(budget=5000, price=36.5, stock_id="0056", name="元大高股息")

        # 多股預算分配
        portfolio = engine.allocate(
            budget=20000,
            stocks=[
                {"stock_id": "2330", "name": "台積電",   "price": 850.0, "weight": 0.6},
                {"stock_id": "0056", "name": "元大高股息","price":  36.5, "weight": 0.4},
            ]
        )

        # 定期定額
        dca = engine.dca(monthly=3000, price=36.5, months=12, stock_id="0056", name="元大高股息")
    """

    def __init__(self, discount: float = DEFAULT_DISCOUNT):
        self.discount = discount

    # ── 核心費用計算 ──────────────────────────────────────────────────────

    def buy_fee(self, price: float, shares: int) -> float:
        """買入手續費（最低 20 元）"""
        return max(MIN_COMMISSION, price * shares * COMMISSION_RATE * self.discount)

    def sell_fee(self, price: float, shares: int) -> float:
        """賣出手續費（最低 20 元）"""
        return max(MIN_COMMISSION, price * shares * COMMISSION_RATE * self.discount)

    def sell_tax(self, price: float, shares: int) -> float:
        """交易稅（賣方，固定 0.3%，無最低限制）"""
        return price * shares * TAX_RATE

    def total_buy_cost(self, price: float, shares: int) -> float:
        """買入總成本 = 成交金額 + 手續費"""
        return price * shares + self.buy_fee(price, shares)

    def total_sell_proceeds(self, price: float, shares: int) -> float:
        """賣出淨收入 = 成交金額 - 手續費 - 交易稅"""
        return price * shares - self.sell_fee(price, shares) - self.sell_tax(price, shares)

    def break_even_price(self, buy_price: float, shares: int) -> float:
        """
        損益兩平價：賣出得到的錢剛好等於買入的錢。
        解方程：
          (P_be × shares) - sell_fee(P_be, shares) - sell_tax(P_be, shares)
          = buy_price × shares + buy_fee(buy_price, shares)

        近似解（忽略 sell_fee 的 min=20 效果，精確到小數點後兩位）：
          P_be × shares × (1 - commission × discount - tax) = cost
          P_be = cost / (shares × (1 - commission × discount - tax))
        """
        cost = self.total_buy_cost(buy_price, shares)
        net_factor = 1 - COMMISSION_RATE * self.discount - TAX_RATE
        if shares <= 0 or net_factor <= 0:
            return buy_price
        p_be = cost / (shares * net_factor)
        return round(p_be, 2)

    # ── 單股計算 ──────────────────────────────────────────────────────────

    def calc(
        self,
        budget:       float,
        price:        float,
        stock_id:     str   = "????",
        name:         str   = "",
        target_price: Optional[float] = None,
    ) -> OddLotResult:
        """
        計算指定預算可買幾股零股，並回傳完整成本分析。

        budget      : 可用資金（元）
        price       : 現在股價
        target_price: 若有目標價，額外試算損益
        """
        if price <= 0:
            raise ValueError(f"股價必須 > 0，got {price}")
        if budget <= 0:
            raise ValueError(f"預算必須 > 0，got {budget}")

        # 最多能買幾股（考量手續費）
        # 二分搜尋：找最大 shares 使 total_buy_cost <= budget
        lo, hi = 0, int(budget / price) + 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.total_buy_cost(price, mid) <= budget:
                lo = mid
            else:
                hi = mid - 1
        shares = lo

        if shares <= 0:
            # 即使 1 股也買不起
            return OddLotResult(
                stock_id=stock_id, name=name, price=price,
                shares=0, budget_used=0, budget_left=budget,
                buy_fee=0, fee_rate_pct=0,
                break_even_price=price, min_profit_pct=0,
            )

        cost      = self.total_buy_cost(price, shares)
        b_fee     = self.buy_fee(price, shares)
        b_left    = budget - cost
        gross     = price * shares

        # 手續費佔成交金額比例
        fee_rate  = b_fee / gross * 100 if gross > 0 else 0

        # 損益兩平
        b_even    = self.break_even_price(price, shares)
        min_profit_pct = (b_even - price) / price * 100 if price > 0 else 0

        # 目標損益試算
        target_pnl = None
        if target_price is not None:
            proceeds = self.total_sell_proceeds(float(target_price), shares)
            target_pnl = proceeds - cost

        return OddLotResult(
            stock_id=stock_id, name=name, price=price,
            shares=shares,
            budget_used=round(cost, 2),
            budget_left=round(b_left, 2),
            buy_fee=round(b_fee, 2),
            fee_rate_pct=round(fee_rate, 3),
            break_even_price=b_even,
            min_profit_pct=round(min_profit_pct, 3),
            target_pnl=round(target_pnl, 2) if target_pnl is not None else None,
            target_price=target_price,
        )

    # ── 多股預算分配 ──────────────────────────────────────────────────────

    def allocate(
        self,
        budget: float,
        stocks: list[dict],
        strategy: str = "weight",   # "weight" 依權重分配 / "equal" 均分 / "signal" 依信心
    ) -> PortfolioAllocation:
        """
        依預算分配到多檔零股。

        stocks 每筆：{stock_id, name, price, weight（可選）, confidence（可選）}

        strategy:
          "weight"  - 依 weight 欄位分配
          "equal"   - 均等分配
          "signal"  - 依 confidence 分配（信心越高獲得越多資金）
        """
        if not stocks:
            raise ValueError("stocks 不可為空")

        n = len(stocks)

        # 計算各股分配比例
        if strategy == "weight":
            raw_w = [float(s.get("weight", 1.0 / n)) for s in stocks]
        elif strategy == "signal":
            raw_w = [max(1.0, float(s.get("confidence", 50))) for s in stocks]
        else:  # equal
            raw_w = [1.0] * n

        total_w = sum(raw_w)
        proportions = [w / total_w for w in raw_w]

        allocations = []
        remaining   = budget
        total_fee   = 0.0
        total_cost  = 0.0

        for s, prop in zip(stocks, proportions):
            sub_budget = budget * prop
            if sub_budget < s.get("price", 1):
                # 預算不足買 1 股 → 跳過
                allocations.append(OddLotResult(
                    stock_id=s.get("stock_id","?"), name=s.get("name",""),
                    price=s.get("price", 0), shares=0,
                    budget_used=0, budget_left=sub_budget,
                    buy_fee=0, fee_rate_pct=0,
                    break_even_price=s.get("price", 0),
                    min_profit_pct=0,
                ).to_dict())
                continue

            r = self.calc(
                budget=sub_budget,
                price=float(s.get("price", 1)),
                stock_id=s.get("stock_id", "?"),
                name=s.get("name", ""),
            )
            allocations.append(r.to_dict())
            remaining -= r.budget_used
            total_fee += r.buy_fee
            total_cost += r.budget_used

        return PortfolioAllocation(
            total_budget=budget,
            allocations=allocations,
            total_cost=round(total_cost, 0),
            total_fee=round(total_fee, 0),
            remaining=round(remaining, 0),
        )

    # ── 定期定額試算 ──────────────────────────────────────────────────────

    def dca(
        self,
        monthly:  float,
        price:    float,
        months:   int   = 12,
        stock_id: str   = "????",
        name:     str   = "",
    ) -> DCAResult:
        """
        定期定額試算（假設每月以當前價格買入，忽略價格變動）。
        實際上股價每月不同，此為簡化試算。
        """
        result = self.calc(monthly, price, stock_id, name)
        shares_per_month = result.shares

        total_shares = shares_per_month * months
        single_cost  = result.budget_used
        single_fee   = result.buy_fee
        total_cost   = single_cost * months
        total_fee    = single_fee * months
        avg_cost     = total_cost / total_shares if total_shares > 0 else price
        current_val  = total_shares * price
        unreal_pnl   = current_val - total_cost

        return DCAResult(
            stock_id=stock_id,
            name=name,
            monthly_budget=monthly,
            price=price,
            months=months,
            shares_per_month=shares_per_month,
            total_shares=total_shares,
            total_cost=total_cost,
            total_fee=total_fee,
            avg_cost=avg_cost,
            current_value=current_val,
            unrealized_pnl=unreal_pnl,
        )

    # ── 損益試算 ──────────────────────────────────────────────────────────

    def pnl_table(
        self,
        buy_price:     float,
        shares:        int,
        target_prices: list[float],
    ) -> list[dict]:
        """
        給定買入價與股數，對一組目標價批次試算損益。
        回傳 list，每筆含 target, pnl, pnl_pct。
        """
        cost = self.total_buy_cost(buy_price, shares)
        results = []
        for tp in target_prices:
            proceeds  = self.total_sell_proceeds(tp, shares)
            pnl       = proceeds - cost
            pnl_pct   = pnl / cost * 100 if cost > 0 else 0
            results.append({
                "target_price": tp,
                "pnl":          round(pnl, 0),
                "pnl_pct":      round(pnl_pct, 2),
                "profit": pnl >= 0,
            })
        return results


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    engine = OddLotEngine(discount=0.6)

    print("=== /odd 5000（0056 元大高股息）===")
    r = engine.calc(5000, 36.5, "0056", "元大高股息", target_price=40.0)
    print(r.to_line_text())

    print("\n=== /odd 5000（台積電 850）===")
    r2 = engine.calc(5000, 850.0, "2330", "台積電", target_price=950.0)
    print(r2.to_line_text())

    print("\n=== 預算 20000 分配到 5 檔 ===")
    stocks = [
        {"stock_id":"2330","name":"台積電",    "price":850.0, "weight":0.35},
        {"stock_id":"0056","name":"元大高股息","price":36.5,  "weight":0.25},
        {"stock_id":"2454","name":"聯發科",    "price":1150.0,"weight":0.20},
        {"stock_id":"2412","name":"中華電",    "price":118.0, "weight":0.10},
        {"stock_id":"2317","name":"鴻海",      "price":118.5, "weight":0.10},
    ]
    portfolio = engine.allocate(20000, stocks, strategy="weight")
    print(portfolio.to_line_text())

    print("\n=== 定期定額：0056，每月 3000，12 個月 ===")
    dca = engine.dca(3000, 36.5, 12, "0056", "元大高股息")
    d = dca.to_dict()
    print(f"  每月買：{d['shares_per_month']} 股")
    print(f"  12 月後：{d['total_shares']} 股")
    print(f"  總成本：{d['total_cost']:.0f}（含手續費 {d['total_fee']:.0f}）")
    print(f"  平均成本：{d['avg_cost']:.2f}  當前市值：{d['current_value']:.0f}")

    print("\n=== 損益試算（2330 買 3 股@850）===")
    table = engine.pnl_table(850.0, 3, [880, 900, 950, 1000])
    for t in table:
        mark = "+" if t["profit"] else ""
        print(f"  目標 {t['target_price']:.0f} -> 損益 {mark}{t['pnl']:.0f}（{mark}{t['pnl_pct']:.2f}%）")

    print("\n=== 手續費效應分析 ===")
    for budget in [500, 1000, 5000, 10000, 50000]:
        r = engine.calc(budget, 36.5, "0056", "")
        if r.shares > 0:
            print(f"  預算 {budget:6.0f} -> {r.shares:3d} 股  "
                  f"手續費 {r.buy_fee:.0f}（{r.fee_rate_pct:.2f}%）  "
                  f"損平 {r.break_even_price:.2f}")
