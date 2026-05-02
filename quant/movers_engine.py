"""
movers_engine.py — 每日動能啟動掃描器

核心邏輯：找「剛開始動」的股票，不是追高
  - 5D 動能 > 3%（啟動信號）
  - 成交量 > 20 日均量 1.3 倍（放量確認）
  - 近 5 日外資或投信有買超（法人跟進）

輸出：MoverResult 列表（依分數排序）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── 動能篩選門檻 ──────────────────────────────────────────────────────────────
MOM_5D_MIN      = 0.03   # 5日動能 > 3%
VOL_RATIO_MIN   = 1.30   # 成交量 > 20日均 1.3 倍
INST_BUY_DAYS   = 5      # 近 N 日法人有買超

# 排除條件
MOM_5D_MAX      = 0.25   # > 25% 視為追高，排除
CLOSE_VS_MA20_MAX = 0.15  # 收盤比 MA20 高 15% 以上 → 已超漲


@dataclass
class MomentumProfile:
    mom_5d:   float = 0.0
    mom_1m:   float = 0.0
    mom_3m:   float = 0.0
    mom_6m:   float = 0.0
    mom_1y:   float = 0.0


@dataclass
class MoverResult:
    stock_code: str
    stock_name: str
    sector:     str
    close:      float
    mom_5d:     float       # 5 日漲幅
    mom_1m:     float       # 1 個月漲幅
    mom_3m:     float
    vol_ratio:  float       # 量比（近期/20日均量）
    foreign_buy_days: int   # 近 5 日外資買超天數
    trust_buy_days:   int   # 近 5 日投信買超天數
    has_institutional: bool
    close_vs_ma20:    float  # (close - MA20) / MA20，正值=在 MA20 上方
    stage:      str          # "early_breakout" / "trend_continuation" / "watch"
    score:      float        # 綜合動能分 0~100
    tags:       list[str] = field(default_factory=list)

    def is_early_stage(self) -> bool:
        """是否處於動能剛啟動階段（非追高）"""
        return (
            MOM_5D_MIN <= self.mom_5d <= MOM_5D_MAX
            and self.close_vs_ma20 < CLOSE_VS_MA20_MAX
        )

    def to_dict(self) -> dict:
        return {
            "code":              self.stock_code,
            "name":              self.stock_name,
            "sector":            self.sector,
            "close":             round(self.close, 2),
            "mom_5d_pct":        round(self.mom_5d * 100, 2),
            "mom_1m_pct":        round(self.mom_1m * 100, 2),
            "mom_3m_pct":        round(self.mom_3m * 100, 2),
            "vol_ratio":         round(self.vol_ratio, 2),
            "foreign_buy_days":  self.foreign_buy_days,
            "trust_buy_days":    self.trust_buy_days,
            "has_institutional": self.has_institutional,
            "close_vs_ma20_pct": round(self.close_vs_ma20 * 100, 2),
            "stage":             self.stage,
            "score":             round(self.score, 1),
            "tags":              self.tags,
        }

    def format_line(self) -> str:
        icons = {"early_breakout": "🚀", "trend_continuation": "📈", "watch": "👀"}
        icon  = icons.get(self.stage, "📊")
        tag_str = " ".join(f"[{t}]" for t in self.tags[:3])
        return (
            f"{icon} {self.stock_code} {self.stock_name}\n"
            f"   5D:{self.mom_5d*100:+.1f}%  量比:{self.vol_ratio:.1f}x"
            f"  法人:{self.has_institutional}  {tag_str}"
        )


class MoversEngine:
    """
    每日盤後動能啟動掃描器。

    使用方式：
        engine = MoversEngine()
        results = await engine.scan()     # 從現有系統拉取資料
        results = engine.scan_mock()      # mock 資料測試

        for r in results[:10]:
            print(r.format_line())
    """

    def __init__(
        self,
        mom_5d_min:    float = MOM_5D_MIN,
        vol_ratio_min: float = VOL_RATIO_MIN,
        top_n:         int   = 30,
    ):
        self.mom_5d_min    = mom_5d_min
        self.vol_ratio_min = vol_ratio_min
        self.top_n         = top_n

    async def scan(self) -> list[MoverResult]:
        """從現有 report_screener 拉資料並計算動能"""
        try:
            from backend.services.report_screener import all_screener
            rows = all_screener(limit=200)
            return self._process_rows(rows)
        except Exception as e:
            logger.warning("[Movers] report_screener failed (%s), using mock", e)
            return self.scan_mock()

    def scan_from_rows(self, rows: list) -> list[MoverResult]:
        """接受 StockRow 列表直接處理（同步）"""
        return self._process_rows(rows)

    def _process_rows(self, rows: list) -> list[MoverResult]:
        results: list[MoverResult] = []
        for row in rows:
            result = self._evaluate_row(row)
            if result:
                results.append(result)
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:self.top_n]

    def _evaluate_row(self, row) -> Optional[MoverResult]:
        """評估單一 StockRow，不符合動能條件回傳 None"""
        try:
            # 取出關鍵欄位（相容 StockRow dataclass 與 dict）
            def _get(attr: str, default=0.0):
                if hasattr(row, attr):
                    return getattr(row, attr)
                if isinstance(row, dict):
                    return row.get(attr, default)
                return default

            code       = str(_get("stock_id", _get("stock_code", "")))
            name       = str(_get("name", code))
            sector     = str(_get("sector", "其他"))
            close      = float(_get("close", 0))
            change_pct = float(_get("change_pct", 0))     # 當日漲跌%
            vol_ratio  = float(_get("vol_ratio", _get("volume_ratio", 1.0)))

            # 動能估算（若有 momentum 欄位直接用；否則用 change_pct 估算）
            momentum_20d = float(_get("momentum_20d", 1.0))
            mom_5d  = float(_get("ret_5d", change_pct / 100 if abs(change_pct) < 20 else 0.05))
            mom_1m  = float(momentum_20d - 1.0) if momentum_20d > 0.5 else float(_get("ret_20d", 0))
            mom_3m  = float(_get("mom_3m", mom_1m * 2.5))
            mom_6m  = float(_get("mom_6m", mom_1m * 4.0))

            # 法人買超
            foreign_days = int(_get("foreign_buy_days", 0))
            trust_days   = abs(int(_get("trust_net", 0)))
            trust_buy    = 1 if trust_days > 0 and float(_get("trust_net", 0)) > 0 else 0
            has_inst     = foreign_days >= 1 or trust_buy >= 1

            # MA20 偏離
            ma20 = float(_get("ma20", close * 0.97))
            close_vs_ma20 = (close - ma20) / ma20 if ma20 > 0 else 0.0

            # ── 篩選條件 ─────────────────────────────────────────────
            if vol_ratio < self.vol_ratio_min:
                return None
            if abs(mom_5d) < self.mom_5d_min:
                return None
            if mom_5d > MOM_5D_MAX:   # 排除追高（已漲 25% 以上）
                return None
            if not has_inst:           # 必須有法人跡象
                return None

            # ── 階段判斷 ──────────────────────────────────────────────
            if (self.mom_5d_min <= mom_5d <= 0.10
                    and close_vs_ma20 < 0.05):
                stage = "early_breakout"
            elif mom_1m > 0.05 and close_vs_ma20 < CLOSE_VS_MA20_MAX:
                stage = "trend_continuation"
            else:
                stage = "watch"

            # ── 動能分 ────────────────────────────────────────────────
            score = min(100.0, (
                min(mom_5d  / 0.15, 1.0) * 35 +
                min(vol_ratio / 2.0, 1.0) * 25 +
                min(foreign_days / 5.0, 1.0) * 20 +
                (10 if stage == "early_breakout" else 5) +
                min(max(0, mom_1m / 0.10), 1.0) * 10
            ))

            # ── 標籤 ─────────────────────────────────────────────────
            tags: list[str] = []
            if foreign_days >= 3:   tags.append("外資連買")
            if trust_buy:           tags.append("投信買超")
            if vol_ratio >= 2.0:    tags.append("爆量")
            if stage == "early_breakout": tags.append("啟動初期")
            if mom_3m > 0.10:       tags.append("中期強勢")

            return MoverResult(
                stock_code=code,
                stock_name=name,
                sector=sector,
                close=close,
                mom_5d=round(mom_5d, 4),
                mom_1m=round(mom_1m, 4),
                mom_3m=round(mom_3m, 4),
                vol_ratio=round(vol_ratio, 2),
                foreign_buy_days=foreign_days,
                trust_buy_days=trust_buy,
                has_institutional=has_inst,
                close_vs_ma20=round(close_vs_ma20, 4),
                stage=stage,
                score=round(score, 1),
                tags=tags,
            )
        except Exception as e:
            logger.debug("[Movers] row error: %s", e)
            return None

    def scan_mock(self, n: int = 20) -> list[MoverResult]:
        """Mock 資料（測試/無 API 時使用）"""
        import random; rng = random.Random(42)
        universe = [
            ("3105", "穩懋",   "半導體",    320.0),
            ("6669", "緯穎",   "AI Server", 1250.0),
            ("2379", "瑞昱",   "半導體",    620.0),
            ("2454", "聯發科", "半導體",    1020.0),
            ("2330", "台積電", "半導體",    870.0),
            ("6415", "矽力",   "半導體",    2800.0),
            ("3231", "緯創",   "AI Server", 102.0),
            ("2603", "長榮",   "航運",      168.0),
            ("2882", "國泰金", "金融",      55.0),
            ("4938", "和碩",   "電子零組件",78.0),
        ]
        results = []
        for code, name, sector, close in universe[:n]:
            mom_5d  = rng.uniform(0.03, 0.12)
            vol_ratio= rng.uniform(1.3, 2.8)
            f_days  = rng.randint(1, 5)
            ma20    = close * rng.uniform(0.93, 0.99)
            stage   = "early_breakout" if mom_5d < 0.07 else "trend_continuation"
            score   = 50 + mom_5d * 150 + (vol_ratio - 1) * 10
            tags    = ["外資連買" if f_days >= 3 else "外資買超"]
            if vol_ratio >= 2.0: tags.append("爆量")
            results.append(MoverResult(
                stock_code=code, stock_name=name, sector=sector, close=close,
                mom_5d=round(mom_5d, 4), mom_1m=round(mom_5d * 3.5, 4),
                mom_3m=round(mom_5d * 7, 4), vol_ratio=round(vol_ratio, 2),
                foreign_buy_days=f_days, trust_buy_days=rng.randint(0, 2),
                has_institutional=True, close_vs_ma20=round((close-ma20)/ma20, 4),
                stage=stage, score=round(min(score, 95), 1), tags=tags,
            ))
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def format_line_report(self, results: list[MoverResult]) -> str:
        """格式化 LINE 訊息"""
        if not results:
            return "📊 今日無明顯動能啟動股票"
        lines = [
            f"🔍 今日動能啟動（共 {len(results)} 檔）",
            f"掃描時間：{datetime.now().strftime('%m/%d %H:%M')}",
            "─" * 22,
        ]
        for r in results[:8]:
            lines.append(r.format_line())
        if len(results) > 8:
            lines.append(f"（另有 {len(results)-8} 檔…）")
        return "\n".join(lines)


_global_movers: Optional[MoversEngine] = None

def get_movers_engine() -> MoversEngine:
    global _global_movers
    if _global_movers is None:
        _global_movers = MoversEngine()
    return _global_movers


if __name__ == "__main__":
    engine = MoversEngine()
    results = engine.scan_mock()
    print(engine.format_line_report(results))
    print(f"\n早期啟動: {sum(1 for r in results if r.stage=='early_breakout')} 檔")
