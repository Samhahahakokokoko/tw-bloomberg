"""
filter_engine.py — 垃圾清洗過濾器（這步最重要）

砍掉以下條件：
  1. 日均成交量 < 500 張（流動性太差）
  2. 營收年增率 < -10%（基本面惡化）
  3. 本益比 > 60（估值過熱無基本面支撐）
  4. 近3月外資連續賣超（籌碼轉弱）
  5. 純情緒炒作（Buzz 高但無法人買盤）
  6. 商品循環股非趨勢期（鋼鐵/航運）

只留下「真正值得研究的股票」
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── 過濾門檻 ──────────────────────────────────────────────────────────────────
MIN_VOL_K         = 500      # 日均成交量最低 500 張
MAX_REV_DECLINE   = -0.10    # 營收年增 > -10%（低於此砍掉）
MAX_PE_RATIO      = 60.0     # 本益比 ≤ 60
FOREIGN_SELL_DAYS = -3       # 外資連賣 ≥ 3 天 → 砍掉（用負值）
BUZZ_NO_INST_BUZZ = 50       # Buzz > 50 但外資/投信 ≤ 0 → 純炒作
CYCLICAL_SECTORS  = {"鋼鐵", "航運", "散裝", "貨櫃", "原物料", "塑膠"}


@dataclass
class FilterReason:
    rule:    str
    detail:  str
    blocked: bool


@dataclass
class FilterResult:
    stock_code:  str
    stock_name:  str
    passed:      bool
    fail_reasons: list[FilterReason] = field(default_factory=list)
    pass_notes:   list[str]          = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "code":    self.stock_code,
            "name":    self.stock_name,
            "passed":  self.passed,
            "fail_reasons": [{"rule": r.rule, "detail": r.detail}
                              for r in self.fail_reasons if r.blocked],
            "pass_notes": self.pass_notes,
        }


class FilterEngine:
    """
    垃圾清洗過濾器。接受候選股票列表，逐一檢查 6 條過濾規則。

    使用方式：
        engine = FilterEngine()
        passed, rejected = engine.filter(candidates)
    """

    def __init__(
        self,
        min_vol_k:       float = MIN_VOL_K,
        max_rev_decline: float = MAX_REV_DECLINE,
        max_pe:          float = MAX_PE_RATIO,
        allow_cyclical:  bool  = False,   # 趨勢期可以放行循環股
    ):
        self.min_vol_k       = min_vol_k
        self.max_rev_decline = max_rev_decline
        self.max_pe          = max_pe
        self.allow_cyclical  = allow_cyclical

    def filter(self, candidates: list) -> tuple[list, list]:
        """
        回傳 (passed_list, rejected_list)，皆為 FilterResult。
        """
        passed:   list[FilterResult] = []
        rejected: list[FilterResult] = []

        for item in candidates:
            result = self._check(item)
            (passed if result.passed else rejected).append(result)

        return passed, rejected

    def filter_candidates(self, candidates: list) -> list:
        """
        只回傳通過過濾的原始物件（保留輸入型別）。
        """
        passed_results, _ = self.filter(candidates)
        passed_codes = {r.stock_code for r in passed_results}
        return [c for c in candidates
                if self._get_code(c) in passed_codes]

    def _check(self, item) -> FilterResult:
        def _g(attr, default=0.0):
            if hasattr(item, attr):
                return getattr(item, attr)
            if isinstance(item, dict):
                return item.get(attr, default)
            return default

        code     = str(_g("stock_code", _g("stock_id", _g("code", ""))))
        name     = str(_g("stock_name", _g("name", code)))
        sector   = str(_g("sector", "其他"))
        vol_k    = float(_g("volume", 0)) / 1000   # volume in shares → lots
        rev_yoy  = float(_g("rev_yoy", 0))
        pe_ratio = float(_g("pe_ratio", 20))
        f_days   = int(_g("foreign_buy_days", 0))
        trust_net= float(_g("trust_net", 0))
        buzz     = float(_g("buzz_score", 0))
        model_sc = float(_g("model_score", 50))

        # 若 volume 已是張（如 StockRow volume 是股數），再除以 1000
        # 但有時 vol_ratio 欄位會是量比，需要額外處理
        if vol_k < 1:   # StockRow.volume 是股數（已知格式）
            vol_k_raw = float(_g("volume", 0))
            # StockRow.volume = 股數，1張=1000股
            vol_k = vol_k_raw / 1000 if vol_k_raw > 1000 else vol_k_raw

        reasons: list[FilterReason] = []
        pass_notes: list[str] = []

        # ── Rule 1: 流動性 ──────────────────────────────────────────────
        if vol_k > 0 and vol_k < self.min_vol_k:
            reasons.append(FilterReason(
                rule="liquidity",
                detail=f"日均量 {vol_k:.0f} 張 < {self.min_vol_k:.0f} 張",
                blocked=True,
            ))
        else:
            pass_notes.append("流動性 OK")

        # ── Rule 2: 營收趨勢 ────────────────────────────────────────────
        if rev_yoy < self.max_rev_decline:
            reasons.append(FilterReason(
                rule="revenue",
                detail=f"營收YoY {rev_yoy*100:.1f}% < {self.max_rev_decline*100:.0f}%",
                blocked=True,
            ))
        else:
            pass_notes.append(f"營收 {rev_yoy*100:+.0f}% OK")

        # ── Rule 3: 估值 ────────────────────────────────────────────────
        if pe_ratio > self.max_pe:
            reasons.append(FilterReason(
                rule="valuation",
                detail=f"PE {pe_ratio:.0f} > {self.max_pe:.0f}（估值過熱）",
                blocked=True,
            ))
        elif pe_ratio > 0:
            pass_notes.append(f"PE {pe_ratio:.0f} 合理")

        # ── Rule 4: 外資籌碼 ────────────────────────────────────────────
        if f_days <= FOREIGN_SELL_DAYS:
            reasons.append(FilterReason(
                rule="institutional",
                detail=f"外資連賣 {abs(f_days)} 日",
                blocked=True,
            ))
        elif f_days >= 1:
            pass_notes.append(f"外資買超 {f_days} 日")

        # ── Rule 5: 純情緒炒作 ─────────────────────────────────────────
        if buzz >= BUZZ_NO_INST_BUZZ and f_days <= 0 and trust_net <= 0:
            reasons.append(FilterReason(
                rule="speculation",
                detail=f"Buzz={buzz:.0f} 高但無法人買盤（純炒作）",
                blocked=True,
            ))

        # ── Rule 6: 循環股非趨勢期 ─────────────────────────────────────
        if not self.allow_cyclical and any(s in sector for s in CYCLICAL_SECTORS):
            if rev_yoy < 0 or f_days < 0:
                reasons.append(FilterReason(
                    rule="cyclical",
                    detail=f"{sector} 循環股非趨勢期",
                    blocked=True,
                ))

        blocked = [r for r in reasons if r.blocked]
        passed  = len(blocked) == 0

        return FilterResult(
            stock_code=code,
            stock_name=name,
            passed=passed,
            fail_reasons=blocked,
            pass_notes=pass_notes if passed else [],
        )

    @staticmethod
    def _get_code(item) -> str:
        if hasattr(item, "stock_code"): return item.stock_code
        if hasattr(item, "stock_id"):   return item.stock_id
        if isinstance(item, dict):
            return item.get("stock_code", item.get("stock_id", item.get("code", "")))
        return ""

    def summary_report(self, passed: list, rejected: list) -> str:
        """格式化過濾結果摘要"""
        lines = [
            f"🔍 過濾結果：通過 {len(passed)} 檔，排除 {len(rejected)} 檔",
            "─" * 22,
        ]
        if rejected:
            lines.append("❌ 排除原因統計：")
            rule_counts: dict[str, int] = {}
            for r in rejected:
                for fr in r.fail_reasons:
                    rule_counts[fr.rule] = rule_counts.get(fr.rule, 0) + 1
            for rule, count in sorted(rule_counts.items(), key=lambda x: -x[1]):
                rule_names = {
                    "liquidity":    "流動性不足",
                    "revenue":      "營收惡化",
                    "valuation":    "估值過熱",
                    "institutional":"籌碼轉弱",
                    "speculation":  "純情緒炒作",
                    "cyclical":     "循環股非趨勢",
                }
                lines.append(f"  {rule_names.get(rule, rule)}: {count} 檔")
        if passed:
            lines.append(f"\n✅ 通過（值得研究）：")
            for r in passed[:6]:
                lines.append(f"  {r.stock_code} {r.stock_name}")
        return "\n".join(lines)


_global_filter: Optional[FilterEngine] = None

def get_filter_engine() -> FilterEngine:
    global _global_filter
    if _global_filter is None:
        _global_filter = FilterEngine()
    return _global_filter


if __name__ == "__main__":
    from quant.movers_engine import MoversEngine
    movers = MoversEngine().scan_mock()
    engine = FilterEngine()
    passed, rejected = engine.filter(movers)
    print(engine.summary_report(passed, rejected))
