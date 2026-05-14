"""
movers_engine.py — Layer 1: 每日動能啟動掃描

找「剛開始動、非追高」的股票。

納入條件（全部通過）：
  - 5D return > 3%（脫離盤整）
  - volume_ratio > 1.3x 20日均量（不是無量上漲）
  - foreign_buy_5d > 0（法人開始進場）

排除條件（任一觸發即排除）：
  - 5D return > 25%（過熱追高）
  - distance_from_MA20 > 15%（乖離過大）
  - avg_volume < 500 張（流動性差）

輸出：DataFrame 含 stock_id/name/5d_return/volume_ratio/foreign_buy_5d/score
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── 納入門檻 ──────────────────────────────────────────────────────────────────
INC_5D_RETURN_MIN   = 0.03   # 5日報酬 > 3%
INC_VOL_RATIO_MIN   = 1.30   # 量比 > 1.3x
INC_FOREIGN_BUY_MIN = 0      # 外資5日淨買 > 0（正值即可）

# ── 排除門檻 ──────────────────────────────────────────────────────────────────
EXC_5D_RETURN_MAX   = 0.25   # 5日報酬 > 25% → 過熱
EXC_MA20_DISTANCE   = 0.15   # 偏離MA20 > 15% → 乖離過大
EXC_AVG_VOL_MIN_K   = 500    # 日均量 < 500 張 → 流動性差


@dataclass
class MoverResult:
    stock_id:       str
    name:           str
    sector:         str
    close:          float
    ret_5d:         float      # 5日報酬率
    ret_1m:         float
    ret_3m:         float
    volume_ratio:   float      # 近5日量 / 20日均量
    avg_volume_k:   float      # 日均量（張）
    foreign_buy_5d: float      # 外資近5日淨買（張）
    trust_buy_5d:   float
    ma20:           float
    ma60:           float
    distance_from_ma20: float  # (close - MA20) / MA20
    score:          float      # 0~100 動能啟動分
    stage:          str        # early_breakout / trend_continuation / watch
    include_reasons: list[str] = field(default_factory=list)
    is_mock:        bool = False   # 標記是否為假資料（screener 失敗 fallback）

    def to_series(self) -> pd.Series:
        return pd.Series({
            "stock_id":         self.stock_id,
            "name":             self.name,
            "sector":           self.sector,
            "close":            round(self.close, 2),
            "5d_return":        round(self.ret_5d * 100, 2),
            "1m_return":        round(self.ret_1m * 100, 2),
            "3m_return":        round(self.ret_3m * 100, 2),
            "volume_ratio":     round(self.volume_ratio, 2),
            "avg_volume_k":     round(self.avg_volume_k, 0),
            "foreign_buy_5d":   round(self.foreign_buy_5d, 0),
            "trust_buy_5d":     round(self.trust_buy_5d, 0),
            "distance_from_ma20": round(self.distance_from_ma20 * 100, 2),
            "score":            round(self.score, 1),
            "stage":            self.stage,
        })

    def format_line(self) -> str:
        icons = {"early_breakout": "🚀", "trend_continuation": "📈", "watch": "👀"}
        icon  = icons.get(self.stage, "📊")
        return (
            f"{icon} {self.stock_id} {self.name}\n"
            f"   5D:{self.ret_5d*100:+.1f}%  量比:{self.volume_ratio:.1f}x"
            f"  外資:{self.foreign_buy_5d:+.0f}張  分:{self.score:.0f}"
        )


class MoversEngine:
    """
    每日盤後動能啟動掃描器。

    async scan()   → 從 report_screener 取資料
    scan_mock()    → Mock 資料，可獨立測試
    to_dataframe() → 輸出 DataFrame（pipeline 用）
    """

    def __init__(self, top_n: int = 30):
        self.top_n = top_n

    # ── 主入口 ───────────────────────────────────────────────────────────────

    async def scan(self) -> list[MoverResult]:
        """從 TWSE 全市場動態池取資料並評估（優先）；失敗才用靜態 pool；最後 mock"""
        try:
            from backend.services.report_screener import async_all_screener
            rows = await async_all_screener(limit=500)
            if not rows:
                raise ValueError("async_all_screener returned empty")
            results = [r for r in (self._eval(row) for row in rows) if r]
            results.sort(key=lambda r: r.score, reverse=True)
            logger.info("[Movers] universe=%d → passed=%d", len(rows), len(results))
            return results[:self.top_n]
        except Exception as e:
            logger.warning("[Movers] async_all_screener failed (%s), trying static pool", e)

        try:
            from backend.services.report_screener import all_screener
            rows = all_screener(limit=300)
            if not rows:
                raise ValueError("all_screener empty")
            results = [r for r in (self._eval(row) for row in rows) if r]
            results.sort(key=lambda r: r.score, reverse=True)
            logger.info("[Movers] static pool=%d → passed=%d", len(rows), len(results))
            return results[:self.top_n]
        except Exception as e2:
            logger.warning("[Movers] static pool failed (%s) → MOCK", e2)

        mock = self.scan_mock()
        for r in mock:
            r.is_mock = True
        return mock

    def scan_mock(self, n: int = 20) -> list[MoverResult]:
        """Mock 結構資料（測試 / API 失敗時）。
        close 欄位來自 _MOCK_UNIVERSE（可能為 0），
        呼叫方應在取得 rt_cache 後用 _enrich_mock_close() 填入今日真實收盤。
        """
        universe = _MOCK_UNIVERSE[:n]
        results = []
        rng = np.random.default_rng(42)
        for s in universe:
            close  = s["close"]   # 可能為 0，待 rt_cache 覆蓋
            ret5   = rng.uniform(0.03, 0.14)
            vol_r  = rng.uniform(1.3, 2.8)
            f_buy  = rng.uniform(200, 8000)
            ma20   = close * rng.uniform(0.91, 0.99) if close > 0 else 0.0
            ma60   = ma20  * rng.uniform(0.92, 0.99) if ma20  > 0 else 0.0
            dist   = (close - ma20) / ma20 if ma20 > 0 else 0.0
            score  = self._calc_score(ret5, vol_r, f_buy, dist)
            stage  = ("early_breakout" if ret5 < 0.07 and dist < 0.05
                      else "trend_continuation")
            results.append(MoverResult(
                stock_id=s["stock_id"], name=s["name"], sector=s["sector"],
                close=close, ret_5d=ret5, ret_1m=ret5*3.2, ret_3m=ret5*7,
                volume_ratio=round(vol_r, 2), avg_volume_k=rng.uniform(500, 8000),
                foreign_buy_5d=round(f_buy, 0), trust_buy_5d=round(rng.uniform(0, 500), 0),
                ma20=round(ma20, 2), ma60=round(ma60, 2),
                distance_from_ma20=round(dist, 4), score=round(score, 1),
                stage=stage, include_reasons=["5D>3%", "量比>1.3x", "外資買超"],
            ))
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def to_dataframe(self, results: list[MoverResult]) -> pd.DataFrame:
        """輸出 pipeline 用 DataFrame"""
        if not results:
            return pd.DataFrame()
        return pd.DataFrame([r.to_series() for r in results]).reset_index(drop=True)

    # ── 單股評估 ─────────────────────────────────────────────────────────────

    def _eval(self, row) -> Optional[MoverResult]:
        try:
            def g(attr, d=0.0):
                return float(getattr(row, attr, None) or
                             (row.get(attr, d) if isinstance(row, dict) else d))
            def gs(attr, d=""):
                return str(getattr(row, attr, None) or
                           (row.get(attr, d) if isinstance(row, dict) else d))

            stock_id = gs("stock_id", gs("code", ""))
            name     = gs("name", stock_id)
            sector   = gs("sector", "其他")
            close    = g("close", 0.0)
            vol_k    = g("volume", 0)   # StockRow.volume 已是「張」，不再 ÷1000

            if close <= 0:
                return None   # 無有效收盤價，不應進入決策

            # ── 量比：優先讀 StockRow 衍生欄位 vol_ratio ──────────────────────
            vol_r = g("vol_ratio", g("volume_ratio", 0.0))
            if vol_r == 0.0:
                # fallback：vol_k 和 vol_20d_max 都是「張」
                vol_max = g("vol_20d_max", 0)
                vol_r = (vol_k * 1.8 / vol_max) if vol_max > 100 else 1.0

            # ── 外資/法人淨買：優先 foreign_net_5d（chip_5d 別名）──────────────
            f_days   = int(g("foreign_buy_days", 0))
            foreign5 = g("foreign_net_5d", g("chip_5d", g("foreign_net", f_days * 300)))
            trust5   = g("trust_net", 0)

            # ── 5日報酬：優先 ret_5d_approx（screener 計算的估算值）────────────
            ret_5d = g("ret_5d", g("ret_5d_approx", 0.0))
            if ret_5d == 0.0:
                # 最後備援：用今日漲幅 × 2 + 斜率補正
                ret_5d = g("change_pct", 0) / 100 * 2.0 + g("ma20_slope", 0) * 0.005

            ret_1m  = ret_5d * 4
            ret_3m  = ret_5d * 10

            # ── 均線：優先讀欄位，否則從斜率估算 ─────────────────────────────
            ma20 = g("ma20", 0.0)
            if ma20 <= 0:
                slope = g("ma20_slope", 0)
                # slope 為%/期，正斜率表示股價在均線上方
                # close = ma20 × (1 + slope × 0.01) → ma20 = close / (1 + slope*0.01)
                ma20 = close / (1.0 + max(slope, 0) * 0.01) if close > 0 else close * 0.97
            ma60 = g("ma60", close * 0.93)

            dist = (close - ma20) / ma20 if ma20 > 0 else 0.0

            # ── 排除條件 ──────────────────────────────────────────────
            if vol_k > 0 and vol_k < EXC_AVG_VOL_MIN_K:
                return None
            if abs(ret_5d) > EXC_5D_RETURN_MAX:
                return None
            if dist > EXC_MA20_DISTANCE:
                return None

            # ── 納入條件 ──────────────────────────────────────────────
            if ret_5d < INC_5D_RETURN_MIN:
                return None
            if vol_r < INC_VOL_RATIO_MIN:
                return None
            if foreign5 <= INC_FOREIGN_BUY_MIN and f_days <= 0:
                return None

            score = self._calc_score(ret_5d, vol_r, max(foreign5, f_days * 200), dist)
            stage = ("early_breakout" if ret_5d < 0.08 and dist < 0.06
                     else "trend_continuation")

            reasons = []
            if ret_5d >= 0.03: reasons.append(f"5D+{ret_5d*100:.1f}%")
            if vol_r >= 1.3:   reasons.append(f"量比{vol_r:.1f}x")
            if foreign5 > 0:   reasons.append(f"外資+{foreign5:.0f}張")

            return MoverResult(
                stock_id=stock_id, name=name, sector=sector, close=close,
                ret_5d=round(ret_5d, 4), ret_1m=round(ret_1m, 4), ret_3m=round(ret_3m, 4),
                volume_ratio=round(vol_r, 2), avg_volume_k=round(vol_k, 0),
                foreign_buy_5d=round(foreign5, 0), trust_buy_5d=round(trust5, 0),
                ma20=round(ma20, 2), ma60=round(ma60, 2),
                distance_from_ma20=round(dist, 4), score=round(score, 1),
                stage=stage, include_reasons=reasons,
            )
        except Exception as e:
            logger.debug("[Movers] eval error: %s", e)
            return None

    @staticmethod
    def _calc_score(ret_5d, vol_r, foreign_buy, dist_ma20) -> float:
        s  = min(ret_5d  / 0.12, 1.0) * 35
        s += min(vol_r   / 2.5,  1.0) * 25
        s += min(max(foreign_buy, 0) / 5000, 1.0) * 20
        s += max(0, 1.0 - dist_ma20 / 0.10) * 10  # 靠近MA20加分
        s += 10  # base
        return min(s, 100.0)

    def format_report(self, results: list[MoverResult]) -> str:
        if not results:
            return "🔍 今日無明顯動能啟動股票"
        early = [r for r in results if r.stage == "early_breakout"]
        trend = [r for r in results if r.stage == "trend_continuation"]
        lines = [
            f"🔍 動能啟動股票（{len(results)} 檔）  {datetime.now().strftime('%m/%d %H:%M')}",
            f"早期啟動 {len(early)} / 趨勢延續 {len(trend)}",
            "─" * 22,
        ]
        for r in results[:8]:
            lines.append(r.format_line())
        if len(results) > 8:
            lines.append(f"…另有 {len(results)-8} 檔")
        return "\n".join(lines)


# ── 共用 Mock 股票池 ──────────────────────────────────────────────────────────
# ⚠️ MOCK_UNIVERSE 只在測試或 API 完全失敗時使用，正常路徑不應觸及。
# 此處 close=0 表示「待 API 填入」，防止假股價顯示給用戶。
_MOCK_UNIVERSE = [
    {"stock_id": "3105", "name": "穩懋",    "sector": "半導體",    "close": 0},
    {"stock_id": "6669", "name": "緯穎",    "sector": "AI Server", "close": 0},
    {"stock_id": "2379", "name": "瑞昱",    "sector": "半導體",    "close": 0},
    {"stock_id": "2330", "name": "台積電",  "sector": "半導體",    "close": 0},
    {"stock_id": "2454", "name": "聯發科",  "sector": "IC設計",    "close": 0},
    {"stock_id": "6415", "name": "矽力-KY", "sector": "半導體",    "close": 0},
    {"stock_id": "3231", "name": "緯創",    "sector": "AI Server", "close": 0},
    {"stock_id": "2317", "name": "鴻海",    "sector": "電子製造",  "close": 0},
    {"stock_id": "5347", "name": "世界先進","sector": "半導體",    "close": 0},
    {"stock_id": "4938", "name": "和碩",    "sector": "電子製造",  "close": 0},
    {"stock_id": "2382", "name": "廣達",    "sector": "AI Server", "close": 0},
    {"stock_id": "3034", "name": "聯詠",    "sector": "IC設計",    "close": 0},
    {"stock_id": "2308", "name": "台達電",  "sector": "電源零組件","close": 0},
    {"stock_id": "6271", "name": "同欣電",  "sector": "半導體",    "close": 0},
    {"stock_id": "2303", "name": "聯電",    "sector": "半導體",    "close": 0},
    {"stock_id": "2357", "name": "華碩",    "sector": "電腦週邊",  "close": 0},
    {"stock_id": "2412", "name": "中華電",  "sector": "電信",      "close": 0},
    {"stock_id": "2882", "name": "國泰金",  "sector": "金融",      "close": 0},
    {"stock_id": "2603", "name": "長榮",    "sector": "航運",      "close": 0},
    {"stock_id": "2609", "name": "陽明",    "sector": "航運",      "close": 0},
]


def get_movers_engine() -> MoversEngine:
    return MoversEngine()


if __name__ == "__main__":
    engine  = MoversEngine()
    results = engine.scan_mock()
    df      = engine.to_dataframe(results)
    print(df[["stock_id", "name", "5d_return", "volume_ratio",
              "foreign_buy_5d", "score", "stage"]].to_string())
    print(f"\n{engine.format_report(results)}")
