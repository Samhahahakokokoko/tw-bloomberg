"""
lead_lag_engine.py — 產業鏈領先/滯後關係引擎

已知領先關係（可手動設定）：
  散熱 → PCB → AI伺服器          (3-5日領先)
  外資期貨 → 現貨               (1-2日領先)
  台積電 → 半導體族群            (1-3日領先)
  記憶體 → 電子下游             (2-5日領先)
  台幣升貶 → 出口電子           (隔日效應)

自動計算：cross-correlation 偵測 lag 最大相關係數
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


KNOWN_PAIRS: list[dict] = [
    {"leader": "散熱",  "follower": "PCB",    "lag_days": 3, "corr": 0.72, "note": "熱管需求傳遞"},
    {"leader": "PCB",   "follower": "AI伺服器", "lag_days": 5, "corr": 0.68, "note": "PCB出貨確認"},
    {"leader": "台積電", "follower": "半導體",   "lag_days": 2, "corr": 0.81, "note": "市值佔比效應"},
    {"leader": "記憶體", "follower": "面板",     "lag_days": 3, "corr": 0.55, "note": "零組件庫存連動"},
    {"leader": "外資期", "follower": "現貨",     "lag_days": 1, "corr": 0.77, "note": "期現套利"},
    {"leader": "美費城半", "follower": "台半導", "lag_days": 1, "corr": 0.85, "note": "隔夜連動"},
    {"leader": "ETF申購", "follower": "成份股", "lag_days": 1, "corr": 0.70, "note": "被動買盤"},
    {"leader": "蘋果供應鏈", "follower": "組裝", "lag_days": 2, "corr": 0.63, "note": "組裝滯後"},
]

SECTOR_STOCKS: dict[str, list[str]] = {
    "散熱":   ["3324", "8097", "6230", "2382"],
    "PCB":    ["8046", "3037", "2383"],
    "AI伺服器": ["3231", "6669", "2382"],
    "台積電":  ["2330"],
    "半導體":  ["2303", "2454", "2379", "3711"],
    "記憶體":  ["2408", "4256"],
    "面板":    ["2409", "3481"],
}


@dataclass
class LeadLagSignal:
    leader_sector:   str
    follower_sector: str
    lag_days:        int
    correlation:     float
    leader_return:   float    # 領先族群近期報酬
    signal_strength: float    # 0-1，越大越強
    note:            str = ""
    triggered:       bool = False   # 是否已觸發領先信號
    ts:              str = field(default_factory=lambda: datetime.now().isoformat())

    def to_line_text(self) -> str:
        icon = "🟢" if self.triggered else "⚪"
        lines = [
            f"{icon} {self.leader_sector} → {self.follower_sector}",
            f"領先週期：{self.lag_days} 日｜相關係數：{self.correlation:.2f}",
            f"領先族群近期：{self.leader_return:+.1%}",
            f"信號強度：{'▓' * int(self.signal_strength * 10)}{'░' * (10 - int(self.signal_strength * 10))}",
        ]
        if self.note:
            lines.append(f"邏輯：{self.note}")
        if self.triggered:
            lines.append(f"⚡ 已觸發！預期 {self.follower_sector} {self.lag_days} 日內跟漲")
        return "\n".join(lines)


@dataclass
class LeadLagResult:
    triggered_signals: list[LeadLagSignal] = field(default_factory=list)
    all_signals:       list[LeadLagSignal] = field(default_factory=list)
    ts:                str = field(default_factory=lambda: datetime.now().isoformat())

    def top_signals(self, n: int = 3) -> list[LeadLagSignal]:
        return sorted(self.triggered_signals, key=lambda s: -s.signal_strength)[:n]

    def to_line_text(self) -> str:
        if not self.triggered_signals:
            return "⚡ 領先/滯後信號：目前無強烈觸發"
        lines = [f"⚡ 領先信號（共{len(self.triggered_signals)}組觸發）"]
        for s in self.top_signals(3):
            lines.append("")
            lines.append(s.to_line_text())
        return "\n".join(lines)


def _cross_corr_lag(series_a: np.ndarray, series_b: np.ndarray, max_lag: int = 10) -> tuple[int, float]:
    """計算 a 領先 b 的最佳 lag 與相關係數"""
    best_lag, best_corr = 0, 0.0
    n = len(series_a)
    for lag in range(1, min(max_lag + 1, n - 1)):
        a = series_a[:-lag]
        b = series_b[lag:]
        if len(a) < 5:
            break
        corr = float(np.corrcoef(a, b)[0, 1])
        if corr > best_corr:
            best_corr = corr
            best_lag = lag
    return best_lag, best_corr


async def run_lead_lag_scan(sector_returns: dict[str, list[float]] | None = None) -> LeadLagResult:
    """
    sector_returns: {sector_name: [daily_returns last 30 days]}
    若為 None，使用 mock 資料
    """
    if not sector_returns:
        sector_returns = {
            "散熱":    [0.01, 0.02, 0.03, 0.01, 0.04, 0.02, 0.00, 0.01, 0.03, 0.05,
                        0.02, 0.01, 0.03, 0.02, 0.01, 0.00, 0.02, 0.03, 0.01, 0.02],
            "PCB":     [0.00, 0.01, 0.01, 0.02, 0.01, 0.03, 0.01, 0.00, 0.01, 0.02,
                        0.04, 0.01, 0.01, 0.03, 0.02, 0.01, 0.01, 0.02, 0.03, 0.01],
            "AI伺服器": [0.00, 0.01, 0.01, 0.01, 0.02, 0.01, 0.02, 0.01, 0.00, 0.01,
                        0.02, 0.03, 0.01, 0.01, 0.02, 0.03, 0.01, 0.01, 0.02, 0.04],
            "台積電":  [0.01, 0.03, 0.02, 0.01, 0.02, 0.03, 0.01, 0.02, 0.03, 0.02,
                        0.01, 0.02, 0.02, 0.01, 0.03, 0.02, 0.01, 0.02, 0.01, 0.02],
            "半導體":  [0.00, 0.02, 0.01, 0.00, 0.02, 0.02, 0.02, 0.01, 0.02, 0.01,
                        0.01, 0.01, 0.02, 0.01, 0.02, 0.02, 0.02, 0.01, 0.01, 0.02],
        }

    all_signals: list[LeadLagSignal] = []

    for pair in KNOWN_PAIRS:
        leader = pair["leader"]
        follower = pair["follower"]
        known_lag = pair["lag_days"]
        known_corr = pair["corr"]

        # 使用已知 lag，若有實際資料則計算 leader 近期報酬
        leader_data = sector_returns.get(leader, [])
        follower_data = sector_returns.get(follower, [])

        leader_ret = float(np.mean(leader_data[-5:])) if len(leader_data) >= 5 else 0.0

        # 自動計算 cross-correlation（若資料充足）
        auto_lag, auto_corr = known_lag, known_corr
        if len(leader_data) >= 15 and len(follower_data) >= 15:
            la = np.array(leader_data)
            lb = np.array(follower_data)
            auto_lag, auto_corr = _cross_corr_lag(la, lb)
            if auto_corr < 0.3:
                auto_lag, auto_corr = known_lag, known_corr

        # 觸發條件：領先族群近5日報酬 > 3%，相關係數 > 0.5
        triggered = (leader_ret > 0.03) and (auto_corr > 0.5)

        signal_strength = min(1.0, (leader_ret * 10) * auto_corr)

        sig = LeadLagSignal(
            leader_sector   = leader,
            follower_sector = follower,
            lag_days        = auto_lag,
            correlation     = auto_corr,
            leader_return   = leader_ret,
            signal_strength = signal_strength,
            note            = pair.get("note", ""),
            triggered       = triggered,
        )
        all_signals.append(sig)

    triggered_signals = [s for s in all_signals if s.triggered]
    triggered_signals.sort(key=lambda s: -s.signal_strength)

    return LeadLagResult(
        triggered_signals = triggered_signals,
        all_signals       = all_signals,
    )
