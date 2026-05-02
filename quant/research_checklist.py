"""
research_checklist.py — 新標的研究清單

任何新股票進入選股名單前必須通過 6 項研究確認：
  1. 近兩季財報成長
  2. 法說會 Guidance 是否上修
  3. 產業地位（龍頭或快速成長）
  4. 市場規模夠大（TAM）
  5. 競爭優勢是否清楚
  6. 成長邏輯可持續

未確認項目 → 標記「需研究」，不直接推薦

LINE 指令：/research 2330 → 自動產生研究清單報告
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# 可自動驗證的項目
AUTO_CHECK_ITEMS = [
    "revenue_growth_2q",   # 近兩季財報成長
    "eps_positive",        # EPS 為正
    "institutional_buy",   # 法人持續買超（代理 guidance 信心）
    "valuation_ok",        # 估值不過熱（PE < 60）
    "liquidity_ok",        # 流動性足夠
    "momentum_ok",         # 有正向動能
]

# 需人工確認的項目（AI 只能猜測）
MANUAL_CHECK_ITEMS = [
    "guidance_revision",   # 法說會 Guidance 上修
    "industry_position",   # 產業地位（龍頭/快速成長）
    "market_size",         # 市場規模夠大（TAM）
    "competitive_moat",    # 競爭優勢清楚
    "growth_logic",        # 成長邏輯可持續
]

ITEM_LABELS = {
    "revenue_growth_2q": "近兩季財報成長",
    "eps_positive":      "EPS 為正值",
    "institutional_buy": "法人持續買超",
    "valuation_ok":      "估值合理（PE < 60）",
    "liquidity_ok":      "流動性足夠（量 > 500 張）",
    "momentum_ok":       "有正向動能",
    "guidance_revision": "法說會 Guidance 上修",
    "industry_position": "產業地位（龍頭或快速成長）",
    "market_size":       "市場規模夠大（TAM）",
    "competitive_moat":  "競爭優勢清楚",
    "growth_logic":      "成長邏輯可持續",
}


@dataclass
class CheckItem:
    key:      str
    label:    str
    result:   str       # "pass" / "fail" / "unknown" / "needs_research"
    detail:   str = ""
    auto:     bool = True

    @property
    def icon(self) -> str:
        return {"pass": "✅", "fail": "❌",
                "unknown": "❓", "needs_research": "📋"}.get(self.result, "❓")


@dataclass
class ResearchResult:
    stock_code: str
    stock_name: str
    pass_all:   bool            # 全部通過才推薦
    auto_pass:  int             # 自動通過項目數
    auto_fail:  int             # 自動失敗項目數
    needs_research: list[str]   # 需要人工研究的項目
    items:      list[CheckItem] = field(default_factory=list)
    overall:    str = ""        # "READY" / "NEEDS_RESEARCH" / "REJECTED"
    summary:    str = ""

    def to_dict(self) -> dict:
        return {
            "code":           self.stock_code,
            "name":           self.stock_name,
            "overall":        self.overall,
            "pass_all":       self.pass_all,
            "auto_pass":      self.auto_pass,
            "auto_fail":      self.auto_fail,
            "needs_research": self.needs_research,
            "items": [
                {"key": i.key, "label": i.label, "result": i.result,
                 "detail": i.detail, "icon": i.icon}
                for i in self.items
            ],
            "summary": self.summary,
        }

    def format_line(self) -> str:
        icon_map = {"READY": "🟢", "NEEDS_RESEARCH": "🟡", "REJECTED": "🔴"}
        icon     = icon_map.get(self.overall, "📋")
        lines    = [
            f"{icon} {self.stock_code} {self.stock_name} 研究清單",
            "─" * 22,
        ]
        for item in self.items:
            lines.append(f"{item.icon} {item.label}")
            if item.detail:
                lines.append(f"   {item.detail}")
        lines.append("")
        lines.append(f"📋 結論：{self.summary}")
        if self.needs_research:
            lines.append("需確認：" + "、".join(self.needs_research[:3]))
        return "\n".join(lines)


class ResearchChecklist:
    """
    個股研究清單產生器。

    使用方式：
        checker = ResearchChecklist()
        result  = await checker.check("2330")
        print(result.format_line())
    """

    async def check(self, stock_code: str) -> ResearchResult:
        """從多個資料來源自動填寫研究清單"""
        # 嘗試取得股票基本資料
        data = await self._fetch_stock_data(stock_code)
        return self._evaluate(stock_code, data)

    def check_from_data(self, stock_code: str, data: dict) -> ResearchResult:
        """同步版本，直接傳入資料 dict"""
        return self._evaluate(stock_code, data)

    async def _fetch_stock_data(self, stock_code: str) -> dict:
        """從多個來源彙整資料"""
        data: dict = {"code": stock_code}
        try:
            from backend.services.twse_service import fetch_realtime_quote
            q = await fetch_realtime_quote(stock_code)
            if q:
                data.update({
                    "close":      float(q.get("price", 0)),
                    "change_pct": float(q.get("change_pct", 0)),
                    "name":       q.get("name", stock_code),
                })
        except Exception:
            pass

        try:
            from backend.services.report_screener import all_screener
            rows = all_screener(limit=300)
            for row in rows:
                rc = row.stock_id if hasattr(row, "stock_id") else ""
                if rc == stock_code:
                    data.update({
                        "rev_yoy":         getattr(row, "rev_yoy", 0),
                        "eps_growth":      getattr(row, "eps_growth", 0),
                        "pe_ratio":        getattr(row, "pe_ratio", 20),
                        "foreign_buy_days":getattr(row, "foreign_buy_days", 0),
                        "volume":          getattr(row, "volume", 0),
                        "momentum_score":  getattr(row, "momentum_score", 50),
                        "model_score":     getattr(row, "model_score", 50),
                        "sector":          getattr(row, "sector", ""),
                        "name":            getattr(row, "name", data.get("name", stock_code)),
                    })
                    break
        except Exception:
            pass

        return data

    def _evaluate(self, stock_code: str, data: dict) -> ResearchResult:
        name     = data.get("name", stock_code)
        rev_yoy  = float(data.get("rev_yoy", 0))
        eps_g    = float(data.get("eps_growth", 0))
        pe       = float(data.get("pe_ratio", 20))
        f_days   = int(data.get("foreign_buy_days", 0))
        vol      = float(data.get("volume", 0))
        mom_sc   = float(data.get("momentum_score", 50))
        sector   = str(data.get("sector", ""))

        items: list[CheckItem] = []

        # ── 自動可驗證項目 ─────────────────────────────────────────────────
        # 1. 近兩季財報成長
        if rev_yoy > 0.05 or eps_g > 0.05:
            items.append(CheckItem("revenue_growth_2q", ITEM_LABELS["revenue_growth_2q"],
                                   "pass", f"營收YoY {rev_yoy*100:+.1f}%  EPS成長 {eps_g*100:+.1f}%"))
        elif rev_yoy < -0.10:
            items.append(CheckItem("revenue_growth_2q", ITEM_LABELS["revenue_growth_2q"],
                                   "fail", f"營收衰退 {rev_yoy*100:.1f}%"))
        else:
            items.append(CheckItem("revenue_growth_2q", ITEM_LABELS["revenue_growth_2q"],
                                   "unknown", "數據不足，需查近兩季財報"))

        # 2. EPS 為正
        if eps_g >= 0 and mom_sc >= 45:
            items.append(CheckItem("eps_positive", ITEM_LABELS["eps_positive"],
                                   "pass", f"EPS趨勢正向"))
        elif eps_g < -0.20:
            items.append(CheckItem("eps_positive", ITEM_LABELS["eps_positive"],
                                   "fail", f"EPS衰退 {eps_g*100:.1f}%"))
        else:
            items.append(CheckItem("eps_positive", ITEM_LABELS["eps_positive"],
                                   "unknown", "需查近兩季 EPS"))

        # 3. 法人持續買超（Guidance 代理）
        if f_days >= 3:
            items.append(CheckItem("institutional_buy", ITEM_LABELS["institutional_buy"],
                                   "pass", f"外資連買 {f_days} 日"))
        elif f_days <= -3:
            items.append(CheckItem("institutional_buy", ITEM_LABELS["institutional_buy"],
                                   "fail", f"外資連賣 {abs(f_days)} 日"))
        else:
            items.append(CheckItem("institutional_buy", ITEM_LABELS["institutional_buy"],
                                   "unknown", "法人動向不明確"))

        # 4. 估值合理
        if 0 < pe <= 40:
            items.append(CheckItem("valuation_ok", ITEM_LABELS["valuation_ok"],
                                   "pass", f"PE {pe:.0f}"))
        elif pe > 60:
            items.append(CheckItem("valuation_ok", ITEM_LABELS["valuation_ok"],
                                   "fail", f"PE {pe:.0f} 過高"))
        else:
            items.append(CheckItem("valuation_ok", ITEM_LABELS["valuation_ok"],
                                   "unknown", "PE 待確認"))

        # 5. 流動性
        vol_k = vol / 1000
        if vol_k >= 500:
            items.append(CheckItem("liquidity_ok", ITEM_LABELS["liquidity_ok"],
                                   "pass", f"日均量 {vol_k:.0f} 張"))
        elif vol_k > 0:
            items.append(CheckItem("liquidity_ok", ITEM_LABELS["liquidity_ok"],
                                   "fail", f"日均量 {vol_k:.0f} 張 < 500 張"))
        else:
            items.append(CheckItem("liquidity_ok", ITEM_LABELS["liquidity_ok"],
                                   "unknown", "量能待確認"))

        # 6. 正向動能
        if mom_sc >= 60:
            items.append(CheckItem("momentum_ok", ITEM_LABELS["momentum_ok"],
                                   "pass", f"動能分 {mom_sc:.0f}"))
        elif mom_sc < 35:
            items.append(CheckItem("momentum_ok", ITEM_LABELS["momentum_ok"],
                                   "fail", f"動能弱（{mom_sc:.0f}）"))
        else:
            items.append(CheckItem("momentum_ok", ITEM_LABELS["momentum_ok"],
                                   "unknown", "動能中性"))

        # ── 需人工確認項目（標記需研究）─────────────────────────────────────
        manual_keys = ["guidance_revision", "industry_position",
                       "market_size", "competitive_moat", "growth_logic"]
        for key in manual_keys:
            items.append(CheckItem(
                key=key, label=ITEM_LABELS[key],
                result="needs_research",
                detail="需人工確認",
                auto=False,
            ))

        # ── 判斷整體結論 ──────────────────────────────────────────────────
        auto_items    = [i for i in items if i.auto]
        auto_pass     = sum(1 for i in auto_items if i.result == "pass")
        auto_fail     = sum(1 for i in auto_items if i.result == "fail")
        needs_research= [ITEM_LABELS[k] for k in manual_keys]

        if auto_fail >= 2:
            overall  = "REJECTED"
            pass_all = False
            summary  = f"自動項目失敗 {auto_fail} 項，建議排除此標的"
        elif auto_pass >= 4 and auto_fail == 0:
            overall  = "NEEDS_RESEARCH"   # 需確認人工項目後才推薦
            pass_all = False
            summary  = f"自動核查通過 {auto_pass}/6，需確認 {len(manual_keys)} 項人工研究點"
        else:
            overall  = "NEEDS_RESEARCH"
            pass_all = False
            summary  = f"資料不足（通過 {auto_pass}，失敗 {auto_fail}，未知 {len(auto_items)-auto_pass-auto_fail}），需補充研究"

        return ResearchResult(
            stock_code=stock_code,
            stock_name=name,
            pass_all=pass_all,
            auto_pass=auto_pass,
            auto_fail=auto_fail,
            needs_research=needs_research[:3],
            items=items,
            overall=overall,
            summary=summary,
        )


_global_checker: Optional[ResearchChecklist] = None

def get_research_checklist() -> ResearchChecklist:
    global _global_checker
    if _global_checker is None:
        _global_checker = ResearchChecklist()
    return _global_checker


if __name__ == "__main__":
    import asyncio

    async def _test():
        checker = ResearchChecklist()
        result  = await checker.check("2330")
        print(result.format_line())

    asyncio.run(_test())
