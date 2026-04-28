"""
portfolio_engine.py — 馬可維茲投資組合最佳化引擎

功能：
  1. 計算期望報酬率與共變異數矩陣
  2. 馬可維茲有效前沿最佳化（scipy SLSQP，失敗自動 Monte Carlo 回退）
  3. 約束條件：
       個股最大權重   MAX_WEIGHT_PER_STOCK（預設 20%）
       單一產業最大   MAX_SECTOR_WEIGHT（預設 40%）
       最小持股數     MIN_STOCKS（預設 3 檔，分散風險）
  4. 三種最佳化目標：
       max_sharpe  — 最大夏普值（預設）
       min_vol     — 最小波動
       max_ret     — 最大報酬（容易過度集中，謹慎使用）
  5. VaR / CVaR、相關矩陣、有效前沿曲線（多點）

使用方式：
    pe = PortfolioEngine()
    result = pe.optimize(price_dict, sectors={"2330":"半導體","2454":"半導體","2412":"電信"})
    # price_dict: {code: pd.Series of close prices}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── 約束常數 ─────────────────────────────────────────────────────────────────

MAX_WEIGHT_PER_STOCK = 0.20   # 個股最大 20%
MAX_SECTOR_WEIGHT    = 0.40   # 單一產業最大 40%
MIN_WEIGHT_PER_STOCK = 0.01   # 個股最小 1%（避免 0 權重無意義持倉）
MIN_STOCKS           = 3      # 最少持股數
TRADING_DAYS_PER_YEAR = 252


# ── 資料類別 ─────────────────────────────────────────────────────────────────

@dataclass
class PortfolioResult:
    """最佳化結果"""
    weights:       dict[str, float]   # {code: weight}
    expected_ret:  float              # 年化期望報酬
    volatility:    float              # 年化波動率
    sharpe:        float              # 夏普值（無風險利率 1.5%）
    var_95:        float              # 95% VaR（日）
    cvar_95:       float              # 95% CVaR（日）
    sector_weights: dict[str, float]  # 產業權重
    method:        str                # "scipy" or "monte_carlo"
    frontier:      list[dict] = field(default_factory=list)  # 有效前沿點
    corr_matrix:   Optional[dict] = None  # 相關矩陣（JSON 格式）
    warnings:      list[str] = field(default_factory=list)


# ── 投資組合引擎 ─────────────────────────────────────────────────────────────

class PortfolioEngine:
    """
    馬可維茲投資組合最佳化引擎。

    使用方式：
        pe = PortfolioEngine(risk_free_rate=0.015)  # 無風險利率 1.5%
        result = pe.optimize(
            price_dict={"2330": series_2330, "2412": series_2412, ...},
            sectors={"2330": "半導體", "2412": "電信"},
            objective="max_sharpe",
        )
    """

    def __init__(
        self,
        risk_free_rate:       float = 0.015,         # 無風險利率（年化）
        max_weight_per_stock: float = MAX_WEIGHT_PER_STOCK,
        max_sector_weight:    float = MAX_SECTOR_WEIGHT,
        min_stocks:           int   = MIN_STOCKS,
        n_monte_carlo:        int   = 5000,
    ):
        self.risk_free_rate       = risk_free_rate
        self.max_weight_per_stock = max_weight_per_stock
        self.max_sector_weight    = max_sector_weight
        self.min_stocks           = min_stocks
        self.n_monte_carlo        = n_monte_carlo

    # ── 主入口 ────────────────────────────────────────────────────────────

    def optimize(
        self,
        price_dict: dict[str, pd.Series],
        sectors:    Optional[dict[str, str]] = None,
        objective:  str = "max_sharpe",     # max_sharpe / min_vol / max_ret
        include_frontier: bool = True,
        include_corr:     bool = True,
    ) -> PortfolioResult:
        """
        執行最佳化。

        price_dict: {stock_code: pd.Series（索引 = 日期，值 = 收盤價）}
        sectors:    {stock_code: 產業名稱}（可選，用於產業約束）
        """
        warnings_list: list[str] = []

        # ── 整理資料 ──────────────────────────────────────────────────────
        if len(price_dict) < self.min_stocks:
            warnings_list.append(f"持股數 {len(price_dict)} < 建議最低 {self.min_stocks}，風險集中")

        codes = list(price_dict.keys())
        prices_df = pd.DataFrame(price_dict).dropna()
        if len(prices_df) < 60:
            raise ValueError(f"價格資料不足（{len(prices_df)} 列），至少需要 60 筆")

        returns = prices_df.pct_change().dropna()
        mean_ret = returns.mean() * TRADING_DAYS_PER_YEAR          # 年化期望報酬
        cov      = returns.cov() * TRADING_DAYS_PER_YEAR           # 年化共變異數
        sectors_list = [sectors.get(c, "其他") for c in codes] if sectors else ["其他"] * len(codes)

        # ── scipy 最佳化 ──────────────────────────────────────────────────
        method = "monte_carlo"
        weights = None
        try:
            weights = self._optimize_scipy(
                mean_ret=mean_ret.values,
                cov=cov.values,
                sectors=sectors_list,
                objective=objective,
            )
            method = "scipy"
        except Exception as e:
            logger.warning(f"[PortfolioEngine] scipy 最佳化失敗（{e}），改用 Monte Carlo")

        if weights is None:
            weights = self._optimize_mc(
                mean_ret=mean_ret.values,
                cov=cov.values,
                sectors=sectors_list,
                objective=objective,
            )

        # ── 計算績效指標 ──────────────────────────────────────────────────
        w = np.array(weights)
        port_ret = float(w @ mean_ret.values)
        port_var = float(w @ cov.values @ w)
        port_vol = float(np.sqrt(port_var))
        sharpe   = (port_ret - self.risk_free_rate) / port_vol if port_vol > 0 else 0.0

        # 日 VaR / CVaR（歷史模擬法）
        port_daily_rets = (returns.values @ w)
        var_95  = float(np.percentile(port_daily_rets, 5))
        cvar_95 = float(port_daily_rets[port_daily_rets <= var_95].mean()) \
                  if (port_daily_rets <= var_95).any() else var_95

        # 產業權重
        sector_weights: dict[str, float] = {}
        for c, w_val, sec in zip(codes, weights, sectors_list):
            sector_weights[sec] = sector_weights.get(sec, 0.0) + w_val

        # 相關矩陣
        corr_dict = None
        if include_corr:
            corr = returns.corr()
            corr_dict = {
                "codes":  codes,
                "matrix": corr.values.round(3).tolist(),
            }

        # 有效前沿（多點）
        frontier_points: list[dict] = []
        if include_frontier:
            frontier_points = self._calc_frontier(mean_ret.values, cov.values, sectors_list, n=20)

        return PortfolioResult(
            weights={c: round(float(w_val), 4) for c, w_val in zip(codes, weights)},
            expected_ret=round(port_ret, 4),
            volatility=round(port_vol, 4),
            sharpe=round(sharpe, 4),
            var_95=round(var_95, 4),
            cvar_95=round(cvar_95, 4),
            sector_weights={k: round(v, 4) for k, v in sector_weights.items()},
            method=method,
            frontier=frontier_points,
            corr_matrix=corr_dict,
            warnings=warnings_list,
        )

    # ── scipy 最佳化（精確解）────────────────────────────────────────────

    def _optimize_scipy(
        self,
        mean_ret: np.ndarray,
        cov:      np.ndarray,
        sectors:  list[str],
        objective: str,
    ) -> list[float]:
        from scipy.optimize import minimize

        n = len(mean_ret)
        bounds = [(MIN_WEIGHT_PER_STOCK, self.max_weight_per_stock)] * n

        # 基礎約束：權重總和 = 1
        constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1}]

        # 產業約束
        for sec in set(sectors):
            idx = [i for i, s in enumerate(sectors) if s == sec]
            if len(idx) > 1:
                constraints.append({
                    "type": "ineq",
                    "fun":  lambda w, idx=idx: self.max_sector_weight - sum(w[i] for i in idx),
                })

        def neg_sharpe(w):
            ret = float(w @ mean_ret)
            vol = float(np.sqrt(w @ cov @ w))
            return -(ret - self.risk_free_rate) / (vol + 1e-9)

        def portfolio_vol(w):
            return float(np.sqrt(w @ cov @ w))

        def neg_ret(w):
            return -float(w @ mean_ret)

        obj_fn = {"max_sharpe": neg_sharpe, "min_vol": portfolio_vol, "max_ret": neg_ret}[objective]

        w0 = np.ones(n) / n
        res = minimize(
            obj_fn, w0,
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options={"maxiter": 1000, "ftol": 1e-9},
        )
        if not res.success:
            raise ValueError(f"scipy 未收斂: {res.message}")
        return res.x.tolist()

    # ── Monte Carlo 回退（近似解）────────────────────────────────────────

    def _optimize_mc(
        self,
        mean_ret: np.ndarray,
        cov:      np.ndarray,
        sectors:  list[str],
        objective: str,
    ) -> list[float]:
        n = len(mean_ret)
        rng = np.random.default_rng(42)
        best_score = -np.inf if objective != "min_vol" else np.inf
        best_w = np.ones(n) / n

        for _ in range(self.n_monte_carlo):
            # 產生隨機權重並強制滿足個股上限
            raw = rng.dirichlet(np.ones(n))
            raw = np.clip(raw, MIN_WEIGHT_PER_STOCK, self.max_weight_per_stock)

            # 產業約束修正（貪心截斷）
            for sec in set(sectors):
                idx = [i for i, s in enumerate(sectors) if s == sec]
                sec_sum = sum(raw[i] for i in idx)
                if sec_sum > self.max_sector_weight:
                    factor = self.max_sector_weight / sec_sum
                    for i in idx:
                        raw[i] *= factor

            raw = raw / raw.sum()  # 歸一化

            ret = float(raw @ mean_ret)
            vol = float(np.sqrt(raw @ cov @ raw)) + 1e-9
            sharpe = (ret - self.risk_free_rate) / vol

            if objective == "max_sharpe":
                score = sharpe
            elif objective == "min_vol":
                score = -vol
            else:
                score = ret

            if (objective == "min_vol" and -score < best_score) or \
               (objective != "min_vol" and score > best_score):
                best_score = score if objective != "min_vol" else -score
                best_w = raw

        return best_w.tolist()

    # ── 有效前沿 ──────────────────────────────────────────────────────────

    def _calc_frontier(
        self,
        mean_ret: np.ndarray,
        cov:      np.ndarray,
        sectors:  list[str],
        n:        int = 20,
    ) -> list[dict]:
        """沿有效前沿取 n 個點（最小波動到最大報酬）"""
        points: list[dict] = []
        n_stocks = len(mean_ret)
        rng = np.random.default_rng(0)

        for _ in range(3000):
            raw = rng.dirichlet(np.ones(n_stocks))
            raw = np.clip(raw, 0, self.max_weight_per_stock)
            raw /= raw.sum()
            ret = float(raw @ mean_ret)
            vol = float(np.sqrt(raw @ cov @ raw))
            points.append({"ret": ret, "vol": vol, "sharpe": (ret - self.risk_free_rate) / (vol + 1e-9)})

        df = pd.DataFrame(points).sort_values("vol")
        # 取前沿部分（每個波動區間取最大報酬）
        df["vol_bin"] = pd.cut(df["vol"], bins=n)
        frontier = (
            df.groupby("vol_bin", observed=False)["ret"].max()
            .reset_index()
            .dropna()
        )
        result = []
        for _, row in frontier.iterrows():
            v_center = row["vol_bin"].mid
            r = row["ret"]
            result.append({
                "vol": round(float(v_center), 4),
                "ret": round(float(r), 4),
                "sharpe": round((r - self.risk_free_rate) / (v_center + 1e-9), 4),
            })
        return result


# ── 便利函式（FastAPI endpoint 直接呼叫）──────────────────────────────────────

async def build_optimal_portfolio(
    holdings: list[dict],
    price_fetcher=None,
    lookback_days: int = 250,
) -> dict:
    """
    給定持股清單，非同步抓取歷史價格後最佳化。

    holdings: [{"code":"2330","sector":"半導體","name":"台積電"}, ...]
    price_fetcher: async func(code, days) -> pd.Series（如未提供則使用 mock）
    """
    if len(holdings) < 2:
        return {"error": "至少需要 2 檔股票"}

    codes   = [h["code"]   for h in holdings]
    sectors = {h["code"]: h.get("sector", "其他") for h in holdings}

    # 取得歷史價格
    price_dict: dict[str, pd.Series] = {}
    for code in codes:
        try:
            if price_fetcher:
                series = await price_fetcher(code, lookback_days)
            else:
                # mock：隨機漫步
                rng = np.random.default_rng(int(code))
                vals = 100 * np.cumprod(1 + rng.normal(0, 0.01, lookback_days))
                series = pd.Series(vals)
            price_dict[code] = series
        except Exception as e:
            logger.warning(f"[PortfolioEngine] 無法取得 {code} 歷史價格: {e}")

    if len(price_dict) < 2:
        return {"error": "有效股票數 < 2，無法最佳化"}

    pe = PortfolioEngine()
    try:
        result = pe.optimize(price_dict, sectors=sectors)
    except ValueError as e:
        return {"error": str(e)}

    from dataclasses import asdict
    return asdict(result)


# ── Mock 資料 + 獨立測試 ────────────────────────────────────────────────────

if __name__ == "__main__":
    import numpy as np

    rng = np.random.default_rng(42)
    n_days = 500

    # 模擬 5 檔股票的歷史收盤價
    mock_prices = {
        "2330": pd.Series(100 * np.cumprod(1 + rng.normal(0.0008, 0.015, n_days))),
        "2412": pd.Series(100 * np.cumprod(1 + rng.normal(0.0003, 0.010, n_days))),
        "2317": pd.Series(100 * np.cumprod(1 + rng.normal(0.0005, 0.012, n_days))),
        "2881": pd.Series(100 * np.cumprod(1 + rng.normal(0.0002, 0.008, n_days))),
        "6505": pd.Series(100 * np.cumprod(1 + rng.normal(0.0004, 0.011, n_days))),
    }
    mock_sectors = {
        "2330": "半導體",
        "2412": "電信",
        "2317": "電子",
        "2881": "金融",
        "6505": "石化",
    }

    pe = PortfolioEngine()
    result = pe.optimize(mock_prices, sectors=mock_sectors, objective="max_sharpe")

    print("=== 馬可維茲最佳化結果 ===")
    print(f"最佳化方法: {result.method}")
    print(f"年化期望報酬: {result.expected_ret*100:.2f}%")
    print(f"年化波動率:   {result.volatility*100:.2f}%")
    print(f"夏普值:       {result.sharpe:.3f}")
    print(f"VaR(95%):    {result.var_95*100:.2f}%  CVaR: {result.cvar_95*100:.2f}%")

    print("\n最佳權重：")
    for code, w in sorted(result.weights.items(), key=lambda x: -x[1]):
        print(f"  {code}: {w*100:.1f}%")

    print("\n產業權重：")
    for sec, w in result.sector_weights.items():
        print(f"  {sec}: {w*100:.1f}%")

    if result.warnings:
        print(f"\n警告: {result.warnings}")

    print(f"\n有效前沿點數: {len(result.frontier)}")
    print(f"相關矩陣: {result.corr_matrix['codes']}")
