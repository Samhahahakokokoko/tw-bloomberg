"""
feature_registry.py — 功能開關統一管理

每個功能記錄：
  name:         顯示名稱（含 emoji）
  line_enabled: LINE Bot 是否開放
  web_enabled:  Web 界面是否開放
  beta:         Beta 功能標記（上線但不穩定）
  description:  功能說明
"""
from __future__ import annotations

FEATURES: dict[str, dict] = {
    # ── 核心功能 ──────────────────────────────────────────────────────────────
    "portfolio":    {"name": "💼 投資組合",   "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "庫存管理、損益計算"},
    "news":         {"name": "📰 市場新聞",   "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "財經新聞爬蟲 + 情緒分析"},
    "screener":     {"name": "📊 選股系統",   "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "多維度選股引擎"},
    "ai":           {"name": "🤖 AI 分析",    "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "Claude AI 股票分析"},
    "alert":        {"name": "🔔 警報設定",   "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "到價/漲跌幅通知"},

    # ── 量化功能 ──────────────────────────────────────────────────────────────
    "backtest":     {"name": "📈 策略回測",   "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "近3個月策略績效回測"},
    "risk":         {"name": "🛡️ 風控分析",   "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "VaR、集中度、市場狀態"},
    "walkforward":  {"name": "🔬 Walk-Forward","line_enabled": False, "web_enabled": True,  "beta": False, "description": "防過擬合滾動回測"},
    "montecarlo":   {"name": "🎲 蒙地卡羅",   "line_enabled": True,  "web_enabled": True,  "beta": True,  "description": "1000次隨機交易模擬"},
    "factor_ic":    {"name": "⚗️ 因子 IC",    "line_enabled": False, "web_enabled": True,  "beta": False, "description": "因子有效性分析"},
    "alpha_registry": {"name": "🧬 Alpha 登記", "line_enabled": False, "web_enabled": True, "beta": True, "description": "Alpha 狀態 + 生死管理"},

    # ── 情緒與族群 ────────────────────────────────────────────────────────────
    "sentiment":    {"name": "💬 族群情緒",   "line_enabled": True,  "web_enabled": True,  "beta": True,  "description": "新聞 + PTT Buzz Score"},

    # ── 策略管理 ──────────────────────────────────────────────────────────────
    "strategy":     {"name": "🎯 策略管理",   "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "啟用/停用策略 + 動態權重"},
    "odd_lot":      {"name": "🔢 零股計算",   "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "零股試算"},
    "ranking":      {"name": "🏆 績效排行",   "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "AI 推薦準確率統計"},
    "report":       {"name": "📋 選股報告",   "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "族群連動選股圖表"},
    "compare":      {"name": "⚖️ 比較分析",   "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "多股績效比較"},

    # ── 工具 ──────────────────────────────────────────────────────────────────
    "optimize":     {"name": "📐 投組最佳化", "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "馬可維茲最佳配置"},
    "morning_report": {"name": "🌅 今日早報", "line_enabled": True,  "web_enabled": True,  "beta": False, "description": "每日市場摘要"},
}


def is_line_enabled(feature: str) -> bool:
    return FEATURES.get(feature, {}).get("line_enabled", False)


def is_web_enabled(feature: str) -> bool:
    return FEATURES.get(feature, {}).get("web_enabled", False)


def is_beta(feature: str) -> bool:
    return FEATURES.get(feature, {}).get("beta", False)


def get_line_features() -> list[dict]:
    """回傳所有 LINE Bot 可用功能列表"""
    return [{"key": k, **v} for k, v in FEATURES.items() if v.get("line_enabled")]


def get_web_features() -> list[dict]:
    """回傳所有 Web 可用功能列表"""
    return [{"key": k, **v} for k, v in FEATURES.items() if v.get("web_enabled")]


def get_beta_features() -> list[dict]:
    """回傳所有 Beta 功能"""
    return [{"key": k, **v} for k, v in FEATURES.items() if v.get("beta")]


def get_feature_name(feature: str) -> str:
    return FEATURES.get(feature, {}).get("name", feature)


def get_all_as_list() -> list[dict]:
    return [{"key": k, **v} for k, v in FEATURES.items()]
