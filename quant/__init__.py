"""
quant/ — 台股 AI 量化交易核心模組

模組架構：
  feature_engine.py   — 技術特徵計算（MA / RSI / MACD / KD / Bollinger / ATR …）
  alpha_model.py      — 規則型 Alpha + LightGBM 預測模型
  execution_engine.py — 下單 / 倉位 / 風控管理（含台股真實成本）
  database.py         — 獨立 PostgreSQL schema（stocks/prices/features/predictions/trades）
  risk_engine.py      — 盤態偵測（Regime）+ 回撤控制 + VaR
  portfolio_engine.py — 馬可維茲最佳化 + 個股/產業權重約束
  backtest_engine.py  — 完整回測（手續費/交易稅/滑價/漲跌停/成交量限制）
  feedback_engine.py  — 回饋學習：記錄回測結果 + 自動調整策略/特徵權重
  strategy_engine.py  — 多策略選股（動能/價值/籌碼）+ 信心指數 + 買賣建議
  confidence_engine.py — 信心指數三源合成（回測30%+模型40%+訊號30%）
  odd_lot_engine.py   — 零股投資計算（手續費/損平/預算分配/定期定額）
  signal_db.py        — 新增 DB 表：strategy_signals / user_settings / alerts_log
  main.py             — FastAPI：所有量化端點（回測/訊號/組合/策略/零股/比較）
"""

from .feature_engine import FeatureEngine
from .alpha_model import AlphaModel, RuleBasedAlpha
from .execution_engine import ExecutionEngine, Order, OrderSide, OrderStatus
from .database import QuantDB
from .risk_engine import RiskEngine, MarketRegime, DrawdownState
from .portfolio_engine import PortfolioEngine, PortfolioResult
from .backtest_engine import BacktestEngine, BacktestReport
from .feedback_engine import FeedbackEngine, get_feedback_engine
from .strategy_engine import StrategyEngine, StrategySignal, Action, RiskLevel
from .confidence_engine import ConfidenceEngine, ConfidenceBreakdown
from .odd_lot_engine import OddLotEngine, OddLotResult
from .signal_db import SignalDB, get_signal_db

__all__ = [
    "FeatureEngine",
    "AlphaModel",
    "RuleBasedAlpha",
    "ExecutionEngine",
    "Order",
    "OrderSide",
    "OrderStatus",
    "QuantDB",
    "RiskEngine",
    "MarketRegime",
    "DrawdownState",
    "PortfolioEngine",
    "PortfolioResult",
    "BacktestEngine",
    "BacktestReport",
    "FeedbackEngine",
    "get_feedback_engine",
    "StrategyEngine",
    "StrategySignal",
    "Action",
    "RiskLevel",
    "ConfidenceEngine",
    "ConfidenceBreakdown",
    "OddLotEngine",
    "OddLotResult",
    "SignalDB",
    "get_signal_db",
]
