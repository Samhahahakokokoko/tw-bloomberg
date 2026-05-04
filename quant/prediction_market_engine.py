"""
prediction_market_engine.py — 預測市場引擎

模擬預測市場機制：
  - 每個「命題」有 YES/NO 機率
  - 每次訊號觸發更新機率（Bayesian update）
  - 記錄預測準確率，反饋到信心校準

命題範例：
  "2330 在 30 日內上漲 10% 以上" — 目前機率 68%
  "台股月底前收在 22000 點以上"  — 目前機率 55%
  "3661 下季 EPS 超預期"          — 目前機率 72%

儲存在 quant_signals.db 的 predictions 表（若有）
否則使用 in-memory 狀態
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "quant_signals.db")


@dataclass
class Prediction:
    id:           str              # 唯一 ID
    proposition:  str              # 命題文字
    stock_id:     Optional[str]
    stock_name:   Optional[str]
    yes_prob:     float            # 0-1
    created_at:   str
    deadline:     str              # 到期日
    resolved:     Optional[bool] = None   # None=未結算, True/False=結算結果
    resolved_at:  Optional[str] = None
    signal_count: int = 0          # 累積信號次數
    tags:         list[str] = field(default_factory=list)

    @property
    def no_prob(self) -> float:
        return 1.0 - self.yes_prob

    @property
    def days_left(self) -> int:
        try:
            deadline_dt = datetime.fromisoformat(self.deadline)
            return max(0, (deadline_dt - datetime.now()).days)
        except Exception:
            return 0

    @property
    def confidence_bar(self) -> str:
        filled = int(self.yes_prob * 10)
        return "█" * filled + "░" * (10 - filled)

    def bayesian_update(self, likelihood_yes: float, likelihood_no: float):
        """Bayesian 更新機率"""
        prior_yes = self.yes_prob
        prior_no  = self.no_prob
        posterior_yes = prior_yes * likelihood_yes
        posterior_no  = prior_no  * likelihood_no
        total = posterior_yes + posterior_no
        if total > 0:
            self.yes_prob = max(0.05, min(0.95, posterior_yes / total))
        self.signal_count += 1

    def to_line_text(self) -> str:
        icon = "✅" if self.resolved is True else "❌" if self.resolved is False else "🔮"
        lines = [
            f"{icon} {self.proposition}",
            f"YES {self.yes_prob:.0%} [{self.confidence_bar}] NO {self.no_prob:.0%}",
            f"剩餘：{self.days_left} 天｜更新次數：{self.signal_count}",
        ]
        if self.resolved is not None:
            result = "✓ 命中" if self.resolved else "✗ 未命中"
            lines.append(f"結算：{result}")
        return "\n".join(lines)


@dataclass
class PredictionMarketSnapshot:
    predictions:  list[Prediction] = field(default_factory=list)
    accuracy_30d: float = 0.0      # 近30日準確率
    total_active: int = 0
    total_resolved: int = 0
    ts:           str = field(default_factory=lambda: datetime.now().isoformat())

    def to_line_text(self) -> str:
        lines = [
            f"🔮 預測市場快照",
            f"活躍命題：{self.total_active}  已結算：{self.total_resolved}",
            f"近30日準確率：{self.accuracy_30d:.0%}",
            "",
        ]
        for p in sorted(self.predictions, key=lambda x: -x.yes_prob)[:5]:
            lines.append(p.to_line_text())
            lines.append("")
        return "\n".join(lines).strip()


def _init_db():
    """初始化 SQLite predictions 表"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id TEXT PRIMARY KEY,
                proposition TEXT,
                stock_id TEXT,
                stock_name TEXT,
                yes_prob REAL DEFAULT 0.5,
                created_at TEXT,
                deadline TEXT,
                resolved INTEGER,
                resolved_at TEXT,
                signal_count INTEGER DEFAULT 0,
                tags TEXT DEFAULT '[]'
            )
        """)
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.warning("[prediction] db init failed: %s", e)
        return False


def _load_predictions() -> list[Prediction]:
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id,proposition,stock_id,stock_name,yes_prob,created_at,deadline,"
            "resolved,resolved_at,signal_count,tags FROM predictions"
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            result.append(Prediction(
                id           = r[0],
                proposition  = r[1],
                stock_id     = r[2],
                stock_name   = r[3],
                yes_prob     = r[4],
                created_at   = r[5],
                deadline     = r[6],
                resolved     = None if r[7] is None else bool(r[7]),
                resolved_at  = r[8],
                signal_count = r[9],
                tags         = json.loads(r[10] or "[]"),
            ))
        return result
    except Exception as e:
        logger.warning("[prediction] load failed: %s", e)
        return _default_predictions()


def _save_prediction(p: Prediction):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT OR REPLACE INTO predictions
            (id,proposition,stock_id,stock_name,yes_prob,created_at,deadline,
             resolved,resolved_at,signal_count,tags)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            p.id, p.proposition, p.stock_id, p.stock_name, p.yes_prob,
            p.created_at, p.deadline,
            None if p.resolved is None else int(p.resolved),
            p.resolved_at, p.signal_count, json.dumps(p.tags, ensure_ascii=False)
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[prediction] save failed: %s", e)


def _default_predictions() -> list[Prediction]:
    now = datetime.now()
    return [
        Prediction(
            id="pred_2330_10pct_30d", proposition="2330 台積電 30日內上漲10%",
            stock_id="2330", stock_name="台積電",
            yes_prob=0.62, created_at=now.isoformat(),
            deadline=(now + timedelta(days=30)).strftime("%Y-%m-%d"),
            tags=["semiconductor", "ai"],
        ),
        Prediction(
            id="pred_taiex_22000", proposition="台股月底前收在22000點以上",
            stock_id=None, stock_name=None,
            yes_prob=0.55, created_at=now.isoformat(),
            deadline=(now + timedelta(days=25)).strftime("%Y-%m-%d"),
            tags=["index", "market"],
        ),
        Prediction(
            id="pred_3661_eps_beat", proposition="3661 世芯-KY 下季EPS超預期",
            stock_id="3661", stock_name="世芯-KY",
            yes_prob=0.71, created_at=now.isoformat(),
            deadline=(now + timedelta(days=60)).strftime("%Y-%m-%d"),
            tags=["semiconductor", "earnings"],
        ),
        Prediction(
            id="pred_ai_server_q3", proposition="AI伺服器族群Q3營收年增>50%",
            stock_id=None, stock_name=None,
            yes_prob=0.68, created_at=now.isoformat(),
            deadline=(now + timedelta(days=90)).strftime("%Y-%m-%d"),
            tags=["ai", "server"],
        ),
    ]


async def get_snapshot() -> PredictionMarketSnapshot:
    """取得目前預測市場快照"""
    _init_db()
    predictions = _load_predictions()

    active   = [p for p in predictions if p.resolved is None and p.days_left > 0]
    resolved = [p for p in predictions if p.resolved is not None]

    hits = sum(1 for p in resolved if p.resolved is True)
    accuracy = hits / len(resolved) if resolved else 0.0

    return PredictionMarketSnapshot(
        predictions   = active[:8],
        accuracy_30d  = accuracy,
        total_active  = len(active),
        total_resolved = len(resolved),
    )


async def update_prediction_signal(pred_id: str, bullish_signal: bool):
    """根據新信號 Bayesian 更新特定命題的機率"""
    _init_db()
    preds = _load_predictions()
    for p in preds:
        if p.id == pred_id:
            if bullish_signal:
                p.bayesian_update(likelihood_yes=0.7, likelihood_no=0.3)
            else:
                p.bayesian_update(likelihood_yes=0.35, likelihood_no=0.65)
            _save_prediction(p)
            logger.info("[prediction] updated %s → yes=%.2f", pred_id, p.yes_prob)
            return


async def add_prediction(
    proposition: str,
    deadline_days: int = 30,
    stock_id: Optional[str] = None,
    stock_name: Optional[str] = None,
    initial_prob: float = 0.5,
    tags: list[str] | None = None,
) -> Prediction:
    """新增預測命題"""
    _init_db()
    import hashlib
    pid = hashlib.md5(f"{proposition}{datetime.now().isoformat()}".encode()).hexdigest()[:12]
    now = datetime.now()
    p = Prediction(
        id           = f"pred_{pid}",
        proposition  = proposition,
        stock_id     = stock_id,
        stock_name   = stock_name,
        yes_prob     = initial_prob,
        created_at   = now.isoformat(),
        deadline     = (now + timedelta(days=deadline_days)).strftime("%Y-%m-%d"),
        tags         = tags or [],
    )
    _save_prediction(p)
    return p


async def resolve_prediction(pred_id: str, outcome: bool):
    """結算預測命題"""
    _init_db()
    preds = _load_predictions()
    for p in preds:
        if p.id == pred_id:
            p.resolved    = outcome
            p.resolved_at = datetime.now().isoformat()
            _save_prediction(p)
            logger.info("[prediction] resolved %s → %s", pred_id, outcome)
            return
