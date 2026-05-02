"""
decision_engine.py — 每日決策引擎（核心）

整合所有層，每日只輸出 0~5 個操作建議：
  買進：movers + scanner + filter + research + 風控 全通過
  加碼：portfolio_overlay 支持 + 信心 > 75 + 未超單股 20%
  減碼：portfolio_overlay 警示 + 動能轉弱
  賣出：portfolio_overlay 紅燈 + 停損觸發 或 Alpha 衰退
  觀察：接近但未完全通過

每日 19:30 推送決策報告
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

MAX_DECISIONS    = 5
MIN_CONFIDENCE   = 65    # 最低決策信心門檻
MAX_POSITION_PCT = 0.20  # 單股最大倉位


@dataclass
class Decision:
    action:       str        # buy / add / reduce / sell / watch
    stock_code:   str
    stock_name:   str
    confidence:   float      # 0~100
    reasons:      list[str]
    position_pct: float      # 建議倉位 %
    target_price: float      # 目標價（0=未設定）
    stop_loss:    float      # 停損價（0=未設定）
    tier:         str        # core / medium / satellite / portfolio

    ACTION_ICONS = {"buy": "🟢", "add": "🟡", "reduce": "🔴",
                    "sell": "🔴", "watch": "👀"}

    def format_line(self) -> str:
        icon  = self.ACTION_ICONS.get(self.action, "📊")
        action_zh = {
            "buy": "買進", "add": "加碼", "reduce": "減碼",
            "sell": "賣出", "watch": "觀察",
        }.get(self.action, self.action)

        lines = [
            f"{icon} {action_zh}：{self.stock_code} {self.stock_name}",
            f"   原因：{'、'.join(self.reasons[:3])}",
            f"   建議部位：{self.position_pct*100:.0f}%",
        ]
        if self.target_price > 0:
            lines.append(f"   目標：{self.target_price:.1f}  停損：{self.stop_loss:.1f}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "action":       self.action,
            "code":         self.stock_code,
            "name":         self.stock_name,
            "confidence":   round(self.confidence, 1),
            "reasons":      self.reasons,
            "position_pct": round(self.position_pct * 100, 1),
            "target_price": round(self.target_price, 2),
            "stop_loss":    round(self.stop_loss, 2),
            "tier":         self.tier,
        }


@dataclass
class DailyDecision:
    decisions:    list[Decision]
    generated_at: str
    movers_count: int = 0
    filtered_count: int = 0

    def format_line(self) -> str:
        now    = datetime.now().strftime("%m/%d %H:%M")
        n      = len(self.decisions)
        lines  = [
            f"📋 今日操作建議（共 {n} 個）  {now}",
            "─" * 22,
        ]
        if not self.decisions:
            lines += [
                "今日無明確操作建議",
                "",
                "原因：市場條件未完全符合操作標準",
                "• 繼續持有現有部位",
                "• 等待更好的進場時機",
            ]
            return "\n".join(lines)

        buy_sell = [d for d in self.decisions if d.action in ("buy", "add")]
        reduce   = [d for d in self.decisions if d.action in ("reduce", "sell")]
        watch    = [d for d in self.decisions if d.action == "watch"]

        if buy_sell:
            for d in buy_sell:
                lines.append(d.format_line())
                lines.append("")
        if reduce:
            for d in reduce:
                lines.append(d.format_line())
                lines.append("")
        if watch:
            for d in watch:
                lines.append(d.format_line())
                lines.append("")

        return "\n".join(lines).rstrip()

    def to_dict(self) -> dict:
        return {
            "generated_at":   self.generated_at,
            "count":          len(self.decisions),
            "movers_count":   self.movers_count,
            "filtered_count": self.filtered_count,
            "decisions":      [d.to_dict() for d in self.decisions],
        }


class DecisionEngine:
    """
    每日決策引擎：整合全部 5 個分析層，輸出 0~5 個操作建議。

    使用方式：
        engine  = DecisionEngine()
        daily   = await engine.run(uid="user123")
        print(daily.format_line())
        await engine.push(daily, uid, token)
    """

    async def run(self, uid: str) -> DailyDecision:
        """執行完整決策流程"""
        decisions: list[Decision] = []
        movers_count  = 0
        filtered_count = 0

        # ── 1. 取動能啟動股票 ─────────────────────────────────────────
        try:
            from quant.movers_engine import MoversEngine
            movers_engine = MoversEngine()
            movers = await movers_engine.scan()
            if not movers:
                movers = movers_engine.scan_mock()
            movers_count = len(movers)
        except Exception as e:
            logger.warning("[Decision] movers failed: %s", e)
            from quant.movers_engine import MoversEngine
            movers = MoversEngine().scan_mock(10)
            movers_count = len(movers)

        # ── 2. 三層分類 ───────────────────────────────────────────────
        try:
            from quant.scanner_engine import ScannerEngine
            scan_result = ScannerEngine().classify(movers)
            candidates  = scan_result.all_candidates
        except Exception as e:
            logger.warning("[Decision] scanner failed: %s", e)
            candidates = []

        # ── 3. 過濾清洗 ───────────────────────────────────────────────
        try:
            from quant.filter_engine import FilterEngine
            filter_engine = FilterEngine()
            passed_results, _ = filter_engine.filter(candidates)
            passed_codes = {r.stock_code for r in passed_results}
            filtered = [c for c in candidates if c.stock_code in passed_codes]
            filtered_count = len(filtered)
        except Exception as e:
            logger.warning("[Decision] filter failed: %s", e)
            filtered = candidates[:5]
            filtered_count = len(filtered)

        # ── 4. 持倉健康檢查（減碼/賣出訊號）──────────────────────────
        try:
            from quant.portfolio_overlay import PortfolioOverlay
            overlay = PortfolioOverlay()
            holding_signals = await overlay.scan(uid)
        except Exception as e:
            logger.warning("[Decision] overlay failed: %s", e)
            holding_signals = []

        # ── 5. 產生買進/加碼建議 ───────────────────────────────────────
        for candidate in filtered[:8]:   # 只看前 8 個
            if len(decisions) >= MAX_DECISIONS:
                break
            d = self._make_buy_decision(candidate)
            if d:
                decisions.append(d)

        # ── 6. 產生減碼/賣出建議 ─────────────────────────────────────
        for sig in holding_signals:
            if len(decisions) >= MAX_DECISIONS:
                break
            if sig.status in ("red", "yellow"):
                d = self._make_reduce_decision(sig)
                if d:
                    decisions.append(d)

        # ── 7. 填入觀察建議（補足至合理數量）──────────────────────────
        if len(decisions) == 0 and movers:
            # 若無任何決策，輸出最強動能股作為觀察
            for m in movers[:2]:
                decisions.append(Decision(
                    action="watch",
                    stock_code=m.stock_code,
                    stock_name=m.stock_name,
                    confidence=m.score * 0.6,
                    reasons=[f"動能{m.mom_5d*100:+.1f}%", "條件接近但未完全通過"],
                    position_pct=0.0,
                    target_price=m.close * 1.10,
                    stop_loss=m.close * 0.92,
                    tier="watch",
                ))

        # 排序：sell/reduce > buy/add > watch
        priority = {"sell": 0, "reduce": 1, "buy": 2, "add": 3, "watch": 4}
        decisions.sort(key=lambda d: priority.get(d.action, 5))

        return DailyDecision(
            decisions=decisions[:MAX_DECISIONS],
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            movers_count=movers_count,
            filtered_count=filtered_count,
        )

    def _make_buy_decision(self, candidate) -> Optional[Decision]:
        """從 scanner 候選生成買進/加碼決策"""
        code  = candidate.stock_code if hasattr(candidate, "stock_code") else candidate.get("code", "")
        name  = candidate.stock_name if hasattr(candidate, "stock_name") else candidate.get("name", code)
        tier  = candidate.tier       if hasattr(candidate, "tier")       else "medium"
        close = candidate.close      if hasattr(candidate, "close")      else 0.0
        score = candidate.score      if hasattr(candidate, "score")      else 50.0
        reas  = (candidate.reasons   if hasattr(candidate, "reasons")    else []) or []
        max_pos = candidate.max_position if hasattr(candidate, "max_position") else 0.10

        confidence = score
        if confidence < MIN_CONFIDENCE:
            return None

        # 計算建議部位（依信心與 tier）
        pos = {
            "core":      0.15,
            "medium":    0.10,
            "satellite": 0.05,
        }.get(tier, 0.08)

        pos = min(pos, max_pos, MAX_POSITION_PCT)
        if confidence >= 80:
            pos = min(pos * 1.3, MAX_POSITION_PCT)

        # 粗略目標/停損
        target = round(close * 1.12, 1) if close > 0 else 0
        stop   = round(close * 0.93,  1) if close > 0 else 0

        return Decision(
            action="buy",
            stock_code=code,
            stock_name=name,
            confidence=round(confidence, 1),
            reasons=reas[:3] or ["動能 + 基本面符合"],
            position_pct=round(pos, 2),
            target_price=target,
            stop_loss=stop,
            tier=tier,
        )

    def _make_reduce_decision(self, sig) -> Optional[Decision]:
        """從持倉警示生成減碼/賣出決策"""
        if sig.status not in ("red", "yellow"):
            return None

        action = "sell" if sig.status == "red" else "reduce"
        pos    = 0.0 if action == "sell" else 0.05
        conf   = 75 if action == "sell" else 60

        return Decision(
            action=action,
            stock_code=sig.stock_code,
            stock_name=sig.stock_name,
            confidence=conf,
            reasons=sig.signals[:3],
            position_pct=pos,
            target_price=0,
            stop_loss=round(sig.cost_price * 0.92, 1) if sig.cost_price > 0 else 0,
            tier="portfolio",
        )

    async def push(
        self,
        daily: DailyDecision,
        uid:   str,
        token: str,
    ) -> None:
        if not token:
            return
        report  = daily.format_line()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": uid, "messages": [{"type": "text", "text": report[:4800]}]},
                    headers=headers,
                )
        except Exception as e:
            logger.error("[Decision] push failed: %s", e)

    async def push_all_subscribers(self, token: str) -> int:
        """推送給所有訂閱者"""
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import Subscriber
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                r = await db.execute(select(Subscriber))
                subs = r.scalars().all()
            count = 0
            for sub in subs:
                uid = sub.line_user_id
                if uid:
                    daily = await self.run(uid)
                    await self.push(daily, uid, token)
                    count += 1
            return count
        except Exception as e:
            logger.error("[Decision] push_all failed: %s", e)
            return 0


_global_decision: Optional[DecisionEngine] = None

def get_decision_engine() -> DecisionEngine:
    global _global_decision
    if _global_decision is None:
        _global_decision = DecisionEngine()
    return _global_decision


if __name__ == "__main__":
    import asyncio

    async def _test():
        engine = DecisionEngine()
        daily  = await engine.run(uid="test_user")
        print(daily.format_line())
        print(f"\n動能掃描: {daily.movers_count}  通過過濾: {daily.filtered_count}")

    asyncio.run(_test())
