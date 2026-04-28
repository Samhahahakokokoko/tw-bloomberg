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
  main.py             — FastAPI：/run_backtest /get_signals /get_portfolio /get_performance
"""

from .feature_engine import FeatureEngine
from .alpha_model import AlphaModel, RuleBasedAlpha
from .execution_engine import ExecutionEngine, Order, OrderSide, OrderStatus
from .database import QuantDB
from .risk_engine import RiskEngine, MarketRegime, DrawdownState
from .portfolio_engine import PortfolioEngine, PortfolioResult
from .backtest_engine import BacktestEngine, BacktestReport
from .feedback_engine import FeedbackEngine, get_feedback_engine

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
]
