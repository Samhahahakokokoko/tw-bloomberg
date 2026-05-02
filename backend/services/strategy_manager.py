"""
strategy_manager.py — 用戶策略管理服務

功能：
  1. 取得用戶策略設定（開關狀態 + 權重）
  2. 切換單一策略開關（toggle）
  3. 套用預設組合（conservative / balanced / aggressive）
  4. 查看策略近 30 日績效（從 recommendation_results 統計）
  5. 生成策略管理 Flex Message（供 LINE Bot 顯示）

預設策略組合：
  保守型：value 2.0 / defensive 2.0 / chip 0.8 / momentum 0.3 / breakout 0.3
  穩健型：各策略等權（1.0）
  積極型：momentum 2.0 / breakout 2.0 / chip 1.5 / value 0.5 / defensive 0.3
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.models import StrategySetting

logger = logging.getLogger(__name__)

# ── 常數 ─────────────────────────────────────────────────────────────────────

ALL_STRATEGIES = ["momentum", "value", "chip", "breakout", "defensive"]

STRATEGY_LABELS = {
    "momentum":  "⚡ 動能",
    "value":     "💰 存股",
    "chip":      "🏛 籌碼",
    "breakout":  "🚀 突破",
    "defensive": "🛡 防禦",
}

STRATEGY_DESC = {
    "momentum":  "追蹤強勢股與技術突破訊號",
    "value":     "高股息、低本益比、穩健成長",
    "chip":      "外資 / 投信大量買進",
    "breakout":  "價量突破、壓力位攻破",
    "defensive": "低波動、存股型，空頭保護",
}

STRATEGY_PRESETS = {
    "conservative": {
        "momentum": 0.3, "value": 2.0, "chip": 0.8,
        "breakout": 0.3, "defensive": 2.0,
    },
    "balanced": {
        "momentum": 1.0, "value": 1.0, "chip": 1.0,
        "breakout": 1.0, "defensive": 1.0,
    },
    "aggressive": {
        "momentum": 2.0, "value": 0.5, "chip": 1.5,
        "breakout": 2.0, "defensive": 0.3,
    },
}

PRESET_LABELS = {
    "conservative": "🐢 保守型",
    "balanced":     "⚖️ 穩健型",
    "aggressive":   "🦁 積極型",
}


# ── 資料庫操作 ────────────────────────────────────────────────────────────────

async def get_settings(db: AsyncSession, user_id: str) -> dict[str, dict]:
    """
    取得用戶所有策略設定。
    若無設定則自動建立預設（balanced）。
    回傳：{strategy: {enabled, weight, preset}}
    """
    result = await db.execute(
        select(StrategySetting).where(StrategySetting.user_id == user_id)
    )
    rows = result.scalars().all()

    existing = {r.strategy: r for r in rows}

    # 補充缺少的策略（使用預設 balanced）
    changed = False
    for name in ALL_STRATEGIES:
        if name not in existing:
            s = StrategySetting(
                user_id=user_id,
                strategy=name,
                enabled=True,
                weight=1.0,
                preset="balanced",
            )
            db.add(s)
            existing[name] = s
            changed = True

    if changed:
        await db.commit()

    return {
        name: {
            "enabled": row.enabled,
            "weight":  row.weight,
            "preset":  row.preset,
        }
        for name, row in existing.items()
    }


async def toggle_strategy(
    db: AsyncSession, user_id: str, strategy: str
) -> tuple[bool, str]:
    """
    切換策略開關。回傳 (新狀態, 訊息)。
    """
    if strategy not in ALL_STRATEGIES:
        return False, f"❌ 未知策略：{strategy}"

    result = await db.execute(
        select(StrategySetting).where(
            StrategySetting.user_id == user_id,
            StrategySetting.strategy == strategy,
        )
    )
    row = result.scalar_one_or_none()

    if not row:
        row = StrategySetting(user_id=user_id, strategy=strategy, enabled=False)
        db.add(row)

    row.enabled    = not row.enabled
    row.updated_at = datetime.utcnow()
    await db.commit()

    label = STRATEGY_LABELS.get(strategy, strategy)
    state = "✅ 已開啟" if row.enabled else "⛔ 已關閉"
    return row.enabled, f"{label} {state}"


async def apply_preset(
    db: AsyncSession, user_id: str, preset: str
) -> str:
    """
    套用預設組合。回傳確認訊息。
    """
    if preset not in STRATEGY_PRESETS:
        return f"❌ 未知預設：{preset}"

    weights = STRATEGY_PRESETS[preset]
    result  = await db.execute(
        select(StrategySetting).where(StrategySetting.user_id == user_id)
    )
    existing = {r.strategy: r for r in result.scalars().all()}

    for name, w in weights.items():
        if name in existing:
            existing[name].weight     = w
            existing[name].enabled    = w > 0
            existing[name].preset     = preset
            existing[name].updated_at = datetime.utcnow()
        else:
            db.add(StrategySetting(
                user_id=user_id, strategy=name,
                enabled=w > 0, weight=w, preset=preset,
            ))

    await db.commit()
    return PRESET_LABELS.get(preset, preset)


# ── 績效統計 ──────────────────────────────────────────────────────────────────

async def get_strategy_performance(
    db: AsyncSession,
    strategy: str,
    days: int = 30,
) -> dict:
    """
    從 recommendation_results 取得策略近 N 日績效。
    若表不存在或無資料，回傳 mock 統計。
    """
    try:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        from sqlalchemy import text
        r = await db.execute(text("""
            SELECT
              COUNT(*)                                        AS n,
              AVG(CASE WHEN actual_return > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
              AVG(actual_return)                              AS avg_return,
              MAX(actual_return)                              AS max_return,
              MIN(actual_return)                              AS min_return
            FROM recommendation_results
            WHERE strategy = :s AND recommend_date >= :c
              AND actual_return IS NOT NULL
        """), {"s": strategy, "c": cutoff})
        row = r.fetchone()
        if row and row[0]:
            return {
                "strategy":   strategy,
                "n":          int(row[0]),
                "win_rate":   round(float(row[1] or 0) * 100, 1),
                "avg_return": round(float(row[2] or 0) * 100, 2),
                "max_return": round(float(row[3] or 0) * 100, 2),
                "min_return": round(float(row[4] or 0) * 100, 2),
                "days":       days,
            }
    except Exception as e:
        logger.debug("[strategy_mgr] perf query failed: %s", e)

    # Mock fallback（無歷史資料時）
    import hashlib
    seed = int(hashlib.md5(strategy.encode()).hexdigest()[:8], 16)
    import random; rng = random.Random(seed)
    return {
        "strategy":   strategy,
        "n":          rng.randint(10, 50),
        "win_rate":   round(rng.uniform(45, 68), 1),
        "avg_return": round(rng.uniform(-0.5, 3.5), 2),
        "max_return": round(rng.uniform(3.0, 12.0), 2),
        "min_return": round(rng.uniform(-8.0, -1.0), 2),
        "days":       days,
        "mock":       True,
    }


# ── LINE Flex Message 生成 ────────────────────────────────────────────────────

def build_strategy_menu_flex(settings: dict[str, dict]) -> dict:
    """
    生成策略管理 Flex Message（Bubble）。
    每個策略顯示：名稱 / 開關狀態 / 權重 / 點擊可 toggle。
    """
    def _row(name: str, cfg: dict) -> dict:
        label  = STRATEGY_LABELS.get(name, name)
        on     = cfg.get("enabled", True)
        weight = cfg.get("weight", 1.0)
        status = "🟢 開啟" if on else "⚫ 關閉"
        return {
            "type": "box",
            "layout": "horizontal",
            "paddingAll": "8px",
            "backgroundColor": "#0D1A2D" if on else "#060C18",
            "cornerRadius": "6px",
            "margin": "xs",
            "action": {
                "type": "postback",
                "data": f"act=strategy_toggle&name={name}",
                "displayText": f"{label} 切換",
            },
            "contents": [
                {
                    "type": "text", "text": label,
                    "color": "#E8EEF8" if on else "#6A7E9C",
                    "size": "sm", "weight": "bold", "flex": 3,
                },
                {
                    "type": "text", "text": status,
                    "color": "#4ADE80" if on else "#6A7E9C",
                    "size": "xs", "flex": 2, "align": "center",
                },
                {
                    "type": "text", "text": f"×{weight:.1f}",
                    "color": "#4A90E2",
                    "size": "xs", "flex": 1, "align": "end",
                },
            ],
        }

    rows = [_row(name, settings.get(name, {})) for name in ALL_STRATEGIES]

    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "paddingAll": "14px",
            "backgroundColor": "#060B14",
            "contents": [
                {"type": "text", "text": "🎯 策略管理",
                 "color": "#E8EEF8", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "點選策略可切換開關",
                 "color": "#6A7E9C", "size": "xs"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "10px", "spacing": "xs",
            "backgroundColor": "#0A0F1E",
            "contents": rows,
        },
        "footer": {
            "type": "box", "layout": "horizontal",
            "paddingAll": "10px", "spacing": "xs",
            "backgroundColor": "#060B14",
            "contents": [
                {
                    "type": "button", "style": "secondary",
                    "color": "#1A2840", "height": "sm",
                    "action": {"type": "postback",
                               "data": "act=strategy_preset&preset=conservative",
                               "displayText": "🐢 保守型"},
                    "contents": [
                        {"type": "text", "text": "🐢 保守",
                         "color": "#E8EEF8", "size": "xs"}
                    ],
                },
                {
                    "type": "button", "style": "secondary",
                    "color": "#1A3A8F", "height": "sm",
                    "action": {"type": "postback",
                               "data": "act=strategy_preset&preset=balanced",
                               "displayText": "⚖️ 穩健型"},
                    "contents": [
                        {"type": "text", "text": "⚖️ 穩健",
                         "color": "#E8EEF8", "size": "xs"}
                    ],
                },
                {
                    "type": "button", "style": "secondary",
                    "color": "#C00020", "height": "sm",
                    "action": {"type": "postback",
                               "data": "act=strategy_preset&preset=aggressive",
                               "displayText": "🦁 積極型"},
                    "contents": [
                        {"type": "text", "text": "🦁 積極",
                         "color": "#E8EEF8", "size": "xs"}
                    ],
                },
            ],
        },
    }


def build_strategy_perf_flex(perf: dict) -> dict:
    """
    生成單一策略績效 Bubble。
    """
    name     = perf["strategy"]
    label    = STRATEGY_LABELS.get(name, name)
    desc     = STRATEGY_DESC.get(name, "")
    mock_tag = "（模擬）" if perf.get("mock") else ""

    wr_color = "#4ADE80" if perf["win_rate"] >= 55 else "#FF4455"
    rt_color = "#4ADE80" if perf["avg_return"] >= 0 else "#FF4455"

    def _stat(key: str, val: str, clr: str = "#E8EEF8") -> dict:
        return {
            "type": "box", "layout": "horizontal", "margin": "xs",
            "contents": [
                {"type": "text", "text": key,   "color": "#6A7E9C", "size": "xs", "flex": 2},
                {"type": "text", "text": val,   "color": clr,        "size": "sm", "flex": 3,
                 "weight": "bold", "align": "end"},
            ],
        }

    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "12px",
            "backgroundColor": "#060B14",
            "contents": [
                {"type": "text", "text": f"{label} 策略{mock_tag}",
                 "color": "#E8EEF8", "weight": "bold", "size": "md"},
                {"type": "text", "text": desc, "color": "#6A7E9C", "size": "xs"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "12px", "spacing": "xs",
            "backgroundColor": "#0A0F1E",
            "contents": [
                _stat(f"近{perf['days']}日樣本",  f"{perf['n']} 筆"),
                _stat("勝率",                      f"{perf['win_rate']}%",    wr_color),
                _stat("平均報酬",                  f"{perf['avg_return']:+.2f}%", rt_color),
                _stat("最佳",                      f"+{perf['max_return']:.2f}%", "#4ADE80"),
                _stat("最差",                      f"{perf['min_return']:.2f}%",  "#FF4455"),
            ],
        },
        "footer": {
            "type": "box", "layout": "horizontal",
            "paddingAll": "10px", "spacing": "xs",
            "backgroundColor": "#060B14",
            "contents": [
                {
                    "type": "button", "style": "primary",
                    "color": "#1A3A8F", "height": "sm",
                    "action": {
                        "type": "postback",
                        "data": f"act=strategy_toggle&name={name}",
                        "displayText": f"{label} 切換開關",
                    },
                    "contents": [
                        {"type": "text", "text": "開/關切換",
                         "color": "#FFFFFF", "size": "xs"}
                    ],
                },
            ],
        },
    }
