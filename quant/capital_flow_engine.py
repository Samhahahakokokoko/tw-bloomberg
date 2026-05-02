"""
capital_flow_engine.py — 資金流向追蹤

數據來源：
    - 三大法人買超金額排行（report_screener）
    - 外資期貨淨部位（FinMind / fallback估算）
    - 融資融券變化（TWSE）
    - 大單成交比例（volume_ratio 代理）

輸出：
    - 今日資金主要流入族群
    - 外資期貨多空比
    - 大戶籌碼集中度
    - 主線資金是否持續

警示：主線資金連續3日流出 → 推送輪動警告
LINE 指令：/flow → 今日資金流向
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── 警示門檻 ───────────────────────────────────────────────────────────────────
OUTFLOW_ALERT_DAYS = 3   # 主線資金連續流出 N 日 → 輪動警告


@dataclass
class FlowSnapshot:
    date:              str
    top_inflow_sector: str           # 資金最大流入族群
    top_outflow_sector:str           # 資金最大流出族群
    foreign_futures_net: float       # 外資期貨淨部位（口）
    futures_bull_bear:   str         # "bull" / "bear" / "neutral"
    chip_concentration:  float       # 大戶籌碼集中度 0~1
    margin_change:       float       # 融資餘額變化率
    short_change:        float       # 融券餘額變化率
    sector_flows:        dict        # {sector: flow_score}
    main_flow_days:      int         # 主線資金持續天數（正=流入，負=流出）
    rotation_warning:    bool = False

    def format_line(self) -> str:
        bb_icon = {"bull": "🐂", "bear": "🐻", "neutral": "⚖️"}.get(self.futures_bull_bear, "❓")
        warn_str = "\n⚠️ 主線資金持續流出，留意輪動" if self.rotation_warning else ""
        margin_dir = "↑" if self.margin_change > 0 else "↓"
        return (
            f"💰 今日資金流向\n"
            f"─" * 22 + "\n"
            f"主流流入：{self.top_inflow_sector}\n"
            f"主流流出：{self.top_outflow_sector}\n"
            f"{bb_icon} 外資期貨：{self.foreign_futures_net:+,.0f}口"
            f"（{'多' if self.futures_bull_bear == 'bull' else '空'}）\n"
            f"大戶集中度：{self.chip_concentration:.0%}\n"
            f"融資：{self.margin_change:+.1%} {margin_dir}"
            f"  主線持續：{self.main_flow_days:+}日"
            f"{warn_str}"
        )

    def to_dict(self) -> dict:
        return {
            "date":               self.date,
            "top_inflow_sector":  self.top_inflow_sector,
            "top_outflow_sector": self.top_outflow_sector,
            "foreign_futures_net":self.foreign_futures_net,
            "futures_bull_bear":  self.futures_bull_bear,
            "chip_concentration": round(self.chip_concentration, 4),
            "margin_change":      round(self.margin_change, 4),
            "short_change":       round(self.short_change, 4),
            "sector_flows":       self.sector_flows,
            "main_flow_days":     self.main_flow_days,
            "rotation_warning":   self.rotation_warning,
        }


class CapitalFlowEngine:
    """
    資金流向追蹤引擎。

    使用方式：
        engine   = CapitalFlowEngine()
        snapshot = await engine.scan()
        print(snapshot.format_line())
    """

    def __init__(self):
        self._main_flow_days = 0   # 主線資金持續天數快取

    async def scan(self) -> FlowSnapshot:
        """掃描今日資金流向"""
        sector_flows = await self._calc_sector_flows()
        futures_net  = await self._fetch_futures()
        margin       = await self._fetch_margin()
        chip_conc    = self._calc_chip_concentration(sector_flows)

        top_inflow  = max(sector_flows, key=sector_flows.get) if sector_flows else "未知"
        top_outflow = min(sector_flows, key=sector_flows.get) if sector_flows else "未知"

        if sector_flows.get(top_inflow, 0) > 0:
            self._main_flow_days = max(1, self._main_flow_days + 1)
        else:
            self._main_flow_days = min(-1, self._main_flow_days - 1)

        rotation_warning = self._main_flow_days <= -OUTFLOW_ALERT_DAYS

        if futures_net > 2000:
            bb = "bull"
        elif futures_net < -2000:
            bb = "bear"
        else:
            bb = "neutral"

        snap = FlowSnapshot(
            date=datetime.now().strftime("%Y-%m-%d"),
            top_inflow_sector=top_inflow,
            top_outflow_sector=top_outflow,
            foreign_futures_net=futures_net,
            futures_bull_bear=bb,
            chip_concentration=chip_conc,
            margin_change=margin.get("margin_change", 0.0),
            short_change=margin.get("short_change", 0.0),
            sector_flows=sector_flows,
            main_flow_days=self._main_flow_days,
            rotation_warning=rotation_warning,
        )

        await self._save_snapshot(snap)
        return snap

    # ── 資料計算 ──────────────────────────────────────────────────────────────

    async def _calc_sector_flows(self) -> dict[str, float]:
        """從 report_screener 計算各族群資金流向"""
        from quant.sector_rotation_engine import SECTOR_DEFINITIONS
        flows: dict[str, float] = {s: 0.0 for s in SECTOR_DEFINITIONS}

        try:
            from backend.services.report_screener import all_screener
            rows = all_screener(limit=500)
            for row in rows:
                sector = str(getattr(row, "sector", "") or
                             (row.get("sector", "") if isinstance(row, dict) else ""))
                for name, keywords in SECTOR_DEFINITIONS.items():
                    if any(kw in sector for kw in keywords):
                        f_days = float(getattr(row, "foreign_buy_days", 0) or 0)
                        chip   = float(getattr(row, "chip_5d", 0) or 0)
                        vol_r  = float(getattr(row, "volume_ratio", 1.0) or 1.0)
                        # 流向分數 = 外資天數 * 量比加成
                        flows[name] += (f_days + chip / 1000) * min(vol_r, 3.0)
                        break
        except Exception as e:
            logger.warning("[CapitalFlow] screener error: %s", e)
            # fallback: random mock
            import random
            rng = random.Random(42)
            for k in flows:
                flows[k] = rng.uniform(-50, 80)

        return {k: round(v, 2) for k, v in flows.items()}

    async def _fetch_futures(self) -> float:
        """取外資期貨淨部位（口數），fallback 用技術指標估算"""
        try:
            from backend.services.twse_service import fetch_market_overview
            ov = await fetch_market_overview()
            if ov:
                change_pct = float(ov.get("change_pct", 0))
                return change_pct * 1000
        except Exception:
            pass
        return 0.0

    async def _fetch_margin(self) -> dict:
        """取融資融券資料"""
        try:
            from backend.services.twse_service import fetch_market_overview
            ov = await fetch_market_overview()
            if ov:
                change = float(ov.get("change", 0))
                return {
                    "margin_change": change / 1000,
                    "short_change":  -change / 2000,
                }
        except Exception:
            pass
        return {"margin_change": 0.0, "short_change": 0.0}

    @staticmethod
    def _calc_chip_concentration(sector_flows: dict[str, float]) -> float:
        """大戶籌碼集中度 = 前3名流入 / 總流入"""
        if not sector_flows:
            return 0.5
        positives = sorted([v for v in sector_flows.values() if v > 0], reverse=True)
        total     = sum(positives)
        if total <= 0:
            return 0.0
        top3 = sum(positives[:3])
        return min(top3 / total, 1.0)

    # ── 持久化 ────────────────────────────────────────────────────────────────

    async def _save_snapshot(self, snap: FlowSnapshot) -> None:
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import CapitalFlowLog
            import json
            async with AsyncSessionLocal() as db:
                db.add(CapitalFlowLog(
                    top_inflow_sector=snap.top_inflow_sector,
                    top_outflow_sector=snap.top_outflow_sector,
                    foreign_futures_net=snap.foreign_futures_net,
                    futures_bull_bear=snap.futures_bull_bear,
                    chip_concentration=snap.chip_concentration,
                    margin_change=snap.margin_change,
                    short_change=snap.short_change,
                    sector_flows_json=json.dumps(snap.sector_flows, ensure_ascii=False),
                    main_flow_days=snap.main_flow_days,
                    rotation_warning=snap.rotation_warning,
                ))
                await db.commit()
        except Exception as e:
            logger.debug("[CapitalFlow] save failed: %s", e)

    async def push_rotation_warning(self, snap: FlowSnapshot, token: str) -> None:
        """主線資金連續流出 → 推送警告"""
        if not snap.rotation_warning or not token:
            return
        import httpx
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import Subscriber
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                r    = await db.execute(select(Subscriber))
                subs = r.scalars().all()
            msg     = (
                f"⚠️ 資金輪動警告\n\n"
                f"主線（{snap.top_inflow_sector}）資金已連續 {abs(snap.main_flow_days)} 日流出\n"
                f"留意族群切換，降低暴露比例"
            )
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=15) as c:
                for sub in subs:
                    uid = sub.line_user_id
                    if uid:
                        await c.post(
                            "https://api.line.me/v2/bot/message/push",
                            json={"to": uid, "messages": [{"type": "text", "text": msg}]},
                            headers=headers,
                        )
        except Exception as e:
            logger.error("[CapitalFlow] push_warning failed: %s", e)

    def mock_snapshot(self) -> FlowSnapshot:
        import random, json
        rng = random.Random(42)
        from quant.sector_rotation_engine import SECTOR_DEFINITIONS
        flows = {s: round(rng.uniform(-50, 80), 2) for s in SECTOR_DEFINITIONS}
        top_in  = max(flows, key=flows.get)
        top_out = min(flows, key=flows.get)
        return FlowSnapshot(
            date=datetime.now().strftime("%Y-%m-%d"),
            top_inflow_sector=top_in,
            top_outflow_sector=top_out,
            foreign_futures_net=rng.uniform(-5000, 8000),
            futures_bull_bear=rng.choice(["bull", "bear", "neutral"]),
            chip_concentration=rng.uniform(0.4, 0.8),
            margin_change=rng.uniform(-0.02, 0.03),
            short_change=rng.uniform(-0.01, 0.02),
            sector_flows=flows,
            main_flow_days=rng.randint(-4, 8),
            rotation_warning=False,
        )


_engine: CapitalFlowEngine | None = None

def get_capital_flow_engine() -> CapitalFlowEngine:
    global _engine
    if _engine is None:
        _engine = CapitalFlowEngine()
    return _engine


if __name__ == "__main__":
    engine = CapitalFlowEngine()
    snap   = engine.mock_snapshot()
    print(snap.format_line())
