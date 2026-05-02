"""
adaptive_weight_engine.py — 30日 IC 動態因子加權引擎

規則：
  1. 取最近 30 日每個因子的 Spearman IC（與未來 5 日報酬的相關係數）
  2. weight_raw = IC 均值（30日）
  3. IC < 0 的因子：weight = 0（反向因子不使用）
  4. 歸一化：weight = weight_raw / sum(all positive weights)
  5. 每日自動更新，優先存入 PostgreSQL，否則存 JSON 快取

輸出：{factor_name: weight}  可直接傳入 EnsembleEngine / DynamicWeightEngine
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

logger = logging.getLogger(__name__)

# ── 預設因子列表 ──────────────────────────────────────────────────────────────

DEFAULT_FACTORS = [
    "rsi14", "macd_hist", "vol_ratio", "boll_b", "obv_slope5",
    "ret_1d", "ret_5d", "ret_10d", "ret_20d",
    "atr14", "excess_ret", "body_ratio", "hl_ratio", "boll_width",
]

IC_WINDOW    = 30   # 滾動 IC 計算窗口（天）
FORWARD_DAYS = 5    # 預測未來 N 日報酬

_CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "quant_factor_weights.json")


@dataclass
class FactorWeightResult:
    weights:     dict[str, float]   # 最終權重（IC<0 已剔除，已歸一化）
    raw_ic:      dict[str, float]   # 原始 IC 均值（含負值）
    valid_count: int                # 有效因子數（IC > 0）
    total_count: int
    updated_at:  str

    def to_dict(self) -> dict:
        return {
            "weights":     {k: round(v, 6) for k, v in self.weights.items()},
            "raw_ic":      {k: round(v, 6) for k, v in self.raw_ic.items()},
            "valid_count": self.valid_count,
            "total_count": self.total_count,
            "updated_at":  self.updated_at,
            "top5": sorted(
                [{"factor": k, "weight": round(v, 6), "ic": round(self.raw_ic.get(k, 0), 4)}
                 for k, v in self.weights.items()],
                key=lambda x: x["weight"], reverse=True,
            )[:5],
        }


class AdaptiveWeightEngine:
    """
    30日 IC 動態因子加權引擎。

    使用方式（每日更新）：
        engine = AdaptiveWeightEngine()
        result = engine.compute(feat_df)
        await engine.save(result)       # DB 或 JSON
        weights = result.weights        # {factor: weight}

    無 DB 時：
        result = engine.compute(feat_df)
        engine.save_json(result)        # 同步 JSON 存檔
        weights = result.weights
    """

    def __init__(
        self,
        factors:      Optional[list[str]] = None,
        ic_window:    int = IC_WINDOW,
        forward_days: int = FORWARD_DAYS,
        cache_path:   str = _CACHE_PATH,
    ):
        self.factors      = factors or DEFAULT_FACTORS
        self.ic_window    = ic_window
        self.forward_days = forward_days
        self.cache_path   = cache_path
        self._last_result: Optional[FactorWeightResult] = None

    def compute(self, feat_df: pd.DataFrame) -> FactorWeightResult:
        """
        計算最近 ic_window 日各因子 IC，並依規則歸一化為權重。

        feat_df 須包含 close 欄位（計算遠期報酬）及各因子欄位。
        """
        n = len(feat_df)
        min_required = self.ic_window + self.forward_days + 5

        if n < min_required:
            logger.warning("[AdaptiveWeight] 資料不足（%d < %d），使用等權", n, min_required)
            return self._equal_weight()

        # 遠期報酬（前移 forward_days，不造成 lookahead bias）
        fwd_ret = feat_df["close"].pct_change(self.forward_days).shift(-self.forward_days)

        # 取最近一段計算 IC
        window_len = self.ic_window + self.forward_days
        w_df  = feat_df.tail(window_len).reset_index(drop=True)
        w_fwd = fwd_ret.tail(window_len).reset_index(drop=True)

        raw_ic: dict[str, float] = {}

        for factor in self.factors:
            if factor not in w_df.columns:
                continue
            f_vals = pd.to_numeric(w_df[factor], errors="coerce")
            paired = pd.DataFrame({"f": f_vals, "r": w_fwd}).dropna()

            if len(paired) < 10:
                continue
            try:
                corr, _ = scipy_stats.spearmanr(paired["f"], paired["r"])
                if not np.isnan(corr):
                    raw_ic[factor] = float(corr)
            except Exception:
                pass

        if not raw_ic:
            return self._equal_weight()

        # 剔除 IC < 0（反向因子 weight = 0）
        positive_ic = {k: v for k, v in raw_ic.items() if v > 0}

        if not positive_ic:
            logger.warning("[AdaptiveWeight] 所有因子 IC ≤ 0，使用等權（降級）")
            return self._equal_weight(list(raw_ic.keys()), raw_ic)

        # 歸一化：weight = IC / sum(positive IC)
        total = sum(positive_ic.values())
        weights = {k: v / total for k, v in positive_ic.items()}

        result = FactorWeightResult(
            weights=weights,
            raw_ic=raw_ic,
            valid_count=len(positive_ic),
            total_count=len(raw_ic),
            updated_at=datetime.utcnow().isoformat()[:19],
        )
        self._last_result = result
        return result

    def _equal_weight(
        self,
        factors: Optional[list[str]] = None,
        raw_ic:  Optional[dict]      = None,
    ) -> FactorWeightResult:
        fs = factors or self.factors
        w  = 1.0 / len(fs) if fs else 0.0
        return FactorWeightResult(
            weights={f: w for f in fs},
            raw_ic=raw_ic or {},
            valid_count=0,
            total_count=len(fs),
            updated_at=datetime.utcnow().isoformat()[:19],
        )

    def save_json(self, result: FactorWeightResult) -> None:
        """同步存入本地 JSON（無 DB 時的後備）"""
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[AdaptiveWeight] JSON 儲存失敗: %s", e)

    async def save(self, result: FactorWeightResult) -> None:
        """
        非同步存入 PostgreSQL；若 DB 不可用則降級至 JSON。
        DB 須有 factor_weights 表（factor TEXT PRIMARY KEY, weight FLOAT, ic_value FLOAT, updated_at TEXT）。
        """
        saved_db = False
        try:
            from backend.models.database import AsyncSessionLocal
            from sqlalchemy import text
            rows = [
                {
                    "factor":     f,
                    "weight":     result.weights.get(f, 0.0),
                    "ic_value":   result.raw_ic.get(f, 0.0),
                    "updated_at": result.updated_at,
                }
                for f in set(list(result.weights) + list(result.raw_ic))
            ]
            async with AsyncSessionLocal() as db:
                for row in rows:
                    await db.execute(text("""
                        INSERT INTO factor_weights (factor, weight, ic_value, updated_at)
                        VALUES (:factor, :weight, :ic_value, :updated_at)
                        ON CONFLICT (factor) DO UPDATE
                          SET weight = EXCLUDED.weight,
                              ic_value = EXCLUDED.ic_value,
                              updated_at = EXCLUDED.updated_at
                    """), row)
                await db.commit()
            saved_db = True
        except Exception as e:
            logger.debug("[AdaptiveWeight] DB 略過（%s），改用 JSON", e)

        if not saved_db:
            self.save_json(result)

    def load_json(self) -> Optional[FactorWeightResult]:
        """從 JSON 快取載入"""
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return FactorWeightResult(
                weights=d.get("weights", {}),
                raw_ic=d.get("raw_ic", {}),
                valid_count=d.get("valid_count", 0),
                total_count=d.get("total_count", 0),
                updated_at=d.get("updated_at", ""),
            )
        except Exception:
            return None

    @property
    def last_result(self) -> Optional[FactorWeightResult]:
        return self._last_result


_global_aw: Optional[AdaptiveWeightEngine] = None


def get_adaptive_weight_engine() -> AdaptiveWeightEngine:
    global _global_aw
    if _global_aw is None:
        _global_aw = AdaptiveWeightEngine()
    return _global_aw


# ── Mock data + 獨立測試 ───────────────────────────────────────────────────

def _gen_mock_feat(n: int = 200, seed: int = 42) -> pd.DataFrame:
    rng   = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    close = 100.0 * np.cumprod(1 + rng.normal(0.0003, 0.012, n))
    ret5  = pd.Series(close).pct_change(5).fillna(0).values
    df    = pd.DataFrame({"date": dates, "close": close})
    for factor in DEFAULT_FACTORS:
        noise         = rng.normal(0, 1, n)
        signal_weight = rng.uniform(-0.5, 2.0)
        df[factor]    = noise + ret5 * signal_weight
    return df


if __name__ == "__main__":
    feat_df = _gen_mock_feat(200)
    engine  = AdaptiveWeightEngine()
    result  = engine.compute(feat_df)

    print("=== 30日 IC 動態加權 ===")
    print(f"有效因子: {result.valid_count}/{result.total_count}")
    print(f"更新時間: {result.updated_at}")
    print("\n前5大因子：")
    for item in result.to_dict()["top5"]:
        print(f"  {item['factor']:20s}  weight={item['weight']:.4f}  IC={item['ic']:+.4f}")
    print("\nIC < 0（已剔除）：")
    for k, v in sorted(result.raw_ic.items(), key=lambda x: x[1]):
        if v <= 0:
            print(f"  {k:20s}  IC={v:+.4f}")
