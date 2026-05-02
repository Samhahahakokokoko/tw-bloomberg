"""
scanner_engine.py — 三層分類掃描器

Core（長期核心）：穩定營收 + ROE + 外資持續買 + AI/半導體產業
Medium（中期成長）：有題材 + EPS 季增加速 + 投信連買
Satellite（高風險爆發）：新題材/Turnaround，最大倉位 5%
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── 分層標準 ─────────────────────────────────────────────────────────────────
CORE_SECTORS    = {"半導體", "AI Server", "AI", "伺服器", "雲端", "晶圓代工", "IC設計"}
CORE_REV_YOY    = 0.15   # 營收年增 > 15%
CORE_ROE        = 0.15   # ROE > 15%（以 eps_stability > 0.7 代理）
CORE_FOREIGN_DAYS = 3    # 外資連買 >= 3 天

MED_EPS_GROWTH  = 0.10   # EPS 季增率 > 10%
MED_TRUST_DAYS  = 2      # 投信連買 >= 2 天
MED_MOM_1M      = 0.05   # 1 個月漲幅 > 5%

SAT_MAX_POSITION = 0.05  # 最大倉位 5%
SAT_VOL_RATIO   = 1.5    # 高波動（量比 > 1.5）


@dataclass
class StockCandidate:
    stock_code: str
    stock_name: str
    sector:     str
    tier:       str          # "core" / "medium" / "satellite"
    close:      float
    score:      float        # 綜合評分
    max_position: float      # 建議最大倉位比例
    reasons:    list[str] = field(default_factory=list)
    risk_note:  str = ""

    def to_dict(self) -> dict:
        return {
            "code":         self.stock_code,
            "name":         self.stock_name,
            "sector":       self.sector,
            "tier":         self.tier,
            "close":        round(self.close, 2),
            "score":        round(self.score, 1),
            "max_position": self.max_position,
            "reasons":      self.reasons,
            "risk_note":    self.risk_note,
        }


@dataclass
class ScanResult:
    core:      list[StockCandidate] = field(default_factory=list)
    medium:    list[StockCandidate] = field(default_factory=list)
    satellite: list[StockCandidate] = field(default_factory=list)

    @property
    def all_candidates(self) -> list[StockCandidate]:
        return self.core + self.medium + self.satellite

    def to_dict(self) -> dict:
        return {
            "core":      [c.to_dict() for c in self.core],
            "medium":    [c.to_dict() for c in self.medium],
            "satellite": [c.to_dict() for c in self.satellite],
            "total":     len(self.core) + len(self.medium) + len(self.satellite),
        }

    def format_line(self) -> str:
        lines = [
            f"📊 選股分類結果",
            f"核心({len(self.core)}) / 成長({len(self.medium)}) / 衛星({len(self.satellite)})",
            "─" * 22,
        ]
        if self.core:
            lines.append("🔵 Core（長期核心）")
            for c in self.core[:3]:
                r = "、".join(c.reasons[:2])
                lines.append(f"  {c.stock_code} {c.stock_name}  {r}")
        if self.medium:
            lines.append("🟡 Medium（中期成長）")
            for c in self.medium[:3]:
                r = "、".join(c.reasons[:2])
                lines.append(f"  {c.stock_code} {c.stock_name}  {r}")
        if self.satellite:
            lines.append("🔴 Satellite（高風險，倉位≤5%）")
            for c in self.satellite[:2]:
                lines.append(f"  {c.stock_code} {c.stock_name}  {c.risk_note}")
        return "\n".join(lines)


class ScannerEngine:
    """
    三層分類掃描器。接受 MoverResult 列表，分類為 Core/Medium/Satellite。

    使用方式：
        scanner = ScannerEngine()
        result  = scanner.classify(movers)
        print(result.format_line())
    """

    def classify(self, candidates) -> ScanResult:
        """
        接受 MoverResult 或 StockRow 列表，分類成三層。
        """
        result = ScanResult()
        for item in candidates:
            c = self._classify_one(item)
            if c is None:
                continue
            if c.tier == "core":
                result.core.append(c)
            elif c.tier == "medium":
                result.medium.append(c)
            else:
                result.satellite.append(c)

        result.core.sort(key=lambda c: c.score, reverse=True)
        result.medium.sort(key=lambda c: c.score, reverse=True)
        result.satellite.sort(key=lambda c: c.score, reverse=True)
        return result

    def _classify_one(self, item) -> Optional[StockCandidate]:
        def _g(attr, default=0.0):
            if hasattr(item, attr):
                return getattr(item, attr)
            if isinstance(item, dict):
                return item.get(attr, default)
            return default

        code    = str(_g("stock_code", _g("stock_id", "")))
        name    = str(_g("stock_name", _g("name", code)))
        sector  = str(_g("sector", "其他"))
        close   = float(_g("close", 0))
        f_days  = int(_g("foreign_buy_days", 0))
        trust   = float(_g("trust_net", _g("trust_buy_days", 0)))
        rev_yoy = float(_g("rev_yoy", 0))   # 0~1 scale or -1~+1
        eps_g   = float(_g("eps_growth", 0))
        eps_stab= float(_g("eps_stability", 0.5))
        vol_r   = float(_g("vol_ratio", _g("volume_ratio", 1.0)))
        mom_1m  = float(_g("mom_1m", _g("momentum_20d", 1.0)) - 1.0
                        if _g("momentum_20d", 1.0) > 0.5
                        else _g("mom_1m", 0.0))
        score   = float(_g("score", 50))
        model_s = float(_g("model_score", score))

        reasons: list[str] = []

        # ── Core 判斷 ─────────────────────────────────────────────────────
        is_core_sector  = any(s in sector for s in CORE_SECTORS)
        is_core_rev     = rev_yoy > CORE_REV_YOY or rev_yoy > 0.05
        is_core_roe     = eps_stab >= CORE_ROE or eps_stab >= 0.65
        is_core_foreign = f_days >= CORE_FOREIGN_DAYS

        core_score = sum([is_core_sector, is_core_rev, is_core_roe, is_core_foreign])
        if core_score >= 3:
            if is_core_sector:   reasons.append(f"{sector}產業")
            if is_core_rev:      reasons.append(f"營收YoY+{rev_yoy*100:.0f}%")
            if is_core_foreign:  reasons.append(f"外資連買{f_days}日")
            return StockCandidate(
                stock_code=code, stock_name=name, sector=sector, tier="core",
                close=close,
                score=round(60 + core_score * 8 + model_s * 0.2, 1),
                max_position=0.20,
                reasons=reasons,
            )

        # ── Medium 判斷 ───────────────────────────────────────────────────
        is_med_eps  = eps_g > MED_EPS_GROWTH or eps_g > 0.05
        is_med_trust= trust > 0 and trust >= MED_TRUST_DAYS
        is_med_mom  = mom_1m > MED_MOM_1M or (hasattr(item, "stage") and
                                               item.stage in ("early_breakout", "trend_continuation"))

        med_score = sum([is_med_eps, is_med_trust, is_med_mom])
        if med_score >= 2:
            if is_med_eps:   reasons.append(f"EPS季增{eps_g*100:.0f}%")
            if is_med_trust: reasons.append("投信連買")
            if is_med_mom:   reasons.append("趨勢加速")
            return StockCandidate(
                stock_code=code, stock_name=name, sector=sector, tier="medium",
                close=close,
                score=round(45 + med_score * 10 + model_s * 0.15, 1),
                max_position=0.10,
                reasons=reasons,
            )

        # ── Satellite 判斷 ────────────────────────────────────────────────
        is_sat_vol  = vol_r >= SAT_VOL_RATIO
        is_sat_chip = f_days >= 1 or trust > 0
        is_sat_mom  = abs(mom_1m) > 0.05

        if is_sat_vol and is_sat_chip and is_sat_mom:
            risk_note = "高波動，倉位 ≤ 5%"
            if mom_1m < 0 and f_days > 0:
                risk_note = "Turnaround 題材，籌碼進駐"
            return StockCandidate(
                stock_code=code, stock_name=name, sector=sector, tier="satellite",
                close=close,
                score=round(35 + model_s * 0.1, 1),
                max_position=SAT_MAX_POSITION,
                reasons=["高波動爆量", "籌碼跡象"],
                risk_note=risk_note,
            )

        return None

    def classify_mock(self) -> ScanResult:
        from quant.movers_engine import MoversEngine
        movers = MoversEngine().scan_mock()
        return self.classify(movers)


_global_scanner: Optional[ScannerEngine] = None

def get_scanner_engine() -> ScannerEngine:
    global _global_scanner
    if _global_scanner is None:
        _global_scanner = ScannerEngine()
    return _global_scanner


if __name__ == "__main__":
    engine = ScannerEngine()
    result = engine.classify_mock()
    print(result.format_line())
    print(f"\ncore={len(result.core)} medium={len(result.medium)} satellite={len(result.satellite)}")
