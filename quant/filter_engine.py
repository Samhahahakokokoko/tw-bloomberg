"""
filter_engine.py — Layer 3: 六大過濾器

任一觸發即踢出：
  1. 流動性：avg_volume_20d < 500 張
  2. 營收趨勢：revenue_yoy < -10%
  3. 估值：PE > 60 且無高成長支撐（rev_yoy < 20%）
  4. 籌碼：外資連續賣超 > 10 日
  5. 炒作偵測：單日爆量 > 5x 均量 且無基本面（f_days<=0 且 rev_yoy<0）
  6. 循環股：鋼鐵/航運/塑化 非景氣上升期（rev_yoy < 0）

輸出：{"passed": [...], "rejected": [...], "reason": {stock_id: [reasons...]}}
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

CYCLICAL_SECTORS = {"鋼鐵", "航運", "散裝", "貨櫃", "塑化", "原物料", "石化"}

# 估值過熱例外：高成長股可接受高PE
HIGH_GROWTH_REV_THRESHOLD = 0.20   # 營收YoY > 20% 可接受高PE


@dataclass
class FilterRecord:
    stock_id:  str
    name:      str
    passed:    bool
    reasons:   list[str] = field(default_factory=list)  # 失敗原因


class FilterEngine:
    """
    六大過濾器。接受任意含有基本欄位的物件列表，輸出通過/未通過分組。

    使用方式：
        engine = FilterEngine()
        result = engine.filter(candidates)
        # result["passed"]   → 通過的物件列表
        # result["rejected"] → 被篩掉的物件列表
        # result["reason"]   → {stock_id: [reason, ...]}
    """

    def __init__(self, allow_cyclical_trend: bool = False):
        """allow_cyclical_trend: 景氣上升期可放行循環股"""
        self.allow_cyclical = allow_cyclical_trend

    def filter(self, candidates: list) -> dict:
        """
        主過濾函式。
        回傳 {"passed": [...], "rejected": [...], "reason": {stock_id: [reasons]}}
        """
        passed:   list = []
        rejected: list = []
        reason:   dict[str, list[str]] = {}

        # 逐一過濾並統計各規則命中數
        rule_hits: dict[str, int] = {
            "流動性": 0, "營收": 0, "估值": 0,
            "籌碼": 0, "炒作": 0, "循環": 0,
        }

        for item in candidates:
            rec = self._check(item)
            if rec.passed:
                passed.append(item)
            else:
                rejected.append(item)
                reason[rec.stock_id] = rec.reasons
                for r in rec.reasons:
                    if "流動性" in r:   rule_hits["流動性"] += 1
                    elif "營收" in r:   rule_hits["營收"]   += 1
                    elif "估值" in r:   rule_hits["估值"]   += 1
                    elif "籌碼" in r:   rule_hits["籌碼"]   += 1
                    elif "炒作" in r:   rule_hits["炒作"]   += 1
                    elif "循環" in r:   rule_hits["循環"]   += 1

        n_in = len(candidates)
        logger.info("[Filter] 輸入=%d → 通過=%d → 排除=%d", n_in, len(passed), len(rejected))
        for rule, hits in rule_hits.items():
            if hits > 0:
                logger.info("[Filter] 過濾器[%s]：排除 %d 檔", rule, hits)

        return {"passed": passed, "rejected": rejected, "reason": reason}

    def filter_df(self, df) -> tuple:
        """接受 DataFrame，回傳 (passed_df, rejected_df, reason_dict)"""
        import pandas as pd
        if df is None or len(df) == 0:
            return pd.DataFrame(), pd.DataFrame(), {}

        result  = self.filter(df.to_dict("records"))
        pass_ids = {self._get_id(r) for r in result["passed"]}
        passed_df  = df[df["stock_id"].isin(pass_ids)].reset_index(drop=True)
        rejected_df= df[~df["stock_id"].isin(pass_ids)].reset_index(drop=True)
        return passed_df, rejected_df, result["reason"]

    def _check(self, item) -> FilterRecord:
        def g(attr, d=0.0):
            if hasattr(item, attr):
                v = getattr(item, attr)
                return float(v) if v is not None else d
            if isinstance(item, dict):
                return float(item.get(attr, d) or d)
            return d
        def gs(attr, d=""):
            if hasattr(item, attr): return str(getattr(item, attr) or d)
            if isinstance(item, dict): return str(item.get(attr, d) or d)
            return d

        stock_id   = gs("stock_id", gs("code", ""))
        name       = gs("name", stock_id)
        sector     = gs("sector", "其他")
        avg_vol_k  = g("avg_volume_k", g("volume", 0) / 1000)
        rev_yoy    = g("rev_yoy", 0)
        pe_ratio   = g("pe_ratio", 20)
        f_days     = int(g("foreign_buy_days", 0))
        vol_r      = g("volume_ratio", 1.0)
        eps_growth = g("eps_growth", 0)

        reasons: list[str] = []

        # ── Filter 1: 流動性 ──────────────────────────────────────────
        if avg_vol_k > 0 and avg_vol_k < 500:
            reasons.append(f"流動性不足：均量 {avg_vol_k:.0f} 張 < 500 張")

        # ── Filter 2: 營收趨勢 ────────────────────────────────────────
        if rev_yoy < -0.10:
            reasons.append(f"營收惡化：YoY {rev_yoy*100:.1f}% < -10%")

        # ── Filter 3: 估值（高PE且無高成長支撐）───────────────────────
        if pe_ratio > 60 and rev_yoy < HIGH_GROWTH_REV_THRESHOLD:
            reasons.append(f"估值過熱：PE {pe_ratio:.0f} > 60，YoY僅{rev_yoy*100:.0f}%")

        # ── Filter 4: 籌碼轉弱（外資連賣 > 10 日）───────────────────
        if f_days < -10:
            reasons.append(f"籌碼轉弱：外資連賣 {abs(f_days)} 日 > 10 日")

        # ── Filter 5: 炒作偵測（爆量 > 5x 且無基本面）────────────────
        if vol_r > 5.0 and f_days <= 0 and rev_yoy < 0:
            reasons.append(f"疑似炒作：量比 {vol_r:.1f}x > 5x，無法人且營收衰退")

        # ── Filter 6: 循環股非景氣期 ─────────────────────────────────
        if not self.allow_cyclical:
            is_cyclical = any(s in sector for s in CYCLICAL_SECTORS)
            if is_cyclical and rev_yoy < 0:
                reasons.append(f"循環股非景氣上升期：{sector} YoY {rev_yoy*100:.1f}%")

        return FilterRecord(
            stock_id=stock_id,
            name=name,
            passed=len(reasons) == 0,
            reasons=reasons,
        )

    @staticmethod
    def _get_id(item) -> str:
        if hasattr(item, "stock_id"):   return item.stock_id
        if isinstance(item, dict):
            return item.get("stock_id", item.get("code", ""))
        return ""

    def summary(self, result: dict) -> str:
        passed   = result["passed"]
        rejected = result["rejected"]
        reason   = result["reason"]
        lines    = [
            f"🔍 過濾結果：通過 {len(passed)} / 排除 {len(rejected)}",
            "─" * 22,
        ]
        if reason:
            rule_counts: dict[str, int] = {}
            for reasons in reason.values():
                for r in reasons:
                    key = r.split("：")[0]
                    rule_counts[key] = rule_counts.get(key, 0) + 1
            lines.append("排除原因：")
            for k, v in sorted(rule_counts.items(), key=lambda x: -x[1]):
                lines.append(f"  {k}: {v} 檔")
        if passed:
            ids = [self._get_id(p) for p in passed[:5]]
            lines.append(f"✅ 通過：{', '.join(ids)}")
        return "\n".join(lines)


def get_filter_engine() -> FilterEngine:
    return FilterEngine()


if __name__ == "__main__":
    from quant.movers_engine import MoversEngine
    movers = MoversEngine().scan_mock(20)
    engine = FilterEngine()
    result = engine.filter(movers)
    print(engine.summary(result))
