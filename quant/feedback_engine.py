"""
feedback_engine.py — 回饋學習引擎：自動調整 Alpha 訊號權重

架構：
  BacktestRecord   — 單次回測結果快照（持久化到 JSON 或 DB）
  FeedbackEngine   — 核心：分析歷史表現 → 自動調整各策略/特徵權重

權重調整邏輯：
  1. 每次回測完成後記錄 (strategy, sharpe, win_rate, total_return, regime)
  2. 按盤態分組，計算各策略的「勝率加權夏普值」
  3. 對表現超過基準的策略升權，表現不足的降權
  4. 使用指數衰退：越新的回測影響越大（α = 0.9）
  5. 最終權重輸出給 RuleBasedAlpha.WEIGHTS 或 AlphaModel feature importance 調整

自動調整規則（每週日 22:00 由排程觸發）：
  - 夏普值 > 1.0 AND 勝率 > 55% → 策略權重 × 1.1（上限 2.0）
  - 夏普值 < 0.0 OR  勝率 < 40% → 策略權重 × 0.9（下限 0.3）
  - 其他 → 不調整（避免過度震盪）

使用方式：
    fe = FeedbackEngine()
    fe.record_backtest(result, stock_code="2330", strategy="ma_cross", regime="bull")
    weights = fe.get_strategy_weights()
    summary = fe.performance_summary()
    await fe.auto_adjust()   # 非同步版本，寫入 DB
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── 常數 ─────────────────────────────────────────────────────────────────────

DECAY_ALPHA       = 0.90   # 指數衰退：越新的記錄權重越高
SHARPE_GOOD       = 1.0    # 夏普值達此 → 升權
SHARPE_BAD        = 0.0    # 夏普值低於此 → 降權
WIN_RATE_GOOD     = 0.55   # 勝率達此 → 升權
WIN_RATE_BAD      = 0.40   # 勝率低於此 → 降權
WEIGHT_UP_FACTOR  = 1.10   # 升權乘數
WEIGHT_DN_FACTOR  = 0.90   # 降權乘數
WEIGHT_MAX        = 2.0    # 策略權重上限
WEIGHT_MIN        = 0.3    # 策略權重下限
MIN_RECORDS_ADJUST = 5     # 至少需要 N 筆記錄才進行調整

# 預設儲存路徑（可由環境變數覆蓋）
DEFAULT_STORE_PATH = Path(os.getenv("QUANT_FEEDBACK_PATH", "./quant_feedback.json"))

# 各策略初始權重（相對分數，最終會歸一化到 0~1）
DEFAULT_STRATEGY_WEIGHTS: dict[str, float] = {
    "ma_cross":      1.0,
    "rsi":           1.0,
    "macd":          1.0,
    "kd":            1.0,
    "bollinger":     1.0,
    "pvd":           1.0,
    "institutional": 1.0,
    "momentum":      1.0,
    "mean_reversion":1.0,
    "defensive":     1.0,
}

# RuleBasedAlpha 子訊號維度的初始權重
DEFAULT_ALPHA_WEIGHTS: dict[str, float] = {
    "trend":    1.0,   # 趨勢（MA 排列）
    "momentum": 1.0,   # 動能（RSI + MACD）
    "volume":   1.0,   # 量能
    "chip":     1.0,   # 籌碼
}


# ── 資料類別 ─────────────────────────────────────────────────────────────────

@dataclass
class BacktestRecord:
    """單次回測結果快照（回饋學習輸入）"""
    record_id:     str
    stock_code:    str
    strategy:      str
    regime:        str              # bull / bear / sideways / volatile
    created_at:    str              # ISO datetime string

    # 績效指標
    total_return:  float
    annual_return: float
    sharpe_ratio:  float
    max_drawdown:  float
    win_rate:      float
    profit_factor: float
    n_trades:      int
    avg_holding_days: float

    # 成本
    cost_impact_pct: float

    # 策略參數（自由欄位）
    params: dict = field(default_factory=dict)


@dataclass
class StrategyStats:
    """單一策略的聚合統計"""
    strategy:      str
    regime:        str
    n_records:     int
    avg_sharpe:    float
    avg_win_rate:  float
    avg_return:    float
    avg_drawdown:  float
    current_weight: float
    recommendation: str   # "upgrade" / "downgrade" / "keep"


# ── 回饋引擎 ─────────────────────────────────────────────────────────────────

class FeedbackEngine:
    """
    回饋學習引擎：記錄回測結果並自動調整策略/特徵權重。

    使用方式：
        fe = FeedbackEngine()

        # 記錄一次回測
        fe.record_backtest(backtest_report, stock_code="2330",
                           strategy="ma_cross", regime="bull")

        # 取得當前策略權重
        weights = fe.get_strategy_weights()

        # 取得效能摘要
        stats = fe.performance_summary()

        # 自動調整（通常由排程觸發）
        updated = fe.auto_adjust()
    """

    def __init__(self, store_path: Path = DEFAULT_STORE_PATH):
        self.store_path = store_path
        self._records: list[BacktestRecord]      = []
        self._strategy_weights: dict[str, float] = dict(DEFAULT_STRATEGY_WEIGHTS)
        self._alpha_weights:    dict[str, float] = dict(DEFAULT_ALPHA_WEIGHTS)
        self._load()

    # ── 記錄 ──────────────────────────────────────────────────────────────

    def record_backtest(
        self,
        report,                        # BacktestReport instance（或 dict）
        stock_code: str,
        strategy:   str,
        regime:     str = "unknown",
    ) -> BacktestRecord:
        """
        記錄一次回測結果到回饋資料庫。
        report 接受 BacktestReport 物件或相容 dict。
        """
        if hasattr(report, "to_dict"):
            d = report.to_dict()
        elif isinstance(report, dict):
            d = report
        else:
            raise ValueError("report 必須是 BacktestReport 或 dict")

        rec = BacktestRecord(
            record_id=f"{strategy}_{stock_code}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            stock_code=stock_code,
            strategy=strategy,
            regime=regime,
            created_at=datetime.utcnow().isoformat(),
            total_return=float(d.get("total_return", 0)),
            annual_return=float(d.get("annual_return", 0)),
            sharpe_ratio=float(d.get("sharpe_ratio", 0)),
            max_drawdown=float(d.get("max_drawdown", 0)),
            win_rate=float(d.get("win_rate", 0)),
            profit_factor=float(d.get("profit_factor", 0)),
            n_trades=int(d.get("n_trades", 0)),
            avg_holding_days=float(d.get("avg_holding_days", 0)),
            cost_impact_pct=float(d.get("cost_impact_pct", 0)),
            params=d.get("params", {}),
        )
        self._records.append(rec)
        self._save()
        logger.info(
            f"[Feedback] 記錄回測: {strategy}/{stock_code}/{regime} "
            f"夏普={rec.sharpe_ratio:.2f} 勝率={rec.win_rate*100:.1f}%"
        )
        return rec

    # ── 查詢 ──────────────────────────────────────────────────────────────

    def get_strategy_weights(self) -> dict[str, float]:
        """取得當前策略權重（歸一化到 0~2 範圍）"""
        return dict(self._strategy_weights)

    def get_alpha_weights(self) -> dict[str, float]:
        """取得 RuleBasedAlpha 子維度權重"""
        return dict(self._alpha_weights)

    def get_records(
        self,
        strategy: Optional[str] = None,
        regime:   Optional[str] = None,
        last_n:   Optional[int] = None,
    ) -> list[BacktestRecord]:
        """篩選歷史記錄"""
        recs = self._records
        if strategy:
            recs = [r for r in recs if r.strategy == strategy]
        if regime:
            recs = [r for r in recs if r.regime == regime]
        if last_n:
            recs = recs[-last_n:]
        return recs

    def performance_summary(self) -> list[StrategyStats]:
        """各策略 × 盤態 聚合統計"""
        from collections import defaultdict
        grouped: dict[tuple, list[BacktestRecord]] = defaultdict(list)
        for r in self._records:
            grouped[(r.strategy, r.regime)].append(r)

        stats = []
        for (strategy, regime), recs in grouped.items():
            n = len(recs)
            # 指數衰退加權
            weights_decay = np.array([DECAY_ALPHA ** (n - 1 - i) for i in range(n)])
            weights_decay /= weights_decay.sum()

            sharpes  = np.array([r.sharpe_ratio for r in recs])
            win_rates= np.array([r.win_rate     for r in recs])
            returns  = np.array([r.total_return  for r in recs])
            drawdowns= np.array([r.max_drawdown  for r in recs])

            avg_sharpe   = float(np.dot(weights_decay, sharpes))
            avg_win_rate = float(np.dot(weights_decay, win_rates))
            avg_return   = float(np.dot(weights_decay, returns))
            avg_drawdown = float(np.dot(weights_decay, drawdowns))

            if avg_sharpe >= SHARPE_GOOD and avg_win_rate >= WIN_RATE_GOOD:
                rec_str = "upgrade"
            elif avg_sharpe <= SHARPE_BAD or avg_win_rate <= WIN_RATE_BAD:
                rec_str = "downgrade"
            else:
                rec_str = "keep"

            stats.append(StrategyStats(
                strategy=strategy,
                regime=regime,
                n_records=n,
                avg_sharpe=round(avg_sharpe, 3),
                avg_win_rate=round(avg_win_rate, 3),
                avg_return=round(avg_return, 4),
                avg_drawdown=round(avg_drawdown, 4),
                current_weight=round(self._strategy_weights.get(strategy, 1.0), 3),
                recommendation=rec_str,
            ))

        return sorted(stats, key=lambda s: -s.avg_sharpe)

    # ── 自動調整 ──────────────────────────────────────────────────────────

    def auto_adjust(self) -> dict[str, dict]:
        """
        自動調整策略權重，回傳調整記錄。
        通常由排程（每週日 22:00）觸發。

        回傳格式：
        {
          "strategy_weights": {"ma_cross": 1.1, "rsi": 0.9, ...},
          "alpha_weights":    {"trend": 1.0, ...},
          "changes": [{"strategy": ..., "old": ..., "new": ..., "reason": ...}]
        }
        """
        stats   = self.performance_summary()
        changes = []

        for s in stats:
            if s.n_records < MIN_RECORDS_ADJUST:
                continue   # 資料不足，不調整

            old_w = self._strategy_weights.get(s.strategy, 1.0)

            if s.recommendation == "upgrade":
                new_w = min(WEIGHT_MAX, old_w * WEIGHT_UP_FACTOR)
                reason = f"夏普{s.avg_sharpe:.2f}≥{SHARPE_GOOD} & 勝率{s.avg_win_rate*100:.1f}%≥{WIN_RATE_GOOD*100:.0f}%"
            elif s.recommendation == "downgrade":
                new_w = max(WEIGHT_MIN, old_w * WEIGHT_DN_FACTOR)
                reason = f"夏普{s.avg_sharpe:.2f}<{SHARPE_BAD} | 勝率{s.avg_win_rate*100:.1f}%<{WIN_RATE_BAD*100:.0f}%"
            else:
                continue   # keep → 不動

            if abs(new_w - old_w) > 0.001:
                self._strategy_weights[s.strategy] = round(new_w, 4)
                changes.append({
                    "strategy": s.strategy,
                    "regime":   s.regime,
                    "old":      round(old_w, 4),
                    "new":      round(new_w, 4),
                    "reason":   reason,
                })
                logger.info(f"[Feedback] {s.strategy}({s.regime}): {old_w:.3f} → {new_w:.3f} ({reason})")

        # 同步調整 alpha 子維度權重（基於所有策略表現的加總）
        self._adjust_alpha_weights()
        self._save()

        return {
            "strategy_weights": dict(self._strategy_weights),
            "alpha_weights":    dict(self._alpha_weights),
            "changes":          changes,
            "adjusted_at":      datetime.utcnow().isoformat(),
        }

    def _adjust_alpha_weights(self) -> None:
        """
        根據各維度貢獻調整 RuleBasedAlpha 子權重。

        趨勢相關策略（ma_cross/momentum）好 → trend 維度升權
        動能相關策略（rsi/macd）好         → momentum 維度升權
        量能相關策略（pvd/bollinger）好     → volume 維度升權
        籌碼相關策略（institutional）好    → chip 維度升權
        """
        dimension_map = {
            "trend":    ["ma_cross", "momentum"],
            "momentum": ["rsi", "macd", "kd"],
            "volume":   ["pvd", "bollinger"],
            "chip":     ["institutional"],
        }
        for dim, strategies in dimension_map.items():
            scores = [self._strategy_weights.get(s, 1.0) for s in strategies]
            if scores:
                avg_score = sum(scores) / len(scores)
                # 歸一化到原始權重附近（±0.1 調整）
                old = self._alpha_weights.get(dim, 1.0)
                new = max(WEIGHT_MIN, min(WEIGHT_MAX, avg_score))
                self._alpha_weights[dim] = round(new, 4)

    # ── 最佳策略推薦 ─────────────────────────────────────────────────────

    def recommend_strategy(self, regime: str) -> str:
        """根據當前盤態和歷史表現推薦最佳策略"""
        stats = [s for s in self.performance_summary() if s.regime == regime]
        if not stats:
            # 無歷史資料 → 依盤態給預設推薦
            defaults = {
                "bull":      "momentum",
                "bear":      "defensive",
                "sideways":  "mean_reversion",
                "volatile":  "defensive",
            }
            return defaults.get(regime, "ma_cross")
        # 依加權夏普值排序取最佳
        best = max(stats, key=lambda s: s.avg_sharpe * s.current_weight)
        return best.strategy

    # ── DB 非同步版本（供排程呼叫）──────────────────────────────────────

    async def auto_adjust_async(self) -> dict:
        """非同步版本，調整後將結果記錄到 backend DB"""
        result = self.auto_adjust()
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import FeatureWeight
            from sqlalchemy import select
            async with AsyncSessionLocal() as db:
                for strategy, weight in result["strategy_weights"].items():
                    row = await db.execute(
                        select(FeatureWeight).where(FeatureWeight.feature_name == strategy)
                    )
                    obj = row.scalar_one_or_none()
                    if obj:
                        obj.weight    = weight
                        obj.updated_at = datetime.utcnow()
                    else:
                        db.add(FeatureWeight(feature_name=strategy, weight=weight))
                await db.commit()
            logger.info(f"[Feedback] 權重已同步到 DB，共調整 {len(result['changes'])} 項")
        except Exception as e:
            logger.warning(f"[Feedback] DB 同步失敗（{e}），使用本地 JSON 儲存")
        return result

    async def save_backtest_async(
        self,
        report,
        stock_code: str,
        strategy:   str,
        regime:     str = "unknown",
    ) -> None:
        """非同步記錄回測結果（從 FastAPI endpoint 呼叫）"""
        rec = self.record_backtest(report, stock_code, strategy, regime)
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import BacktestSession
            import uuid
            async with AsyncSessionLocal() as db:
                d = report.to_dict() if hasattr(report, "to_dict") else report
                db.add(BacktestSession(
                    session_id=rec.record_id,
                    stock_code=stock_code,
                    strategy=strategy,
                    total_return=rec.total_return,
                    sharpe_ratio=rec.sharpe_ratio,
                    win_rate=rec.win_rate,
                    max_drawdown=rec.max_drawdown,
                    market_regime=regime,
                    cost_impact=rec.cost_impact_pct,
                ))
                await db.commit()
        except Exception as e:
            logger.debug(f"[Feedback] BacktestSession DB 寫入略過: {e}")

    # ── 持久化 ────────────────────────────────────────────────────────────

    def _save(self) -> None:
        """儲存到 JSON 檔案"""
        data = {
            "records":          [asdict(r) for r in self._records],
            "strategy_weights": self._strategy_weights,
            "alpha_weights":    self._alpha_weights,
            "saved_at":         datetime.utcnow().isoformat(),
        }
        try:
            self.store_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[Feedback] 儲存失敗: {e}")

    def _load(self) -> None:
        """從 JSON 檔案載入（若存在）"""
        if not self.store_path.exists():
            return
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            self._records = [BacktestRecord(**r) for r in data.get("records", [])]
            self._strategy_weights = {**DEFAULT_STRATEGY_WEIGHTS, **data.get("strategy_weights", {})}
            self._alpha_weights    = {**DEFAULT_ALPHA_WEIGHTS,    **data.get("alpha_weights", {})}
            logger.info(f"[Feedback] 載入 {len(self._records)} 筆歷史回測記錄")
        except Exception as e:
            logger.warning(f"[Feedback] 載入失敗，使用預設: {e}")

    def clear(self) -> None:
        """清空所有記錄（測試用）"""
        self._records            = []
        self._strategy_weights   = dict(DEFAULT_STRATEGY_WEIGHTS)
        self._alpha_weights      = dict(DEFAULT_ALPHA_WEIGHTS)
        if self.store_path.exists():
            self.store_path.unlink()


# ── 全域單例（供各模組共享）─────────────────────────────────────────────────

_global_feedback: Optional[FeedbackEngine] = None

def get_feedback_engine() -> FeedbackEngine:
    global _global_feedback
    if _global_feedback is None:
        _global_feedback = FeedbackEngine()
    return _global_feedback


# ── Mock 資料 + 獨立測試 ────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, tempfile
    from pathlib import Path

    # 使用暫存路徑避免污染正式資料
    tmp = Path(tempfile.mktemp(suffix=".json"))
    fe  = FeedbackEngine(store_path=tmp)

    print("=== 模擬 20 筆回測記錄 ===")
    strategies = ["ma_cross", "rsi", "macd", "momentum", "defensive"]
    regimes    = ["bull", "bull", "bear", "sideways"]
    rng = np.random.default_rng(42)

    for i in range(20):
        strat  = strategies[i % len(strategies)]
        regime = regimes[i % len(regimes)]
        # bull 市場下 momentum 表現好
        base_sharpe = 1.2 if (strat == "momentum" and regime == "bull") else 0.3
        mock_report = {
            "total_return":    float(rng.normal(0.08, 0.05)),
            "annual_return":   float(rng.normal(0.10, 0.06)),
            "sharpe_ratio":    float(rng.normal(base_sharpe, 0.3)),
            "max_drawdown":    float(abs(rng.normal(0.10, 0.04))),
            "win_rate":        float(np.clip(rng.normal(0.55 if strat == "momentum" else 0.45, 0.08), 0, 1)),
            "profit_factor":   float(abs(rng.normal(1.2, 0.4))),
            "n_trades":        int(rng.integers(5, 30)),
            "avg_holding_days": float(rng.uniform(3, 20)),
            "cost_impact_pct": float(rng.uniform(0.01, 0.04)),
            "params":          {},
        }
        fe.record_backtest(mock_report, stock_code="2330", strategy=strat, regime=regime)

    print(f"記錄總數: {len(fe.get_records())}")

    print("\n=== 效能摘要（按夏普排序）===")
    for s in fe.performance_summary():
        print(f"  {s.strategy:15s} [{s.regime:8s}] n={s.n_records:2d} "
              f"夏普={s.avg_sharpe:+.2f} 勝率={s.avg_win_rate*100:.1f}% "
              f"建議={s.recommendation}")

    print("\n=== 自動調整權重 ===")
    result = fe.auto_adjust()
    if result["changes"]:
        for c in result["changes"]:
            print(f"  {c['strategy']:15s} {c['old']:.3f} → {c['new']:.3f}  ({c['reason']})")
    else:
        print("  （記錄數不足，無調整）")

    print("\n=== 最佳策略推薦 ===")
    for regime in ["bull", "bear", "sideways"]:
        best = fe.recommend_strategy(regime)
        print(f"  {regime:8s} → {best}")

    print("\n=== Alpha 子維度權重 ===")
    for dim, w in fe.get_alpha_weights().items():
        print(f"  {dim:10s}: {w:.3f}")

    # 清理暫存檔
    tmp.unlink(missing_ok=True)
    print("\n測試完成")
