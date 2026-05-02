"""
sector_rotation_engine.py — 台股族群輪動追蹤

追蹤8大族群：AI伺服器/散熱/半導體/PCB/記憶體/金融/航運/生技

計算每個族群：
    - 近5日平均漲幅
    - 外資資金流入排名
    - 成交量相對變化
    - 法人買超集中度

輪動判斷：
    strength_rank 前3名 → 主流族群
    上週主流 → 本週掉出前3 → 輪動訊號

LINE 指令：/sector → 今日族群熱度圖
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── 8大族群定義 ───────────────────────────────────────────────────────────────
SECTOR_DEFINITIONS = {
    "AI伺服器": ["AI Server", "伺服器", "雲端", "AI"],
    "散熱":     ["散熱", "機殼", "散熱模組"],
    "半導體":   ["半導體", "晶圓代工", "IC設計", "封裝測試"],
    "PCB":      ["PCB", "印刷電路板", "電路板"],
    "記憶體":   ["記憶體", "DRAM", "NAND"],
    "金融":     ["金融", "銀行", "保險", "證券"],
    "航運":     ["航運", "貨櫃", "散裝", "海運"],
    "生技":     ["生技", "醫療", "製藥", "生醫"],
}

# 前幾名視為主流
TOP_N_MAINSTREAM = 3


@dataclass
class SectorStrength:
    name:            str
    avg_return_5d:   float      # 5日平均漲幅
    foreign_flow:    float      # 外資資金流入（相對分數 0~100）
    volume_change:   float      # 成交量相對變化（vs 均值）
    chip_concentration: float   # 法人買超集中度 0~1
    composite_score: float      # 綜合強度分數 0~100
    rank:            int = 0
    trend:           str = "→"  # ↑ / ↓ / →
    stock_count:     int = 0

    @property
    def heat_icon(self) -> str:
        if self.composite_score >= 70:  return "🔥"
        if self.composite_score >= 50:  return "🌡️"
        if self.composite_score >= 30:  return "🌊"
        return "❄️"

    def format_line(self) -> str:
        bar_len = int(self.composite_score / 5)
        bar     = "█" * bar_len
        return (
            f"{self.heat_icon} {self.name:<8s}  {self.composite_score:.0f}分  "
            f"5D:{self.avg_return_5d*100:+.1f}%  {bar}"
        )

    def to_dict(self) -> dict:
        return {
            "name":               self.name,
            "composite_score":    round(self.composite_score, 2),
            "avg_return_5d":      round(self.avg_return_5d, 4),
            "foreign_flow":       round(self.foreign_flow, 2),
            "volume_change":      round(self.volume_change, 4),
            "chip_concentration": round(self.chip_concentration, 4),
            "rank":               self.rank,
            "trend":              self.trend,
            "stock_count":        self.stock_count,
        }


@dataclass
class RotationSignal:
    """輪動訊號"""
    mainstream:    list[str]   # 本期主流族群（前3名）
    rising:        list[str]   # 崛起族群（排名上升≥2位）
    cooling:       list[str]   # 退燒族群（排名下降≥2位）
    rotation_alert:bool = False
    note:          str  = ""


class SectorRotationEngine:
    """
    族群輪動追蹤引擎。

    使用方式：
        engine = SectorRotationEngine()
        strengths = await engine.scan()
        signal    = engine.detect_rotation(strengths)
        report    = engine.format_report(strengths, signal)
    """

    def __init__(self):
        self._prev_ranks: dict[str, int] = {}   # 上期排名快取

    async def scan(self) -> list[SectorStrength]:
        """從 report_screener 取資料並計算族群強度"""
        try:
            from backend.services.report_screener import all_screener
            rows = all_screener(limit=500)
            return self._calc_strengths(rows)
        except Exception as e:
            logger.warning("[SectorRotation] screener failed (%s) → mock", e)
            return self.scan_mock()

    def _calc_strengths(self, rows) -> list[SectorStrength]:
        """計算各族群強度"""
        sector_data: dict[str, list[dict]] = {s: [] for s in SECTOR_DEFINITIONS}

        for row in rows:
            sector = str(getattr(row, "sector", "") or
                         (row.get("sector", "") if isinstance(row, dict) else ""))
            mapped = self._map_sector(sector)
            if not mapped:
                continue

            def g(attr, d=0.0):
                if hasattr(row, attr):
                    v = getattr(row, attr)
                    return float(v) if v is not None else d
                return float(row.get(attr, d) or d) if isinstance(row, dict) else d

            sector_data[mapped].append({
                "ret_5d":    g("change_pct", 0) / 100,
                "foreign":   g("foreign_buy_days", 0),
                "volume_r":  g("volume_ratio", 1.0),
                "trust":     g("chip_5d", 0),
            })

        results = []
        for sector_name, stocks in sector_data.items():
            if not stocks:
                s = self._empty_sector(sector_name)
            else:
                s = self._aggregate(sector_name, stocks)
            results.append(s)

        # 排名（依 composite_score 降序）
        results.sort(key=lambda s: -s.composite_score)
        for i, s in enumerate(results, 1):
            prev_rank = self._prev_ranks.get(s.name, i)
            if i < prev_rank - 1:
                s.trend = "↑"
            elif i > prev_rank + 1:
                s.trend = "↓"
            else:
                s.trend = "→"
            s.rank = i

        self._prev_ranks = {s.name: s.rank for s in results}
        return results

    def _map_sector(self, sector: str) -> Optional[str]:
        """將個股 sector 字串對應到8大族群"""
        for name, keywords in SECTOR_DEFINITIONS.items():
            if any(kw in sector for kw in keywords):
                return name
        return None

    def _aggregate(self, name: str, stocks: list[dict]) -> SectorStrength:
        if not stocks:
            return self._empty_sector(name)

        arr_ret   = [s["ret_5d"] for s in stocks]
        arr_for   = [s["foreign"] for s in stocks]
        arr_volr  = [s["volume_r"] for s in stocks]
        arr_trust = [s["trust"] for s in stocks]

        avg_ret   = float(np.mean(arr_ret))
        avg_for   = float(np.mean(arr_for))
        avg_volr  = float(np.mean(arr_volr))
        total_chip= float(np.sum([f + t for f, t in zip(arr_for, arr_trust)]))
        max_chip  = max(abs(total_chip), 1.0)
        chip_conc = min(abs(total_chip) / max_chip, 1.0)

        # foreign_flow 正規化（-10 ~ +10 天 → 0~100）
        foreign_flow = min(max((avg_for + 10) * 5, 0), 100)

        # 綜合分數
        score = (
            min(max(avg_ret * 100 + 50, 0), 100) * 0.35 +
            foreign_flow * 0.30 +
            min(max((avg_volr - 0.5) * 40 + 50, 0), 100) * 0.20 +
            chip_conc * 100 * 0.15
        )

        return SectorStrength(
            name=name,
            avg_return_5d=avg_ret,
            foreign_flow=round(foreign_flow, 2),
            volume_change=round(avg_volr - 1.0, 4),
            chip_concentration=round(chip_conc, 4),
            composite_score=round(min(max(score, 0), 100), 2),
            stock_count=len(stocks),
        )

    def _empty_sector(self, name: str) -> SectorStrength:
        return SectorStrength(
            name=name, avg_return_5d=0.0, foreign_flow=50.0,
            volume_change=0.0, chip_concentration=0.0,
            composite_score=30.0, stock_count=0,
        )

    def detect_rotation(self, strengths: list[SectorStrength]) -> RotationSignal:
        """判斷輪動訊號"""
        mainstream = [s.name for s in strengths[:TOP_N_MAINSTREAM]]
        rising     = [s.name for s in strengths if s.trend == "↑" and s.rank > TOP_N_MAINSTREAM]
        cooling    = [s.name for s in strengths if s.trend == "↓" and s.rank <= TOP_N_MAINSTREAM + 1]

        rotation_alert = len(cooling) > 0 and len(rising) > 0
        note = ""
        if rotation_alert:
            note = f"{cooling[0]}退燒，{rising[0]}崛起，注意輪動"

        return RotationSignal(
            mainstream=mainstream,
            rising=rising[:2],
            cooling=cooling[:2],
            rotation_alert=rotation_alert,
            note=note,
        )

    def format_report(
        self,
        strengths: list[SectorStrength],
        signal: Optional[RotationSignal] = None,
    ) -> str:
        if not strengths:
            return "📊 族群輪動雷達\n\n暫無資料"

        now  = datetime.now().strftime("%m/%d %H:%M")
        lines = [
            f"🔥 族群輪動雷達  {now}",
            "─" * 22,
        ]

        if signal:
            if signal.mainstream:
                main_str = "、".join(signal.mainstream[:3])
                lines.append(f"主線：{main_str}")
            if signal.rising:
                lines.append(f"崛起：{signal.rising[0]}（外資開始布局）")
            if signal.cooling:
                lines.append(f"退燒：{signal.cooling[0]}（動能轉弱）")
            if signal.rotation_alert:
                lines.append(f"\n⚠️ {signal.note}")
            lines.append("─" * 22)

        for s in strengths[:8]:
            rank_str = f"#{s.rank}" if s.rank else ""
            trend    = s.trend
            lines.append(f"{rank_str:3s} {s.heat_icon} {s.name:<8s} {trend}  {s.composite_score:.0f}分  5D:{s.avg_return_5d*100:+.1f}%")

        if signal and signal.mainstream:
            main_str = "、".join(signal.mainstream[:2])
            rising_str = "、".join(signal.rising[:1]) if signal.rising else ""
            cool_str   = "、".join(signal.cooling[:1]) if signal.cooling else ""
            tip = f"\n建議：聚焦{main_str}"
            if rising_str: tip += f"+{rising_str}"
            if cool_str:   tip += f"，降低{cool_str}暴露"
            lines.append(tip)

        return "\n".join(lines)

    def scan_mock(self) -> list[SectorStrength]:
        """Mock 資料（測試 / screener 失敗時）"""
        import random
        rng = random.Random(42)
        results = []
        for i, name in enumerate(SECTOR_DEFINITIONS.keys()):
            base_score = rng.uniform(25, 85)
            results.append(SectorStrength(
                name=name,
                avg_return_5d=rng.uniform(-0.03, 0.08),
                foreign_flow=rng.uniform(30, 90),
                volume_change=rng.uniform(-0.3, 1.5),
                chip_concentration=rng.uniform(0.1, 0.8),
                composite_score=round(base_score, 2),
                stock_count=rng.randint(5, 30),
            ))
        results.sort(key=lambda s: -s.composite_score)
        for i, s in enumerate(results, 1):
            s.rank = i
            s.trend = ["↑", "→", "↓"][i % 3]
        return results

    async def save_snapshot(self, strengths: list[SectorStrength]) -> None:
        """儲存族群強度快照到 DB"""
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import SectorRotationLog
            async with AsyncSessionLocal() as db:
                for s in strengths:
                    db.add(SectorRotationLog(
                        sector_name=s.name,
                        composite_score=s.composite_score,
                        avg_return_5d=s.avg_return_5d,
                        foreign_flow=s.foreign_flow,
                        volume_change=s.volume_change,
                        chip_concentration=s.chip_concentration,
                        rank=s.rank,
                        trend=s.trend,
                    ))
                await db.commit()
        except Exception as e:
            logger.debug("[SectorRotation] save failed: %s", e)

    async def push_report(self, token: str) -> None:
        """推送族群熱度報告給所有訂閱者"""
        import httpx
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import Subscriber
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                r    = await db.execute(select(Subscriber))
                subs = r.scalars().all()

            strengths = await self.scan()
            signal    = self.detect_rotation(strengths)
            report    = self.format_report(strengths, signal)
            await self.save_snapshot(strengths)

            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=15) as c:
                for sub in subs:
                    uid = sub.line_user_id
                    if uid:
                        await c.post(
                            "https://api.line.me/v2/bot/message/push",
                            json={"to": uid, "messages": [{"type": "text", "text": report[:4800]}]},
                            headers=headers,
                        )
        except Exception as e:
            logger.error("[SectorRotation] push failed: %s", e)


_engine: SectorRotationEngine | None = None

def get_sector_rotation_engine() -> SectorRotationEngine:
    global _engine
    if _engine is None:
        _engine = SectorRotationEngine()
    return _engine


if __name__ == "__main__":
    engine    = SectorRotationEngine()
    strengths = engine.scan_mock()
    signal    = engine.detect_rotation(strengths)
    print(engine.format_report(strengths, signal))
