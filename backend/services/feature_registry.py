"""
feature_registry.py — 功能清單與路由定義

每個功能記錄：
  name: 顯示名稱（含 emoji）
  line: 是否在 LINE Bot 可用
  web:  是否在 Web 界面可用
"""

from __future__ import annotations

FEATURES: dict[str, dict] = {
    "backtest":  {"name": "📈 策略回測",  "line": True,  "web": True},
    "risk":      {"name": "🛡️ 風控分析",  "line": True,  "web": True},
    "portfolio": {"name": "💼 投資組合",  "line": True,  "web": True},
    "screener":  {"name": "📊 選股系統",  "line": True,  "web": True},
    "news":      {"name": "📰 市場新聞",  "line": True,  "web": True},
    "ai":        {"name": "🤖 AI 分析",   "line": True,  "web": True},
    "strategy":  {"name": "🎯 策略管理",  "line": True,  "web": True},
    "odd_lot":   {"name": "🔢 零股計算",  "line": True,  "web": True},
    "alert":     {"name": "🔔 警報設定",  "line": True,  "web": True},
    "ranking":   {"name": "🏆 績效排行",  "line": True,  "web": True},
    "report":    {"name": "📋 選股報告",  "line": True,  "web": True},
    "compare":   {"name": "⚖️ 比較分析",  "line": True,  "web": True},
    "walkforward": {"name": "🔬 Walk-Forward", "line": False, "web": True},
    "factor_ic": {"name": "⚗️ 因子 IC",   "line": False, "web": True},
}


def is_line_enabled(feature: str) -> bool:
    return FEATURES.get(feature, {}).get("line", False)


def is_web_enabled(feature: str) -> bool:
    return FEATURES.get(feature, {}).get("web", False)


def get_line_features() -> list[dict]:
    """回傳所有 LINE Bot 可用功能列表"""
    return [{"key": k, **v} for k, v in FEATURES.items() if v.get("line")]


def get_feature_name(feature: str) -> str:
    return FEATURES.get(feature, {}).get("name", feature)
