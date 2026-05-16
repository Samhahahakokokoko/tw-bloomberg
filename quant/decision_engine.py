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

MAX_DECISIONS       = 8
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
    decisions:       list[Decision]
    generated_at:    str
    movers_count:    int = 0
    filtered_count:  int = 0
    pipeline_note:   str = ""
    market_overview: dict = field(default_factory=dict)

    def format_line(self) -> str:
        now   = datetime.now().strftime("%m/%d %H:%M")
        n     = len(self.decisions)
        lines: list[str] = []

        # ── 市場狀態區塊（最上方）──────────────────────────────────────────
        ov = self.market_overview or {}
        if ov:
            cpct  = ov.get("change_pct", 0) or 0
            chg   = ov.get("change",     0) or 0
            val   = ov.get("value",      0) or 0
            sign  = "▲" if chg >= 0 else "▼"
            regime_zh = {"bull": "多頭", "bear": "空頭", "sideways": "盤整"}.get(
                ov.get("regime", ""), "unknown"
            )
            lines += [
                f"📊 市場狀態：{regime_zh}",
                f"加權指數 {val:,.2f}  {sign}{abs(chg):.2f} ({cpct:+.2f}%)",
                "─" * 22,
            ]

        lines += [
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

        # ── 前置：無條件呼叫 _fetch_rt_cache()，TTL 由其內部處理 ─────────────────
        logger.info("[Decision] Layer 0: rt_cache 暖機開始")
        try:
            from backend.services.report_screener import _rt_cache, _fetch_rt_cache
            await _fetch_rt_cache()
            prices_ok = bool(_rt_cache.get("prices"))
            if not prices_ok:
                logger.warning("[Decision] TWSE rt_cache 仍為空，決策將使用 mock 結構（無硬編碼股價）")
            else:
                logger.info("[Decision] Layer 0: rt_cache 暖機完成，%d 檔", len(_rt_cache.get("prices", {})))
        except Exception as _cache_err:
            logger.warning("[Decision] rt_cache warm failed: %s", _cache_err)
            prices_ok = False

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
        logger.info("[Decision] Layer 1: 動能啟動掃描開始")
        movers = []
        try:
            from quant.movers_engine import MoversEngine
            engine   = MoversEngine()
            movers   = await engine.scan()
            if not movers:
                movers = engine.scan_mock(15)
                for m in movers:
                    m.is_mock = True
                _enrich_mock_close(movers)   # mock close=0 或硬編碼舊價 → TWSE 即時覆蓋
            movers_count = len(movers)
        except Exception as e:
            logger.warning("[Decision] movers failed: %s", e)
            try:
                from quant.movers_engine import MoversEngine
                movers = MoversEngine().scan_mock(10)
                for m in movers:
                    m.is_mock = True
                _enrich_mock_close(movers)
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

        logger.info("[Decision] Layer 1: 完成，movers=%d（mock=%d）", len(movers), sum(1 for m in movers if getattr(m, "is_mock", False)))
        # ── Layer 2: 三層分類 ─────────────────────────────────────────────────
        logger.info("[Decision] Layer 2: 三層分類開始")
        scan_records: list = []
        try:
            from quant.scanner_engine import ScannerEngine
            scan_result  = ScannerEngine().classify(movers)
            scan_records = scan_result.core + scan_result.medium + scan_result.satellite
        except Exception as e:
            logger.warning("[Decision] scanner failed: %s", e)

        core_n = sum(1 for r in scan_records if getattr(r,'layer','') == 'core')
        med_n  = sum(1 for r in scan_records if getattr(r,'layer','') == 'medium')
        sat_n  = sum(1 for r in scan_records if getattr(r,'layer','') == 'satellite')
        logger.info("[Decision] Layer 2: 完成，scan_records=%d (Core=%d/Med=%d/Sat=%d)",
                    len(scan_records), core_n, med_n, sat_n)
        # ── Layer 3: 六大過濾器 ───────────────────────────────────────────────
        logger.info("[Decision] Layer 3: 六大過濾器開始")
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

        logger.info("[Decision] Layer 3: 完成，passed=%d", len(filter_result.get("passed", [])))
        # ── Layer 4: Research 狀態（快速 sync 版）───────────────────────────
        logger.info("[Decision] Layer 4: Research 狀態檢查開始")
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

        logger.info("[Decision] Layer 4: 完成，ready_codes=%d", len(ready_codes))
        # ── Layer 5: 持倉健康檢查（減碼/賣出訊號）────────────────────────────
        logger.info("[Decision] Layer 5: 持倉健康檢查開始")
        holding_signals = []
        try:
            from quant.portfolio_overlay import PortfolioOverlay
            holding_signals = await PortfolioOverlay().scan(uid)
        except Exception as e:
            logger.warning("[Decision] overlay failed: %s", e)

        logger.info("[Decision] Layer 5: 完成，holding_signals=%d", len(holding_signals))
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
                # 同樣統一從 rt_cache 取今日收盤（watch 也必須用真實價格）
                close        = 0.0
                watch_source = "none"
                try:
                    from backend.services.report_screener import _rt_cache
                    p = _rt_cache.get("prices", {}).get(m.stock_id, {})
                    if p.get("close", 0) > 0:
                        close        = float(p["close"])
                        watch_source = "rt_cache(TWSE)"
                except Exception:
                    pass
                if close <= 0 and m.close > 0:
                    close        = m.close
                    watch_source = "mover"
                logger.info("[WATCH] %s 收盤價=%.1f 來源=%s", m.stock_id, close, watch_source)
                if m.stock_id == "2330":
                    print(f"2330 使用的收盤價: {close}  (來源: {watch_source})")
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

        logger.info("[Decision] Layer 5→決策: buy/add=%d, reduce/sell=%d, watch=%d",
                    sum(1 for d in decisions if d.action in ("buy","add")),
                    sum(1 for d in decisions if d.action in ("reduce","sell")),
                    sum(1 for d in decisions if d.action == "watch"))
        # ── Step 11: Analyst Consensus 驗證 ────────────────────────────────────
        logger.info("[Decision] Layer 6: Analyst Consensus 驗證開始，%d 個 buy/add", sum(1 for d in decisions if d.action in ('buy','add')))
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
                if consensus and consensus.total_analysts > 0:
                    if consensus.bullish_count > 0 and boost >= 0:
                        hi = f"（{consensus.high_cred_count}位高可信）" if consensus.high_cred_count else ""
                        d.reasons.append(f"{consensus.bullish_count}位分析師看多{hi}")
                    elif boost < 0:
                        d.reasons.append("分析師高分歧，建議謹慎")
                elif boost > 0:
                    d.reasons.append(f"分析師共識支撐")
                # 保留最多 4 個原因
                d.reasons = d.reasons[:4]
        except Exception as _e:
            logger.debug("[Decision] Step11 analyst consensus failed: %s", _e)

        logger.info("[Decision] Layer 6: 完成")
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

        # ── 抓取大盤資料供報告標題使用 ─────────────────────────────────────
        market_overview: dict = {}
        try:
            from backend.services.twse_service import fetch_market_overview
            ov = await fetch_market_overview()
            if ov:
                cpct = ov.get("change_pct", 0) or 0
                ov["regime"] = "bull" if cpct >= 0.5 else "bear" if cpct <= -0.5 else "sideways"
                market_overview = ov
        except Exception as _ov_err:
            logger.warning("[Decision] market overview fetch failed: %s", _ov_err)

        return DailyDecision(
            decisions=decisions[:MAX_DECISIONS],
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            movers_count=movers_count,
            filtered_count=filtered_count,
            pipeline_note="  ".join(notes),
            market_overview=market_overview,
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

        # ── 統一從 TWSE rt_cache 取今日收盤（主要路徑）─────────────────────────
        # rt_cache 由 _fetch_rt_cache() 每 5 分鐘自 TWSE STOCK_DAY_ALL 更新，
        # 是整個 pipeline 的唯一真實股價來源，不依賴 movers 或 mock 數據。
        close       = 0.0
        price_source = "none"
        try:
            from backend.services.report_screener import _rt_cache
            p = _rt_cache.get("prices", {}).get(stock_id, {})
            if p.get("close", 0) > 0:
                close        = float(p["close"])
                price_source = "rt_cache(TWSE)"
        except Exception:
            pass

        # rt_cache 無此股（上櫃延遲或停牌）→ fallback 到 movers
        if close <= 0:
            for m in movers:
                if m.stock_id == stock_id:
                    if m.close > 0:
                        close        = m.close
                        price_source = "mover"
                    break

        # Debug：每支進入 _make_buy 的股票都印出收盤價與原因
        logger.info("[BUY] %s 收盤價=%.1f 來源=%s", stock_id, close, price_source)
        logger.info("[BUY] %s 原因列表: %s", stock_id, reasons)
        if stock_id == "2330":
            print(f"2330 使用的收盤價: {close}  (來源: {price_source})")

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
    # close：優先讀 rec 本身，再查 TWSE 即時快取，最後用 0（不用假值 100）
    close = g("close", 0.0)
    if close <= 0:
        try:
            from backend.services.report_screener import _rt_cache
            _p = _rt_cache.get("prices", {}).get(gs("stock_id"), {})
            close = float(_p.get("close", 0) or 0)
        except Exception:
            close = 0.0
    return {
        "name":            gs("name"),
        "rev_yoy":         g("rev_yoy",    score * 0.3),
        "eps_growth":      g("eps_growth", score * 0.2),
        "pe_ratio":        g("pe_ratio",   20),
        "foreign_buy_days":g("foreign_days", 3),
        "trust_net":       g("trust_net",  200),
        "volume":          5_000_000,
        "close":           close,
        "volatility":      0.02,
        "sector":          gs("sector"),
    }


def _enrich_mock_close(movers: list) -> None:
    """
    Mock movers 可能含有硬編碼舊收盤（如 _MOCK_UNIVERSE close=870）或 close=0。
    用 TWSE 即時快取（_rt_cache）覆蓋，確保目標/停損計算使用今日真實股價。
    """
    try:
        from backend.services.report_screener import _rt_cache
        prices = _rt_cache.get("prices", {})
        if not prices:
            return
        for m in movers:
            p = prices.get(m.stock_id, {})
            if p.get("close", 0) > 0:
                m.close = p["close"]
    except Exception:
        pass


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
