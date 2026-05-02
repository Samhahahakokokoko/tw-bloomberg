"""
alpha_registry.py — Alpha 因子狀態管理引擎

狀態機：
  ACTIVE  → IC 正常（IC_30d_mean >= 0）
  PAUSED  → IC 轉負但未達 DEAD 門檻（dead_days < DEAD_THRESHOLD）
  DEAD    → IC < 0 連續 DEAD_THRESHOLD 天 → 停用、weight = 0

自動權重計算：
  weight = IC_30d_mean / sum(all positive IC)
  IC < 0 的 alpha → weight = 0（不貢獻組合分數）

儲存：優先 PostgreSQL alpha_registry 表；失敗降級 JSON 快取
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

DEAD_THRESHOLD  = 10    # 連續 N 天 IC < 0 → DEAD
IC_WINDOW       = 30    # 計算 IC 均值的窗口（天）
_CACHE_PATH     = os.path.join(os.path.dirname(__file__), "..", "alpha_registry_cache.json")

# 預設 alpha 清單（啟動時自動初始化）
DEFAULT_ALPHAS = [
    "momentum", "value", "chip", "breakout", "defensive",
    "rsi_reversion", "macd_trend", "volume_surge", "foreign_buy",
]


class AlphaStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DEAD   = "DEAD"


@dataclass
class AlphaRecord:
    alpha_name:  str
    status:      AlphaStatus = AlphaStatus.ACTIVE
    ic_current:  float       = 0.0
    ic_30d_mean: float       = 0.0
    ic_history:  list[float] = field(default_factory=list)   # last 30 days
    weight:      float       = 1.0
    dead_days:   int         = 0
    notes:       str         = ""

    def to_dict(self) -> dict:
        return {
            "alpha_name":  self.alpha_name,
            "status":      self.status.value,
            "ic_current":  round(self.ic_current, 6),
            "ic_30d_mean": round(self.ic_30d_mean, 6),
            "ic_history":  [round(v, 6) for v in self.ic_history[-30:]],
            "weight":      round(self.weight, 6),
            "dead_days":   self.dead_days,
            "notes":       self.notes,
        }


class AlphaRegistry:
    """
    Alpha 因子狀態管理。

    使用方式：
        registry = AlphaRegistry()
        await registry.load()

        # 每日收盤後更新 IC
        registry.update_ic("momentum", ic_value=0.032)
        registry.update_ic("rsi_reversion", ic_value=-0.015)

        # 取得有效 alpha 與權重
        weights = registry.get_weights()          # {alpha: weight}
        active  = registry.get_active_alphas()    # list[AlphaRecord]

        await registry.save()
    """

    def __init__(self, dead_threshold: int = DEAD_THRESHOLD):
        self._dead_threshold = dead_threshold
        self._alphas: dict[str, AlphaRecord] = {}

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def register(self, alpha_name: str, notes: str = "") -> AlphaRecord:
        """登記新 alpha（已存在則忽略）"""
        if alpha_name not in self._alphas:
            self._alphas[alpha_name] = AlphaRecord(alpha_name=alpha_name, notes=notes)
        return self._alphas[alpha_name]

    def update_ic(self, alpha_name: str, ic_value: float) -> AlphaRecord:
        """
        更新指定 alpha 的當日 IC，觸發狀態機轉換。
        """
        if alpha_name not in self._alphas:
            self.register(alpha_name)

        rec = self._alphas[alpha_name]
        rec.ic_current = ic_value

        # 更新 IC 歷史（保留最近 30 天）
        rec.ic_history.append(ic_value)
        if len(rec.ic_history) > IC_WINDOW:
            rec.ic_history = rec.ic_history[-IC_WINDOW:]

        # 計算30日均值
        if rec.ic_history:
            rec.ic_30d_mean = sum(rec.ic_history) / len(rec.ic_history)

        # 連續負 IC 計數
        if ic_value < 0:
            rec.dead_days += 1
        else:
            rec.dead_days = 0   # 一旦 IC 轉正，重設計數

        # 狀態機轉換
        if rec.dead_days >= self._dead_threshold:
            rec.status = AlphaStatus.DEAD
            rec.weight = 0.0
        elif ic_value < 0 or rec.ic_30d_mean < 0:
            if rec.status != AlphaStatus.DEAD:
                rec.status = AlphaStatus.PAUSED
        else:
            rec.status = AlphaStatus.ACTIVE

        return rec

    def revive(self, alpha_name: str) -> bool:
        """手動復活 DEAD alpha（重設狀態為 ACTIVE）"""
        rec = self._alphas.get(alpha_name)
        if not rec:
            return False
        rec.status    = AlphaStatus.ACTIVE
        rec.dead_days = 0
        rec.ic_history.clear()
        logger.info("[AlphaRegistry] %s revived", alpha_name)
        return True

    def pause(self, alpha_name: str) -> bool:
        rec = self._alphas.get(alpha_name)
        if not rec:
            return False
        rec.status = AlphaStatus.PAUSED
        return True

    # ── 查詢 ─────────────────────────────────────────────────────────────────

    def get_active_alphas(self) -> list[AlphaRecord]:
        return [r for r in self._alphas.values() if r.status == AlphaStatus.ACTIVE]

    def get_all(self) -> list[AlphaRecord]:
        return list(self._alphas.values())

    def get(self, alpha_name: str) -> Optional[AlphaRecord]:
        return self._alphas.get(alpha_name)

    def get_weights(self) -> dict[str, float]:
        """
        回傳歸一化權重 dict（IC < 0 的 alpha weight = 0）。
        weight = IC_30d_mean / sum(positive IC_30d_mean)
        """
        positives = {
            name: rec.ic_30d_mean
            for name, rec in self._alphas.items()
            if rec.status == AlphaStatus.ACTIVE and rec.ic_30d_mean > 0
        }
        total = sum(positives.values())
        if total <= 0:
            # fallback: 等權
            n = len([r for r in self._alphas.values() if r.status == AlphaStatus.ACTIVE])
            return {name: 1.0 / n if n > 0 else 0.0
                    for name, rec in self._alphas.items()
                    if rec.status == AlphaStatus.ACTIVE}

        weights = {name: v / total for name, v in positives.items()}

        # PAUSED / DEAD → weight = 0
        for name, rec in self._alphas.items():
            if name not in weights:
                weights[name] = 0.0

        # 同步寫回每條記錄
        for name, w in weights.items():
            if name in self._alphas:
                self._alphas[name].weight = round(w, 6)

        return weights

    def summary(self) -> dict:
        all_r = self.get_all()
        return {
            "total":    len(all_r),
            "active":   sum(1 for r in all_r if r.status == AlphaStatus.ACTIVE),
            "paused":   sum(1 for r in all_r if r.status == AlphaStatus.PAUSED),
            "dead":     sum(1 for r in all_r if r.status == AlphaStatus.DEAD),
            "weights":  {r.alpha_name: round(r.weight, 4) for r in all_r},
        }

    # ── 持久化 ────────────────────────────────────────────────────────────────

    async def save(self) -> None:
        """優先存 PostgreSQL；失敗降級 JSON"""
        saved_db = False
        try:
            from backend.models.database import AsyncSessionLocal
            from sqlalchemy import text
            async with AsyncSessionLocal() as db:
                for rec in self._alphas.values():
                    await db.execute(text("""
                        INSERT INTO alpha_registry
                          (alpha_name, status, ic_current, ic_30d_mean,
                           ic_history, weight, dead_days, notes, updated_at)
                        VALUES
                          (:name, :status, :ic_cur, :ic_mean,
                           :ic_hist, :weight, :dead, :notes, :now)
                        ON CONFLICT (alpha_name) DO UPDATE SET
                          status      = EXCLUDED.status,
                          ic_current  = EXCLUDED.ic_current,
                          ic_30d_mean = EXCLUDED.ic_30d_mean,
                          ic_history  = EXCLUDED.ic_history,
                          weight      = EXCLUDED.weight,
                          dead_days   = EXCLUDED.dead_days,
                          notes       = EXCLUDED.notes,
                          updated_at  = EXCLUDED.updated_at
                    """), {
                        "name":    rec.alpha_name,
                        "status":  rec.status.value,
                        "ic_cur":  rec.ic_current,
                        "ic_mean": rec.ic_30d_mean,
                        "ic_hist": json.dumps(rec.ic_history[-30:]),
                        "weight":  rec.weight,
                        "dead":    rec.dead_days,
                        "notes":   rec.notes,
                        "now":     datetime.utcnow().isoformat(),
                    })
                await db.commit()
            saved_db = True
        except Exception as e:
            logger.debug("[AlphaRegistry] DB save failed (%s), fallback JSON", e)

        if not saved_db:
            self._save_json()

    async def load(self) -> None:
        """從 DB 載入；失敗降級 JSON；再失敗則初始化預設"""
        loaded = False
        try:
            from backend.models.database import AsyncSessionLocal
            from sqlalchemy import text
            async with AsyncSessionLocal() as db:
                r = await db.execute(text("SELECT * FROM alpha_registry"))
                rows = r.fetchall()
            for row in rows:
                # row columns: id, alpha_name, status, ic_current, ic_30d_mean,
                #              ic_history, weight, dead_days, notes, created_at, updated_at
                name = row[1]
                rec  = AlphaRecord(
                    alpha_name=name,
                    status=AlphaStatus(row[2]),
                    ic_current=float(row[3] or 0),
                    ic_30d_mean=float(row[4] or 0),
                    ic_history=json.loads(row[5] or "[]"),
                    weight=float(row[6] or 1.0),
                    dead_days=int(row[7] or 0),
                    notes=row[8] or "",
                )
                self._alphas[name] = rec
            loaded = True
            logger.info("[AlphaRegistry] Loaded %d alphas from DB", len(rows))
        except Exception as e:
            logger.debug("[AlphaRegistry] DB load failed (%s), trying JSON", e)

        if not loaded:
            cache = self._load_json()
            if cache:
                self._alphas = cache
                loaded = True

        if not loaded:
            self._init_defaults()

    def _init_defaults(self) -> None:
        for name in DEFAULT_ALPHAS:
            self.register(name)
        logger.info("[AlphaRegistry] Initialized %d default alphas", len(DEFAULT_ALPHAS))

    def _save_json(self) -> None:
        try:
            data = {name: rec.to_dict() for name, rec in self._alphas.items()}
            with open(_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("[AlphaRegistry] JSON save failed: %s", e)

    def _load_json(self) -> Optional[dict[str, AlphaRecord]]:
        try:
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            result: dict[str, AlphaRecord] = {}
            for name, d in data.items():
                result[name] = AlphaRecord(
                    alpha_name=name,
                    status=AlphaStatus(d.get("status", "ACTIVE")),
                    ic_current=d.get("ic_current", 0.0),
                    ic_30d_mean=d.get("ic_30d_mean", 0.0),
                    ic_history=d.get("ic_history", []),
                    weight=d.get("weight", 1.0),
                    dead_days=d.get("dead_days", 0),
                    notes=d.get("notes", ""),
                )
            return result
        except Exception:
            return None


_global_registry: Optional[AlphaRegistry] = None


def get_alpha_registry() -> AlphaRegistry:
    global _global_registry
    if _global_registry is None:
        _global_registry = AlphaRegistry()
    return _global_registry


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    async def _test():
        reg = AlphaRegistry()
        reg._init_defaults()

        # 模擬10天 IC 更新
        import random; random.seed(42)
        print("=== 模擬10天 IC 更新 ===")
        for day in range(10):
            for alpha in DEFAULT_ALPHAS[:5]:
                ic = random.gauss(0.02, 0.05)
                rec = reg.update_ic(alpha, ic)
            # momentum 連續10天負 → DEAD
            rec = reg.update_ic("momentum", -0.05)

        print("\n=== Alpha 狀態 ===")
        for r in reg.get_all():
            print(f"  {r.alpha_name:20s} {r.status.value:8s} "
                  f"IC30d={r.ic_30d_mean:+.4f}  dead={r.dead_days}  w={r.weight:.4f}")

        print(f"\n=== 摘要 ===")
        s = reg.summary()
        print(f"  total={s['total']} active={s['active']} paused={s['paused']} dead={s['dead']}")

        print(f"\n=== 歸一化權重 ===")
        for name, w in reg.get_weights().items():
            if w > 0:
                print(f"  {name:20s} {w:.4f}")

    asyncio.run(_test())
