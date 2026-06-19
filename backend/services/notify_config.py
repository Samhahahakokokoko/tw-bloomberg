"""通知推播控制 — 各推播任務的開關設定

所有選配推播任務預設關閉（disabled）。
可透過 LINE 指令 /notify on|off <job_id> 或 /notify list 管理。
"""
from __future__ import annotations
import json
from pathlib import Path
from loguru import logger

_CONFIG_PATH = Path("/tmp/tw_bloomberg_notify.json")

# job_id → 顯示名稱（用於 /notify list）
NOTIFY_LABELS: dict[str, str] = {
    # 盤前推播
    "morning_report":           "08:30 早報",
    "industry_news_morning":    "08:20 產業新聞早報",
    "premarket_brief":          "08:10 盤前簡報",
    "daily_trade_plan":         "08:35 每日交易計畫",
    "daily_qa_push":            "08:00 每日問答",
    "wisdom_push":              "08:05 每日智慧",
    "conference_reminder":      "08:05 法說會提醒",
    "opex_alert":               "08:32 選擇權結算提醒",
    "ai_feed":                  "08:31 AI Feed",
    "youtube_morning_check":    "08:00 YouTube分析師晨報",
    "earnings_reminder":        "08:15 財報提醒",
    "macro_weekly":             "週一08:05 總經週報",
    "investor_meetings_weekly": "週一08:10 法說會日曆",
    "ai_contest_pick":          "週一07:00 AI選股競賽",
    # 盤中推播
    "market_scan":              "10/12/14點 市場掃描",
    "dashboard_snapshot":       "盤中多空儀表板",
    "pcr_alert":                "盤中PCR警報",
    "black_swan_alert":         "黑天鵝預警",
    "vix_alert":                "VIX警報",
    "midday_push_1030":         "10:30 盤中解說",
    "midday_push_1300":         "13:00 盤中解說",
    "morning_alert_flush":      "12:00 上午警報彙整",
    "smart_alert_v2":           "盤中智慧警報",
    "price_zone_alert":         "盤中股價預警區間",
    "pair_monitor_alerts":      "盤中配對監控警報",
    "breaking_news_push":       "即時新聞快訊",
    # 收盤後推播
    "chip_alerts":              "15:32 籌碼異動警報",
    "afternoon_alert_flush":    "15:30 下午警報彙整",
    "techrating_update":        "15:30 技術評級更新",
    "sector_strength_push":     "15:05 產業強度排行",
    "inst_detail_push":         "15:30 法人明細",
    "margin_alert":             "16:00 融資概況",
    "deep_review_push":         "16:30 盤後深度覆盤",
    "analyst_alert_check":      "16:30 分析師觀點警報",
    "euphoria_stress_push":     "17:00 過熱壓力報告",
    "autonomous_research":      "17:30 自主研究報告",
    "sector_heatmap":           "18:30 產業熱力圖",
    "smart_money":              "18:30 聰明錢訊號",
    "smart_money_v2":           "19:00 聰明錢v2",
    "watchlist_daily":          "19:00 自選股日報",
    "portfolio_overlay":        "19:00 投組健康報告",
    "agent_c_decision":         "19:00 AI決策員報告",
    "daily_advice":             "19:30 AI操作建議",
    "daily_decision":           "19:30 每日決策報告",
    "group_report":             "19:30 群組選股報告",
    "portfolio_manager_advice": "19:30 AI投組建議",
    "agent_report":             "19:30 AI Agent決策",
    "analyst_consensus_push":   "20:00 分析師共識報告",
    "narrative_map_push":       "20:00 市場敘事地圖",
    "ai_debate_push":           "20:30 AI多空辯論",
    "committee_batch":          "20:30 委員會批次決議",
    "diary_report":             "21:00 操盤日記",
    # 週/月推播
    "weekly_report":            "週五14:30 週報",
    "weekly_picks":             "週五15:00 週選股",
    "weekly_picks_push":        "週五15:30 精選推播",
    "friday_summary":           "週五15:00 績效摘要",
    "ai_contest_score":         "週五15:35 AI競賽評分",
    "meta_alpha_weekly":        "週五18:30 Meta Alpha週報",
    "mistake_detector_weekly":  "週五18:00 錯誤偵測週報",
    "prediction_market_weekly": "週五19:00 預測市場",
    "weekplan_push":            "週日20:00 週策略報告",
    "monthly_report_check":     "月末 績效月報",
    # 盤前選股（保留但可關）
    "morning_picks":            "08:30 盤前選股表",
}

_state: dict[str, bool] = {}


def _load() -> None:
    global _state
    try:
        if _CONFIG_PATH.exists():
            _state = json.loads(_CONFIG_PATH.read_text())
    except Exception as e:
        logger.debug(f"[notify_config] load failed: {e}")
        _state = {}


def _save() -> None:
    try:
        _CONFIG_PATH.write_text(json.dumps(_state))
    except Exception as e:
        logger.debug(f"[notify_config] save failed: {e}")


def is_push_enabled(job_id: str) -> bool:
    """查詢某推播任務是否已啟用（預設 False）"""
    return _state.get(job_id, False)


def set_push_enabled(job_id: str, enabled: bool) -> bool:
    """設定推播任務開關，回傳是否為已知 job_id"""
    if job_id not in NOTIFY_LABELS:
        return False
    _state[job_id] = enabled
    _save()
    return True


def list_config() -> dict[str, dict]:
    """回傳所有推播任務的狀態"""
    return {
        job_id: {
            "enabled": _state.get(job_id, False),
            "label":   label,
        }
        for job_id, label in NOTIFY_LABELS.items()
    }


# ── 安靜模式（Quiet Mode）────────────────────────────────────────────────────
# 儲存在 _state["__quiet_until__"]，值為 ISO 時間字串

_QUIET_KEY = "__quiet_until__"


def set_quiet_mode(hours: int = 24) -> "datetime":
    """啟用安靜模式，hours 小時後自動恢復。回傳恢復時間。"""
    from datetime import datetime, timedelta
    until = datetime.now() + timedelta(hours=hours)
    _state[_QUIET_KEY] = until.isoformat()
    _save()
    return until


def clear_quiet_mode() -> None:
    """立即關閉安靜模式"""
    _state.pop(_QUIET_KEY, None)
    _save()


def is_quiet_mode() -> bool:
    """目前是否處於安靜模式（未到期）"""
    from datetime import datetime
    ts = _state.get(_QUIET_KEY)
    if not ts:
        return False
    try:
        until = datetime.fromisoformat(ts)
        if datetime.now() >= until:
            _state.pop(_QUIET_KEY, None)
            _save()
            return False
        return True
    except Exception:
        return False


def get_quiet_until() -> "datetime | None":
    """回傳安靜模式到期時間，若未啟用回傳 None"""
    from datetime import datetime
    ts = _state.get(_QUIET_KEY)
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


_load()
