"""
alpha_model.py — Alpha 訊號產生器

架構：
  RuleBasedAlpha — 規則型 Alpha（不需訓練，可立即上線）
  AlphaModel     — LightGBM 機器學習模型（需訓練資料，選用）

RuleBasedAlpha 訊號邏輯：
  1. 趨勢訊號：MA5 > MA20 > MA60（多頭排列）
  2. 動能訊號：RSI 20~70 區間 + MACD 金叉
  3. 量能訊號：成交量放大 × 收盤站上均線
  4. 籌碼訊號（外部輸入）：外資連買天數
  綜合得分 0~100，> 60 為買進訊號，< 40 為賣出訊號

AlphaModel 使用 LightGBM Regressor 預測 5 日後報酬率，
  並以 0.02（+2%）為閾值產生買進訊號。
  LightGBM 為選用套件，未安裝時自動降級至 RuleBasedAlpha。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# LightGBM 為選用套件：未安裝時模型自動降級至規則型
try:
    import lightgbm as lgb
    _LGB_AVAILABLE = True
except ImportError:
    lgb = None  # type: ignore
    _LGB_AVAILABLE = False
    logger.warning("lightgbm 未安裝，AlphaModel 將降級至 RuleBasedAlpha")


class Signal(str, Enum):
    BUY  = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class AlphaOutput:
    """Alpha 模型輸出結果"""
    signal:    Signal
    score:     float          # 0~100，越高越偏多
    pred_ret:  Optional[float] = None   # 5日預測報酬率（LightGBM）
    reasons:   list[str]       = field(default_factory=list)
    features:  dict            = field(default_factory=dict)


# ── 規則型 Alpha ─────────────────────────────────────────────────────────────

class RuleBasedAlpha:
    """
    規則型 Alpha：基於技術指標打分（不需訓練，無過擬合風險）。

    使用方式：
        alpha = RuleBasedAlpha()
        result = alpha.evaluate(row)    # row = 單日特徵 Series（來自 FeatureEngine）
        results_df = alpha.batch_eval(feat_df)  # 批次計算整個 DataFrame
    """

    # 各子訊號權重（合計 100）
    WEIGHTS = {
        "trend":     35,   # 均線多頭排列
        "momentum":  30,   # RSI + MACD
        "volume":    20,   # 量能放大
        "chip":      15,   # 外部籌碼（可選）
    }

    def evaluate(
        self,
        row: pd.Series,
        chip_days: int = 0,       # 外資連買天數（外部輸入）
        foreign_net: float = 0.0, # 外資淨買（張）
    ) -> AlphaOutput:
        """評估單筆資料列，回傳 AlphaOutput"""
        reasons: list[str] = []
        scores:  dict[str, float] = {}

        # ── 1. 趨勢分 ──────────────────────────────────────────
        trend_score = 0.0
        ma5  = row.get("ma5",  np.nan)
        ma20 = row.get("ma20", np.nan)
        ma60 = row.get("ma60", np.nan)
        ma200= row.get("ma200", np.nan)
        close= row.get("close", np.nan)

        if not np.isnan(ma5) and not np.isnan(ma20):
            if ma5 > ma20:
                trend_score += 40
                reasons.append("MA5>MA20（短線偏多）")
        if not np.isnan(ma20) and not np.isnan(ma60):
            if ma20 > ma60:
                trend_score += 30
                reasons.append("MA20>MA60（中線偏多）")
        if not np.isnan(close) and not np.isnan(ma200):
            if close > ma200:
                trend_score += 30
                reasons.append("股價站上MA200（長線多頭）")
        scores["trend"] = trend_score  # 0~100

        # ── 2. 動能分 ──────────────────────────────────────────
        mom_score = 0.0
        rsi = row.get("rsi14", np.nan)
        macd_hist = row.get("macd_hist", np.nan)
        macd_golden = row.get("macd_golden", 0)

        if not np.isnan(rsi):
            if 40 <= rsi <= 70:
                mom_score += 40
                reasons.append(f"RSI={rsi:.1f}（健康多頭區間）")
            elif rsi < 30:
                mom_score += 30
                reasons.append(f"RSI={rsi:.1f}（超賣反彈機會）")
            elif rsi > 80:
                mom_score -= 20  # 過熱扣分
        if not np.isnan(macd_hist) and macd_hist > 0:
            mom_score += 30
            reasons.append("MACD Histogram 正值")
        if macd_golden:
            mom_score += 30
            reasons.append("MACD 金叉")
        scores["momentum"] = max(0.0, min(100.0, mom_score))

        # ── 3. 量能分 ──────────────────────────────────────────
        vol_score = 0.0
        vol_ratio = row.get("vol_ratio", np.nan)
        obv_slope = row.get("obv_slope5", np.nan)
        boll_b    = row.get("boll_b", np.nan)

        if not np.isnan(vol_ratio):
            if vol_ratio > 1.5:
                vol_score += 40
                reasons.append(f"放量({vol_ratio:.1f}x均量)")
            elif vol_ratio > 1.0:
                vol_score += 20
        if not np.isnan(obv_slope) and obv_slope > 0:
            vol_score += 30
            reasons.append("OBV 上升（量能增加）")
        if not np.isnan(boll_b):
            if 0.2 <= boll_b <= 0.8:
                vol_score += 30
                reasons.append("布林%B 健康區間")
            elif boll_b > 1.0:
                vol_score -= 20  # 超漲扣分
        scores["volume"] = max(0.0, min(100.0, vol_score))

        # ── 4. 籌碼分（外部輸入）──────────────────────────────
        chip_score = 0.0
        if chip_days >= 5:
            chip_score = 100
            reasons.append(f"外資連買{chip_days}日")
        elif chip_days >= 3:
            chip_score = 70
            reasons.append(f"外資連買{chip_days}日")
        elif chip_days >= 1:
            chip_score = 40
        elif chip_days <= -3:
            chip_score = -20
            reasons.append(f"外資連賣{abs(chip_days)}日（風險）")
        if foreign_net > 1000:
            chip_score = min(100, chip_score + 20)
        scores["chip"] = max(0.0, min(100.0, chip_score))

        # ── 綜合得分 ──────────────────────────────────────────
        total = sum(
            scores[k] * w / 100
            for k, w in self.WEIGHTS.items()
        )
        # total 範圍 0~100

        if total >= 60:
            sig = Signal.BUY
        elif total <= 40:
            sig = Signal.SELL
        else:
            sig = Signal.HOLD

        return AlphaOutput(
            signal=sig,
            score=round(total, 2),
            reasons=reasons,
            features=scores,
        )

    def batch_eval(self, feat_df: pd.DataFrame) -> pd.DataFrame:
        """批次評估整個 DataFrame，新增 signal / alpha_score 欄位"""
        results = [self.evaluate(row) for _, row in feat_df.iterrows()]
        feat_df = feat_df.copy()
        feat_df["alpha_signal"] = [r.signal.value for r in results]
        feat_df["alpha_score"]  = [r.score for r in results]
        return feat_df


# ── LightGBM Alpha 模型 ───────────────────────────────────────────────────────

# 預設訓練特徵（需與 FeatureEngine.feature_columns 對應）
DEFAULT_FEATURES = [
    "ma5", "ma20", "ma60", "ma200",
    "ema12", "ema26",
    "rsi14",
    "macd", "macd_signal", "macd_hist",
    "k", "d", "j",
    "boll_b", "boll_width",
    "atr14",
    "vol_ratio", "obv_slope5",
    "ret_1d", "ret_5d", "ret_10d", "ret_20d",
    "excess_ret",
    "body_ratio", "hl_ratio",
]

BUY_THRESHOLD  =  0.02   # 預測 5 日報酬 > 2% → 買進
SELL_THRESHOLD = -0.01   # 預測 5 日報酬 < -1% → 賣出


class AlphaModel:
    """
    LightGBM 回歸模型：預測 5 日後報酬率。

    訓練流程：
        model = AlphaModel()
        model.train(feat_df)   # feat_df 來自 FeatureEngine.compute_all()
        model.save("model.lgb")

    預測流程：
        model = AlphaModel()
        model.load("model.lgb")
        result = model.predict(row)

    未安裝 LightGBM 時，predict / batch_predict 自動降級至 RuleBasedAlpha。
    """

    def __init__(
        self,
        features: list[str] = DEFAULT_FEATURES,
        target_days: int = 5,
        params: Optional[dict] = None,
    ):
        self.features     = features
        self.target_days  = target_days
        self.model_       = None   # 訓練後的 lgb.Booster
        self._rule_alpha  = RuleBasedAlpha()
        self.params = params or {
            "objective":       "regression",
            "metric":          "rmse",
            "num_leaves":      63,
            "learning_rate":   0.05,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq":    5,
            "min_child_samples": 20,
            "n_estimators":    500,
            "verbose":         -1,
        }

    # ── 訓練 ──────────────────────────────────────────────────────────────

    def train(
        self,
        feat_df: pd.DataFrame,
        val_size: float = 0.2,
        early_stopping_rounds: int = 50,
    ) -> dict:
        """
        訓練 LightGBM 模型。

        feat_df 必須已包含 FeatureEngine 產生的特徵欄位。
        目標變數（y）= target_days 日後的報酬率，由函式內部自動計算。
        回傳訓練摘要 dict（RMSE / 特徵重要度）。
        """
        if not _LGB_AVAILABLE:
            raise RuntimeError("lightgbm 未安裝，無法訓練模型。請執行 pip install lightgbm")

        df = feat_df.copy()
        df["_target"] = df["close"].pct_change(self.target_days).shift(-self.target_days)
        df = df.dropna(subset=self.features + ["_target"])

        X = df[self.features].values
        y = df["_target"].values

        # 按時間切分（不能隨機 shuffle，避免前視偏差）
        split = int(len(X) * (1 - val_size))
        X_tr, X_val = X[:split], X[split:]
        y_tr, y_val = y[:split], y[split:]

        dtrain = lgb.Dataset(X_tr, label=y_tr, feature_name=self.features)
        dval   = lgb.Dataset(X_val, label=y_val, feature_name=self.features, reference=dtrain)

        callbacks = [lgb.early_stopping(early_stopping_rounds, verbose=False)]
        self.model_ = lgb.train(
            self.params,
            dtrain,
            num_boost_round=self.params.get("n_estimators", 500),
            valid_sets=[dval],
            callbacks=callbacks,
        )

        # 驗證集 RMSE
        preds = self.model_.predict(X_val)
        rmse  = float(np.sqrt(np.mean((preds - y_val) ** 2)))

        importance = dict(zip(
            self.features,
            self.model_.feature_importance(importance_type="gain").tolist(),
        ))
        top5 = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:5]
        logger.info(f"[AlphaModel] 訓練完成 RMSE={rmse:.4f}, 最重要特徵: {top5}")
        return {"rmse": rmse, "feature_importance": importance, "n_train": split, "n_val": len(y_val)}

    # ── 儲存 / 載入 ───────────────────────────────────────────────────────

    def save(self, path: str | Path) -> None:
        """儲存模型至檔案（.lgb 格式）"""
        if self.model_ is None:
            raise RuntimeError("模型尚未訓練")
        self.model_.save_model(str(path))
        meta_path = str(path) + ".meta.json"
        with open(meta_path, "w") as f:
            json.dump({"features": self.features, "target_days": self.target_days}, f)
        logger.info(f"[AlphaModel] 模型已儲存: {path}")

    def load(self, path: str | Path) -> None:
        """載入已儲存的模型"""
        if not _LGB_AVAILABLE:
            logger.warning("lightgbm 未安裝，load() 無作用")
            return
        self.model_ = lgb.Booster(model_file=str(path))
        meta_path = str(path) + ".meta.json"
        if Path(meta_path).exists():
            with open(meta_path) as f:
                meta = json.load(f)
            self.features    = meta.get("features", self.features)
            self.target_days = meta.get("target_days", self.target_days)
        logger.info(f"[AlphaModel] 模型已載入: {path}")

    # ── 預測 ──────────────────────────────────────────────────────────────

    def predict(self, row: pd.Series, chip_days: int = 0) -> AlphaOutput:
        """
        預測單日訊號。
        若 LightGBM 不可用或未訓練，自動降級至 RuleBasedAlpha。
        """
        if not _LGB_AVAILABLE or self.model_ is None:
            return self._rule_alpha.evaluate(row, chip_days=chip_days)

        # 補齊缺失特徵（填 0，避免預測失敗）
        x = np.array([row.get(f, 0.0) for f in self.features], dtype=float)
        x = np.nan_to_num(x, nan=0.0)
        pred_ret = float(self.model_.predict(x.reshape(1, -1))[0])

        if pred_ret >= BUY_THRESHOLD:
            sig = Signal.BUY
        elif pred_ret <= SELL_THRESHOLD:
            sig = Signal.SELL
        else:
            sig = Signal.HOLD

        # 同時計算規則型得分（提供可解釋性）
        rule_out = self._rule_alpha.evaluate(row, chip_days=chip_days)

        return AlphaOutput(
            signal=sig,
            score=rule_out.score,
            pred_ret=round(pred_ret, 4),
            reasons=rule_out.reasons,
            features=rule_out.features,
        )

    def batch_predict(self, feat_df: pd.DataFrame) -> pd.DataFrame:
        """批次預測，新增 lgb_signal / lgb_pred_ret / alpha_score 欄位"""
        out_df = feat_df.copy()
        results = [self.predict(row) for _, row in feat_df.iterrows()]
        out_df["lgb_signal"]   = [r.signal.value for r in results]
        out_df["lgb_pred_ret"] = [r.pred_ret for r in results]
        out_df["alpha_score"]  = [r.score for r in results]
        return out_df


# ── Mock 資料 + 獨立測試 ────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from quant.feature_engine import FeatureEngine, _generate_mock_ohlcv

    mock_df = _generate_mock_ohlcv(300)
    fe = FeatureEngine(mock_df)
    feat_df = fe.compute_all()

    print("=== RuleBasedAlpha 批次評估 ===")
    alpha = RuleBasedAlpha()
    result_df = alpha.batch_eval(feat_df)
    signal_counts = result_df["alpha_signal"].value_counts()
    print(signal_counts)
    last = result_df.iloc[-1]
    out = alpha.evaluate(last, chip_days=3)
    print(f"\n最新訊號: {out.signal.value}  評分: {out.score}")
    print(f"理由: {', '.join(out.reasons)}")

    print("\n=== AlphaModel（LightGBM）===")
    model = AlphaModel()
    if _LGB_AVAILABLE:
        summary = model.train(feat_df)
        print(f"訓練完成 RMSE={summary['rmse']:.4f}")
        last_out = model.predict(feat_df.iloc[-1])
        print(f"最新預測: 訊號={last_out.signal.value}, 預測報酬={last_out.pred_ret}")
    else:
        print("LightGBM 未安裝，改用 RuleBasedAlpha 降級輸出：")
        last_out = model.predict(feat_df.iloc[-1])
        print(f"訊號={last_out.signal.value}, 評分={last_out.score}")
