"""
main.py — quant/ 量化系統 FastAPI 應用

端點：
  POST /run_backtest       — 回測指定股票 + 策略，自動儲存 Feedback
  GET  /get_signals/{code} — 取得即時 Alpha 訊號（含盤態 + 策略建議）
  POST /get_portfolio      — 馬可維茲投組最佳化
  GET  /get_performance    — 回饋引擎績效總覽 + 策略排行

資料來源優先順序：
  1. 呼叫 backend.services.twse_service.fetch_kline（同 backtest/ 模組）
  2. 若無法匯入（獨立部署時），使用內建 mock 資料回退

掛載方式（加入 backend/api/routes.py）：
  from quant.main import router as quant_router
  app.include_router(quant_router, prefix="/quant")

或獨立啟動（測試用）：
  uvicorn quant.main:app --reload --port 8001
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .feature_engine import FeatureEngine
from .alpha_model import AlphaModel, RuleBasedAlpha, Signal, _LGB_AVAILABLE
from .backtest_engine import BacktestEngine, BacktestReport
from .risk_engine import RiskEngine, MarketRegime
from .portfolio_engine import PortfolioEngine
from .feedback_engine import FeedbackEngine, get_feedback_engine
from .strategy_engine import StrategyEngine, MOCK_STOCKS as _MOCK_STOCKS
from .confidence_engine import ConfidenceEngine
from .odd_lot_engine import OddLotEngine
from .signal_db import SignalDB, get_signal_db
from .factor_ic_engine import FactorICEngine, DEFAULT_FACTORS
from .dynamic_weight_engine import DynamicWeightEngine, WeightMode, get_dynamic_weight_engine
from .regime_engine import RegimeEngine, get_regime_engine, EnhancedRegimeEngine, get_enhanced_regime_engine
from .alpha_portfolio_engine import AlphaPortfolioEngine, get_alpha_portfolio_engine
from .risk_engine import RiskManagerV2, get_risk_manager_v2, RiskEngineV3, get_risk_engine_v3, RiskIsolation, get_risk_isolation
from .adaptive_weight_engine import AdaptiveWeightEngine, get_adaptive_weight_engine
from .walkforward_engine import WalkForwardAnalyzer, get_walkforward_analyzer
from .slippage_engine import SlippageEngine, get_slippage_engine
from .alpha_registry import AlphaRegistry, AlphaStatus, get_alpha_registry
from .montecarlo_engine import MonteCarloEngine, get_montecarlo_engine
from .sentiment_engine import SentimentEngine, get_sentiment_engine

logger = logging.getLogger(__name__)

# ── FastAPI 應用 ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Quant API — 台股 AI 量化交易",
    description="整合 FeatureEngine / AlphaModel / BacktestEngine / RiskEngine / PortfolioEngine / FeedbackEngine",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 可掛載為 sub-router（讓 backend/api/routes.py include 時有 prefix）
from fastapi import APIRouter
router = APIRouter(prefix="/quant", tags=["quant"])

# 全域共享的引擎實例
_rule_alpha      = RuleBasedAlpha()
_alpha_model     = AlphaModel()          # LightGBM 版（若未訓練則降級至 rule）
_risk_engine     = RiskEngine()
_strategy_engine = StrategyEngine()
_confidence_engine = ConfidenceEngine()
_odd_lot_engine  = OddLotEngine(discount=0.6)


# ── 工具函式 ─────────────────────────────────────────────────────────────────

async def _fetch_kline(stock_code: str, start_date: Optional[str] = None) -> pd.DataFrame:
    """
    從 backend 抓取 K 線資料；若無法匯入則產生 mock 資料。
    回傳 OHLCV DataFrame（含 date 欄位）。
    """
    try:
        from backend.services.twse_service import fetch_kline
        kline = await fetch_kline(stock_code, start_date)
        if not kline:
            raise ValueError("empty kline")
        df = pd.DataFrame(kline)
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception as e:
        logger.warning(f"[quant] fetch_kline({stock_code}) 失敗，改用 mock ({e})")
        return _mock_kline(stock_code)


def _mock_kline(stock_code: str, n: int = 300) -> pd.DataFrame:
    """產生 mock OHLCV（用於測試 / 無法連線時的後備）"""
    seed = sum(ord(c) for c in stock_code)
    rng  = np.random.default_rng(seed)
    dates  = pd.date_range("2023-01-01", periods=n, freq="B")
    close  = 100 * np.cumprod(1 + rng.normal(0.0003, 0.012, n))
    high   = close * rng.uniform(1.000, 1.025, n)
    low    = close * rng.uniform(0.975, 1.000, n)
    open_  = close * rng.uniform(0.990, 1.010, n)
    volume = rng.integers(5_000_000, 50_000_000, n).astype(float)
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high,
        "low": low, "close": close, "volume": volume,
    })


def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    fe = FeatureEngine(df)
    return fe.compute_all()


# ── Request / Response 模型 ───────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    stock_code:       str
    strategy:         str = "rule_based"  # rule_based / ma_cross / rsi / macd / momentum …
    start_date:       Optional[str] = None
    initial_capital:  float = 1_000_000
    commission_discount: float = Field(0.6, ge=0.1, le=1.0)
    stop_loss_pct:    Optional[float] = Field(None, ge=0.01, le=0.30)
    take_profit_pct:  Optional[float] = Field(None, ge=0.01, le=0.50)
    position_size_pct: float = Field(0.95, ge=0.1, le=1.0)
    save_feedback:    bool = True


class PortfolioRequest(BaseModel):
    holdings: list[dict] = Field(
        ...,
        example=[
            {"code": "2330", "sector": "半導體", "name": "台積電"},
            {"code": "2412", "sector": "電信",   "name": "中華電"},
        ],
    )
    objective: str = Field("max_sharpe", pattern="^(max_sharpe|min_vol|max_ret)$")
    lookback_days: int = Field(250, ge=60, le=500)


# ── 端點 ─────────────────────────────────────────────────────────────────────

@router.post("/run_backtest", summary="執行量化回測")
async def run_backtest(req: BacktestRequest):
    """
    執行回測並回傳完整績效報告。

    strategy 可選：
      - `rule_based`  使用 RuleBasedAlpha 自動產生訊號（預設）
      - `ma_cross`    MA5 > MA20 買進，MA5 < MA20 賣出
      - `rsi`         RSI < 30 買進，RSI > 70 賣出
      - `macd`        MACD 金叉買進，死叉賣出
      - `momentum`    5 日報酬率 > 2% 買進，< -1% 賣出
    """
    df = await _fetch_kline(req.stock_code, req.start_date)
    if len(df) < 30:
        raise HTTPException(422, f"K 線資料不足（{len(df)} 筆），至少需要 30 筆")

    feat_df = _build_features(df)

    # 產生訊號
    signals = _generate_signals(feat_df, req.strategy)

    # 盤態偵測
    re      = RiskEngine()
    regime  = re.detect_regime(feat_df)

    # 回測
    engine = BacktestEngine(
        initial_capital=req.initial_capital,
        commission_discount=req.commission_discount,
    )
    report = engine.run(
        feat_df, signals,
        stop_loss_pct=req.stop_loss_pct,
        take_profit_pct=req.take_profit_pct,
        position_size_pct=req.position_size_pct,
    )

    # 非同步儲存到 Feedback（不阻塞回應）
    if req.save_feedback:
        asyncio.create_task(_save_feedback_task(report, req.stock_code, req.strategy, regime.regime.value))

    result = report.to_dict()
    result["regime"] = {
        "current":     regime.regime.value,
        "description": regime.description,
        "confidence":  regime.confidence,
        "tip":         regime.tip,
    }
    result["strategy"] = req.strategy
    result["stock_code"] = req.stock_code
    return result


@router.get("/get_signals/{stock_code}", summary="取得即時 Alpha 訊號")
async def get_signals(
    stock_code: str,
    chip_days:  int   = 0,    # 外資連買天數（外部輸入）
    foreign_net:float = 0.0,  # 外資淨買（張）
):
    """
    計算最新交易日的 Alpha 訊號。

    回傳：
    - signal:       buy / sell / hold
    - score:        RuleBasedAlpha 綜合評分 0~100
    - pred_ret:     LightGBM 預測 5 日報酬（若模型已載入）
    - regime:       市場盤態
    - strategies:   當前盤態建議策略清單
    - features:     最新技術指標快照
    - reasons:      訊號理由清單
    - stop_loss:    建議停損價（ATR × 2）
    - take_profit:  建議停利價（風報比 2:1）
    """
    df      = await _fetch_kline(stock_code)
    if len(df) < 20:
        raise HTTPException(422, "資料不足，無法計算訊號")

    feat_df = _build_features(df)
    last    = feat_df.iloc[-1]

    # Alpha 訊號
    output = _alpha_model.predict(last, chip_days=chip_days)

    # 盤態偵測
    re     = RiskEngine()
    regime = re.detect_regime(feat_df)

    # 停損 / 停利
    atr14  = float(last.get("atr14", 0) or 0)
    entry  = float(last["close"])
    stop   = re.calc_stop_loss(entry, method="atr" if atr14 > 0 else "fixed",
                               atr=atr14, atr_mult=2.0,
                               fixed_pct=regime.stop_loss)
    tp     = re.calc_take_profit(entry, stop_price=stop, rr_ratio=2.0)

    # 回饋引擎推薦策略
    fb     = get_feedback_engine()
    best_strategy = fb.recommend_strategy(regime.regime.value)

    # 特徵快照（選取最有用的子集）
    feature_snapshot = {
        k: round(float(last[k]), 4) if not np.isnan(float(last[k])) else None
        for k in ["ma5", "ma20", "ma60", "ma200", "rsi14", "macd_hist",
                  "k", "d", "boll_b", "atr14", "vol_ratio", "ret_5d", "excess_ret"]
        if k in last.index
    }

    return {
        "stock_code":   stock_code,
        "date":         str(last.get("date", ""))[:10],
        "close":        round(entry, 2),
        "signal":       output.signal.value,
        "score":        output.score,
        "pred_ret":     output.pred_ret,
        "reasons":      output.reasons,
        "regime": {
            "current":          regime.regime.value,
            "description":      regime.description,
            "confidence":       regime.confidence,
            "strategies":       regime.strategies,
            "recommended":      best_strategy,
            "max_long_pct":     regime.max_long_pct,
            "tip":              regime.tip,
        },
        "risk": {
            "entry_price":  round(entry, 2),
            "stop_loss":    stop,
            "take_profit":  tp,
            "atr14":        round(atr14, 2),
        },
        "features":     feature_snapshot,
        "model_type":   "lightgbm" if (_LGB_AVAILABLE and _alpha_model.model_ is not None) else "rule_based",
    }


@router.post("/get_portfolio", summary="馬可維茲投資組合最佳化")
async def get_portfolio(req: PortfolioRequest):
    """
    輸入持股清單，回傳最佳化權重與風險指標。

    holdings 格式：[{"code":"2330","sector":"半導體","name":"台積電"}, ...]
    """
    if len(req.holdings) < 2:
        raise HTTPException(422, "至少需要 2 檔股票才能最佳化")

    codes = [h["code"] for h in req.holdings]

    # 並行抓取所有歷史價格
    tasks = [_fetch_kline(code) for code in codes]
    dfs   = await asyncio.gather(*tasks, return_exceptions=True)

    price_dict: dict[str, pd.Series] = {}
    for code, result in zip(codes, dfs):
        if isinstance(result, Exception):
            logger.warning(f"[quant/portfolio] {code} 取價失敗: {result}")
            continue
        df = result
        if len(df) >= 60:
            price_dict[code] = df["close"].reset_index(drop=True)

    if len(price_dict) < 2:
        raise HTTPException(422, f"有效股票數不足（{len(price_dict)} 檔），無法最佳化")

    sectors = {h["code"]: h.get("sector", "其他") for h in req.holdings}
    pe      = PortfolioEngine()

    try:
        result = pe.optimize(
            price_dict,
            sectors=sectors,
            objective=req.objective,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))

    # 附加每檔股票的 Alpha 訊號（非同步）
    signal_tasks = {code: _get_signal_brief(code) for code in price_dict}
    signals_done = await asyncio.gather(*signal_tasks.values(), return_exceptions=True)
    signals_map  = {
        code: (sig if not isinstance(sig, Exception) else {"signal": "hold", "score": 50})
        for code, sig in zip(signal_tasks.keys(), signals_done)
    }

    return {
        "objective":     req.objective,
        "weights":       result.weights,
        "expected_ret":  round(result.expected_ret * 100, 2),
        "volatility":    round(result.volatility * 100, 2),
        "sharpe":        result.sharpe,
        "var_95_pct":    round(result.var_95 * 100, 2),
        "cvar_95_pct":   round(result.cvar_95 * 100, 2),
        "sector_weights": result.sector_weights,
        "method":        result.method,
        "frontier":      result.frontier[:10],   # 前 10 點避免 payload 過大
        "corr_matrix":   result.corr_matrix,
        "alpha_signals": signals_map,
        "warnings":      result.warnings,
    }


@router.get("/get_performance", summary="量化績效總覽")
async def get_performance(regime: Optional[str] = None):
    """
    回饋引擎績效總覽：各策略 × 盤態 的歷史勝率 / 夏普值統計，
    以及當前策略權重。
    """
    fb    = get_feedback_engine()
    stats = fb.performance_summary()

    if regime:
        stats = [s for s in stats if s.regime == regime]

    # 各盤態最佳策略
    regimes_best: dict[str, str] = {}
    for r in ["bull", "bear", "sideways", "volatile"]:
        regimes_best[r] = fb.recommend_strategy(r)

    return {
        "total_records": len(fb.get_records()),
        "strategy_weights": fb.get_strategy_weights(),
        "alpha_weights":    fb.get_alpha_weights(),
        "regime_best":      regimes_best,
        "stats": [
            {
                "strategy":      s.strategy,
                "regime":        s.regime,
                "n_records":     s.n_records,
                "avg_sharpe":    s.avg_sharpe,
                "avg_win_rate":  round(s.avg_win_rate * 100, 1),
                "avg_return":    round(s.avg_return * 100, 2),
                "avg_drawdown":  round(s.avg_drawdown * 100, 2),
                "current_weight":s.current_weight,
                "recommendation":s.recommendation,
            }
            for s in stats
        ],
    }


@router.post("/adjust_weights", summary="手動觸發權重調整")
async def adjust_weights():
    """手動觸發 Feedback Engine 自動調整（等同排程任務）"""
    fb     = get_feedback_engine()
    result = fb.auto_adjust()
    return {
        "changed":          len(result["changes"]),
        "changes":          result["changes"],
        "strategy_weights": result["strategy_weights"],
        "adjusted_at":      result["adjusted_at"],
    }


@router.get("/health", summary="健康檢查")
async def health():
    fb = get_feedback_engine()
    return {
        "status":          "ok",
        "lgb_available":   _LGB_AVAILABLE,
        "model_loaded":    _alpha_model.model_ is not None,
        "feedback_records": len(fb.get_records()),
        "modules": {
            "feature_engine":  "ok",
            "alpha_model":     "lgb" if (_LGB_AVAILABLE and _alpha_model.model_) else "rule_based",
            "backtest_engine": "ok",
            "risk_engine":     "ok",
            "portfolio_engine":"ok",
            "feedback_engine": "ok",
        },
    }


# ── 內部工具 ─────────────────────────────────────────────────────────────────

def _generate_signals(feat_df: pd.DataFrame, strategy: str) -> pd.Series:
    """根據策略名稱產生訊號 Series"""
    n = len(feat_df)

    if strategy == "rule_based":
        alpha = RuleBasedAlpha()
        return pd.Series([
            alpha.evaluate(row).signal.value
            for _, row in feat_df.iterrows()
        ])

    signals = ["hold"] * n

    if strategy == "ma_cross":
        for i, row in feat_df.iterrows():
            ma5, ma20 = row.get("ma5", np.nan), row.get("ma20", np.nan)
            if np.isnan(ma5) or np.isnan(ma20):
                continue
            if i > 0:
                prev = feat_df.iloc[i - 1]
                if prev.get("ma5", ma5) <= prev.get("ma20", ma20) and ma5 > ma20:
                    signals[i] = "buy"
                elif prev.get("ma5", ma5) >= prev.get("ma20", ma20) and ma5 < ma20:
                    signals[i] = "sell"

    elif strategy == "rsi":
        for i, row in feat_df.iterrows():
            rsi = row.get("rsi14", np.nan)
            if np.isnan(rsi):
                continue
            if rsi < 30:
                signals[i] = "buy"
            elif rsi > 70:
                signals[i] = "sell"

    elif strategy == "macd":
        for i, row in feat_df.iterrows():
            golden = row.get("macd_golden", 0)
            hist   = row.get("macd_hist", np.nan)
            if golden:
                signals[i] = "buy"
            elif not np.isnan(hist) and hist < -0.5:
                signals[i] = "sell"

    elif strategy == "momentum":
        for i, row in feat_df.iterrows():
            ret5 = row.get("ret_5d", np.nan)
            if np.isnan(ret5):
                continue
            if ret5 > 0.02:
                signals[i] = "buy"
            elif ret5 < -0.02:
                signals[i] = "sell"

    elif strategy == "bollinger":
        for i, row in feat_df.iterrows():
            b = row.get("boll_b", np.nan)
            if np.isnan(b):
                continue
            if b < 0.05:
                signals[i] = "buy"
            elif b > 0.95:
                signals[i] = "sell"

    return pd.Series(signals)


async def _get_signal_brief(stock_code: str) -> dict:
    """快速取得單股訊號摘要（用於組合結果附加）"""
    try:
        df      = await _fetch_kline(stock_code)
        feat_df = _build_features(df)
        last    = feat_df.iloc[-1]
        out     = _rule_alpha.evaluate(last)
        return {"signal": out.signal.value, "score": out.score}
    except Exception:
        return {"signal": "hold", "score": 50}


# ═══════════════════════════════════════════════════════════════════
#  新端點：Strategy / OddLot / Compare / Recommend
# ═══════════════════════════════════════════════════════════════════

class StrategyAnalyzeRequest(BaseModel):
    stock_id:   str
    name:       str = ""
    strategy:   str = Field("composite", pattern="^(composite|momentum|value|chip)$")
    # 動能
    momentum_20d:     float = 1.0
    foreign_buy_days: int   = 0
    volume_ratio:     float = 1.0
    # 價值
    dividend_yield:   float = 0.0
    pe_ratio:         float = 20.0
    eps_stability:    float = 0.5
    # 籌碼
    foreign_net:      float = 0.0
    trust_net:        float = 0.0
    dealer_net:       float = 0.0
    chip_concentration: float = 50.0
    # 風險 / 技術
    volatility:   float = 0.015
    max_drawdown: float = 0.10
    close:        float = 100.0
    ma20:         float = 0.0
    ma60:         float = 0.0
    atr14:        float = 0.0
    macd_golden:  int   = 0
    # 回測 / 模型（可選）
    backtest_sharpe: Optional[float] = None
    pred_ret:        Optional[float] = None


class OddLotRequest(BaseModel):
    budget:       float = Field(..., gt=0, description="可用預算（元）")
    price:        float = Field(..., gt=0, description="股票現價")
    stock_id:     str   = "????"
    name:         str   = ""
    target_price: Optional[float] = None
    discount:     float = Field(0.6, ge=0.1, le=1.0, description="手續費折扣")


class OddLotAllocateRequest(BaseModel):
    budget:   float = Field(..., gt=0)
    stocks:   list[dict]    # [{stock_id, name, price, weight?}]
    strategy: str = Field("weight", pattern="^(weight|equal|signal)$")
    discount: float = Field(0.6, ge=0.1, le=1.0)


@router.post("/strategy/analyze", summary="單股策略分析")
async def strategy_analyze(req: StrategyAnalyzeRequest):
    """
    輸入股票基本面 + 技術面數據，回傳完整策略評分與買賣建議。
    """
    # 偵測盤態（若有 K 線資料）
    regime = "unknown"
    try:
        df = await _fetch_kline(req.stock_id)
        if len(df) >= 20:
            feat_df = _build_features(df)
            regime_result = _risk_engine.detect_regime(feat_df)
            regime = regime_result.regime.value
            # 補充技術指標（若未傳入）
            last = feat_df.iloc[-1]
            if req.close <= 0:
                req.close = float(last.get("close", req.close))
    except Exception:
        pass

    data = req.model_dump()
    data["stock_id"] = req.stock_id
    data["name"]     = req.name or req.stock_id

    sig = _strategy_engine.evaluate(data, strategy=req.strategy, regime=regime)

    # 信心指數（整合多源）
    breakdown = _confidence_engine.from_strategy_signal(sig, pred_ret=req.pred_ret)
    sig_dict  = sig.to_dict()
    sig_dict["confidence"] = breakdown.total
    sig_dict["confidence_breakdown"] = breakdown.to_dict()
    sig_dict["regime"] = regime

    # 儲存到 SignalDB（非同步，不阻塞）
    asyncio.create_task(_save_signal_task(sig, regime))
    return sig_dict


@router.get("/strategy/recommend", summary="市場推薦選股")
async def strategy_recommend(
    regime:         str   = "unknown",
    strategy:       str   = "composite",
    min_confidence: float = 60.0,
    limit:          int   = 10,
):
    """
    從 mock 股票池（或 DB 最新訊號）選出高信心標的，格式化為推薦列表。
    實際部署時，stock_pool 來自 signal_db 的 strategy_signals 表。
    """
    # 嘗試從 DB 拿今日訊號
    signals = []
    try:
        sdb = get_signal_db()
        signals = await sdb.get_latest_signals(
            strategy=strategy,
            min_confidence=min_confidence,
            limit=limit,
        )
    except Exception:
        pass

    # fallback：用 MOCK_STOCKS 即時計算
    if not signals:
        batch = _strategy_engine.batch_evaluate(
            _MOCK_STOCKS,
            strategy=strategy,
            regime=regime,
            min_confidence=min_confidence,
        )
        signals = [s.to_dict() for s in batch[:limit]]

    # 市場狀態說明
    regime_labels = {
        "bull": "多頭趨勢", "bear": "空頭趨勢",
        "sideways": "盤整", "volatile": "高波動", "unknown": "未知",
    }
    return {
        "regime":        regime,
        "regime_label":  regime_labels.get(regime, "未知"),
        "strategy":      strategy,
        "min_confidence":min_confidence,
        "count":         len(signals),
        "signals":       signals,
    }


@router.get("/strategy/compare/{code_a}/{code_b}", summary="比較兩股")
async def strategy_compare(
    code_a: str,
    code_b: str,
    regime: str = "unknown",
):
    """
    比較兩檔股票的策略評分，輸出哪個信心較高、風險較低、建議選擇。
    若在 MOCK_STOCKS 找不到，以 code 的 hash 產生 mock 資料。
    """
    def _find_or_mock(code: str) -> dict:
        for s in _MOCK_STOCKS:
            if s["stock_id"] == code:
                return s
        # 根據 code 生成 deterministic mock
        seed = sum(ord(c) for c in code)
        rng  = np.random.default_rng(seed)
        return {
            "stock_id": code, "name": code,
            "momentum_20d":     float(rng.uniform(0.95, 1.15)),
            "foreign_buy_days": int(rng.integers(-5, 8)),
            "volume_ratio":     float(rng.uniform(0.7, 2.0)),
            "dividend_yield":   float(rng.uniform(0, 8)),
            "pe_ratio":         float(rng.uniform(8, 30)),
            "eps_stability":    float(rng.uniform(0.3, 0.95)),
            "foreign_net":      float(rng.uniform(-2000, 5000)),
            "trust_net":        float(rng.uniform(-500, 1000)),
            "dealer_net":       float(rng.uniform(-200, 300)),
            "chip_concentration": float(rng.uniform(40, 85)),
            "volatility":       float(rng.uniform(0.007, 0.025)),
            "max_drawdown":     float(rng.uniform(0.03, 0.25)),
            "close":            float(rng.uniform(30, 1200)),
            "atr14":            float(rng.uniform(0.5, 30)),
        }

    data_a = _find_or_mock(code_a)
    data_b = _find_or_mock(code_b)

    result = _strategy_engine.compare(data_a, data_b, regime=regime)
    return result


@router.post("/odd_lot/calc", summary="零股計算")
async def odd_lot_calc(req: OddLotRequest):
    """
    計算指定預算可買幾股零股，含手續費、損益兩平、最小獲利幅度。
    """
    engine = OddLotEngine(discount=req.discount)
    try:
        result = engine.calc(
            budget=req.budget,
            price=req.price,
            stock_id=req.stock_id,
            name=req.name,
            target_price=req.target_price,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    return result.to_dict()


@router.post("/odd_lot/allocate", summary="零股預算分配")
async def odd_lot_allocate(req: OddLotAllocateRequest):
    """
    將預算分配到多檔零股，回傳組合建議（含各股股數與手續費）。
    """
    engine = OddLotEngine(discount=req.discount)
    try:
        portfolio = engine.allocate(req.budget, req.stocks, strategy=req.strategy)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {
        "total_budget": portfolio.total_budget,
        "total_cost":   portfolio.total_cost,
        "total_fee":    portfolio.total_fee,
        "remaining":    portfolio.remaining,
        "allocations":  portfolio.allocations,
    }


@router.get("/strategy/screener", summary="多條件選股")
async def strategy_screener(
    action:     Optional[str]   = None,   # 強力買進/買進/觀察
    risk_level: Optional[str]   = None,   # 低/中/高
    strategy:   str             = "composite",
    min_confidence: float       = 50.0,
    limit:      int             = 20,
):
    """
    從 mock 股票池篩選符合條件的標的（生產環境改成從 strategy_signals 表查詢）。
    """
    results = _strategy_engine.batch_evaluate(_MOCK_STOCKS, strategy=strategy, min_confidence=min_confidence)
    if action:
        results = [s for s in results if s.action.value == action]
    if risk_level:
        results = [s for s in results if s.risk_level.value == risk_level]
    return {
        "count":   len(results[:limit]),
        "signals": [s.to_dict() for s in results[:limit]],
    }


# ── 工具函式 ─────────────────────────────────────────────────────────────────

async def _save_signal_task(signal, regime: str) -> None:
    """背景任務：儲存策略訊號到 signal_db"""
    try:
        sdb = get_signal_db()
        await sdb.save_signal(signal, regime=regime)
        # 高信心自動觸發警報
        if signal.confidence >= 80:
            await sdb.log_alert(
                stock_id=signal.stock_id,
                alert_type="signal_high",
                message=f"{signal.name} {signal.action.value}（信心{signal.confidence:.0f}）",
                name=signal.name,
                confidence=signal.confidence,
                action=signal.action.value,
            )
    except Exception as e:
        logger.debug(f"[quant] signal_db 儲存略過: {e}")


async def _save_feedback_task(
    report:     BacktestReport,
    stock_code: str,
    strategy:   str,
    regime:     str,
) -> None:
    """非同步 Feedback 儲存（不阻塞主回應）"""
    try:
        fb = get_feedback_engine()
        fb.record_backtest(report, stock_code=stock_code, strategy=strategy, regime=regime)
    except Exception as e:
        logger.warning(f"[quant] feedback 儲存失敗: {e}")


# ═══════════════════════════════════════════════════════════════════
#  第六批整合端點：/run_full_pipeline / /get_factor_ic / /get_regime
# ═══════════════════════════════════════════════════════════════════

class PipelineRequest(BaseModel):
    stock_code:    str = "2330"
    strategy:      str = "rule_based"
    train_days:    int = Field(120, ge=60, le=500)
    test_days:     int = Field(20,  ge=10, le=60)
    initial_capital: float = 1_000_000
    commission_discount: float = Field(0.6, ge=0.1, le=1.0)


@router.post("/run_full_pipeline", summary="完整量化流程（因子→IC→動態加權→Multi-Alpha→Walk-Forward）")
async def run_full_pipeline(req: PipelineRequest):
    """
    執行完整機構級量化流程：
      1. 抓取 K 線資料 → FeatureEngine 計算特徵
      2. FactorICEngine → 計算每個因子 IC / ICIR → 動態權重
      3. DynamicWeightEngine(adaptive) → 依盤態調整因子權重
      4. RegimeEngine v2 → 偵測市場狀態
      5. AlphaPortfolioEngine → Multi-Alpha 綜合評分
      6. RiskManagerV2 → 風控檢查
      7. WalkForwardEngine → Walk-Forward 回測（120/20）
      8. 回傳完整績效報告
    """
    # ── 1. 資料 & 特徵 ────────────────────────────────────────────────
    df      = await _fetch_kline(req.stock_code, req.start_date if hasattr(req, "start_date") else None)
    if len(df) < req.train_days + req.test_days + 20:
        raise HTTPException(422, f"資料不足（{len(df)}筆），需 {req.train_days + req.test_days + 20}")

    feat_df = _build_features(df)

    # ── 2. Factor IC ──────────────────────────────────────────────────
    ic_engine = FactorICEngine(feat_df, forward_days=5)
    factor_weights = ic_engine.get_factor_weights(DEFAULT_FACTORS)
    ic_report      = ic_engine.full_report(DEFAULT_FACTORS)

    # ── 3. Dynamic Weight（Adaptive）─────────────────────────────────
    dw_engine = DynamicWeightEngine(mode=WeightMode.ADAPTIVE)

    # ── 4. Regime v2 ─────────────────────────────────────────────────
    regime_engine = RegimeEngine()
    regime_result = regime_engine.detect(feat_df)
    regime_str    = regime_result.regime.value

    # DW 更新（傳入 regime + ic_weights）
    dw_result = dw_engine.update(
        feat_df, regime=regime_str, ic_weights=factor_weights
    )

    # ── 5. Multi-Alpha 評估 ───────────────────────────────────────────
    ap_engine = AlphaPortfolioEngine(
        ic_weights=dw_result.weights,
    )
    last = feat_df.iloc[-1]
    alpha_data = {
        "stock_id":         req.stock_code,
        "momentum_20d":     float(last.get("ret_20d", 0)) + 1,
        "foreign_buy_days": 0,
        "volume_ratio":     float(last.get("vol_ratio", 1.0) or 1.0),
        "dividend_yield":   0.0,
        "pe_ratio":         0.0,
        "eps_stability":    0.5,
        "foreign_net":      0.0,
        "trust_net":        0.0,
        "dealer_net":       0.0,
        "chip_concentration": 50.0,
        "close":            float(last.get("close", 100)),
        "ma20":             float(last.get("ma20", 0) or 0),
        "ma60":             float(last.get("ma60", 0) or 0),
        "k":                float(last.get("k", 50) or 50),
        "d":                float(last.get("d", 50) or 50),
        "boll_b":           float(last.get("boll_b", 0.5) or 0.5),
        "macd_golden":      int(last.get("macd_golden", 0) or 0),
    }
    alpha_result = ap_engine.evaluate(alpha_data, regime=regime_str)

    # ── 6. Risk check ─────────────────────────────────────────────────
    from .risk_engine import PositionSizerV2, PortfolioSnapshot, DynamicStopLoss
    stop_result = DynamicStopLoss(atr_mult=2.0, fixed_pct=0.08).calc(
        entry=float(last.get("close", 100)),
        atr=float(last.get("atr14", 0) or 0),
    )

    # ── 7. Walk-Forward 回測 ──────────────────────────────────────────
    from .backtest_engine import WalkForwardEngine
    from .alpha_model import RuleBasedAlpha

    def _signal_fn(train_df):
        alpha = RuleBasedAlpha()
        return pd.Series([alpha.evaluate(row).signal.value
                          for _, row in train_df.iterrows()])

    wf_engine = WalkForwardEngine(
        train_days=req.train_days,
        test_days=req.test_days,
        initial_capital=req.initial_capital,
        commission_discount=req.commission_discount,
    )
    try:
        wf_result = wf_engine.run(feat_df, signal_fn=_signal_fn, stop_loss_pct=0.08)
        wf_dict   = wf_result.to_dict()
        wf_summary = wf_result.summary()
    except Exception as e:
        logger.warning(f"[pipeline] WalkForward failed: {e}")
        wf_dict    = {"error": str(e)}
        wf_summary = f"Walk-Forward 失敗: {e}"

    # ── 8. 整合回傳 ───────────────────────────────────────────────────
    return {
        "stock_code":    req.stock_code,
        "pipeline_ts":   str(pd.Timestamp.now())[:19],
        "data_points":   len(feat_df),
        "regime": {
            "regime":      regime_result.regime.value,
            "sub_label":   regime_result.sub_label,
            "confidence":  regime_result.confidence,
            "position_scale": regime_result.position_scale,
            "note":        regime_result.note,
        },
        "factor_ic": {
            "valid_factors": ic_report["valid_factors"],
            "top5":          ic_report["top5"],
            "weights_snapshot": dict(list(factor_weights.items())[:8]),
        },
        "dynamic_weights": {
            "mode":     dw_result.mode.value,
            "top5":     dw_result.top5,
        },
        "alpha_portfolio": alpha_result.to_dict(),
        "risk_stop_loss":  stop_result,
        "walk_forward":    wf_dict,
        "wf_summary":      wf_summary,
    }


@router.get("/get_factor_ic", summary="取得因子 IC / ICIR 動態權重")
async def get_factor_ic(
    stock_code:   str = "2330",
    forward_days: int = 5,
):
    """
    計算指定股票的因子 IC / ICIR，套用淘汰規則後回傳動態權重字典。
    """
    df      = await _fetch_kline(stock_code)
    feat_df = _build_features(df)

    if len(feat_df) < 80:
        raise HTTPException(422, f"資料不足（{len(feat_df)}筆），需 ≥ 80")

    engine  = FactorICEngine(feat_df, forward_days=forward_days)
    report  = engine.full_report(DEFAULT_FACTORS)
    return {
        "stock_code":     stock_code,
        "forward_days":   forward_days,
        "data_points":    len(feat_df),
        **report,
    }


@router.get("/get_regime", summary="取得 Market Regime v2 盤態")
async def get_regime_v2(stock_code: str = "2330"):
    """
    使用 RegimeEngine v2 偵測盤態（bull/bear/sideways/volatile）。
    回傳策略分配建議與倉位乘數。
    """
    df      = await _fetch_kline(stock_code)
    feat_df = _build_features(df)

    engine = RegimeEngine()
    result = engine.detect(feat_df)
    return {
        "stock_code": stock_code,
        "data_points": len(feat_df),
        **result.to_dict(),
    }


@router.post("/walk_forward", summary="Walk-Forward 防過擬合回測")
async def walk_forward_backtest(req: PipelineRequest):
    """
    執行 Walk-Forward 回測，輸出每段績效 + 穩定性分析。
    """
    from .backtest_engine import WalkForwardEngine
    from .alpha_model import RuleBasedAlpha

    df      = await _fetch_kline(req.stock_code)
    feat_df = _build_features(df)

    if len(feat_df) < req.train_days + req.test_days:
        raise HTTPException(422, f"資料不足（{len(feat_df)}筆）")

    def _signal_fn(train_df):
        alpha = RuleBasedAlpha()
        return pd.Series([alpha.evaluate(row).signal.value
                          for _, row in train_df.iterrows()])

    wf = WalkForwardEngine(
        train_days=req.train_days,
        test_days=req.test_days,
        initial_capital=req.initial_capital,
        commission_discount=req.commission_discount,
    )
    try:
        result = wf.run(feat_df, signal_fn=_signal_fn, stop_loss_pct=0.08)
        d = result.to_dict()
        d["summary_text"] = result.summary()
        return d
    except Exception as e:
        raise HTTPException(422, str(e))


# ═══════════════════════════════════════════════════════════════════
#  新端點 v3：/regime  /adaptive_weights  /walkforward
# ═══════════════════════════════════════════════════════════════════

class RegimeEnhancedRequest(BaseModel):
    stock_code:           str   = "2330"
    daily_change_pct:     float = 0.0     # 今日大盤漲跌幅，例 -0.035
    foreign_futures_net:  float = 0.0     # 外資期貨淨多口（正=多）
    limit_up_count:       int   = 0       # 漲停家數
    limit_down_count:     int   = 0       # 跌停家數
    tsmc_trend:           str   = "neutral"  # "up"/"down"/"neutral"
    volume_today:         float = 0.0     # 大盤成交量（億）


class WalkForwardRequest(BaseModel):
    stock_code:           str   = "2330"
    train_days:           int   = Field(120, ge=60, le=500)
    test_days:            int   = Field(20,  ge=10, le=60)
    step_days:            int   = Field(20,  ge=5,  le=60)
    initial_capital:      float = 1_000_000
    commission_discount:  float = Field(0.6, ge=0.1, le=1.0)
    stop_loss_pct:        float = Field(0.08, ge=0.01, le=0.30)


@router.post("/regime", summary="五態市場盤態偵測（含 panic / euphoria）")
async def get_regime_enhanced(req: RegimeEnhancedRequest):
    """
    使用 EnhancedRegimeEngine 偵測五種市場盤態：
      bull / bear / sideways / panic / euphoria

    panic 條件：單日跌 > 3% 或跌停家數 > 30
    euphoria 條件：連漲 5 天 + 爆量，或漲停 > 50 家，或 20 日漲 > 15% + 爆量
    其餘：bull / bear / sideways / volatile（依 MA + 動量判斷）

    外資期貨淨多口與台積電趨勢可強化信心分數。
    """
    df      = await _fetch_kline(req.stock_code)
    feat_df = _build_features(df)

    engine = get_enhanced_regime_engine()
    result = engine.detect_enhanced(
        df=feat_df,
        daily_change_pct=req.daily_change_pct,
        foreign_futures_net=req.foreign_futures_net,
        limit_up_count=req.limit_up_count,
        limit_down_count=req.limit_down_count,
        tsmc_trend=req.tsmc_trend,
        volume_today=req.volume_today,
    )

    re_v3  = get_risk_engine_v3()
    dd_info = re_v3.update_equity(re_v3._equity)   # 不更新（只讀）

    return {
        "stock_code":  req.stock_code,
        "data_points": len(feat_df),
        **result.to_dict(),
        "strategy_hint": {
            "panic":    "超跌反彈（小倉 15%）",
            "euphoria": "降低倉位至 30%，避免追高",
            "bull":     "動能 + 突破策略，滿倉 100%",
            "bear":     "防禦 + 現金，倉位 45%",
            "sideways": "均值回歸 + 籌碼，倉位 75%",
        }.get(result.regime.value, "依盤態調整"),
    }


@router.get("/adaptive_weights", summary="30日 IC 動態因子權重")
async def get_adaptive_weights(
    stock_code:   str = "2330",
    forward_days: int = Field(5, ge=1, le=20),
    ic_window:    int = Field(30, ge=10, le=90),
):
    """
    計算最近 ic_window 日每個因子的 IC（Spearman 相關），
    IC < 0 的因子 weight = 0，其餘歸一化為 weight = IC / sum(IC)。

    每次呼叫自動更新；更新結果同步存入 JSON 快取（DB 可用時存 DB）。
    """
    df      = await _fetch_kline(stock_code)
    feat_df = _build_features(df)

    engine = AdaptiveWeightEngine(ic_window=ic_window, forward_days=forward_days)
    result = engine.compute(feat_df)

    # 非同步存檔（不阻塞回應）
    import asyncio
    asyncio.create_task(engine.save(result))

    return {
        "stock_code":  stock_code,
        "data_points": len(feat_df),
        "ic_window":   ic_window,
        "forward_days":forward_days,
        **result.to_dict(),
    }


@router.post("/walkforward", summary="Walk-Forward 防過擬合回測")
async def walkforward_analysis(req: WalkForwardRequest):
    """
    執行 Walk-Forward 回測分析（嚴格無 lookahead bias）。

    train_days = 訓練期天數（預設 120）
    test_days  = 測試期天數（預設 20）
    step_days  = 滾動步進（預設 20，等於 test_days）

    輸出每段：test_sharpe / test_win_rate / test_max_dd / generalization
    輸出總體：stability_score（穩定性）、avg_sharpe、combined_return

    穩定性分數 = 1 - std(test_sharpes) / range(test_sharpes)
    → 0~1，越接近 1 越穩定，> 0.7 為「穩定」
    """
    df      = await _fetch_kline(req.stock_code)
    feat_df = _build_features(df)

    if len(feat_df) < req.train_days + req.test_days:
        raise HTTPException(422,
            f"資料不足（{len(feat_df)} 筆），需 {req.train_days + req.test_days}")

    analyzer = WalkForwardAnalyzer(
        train_days=req.train_days,
        test_days=req.test_days,
        step_days=req.step_days,
        initial_capital=req.initial_capital,
        commission_discount=req.commission_discount,
    )

    try:
        result = analyzer.run(feat_df, stop_loss_pct=req.stop_loss_pct)
    except Exception as e:
        raise HTTPException(422, str(e))

    d = result.to_dict()
    d["stock_code"]    = req.stock_code
    d["summary_text"]  = result.summary()
    return d


# ═══════════════════════════════════════════════════════════════════
#  Alpha Research Platform 端點
# ═══════════════════════════════════════════════════════════════════

class AlphaUpdateRequest(BaseModel):
    updates: list[dict] = Field(
        ..., example=[{"alpha": "momentum", "ic": 0.032},
                      {"alpha": "value",    "ic": -0.015}]
    )

class MonteCarloRequest(BaseModel):
    trades:          list[float]
    is_return:       bool  = True
    n_sims:          int   = Field(1000, ge=100, le=5000)
    initial_capital: float = 1_000_000
    generate_chart:  bool  = True


@router.get("/alpha_registry", summary="Alpha 因子狀態登記表")
async def get_alpha_registry_status():
    """取得所有 Alpha 的狀態、IC、權重。DEAD Alpha 自動標記。"""
    reg = get_alpha_registry()
    await reg.load()
    return {
        "summary": reg.summary(),
        "alphas":  [r.to_dict() for r in reg.get_all()],
        "weights": reg.get_weights(),
    }


@router.post("/alpha_registry/update", summary="批次更新 Alpha IC")
async def update_alpha_ic(req: AlphaUpdateRequest):
    """批次更新多個 Alpha 的當日 IC，觸發狀態機與權重計算。"""
    reg = get_alpha_registry()
    await reg.load()
    changed: list[dict] = []
    for item in req.updates:
        name = item.get("alpha", "")
        ic   = float(item.get("ic", 0))
        if name:
            rec = reg.update_ic(name, ic)
            changed.append(rec.to_dict())
    weights = reg.get_weights()
    await reg.save()
    return {
        "updated": len(changed),
        "changed": changed,
        "weights": weights,
        "summary": reg.summary(),
    }


@router.post("/montecarlo", summary="蒙地卡羅回測模擬")
async def run_montecarlo(req: MonteCarloRequest):
    """
    輸入交易損益列表，執行蒙地卡羅模擬 N 次。
    is_return=True  → 傳入報酬率（如 0.05 = +5%）
    is_return=False → 傳入損益金額（如 50000 = +5萬）
    """
    if not req.trades:
        raise HTTPException(422, "trades list is empty")
    engine = MonteCarloEngine(n_sims=req.n_sims, initial_capital=req.initial_capital)
    result = engine.run(req.trades, is_return=req.is_return)

    img_url = None
    if req.generate_chart:
        path = engine.generate_chart(result)
        if path:
            base_url = os.getenv("BASE_URL", "")
            if base_url:
                img_url = f"{base_url.rstrip('/')}/static/reports/{path.name}"

    return {**result.to_dict(), "chart_url": img_url}


@router.get("/sentiment", summary="族群情緒 Buzz Score")
async def get_sector_sentiment(
    days: int  = 3,
    sector: Optional[str] = None,
):
    """
    分析近 N 日新聞 + PTT Stock 板的族群情緒。
    輸出：sentiment / buzz（LOW/MEDIUM/HIGH/VIRAL）/ signal（BULLISH/BEARISH/NEUTRAL）
    """
    engine = get_sentiment_engine()
    if sector:
        r = await engine.analyze_sector(sector, days=days)
        if r is None:
            raise HTTPException(404, f"Unknown sector: {sector}")
        return r.to_dict()

    results = await engine.analyze_all(days=days)
    return {
        "days":     days,
        "count":    len(results),
        "sectors":  [r.to_dict() for r in results],
        "top_bullish": [r.to_dict() for r in engine.get_top_bullish(results)],
        "top_bearish": [r.to_dict() for r in engine.get_top_bearish(results)],
    }


@router.get("/risk_isolation", summary="策略獨立資金池狀態")
async def get_risk_isolation_status():
    """取得各策略資金池回撤狀態與倉位縮減情況。"""
    iso = get_risk_isolation()
    return {
        "pools":       iso.get_all_states(),
        "warn_dd_pct": iso._warn_dd * 100,
        "clear_dd_pct":iso._clear_dd * 100,
    }


# ── 將 router 掛到 app（獨立啟動時）─────────────────────────────────────────

app.include_router(router)


# ── 整合測試（python quant/main.py）────────────────────────────────────────

async def _integration_test():
    """
    整合測試：以 mock 資料繞過真實 API，驗證所有模組串接正確。
    不啟動 HTTP server，直接呼叫端點函式。
    """
    # 注意：python -m quant.main 時 __main__ != quant.main，
    # monkey-patch 必須同時修改兩個模組的 _fetch_kline。
    import sys, tempfile
    from pathlib import Path
    import quant.feedback_engine as _fb_mod

    # 取得「真正被 run_backtest 使用」的模組（可能是 __main__ 或 quant.main）
    _main_mod = sys.modules.get("__main__")
    _pkg_mod  = sys.modules.get("quant.main")

    async def _mock_fetch(stock_code, start_date=None):
        return _mock_kline(stock_code, n=400)

    # patch 兩個可能的模組
    _orig_main = getattr(_main_mod, "_fetch_kline", None)
    _orig_pkg  = getattr(_pkg_mod,  "_fetch_kline", None) if _pkg_mod else None
    if _main_mod: _main_mod._fetch_kline = _mock_fetch
    if _pkg_mod:  _pkg_mod._fetch_kline  = _mock_fetch

    # ── 使用獨立暫存 feedback 檔案
    tmp_fb   = Path(tempfile.mktemp(suffix="_test_fb.json"))
    _orig_fb = _fb_mod._global_feedback
    _fb_mod._global_feedback = FeedbackEngine(store_path=tmp_fb)

    try:
        print("=" * 55)
        print(" quant/main.py 整合測試（mock 資料）")
        print("=" * 55)

        # ── /health ──────────────────────────────────────────
        h = await health()
        print(f"\n[health] status={h['status']}  lgb={h['lgb_available']}  "
              f"feedback_records={h['feedback_records']}")
        for mod, st in h["modules"].items():
            print(f"  {mod:20s}: {st}")

        # ── /get_signals ──────────────────────────────────────
        print("\n[get_signals] 2330...")
        sig = await get_signals("2330", chip_days=3, foreign_net=500)
        print(f"  訊號={sig['signal']}  評分={sig['score']}  "
              f"盤態={sig['regime']['current']}({sig['regime']['description']})")
        print(f"  停損={sig['risk']['stop_loss']}  停利={sig['risk']['take_profit']}")
        print(f"  推薦策略={sig['regime']['recommended']}")
        print(f"  理由: {'; '.join(sig['reasons'][:3]) if sig['reasons'] else '無'}")

        # ── /run_backtest（rule_based）────────────────────────
        print("\n[run_backtest] 2330 × rule_based（停損 8%，停利 20%）...")
        req_bt = BacktestRequest(
            stock_code="2330",
            strategy="rule_based",
            stop_loss_pct=0.08,
            take_profit_pct=0.20,
            save_feedback=True,   # 會排程背景任務
        )
        bt = await run_backtest(req_bt)
        await asyncio.sleep(0)    # 讓背景 feedback 任務執行一個 tick
        print(f"  總報酬={bt['total_return']*100:+.2f}%  年化={bt['annual_return']*100:+.2f}%")
        print(f"  夏普={bt['sharpe_ratio']:.3f}  回撤={bt['max_drawdown']*100:.2f}%  勝率={bt['win_rate']*100:.1f}%")
        print(f"  交易={bt['n_trades']} 筆  成本占比={bt['cost_impact_pct']*100:.3f}%")
        print(f"  盤態={bt['regime']['current']}  {bt['regime']['tip']}")

        # ── 多策略比較 ────────────────────────────────────────
        print("\n[run_backtest] 策略比較（2330 × 400 日 mock）:")
        for strat in ["ma_cross", "rsi", "macd", "momentum", "bollinger"]:
            req_s = BacktestRequest(
                stock_code="2330", strategy=strat,
                stop_loss_pct=0.08, save_feedback=True,
            )
            r = await run_backtest(req_s)
            print(f"  {strat:15s}  報酬={r['total_return']*100:+.2f}%  "
                  f"夏普={r['sharpe_ratio']:+.3f}  勝率={r['win_rate']*100:.1f}%  "
                  f"交易={r['n_trades']}")

        # ── /get_portfolio（直接呼叫引擎，繞過 gather）──────────
        print("\n[get_portfolio] 5 檔股票最佳化（mock 資料）...")
        holdings = [
            {"code": "2330", "sector": "半導體", "name": "台積電"},
            {"code": "2412", "sector": "電信",   "name": "中華電"},
            {"code": "2317", "sector": "電子",   "name": "鴻海"},
            {"code": "2881", "sector": "金融",   "name": "富邦金"},
            {"code": "6505", "sector": "石化",   "name": "台塑化"},
        ]
        codes   = [h["code"] for h in holdings]
        sectors = {h["code"]: h.get("sector", "其他") for h in holdings}
        mock_prices = {
            code: _mock_kline(code, n=400)["close"].reset_index(drop=True)
            for code in codes
        }
        pe     = PortfolioEngine()
        pf_res = pe.optimize(mock_prices, sectors=sectors, objective="max_sharpe")
        print(f"  最佳化方法: {pf_res.method}")
        print(f"  年化報酬={pf_res.expected_ret*100:.2f}%  波動={pf_res.volatility*100:.2f}%  夏普={pf_res.sharpe:.3f}")
        print(f"  VaR(95%)={pf_res.var_95*100:.2f}%  CVaR={pf_res.cvar_95*100:.2f}%")
        print("  最佳權重：")
        for code, w in sorted(pf_res.weights.items(), key=lambda x: -x[1]):
            print(f"    {code}: {w*100:.1f}%  [{sectors[code]}]")
        print(f"  產業權重: {', '.join(f'{k}={v*100:.1f}%' for k,v in pf_res.sector_weights.items())}")
        if pf_res.warnings:
            print(f"  警告: {pf_res.warnings}")

        # ── /get_performance ──────────────────────────────────
        print("\n[get_performance] Feedback 績效總覽...")
        perf = await get_performance()
        print(f"  歷史記錄總數: {perf['total_records']}")
        print("  各盤態最佳策略:")
        for regime, best in perf["regime_best"].items():
            print(f"    {regime:8s} → {best}")
        if perf["stats"]:
            print("  策略統計（前 5）:")
            for s in perf["stats"][:5]:
                print(f"    {s['strategy']:15s}[{s['regime']:8s}] "
                      f"n={s['n_records']} 夏普={s['avg_sharpe']:+.2f} "
                      f"勝率={s['avg_win_rate']:.1f}% 建議={s['recommendation']}")

        # ── /adjust_weights ───────────────────────────────────
        print("\n[adjust_weights] 自動調整權重...")
        adj = await adjust_weights()
        print(f"  調整 {adj['changed']} 項策略權重")
        for c in adj["changes"]:
            print(f"    {c['strategy']:15s}({c['regime']}): {c['old']:.3f} → {c['new']:.3f}")
        if not adj["changes"]:
            print("  （記錄數不足，等下週累積更多回測後調整）")

        print("\n" + "=" * 55)
        print(" 整合測試完成")
        print("=" * 55)

    finally:
        if _main_mod and _orig_main: _main_mod._fetch_kline = _orig_main
        if _pkg_mod  and _orig_pkg:  _pkg_mod._fetch_kline  = _orig_pkg
        _fb_mod._global_feedback = _orig_fb
        tmp_fb.unlink(missing_ok=True)


if __name__ == "__main__":
    import asyncio
    asyncio.run(_integration_test())
