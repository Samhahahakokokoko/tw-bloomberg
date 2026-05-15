"""
scanner_engine.py — Layer 2: 三層風險分類掃描器

Core  (倉位上限 20%)：AI/半導體/伺服器 + 營收YoY>15% + 外資連買>=3日 + MA20>MA60 + ROE>15%
Medium(倉位上限 10%)：EPS季增加速 + 投信連買>=3日 + RS強 + MA5>MA20 趨勢確認
Satellite(倉位上限 5%)：Turnaround/新題材 + 高成長高波動 + 小型股

輸出：{stock_id: {"layer": "core/medium/satellite", "score": float, ...}}
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

CORE_SECTORS    = {"半導體", "AI Server", "AI", "伺服器", "雲端", "晶圓代工", "IC設計"}
CORE_REV_YOY    = 0.15
CORE_FOREIGN_D  = 3      # 外資連買 >= 3 日
CORE_MA_COND    = True   # 需要 MA20 > MA60

MED_TRUST_D     = 3      # 投信連買 >= 3 日
MED_RS_THRESH   = 0.05   # Relative Strength：近20日跑贏大盤 > 5%

SAT_MAX_CAP     = 200e8  # 市值 < 200 億視為小型股


@dataclass
class ScanRecord:
    stock_id:     str
    name:         str
    sector:       str
    layer:        str        # core / medium / satellite
    score:        float      # 0~1
    max_position: float      # 最大倉位比例
    reasons:      list[str] = field(default_factory=list)
    risk_note:    str = ""

    def to_dict(self) -> dict:
        return {
            "layer":        self.layer,
            "score":        round(self.score, 4),
            "max_position": self.max_position,
            "reasons":      self.reasons,
            "risk_note":    self.risk_note,
            "name":         self.name,
            "sector":       self.sector,
        }


@dataclass
class ScanResult:
    records:   dict[str, ScanRecord] = field(default_factory=dict)

    @property
    def core(self) -> list[ScanRecord]:
        return sorted([r for r in self.records.values() if r.layer == "core"],
                      key=lambda r: -r.score)
    @property
    def medium(self) -> list[ScanRecord]:
        return sorted([r for r in self.records.values() if r.layer == "medium"],
                      key=lambda r: -r.score)
    @property
    def satellite(self) -> list[ScanRecord]:
        return sorted([r for r in self.records.values() if r.layer == "satellite"],
                      key=lambda r: -r.score)

    def to_dict(self) -> dict:
        return {sid: rec.to_dict() for sid, rec in self.records.items()}

    def format_line(self) -> str:
        lines = [
            f"📊 三層分類結果",
            f"Core({len(self.core)}) / Medium({len(self.medium)}) / Satellite({len(self.satellite)})",
            "─" * 22,
        ]
        for c in self.core[:3]:
            lines.append(f"🔵 {c.stock_id} {c.name}  {'/'.join(c.reasons[:2])}")
        for c in self.medium[:3]:
            lines.append(f"🟡 {c.stock_id} {c.name}  {'/'.join(c.reasons[:2])}")
        for c in self.satellite[:2]:
            lines.append(f"🔴 {c.stock_id} {c.name}  {c.risk_note}")
        return "\n".join(lines)


class ScannerEngine:
    """
    三層風險分類掃描器。

    接受 MoverResult 列表（Layer 1 輸出），分類成 Core / Medium / Satellite。

    使用方式：
        scanner = ScannerEngine()
        result  = scanner.classify(movers)
        print(result.format_line())

        # 直接取 dict 輸出
        d = result.to_dict()   # {stock_id: {"layer": ..., "score": ...}}
    """

    def classify(self, candidates: list) -> ScanResult:
        result = ScanResult()
        for item in candidates:
            rec = self._classify_one(item)
            if rec:
                result.records[rec.stock_id] = rec
        return result

    def _classify_one(self, item) -> Optional[ScanRecord]:
        def g(attr, d=0.0):
            if hasattr(item, attr):
                v = getattr(item, attr)
                return float(v) if v is not None else d
            if isinstance(item, dict):
                return float(item.get(attr, d) or d)
            return d
        def gi(attr, d=0):
            return int(g(attr, d))
        def gs(attr, d=""):
            if hasattr(item, attr): return str(getattr(item, attr) or d)
            if isinstance(item, dict): return str(item.get(attr, d) or d)
            return d

        stock_id   = gs("stock_id", gs("code", ""))
        name       = gs("name", stock_id)
        sector     = gs("sector", "其他")
        close      = g("close", 0.0)
        ma20       = g("ma20", close * 0.97)
        ma60       = g("ma60", close * 0.94)
        ma5        = g("ma5",  close * 0.99)
        rev_yoy    = g("rev_yoy", 0)
        eps_growth = g("eps_growth", 0)
        eps_stab   = g("eps_stability", 0.5)
        f_days     = gi("foreign_buy_days", 0)
        trust_net  = g("trust_net", 0)
        vol_r      = g("volume_ratio", g("vol_ratio", 1.0))
        vol_k      = g("avg_volume_k", g("volume", 0) / 1000)
        ret_5d     = g("ret_5d", g("5d_return", 0) / 100)
        ret_1m     = g("ret_1m", g("1m_return", 0) / 100)
        pe_ratio   = g("pe_ratio", 20)
        score      = g("score", 50)

        # Relative Strength 估算（近20日 vs 市場）
        # 嘗試從快取取大盤月報酬，否則用近7日均漲跌幅 fallback
        try:
            from backend.services.twse_service import _mkt_cache  # type: ignore
            market_ret_1m = float((_mkt_cache or {}).get("monthly_return", 0.02))
        except Exception:
            market_ret_1m = 0.02
        rs = ret_1m - market_ret_1m

        # ROE 代理：eps_stability > 0.7 ≈ ROE > 15%
        roe_proxy = eps_stab >= 0.70

        # MA 排列
        ma20_above_ma60 = ma20 > ma60
        ma5_above_ma20  = ma5  > ma20

        # Trust 連買（trust_net > 0 代理投信買超）
        trust_buy_days = 3 if trust_net > 200 else (1 if trust_net > 0 else 0)

        # ── Core 條件（門檻降為 3/5）────────────────────────────────────
        is_core_sector   = any(s in sector for s in CORE_SECTORS)
        is_core_rev      = rev_yoy > CORE_REV_YOY
        is_core_foreign  = f_days >= CORE_FOREIGN_D
        is_core_ma       = ma20_above_ma60
        is_core_roe      = roe_proxy

        core_count = sum([is_core_sector, is_core_rev, is_core_foreign,
                          is_core_ma, is_core_roe])
        if core_count >= 3:
            reasons = []
            if is_core_sector:  reasons.append(f"{sector}核心產業")
            if is_core_rev:     reasons.append(f"營收YoY+{rev_yoy*100:.0f}%")
            if is_core_foreign: reasons.append(f"外資連買{f_days}日")
            if is_core_ma:      reasons.append("MA20>MA60")
            if is_core_roe:     reasons.append("ROE>15%")
            if not reasons:     reasons.append("動能+基本面")
            core_score = 0.55 + (core_count - 2) * 0.10 + min(score, 100) * 0.003
            return ScanRecord(
                stock_id=stock_id, name=name, sector=sector,
                layer="core", score=round(min(core_score, 1.0), 4),
                max_position=0.20, reasons=reasons,
            )

        # ── Medium 條件（門檻降為 1/4）──────────────────────────────────
        is_med_eps   = eps_growth > 0.05 or (ret_1m > 0.06)
        is_med_trust = trust_buy_days >= 1 or trust_net > 100
        is_med_rs    = rs > 0.02   # 跑贏大盤 2%（原 5%）
        is_med_ma    = ma5_above_ma20

        med_count = sum([is_med_eps, is_med_trust, is_med_rs, is_med_ma])
        if med_count >= 1:
            reasons = []
            if is_med_eps:   reasons.append(f"EPS加速+{eps_growth*100:.0f}%" if eps_growth > 0.05 else f"月報酬+{ret_1m*100:.1f}%")
            if is_med_trust: reasons.append("投信買超")
            if is_med_rs:    reasons.append(f"RS跑贏+{rs*100:.1f}%")
            if is_med_ma:    reasons.append("MA5>MA20")
            if not reasons:  reasons.append("技術動能")
            med_score = 0.35 + med_count * 0.07 + min(score, 100) * 0.002
            return ScanRecord(
                stock_id=stock_id, name=name, sector=sector,
                layer="medium", score=round(min(med_score, 0.75), 4),
                max_position=0.10, reasons=reasons,
            )

        # ── Satellite 條件 ────────────────────────────────────────────────
        is_sat_small  = vol_k > 0 and vol_k < 2000
        is_sat_growth = eps_growth > 0.10 or rev_yoy > 0.15
        is_sat_chip   = f_days > 0 or trust_net > 0
        is_sat_vol    = vol_r >= 1.0   # 原 1.5，放寬

        if (is_sat_chip and is_sat_vol and (is_sat_small or is_sat_growth)):
            risk = "小型高成長，高波動" if is_sat_small else "高成長Turnaround"
            sat_reasons = []
            if rev_yoy > 0.15:  sat_reasons.append(f"營收年增{rev_yoy*100:.0f}%")
            if eps_growth > 0.10: sat_reasons.append(f"EPS加速+{eps_growth*100:.0f}%")
            if f_days > 0:      sat_reasons.append(f"外資連買{f_days}日")
            elif trust_net > 0: sat_reasons.append("投信買超")
            if vol_r >= 1.0:    sat_reasons.append(f"量比{vol_r:.1f}x")
            if not sat_reasons: sat_reasons = ["動能啟動"]
            return ScanRecord(
                stock_id=stock_id, name=name, sector=sector,
                layer="satellite", score=0.30,
                max_position=0.05,
                reasons=sat_reasons[:3],
                risk_note=risk,
            )

        # ── Fallback Satellite：通過 Layer 1 但未達分類條件 ──────────────
        # 任何進入分類器的 mover 都給 satellite，避免全部落空
        fb_reasons = []
        if ret_1m > 0:    fb_reasons.append(f"月報酬+{ret_1m*100:.1f}%")
        if vol_r >= 0.7:  fb_reasons.append(f"量比{vol_r:.1f}x")
        if f_days > 0:    fb_reasons.append(f"外資+{f_days}日")
        elif trust_net > 0: fb_reasons.append("投信買超")
        if not fb_reasons: fb_reasons = ["動能觀察"]
        return ScanRecord(
            stock_id=stock_id, name=name, sector=sector,
            layer="satellite", score=0.20 + min(score, 100) * 0.001,
            max_position=0.05,
            reasons=fb_reasons[:3],
            risk_note="待觀察",
        )

    def classify_mock(self) -> ScanResult:
        from quant.movers_engine import MoversEngine
        return self.classify(MoversEngine().scan_mock())


def get_scanner_engine() -> ScannerEngine:
    return ScannerEngine()


if __name__ == "__main__":
    engine = ScannerEngine()
    result = engine.classify_mock()
    print(result.format_line())
    print(f"\n輸出 dict:")
    for sid, d in list(result.to_dict().items())[:3]:
        print(f"  {sid}: {d}")
