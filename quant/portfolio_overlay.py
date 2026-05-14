"""
portfolio_overlay.py — Layer 5: 持倉每日健康檢查

每日 19:00 掃描持倉，三燈訊號：
  🟢 Green（趨勢持續）：5D動能>0 + 法人持續流入 + Relative Strength 維持
  🟡 Yellow（動能轉弱）：RS下滑>10% OR 成交量連續3日萎縮 OR 模型分數下滑>20%
  🔴 Red（籌碼轉弱）：外資翻空(連續2日賣超) AND 投信撤退 AND 5D動能轉負

輸出：每檔 HoldingSignal，推送持倉健康報告
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── 燈號門檻 ──────────────────────────────────────────────────────────────────
GREEN_5D_MIN        = 0.0     # 5D動能 > 0
GREEN_INST_MIN      = 1       # 外資連買天數 ≥ 1
GREEN_RS_MIN        = -0.05   # RS ≥ -5%（維持強勢）

YELLOW_RS_DROP      = 0.10    # RS 下滑 > 10%
YELLOW_VOL_SHRINK   = 3       # 成交量連縮天數 ≥ 3（以 vol_ratio < 0.8 代理）
YELLOW_SCORE_DROP   = 0.20    # 模型分數下滑 > 20%

RED_FOREIGN_SELL    = -2      # 外資連賣 ≥ 2 日（翻空）
RED_TRUST_WITHDRAW  = True    # 投信撤退（trust_net < 0）
RED_5D_NEGATIVE     = 0.0     # 5D動能 < 0


@dataclass
class HoldingSignal:
    stock_code:    str
    stock_name:    str
    cost_price:    float
    shares:        int
    current_price: float
    pnl_pct:       float

    status:        str           # "green" / "yellow" / "red"
    reasons:       list[str] = field(default_factory=list)
    action:        str = "hold"  # hold / add / reduce / sell
    action_note:   str = ""

    # 技術數據（供 decision_engine 使用）
    ret_5d:        float = 0.0
    foreign_days:  int   = 0
    trust_net:     float = 0.0
    rs_score:      float = 0.0
    model_score:   float = 50.0

    @property
    def status_icon(self) -> str:
        return {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(self.status, "📊")

    def format_line(self) -> str:
        pnl_sign = "+" if self.pnl_pct >= 0 else ""
        first = self.reasons[0] if self.reasons else "持續觀察"
        return (
            f"{self.status_icon} {self.stock_name}：{first}\n"
            f"   {self.current_price:.1f}（{pnl_sign}{self.pnl_pct:.1f}%）  {self.action_note}"
        )

    def to_dict(self) -> dict:
        return {
            "code":          self.stock_code,
            "name":          self.stock_name,
            "status":        self.status,
            "reasons":       self.reasons,
            "action":        self.action,
            "action_note":   self.action_note,
            "pnl_pct":       round(self.pnl_pct, 2),
            "current_price": round(self.current_price, 2),
            "ret_5d":        round(self.ret_5d, 4),
            "foreign_days":  self.foreign_days,
            "model_score":   round(self.model_score, 1),
        }


class PortfolioOverlay:
    """
    Layer 5：持倉每日健康掃描器。

    使用方式：
        overlay = PortfolioOverlay()
        signals = await overlay.scan(uid)
        report  = overlay.format_report(signals)
        await overlay.push(signals, uid, token)
    """

    async def scan(self, uid: str) -> list[HoldingSignal]:
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.services import portfolio_service
            from backend.services.twse_service import fetch_realtime_quote

            async with AsyncSessionLocal() as db:
                holdings = await portfolio_service.get_portfolio(db, uid)

            if not holdings:
                return []

            signals: list[HoldingSignal] = []
            for h in holdings:
                code   = h.get("stock_code", "")
                name   = h.get("stock_name", code)
                cost   = float(h.get("cost_price", 0))
                shares = int(h.get("shares", 0))

                current = cost
                try:
                    # 優先使用 report_screener 的全市場快取（已批次從 TWSE 取得今日收盤）
                    from backend.services.report_screener import _rt_cache, _fetch_rt_cache
                    cached_prices = _rt_cache.get("prices", {})
                    if not cached_prices:
                        await _fetch_rt_cache()
                        cached_prices = _rt_cache.get("prices", {})
                    p = cached_prices.get(code, {})
                    if p and p.get("close", 0) > 0:
                        current = p["close"]
                    else:
                        # 快取無此代碼（可能上櫃），改用即時查詢
                        q = await fetch_realtime_quote(code)
                        current = float(q.get("price", cost)) if q else cost
                except Exception:
                    pass

                pnl_pct = (current - cost) / cost * 100 if cost > 0 else 0.0
                market_data = self._get_market_data(code)
                sig = self._evaluate(code, name, cost, current, shares, pnl_pct, market_data)
                signals.append(sig)

            signals.sort(key=lambda s: {"red": 0, "yellow": 1, "green": 2}[s.status])
            return signals

        except Exception as e:
            logger.error("[PortfolioOverlay] scan failed: %s", e)
            return []

    def _get_market_data(self, code: str) -> dict:
        """從 report_screener 取最新市場數據"""
        try:
            from backend.services.report_screener import all_screener
            for row in all_screener(limit=300):
                rid = getattr(row, "stock_id", "") or (row.get("stock_id", "") if isinstance(row, dict) else "")
                if rid == code:
                    vol_r = float(getattr(row, "volume_ratio", 1.0) or 1.0)
                    return {
                        # ret_5d_approx 是 safe_build_row 計算的5日估算報酬（小數形式）
                        "ret_5d":       float(getattr(row, "ret_5d_approx",    0) or 0),
                        "foreign_days": int(getattr(row,   "foreign_buy_days", 0) or 0),
                        "trust_net":    float(getattr(row, "chip_5d",           0) or 0),
                        "model_score":  float(getattr(row, "model_score",      50) or 50),
                        "vol_ratio":    vol_r,
                        "ret_1m":       float(getattr(row, "momentum_score",   50) or 50) / 50 - 1,
                    }
        except Exception:
            pass
        return {}

    def _evaluate(
        self,
        code:        str,
        name:        str,
        cost:        float,
        current:     float,
        shares:      int,
        pnl_pct:     float,
        md:          dict,
    ) -> HoldingSignal:
        ret_5d      = float(md.get("ret_5d",      pnl_pct / 100 * 0.3))
        f_days      = int(md.get("foreign_days",  0))
        trust_net   = float(md.get("trust_net",   0))
        model_sc    = float(md.get("model_score", 50))
        vol_ratio   = float(md.get("vol_ratio",   1.0))
        ret_1m      = float(md.get("ret_1m",      pnl_pct / 100))

        market_ret_1m = 0.03
        rs = ret_1m - market_ret_1m    # Relative Strength vs market

        # ── Red 條件：外資翻空(連賣≥2日) AND 投信撤退 AND 5D動能轉負 ─────────────
        red_foreign  = f_days <= RED_FOREIGN_SELL
        red_trust    = trust_net < 0
        red_momentum = ret_5d < RED_5D_NEGATIVE

        # Red = 主要條件（外資翻空）+ 至少一個輔助條件
        is_red = red_foreign and (red_trust or red_momentum)

        # ── Yellow 條件：RS下滑>10% OR 成交量連縮 OR 模型分數下滑>20% ──────────
        yellow_rs    = rs < -YELLOW_RS_DROP
        yellow_vol   = vol_ratio < 0.80    # 量比 < 0.8 代理「成交量萎縮」
        yellow_score = model_sc < 40       # 低於 40 代理「分數下滑 > 20%」

        is_yellow = not is_red and (yellow_rs or yellow_vol or yellow_score)

        # ── Green 條件：5D>0 AND 法人流入 AND RS維持 ────────────────────────────
        green_mom  = ret_5d > GREEN_5D_MIN
        green_inst = f_days >= GREEN_INST_MIN or trust_net > 0
        green_rs   = rs >= GREEN_RS_MIN

        is_green = not is_red and not is_yellow and green_mom and green_inst and green_rs

        # ── 組裝 reasons ─────────────────────────────────────────────────────────
        reasons: list[str] = []
        if is_red:
            if red_foreign:   reasons.append(f"外資連賣 {abs(f_days)} 日（翻空）")
            if red_trust:     reasons.append("投信撤退")
            if red_momentum:  reasons.append(f"5D動能轉負（{ret_5d*100:+.1f}%）")
        elif is_yellow:
            if yellow_rs:     reasons.append(f"RS下滑 {abs(rs)*100:.1f}% 跑輸大盤")
            if yellow_vol:    reasons.append(f"量比 {vol_ratio:.2f}x 成交萎縮")
            if yellow_score:  reasons.append(f"模型分數偏低（{model_sc:.0f}）")
        else:
            if green_mom:     reasons.append(f"5D動能持續（{ret_5d*100:+.1f}%）")
            if green_inst:
                if f_days > 0:  reasons.append(f"外資連買 {f_days} 日")
                if trust_net > 0: reasons.append(f"投信淨買 {trust_net:.0f} 張")
            if green_rs:      reasons.append(f"RS跑贏大盤 {rs*100:+.1f}%")

        if not reasons:
            reasons = ["無明顯訊號，持續觀察"]

        # ── 決定狀態與建議動作 ────────────────────────────────────────────────────
        if is_red:
            status      = "red"
            action      = "sell" if (pnl_pct < -10 or red_momentum) else "reduce"
            action_note = "建議停損" if action == "sell" else "考慮減碼"
        elif is_yellow:
            status      = "yellow"
            action      = "reduce" if pnl_pct < -5 else "watch"
            action_note = "注意觀察，可減倉" if action == "reduce" else "注意觀察"
        elif is_green:
            status      = "green"
            action      = "add" if pnl_pct > 3 and f_days >= 3 else "hold"
            action_note = "可考慮加碼" if action == "add" else "繼續持有"
        else:
            status      = "yellow"
            action      = "hold"
            action_note = "持續觀察"

        return HoldingSignal(
            stock_code=code, stock_name=name,
            cost_price=cost, shares=shares,
            current_price=current, pnl_pct=round(pnl_pct, 2),
            status=status, reasons=reasons,
            action=action, action_note=action_note,
            ret_5d=round(ret_5d, 4), foreign_days=f_days,
            trust_net=trust_net, rs_score=round(rs, 4),
            model_score=round(model_sc, 1),
        )

    def evaluate_mock(self, code: str, name: str) -> HoldingSignal:
        """Mock 評估，用於測試"""
        import random
        rng = random.Random(hash(code) % 999)
        ret_5d   = rng.uniform(-0.05, 0.10)
        f_days   = rng.randint(-3, 6)
        trust_n  = rng.uniform(-200, 800)
        model_sc = rng.uniform(30, 90)
        vol_r    = rng.uniform(0.6, 2.0)
        return self._evaluate(
            code, name, cost=100.0, current=100.0 * (1 + ret_5d),
            shares=1000, pnl_pct=ret_5d * 100,
            md={
                "ret_5d":      ret_5d,
                "foreign_days": f_days,
                "trust_net":   trust_n,
                "model_score": model_sc,
                "vol_ratio":   vol_r,
                "ret_1m":      ret_5d * 4,
            },
        )

    def format_report(self, signals: list[HoldingSignal]) -> str:
        if not signals:
            return "🛡️ 持倉健康報告\n\n庫存為空"

        now    = datetime.now().strftime("%m/%d %H:%M")
        green  = [s for s in signals if s.status == "green"]
        yellow = [s for s in signals if s.status == "yellow"]
        red    = [s for s in signals if s.status == "red"]

        lines = [
            f"🛡️ 持倉健康報告  {now}",
            f"🟢{len(green)} 正常  🟡{len(yellow)} 注意  🔴{len(red)} 警示",
            "─" * 22,
        ]
        for s in signals:
            lines.append(s.format_line())

        if red:
            lines += ["", "⚠️ 請優先處理紅燈持股"]

        return "\n".join(lines)

    async def push(self, signals: list[HoldingSignal], uid: str, token: str) -> None:
        if not signals or not token:
            return
        report  = self.format_report(signals)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": uid, "messages": [{"type": "text", "text": report[:4800]}]},
                    headers=headers,
                )
        except Exception as e:
            logger.error("[PortfolioOverlay] push failed: %s", e)

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
                    signals = await self.scan(uid)
                    await self.push(signals, uid, token)
                    count += 1
            return count
        except Exception as e:
            logger.error("[PortfolioOverlay] push_all failed: %s", e)
            return 0


_global_overlay: Optional[PortfolioOverlay] = None

def get_portfolio_overlay() -> PortfolioOverlay:
    global _global_overlay
    if _global_overlay is None:
        _global_overlay = PortfolioOverlay()
    return _global_overlay


if __name__ == "__main__":
    overlay = PortfolioOverlay()
    for code, name in [("2330", "台積電"), ("6669", "緯穎"), ("2603", "長榮")]:
        sig = overlay.evaluate_mock(code, name)
        print(sig.format_line())
        print(f"   ret_5d={sig.ret_5d*100:+.1f}%  f_days={sig.foreign_days}  rs={sig.rs_score*100:+.1f}%\n")
