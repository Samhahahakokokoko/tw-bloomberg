"""
portfolio_overlay.py — 持倉每日健康檢查

每日掃描庫存，輸出每檔的訊號狀態：
  🟢 支持（趨勢強、法人支持）→ 可加碼
  🟡 警示（動能轉弱、籌碼開始變化）→ 注意觀察
  🔴 紅燈（外資賣、Alpha 衰退 20%+）→ 考慮減碼

19:00 推送持倉健康報告
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── 訊號門檻 ──────────────────────────────────────────────────────────────────
MOM_WARN_THRESHOLD  = -0.02  # 5D 動能 < -2% → 警示
FOREIGN_SELL_WARN   = -2     # 外資連賣 2 天 → 警示
ALPHA_DECAY_WARN    = 0.20   # Alpha 分數下滑 20% → 警示
CHIP_WEAK_DAYS      = -3     # 外資連賣 3 天 → 紅燈


@dataclass
class HoldingSignal:
    stock_code:  str
    stock_name:  str
    cost_price:  float
    shares:      int
    current_price: float
    pnl_pct:     float        # 損益百分比

    # 訊號
    status:      str          # "green" / "yellow" / "red"
    signals:     list[str] = field(default_factory=list)    # 具體訊號描述
    action:      str = "hold"                               # hold/add/reduce/sell
    action_note: str = ""

    @property
    def status_icon(self) -> str:
        return {"green": "✅", "yellow": "⚠️", "red": "🔴"}.get(self.status, "📊")

    def format_line(self) -> str:
        pnl_sign = "+" if self.pnl_pct >= 0 else ""
        first_signal = self.signals[0] if self.signals else ""
        return (
            f"{self.status_icon} {self.stock_name}：{first_signal}\n"
            f"   {self.current_price:.1f}（{pnl_sign}{self.pnl_pct:.1f}%）  {self.action_note}"
        )

    def to_dict(self) -> dict:
        return {
            "code":          self.stock_code,
            "name":          self.stock_name,
            "status":        self.status,
            "signals":       self.signals,
            "action":        self.action,
            "action_note":   self.action_note,
            "pnl_pct":       round(self.pnl_pct, 2),
            "current_price": round(self.current_price, 2),
        }


class PortfolioOverlay:
    """
    每日持倉健康掃描器。

    使用方式：
        overlay = PortfolioOverlay()
        signals = await overlay.scan(uid)
        report  = overlay.format_report(signals)
        await overlay.push(signals, uid, token)
    """

    async def scan(self, uid: str) -> list[HoldingSignal]:
        """掃描用戶所有持股，回傳健康訊號"""
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
                code  = h.get("stock_code", "")
                name  = h.get("stock_name", code)
                cost  = float(h.get("cost_price", 0))
                shares = int(h.get("shares", 0))

                # 取即時報價
                current_price = cost
                try:
                    q = await fetch_realtime_quote(code)
                    current_price = float(q.get("price", cost)) if q else cost
                except Exception:
                    pass

                pnl_pct = (current_price - cost) / cost * 100 if cost > 0 else 0.0

                # 評估訊號（基於持有損益 + 假設市場數據）
                sig = self._evaluate_holding(
                    code=code, name=name, cost=cost,
                    current=current_price, shares=shares, pnl_pct=pnl_pct,
                )
                signals.append(sig)

            signals.sort(key=lambda s: {"red": 0, "yellow": 1, "green": 2}[s.status])
            return signals

        except Exception as e:
            logger.error("[PortfolioOverlay] scan failed: %s", e)
            return []

    def _evaluate_holding(
        self,
        code:      str,
        name:      str,
        cost:      float,
        current:   float,
        shares:    int,
        pnl_pct:   float,
    ) -> HoldingSignal:
        """
        基於持倉損益 + 從現有 screener 取得的技術指標評估。
        此處使用簡化邏輯，實際部署時可接入 report_screener / alpha_registry。
        """
        signals: list[str] = []
        status   = "green"
        action   = "hold"
        action_note = ""

        # 嘗試從 report_screener 取評分
        row_data = self._get_screener_data(code)
        if row_data:
            mom_5d     = float(row_data.get("mom_5d", row_data.get("ret_5d", pnl_pct / 100)))
            foreign_d  = int(row_data.get("foreign_buy_days", 0))
            model_sc   = float(row_data.get("model_score", 50))
            mom_1m     = float(row_data.get("momentum_20d", 1.0)) - 1.0
        else:
            # fallback：純用損益估算趨勢
            mom_5d    = pnl_pct / 100 * 0.3   # 粗估
            foreign_d = 0
            model_sc  = 50.0
            mom_1m    = pnl_pct / 100

        # ── 支持訊號（Green）─────────────────────────────────────────
        green_count = 0
        if mom_5d > 0.01:
            signals.append("趨勢持續走強")
            green_count += 1
        if foreign_d >= 2:
            signals.append(f"外資連買 {foreign_d} 日")
            green_count += 1
        if model_sc >= 70:
            signals.append("Alpha 分數強")
            green_count += 1
        if mom_1m > 0.05:
            signals.append("中期動能佳")
            green_count += 1

        # ── 警示訊號（Yellow）────────────────────────────────────────
        yellow_count = 0
        if mom_5d < MOM_WARN_THRESHOLD:
            signals.append(f"5日動能轉弱（{mom_5d*100:+.1f}%）")
            yellow_count += 1
        if FOREIGN_SELL_WARN <= foreign_d < 0:
            signals.append("外資開始減碼")
            yellow_count += 1
        if pnl_pct < -5 and mom_5d < 0:
            signals.append(f"已虧損 {pnl_pct:.1f}%，趨勢向下")
            yellow_count += 1

        # ── 紅燈（Red）───────────────────────────────────────────────
        red_count = 0
        if foreign_d <= CHIP_WEAK_DAYS:
            signals.append(f"外資連賣 {abs(foreign_d)} 日")
            red_count += 1
        if model_sc < 35:
            signals.append("Alpha 衰退超過 20%")
            red_count += 1
        if pnl_pct < -12:
            signals.append(f"觸碰停損警戒（-{abs(pnl_pct):.0f}%）")
            red_count += 1

        # ── 判斷最終狀態 ─────────────────────────────────────────────
        if red_count >= 1:
            status = "red"
            action = "reduce" if pnl_pct > -8 else "sell"
            action_note = "考慮減碼" if action == "reduce" else "考慮停損"
        elif yellow_count >= 2:
            status = "yellow"
            action = "watch"
            action_note = "注意觀察"
        elif green_count >= 2 and yellow_count == 0 and red_count == 0:
            status = "green"
            action = "add" if pnl_pct > 0 else "hold"
            action_note = "可考慮加碼" if action == "add" else "繼續持有"
        else:
            status = "yellow" if yellow_count >= 1 else "green"
            action = "hold"
            action_note = "持續觀察" if status == "yellow" else "繼續持有"

        if not signals:
            signals = ["無明顯訊號，持續觀察"]

        return HoldingSignal(
            stock_code=code,
            stock_name=name,
            cost_price=cost,
            shares=shares,
            current_price=current,
            pnl_pct=round(pnl_pct, 2),
            status=status,
            signals=signals,
            action=action,
            action_note=action_note,
        )

    def _get_screener_data(self, code: str) -> Optional[dict]:
        """嘗試從 report_screener 取最新資料"""
        try:
            from backend.services.report_screener import all_screener
            rows = all_screener(limit=200)
            for row in rows:
                row_code = row.stock_id if hasattr(row, "stock_id") else row.get("stock_id", "")
                if row_code == code:
                    return {
                        "mom_5d":          getattr(row, "change_pct", 0) / 100,
                        "foreign_buy_days": getattr(row, "foreign_buy_days", 0),
                        "model_score":     getattr(row, "model_score", 50),
                        "momentum_20d":    getattr(row, "momentum_score", 50) / 50,
                    }
        except Exception:
            pass
        return None

    def format_report(self, signals: list[HoldingSignal]) -> str:
        """格式化 LINE 持倉健康報告"""
        if not signals:
            return "🛡️ 持倉健康報告\n\n庫存為空"

        now   = datetime.now().strftime("%m/%d %H:%M")
        green = [s for s in signals if s.status == "green"]
        yellow= [s for s in signals if s.status == "yellow"]
        red   = [s for s in signals if s.status == "red"]

        lines = [
            f"🛡️ 持倉健康報告  {now}",
            f"✅{len(green)} 正常  ⚠️{len(yellow)} 注意  🔴{len(red)} 警示",
            "─" * 22,
        ]
        for s in signals:
            lines.append(s.format_line())

        return "\n".join(lines)

    async def push(
        self,
        signals: list[HoldingSignal],
        uid:     str,
        token:   str,
    ) -> None:
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
