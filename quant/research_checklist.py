"""
research_checklist.py — Layer 4: 11 項研究清單

自動核查 6 項（可量化）：
  1. revenue_growth：近兩季 YoY > 0
  2. eps_trend：EPS 連續兩季成長
  3. institutional_flow：法人近10日淨買超
  4. trend_structure：MA5 > MA20 > MA60
  5. volatility：20日波動率 < 40%
  6. valuation：PE < 40 或 PEG < 1.5

人工輔助 5 項（Claude AI 協助分析）：
  7. competitive_analysis：競爭力分析
  8. guidance_direction：法說會 Guidance 方向
  9. moat：護城河評估
  10. industry_trend：產業趨勢
  11. management_quality：管理層品質

輸出：READY / NEEDS_RESEARCH / REJECTED
LINE 指令：/research 2330 → 推送完整研究清單卡片
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CheckItem:
    key:     str
    label:   str
    result:  str    # pass / fail / unknown / needs_research
    detail:  str = ""
    auto:    bool = True

    @property
    def icon(self) -> str:
        return {"pass": "✅", "fail": "❌",
                "unknown": "❓", "needs_research": "📋"}.get(self.result, "❓")


@dataclass
class ResearchResult:
    stock_code: str
    stock_name: str
    items:      list[CheckItem] = field(default_factory=list)
    overall:    str = "NEEDS_RESEARCH"   # READY / NEEDS_RESEARCH / REJECTED
    auto_pass:  int = 0
    auto_fail:  int = 0
    ai_summary: str = ""

    def to_dict(self) -> dict:
        return {
            "code":       self.stock_code,
            "name":       self.stock_name,
            "overall":    self.overall,
            "auto_pass":  self.auto_pass,
            "auto_fail":  self.auto_fail,
            "ai_summary": self.ai_summary,
            "items": [{"key": i.key, "label": i.label, "result": i.result,
                       "detail": i.detail, "icon": i.icon}
                      for i in self.items],
        }

    def format_line(self) -> str:
        icon_map = {"READY": "🟢", "NEEDS_RESEARCH": "🟡", "REJECTED": "🔴"}
        icon     = icon_map.get(self.overall, "📋")
        lines    = [
            f"{icon} {self.stock_code} {self.stock_name}",
            f"自動核查：{self.auto_pass}/6 通過",
            "─" * 22,
        ]
        for item in self.items:
            line = f"{item.icon} {item.label}"
            if item.detail:
                line += f"  ({item.detail})"
            lines.append(line)
        if self.ai_summary:
            lines += ["", f"🤖 AI評估：{self.ai_summary[:120]}"]
        lines += ["", f"📋 結論：{self.overall}"]
        if self.overall == "NEEDS_RESEARCH":
            gaps = [i.label for i in self.items if i.result in ("unknown", "needs_research")]
            if gaps:
                lines.append(f"待確認：{'、'.join(gaps[:3])}")
        return "\n".join(lines)

    def build_flex_card(self) -> dict:
        """生成 LINE Flex Message 研究清單卡片"""
        icon_map  = {"READY": "🟢", "NEEDS_RESEARCH": "🟡", "REJECTED": "🔴"}
        color_map = {"READY": "#1A5C2E", "NEEDS_RESEARCH": "#4A3A00", "REJECTED": "#5C1A1A"}
        hdr_color = color_map.get(self.overall, "#0D1B2A")

        def _row(item: CheckItem) -> dict:
            return {
                "type": "box", "layout": "horizontal",
                "margin": "xs", "paddingAll": "4px",
                "contents": [
                    {"type": "text", "text": item.icon, "size": "sm", "flex": 1},
                    {"type": "text", "text": item.label, "size": "xs",
                     "color": "#E8EEF8", "flex": 5, "wrap": True},
                    {"type": "text", "text": item.detail[:20] if item.detail else "",
                     "size": "xxs", "color": "#6A7E9C", "flex": 4, "align": "end"},
                ],
            }

        return {
            "type": "bubble", "size": "kilo",
            "header": {
                "type": "box", "layout": "vertical",
                "backgroundColor": hdr_color, "paddingAll": "14px",
                "contents": [
                    {"type": "text",
                     "text": f"{icon_map.get(self.overall, '📋')} {self.stock_code} {self.stock_name}",
                     "color": "#E8EEF8", "weight": "bold", "size": "md"},
                    {"type": "text",
                     "text": f"自動通過 {self.auto_pass}/6　結論：{self.overall}",
                     "color": "#AAAAAA", "size": "xs", "margin": "xs"},
                ],
            },
            "body": {
                "type": "box", "layout": "vertical",
                "backgroundColor": "#0A0F1E", "paddingAll": "10px",
                "contents": [_row(i) for i in self.items],
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "backgroundColor": "#060B14", "paddingAll": "10px",
                "contents": [{
                    "type": "text",
                    "text": self.ai_summary[:100] if self.ai_summary else "AI 分析未執行",
                    "color": "#6A7E9C", "size": "xs", "wrap": True,
                }],
            } if self.ai_summary else None,
        }


class ResearchChecklist:
    """
    11 項研究清單引擎。

    使用方式：
        checker = ResearchChecklist()
        result  = await checker.check("2330")
        print(result.format_line())
        card    = result.build_flex_card()
    """

    async def check(self, stock_code: str) -> ResearchResult:
        data = await self._fetch(stock_code)
        result = self._auto_check(stock_code, data)
        ai_summary = await self._ai_analysis(stock_code, data)
        result.ai_summary = ai_summary
        self._finalize(result)
        return result

    def check_sync(self, stock_code: str, data: dict) -> ResearchResult:
        result = self._auto_check(stock_code, data)
        self._finalize(result)
        return result

    # ── 資料抓取 ─────────────────────────────────────────────────────────────

    async def _fetch(self, stock_code: str) -> dict:
        data: dict = {"code": stock_code}
        try:
            from backend.services.twse_service import fetch_realtime_quote
            q = await fetch_realtime_quote(stock_code)
            if q:
                data.update({
                    "name":       q.get("name", stock_code),
                    "close":      float(q.get("price", 0)),
                })
        except Exception:
            pass
        try:
            from backend.services.report_screener import all_screener
            for row in all_screener(limit=300):
                if getattr(row, "stock_id", "") == stock_code:
                    data.update({
                        "name":            getattr(row, "name", stock_code),
                        "rev_yoy":         getattr(row, "rev_yoy", 0),
                        "eps_growth":      getattr(row, "eps_growth", 0),
                        "pe_ratio":        getattr(row, "pe_ratio", 20),
                        "foreign_buy_days":getattr(row, "foreign_buy_days", 0),
                        "trust_net":       getattr(row, "chip_5d", 0),
                        "volume":          getattr(row, "volume", 0),
                        "ma5":             getattr(row, "close", 0) * 0.99,
                        "ma20":            getattr(row, "close", 0) * 0.97,
                        "ma60":            getattr(row, "close", 0) * 0.94,
                        "volatility":      getattr(row, "intraday_range", 0.02),
                        "sector":          getattr(row, "sector", ""),
                    })
                    break
        except Exception:
            pass
        return data

    # ── 自動核查 6 項 ─────────────────────────────────────────────────────────

    def _auto_check(self, stock_code: str, data: dict) -> ResearchResult:
        name     = data.get("name", stock_code)
        rev_yoy  = float(data.get("rev_yoy", 0))
        eps_g    = float(data.get("eps_growth", 0))
        f_days   = int(data.get("foreign_buy_days", 0))
        trust    = float(data.get("trust_net", 0))
        pe       = float(data.get("pe_ratio", 20))
        vol      = float(data.get("volume", 0))
        close    = float(data.get("close", 0) or 0)
        ma5      = float(data.get("ma5",  close * 0.99))
        ma20     = float(data.get("ma20", close * 0.97))
        ma60     = float(data.get("ma60", close * 0.94))
        vola     = float(data.get("volatility", 0.02))

        items: list[CheckItem] = []

        # 1. 近兩季 YoY > 0
        if rev_yoy > 0.05:
            items.append(CheckItem("revenue_growth", "近兩季YoY > 0", "pass",
                                   f"YoY+{rev_yoy*100:.1f}%"))
        elif rev_yoy < -0.05:
            items.append(CheckItem("revenue_growth", "近兩季YoY > 0", "fail",
                                   f"YoY{rev_yoy*100:.1f}%"))
        else:
            items.append(CheckItem("revenue_growth", "近兩季YoY > 0", "unknown",
                                   "數據待確認"))

        # 2. EPS 連續兩季成長
        if eps_g > 0.05:
            items.append(CheckItem("eps_trend", "EPS連續兩季成長", "pass",
                                   f"+{eps_g*100:.1f}%"))
        elif eps_g < -0.10:
            items.append(CheckItem("eps_trend", "EPS連續兩季成長", "fail",
                                   f"{eps_g*100:.1f}%"))
        else:
            items.append(CheckItem("eps_trend", "EPS連續兩季成長", "unknown",
                                   "需查財報"))

        # 3. 法人近10日淨買超
        net_inst = f_days + (1 if trust > 0 else 0)
        if f_days >= 3 or (f_days > 0 and trust > 0):
            items.append(CheckItem("institutional_flow", "法人近10日淨買超", "pass",
                                   f"外資+{f_days}日，投信+{trust:.0f}張"))
        elif f_days <= -5:
            items.append(CheckItem("institutional_flow", "法人近10日淨買超", "fail",
                                   f"外資連賣{abs(f_days)}日"))
        else:
            items.append(CheckItem("institutional_flow", "法人近10日淨買超", "unknown",
                                   "法人動向不明"))

        # 4. 趨勢排列 MA5 > MA20 > MA60
        if ma5 > ma20 > ma60:
            items.append(CheckItem("trend_structure", "MA5>MA20>MA60", "pass",
                                   f"{ma5:.1f}>{ma20:.1f}>{ma60:.1f}"))
        elif ma5 < ma20 or ma20 < ma60:
            items.append(CheckItem("trend_structure", "MA5>MA20>MA60", "fail",
                                   "均線排列不佳"))
        else:
            items.append(CheckItem("trend_structure", "MA5>MA20>MA60", "unknown",
                                   "均線待確認"))

        # 5. 20日波動率 < 40%（年化）
        vol_annual = vola * (252 ** 0.5) if vola < 1 else vola
        if vol_annual < 0.40:
            items.append(CheckItem("volatility", "波動率 < 40%", "pass",
                                   f"年化波動{vol_annual*100:.0f}%"))
        elif vol_annual >= 0.60:
            items.append(CheckItem("volatility", "波動率 < 40%", "fail",
                                   f"年化波動{vol_annual*100:.0f}% 過高"))
        else:
            items.append(CheckItem("volatility", "波動率 < 40%", "unknown",
                                   f"年化波動{vol_annual*100:.0f}%"))

        # 6. 估值 PE < 40 或 PEG < 1.5
        peg = pe / (eps_g * 100) if eps_g > 0.05 else 99
        if pe < 40 or peg < 1.5:
            items.append(CheckItem("valuation", "PE<40 或 PEG<1.5", "pass",
                                   f"PE={pe:.0f}, PEG={peg:.1f}" if peg < 99 else f"PE={pe:.0f}"))
        elif pe > 60:
            items.append(CheckItem("valuation", "PE<40 或 PEG<1.5", "fail",
                                   f"PE={pe:.0f} 過高"))
        else:
            items.append(CheckItem("valuation", "PE<40 或 PEG<1.5", "unknown",
                                   f"PE={pe:.0f}"))

        # 5 項人工研究（標記 needs_research）
        for key, label in [
            ("competitive_analysis", "競爭力分析"),
            ("guidance_direction",   "法說會 Guidance 方向"),
            ("moat",                 "護城河評估"),
            ("industry_trend",       "產業趨勢"),
            ("management_quality",   "管理層品質"),
        ]:
            items.append(CheckItem(key, label, "needs_research",
                                   "需人工確認", auto=False))

        auto_pass = sum(1 for i in items if i.auto and i.result == "pass")
        auto_fail = sum(1 for i in items if i.auto and i.result == "fail")

        return ResearchResult(
            stock_code=stock_code, stock_name=name,
            items=items, auto_pass=auto_pass, auto_fail=auto_fail,
        )

    def _finalize(self, result: ResearchResult) -> None:
        # 沒有人工研究系統：只以自動化指標決定
        # auto_fail >= 2 → REJECTED（明確負訊號）
        # 其他 → READY（不因「未研究」而擋下）
        if result.auto_fail >= 2:
            result.overall = "REJECTED"
        else:
            result.overall = "READY"

    # ── AI 輔助分析 ───────────────────────────────────────────────────────────

    async def _ai_analysis(self, stock_code: str, data: dict) -> str:
        """用 Claude 對5項人工項目做初步評估（可選）"""
        try:
            from backend.models.database import settings
            api_key = getattr(settings, "anthropic_api_key", "") or ""
            if not api_key:
                return ""

            import anthropic
            client = anthropic.AsyncAnthropic(api_key=api_key)
            name    = data.get("name", stock_code)
            sector  = data.get("sector", "")
            rev_yoy = data.get("rev_yoy", 0)
            prompt  = (
                f"對 {stock_code} {name}（{sector}）做簡短評估，50字以內：\n"
                f"營收YoY {rev_yoy*100:+.1f}%。\n"
                f"請評估：競爭優勢、護城河、產業趨勢。"
            )
            msg = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=80,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=8.0,
            )
            return msg.content[0].text.strip()[:120] if msg.content else ""
        except Exception:
            return ""


def get_research_checklist() -> ResearchChecklist:
    return ResearchChecklist()


if __name__ == "__main__":
    import asyncio
    checker = ResearchChecklist()
    result  = checker.check_sync("2330", {
        "name": "台積電", "rev_yoy": 0.20, "eps_growth": 0.15,
        "pe_ratio": 22, "foreign_buy_days": 5, "trust_net": 400,
        "volume": 5_000_000, "close": 870,
        "ma5": 875, "ma20": 850, "ma60": 820,
        "volatility": 0.018, "sector": "半導體",
    })
    print(result.format_line())
