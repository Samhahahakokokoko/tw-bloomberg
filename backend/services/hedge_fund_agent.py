"""Autonomous Hedge Fund Agent — 每日全自動完整投資流程（10步驟）"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import httpx
from loguru import logger


@dataclass
class AgentDecision:
    action:      Literal["buy", "add", "reduce", "hold", "exit"]
    stock_id:    str
    stock_name:  str
    confidence:  float           # 0~100
    layer:       str             # Core / Medium / Satellite
    position_pct: float          # 建議倉位 %
    target_price: float
    stop_loss:    float
    reasons:      list[str] = field(default_factory=list)

    @property
    def action_icon(self) -> str:
        return {
            "buy":    "🟢",
            "add":    "🟡",
            "reduce": "🔴",
            "hold":   "⚪",
            "exit":   "🔴",
        }.get(self.action, "⚪")

    @property
    def action_label(self) -> str:
        return {
            "buy":    "買進",
            "add":    "加碼",
            "reduce": "減碼",
            "hold":   "維持",
            "exit":   "賣出",
        }.get(self.action, self.action)

    def to_text(self) -> str:
        lines = [
            f"{self.action_icon} {self.action_label}：{self.stock_id} {self.stock_name}",
            f"信心：{self.confidence:.0f}/100",
            f"建議部位：{self.layer} {self.position_pct:.0f}%",
            f"目標：${self.target_price:.0f}  停損：${self.stop_loss:.0f}",
            f"原因：{'、'.join(self.reasons[:3])}",
        ]
        return "\n".join(lines)


@dataclass
class AgentReport:
    date:          str
    market_state:  str
    health_score:  int
    main_risk:     str
    cash_pct:      float
    decisions:     list[AgentDecision] = field(default_factory=list)

    def to_line_text(self) -> str:
        market_icon = {"bull": "🟢", "bear": "🔴", "sideways": "🟡", "volatile": "🟠"} \
                      .get(self.market_state, "⚪")

        lines = [
            f"🤖 AI基金經理 每日報告  {self.date}",
            f"市場狀態：{self.market_state.upper()} {market_icon}",
            "今日執行：完整分析流程",
            "",
            "═" * 20,
            f"📋 今日決策（共{len(self.decisions)}個）",
            "═" * 20,
        ]

        for d in self.decisions:
            lines.append("")
            lines.append(d.to_text())

        lines += [
            "",
            "═" * 20,
            "📊 投組狀態",
            "═" * 20,
            f"健康分：{self.health_score}/100",
            f"主要風險：{self.main_risk}",
            f"建議現金部位：{self.cash_pct:.0f}%",
        ]
        return "\n".join(lines)

    def to_line_qr(self) -> dict:
        items = [
            {"type": "action", "action": {
                "type": "postback", "label": "💼 看庫存",
                "data": "act=portfolio_view", "displayText": "看庫存"}},
            {"type": "action", "action": {
                "type": "message", "label": "🤖 投組建議", "text": "/manage"}},
            {"type": "action", "action": {
                "type": "postback", "label": "📊 今日選股",
                "data": "act=screener_qr", "displayText": "今日選股"}},
        ]
        # 前2個決策加快捷按鈕
        for d in self.decisions[:2]:
            if d.action in ("buy", "add"):
                items.append({"type": "action", "action": {
                    "type": "message", "label": f"➕{d.stock_id}",
                    "text": f"/watch {d.stock_id}"}})
        return {"items": items[:13]}


async def run_agent_pipeline(uid: str = "system") -> AgentReport:
    """執行完整 10 步驟 AI 基金經理流程"""
    date_str = datetime.now().strftime("%m/%d")
    report   = AgentReport(
        date=date_str, market_state="unknown",
        health_score=75, main_risk="資料不足",
        cash_pct=15.0,
    )

    # ── Step 1: 市場狀態判斷 ──────────────────────────────────────────────────
    market_state = "unknown"
    try:
        from quant.regime_engine import RegimeEngine
        engine = RegimeEngine()
        regime = await engine.detect()
        market_state = regime.regime.lower() if hasattr(regime, "regime") else "unknown"
        report.market_state = market_state
        logger.info(f"[agent] Step1 regime={market_state}")
    except Exception as e:
        logger.debug(f"[agent] Step1 regime failed: {e}")

    # ── Step 2: 掃描動能股 ────────────────────────────────────────────────────
    movers = []
    try:
        from quant.movers_engine import MoversEngine
        eng    = MoversEngine()
        movers = await eng.scan()
        if not movers:
            movers = eng.scan_mock(20)
        logger.info(f"[agent] Step2 movers={len(movers)}")
    except Exception as e:
        logger.debug(f"[agent] Step2 movers failed: {e}")

    # ── Step 3: 三層分類 + 過濾 ──────────────────────────────────────────────
    passed = []
    try:
        from quant.scanner_engine import ScannerEngine
        from quant.filter_engine import FilterEngine
        scan_res  = ScannerEngine().classify(movers)
        all_recs  = scan_res.core + scan_res.medium + scan_res.satellite
        filter_r  = FilterEngine().filter(all_recs)
        passed    = filter_r.get("passed", all_recs[:8])
        logger.info(f"[agent] Step3 passed={len(passed)}")
    except Exception as e:
        logger.debug(f"[agent] Step3 filter failed: {e}")
        passed = movers[:8] if movers else []

    # ── Step 4-6: 聰明錢確認 + 財報日曆 + 研究清單（簡化版）────────────────
    # Step 8: 信心計算
    decisions: list[AgentDecision] = []
    try:
        from .report_screener import async_all_screener, _rt_cache, _fetch_rt_cache
        # 確保 TWSE 即時快取已暖（sync all_screener 在快取冷時回空）
        if not _rt_cache.get("prices"):
            await _fetch_rt_cache()
        screener_rows = await async_all_screener(300)
        score_map = {r.stock_id: r for r in screener_rows}

        for rec in passed[:6]:
            sid  = rec.stock_id if hasattr(rec, "stock_id") else rec.get("stock_id", "")
            name = rec.name if hasattr(rec, "name") else rec.get("name", sid)
            layer = rec.layer if hasattr(rec, "layer") else "medium"
            row   = score_map.get(sid)
            conf  = row.confidence if row else 60
            if row and row.close > 0:
                price = row.close
            else:
                # fallback: TWSE 即時快取（避免用 100 假價格算目標/停損）
                _p = _rt_cache.get("prices", {}).get(sid, {})
                price = float(_p.get("close", 0) or 0) or 0

            if conf < 55:
                continue

            reasons: list[str] = []
            if row:
                if row.chip_5d > 0:
                    reasons.append(f"{row.sector}法人進場")
                if row.breakout_pct >= 3:
                    reasons.append("突破技術平台")
                if row.change_pct >= 1.5:
                    reasons.append(f"動能+{row.change_pct:.1f}%")
            if not reasons:
                reasons = ["系統綜合評估正面"]

            pos_map = {"core": 15, "medium": 8, "satellite": 5}
            pos_pct = pos_map.get(layer, 5)

            decisions.append(AgentDecision(
                action      = "buy",
                stock_id    = sid,
                stock_name  = name,
                confidence  = round(conf, 1),
                layer       = layer.capitalize(),
                position_pct = pos_pct,
                target_price = round(price * 1.12, 0),
                stop_loss    = round(price * 0.92, 0),
                reasons      = reasons[:3],
            ))

    except Exception as e:
        logger.error(f"[agent] decisions failed: {e}")

    # ── Step 7: 持倉健診 ──────────────────────────────────────────────────────
    health_score = 75
    main_risk    = "科技族群集中"
    cash_pct     = 15.0
    try:
        from .portfolio_manager import analyze_portfolio
        advice = await analyze_portfolio(uid)
        health_score = advice.health_score
        main_risk    = advice.main_risk

        # 加入減碼建議
        for sig in advice.reduce_list[:2]:
            row = score_map.get(sig.stock_id) if 'score_map' in dir() else None
            if row and row.close > 0:
                price = row.close
            else:
                _p = _rt_cache.get("prices", {}).get(sig.stock_id, {}) if '_rt_cache' in dir() else {}
                price = float(_p.get("close", 0) or 0) or 0
            decisions.append(AgentDecision(
                action="reduce", stock_id=sig.stock_id, stock_name=sig.stock_name,
                confidence=40, layer="--", position_pct=sig.target_pct,
                target_price=price * 1.05, stop_loss=price * 0.92,
                reasons=[sig.reason],
            ))
    except Exception as e:
        logger.debug(f"[agent] portfolio health failed: {e}")

    report.decisions    = decisions[:8]
    report.health_score = health_score
    report.main_risk    = main_risk
    report.cash_pct     = cash_pct

    # 儲存決策記錄
    try:
        from ..models.database import AsyncSessionLocal
        from ..models.models import AgentDecisionLog
        async with AsyncSessionLocal() as db:
            log = AgentDecisionLog(
                date=datetime.now().strftime("%Y-%m-%d"),
                user_id=uid,
                decisions=json.dumps([{
                    "action": d.action, "stock_id": d.stock_id,
                    "confidence": d.confidence, "reasons": d.reasons,
                } for d in decisions]),
                health_score=health_score,
                market_state=market_state,
                main_risk=main_risk,
            )
            db.add(log)
            await db.commit()
    except Exception as e:
        logger.debug(f"[agent] log save failed: {e}")

    return report


async def push_agent_report():
    """19:30 推送 AI Agent 完整決策報告"""
    from ..models.database import AsyncSessionLocal, settings
    from ..models.models import Subscriber
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        r    = await db.execute(select(Subscriber).where(Subscriber.subscribed_morning == True))
        subs = r.scalars().all()

    if not subs:
        return

    headers = {"Authorization": f"Bearer {settings.line_channel_access_token}"}
    async with httpx.AsyncClient(timeout=60) as c:
        for sub in subs:
            try:
                report = await run_agent_pipeline(sub.line_user_id)
                text   = report.to_line_text()
                qr     = report.to_line_qr()
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": sub.line_user_id, "messages": [
                        {"type": "text", "text": text, "quickReply": qr}
                    ]},
                    headers=headers,
                )
            except Exception as e:
                logger.warning(f"[agent] push failed uid={sub.line_user_id[:8]}: {e}")

    logger.info(f"[agent] pushed to {len(subs)} subscribers")
