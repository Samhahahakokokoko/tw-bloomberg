"""
analyst_dna_engine.py — 分析師 DNA 學習引擎

學習每位分析師的最佳市場條件，並在對應市場加權使用：

DNA 維度：
  1. 最佳市場環境（bull/bear/sideways）
  2. 最佳族群（AI/半導體/存股/總經...）
  3. 最佳時間點（月初/月底/財報季/除息後）
  4. 持倉期偏好（短線3d/中線15d/長線60d）
  5. 行情特徵（突破型/均值回歸/動能/低檔承接）
  6. 大盤溫度偏好（高/中/低 Euphoria）

使用方式：
  在 consensus_engine 計算加權分時：
  market_bonus = dna.get_market_bonus(current_market_state)
  final_weight = base_weight * (1 + market_bonus)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from loguru import logger


@dataclass
class AnalystDNA:
    analyst_id:   str
    analyst_name: str

    # ── 最佳環境分佈 ─────────────────────────────────────────────────────────
    best_market:      str    = ""    # bull / bear / sideways / all
    market_win_rates: dict   = field(default_factory=dict)  # {bull: 0.72, bear: 0.45}

    # ── 最佳族群（依勝率排序）────────────────────────────────────────────────
    best_sectors:     list[str] = field(default_factory=list)
    sector_win_rates: dict      = field(default_factory=dict)  # {sector: win_rate}

    # ── 時間特徵 ─────────────────────────────────────────────────────────────
    best_month_of_year:  Optional[int] = None   # 1-12，哪個月表現最好
    earnings_season_boost: float = 0.0          # 財報季勝率加成
    best_dow:            str = ""               # 哪天發影片表現最好 (Mon-Fri)

    # ── 持倉期偏好 ────────────────────────────────────────────────────────────
    preferred_holding_days: int   = 15          # 中位數持倉天數
    holding_profile:        str   = "medium"    # short(<7d) / medium(7-30d) / long(>30d)

    # ── 行情偏好 ─────────────────────────────────────────────────────────────
    signal_style:           str   = "momentum"  # breakout/mean_revert/momentum/dip_buy

    # ── Euphoria 偏好 ─────────────────────────────────────────────────────────
    best_euphoria_range:    tuple = (40, 70)    # 最適合的市場溫度區間

    # ── 數據品質 ─────────────────────────────────────────────────────────────
    sample_size:    int   = 0
    last_updated:   str   = ""
    confidence:     float = 0.5   # 0-1，DNA 可信度（樣本越多越高）

    def get_market_bonus(self, current_market: str) -> float:
        """
        根據當前市場狀態，計算加成或折扣。
        回傳 -0.5 ~ +0.5 的乘數加成。
        """
        if not self.market_win_rates or current_market not in self.market_win_rates:
            return 0.0

        overall_wr = sum(self.market_win_rates.values()) / len(self.market_win_rates)
        current_wr = self.market_win_rates.get(current_market, overall_wr)

        # 比整體均值好 → 正加成，差 → 負加成
        delta = (current_wr - overall_wr) / max(overall_wr, 0.01)
        return round(min(0.5, max(-0.5, delta * 0.5)) * self.confidence, 3)

    def get_sector_bonus(self, sector: str) -> float:
        """特定族群的加成"""
        if not self.sector_win_rates or sector not in self.sector_win_rates:
            return 0.0
        overall_wr = sum(self.sector_win_rates.values()) / len(self.sector_win_rates)
        sector_wr  = self.sector_win_rates.get(sector, overall_wr)
        delta = (sector_wr - overall_wr) / max(overall_wr, 0.01)
        return round(min(0.4, max(-0.4, delta * 0.4)) * self.confidence, 3)

    def format_line(self) -> str:
        mwr = " / ".join(f"{k}:{v:.0%}" for k, v in self.market_win_rates.items()) if self.market_win_rates else "—"
        best_sec = "、".join(self.best_sectors[:3]) if self.best_sectors else "—"
        lines = [
            f"🧬 {self.analyst_name} DNA",
            f"樣本：{self.sample_size} 筆  DNA 可信度：{self.confidence:.0%}",
            f"最佳市場：{self.best_market or '—'}",
            f"各市場勝率：{mwr}",
            f"最佳族群：{best_sec}",
            f"持倉偏好：{self.holding_profile}（中位 {self.preferred_holding_days} 天）",
            f"訊號風格：{self.signal_style}",
            f"最適溫度：{self.best_euphoria_range[0]}-{self.best_euphoria_range[1]}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "analyst_id":           self.analyst_id,
            "analyst_name":         self.analyst_name,
            "best_market":          self.best_market,
            "market_win_rates":     self.market_win_rates,
            "best_sectors":         self.best_sectors,
            "sector_win_rates":     self.sector_win_rates,
            "preferred_holding_days": self.preferred_holding_days,
            "holding_profile":      self.holding_profile,
            "signal_style":         self.signal_style,
            "best_euphoria_range":  list(self.best_euphoria_range),
            "sample_size":          self.sample_size,
            "confidence":           self.confidence,
            "last_updated":         self.last_updated,
        }


def _holding_profile(days: int) -> str:
    if days < 7:    return "short"
    if days < 30:   return "medium"
    return "long"


def _infer_signal_style(calls_data: list[dict]) -> str:
    """從推薦紀錄的技術特徵推斷訊號風格"""
    breakout = dip_buy = mean_rev = momentum = 0
    for c in calls_data:
        ctx = c.get("context", "").lower()
        if any(k in ctx for k in ["突破", "breakout", "新高"]):
            breakout += 1
        elif any(k in ctx for k in ["低接", "承接", "跌深"]):
            dip_buy += 1
        elif any(k in ctx for k in ["均線", "回調", "回測"]):
            mean_rev += 1
        else:
            momentum += 1

    counts = {
        "breakout": breakout, "dip_buy": dip_buy,
        "mean_revert": mean_rev, "momentum": momentum,
    }
    return max(counts, key=lambda k: counts[k])


async def compute_dna(analyst_id: str) -> Optional[AnalystDNA]:
    """
    從歷史 AnalystCall 資料計算分析師 DNA。
    需要至少 10 筆已結算的推薦紀錄。
    """
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import AnalystCall, Analyst
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Analyst).where(Analyst.analyst_id == analyst_id))
            analyst = r.scalar_one_or_none()
            if not analyst:
                return None

            r2 = await db.execute(
                select(AnalystCall)
                .where(AnalystCall.analyst_id == analyst_id)
                .where(AnalystCall.actual_return != None)
                .order_by(AnalystCall.date.desc())
                .limit(200)
            )
            calls = r2.scalars().all()

        if len(calls) < 5:
            return AnalystDNA(
                analyst_id   = analyst_id,
                analyst_name = analyst.name,
                sample_size  = len(calls),
                confidence   = 0.2,
                last_updated = datetime.now().strftime("%Y-%m-%d"),
            )

        sample = len(calls)
        confidence = min(0.95, 0.3 + sample * 0.01)

        # ── 市場環境分析 ──────────────────────────────────────────────────────
        mkt_wins: dict[str, list[float]] = {}
        for c in calls:
            mkt = getattr(c, "market_state", "unknown") or "unknown"
            if mkt not in mkt_wins:
                mkt_wins[mkt] = []
            mkt_wins[mkt].append(1.0 if (c.actual_return or 0) > 0 else 0.0)

        market_win_rates = {
            mkt: sum(v) / len(v)
            for mkt, v in mkt_wins.items() if len(v) >= 3
        }
        best_market = max(market_win_rates, key=lambda k: market_win_rates[k]) if market_win_rates else ""

        # ── 族群分析 ─────────────────────────────────────────────────────────
        sector_wins: dict[str, list[float]] = {}
        for c in calls:
            sector = getattr(c, "sector", "") or ""
            if not sector:
                continue
            if sector not in sector_wins:
                sector_wins[sector] = []
            sector_wins[sector].append(1.0 if (c.actual_return or 0) > 0 else 0.0)

        sector_win_rates = {
            s: sum(v) / len(v)
            for s, v in sector_wins.items() if len(v) >= 3
        }
        best_sectors = sorted(sector_win_rates, key=lambda s: -sector_win_rates[s])[:5]

        # ── 持倉期計算 ────────────────────────────────────────────────────────
        holding_days_list = []
        for c in calls:
            try:
                entry = datetime.strptime(c.date, "%Y-%m-%d")
                exit_date = getattr(c, "exit_date", None)
                if exit_date:
                    ex    = datetime.strptime(exit_date, "%Y-%m-%d")
                    days  = (ex - entry).days
                    if 0 < days <= 90:
                        holding_days_list.append(days)
            except Exception:
                pass

        median_days = int(sorted(holding_days_list)[len(holding_days_list) // 2]) if holding_days_list else 15

        # ── 訊號風格 ──────────────────────────────────────────────────────────
        calls_data = [
            {"context": getattr(c, "key_points", "") or ""}
            for c in calls
        ]
        signal_style = _infer_signal_style(calls_data)

        dna = AnalystDNA(
            analyst_id          = analyst_id,
            analyst_name        = analyst.name,
            best_market         = best_market,
            market_win_rates    = market_win_rates,
            best_sectors        = best_sectors,
            sector_win_rates    = sector_win_rates,
            preferred_holding_days = median_days,
            holding_profile     = _holding_profile(median_days),
            signal_style        = signal_style,
            sample_size         = sample,
            confidence          = round(confidence, 3),
            last_updated        = datetime.now().strftime("%Y-%m-%d"),
        )

        # 儲存到 DB
        await _save_dna(dna)
        return dna

    except Exception as e:
        logger.error("[DNA] compute failed for %s: %s", analyst_id, e)
        return None


async def _save_dna(dna: AnalystDNA):
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import AnalystDNARecord
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(AnalystDNARecord).where(AnalystDNARecord.analyst_id == dna.analyst_id)
            )
            rec = r.scalar_one_or_none()
            if rec is None:
                rec = AnalystDNARecord(analyst_id=dna.analyst_id)
                db.add(rec)

            rec.analyst_name           = dna.analyst_name
            rec.best_market            = dna.best_market
            rec.market_win_rates_json  = json.dumps(dna.market_win_rates, ensure_ascii=False)
            rec.best_sectors_json      = json.dumps(dna.best_sectors, ensure_ascii=False)
            rec.sector_win_rates_json  = json.dumps(dna.sector_win_rates, ensure_ascii=False)
            rec.preferred_holding_days = dna.preferred_holding_days
            rec.holding_profile        = dna.holding_profile
            rec.signal_style           = dna.signal_style
            rec.sample_size            = dna.sample_size
            rec.confidence             = dna.confidence
            rec.last_updated           = dna.last_updated
            await db.commit()
    except Exception as e:
        logger.warning("[DNA] save failed: %s", e)


async def load_dna(analyst_id: str) -> Optional[AnalystDNA]:
    """從 DB 載入 DNA（若不存在則即時計算）"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import AnalystDNARecord
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(AnalystDNARecord).where(AnalystDNARecord.analyst_id == analyst_id)
            )
            rec = r.scalar_one_or_none()

        if rec:
            return AnalystDNA(
                analyst_id          = rec.analyst_id,
                analyst_name        = rec.analyst_name,
                best_market         = rec.best_market or "",
                market_win_rates    = json.loads(rec.market_win_rates_json or "{}"),
                best_sectors        = json.loads(rec.best_sectors_json or "[]"),
                sector_win_rates    = json.loads(rec.sector_win_rates_json or "{}"),
                preferred_holding_days = rec.preferred_holding_days or 15,
                holding_profile     = rec.holding_profile or "medium",
                signal_style        = rec.signal_style or "momentum",
                sample_size         = rec.sample_size or 0,
                confidence          = rec.confidence or 0.3,
                last_updated        = rec.last_updated or "",
            )
    except Exception:
        pass

    return await compute_dna(analyst_id)


async def get_weighted_analysts(
    current_market: str = "bull",
    current_sector: str = "",
) -> list[tuple[str, float]]:
    """
    回傳所有啟用分析師及其當前市場加權：
    [(analyst_id, effective_weight), ...]
    按加權從大到小排序。
    """
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import Analyst
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(Analyst).where(Analyst.is_active == True)
            )
            analysts = r.scalars().all()

        results: list[tuple[str, float]] = []
        for a in analysts:
            dna = await load_dna(a.analyst_id)
            base_weight = a.weight or 1.0

            if dna:
                market_bonus = dna.get_market_bonus(current_market)
                sector_bonus = dna.get_sector_bonus(current_sector) if current_sector else 0.0
                effective    = round(base_weight * (1 + market_bonus + sector_bonus), 3)
            else:
                effective = base_weight

            results.append((a.analyst_id, effective))

        results.sort(key=lambda x: -x[1])
        return results

    except Exception as e:
        logger.error("[DNA] get_weighted_analysts failed: %s", e)
        return []


async def run_weekly_dna_update():
    """每週五重新計算所有分析師的 DNA"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import Analyst
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Analyst).where(Analyst.is_active == True))
            analysts = r.scalars().all()

        updated = 0
        for a in analysts:
            try:
                dna = await compute_dna(a.analyst_id)
                if dna:
                    updated += 1
            except Exception as e:
                logger.warning("[DNA] update failed for %s: %s", a.analyst_id, e)

        logger.info("[DNA] weekly update: %d/%d analysts updated", updated, len(analysts))
    except Exception as e:
        logger.error("[DNA] weekly update failed: %s", e)
