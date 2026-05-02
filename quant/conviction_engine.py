"""
conviction_engine.py — 信心強度計算與倉位決策

公式：
    conviction = (
        signal_strength   * 0.35 +
        factor_consensus  * 0.30 +
        regime_alignment  * 0.20 +
        research_quality  * 0.15
    )

倉位對應：
    > 0.85          → Core 層上限 20%
    0.70 ~ 0.85     → Core 層一半 10%
    0.55 ~ 0.70     → Medium 層 5%
    < 0.55          → 不交易

輸出：{"ticker":"2330", "conviction":0.87, "position_size":0.18, "layer":"core"}
LINE 指令：/conviction 2330
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── 各分量權重 ─────────────────────────────────────────────────────────────────
W_SIGNAL    = 0.35
W_CONSENSUS = 0.30
W_REGIME    = 0.20
W_RESEARCH  = 0.15

# ── 倉位對應 ───────────────────────────────────────────────────────────────────
CONVICTION_CORE_HIGH  = 0.85   # → 20%
CONVICTION_CORE_LOW   = 0.70   # → 10%
CONVICTION_MEDIUM     = 0.55   # → 5%
# < 0.55 → 不交易

POSITION_CORE_HIGH  = 0.20
POSITION_CORE_LOW   = 0.10
POSITION_MEDIUM     = 0.05


@dataclass
class ConvictionResult:
    ticker:         str
    name:           str
    conviction:     float       # 0~1
    position_size:  float       # 0~0.20
    layer:          str         # core / medium / no_trade
    signal_strength:   float
    factor_consensus:  float
    regime_alignment:  float
    research_quality:  float
    note:           str = ""
    computed_at:    str = ""

    def to_dict(self) -> dict:
        return {
            "ticker":          self.ticker,
            "name":            self.name,
            "conviction":      round(self.conviction, 4),
            "position_size":   round(self.position_size, 4),
            "layer":           self.layer,
            "components": {
                "signal_strength":  round(self.signal_strength, 4),
                "factor_consensus": round(self.factor_consensus, 4),
                "regime_alignment": round(self.regime_alignment, 4),
                "research_quality": round(self.research_quality, 4),
            },
            "note":       self.note,
            "computed_at": self.computed_at,
        }

    def format_line(self) -> str:
        bar_len = int(self.conviction * 20)
        bar     = "█" * bar_len + "░" * (20 - bar_len)
        pos_pct = self.position_size * 100
        layer_zh = {"core": "核心層", "medium": "衛星層", "no_trade": "不交易"}.get(self.layer, self.layer)
        return (
            f"🎯 {self.ticker} {self.name}\n"
            f"信心：{self.conviction:.2%}  {bar}\n"
            f"建議：{layer_zh}  {pos_pct:.0f}% 倉位\n"
            f"訊號={self.signal_strength:.2f}  共識={self.factor_consensus:.2f}"
            f"  盤態={self.regime_alignment:.2f}  研究={self.research_quality:.2f}"
        )


class ConvictionEngine:
    """
    計算每筆交易的信心強度，決定倉位大小。

    使用方式：
        engine = ConvictionEngine()
        result = engine.compute("2330", movers_score=82, scanner_score=0.78,
                                research_pass=5, regime_label="BULL")
        print(result.format_line())
    """

    def compute(
        self,
        ticker:          str,
        name:            str = "",
        movers_score:    float = 50.0,   # 0~100，來自 MoversEngine
        scanner_score:   float = 0.60,   # 0~1，來自 ScannerEngine
        scanner_layer:   str   = "medium",
        research_pass:   int   = 3,      # auto_pass 項數（0~6）
        research_status: str   = "NEEDS_RESEARCH",  # READY/NEEDS_RESEARCH/REJECTED
        regime_label:    str   = "UNKNOWN",
        regime_conf:     float = 0.5,
        factor_count:    int   = 3,      # 通過因子數（0~5）
    ) -> ConvictionResult:

        # ── 1. Signal Strength（動能訊號強度）────────────────────────────────
        signal_strength = min(movers_score / 100.0, 1.0)

        # ── 2. Factor Consensus（多因子一致性）───────────────────────────────
        layer_bonus = {"core": 0.20, "medium": 0.10, "satellite": 0.0}.get(scanner_layer, 0.0)
        factor_consensus = min(scanner_score + layer_bonus, 1.0)
        # 通過因子數加成
        factor_consensus = min(factor_consensus + factor_count * 0.02, 1.0)

        # ── 3. Regime Alignment（市場狀態對齊）────────────────────────────────
        regime_score_map = {
            "BULL":     0.90,
            "RECOVERY": 0.80,
            "SIDEWAYS": 0.55,
            "BEAR":     0.25,
            "PANIC":    0.10,
            "EUPHORIA": 0.30,
            "UNKNOWN":  0.50,
        }
        base_regime    = regime_score_map.get(regime_label.upper(), 0.50)
        regime_alignment = base_regime * regime_conf + (1 - regime_conf) * 0.50

        # ── 4. Research Quality（研究品質）───────────────────────────────────
        research_base = research_pass / 6.0
        status_bonus  = {"READY": 0.15, "NEEDS_RESEARCH": 0.0, "REJECTED": -0.20}.get(
            research_status, 0.0
        )
        research_quality = max(0.0, min(research_base + status_bonus, 1.0))

        # ── 合成信心值 ────────────────────────────────────────────────────────
        conviction = (
            signal_strength  * W_SIGNAL    +
            factor_consensus * W_CONSENSUS +
            regime_alignment * W_REGIME    +
            research_quality * W_RESEARCH
        )
        conviction = round(max(0.0, min(1.0, conviction)), 4)

        # ── 倉位決策 ─────────────────────────────────────────────────────────
        if conviction >= CONVICTION_CORE_HIGH:
            layer         = "core"
            position_size = POSITION_CORE_HIGH
            note          = "信心極高，核心滿倉"
        elif conviction >= CONVICTION_CORE_LOW:
            layer         = "core"
            position_size = POSITION_CORE_LOW
            note          = "信心高，核心半倉"
        elif conviction >= CONVICTION_MEDIUM:
            layer         = "medium"
            position_size = POSITION_MEDIUM
            note          = "信心中等，衛星倉"
        else:
            layer         = "no_trade"
            position_size = 0.0
            note          = f"信心不足（{conviction:.2%}），跳過"

        return ConvictionResult(
            ticker=ticker,
            name=name or ticker,
            conviction=conviction,
            position_size=position_size,
            layer=layer,
            signal_strength=round(signal_strength, 4),
            factor_consensus=round(factor_consensus, 4),
            regime_alignment=round(regime_alignment, 4),
            research_quality=round(research_quality, 4),
            note=note,
            computed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def compute_from_pipeline(
        self,
        mover,          # MoverResult
        scan_rec,       # ScanRecord
        research_result,# ResearchResult (or None)
        regime: dict,   # from RegimeEngine
    ) -> ConvictionResult:
        """直接從各層結果計算信心值"""
        def gs(obj, attr, d=""):
            return str(getattr(obj, attr, d) or d) if hasattr(obj, attr) else d
        def gf(obj, attr, d=0.0):
            v = getattr(obj, attr, None)
            return float(v) if v is not None else d

        ticker  = gs(mover,    "stock_id", gs(scan_rec, "stock_id", ""))
        name    = gs(mover,    "name",     gs(scan_rec, "name", ticker))
        m_score = gf(mover,    "score", 50.0)
        s_score = gf(scan_rec, "score", 0.6)
        s_layer = gs(scan_rec, "layer", "medium")

        if research_result:
            r_pass   = int(gf(research_result, "auto_pass", 3))
            r_status = gs(research_result, "overall", "NEEDS_RESEARCH")
        else:
            r_pass, r_status = 3, "NEEDS_RESEARCH"

        r_label = str(regime.get("regime", "UNKNOWN")).upper()
        r_conf  = float(regime.get("confidence", 0.5))
        reasons = getattr(scan_rec, "reasons", []) or []

        return self.compute(
            ticker=ticker, name=name,
            movers_score=m_score,
            scanner_score=s_score, scanner_layer=s_layer,
            research_pass=r_pass, research_status=r_status,
            regime_label=r_label, regime_conf=r_conf,
            factor_count=len(reasons),
        )

    def batch_compute(self, pipeline_results: list[dict]) -> list[ConvictionResult]:
        """批量計算，pipeline_results 每項含 mover/scan/research/regime"""
        results = []
        for item in pipeline_results:
            try:
                r = self.compute_from_pipeline(
                    mover=item.get("mover"),
                    scan_rec=item.get("scan_rec"),
                    research_result=item.get("research"),
                    regime=item.get("regime", {}),
                )
                if r.layer != "no_trade":
                    results.append(r)
            except Exception as e:
                logger.debug("[Conviction] batch error: %s", e)
        results.sort(key=lambda r: -r.conviction)
        return results

    async def log_to_db(self, result: ConvictionResult) -> None:
        """寫入 conviction_log 資料表"""
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import ConvictionLog
            async with AsyncSessionLocal() as db:
                log = ConvictionLog(
                    ticker=result.ticker,
                    name=result.name,
                    conviction=result.conviction,
                    position_size=result.position_size,
                    layer=result.layer,
                    signal_strength=result.signal_strength,
                    factor_consensus=result.factor_consensus,
                    regime_alignment=result.regime_alignment,
                    research_quality=result.research_quality,
                    note=result.note,
                )
                db.add(log)
                await db.commit()
        except Exception as e:
            logger.debug("[Conviction] db log failed: %s", e)

    def format_batch_report(self, results: list[ConvictionResult]) -> str:
        if not results:
            return "📊 今日信心計算：無達標交易機會"
        lines = [
            f"🎯 今日信心報告（{len(results)} 檔）",
            "─" * 22,
        ]
        for r in results[:6]:
            pos = f"{r.position_size*100:.0f}%"
            cv  = f"{r.conviction:.0%}"
            lines.append(f"• {r.ticker} {r.name[:6]}  {cv}  →{pos}({r.layer})")
        return "\n".join(lines)


_engine: ConvictionEngine | None = None

def get_conviction_engine() -> ConvictionEngine:
    global _engine
    if _engine is None:
        _engine = ConvictionEngine()
    return _engine


if __name__ == "__main__":
    engine = ConvictionEngine()
    for ticker, score, layer, rpass, regime in [
        ("2330", 88, "core",   5, "BULL"),
        ("2454", 72, "medium", 3, "SIDEWAYS"),
        ("6669", 55, "medium", 2, "BEAR"),
    ]:
        r = engine.compute(
            ticker=ticker, name=ticker,
            movers_score=score, scanner_score=score/100,
            scanner_layer=layer, research_pass=rpass,
            regime_label=regime, regime_conf=0.75,
        )
        print(r.format_line())
        print()
