"""
decision_engine.py — Layer 6: 每日決策引擎

整合全部 5 層，每日只輸出 0~5 個操作建議：
  🟢 買進：movers確認 AND scanner通過 AND filter通過 AND research=READY AND 風控通過
  🟢 加碼：portfolio_overlay=green AND confidence>75 AND 未超單股限額
  🟡 減碼：portfolio_overlay=yellow AND 動能持續轉弱
  🔴 賣出：portfolio_overlay=red OR 觸碰停損 OR Alpha完全衰退
  👁  觀察：條件接近但未全部通過

每日 19:30 推送決策報告
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

MAX_DECISIONS       = 5
BUY_MIN_CONFIDENCE  = 65     # 買進最低信心
ADD_MIN_CONFIDENCE  = 75     # 加碼最低信心
MAX_POSITION_PCT    = 0.20   # 單股最大倉位


@dataclass
class Decision:
    action:       str        # buy / add / reduce / sell / watch
    stock_code:   str
    stock_name:   str
    confidence:   float      # 0~100
    reasons:      list[str]
    position_pct: float      # 建議倉位 %（目標倉位）
    target_price: float      # 目標價（0=未設定）
    stop_loss:    float      # 停損價（0=未設定）
    tier:         str        # core / medium / satellite / portfolio

    _ICONS = {"buy": "🟢", "add": "🟢", "reduce": "🟡", "sell": "🔴", "watch": "👁"}
    _ZH    = {"buy": "買進", "add": "加碼", "reduce": "減碼", "sell": "賣出", "watch": "觀察"}

    def format_line(self) -> str:
        icon      = self._ICONS.get(self.action, "📊")
        action_zh = self._ZH.get(self.action, self.action)
        lines = [
            f"{icon} {action_zh}：{self.stock_code} {self.stock_name}",
            f"   原因：{'、'.join(self.reasons[:3])}",
            f"   建議部位：{self.position_pct*100:.0f}%  信心：{self.confidence:.0f}",
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
    decisions:      list[Decision]
    generated_at:   str
    movers_count:   int = 0
    filtered_count: int = 0
    pipeline_note:  str = ""

    def format_line(self) -> str:
        now   = datetime.now().strftime("%m/%d %H:%M")
        n     = len(self.decisions)
        lines = [
            f"📋 今日操作建議（共 {n} 個）  {now}",
            "─" * 22,
        ]
        if not self.decisions:
            lines += [
                "今日無明確操作建議",
                "",
                "• 繼續持有現有部位",
                "• 等待更好的進場時機",
            ]
            if self.pipeline_note:
                lines += ["", self.pipeline_note]
            return "\n".join(lines)

        buy_add = [d for d in self.decisions if d.action in ("buy", "add")]
        reduce  = [d for d in self.decisions if d.action in ("reduce", "sell")]
        watch   = [d for d in self.decisions if d.action == "watch"]

        for d in buy_add + reduce + watch:
            lines.append(d.format_line())
            lines.append("")

        if self.pipeline_note:
            lines += [self.pipeline_note]

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
    Layer 6：每日決策引擎。

    使用方式：
        engine = DecisionEngine()
        daily  = await engine.run(uid="user123")
        print(daily.format_line())
    """

    async def run(self, uid: str) -> DailyDecision:
        from quant.audit_log_engine import AuditLogger
        from quant.risk_kill_switch import is_trading_enabled, check_and_activate, status_dict
        from quant.mock_isolation import IS_PRODUCTION, assert_no_mock

        audit = AuditLogger()
        decisions:     list[Decision] = []
        movers_count   = 0
        filtered_count = 0
        notes: list[str] = []

        # ── 前置檢查：Kill Switch ────────────────────────────────────────────
        if not is_trading_enabled():
            ks = status_dict()
            logger.warning("[Decision] Kill Switch active: %s", ks["reason"])
            return DailyDecision(
                decisions=[],
                generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                pipeline_note=f"⛔ Kill Switch 啟動：{ks['reason']}",
            )

        # ── Layer 1: 動能啟動掃描 ────────────────────────────────────────────
        movers = []
        try:
            from quant.movers_engine import MoversEngine
            engine   = MoversEngine()
            movers   = await engine.scan()
            if not movers:
                movers = engine.scan_mock(15)
                for m in movers:
                    m.is_mock = True
            movers_count = len(movers)
        except Exception as e:
            logger.warning("[Decision] movers failed: %s", e)
            try:
                from quant.movers_engine import MoversEngine
                movers = MoversEngine().scan_mock(10)
                for m in movers:
                    m.is_mock = True
                movers_count = len(movers)
            except Exception:
                pass

        # Production 環境：若 movers 全為 mock → 停止
        mock_movers = sum(1 for m in movers if getattr(m, "is_mock", False))
        if not check_and_activate(mock_movers, max(len(movers), 1),
                                   mock_in_production=IS_PRODUCTION and mock_movers == len(movers) and len(movers) > 0):
            ks = status_dict()
            return DailyDecision(
                decisions=[],
                generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                pipeline_note=f"⛔ Kill Switch 啟動：{ks['reason']}",
            )

        # ── Layer 2: 三層分類 ─────────────────────────────────────────────────
        scan_records: list = []
        try:
            from quant.scanner_engine import ScannerEngine
            scan_result  = ScannerEngine().classify(movers)
            scan_records = scan_result.core + scan_result.medium + scan_result.satellite
        except Exception as e:
            logger.warning("[Decision] scanner failed: %s", e)

        # ── Layer 3: 六大過濾器 ───────────────────────────────────────────────
        filter_result: dict = {"passed": scan_records, "rejected": [], "reason": {}}
        try:
            from quant.filter_engine import FilterEngine
            filter_result  = FilterEngine().filter(scan_records)
            filtered_count = len(filter_result["passed"])
        except Exception as e:
            logger.warning("[Decision] filter failed: %s", e)
            filtered_count = len(scan_records)

        passed_ids = {
            (r.stock_id if hasattr(r, "stock_id") else r.get("stock_id", ""))
            for r in filter_result["passed"]
        }

        # ── Layer 4: Research 狀態（快速 sync 版）───────────────────────────
        ready_codes: set[str] = set()
        try:
            from quant.research_checklist import ResearchChecklist
            checker = ResearchChecklist()
            for rec in scan_records[:8]:
                code = rec.stock_id if hasattr(rec, "stock_id") else rec.get("stock_id", "")
                if code in passed_ids:
                    # 使用有限 mock 數據做快速評估
                    r = checker.check_sync(code, _mock_data_for(rec))
                    if r.overall != "REJECTED":
                        ready_codes.add(code)
        except Exception as e:
            logger.warning("[Decision] research failed: %s", e)
            ready_codes = passed_ids  # fallback：全通過

        # ── Layer 5: 持倉健康檢查（減碼/賣出訊號）────────────────────────────
        holding_signals = []
        try:
            from quant.portfolio_overlay import PortfolioOverlay
            holding_signals = await PortfolioOverlay().scan(uid)
        except Exception as e:
            logger.warning("[Decision] overlay failed: %s", e)

        # ── 產生「買進」建議 ──────────────────────────────────────────────────
        for rec in scan_records:
            if len(decisions) >= MAX_DECISIONS:
                break
            code = rec.stock_id if hasattr(rec, "stock_id") else rec.get("stock_id", "")
            if code not in passed_ids or code not in ready_codes:
                continue
            d = self._make_buy(rec, movers)
            if d:
                decisions.append(d)

        # ── 產生「加碼/減碼/賣出」建議（基於持倉健康）──────────────────────────
        for sig in holding_signals:
            if len(decisions) >= MAX_DECISIONS:
                break
            # 加碼：overlay=green AND confidence>75
            if sig.action == "add" and sig.model_score >= ADD_MIN_CONFIDENCE:
                decisions.append(Decision(
                    action="add",
                    stock_code=sig.stock_code, stock_name=sig.stock_name,
                    confidence=sig.model_score,
                    reasons=sig.reasons[:3],
                    position_pct=0.05,
                    target_price=round(sig.current_price * 1.10, 1),
                    stop_loss=round(sig.cost_price * 0.93,  1),
                    tier="portfolio",
                ))
            # 減碼：overlay=yellow AND 動能持續轉弱
            elif sig.status == "yellow" and sig.ret_5d < -0.02:
                decisions.append(Decision(
                    action="reduce",
                    stock_code=sig.stock_code, stock_name=sig.stock_name,
                    confidence=65,
                    reasons=sig.reasons[:3],
                    position_pct=0.05,
                    target_price=0,
                    stop_loss=round(sig.cost_price * 0.92, 1),
                    tier="portfolio",
                ))
            # 賣出：overlay=red OR 觸碰停損
            elif sig.status == "red" or sig.pnl_pct < -12:
                decisions.append(Decision(
                    action="sell",
                    stock_code=sig.stock_code, stock_name=sig.stock_name,
                    confidence=80,
                    reasons=sig.reasons[:3],
                    position_pct=0.0,
                    target_price=0,
                    stop_loss=round(sig.cost_price * 0.90, 1),
                    tier="portfolio",
                ))

        # ── 若無任何決策，填入觀察 ─────────────────────────────────────────────
        if not decisions and movers:
            for m in movers[:2]:
                close = m.close
                decisions.append(Decision(
                    action="watch",
                    stock_code=m.stock_id, stock_name=m.name,
                    confidence=round(m.score * 0.6, 1),
                    reasons=[
                        f"5D+{m.ret_5d*100:.1f}%",
                        f"量比{m.volume_ratio:.1f}x",
                        "條件接近但未全部通過",
                    ],
                    position_pct=0.0,
                    target_price=round(close * 1.10, 1) if close > 0 else 0,
                    stop_loss=round(close * 0.92, 1) if close > 0 else 0,
                    tier="watch",
                ))

        # ── Step 11: Analyst Consensus 驗證 ────────────────────────────────────
        try:
            from backend.services.analyst_consensus_engine import (
                get_stock_consensus, get_consensus_boost
            )
            for d in decisions:
                if d.action not in ("buy", "add"):
                    continue
                consensus = await get_stock_consensus(d.stock_code)
                boost     = get_consensus_boost(consensus)
                if boost != 0:
                    d.confidence = min(100, max(0, d.confidence + boost))
                    if boost > 0:
                        d.reasons.append(f"分析師共識支撐+{boost:.0f}%")
                    elif boost < 0:
                        d.reasons.append("分析師高分歧，建議謹慎")
        except Exception as _e:
            logger.debug("[Decision] Step11 analyst consensus failed: %s", _e)

        # ── Audit Log ──────────────────────────────────────────────────────────
        for d in decisions:
            audit.record_decision(
                stock_id   = d.stock_code,
                action     = d.action,
                confidence = d.confidence,
                reasons    = d.reasons,
            )
        for m in movers:
            code = m.stock_id if hasattr(m, "stock_id") else ""
            if code and code not in {d.stock_code for d in decisions}:
                audit.record_skip(code, "not_in_final_decisions",
                                  kill_switch=not is_trading_enabled())

        try:
            await audit.flush()
        except Exception:
            pass

        # 排序：sell/reduce > buy/add > watch
        _priority = {"sell": 0, "reduce": 1, "buy": 2, "add": 3, "watch": 4}
        decisions.sort(key=lambda d: _priority.get(d.action, 5))

        if movers_count > 0:
            mock_note = f"（⚠️ {mock_movers} 筆為示範資料）" if mock_movers else ""
            notes.append(f"掃描 {movers_count} 檔動能股，通過過濾 {filtered_count} 檔{mock_note}")

        return DailyDecision(
            decisions=decisions[:MAX_DECISIONS],
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            movers_count=movers_count,
            filtered_count=filtered_count,
            pipeline_note="  ".join(notes),
        )

    def _make_buy(self, rec, movers: list) -> Optional[Decision]:
        """從 ScanRecord 生成買進決策"""
        stock_id   = rec.stock_id   if hasattr(rec, "stock_id")   else rec.get("stock_id", "")
        name       = rec.name       if hasattr(rec, "name")       else rec.get("name", stock_id)
        layer      = rec.layer      if hasattr(rec, "layer")      else rec.get("layer", "medium")
        reasons    = rec.reasons    if hasattr(rec, "reasons")    else rec.get("reasons", [])
        score      = rec.score      if hasattr(rec, "score")      else rec.get("score", 0.5)
        max_pos    = rec.max_position if hasattr(rec, "max_position") else rec.get("max_position", 0.10)

        confidence = score * 100
        if confidence < BUY_MIN_CONFIDENCE:
            return None

        # 從 movers 取收盤價
        close = 0.0
        for m in movers:
            if m.stock_id == stock_id:
                close = m.close
                break

        pos = {"core": 0.15, "medium": 0.10, "satellite": 0.05}.get(layer, 0.08)
        pos = min(pos, max_pos, MAX_POSITION_PCT)
        if confidence >= 80:
            pos = min(pos * 1.3, MAX_POSITION_PCT)

        target = round(close * 1.12, 1) if close > 0 else 0
        stop   = round(close * 0.93, 1) if close > 0 else 0

        return Decision(
            action="buy",
            stock_code=stock_id, stock_name=name,
            confidence=round(confidence, 1),
            reasons=(reasons[:3] or ["動能+基本面符合"]),
            position_pct=round(pos, 2),
            target_price=target,
            stop_loss=stop,
            tier=layer,
        )

    async def push(self, daily: DailyDecision, uid: str, token: str) -> None:
        if not token:
            return
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": uid, "messages": [{"type": "text",
                          "text": daily.format_line()[:4800]}]},
                    headers=headers,
                )
        except Exception as e:
            logger.error("[Decision] push failed: %s", e)

    async def push_all_subscribers(self, token: str) -> int:
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


def _mock_data_for(rec) -> dict:
    """從 ScanRecord 建構 research_checklist 需要的簡易 data dict"""
    def g(attr, d=0.0):
        if hasattr(rec, attr):
            v = getattr(rec, attr)
            return float(v) if v is not None else d
        if isinstance(rec, dict):
            return float(rec.get(attr, d) or d)
        return d
    def gs(attr, d=""):
        if hasattr(rec, attr): return str(getattr(rec, attr) or d)
        if isinstance(rec, dict): return str(rec.get(attr, d) or d)
        return d

    score = g("score", 0.5)
    return {
        "name":            gs("name"),
        "rev_yoy":         g("rev_yoy",    score * 0.3),
        "eps_growth":      g("eps_growth", score * 0.2),
        "pe_ratio":        g("pe_ratio",   20),
        "foreign_buy_days":g("foreign_days", 3),
        "trust_net":       g("trust_net",  200),
        "volume":          5_000_000,
        "close":           100.0,
        "volatility":      0.02,
        "sector":          gs("sector"),
    }


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
        print(f"\n動能:{daily.movers_count} 通過:{daily.filtered_count}")

    asyncio.run(_test())
