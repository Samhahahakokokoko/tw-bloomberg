"""Portfolio Engine — 馬可維茲最佳化 + 持倉限制

特色（相對 portfolio_optimizer.py 更嚴格的約束）：
  - max_weight_per_stock = 0.20（單股不超過 20%）
  - max_sector_weight    = 0.40（同一產業不超過 40%）
  - long-only（不做空）
  - 輸出每檔建議權重 + 預期年化報酬 / 年化波動 / Sharpe

此模組可獨立呼叫，不依賴 FastAPI/DB（方便回測流程直接使用）。
"""
from __future__ import annotations
import asyncio
import numpy as np
from datetime import date, timedelta
from loguru import logger

MAX_WEIGHT_PER_STOCK = 0.20
MAX_SECTOR_WEIGHT    = 0.40
RISK_FREE_RATE       = 0.015  # 台灣定存利率（年）


async def build_optimal_portfolio(
    holdings: list[dict],        # [{"code": "2330", "sector": "半導體", ...}]
    days: int = 252,
    objective: str = "sharpe",   # "sharpe" | "min_vol" | "max_ret"
) -> dict:
    """
    holdings: list of dict with keys: code, sector (optional)
    objective: "sharpe" = max Sharpe, "min_vol" = min volatility, "max_ret" = max return
    """
    if len(holdings) < 2:
        return {"error": "需要至少 2 檔股票"}

    codes   = [h["code"]   for h in holdings]
    sectors = [h.get("sector", "其他") for h in holdings]

    # 抓歷史報酬
    from backend.services.portfolio_optimizer import get_returns_matrix
    names = [h.get("name", c) for h, c in zip(holdings, codes)]
    holdings_fmt = [{"stock_code": c, "stock_name": n} for c, n in zip(codes, names)]

    returns_matrix, valid_codes, valid_names = await get_returns_matrix(holdings_fmt, days)
    if returns_matrix.size == 0 or len(valid_codes) < 2:
        return {"error": "歷史報酬率資料不足"}

    # 對應 sector
    code_to_sector = {h["code"]: h.get("sector", "其他") for h in holdings}
    valid_sectors = [code_to_sector.get(c, "其他") for c in valid_codes]

    mean_ret = returns_matrix.mean(axis=0)
    cov      = np.cov(returns_matrix.T)

    try:
        w = _optimize(mean_ret, cov, valid_sectors, objective)
    except Exception as e:
        logger.error(f"[PortfolioEngine] optimize error: {e}")
        w = np.ones(len(valid_codes)) / len(valid_codes)

    # 計算組合績效
    ann_ret = float(np.sum(w * mean_ret) * 252 * 100)
    ann_vol = float(np.sqrt(w @ cov * 252 @ w) * 100)
    sharpe  = (ann_ret / 100 - RISK_FREE_RATE) / (ann_vol / 100) if ann_vol > 0 else 0

    weights_out = [
        {
            "code":      valid_codes[i],
            "name":      valid_names[i],
            "sector":    valid_sectors[i],
            "weight":    round(float(w[i]) * 100, 2),
            "weight_raw":round(float(w[i]), 4),
        }
        for i in range(len(valid_codes))
        if w[i] > 0.001
    ]
    weights_out.sort(key=lambda x: x["weight"], reverse=True)

    # 產業分配
    sector_allocation: dict[str, float] = {}
    for wo in weights_out:
        s = wo["sector"]
        sector_allocation[s] = sector_allocation.get(s, 0) + wo["weight"]

    return {
        "objective":   objective,
        "weights":     weights_out,
        "sector_allocation": sector_allocation,
        "expected_return":   round(ann_ret, 2),
        "expected_volatility": round(ann_vol, 2),
        "sharpe_ratio":      round(sharpe, 3),
        "constraints": {
            "max_weight_per_stock": MAX_WEIGHT_PER_STOCK * 100,
            "max_sector_weight":    MAX_SECTOR_WEIGHT    * 100,
        },
    }


def _optimize(mean_ret: np.ndarray, cov: np.ndarray,
              sectors: list[str], objective: str) -> np.ndarray:
    """scipy 最佳化，帶持倉上限 + 產業上限約束"""
    from scipy.optimize import minimize

    n = len(mean_ret)
    w0 = np.ones(n) / n

    # 個股上限
    bounds = [(0, MAX_WEIGHT_PER_STOCK) for _ in range(n)]

    # 權重總和 = 1
    constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1}]

    # 產業上限
    unique_sectors = list(set(sectors))
    for sec in unique_sectors:
        idx = [i for i, s in enumerate(sectors) if s == sec]
        if len(idx) > 1:
            constraints.append({
                "type": "ineq",
                "fun": lambda w, idx=idx: MAX_SECTOR_WEIGHT - sum(w[i] for i in idx),
            })

    def neg_sharpe(w):
        ret = float(np.sum(w * mean_ret) * 252)
        vol = float(np.sqrt(w @ cov * 252 @ w))
        return -(ret - RISK_FREE_RATE) / vol if vol > 1e-9 else 0

    def portfolio_vol(w):
        return float(np.sqrt(w @ cov * 252 @ w))

    def neg_return(w):
        return -float(np.sum(w * mean_ret) * 252)

    obj_fn = {
        "sharpe":  neg_sharpe,
        "min_vol": portfolio_vol,
        "max_ret": neg_return,
    }.get(objective, neg_sharpe)

    res = minimize(obj_fn, w0, method="SLSQP", bounds=bounds, constraints=constraints,
                   options={"maxiter": 500, "ftol": 1e-9})
    if res.success:
        w = np.clip(res.x, 0, MAX_WEIGHT_PER_STOCK)
        return w / w.sum()
    # fallback: equal weight
    return w0
