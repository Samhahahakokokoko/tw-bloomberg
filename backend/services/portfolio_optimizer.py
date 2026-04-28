"""馬可維茲投資組合最佳化 + VaR 風險值計算

功能：
  1. 效率前緣（Efficient Frontier）— scipy.optimize.minimize
  2. 最佳 Sharpe 比率組合
  3. VaR 風險值（歷史模擬法 + 參數法）
  4. 相關性矩陣 + 過度集中警示
  5. 現有組合最佳化建議

資料來源：FinMind 調整後股價（還原除權息）
無歷史資料時 fallback 使用 TWSE K線
"""
from __future__ import annotations
import asyncio
import numpy as np
from datetime import date, timedelta
from loguru import logger

from ..models.database import AsyncSessionLocal
from ..models.models import Portfolio
from .finmind_service import fetch_adj_price
from .twse_service import fetch_kline


# ── 資料抓取 ─────────────────────────────────────────────────────────────────

async def _get_returns(stock_code: str, days: int = 252) -> np.ndarray | None:
    """取得歷史日報酬率 (shape: n_days,)"""
    start = (date.today() - timedelta(days=days + 30)).strftime("%Y-%m-%d")

    # 先試 FinMind
    try:
        prices = await fetch_adj_price(stock_code, start, days + 30)
        if len(prices) >= 20:
            closes = np.array([p["close"] for p in prices[-days:] if p.get("close")], dtype=float)
            if len(closes) >= 20:
                return np.diff(np.log(closes))  # log returns
    except Exception as e:
        logger.warning(f"[Optimizer] FinMind fallback for {stock_code}: {e}")

    # fallback: TWSE K線
    try:
        kline = await fetch_kline(stock_code)
        closes = np.array([float(k["close"]) for k in kline if k.get("close")], dtype=float)
        if len(closes) >= 20:
            return np.diff(np.log(closes))
    except Exception as e:
        logger.error(f"[Optimizer] kline error {stock_code}: {e}")

    return None


async def get_returns_matrix(holdings: list[dict], days: int = 252) -> tuple[np.ndarray, list[str], list[str]]:
    """
    回傳 (returns_matrix, codes, names)
    returns_matrix shape: (n_days, n_assets)
    """
    tasks = {h["stock_code"]: asyncio.create_task(_get_returns(h["stock_code"], days))
             for h in holdings}

    codes, names, ret_list = [], [], []
    for h in holdings:
        code = h["stock_code"]
        try:
            ret = await tasks[code]
            if ret is not None and len(ret) >= 20:
                codes.append(code)
                names.append(h.get("stock_name", code))
                ret_list.append(ret)
        except Exception as e:
            logger.error(f"[Optimizer] returns error {code}: {e}")

    if not ret_list:
        return np.array([]), [], []

    # 對齊長度（取最短）
    min_len = min(len(r) for r in ret_list)
    matrix  = np.column_stack([r[-min_len:] for r in ret_list])
    return matrix, codes, names


# ── 組合指標計算 ──────────────────────────────────────────────────────────────

def portfolio_performance(weights: np.ndarray, mean_returns: np.ndarray,
                          cov_matrix: np.ndarray, trading_days: int = 252) -> tuple[float, float, float]:
    """
    回傳 (annualized_return, annualized_vol, sharpe_ratio)
    無風險利率假設 1.5%（台灣定存利率）
    """
    ret  = float(np.sum(mean_returns * weights) * trading_days)
    vol  = float(np.sqrt(np.dot(weights.T, np.dot(cov_matrix * trading_days, weights))))
    sharpe = (ret - 0.015) / vol if vol > 0 else 0.0
    return round(ret * 100, 2), round(vol * 100, 2), round(sharpe, 3)


# ── 效率前緣 ──────────────────────────────────────────────────────────────────

def calc_efficient_frontier(returns_matrix: np.ndarray, n_points: int = 60) -> list[dict]:
    """
    蒙地卡羅模擬法：產生大量隨機組合，識別效率前緣。
    不需要 scipy 也可以運作，結果近似最優解。
    """
    if returns_matrix.size == 0:
        return []

    n_assets = returns_matrix.shape[1]
    mean_ret  = returns_matrix.mean(axis=0)
    cov       = np.cov(returns_matrix.T)
    if n_assets == 1:
        cov = np.array([[cov]])

    # 如果有 scipy 就用精確解，否則用 Monte Carlo
    try:
        return _efficient_frontier_exact(mean_ret, cov, n_points)
    except Exception:
        return _efficient_frontier_mc(mean_ret, cov, n_points)


def _efficient_frontier_mc(mean_ret: np.ndarray, cov: np.ndarray,
                            n_sim: int = 3000) -> list[dict]:
    """Monte Carlo 模擬版（fallback）"""
    n_assets = len(mean_ret)
    results  = []
    for _ in range(n_sim):
        w = np.random.dirichlet(np.ones(n_assets))
        ret, vol, sharpe = portfolio_performance(w, mean_ret, cov)
        results.append({"return": ret, "volatility": vol, "sharpe": sharpe,
                         "weights": w.tolist()})

    # 取效率前緣（同等 volatility 下最高 return）
    results.sort(key=lambda x: x["volatility"])
    frontier = []
    max_ret  = -999.0
    for r in results:
        if r["return"] > max_ret:
            max_ret = r["return"]
            frontier.append(r)

    return frontier


def _efficient_frontier_exact(mean_ret: np.ndarray, cov: np.ndarray,
                               n_points: int = 60) -> list[dict]:
    """scipy 精確解法"""
    from scipy.optimize import minimize

    n = len(mean_ret)
    bounds = tuple((0.0, 1.0) for _ in range(n))
    w0     = np.ones(n) / n

    # 最大/最小可行報酬率
    def neg_return(w):
        return -float(np.sum(w * mean_ret) * 252 * 100)

    def portfolio_vol(w):
        return float(np.sqrt(np.dot(w.T, np.dot(cov * 252, w))) * 100)

    constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1}]
    max_res = minimize(neg_return, w0, method="SLSQP", bounds=bounds, constraints=constraints)
    min_res = minimize(portfolio_vol, w0, method="SLSQP", bounds=bounds, constraints=constraints)

    if not (max_res.success and min_res.success):
        return _efficient_frontier_mc(mean_ret, cov, 3000)

    r_max = -neg_return(max_res.x)
    r_min = float(np.sum(min_res.x * mean_ret) * 252 * 100)

    frontier = []
    for target_ret in np.linspace(r_min, r_max, n_points):
        cons = [
            {"type": "eq", "fun": lambda w: np.sum(w) - 1},
            {"type": "eq", "fun": lambda w, t=target_ret: np.sum(w * mean_ret) * 252 * 100 - t},
        ]
        res = minimize(portfolio_vol, w0, method="SLSQP", bounds=bounds, constraints=cons)
        if res.success:
            w  = res.x
            r, v, sh = portfolio_performance(w, mean_ret, cov)
            frontier.append({
                "return":     r,
                "volatility": v,
                "sharpe":     sh,
                "weights":    [round(float(wi), 4) for wi in w],
            })

    return frontier


# ── 最佳化建議 ────────────────────────────────────────────────────────────────

def find_optimal_weights(frontier: list[dict]) -> dict:
    """在效率前緣上找最大 Sharpe 比率的最佳組合"""
    if not frontier:
        return {}
    best = max(frontier, key=lambda x: x["sharpe"])
    return best


def find_min_variance_weights(frontier: list[dict]) -> dict:
    """找最低波動率組合"""
    if not frontier:
        return {}
    return min(frontier, key=lambda x: x["volatility"])


# ── VaR 風險值 ────────────────────────────────────────────────────────────────

def calc_var(returns_matrix: np.ndarray, weights: np.ndarray,
             confidence: float = 0.95, investment: float = 1_000_000.0) -> dict:
    """
    計算投資組合 VaR（每日最大可能虧損）

    Historical Simulation：
      排列歷史組合報酬，取第 (1-confidence) 百分位
    Parametric（Normal）：
      VaR = μ - z_α * σ，z_{0.95} ≈ 1.645

    investment: 投資總額（元），預設 100 萬
    """
    if returns_matrix.size == 0 or len(weights) == 0:
        return {}

    n = min(len(weights), returns_matrix.shape[1])
    w = np.array(weights[:n])
    w = w / w.sum()  # 正規化

    # 日組合報酬率
    port_returns = returns_matrix[:, :n] @ w

    # Historical VaR
    hist_var_pct    = float(np.percentile(port_returns, (1 - confidence) * 100))
    hist_var_amount = abs(hist_var_pct) * investment

    # Parametric VaR
    mu  = float(port_returns.mean())
    sig = float(port_returns.std())
    from scipy.stats import norm
    z_alpha        = norm.ppf(1 - confidence)
    param_var_pct  = float(mu + z_alpha * sig)
    param_var_amount = abs(param_var_pct) * investment

    # CVaR（Expected Shortfall）= 超過 VaR 的平均損失
    tail     = port_returns[port_returns <= hist_var_pct]
    cvar_pct = float(tail.mean()) if len(tail) > 0 else hist_var_pct
    cvar_amt = abs(cvar_pct) * investment

    return {
        "confidence":          confidence,
        "investment":          investment,
        "hist_var_pct":        round(hist_var_pct * 100, 3),
        "hist_var_amount":     round(hist_var_amount, 0),
        "param_var_pct":       round(param_var_pct * 100, 3),
        "param_var_amount":    round(param_var_amount, 0),
        "cvar_pct":            round(cvar_pct * 100, 3),
        "cvar_amount":         round(cvar_amt, 0),
        "worst_day_pct":       round(float(port_returns.min()) * 100, 2),
        "best_day_pct":        round(float(port_returns.max()) * 100, 2),
        "avg_daily_return_pct":round(float(port_returns.mean()) * 100, 3),
        "daily_vol_pct":       round(float(port_returns.std()) * 100, 3),
    }


# ── 相關性矩陣 ────────────────────────────────────────────────────────────────

def calc_correlation_matrix(returns_matrix: np.ndarray,
                             codes: list[str], names: list[str]) -> dict:
    """
    計算相關係數矩陣，並標記高相關 (>0.8) 的股票對。
    """
    if returns_matrix.size == 0:
        return {}

    corr = np.corrcoef(returns_matrix.T)

    matrix_data = []
    for i, ci in enumerate(codes):
        for j, cj in enumerate(codes):
            matrix_data.append({
                "stock_x": ci,
                "name_x":  names[i],
                "stock_y": cj,
                "name_y":  names[j],
                "corr":    round(float(corr[i, j]), 3),
            })

    # 高相關警示
    warnings = [
        f"{codes[i]}({names[i]}) & {codes[j]}({names[j]}): {corr[i,j]:.2f}"
        for i in range(len(codes))
        for j in range(i + 1, len(codes))
        if corr[i, j] > 0.8
    ]

    # 為前端熱力圖準備的二維陣列
    grid = [[round(float(corr[i, j]), 3) for j in range(len(codes))]
            for i in range(len(codes))]

    return {
        "codes":      codes,
        "names":      names,
        "matrix":     grid,
        "data":       matrix_data,
        "warnings":   warnings,
        "high_corr_count": len(warnings),
    }


# ── 主入口：完整投組分析 ──────────────────────────────────────────────────────

async def full_portfolio_analysis(user_id: str = "") -> dict:
    """
    對使用者現有庫存執行完整馬可維茲分析。
    回傳效率前緣、最佳組合、VaR、相關性矩陣。
    """
    # 取庫存
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        q = select(Portfolio)
        if user_id:
            q = q.where(Portfolio.user_id == user_id)
        r = await db.execute(q)
        holdings_raw = r.scalars().all()

    if not holdings_raw:
        return {"error": "庫存為空，請先新增持股"}

    holdings = [
        {"stock_code": h.stock_code, "stock_name": h.stock_name or h.stock_code,
         "shares": h.shares, "cost_price": h.cost_price}
        for h in holdings_raw
    ]

    if len(holdings) < 2:
        return {"error": "需要至少 2 檔股票才能進行最佳化分析"}

    # 抓歷史報酬
    returns_matrix, codes, names = await get_returns_matrix(holdings, days=252)

    if returns_matrix.size == 0 or len(codes) < 2:
        return {"error": "歷史報酬率資料不足（請確認 FinMind 連線）"}

    mean_ret = returns_matrix.mean(axis=0)
    cov      = np.cov(returns_matrix.T)

    # 現有組合等權重（以股數加權）
    total_shares = sum(h["shares"] for h in holdings if h["stock_code"] in codes)
    current_w    = np.array([
        next((h["shares"] for h in holdings if h["stock_code"] == c), 1) / total_shares
        for c in codes
    ])

    current_perf = portfolio_performance(current_w, mean_ret, cov)

    # 效率前緣
    frontier = calc_efficient_frontier(returns_matrix)

    # 最佳化組合
    optimal = find_optimal_weights(frontier)
    min_var = find_min_variance_weights(frontier)

    # VaR
    var_result = calc_var(returns_matrix, current_w, confidence=0.95,
                          investment=sum(h["shares"] * h["cost_price"] for h in holdings))

    # 相關性矩陣
    corr_result = calc_correlation_matrix(returns_matrix, codes, names)

    # 最佳組合建議
    optimal_weights = optimal.get("weights", [])
    rebalance_suggestions = []
    if optimal_weights:
        for i, code in enumerate(codes):
            cur_pct  = round(float(current_w[i]) * 100, 1)
            opt_pct  = round(optimal_weights[i] * 100, 1) if i < len(optimal_weights) else 0
            diff     = opt_pct - cur_pct
            if abs(diff) >= 2:
                rebalance_suggestions.append({
                    "stock_code": code,
                    "name":       names[i],
                    "current":    cur_pct,
                    "optimal":    opt_pct,
                    "change":     round(diff, 1),
                    "action":     "加碼" if diff > 0 else "減碼",
                })

    return {
        "codes":             codes,
        "names":             names,
        "current_weights":   [round(float(w), 4) for w in current_w],
        "current_performance": {
            "return":     current_perf[0],
            "volatility": current_perf[1],
            "sharpe":     current_perf[2],
        },
        "frontier":          frontier,
        "optimal_portfolio": optimal,
        "min_var_portfolio": min_var,
        "var":               var_result,
        "correlation":       corr_result,
        "rebalance_suggestions": rebalance_suggestions,
        "holdings_count":    len(codes),
    }
