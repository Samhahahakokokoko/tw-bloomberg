"""AI Learn Service — AI 自動學習系統（預測記錄 + 準確率追蹤 + 權重調整）"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from loguru import logger

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "ailearn")
_PRED_FILE = os.path.join(_DATA_DIR, "predictions.json")
_WEIGHTS_FILE = os.path.join(_DATA_DIR, "weights.json")

_cache: dict = {}
_cache_ts: float = 0.0
_TTL = 1800

# 預設指標權重（可被學習系統調整）
DEFAULT_WEIGHTS: dict[str, float] = {
    "rsi":           1.0,
    "macd":          1.0,
    "volume_ratio":  1.0,
    "institutional": 1.2,
    "ma_cross":      1.1,
    "pcr":           0.8,
    "vix":           0.9,
    "adr_premium":   0.7,
    "margin_ratio":  0.8,
    "momentum":      1.0,
}


def _ensure_dir() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)


def load_weights() -> dict[str, float]:
    _ensure_dir()
    try:
        if os.path.exists(_WEIGHTS_FILE):
            with open(_WEIGHTS_FILE, encoding="utf-8") as f:
                return {**DEFAULT_WEIGHTS, **json.load(f)}
    except Exception as e:
        pass
    return dict(DEFAULT_WEIGHTS)


def save_weights(weights: dict[str, float]) -> None:
    _ensure_dir()
    try:
        with open(_WEIGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump(weights, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[ailearn] save_weights: {e}")


def load_predictions() -> list[dict]:
    _ensure_dir()
    try:
        if os.path.exists(_PRED_FILE):
            with open(_PRED_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        pass
    return []


def save_predictions(preds: list[dict]) -> None:
    _ensure_dir()
    try:
        with open(_PRED_FILE, "w", encoding="utf-8") as f:
            json.dump(preds[-500:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[ailearn] save_predictions: {e}")


def record_prediction(code: str, signal_type: str, direction: str,
                      indicators: list[str], confidence: float = 0.6) -> None:
    """記錄一筆新的 AI 預測，1週後追蹤結果"""
    preds = load_predictions()
    today = date.today().isoformat()
    expiry = (date.today() + timedelta(days=7)).isoformat()
    preds.append({
        "id":          int(time.time() * 1000),
        "date":        today,
        "expiry":      expiry,
        "code":        code,
        "signal_type": signal_type,
        "direction":   direction,
        "indicators":  indicators,
        "confidence":  confidence,
        "result":      None,
        "result_date": None,
        "pnl_pct":     None,
    })
    save_predictions(preds)


async def check_and_update_results() -> int:
    """檢查已到期預測，從 Yahoo Finance 取得實際漲跌，更新結果"""
    import httpx
    preds = load_predictions()
    today = date.today().isoformat()
    updated = 0

    for p in preds:
        if p.get("result") is not None:
            continue
        if p.get("expiry", "") > today:
            continue
        code = p.get("code", "")
        if not code or len(code) < 4:
            continue
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(url, params={"interval": "1d", "range": "15d"},
                                headers={"User-Agent": "Mozilla/5.0"})
            data = r.json()
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [x for x in closes if x is not None]
            if len(closes) >= 6:
                entry = closes[-6]
                current = closes[-1]
                pnl = (current - entry) / entry * 100 if entry else 0
                direction = p.get("direction", "bullish")
                correct = (pnl > 0) if direction == "bullish" else (pnl < 0)
                p["result"] = "correct" if correct else "wrong"
                p["result_date"] = today
                p["pnl_pct"] = round(pnl, 2)
                updated += 1
        except Exception as e:
            logger.debug(f"[ailearn] check result {code}: {e}")

    if updated:
        save_predictions(preds)
        _auto_adjust_weights(preds)

    return updated


def _auto_adjust_weights(preds: list[dict]) -> None:
    """根據各指標的準確率自動微調權重（±5% 步進）"""
    weights = load_weights()
    indicator_results: dict[str, list[bool]] = {k: [] for k in weights}

    for p in preds:
        if p.get("result") is None:
            continue
        correct = p["result"] == "correct"
        for ind in p.get("indicators", []):
            if ind in indicator_results:
                indicator_results[ind].append(correct)

    for ind, results in indicator_results.items():
        if len(results) < 5:
            continue
        acc = sum(results) / len(results)
        current = weights.get(ind, 1.0)
        if acc > 0.65:
            weights[ind] = min(2.0, round(current * 1.05, 3))
        elif acc < 0.45:
            weights[ind] = max(0.3, round(current * 0.95, 3))

    save_weights(weights)
    logger.info(f"[ailearn] weights auto-adjusted based on {len(preds)} predictions")


async def get_ailearn_report() -> dict:
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < _TTL:
        return _cache

    result = await _build_report()
    _cache = result
    _cache_ts = now
    return result


async def _build_report() -> dict:
    updated = await check_and_update_results()
    preds = load_predictions()
    weights = load_weights()

    scored = [p for p in preds if p.get("result") is not None]
    total = len(preds)
    correct_cnt = sum(1 for p in scored if p["result"] == "correct")
    overall_acc = correct_cnt / len(scored) * 100 if scored else 0.0

    # 各指標準確率
    indicator_acc: dict[str, list[bool]] = {}
    for p in scored:
        for ind in p.get("indicators", []):
            indicator_acc.setdefault(ind, []).append(p["result"] == "correct")

    ind_summary = []
    for ind, results in indicator_acc.items():
        acc = sum(results) / len(results) * 100
        ind_summary.append({
            "indicator": ind,
            "accuracy":  round(acc, 1),
            "count":     len(results),
            "weight":    round(weights.get(ind, 1.0), 3),
        })
    ind_summary.sort(key=lambda x: x["accuracy"], reverse=True)

    # 最近 30 天趨勢
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    recent = [p for p in scored if p.get("result_date", "") >= cutoff]
    recent_acc = sum(1 for p in recent if p["result"] == "correct") / len(recent) * 100 if recent else 0.0

    return {
        "total":        total,
        "scored":       len(scored),
        "overall_acc":  round(overall_acc, 1),
        "recent_acc":   round(recent_acc, 1),
        "updated_today": updated,
        "indicators":   ind_summary,
        "weights":      weights,
        "top3_best":    ind_summary[:3],
        "top3_worst":   ind_summary[-3:] if len(ind_summary) >= 3 else ind_summary,
    }


def format_ailearn_report(data: dict) -> str:
    total   = data.get("total", 0)
    scored  = data.get("scored", 0)
    acc     = data.get("overall_acc", 0.0)
    r_acc   = data.get("recent_acc", 0.0)
    updated = data.get("updated_today", 0)

    trend = "📈" if r_acc > acc else ("📉" if r_acc < acc - 5 else "➡️")

    lines = [
        "🤖 AI 自我學習報告",
        "─" * 32, "",
        f"總預測筆數：{total}  已驗證：{scored}",
        f"整體準確率：{acc:.1f}%  {trend} 近30天：{r_acc:.1f}%",
        f"今日更新結果：{updated} 筆",
        "",
    ]

    best = data.get("top3_best", [])
    if best:
        lines.append("✅ 準確率最高指標：")
        for b in best:
            bar = "█" * int(b["accuracy"] / 10) + "░" * (10 - int(b["accuracy"] / 10))
            lines.append(f"  {b['indicator']:15s} {b['accuracy']:.0f}% [{bar}] w={b['weight']:.2f}")
        lines.append("")

    worst = data.get("top3_worst", [])
    if worst:
        lines.append("⚠️  準確率偏低指標（已降低權重）：")
        for w in worst:
            bar = "█" * int(w["accuracy"] / 10) + "░" * (10 - int(w["accuracy"] / 10))
            lines.append(f"  {w['indicator']:15s} {w['accuracy']:.0f}% [{bar}] w={w['weight']:.2f}")
        lines.append("")

    if total == 0:
        lines += [
            "💡 系統尚無預測記錄",
            "使用各分析指令後，系統會自動記錄預測",
            "1週後自動追蹤結果並調整權重",
        ]
    else:
        lines += [
            "─" * 28,
            "🔧 自動調整說明",
            "• 準確率 > 65%：指標權重 +5%（加強參考）",
            "• 準確率 < 45%：指標權重 -5%（降低參考）",
            "• 每次驗證後自動更新，無需手動介入",
        ]

    lines += ["", "輸入 /techrating 2330 產生新預測"]
    return "\n".join(lines)
