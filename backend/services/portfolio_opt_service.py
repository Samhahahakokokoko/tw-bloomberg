"""Portfolio Opt Service — AI 馬可維茲投資組合最佳化（簡易版）"""
from __future__ import annotations

import time
import math
from loguru import logger
import asyncio

_cache: dict = {}
_cache_ts: dict = {}
_TTL = 3600 * 6  # 6 小時


async def get_portfolio_opt(uid: str) -> dict:
    key = uid
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < _TTL:
        return _cache[key]
    result = await _fetch_portfolio_opt(uid)
    _cache[key] = result
    _cache_ts[key] = now
    return result


async def _fetch_portfolio_opt(uid: str) -> dict:
    import httpx, asyncio
    from ..models.database import AsyncSessionLocal
    from . import portfolio_service

    # 1. 取得持倉
    try:
        async with AsyncSessionLocal() as db:
            holdings = await portfolio_service.get_portfolio(db, uid)
    except Exception as e:
        logger.debug(f"[portopt] get_portfolio {uid}: {e}")
        holdings = []

    if not holdings:
        return {"error": "目前庫存為空，請先使用 /buy 建立持倉"}

    codes = [h["stock_code"] for h in holdings]
    current_weights = {}
    total_mv = sum(h.get("market_value", 0) or 0 for h in holdings)
    if total_mv > 0:
        for h in holdings:
            current_weights[h["stock_code"]] = (h.get("market_value", 0) or 0) / total_mv

    # 2. 抓取每檔股票的歷史報酬（90天）
    async def fetch_returns(code: str):
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(url, params={"interval": "1d", "range": "6mo"},
                                headers={"User-Agent": "Mozilla/5.0"})
            closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [x for x in closes if x is not None]
            if len(closes) < 20:
                return code, []
            rets = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
            return code, rets
        except Exception as e:
            return code, []

    tasks   = [fetch_returns(c) for c in codes]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    ret_map: dict[str, list] = {}
    for r in results:
        if isinstance(r, tuple):
            code, rets = r
            if rets:
                ret_map[code] = rets

    valid_codes = [c for c in codes if c in ret_map]
    if len(valid_codes) < 2:
        return {"error": "有效股票數量不足（需至少 2 檔有歷史資料）", "holdings": holdings}

    # 3. 計算均值向量 & 協方差矩陣（簡易版）
    n     = len(valid_codes)
    means = {}
    stds  = {}
    for code in valid_codes:
        r_arr = ret_map[code]
        mu    = sum(r_arr) / len(r_arr)
        var   = sum((x - mu) ** 2 for x in r_arr) / len(r_arr)
        means[code] = mu * 252      # 年化
        stds[code]  = math.sqrt(var * 252)  # 年化標準差

    # 協方差（手算，避免 numpy 依賴）
    cov_matrix = [[0.0] * n for _ in range(n)]
    min_len = min(len(ret_map[c]) for c in valid_codes)
    for i, ci in enumerate(valid_codes):
        for j, cj in enumerate(valid_codes):
            ri = ret_map[ci][-min_len:]
            rj = ret_map[cj][-min_len:]
            mi = sum(ri) / len(ri)
            mj = sum(rj) / len(rj)
            cov = sum((ri[k] - mi) * (rj[k] - mj) for k in range(min_len)) / min_len * 252
            cov_matrix[i][j] = cov

    # 4. 蒙地卡羅模擬（5000次）找最大夏普 & 最小波動組合
    import random
    random.seed(42)
    rf     = 0.015  # 無風險利率 1.5%
    best_sharpe = -999.0
    best_minvol = 999.0
    opt_sharpe_w = [1.0 / n] * n
    opt_minvol_w = [1.0 / n] * n
    frontier: list[tuple] = []

    for _ in range(5000):
        raw = [random.random() for _ in range(n)]
        total = sum(raw)
        w = [x / total for x in raw]

        port_ret = sum(w[i] * means[valid_codes[i]] for i in range(n))
        port_var = sum(
            w[i] * w[j] * cov_matrix[i][j]
            for i in range(n) for j in range(n)
        )
        port_std = math.sqrt(max(port_var, 0))
        sharpe   = (port_ret - rf) / port_std if port_std > 0 else 0

        frontier.append((port_std, port_ret, sharpe, w[:]))

        if sharpe > best_sharpe:
            best_sharpe = sharpe
            opt_sharpe_w = w[:]
        if port_std < best_minvol:
            best_minvol = port_std
            opt_minvol_w = w[:]

    # 5. 等權組合 baseline
    eq_w   = [1.0 / n] * n
    eq_ret = sum(eq_w[i] * means[valid_codes[i]] for i in range(n))
    eq_var = sum(eq_w[i] * eq_w[j] * cov_matrix[i][j] for i in range(n) for j in range(n))
    eq_std = math.sqrt(max(eq_var, 0))
    eq_sharpe = (eq_ret - rf) / eq_std if eq_std > 0 else 0

    # 夏普組合指標
    sh_ret = sum(opt_sharpe_w[i] * means[valid_codes[i]] for i in range(n))
    sh_var = sum(opt_sharpe_w[i] * opt_sharpe_w[j] * cov_matrix[i][j] for i in range(n) for j in range(n))
    sh_std = math.sqrt(max(sh_var, 0))

    # 最小波動組合指標
    mv_ret = sum(opt_minvol_w[i] * means[valid_codes[i]] for i in range(n))
    mv_var = sum(opt_minvol_w[i] * opt_minvol_w[j] * cov_matrix[i][j] for i in range(n) for j in range(n))
    mv_std = math.sqrt(max(mv_var, 0))
    mv_sharpe = (mv_ret - rf) / mv_std if mv_std > 0 else 0

    # 6. 相關性矩陣（用於分散度診斷）
    corr_pairs = []
    for i in range(n):
        for j in range(i+1, n):
            corr = cov_matrix[i][j] / (stds[valid_codes[i]] * stds[valid_codes[j]]) \
                   if stds[valid_codes[i]] * stds[valid_codes[j]] > 0 else 0
            corr_pairs.append((valid_codes[i], valid_codes[j], round(corr, 2)))
    corr_pairs.sort(key=lambda x: abs(x[2]), reverse=True)
    high_corr = [p for p in corr_pairs if abs(p[2]) > 0.7]

    # 7. 與現有持倉比較，給出調整建議
    rebalance = []
    for i, code in enumerate(valid_codes):
        cur_w = current_weights.get(code, 0)
        opt_w = opt_sharpe_w[i]
        diff  = opt_w - cur_w
        if abs(diff) > 0.05:
            action = "增持" if diff > 0 else "減持"
            rebalance.append({
                "code": code, "current_pct": round(cur_w*100, 1),
                "optimal_pct": round(opt_w*100, 1),
                "diff_pct": round(diff*100, 1), "action": action,
            })
    rebalance.sort(key=lambda x: abs(x["diff_pct"]), reverse=True)

    return {
        "valid_codes":   valid_codes,
        "n":             n,
        "means":         {k: round(v*100, 2) for k, v in means.items()},
        "stds":          {k: round(v*100, 2) for k, v in stds.items()},
        "current_weights": {k: round(v*100, 1) for k, v in current_weights.items()},
        "eq": {"ret": round(eq_ret*100,2), "std": round(eq_std*100,2), "sharpe": round(eq_sharpe,3)},
        "max_sharpe": {
            "weights":   {valid_codes[i]: round(opt_sharpe_w[i]*100, 1) for i in range(n)},
            "ret":       round(sh_ret*100, 2),
            "std":       round(sh_std*100, 2),
            "sharpe":    round(best_sharpe, 3),
        },
        "min_vol": {
            "weights":   {valid_codes[i]: round(opt_minvol_w[i]*100, 1) for i in range(n)},
            "ret":       round(mv_ret*100, 2),
            "std":       round(mv_std*100, 2),
            "sharpe":    round(mv_sharpe, 3),
        },
        "high_corr":   high_corr[:5],
        "rebalance":   rebalance[:6],
        "updated_at":  time.strftime("%Y-%m-%d %H:%M"),
    }


def format_portfolio_opt_report(data: dict) -> str:
    if data.get("error"):
        return f"❌ {data['error']}"

    n       = data.get("n", 0)
    codes   = data.get("valid_codes", [])
    ms      = data.get("max_sharpe", {})
    mv      = data.get("min_vol", {})
    eq      = data.get("eq", {})
    rebal   = data.get("rebalance", [])
    hcorr   = data.get("high_corr", [])
    means   = data.get("means", {})
    stds    = data.get("stds", {})
    updated = data.get("updated_at", "")

    lines = [
        "📐 AI 投資組合最佳化",
        "─" * 32, "",
        f"分析股票：{n} 檔  更新：{updated}",
        "",
        "── 各股年化指標 ──",
    ]
    for code in codes:
        mu  = means.get(code, 0)
        sig = stds.get(code, 0)
        lines.append(f"  {code}  預期報酬：{mu:+.1f}%  波動度：{sig:.1f}%")

    lines += [
        "",
        "── 組合比較 ──",
        f"  {'類型':8s}  {'預期報酬':>8s}  {'波動度':>7s}  {'夏普':>6s}",
        f"  {'等權組合':8s}  {eq.get('ret',0):>+7.1f}%  {eq.get('std',0):>6.1f}%  {eq.get('sharpe',0):>6.3f}",
        f"  {'最大夏普':8s}  {ms.get('ret',0):>+7.1f}%  {ms.get('std',0):>6.1f}%  {ms.get('sharpe',0):>6.3f}",
        f"  {'最小波動':8s}  {mv.get('ret',0):>+7.1f}%  {mv.get('std',0):>6.1f}%  {mv.get('sharpe',0):>6.3f}",
        "",
        "── 最大夏普比率配置 ──",
    ]
    ms_w = ms.get("weights", {})
    for code, w in sorted(ms_w.items(), key=lambda x: -x[1]):
        bar = "█" * int(w / 5) + "░" * (20 - int(w / 5))
        lines.append(f"  {code}  {bar}  {w:.1f}%")

    if rebal:
        lines += ["", "── 調整建議（vs 現有持倉）──"]
        for r in rebal:
            icon = "⬆️" if r["action"] == "增持" else "⬇️"
            lines.append(
                f"  {icon} {r['code']}  現有{r['current_pct']:.1f}%"
                f" → 最佳{r['optimal_pct']:.1f}%"
                f"  ({r['action']}{abs(r['diff_pct']):.1f}%)"
            )

    if hcorr:
        lines += ["", "⚠️ 高相關性警告（分散風險不足）："]
        for c1, c2, corr in hcorr:
            lines.append(f"  {c1} ↔ {c2}  相關係數：{corr:.2f}")

    lines += [
        "",
        "─" * 28,
        "🤖 AI 建議",
        _gen_opt_verdict(data),
        "",
        "⚠️ 以上為統計模型估算，非投資建議",
        "輸入 /optimize 查 RSI 最佳化 | /correlation 查相關性",
    ]
    return "\n".join(lines)


def _gen_opt_verdict(data: dict) -> str:
    ms     = data.get("max_sharpe", {})
    eq     = data.get("eq", {})
    rebal  = data.get("rebalance", [])
    hcorr  = data.get("high_corr", [])

    tips = []
    sh_gain = ms.get("sharpe", 0) - eq.get("sharpe", 0)
    if sh_gain > 0.1:
        tips.append(f"調整至最大夏普組合可提升風險調整後報酬 {sh_gain:.2f}（夏普值）")
    if len(hcorr) >= 2:
        tips.append(f"持股相關性偏高（{len(hcorr)}組 >0.7），建議引入低相關資產分散風險")
    if rebal:
        top = rebal[0]
        tips.append(f"{top['code']} 建議{top['action']} {abs(top['diff_pct']):.1f}% 以接近最佳配置")
    return "；".join(tips) if tips else "目前持倉配置合理，可維持現況觀察。"


async def push_weekly_portfolio_opt(uid: str) -> bool:
    """每週推播最佳化建議"""
    from .line_push import push_line_messages
    try:
        data   = await get_portfolio_opt(uid)
        report = format_portfolio_opt_report(data)
        ok = await push_line_messages(
            uid,
            [{"type": "text", "text": report[:4000]}],
            context="portfolio_opt.weekly",
        )
        return ok
    except Exception as e:
        logger.error(f"[portfolio_opt] push {uid}: {e}")
        return False
