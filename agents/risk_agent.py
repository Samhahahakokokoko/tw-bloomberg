"""risk_agent.py — AI 風控員（擁有否決權）"""
from __future__ import annotations
import logging
from datetime import datetime
from .base_agent import AgentVote

logger = logging.getLogger(__name__)
AGENT_NAME = "風控Agent"

# 否決觸發條件
VETO_CONDITIONS = [
    ("kill_switch_active",   "Kill Switch 啟動中"),
    ("low_data_quality",     "系統資料品質不足（< 0.6）"),
    ("euphoria_extreme",     "市場極度過熱（Euphoria > 90）"),
    ("stress_crisis",        "市場壓力危機（Stress > 80）"),
    ("mock_data_detected",   "偵測到假資料混入決策"),
]


async def run(stock_id: str = "", sector: str = "") -> AgentVote:
    veto = False
    veto_reasons: list[str] = []
    warnings: list[str] = []
    data_quality = 0.9

    try:
        # Kill Switch
        from quant.risk_kill_switch import is_trading_enabled, status_dict
        if not is_trading_enabled():
            ks = status_dict()
            veto = True
            veto_reasons.append(f"Kill Switch：{ks.get('reason', '未知')}")

        # 過熱檢查
        try:
            from quant.euphoria_engine import compute_euphoria
            eu = await compute_euphoria()
            if eu.euphoria_score > 90:
                veto = True
                veto_reasons.append(f"Euphoria 極度過熱（{eu.euphoria_score:.0f}）")
            elif eu.euphoria_score > 75:
                warnings.append(f"Euphoria 偏高（{eu.euphoria_score:.0f}），請謹慎")
        except Exception:
            pass

        # 壓力測試
        try:
            from quant.stress_engine import compute_stress
            stress = await compute_stress()
            if stress.stress_score > 80:
                veto = True
                veto_reasons.append(f"市場壓力危機（{stress.stress_score:.0f}）")
            elif stress.stress_score > 60:
                warnings.append(f"壓力指數偏高（{stress.stress_score:.0f}）")
        except Exception:
            pass

        # 資料品質
        try:
            from quant.system_health_dashboard import collect_health
            health = await collect_health()
            if health.global_data_quality < 0.6:
                veto = True
                veto_reasons.append(f"資料品質不足（{health.global_data_quality:.0%}）")
                data_quality = health.global_data_quality
        except Exception:
            pass

    except Exception as e:
        logger.warning("[RiskAgent] check failed: %s", e)
        warnings.append(f"風控檢查部分失敗：{type(e).__name__}")

    if veto:
        return AgentVote(
            AGENT_NAME, "bearish", 1.0,
            veto_reasons,
            veto=True,
            veto_reason=veto_reasons[0] if veto_reasons else "風控否決",
            data_quality=data_quality,
        )

    reasons = ["✅ 無重大風險"] + warnings
    return AgentVote(AGENT_NAME, "neutral", 0.8, reasons, data_quality=data_quality)
