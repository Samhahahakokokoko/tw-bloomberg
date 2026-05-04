"""
institutional_footprint_engine.py — 4D 法人足跡追蹤

四個維度同時確認才算「Smart Money 進場」：
  1. 外資 (Foreign Investors)   — 持倉變化 + 買賣超
  2. 投信 (Investment Trust)    — 買賣超 + 持倉
  3. ETF 申購 (ETF Flow)        — 成分股申購贖回
  4. 主力分點 (Key Brokers)     — 特定券商分點異常買盤

信號等級：
  ★★★★ 四維共振 — 最強進場信號
  ★★★  三維共振 — 強烈信號
  ★★   二維共振 — 中等信號
  ★    單維異動 — 觀察
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ForeignSignal:
    net_buy_5d:    float   # 萬股
    holding_change: float  # 持股比例變化 %
    consecutive_buy: int   # 連續買超天數
    score: float = 0.0     # 0-1

    def compute_score(self) -> float:
        s = 0.0
        if self.net_buy_5d > 5000:   s += 0.4
        elif self.net_buy_5d > 1000: s += 0.2
        if self.holding_change > 0.5: s += 0.3
        elif self.holding_change > 0.1: s += 0.15
        if self.consecutive_buy >= 5: s += 0.3
        elif self.consecutive_buy >= 3: s += 0.15
        self.score = min(1.0, s)
        return self.score


@dataclass
class TrustSignal:
    net_buy_5d:    float
    holding_pct:   float   # 投信持股比 %
    recent_trend:  str     # "increasing" / "stable" / "decreasing"
    score: float = 0.0

    def compute_score(self) -> float:
        s = 0.0
        if self.net_buy_5d > 1000:  s += 0.4
        elif self.net_buy_5d > 200: s += 0.2
        if self.holding_pct > 3.0:  s += 0.3
        elif self.holding_pct > 1.0: s += 0.15
        if self.recent_trend == "increasing": s += 0.3
        elif self.recent_trend == "stable":   s += 0.1
        self.score = min(1.0, s)
        return self.score


@dataclass
class ETFFlowSignal:
    net_subscribe_5d: float   # 淨申購張數（正=申購，負=贖回）
    top_etf_holding:  float   # 主要ETF持股比 %
    score: float = 0.0

    def compute_score(self) -> float:
        s = 0.0
        if self.net_subscribe_5d > 2000:  s += 0.5
        elif self.net_subscribe_5d > 500: s += 0.25
        if self.top_etf_holding > 5.0:    s += 0.5
        elif self.top_etf_holding > 2.0:  s += 0.25
        self.score = min(1.0, s)
        return self.score


@dataclass
class BrokerSignal:
    key_brokers_buying: int    # 主力分點買進數
    concentration:      float  # 買盤集中度 0-1
    anomaly_detected:   bool   # 是否偵測到異常買盤
    score: float = 0.0

    def compute_score(self) -> float:
        s = 0.0
        if self.key_brokers_buying >= 3:  s += 0.3
        elif self.key_brokers_buying >= 1: s += 0.15
        if self.concentration > 0.6:  s += 0.4
        elif self.concentration > 0.3: s += 0.2
        if self.anomaly_detected: s += 0.3
        self.score = min(1.0, s)
        return self.score


@dataclass
class InstitutionalFootprint:
    stock_id:     str
    stock_name:   str
    foreign:      ForeignSignal
    trust:        TrustSignal
    etf_flow:     ETFFlowSignal
    broker:       BrokerSignal
    dimensions:   int = 0       # 多少維度確認（0-4）
    total_score:  float = 0.0   # 0-100 綜合分數
    is_smart_money: bool = False
    ts:           str = field(default_factory=lambda: datetime.now().isoformat())

    def compute(self) -> "InstitutionalFootprint":
        fs = self.foreign.compute_score()
        ts = self.trust.compute_score()
        es = self.etf_flow.compute_score()
        bs = self.broker.compute_score()

        THRESHOLD = 0.35
        dims = sum([
            fs >= THRESHOLD,
            ts >= THRESHOLD,
            es >= THRESHOLD,
            bs >= THRESHOLD,
        ])
        self.dimensions  = dims
        self.total_score = round((fs * 0.35 + ts * 0.30 + es * 0.20 + bs * 0.15) * 100, 1)
        self.is_smart_money = dims >= 3
        return self

    @property
    def stars(self) -> str:
        return "★" * self.dimensions + "☆" * (4 - self.dimensions)

    def to_line_text(self) -> str:
        lines = [
            f"{self.stars} {self.stock_id} {self.stock_name}",
            f"法人足跡：{self.dimensions}/4 維度確認",
            f"外資  {'✅' if self.foreign.score >= 0.35 else '⬜'} "
            f"淨買:{self.foreign.net_buy_5d:+.0f}張 連{self.foreign.consecutive_buy}日",
            f"投信  {'✅' if self.trust.score >= 0.35 else '⬜'} "
            f"淨買:{self.trust.net_buy_5d:+.0f}張",
            f"ETF   {'✅' if self.etf_flow.score >= 0.35 else '⬜'} "
            f"申購:{self.etf_flow.net_subscribe_5d:+.0f}張",
            f"主力  {'✅' if self.broker.score >= 0.35 else '⬜'} "
            f"{self.broker.key_brokers_buying}分點進場",
            f"綜合分：{self.total_score:.0f}/100",
        ]
        if self.is_smart_money:
            lines.append("🔥 Smart Money 四維共振！")
        return "\n".join(lines)


async def scan_institutional_footprint(
    stocks_data: list[dict] | None = None
) -> list[InstitutionalFootprint]:
    """
    stocks_data 格式：
    [{
      stock_id, stock_name,
      foreign_net_5d, foreign_holding_change, foreign_consecutive,
      trust_net_5d, trust_holding_pct, trust_trend,
      etf_net_subscribe, etf_holding_pct,
      broker_count, broker_concentration, broker_anomaly
    }]
    """
    if not stocks_data:
        stocks_data = [
            {
                "stock_id": "2330", "stock_name": "台積電",
                "foreign_net_5d": 8000, "foreign_holding_change": 0.6, "foreign_consecutive": 6,
                "trust_net_5d": 1500, "trust_holding_pct": 2.5, "trust_trend": "increasing",
                "etf_net_subscribe": 3000, "etf_holding_pct": 8.0,
                "broker_count": 4, "broker_concentration": 0.65, "broker_anomaly": True,
            },
            {
                "stock_id": "3661", "stock_name": "世芯-KY",
                "foreign_net_5d": 3000, "foreign_holding_change": 0.8, "foreign_consecutive": 4,
                "trust_net_5d": 800, "trust_holding_pct": 4.0, "trust_trend": "increasing",
                "etf_net_subscribe": 500, "etf_holding_pct": 2.0,
                "broker_count": 2, "broker_concentration": 0.45, "broker_anomaly": False,
            },
            {
                "stock_id": "2382", "stock_name": "廣達",
                "foreign_net_5d": 500, "foreign_holding_change": 0.05, "foreign_consecutive": 1,
                "trust_net_5d": 100, "trust_holding_pct": 0.5, "trust_trend": "stable",
                "etf_net_subscribe": 200, "etf_holding_pct": 1.0,
                "broker_count": 1, "broker_concentration": 0.2, "broker_anomaly": False,
            },
        ]

    results: list[InstitutionalFootprint] = []
    for d in stocks_data:
        fp = InstitutionalFootprint(
            stock_id   = d["stock_id"],
            stock_name = d["stock_name"],
            foreign = ForeignSignal(
                net_buy_5d      = d.get("foreign_net_5d", 0),
                holding_change  = d.get("foreign_holding_change", 0),
                consecutive_buy = d.get("foreign_consecutive", 0),
            ),
            trust = TrustSignal(
                net_buy_5d   = d.get("trust_net_5d", 0),
                holding_pct  = d.get("trust_holding_pct", 0),
                recent_trend = d.get("trust_trend", "stable"),
            ),
            etf_flow = ETFFlowSignal(
                net_subscribe_5d = d.get("etf_net_subscribe", 0),
                top_etf_holding  = d.get("etf_holding_pct", 0),
            ),
            broker = BrokerSignal(
                key_brokers_buying = d.get("broker_count", 0),
                concentration      = d.get("broker_concentration", 0),
                anomaly_detected   = d.get("broker_anomaly", False),
            ),
        ).compute()
        results.append(fp)

    results.sort(key=lambda r: (-r.dimensions, -r.total_score))
    return results


def format_footprint_summary(results: list[InstitutionalFootprint]) -> str:
    if not results:
        return "⚠️ 暫無法人足跡資料"
    smart = [r for r in results if r.is_smart_money]
    lines = [f"🏦 法人足跡掃描（Smart Money: {len(smart)}檔）"]
    for r in results[:5]:
        lines.append(f"{r.stars} {r.stock_id} {r.stock_name} {r.total_score:.0f}分 {r.dimensions}維")
    return "\n".join(lines)
