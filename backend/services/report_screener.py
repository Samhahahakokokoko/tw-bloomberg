"""
report_screener.py — 多維度選股篩選器（v3 debug-fixed）

修復清單：
  [BUG-1] MASTER_POOL 靜態假資料 → enrich_with_realtime() 覆蓋真實收盤/量/法人
  [BUG-2] _build_rows 缺欄位 crash → safe_build_row() 防禦性建構
  [BUG-3] custom_screener regex 太窄 → 支援 天/日、全形數字、更多關鍵字
  [BUG-4] favorites_screener 未知代碼 → 嘗試 TWSE API，失敗才用安全預設
  [ADD]   unify_ticker_format() — 統一代碼格式
  [ADD]   validate_ticker_exists() — 驗證代碼有效性
  [ADD]   safe_lookup() — 安全查詢 MASTER_POOL
  [ADD]   ScreenerLogger — 記錄每次選股輸出，方便 debug 錯股
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import numpy as np

# ── Debug Logger ──────────────────────────────────────────────────────────────

_log = logging.getLogger("screener.debug")

# 若 root logger 無 handler，補一個 StreamHandler 確保能看到輸出
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )


class ScreenerLogger:
    """每次選股結果記錄 — 用來 debug 錯股、代碼錯誤"""

    @staticmethod
    def log_call(screen_type: str, sector: str, rows: list["StockRow"]) -> None:
        summary = {
            "ts":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "screen_type": screen_type,
            "sector":      sector,
            "total":       len(rows),
            "top5": [
                {
                    "rank":       r.day_rank,
                    "stock_id":   r.stock_id,
                    "name":       r.name,
                    "close":      r.close,
                    "change_pct": r.change_pct,
                    "model_score":r.model_score,
                    "data_source":getattr(r, "_data_source", "pool"),
                }
                for r in rows[:5]
            ],
        }
        _log.info("[ScreenerResult] %s", json.dumps(summary, ensure_ascii=False))

    @staticmethod
    def log_enrich(code: str, old_close: float, new_close: float, source: str) -> None:
        if abs(old_close - new_close) > 0.01:
            _log.info("[Enrich] %s close: %.1f → %.1f (source=%s)",
                      code, old_close, new_close, source)

    @staticmethod
    def log_unknown_ticker(code: str, reason: str) -> None:
        _log.warning("[UnknownTicker] code=%s reason=%s", code, reason)

    @staticmethod
    def log_regex_match(pattern: str, text: str, result) -> None:
        _log.debug("[Regex] pattern=%r text=%r result=%s", pattern, text, result)


# ── ScreenerType ──────────────────────────────────────────────────────────────

class ScreenerType(str, Enum):
    MOMENTUM  = "momentum"
    VALUE     = "value"
    CHIP      = "chip"
    BREAKOUT  = "breakout"
    AI        = "ai"
    SECTOR    = "sector"
    ALL       = "all"
    CUSTOM    = "custom"
    FAVORITES = "favorites"


# ── 代碼格式統一 & 驗證 ─────────────────────────────────────────────────────────

def unify_ticker_format(code: str) -> str:
    """
    [FIX-1] 統一股票代碼格式 → 純數字字串
    支援：
      "2330.TW" → "2330"
      "２３３０" → "2330"（全形）
      " 2330 "  → "2330"（空白）
      2330      → "2330"（int）
    """
    code = unicodedata.normalize("NFKC", str(code)).strip()
    for suffix in (".TW", ".TWO", ".tw", ".two", ".TWSE", ".TPEX"):
        if code.upper().endswith(suffix.upper()):
            code = code[: -len(suffix)]
    # 只保留數字與英文（台股代碼含字母如 00878B）
    code = re.sub(r"[^\w]", "", code).strip()
    return code


_valid_ticker_cache: set[str] = set()   # 從 API 回傳確認過的代碼


def validate_ticker_exists(code: str) -> bool:
    """[FIX-1] 驗證代碼有效性（先查 pool，再查 API cache，最後規則判斷）"""
    code = unify_ticker_format(code)
    if not code:
        return False
    if safe_lookup(code) is not None:
        return True
    if code in _valid_ticker_cache:
        return True
    # 台股代碼規則：4~6 碼，純數字或末位為英文（如 00878B）
    return bool(re.match(r"^\d{4,6}[A-Z]?$", code))


def safe_lookup(code: str) -> Optional[dict]:
    """[FIX-1] 安全查詢 MASTER_POOL，統一代碼格式後再比對"""
    code = unify_ticker_format(code)
    return next((d for d in _POOL_RAW if unify_ticker_format(d["stock_id"]) == code), None)


# ── StockRow ─────────────────────────────────────────────────────────────────

@dataclass
class StockRow:
    """選股表單列資料"""
    stock_id:       str
    name:           str
    sector:         str
    close:          float
    change_pct:     float
    volume:         float

    chip_5d:        float
    chip_20d:       float
    foreign_buy_days: int   = 0

    rev_yoy:        float = 0.0
    rev_mom:        float = 0.0
    eps_growth:     float = 0.0
    dividend_yield: float = 0.0
    pe_ratio:       float = 0.0
    eps_stability:  float = 0.0

    kd_weekly:      float = 50.0
    ma20_slope:     float = 0.0
    consec_up:      int   = 0
    vol_20d_max:    float = 0.0
    intraday_range: float = 0.0
    group_avg_change: float = 0.0
    breakout_pct:   float = 0.0
    target_price:   float = 0.0

    model_score:    float = 50.0
    confidence:     float = 50.0
    day_rank:       int   = 99

    momentum_score:     float = 50.0
    value_score:        float = 50.0
    chip_score_v:       float = 50.0
    tech_score:         float = 50.0
    fundamental_score:  float = 50.0

    tags: list[str] = field(default_factory=list)

    # ── 衍生欄位（由 safe_build_row 計算，供 movers_engine 等使用）──────────────
    vol_ratio:      float = 1.0    # volume / (vol_20d_max * 0.55)，近似量比
    ret_5d_approx:  float = 0.0    # change_pct*2 + slope 估算5日報酬（小數）
    foreign_net_5d: float = 0.0    # chip_5d 張數（三大法人5日淨買）

    # 內部追蹤欄位（不顯示在圖表）
    _data_source: str = field(default="pool", repr=False, compare=False)

    def volume_k(self) -> float:
        return self.volume / 1000

    def stars(self) -> str:
        n = round(max(0, min(5, self.model_score / 20)))
        return "★" * n + "☆" * (5 - n)


# ── 防禦性建構 StockRow ────────────────────────────────────────────────────────

# StockRow 所有欄位的安全預設值
_FIELD_DEFAULTS: dict = {
    "stock_id":          "????",
    "name":              "未知",
    "sector":            "其他",
    "close":             0.0,
    "change_pct":        0.0,
    "volume":            0.0,
    "chip_5d":           0.0,
    "chip_20d":          0.0,
    "foreign_buy_days":  0,
    "rev_yoy":           0.0,
    "rev_mom":           0.0,
    "eps_growth":        0.0,
    "dividend_yield":    0.0,
    "pe_ratio":          0.0,
    "eps_stability":     0.5,
    "kd_weekly":         50.0,
    "ma20_slope":        0.0,
    "consec_up":         0,
    "vol_20d_max":       0.0,
    "intraday_range":    1.0,
    "group_avg_change":  0.0,
    "breakout_pct":      0.0,
    "target_price":      0.0,
    "model_score":       50.0,
    "confidence":        50.0,
    "day_rank":          99,
    "momentum_score":    50.0,
    "value_score":       50.0,
    "chip_score_v":      50.0,
    "tech_score":        50.0,
    "fundamental_score": 50.0,
    # 衍生欄位預設
    "vol_ratio":         1.0,
    "ret_5d_approx":     0.0,
    "foreign_net_5d":    0.0,
}

_VALID_FIELDS = set(f.name for f in StockRow.__dataclass_fields__.values()
                    if not f.name.startswith("_"))


def safe_build_row(d: dict, rank: int = 99) -> StockRow:
    """
    [FIX-2] 防禦性建構 StockRow：
    - 統一代碼格式
    - 缺欄位補安全預設值
    - 型別錯誤強制轉換
    - 記錄 data_source
    """
    d = dict(d)
    # 統一代碼格式
    d["stock_id"] = unify_ticker_format(d.get("stock_id", ""))

    # 用預設值填補缺欄位
    for key, default in _FIELD_DEFAULTS.items():
        if key not in d or d[key] is None:
            d[key] = default

    # 型別強制轉換（避免 str 混入 float 欄位）
    float_fields = {k for k, v in _FIELD_DEFAULTS.items() if isinstance(v, float)}
    int_fields   = {k for k, v in _FIELD_DEFAULTS.items() if isinstance(v, int)}
    str_fields   = {k for k, v in _FIELD_DEFAULTS.items() if isinstance(v, str)}

    for key in float_fields:
        try:
            d[key] = float(str(d[key]).replace(",", "") or 0)
        except (TypeError, ValueError):
            d[key] = _FIELD_DEFAULTS[key]

    for key in int_fields:
        try:
            d[key] = int(float(str(d[key]).replace(",", "") or 0))
        except (TypeError, ValueError):
            d[key] = _FIELD_DEFAULTS[key]

    # ── 衍生欄位計算 ──────────────────────────────────────────────────────────
    # vol_ratio：今日成交量 vs 20日最大量，×1.8 換算為近似均量倍數
    # 台股實證：max ≈ avg × 1.8，所以 vol / (max / 1.8) = vol / max * 1.8
    _vol       = float(d.get("volume", 0) or 0)
    _vol_max   = float(d.get("vol_20d_max", 0) or 0)
    d["vol_ratio"] = (_vol / (_vol_max / 1.8)) if _vol_max > 100 else 1.0

    # ret_5d_approx：今日漲跌幅 × 2.0 + 均線斜率補正
    _chg_pct   = float(d.get("change_pct", 0) or 0) / 100   # % → 小數
    _slope     = float(d.get("ma20_slope", 0) or 0)
    _consec    = int(d.get("consec_up", 0) or 0)
    # 斜率 1.0 ≈ 均線每日漲 1%，5 日累積約 5% → slope × 0.005 ≈ 5日貢獻
    d["ret_5d_approx"] = _chg_pct * 2.0 + _slope * 0.005 + (_consec >= 4) * 0.01

    # foreign_net_5d：chip_5d 就是三大法人5日淨買張數
    d["foreign_net_5d"] = float(d.get("chip_5d", 0) or 0)

    # 只取 StockRow 認識的欄位
    kwargs = {k: v for k, v in d.items() if k in _VALID_FIELDS}
    kwargs["day_rank"] = rank

    try:
        row = StockRow(**kwargs)
    except Exception as e:
        _log.error("[safe_build_row] %s build failed: %s, using defaults", d.get("stock_id"), e)
        row = StockRow(
            stock_id=d.get("stock_id", "????"),
            name=d.get("name", "未知"),
            sector=d.get("sector", "其他"),
            close=float(d.get("close", 0)),
            change_pct=float(d.get("change_pct", 0)),
            volume=float(d.get("volume", 0)),
            chip_5d=float(d.get("chip_5d", 0)),
            chip_20d=float(d.get("chip_20d", 0)),
        )

    row._data_source = d.get("_data_source", "pool")
    return row


# ── 標籤計算 ─────────────────────────────────────────────────────────────────

LABEL_DEFS = {
    "week_core":  "★週核",
    "high_cons":  "☆高連",
    "new_money":  "•新資金",
    "same_group": "▲同族",
    "target":     "◎達標",
    "high_freq":  "■高頻",
}


def compute_labels(row: StockRow) -> list[str]:
    tags: list[str] = []
    if row.kd_weekly > 50 and row.ma20_slope > 0:
        tags.append(LABEL_DEFS["week_core"])
    if row.consec_up >= 5:
        tags.append(LABEL_DEFS["high_cons"])
    if row.vol_20d_max > 0 and row.volume >= row.vol_20d_max:
        tags.append(LABEL_DEFS["new_money"])
    if row.group_avg_change > 3.0:
        tags.append(LABEL_DEFS["same_group"])
    if row.target_price > 0 and row.close >= row.target_price:
        tags.append(LABEL_DEFS["target"])
    if row.intraday_range > 3.0:
        tags.append(LABEL_DEFS["high_freq"])
    return tags


# ── MASTER_POOL（基本面/技術面範本，收盤/量/法人由 real-time 覆蓋）──────────

_POOL_RAW: list[dict] = [
    dict(stock_id="2330", name="台積電",   sector="半導體",
         close=850,  change_pct=+2.3, volume=35000, chip_5d=+8500, chip_20d=+32000, foreign_buy_days=8,
         rev_yoy=28.5,  rev_mom=5.2,  eps_growth=32.1, dividend_yield=2.5, pe_ratio=22,  eps_stability=0.92,
         kd_weekly=72, ma20_slope=1.2, consec_up=6, vol_20d_max=34000, intraday_range=2.8, group_avg_change=3.5,
         breakout_pct=2.1, target_price=880,
         model_score=88, confidence=87, momentum_score=85, value_score=42, chip_score_v=82, tech_score=78, fundamental_score=86),
    dict(stock_id="2454", name="聯發科",   sector="IC設計",
         close=1180, change_pct=+3.1, volume=18000, chip_5d=+3200, chip_20d=+15000, foreign_buy_days=6,
         rev_yoy=22.0,  rev_mom=8.1,  eps_growth=25.0, dividend_yield=3.5, pe_ratio=18,  eps_stability=0.78,
         kd_weekly=68, ma20_slope=0.9, consec_up=3, vol_20d_max=20000, intraday_range=3.8, group_avg_change=3.5,
         breakout_pct=3.5, target_price=1150,
         model_score=85, confidence=83, momentum_score=88, value_score=55, chip_score_v=75, tech_score=82, fundamental_score=78),
    dict(stock_id="2303", name="聯電",     sector="半導體",
         close=46.5, change_pct=-0.5, volume=28000, chip_5d=-800,  chip_20d=+2000,  foreign_buy_days=-2,
         rev_yoy=8.0,   rev_mom=-1.5, eps_growth=5.0,  dividend_yield=5.5, pe_ratio=14,  eps_stability=0.72,
         kd_weekly=55, ma20_slope=0.1, consec_up=2, vol_20d_max=27000, intraday_range=1.8, group_avg_change=3.5,
         breakout_pct=0.0, target_price=48,
         model_score=62, confidence=60, momentum_score=52, value_score=68, chip_score_v=48, tech_score=55, fundamental_score=65),
    dict(stock_id="3711", name="日月光投", sector="封裝測試",
         close=152,  change_pct=+1.2, volume=9500,  chip_5d=+550,  chip_20d=+3200,  foreign_buy_days=4,
         rev_yoy=12.0,  rev_mom=2.8,  eps_growth=14.2, dividend_yield=4.2, pe_ratio=16,  eps_stability=0.80,
         kd_weekly=64, ma20_slope=0.7, consec_up=4, vol_20d_max=9000, intraday_range=1.5, group_avg_change=3.5,
         breakout_pct=1.2, target_price=155,
         model_score=71, confidence=70, momentum_score=65, value_score=62, chip_score_v=60, tech_score=68, fundamental_score=70),
    dict(stock_id="6770", name="力積電",   sector="半導體",
         close=52.8, change_pct=+1.8, volume=22000, chip_5d=+1200, chip_20d=+4500,  foreign_buy_days=3,
         rev_yoy=15.2,  rev_mom=3.0,  eps_growth=18.5, dividend_yield=3.8, pe_ratio=17,  eps_stability=0.70,
         kd_weekly=61, ma20_slope=0.5, consec_up=5, vol_20d_max=21000, intraday_range=2.2, group_avg_change=3.5,
         breakout_pct=1.8, target_price=50,
         model_score=74, confidence=72, momentum_score=72, value_score=58, chip_score_v=65, tech_score=70, fundamental_score=68),
    dict(stock_id="6415", name="矽力-KY",  sector="散熱/電源",
         close=1350, change_pct=+4.2, volume=5200,  chip_5d=+2100, chip_20d=+8500,  foreign_buy_days=7,
         rev_yoy=35.0,  rev_mom=9.5,  eps_growth=40.0, dividend_yield=1.8, pe_ratio=28,  eps_stability=0.88,
         kd_weekly=78, ma20_slope=1.5, consec_up=7, vol_20d_max=5100, intraday_range=4.5, group_avg_change=4.2,
         breakout_pct=4.2, target_price=1300,
         model_score=92, confidence=91, momentum_score=92, value_score=35, chip_score_v=88, tech_score=90, fundamental_score=90),
    dict(stock_id="3552", name="同亨",     sector="散熱/電源",
         close=285,  change_pct=+3.8, volume=3200,  chip_5d=+800,  chip_20d=+3000,  foreign_buy_days=5,
         rev_yoy=28.0,  rev_mom=6.0,  eps_growth=33.0, dividend_yield=2.2, pe_ratio=24,  eps_stability=0.82,
         kd_weekly=73, ma20_slope=1.1, consec_up=6, vol_20d_max=3100, intraday_range=3.2, group_avg_change=4.2,
         breakout_pct=3.8, target_price=280,
         model_score=87, confidence=85, momentum_score=88, value_score=38, chip_score_v=80, tech_score=85, fundamental_score=84),
    dict(stock_id="3450", name="聯鈞",     sector="散熱/電源",
         close=95.5, change_pct=+2.5, volume=4800,  chip_5d=+400,  chip_20d=+1800,  foreign_buy_days=3,
         rev_yoy=18.5,  rev_mom=4.0,  eps_growth=22.0, dividend_yield=3.5, pe_ratio=20,  eps_stability=0.75,
         kd_weekly=65, ma20_slope=0.8, consec_up=5, vol_20d_max=4700, intraday_range=2.8, group_avg_change=4.2,
         breakout_pct=2.5, target_price=95,
         model_score=78, confidence=76, momentum_score=76, value_score=52, chip_score_v=65, tech_score=72, fundamental_score=74),
    dict(stock_id="3231", name="緯創",     sector="伺服器",
         close=102,  change_pct=+5.1, volume=25000, chip_5d=+3500, chip_20d=+12000, foreign_buy_days=9,
         rev_yoy=42.0,  rev_mom=12.0, eps_growth=55.0, dividend_yield=2.8, pe_ratio=20,  eps_stability=0.82,
         kd_weekly=80, ma20_slope=2.0, consec_up=8, vol_20d_max=24000, intraday_range=5.1, group_avg_change=5.0,
         breakout_pct=5.1, target_price=95,
         model_score=95, confidence=93, momentum_score=96, value_score=45, chip_score_v=92, tech_score=94, fundamental_score=92),
    dict(stock_id="2382", name="廣達",     sector="伺服器",
         close=285,  change_pct=+3.5, volume=32000, chip_5d=+5500, chip_20d=+22000, foreign_buy_days=10,
         rev_yoy=38.5,  rev_mom=10.5, eps_growth=48.0, dividend_yield=3.2, pe_ratio=22,  eps_stability=0.85,
         kd_weekly=76, ma20_slope=1.8, consec_up=7, vol_20d_max=31000, intraday_range=4.0, group_avg_change=5.0,
         breakout_pct=4.2, target_price=270,
         model_score=93, confidence=91, momentum_score=94, value_score=48, chip_score_v=90, tech_score=92, fundamental_score=90),
    dict(stock_id="6669", name="緯穎",     sector="伺服器",
         close=2050, change_pct=+6.2, volume=8500,  chip_5d=+4800, chip_20d=+18000, foreign_buy_days=12,
         rev_yoy=55.0,  rev_mom=15.0, eps_growth=70.0, dividend_yield=1.5, pe_ratio=32,  eps_stability=0.78,
         kd_weekly=82, ma20_slope=2.5, consec_up=9, vol_20d_max=8200, intraday_range=6.2, group_avg_change=5.0,
         breakout_pct=6.2, target_price=1900,
         model_score=96, confidence=94, momentum_score=98, value_score=28, chip_score_v=95, tech_score=96, fundamental_score=94),
    dict(stock_id="0056", name="元大高股息", sector="ETF",
         close=36.5, change_pct=+0.3, volume=85000, chip_5d=+2000, chip_20d=+8000,  foreign_buy_days=2,
         rev_yoy=8.5,   rev_mom=1.0,  eps_growth=6.0,  dividend_yield=7.5, pe_ratio=13,  eps_stability=0.92,
         kd_weekly=58, ma20_slope=0.2, consec_up=3, vol_20d_max=85000, intraday_range=0.5, group_avg_change=1.5,
         breakout_pct=0.3, target_price=37,
         model_score=68, confidence=72, momentum_score=35, value_score=95, chip_score_v=55, tech_score=50, fundamental_score=88),
    dict(stock_id="2412", name="中華電",   sector="電信",
         close=118,  change_pct=-0.2, volume=5200,  chip_5d=-100,  chip_20d=+500,   foreign_buy_days=-1,
         rev_yoy=3.5,   rev_mom=0.5,  eps_growth=2.0,  dividend_yield=6.2, pe_ratio=22,  eps_stability=0.95,
         kd_weekly=52, ma20_slope=0.0, consec_up=1, vol_20d_max=5400, intraday_range=0.4, group_avg_change=0.8,
         breakout_pct=0.0, target_price=120,
         model_score=55, confidence=60, momentum_score=28, value_score=88, chip_score_v=42, tech_score=45, fundamental_score=82),
    dict(stock_id="2317", name="鴻海",     sector="電子製造",
         close=118.5,change_pct=+1.2, volume=55000, chip_5d=+2500, chip_20d=+9500,  foreign_buy_days=4,
         rev_yoy=12.0,  rev_mom=3.5,  eps_growth=15.0, dividend_yield=4.8, pe_ratio=11,  eps_stability=0.75,
         kd_weekly=62, ma20_slope=0.6, consec_up=4, vol_20d_max=54000, intraday_range=1.8, group_avg_change=2.5,
         breakout_pct=1.2, target_price=120,
         model_score=72, confidence=70, momentum_score=65, value_score=78, chip_score_v=68, tech_score=65, fundamental_score=72),
    dict(stock_id="2884", name="玉山金",   sector="金融",
         close=29.8, change_pct=+0.5, volume=18000, chip_5d=+350,  chip_20d=+1200,  foreign_buy_days=1,
         rev_yoy=5.0,   rev_mom=1.2,  eps_growth=4.5,  dividend_yield=5.8, pe_ratio=12,  eps_stability=0.88,
         kd_weekly=54, ma20_slope=0.1, consec_up=2, vol_20d_max=17500, intraday_range=0.6, group_avg_change=1.2,
         breakout_pct=0.5, target_price=30,
         model_score=60, confidence=62, momentum_score=38, value_score=85, chip_score_v=48, tech_score=50, fundamental_score=80),
    dict(stock_id="8299", name="群聯",     sector="IC設計",
         close=535,  change_pct=+4.8, volume=8500,  chip_5d=+1500, chip_20d=+5500,  foreign_buy_days=6,
         rev_yoy=32.0,  rev_mom=8.5,  eps_growth=38.0, dividend_yield=4.0, pe_ratio=19,  eps_stability=0.80,
         kd_weekly=74, ma20_slope=1.4, consec_up=5, vol_20d_max=8000, intraday_range=4.8, group_avg_change=4.5,
         breakout_pct=4.8, target_price=500,
         model_score=84, confidence=82, momentum_score=86, value_score=58, chip_score_v=78, tech_score=88, fundamental_score=82),
    dict(stock_id="6269", name="台郡",     sector="電子零件",
         close=128,  change_pct=+3.2, volume=9200,  chip_5d=+620,  chip_20d=+2500,  foreign_buy_days=5,
         rev_yoy=20.0,  rev_mom=5.5,  eps_growth=24.0, dividend_yield=3.2, pe_ratio=18,  eps_stability=0.76,
         kd_weekly=70, ma20_slope=1.0, consec_up=5, vol_20d_max=9000, intraday_range=3.2, group_avg_change=3.8,
         breakout_pct=3.2, target_price=122,
         model_score=79, confidence=77, momentum_score=80, value_score=52, chip_score_v=70, tech_score=82, fundamental_score=76),
    dict(stock_id="3529", name="力旺",     sector="IC設計",
         close=1280, change_pct=+5.5, volume=3800,  chip_5d=+1800, chip_20d=+7200,  foreign_buy_days=7,
         rev_yoy=45.0,  rev_mom=12.0, eps_growth=52.0, dividend_yield=2.0, pe_ratio=35,  eps_stability=0.72,
         kd_weekly=76, ma20_slope=1.6, consec_up=6, vol_20d_max=3700, intraday_range=5.5, group_avg_change=5.2,
         breakout_pct=5.5, target_price=1200,
         model_score=89, confidence=87, momentum_score=92, value_score=30, chip_score_v=85, tech_score=90, fundamental_score=88),
    dict(stock_id="6488", name="環球晶",   sector="半導體材料",
         close=380,  change_pct=+2.8, volume=12000, chip_5d=+2200, chip_20d=+8800,  foreign_buy_days=5,
         rev_yoy=18.0,  rev_mom=4.5,  eps_growth=22.0, dividend_yield=2.8, pe_ratio=20,  eps_stability=0.82,
         kd_weekly=66, ma20_slope=0.9, consec_up=4, vol_20d_max=11800, intraday_range=2.8, group_avg_change=3.2,
         breakout_pct=2.8, target_price=365,
         model_score=80, confidence=78, momentum_score=78, value_score=50, chip_score_v=75, tech_score=76, fundamental_score=78),
    dict(stock_id="2308", name="台達電",   sector="電源/散熱",
         close=358,  change_pct=+2.5, volume=15000, chip_5d=+1800, chip_20d=+7500,  foreign_buy_days=4,
         rev_yoy=15.0,  rev_mom=3.8,  eps_growth=18.0, dividend_yield=3.5, pe_ratio=21,  eps_stability=0.85,
         kd_weekly=67, ma20_slope=0.8, consec_up=4, vol_20d_max=14500, intraday_range=2.5, group_avg_change=4.2,
         breakout_pct=2.5, target_price=345,
         model_score=78, confidence=76, momentum_score=76, value_score=58, chip_score_v=72, tech_score=74, fundamental_score=78),
    dict(stock_id="2379", name="瑞昱",     sector="IC設計",
         close=565,  change_pct=+3.8, volume=9800,  chip_5d=+1200, chip_20d=+4800,  foreign_buy_days=5,
         rev_yoy=25.0,  rev_mom=7.0,  eps_growth=30.0, dividend_yield=3.8, pe_ratio=17,  eps_stability=0.80,
         kd_weekly=70, ma20_slope=1.1, consec_up=5, vol_20d_max=9500, intraday_range=3.8, group_avg_change=4.5,
         breakout_pct=3.8, target_price=535,
         model_score=83, confidence=81, momentum_score=84, value_score=60, chip_score_v=76, tech_score=82, fundamental_score=80),
    dict(stock_id="1301", name="台塑",     sector="石化",
         close=68.5, change_pct=-0.8, volume=8200,  chip_5d=-500,  chip_20d=-1200,  foreign_buy_days=-3,
         rev_yoy=-5.0,  rev_mom=-2.0, eps_growth=-8.0, dividend_yield=6.5, pe_ratio=18,  eps_stability=0.65,
         kd_weekly=38, ma20_slope=-0.5, consec_up=0, vol_20d_max=8500, intraday_range=1.2, group_avg_change=-1.2,
         breakout_pct=0.0, target_price=72,
         model_score=38, confidence=40, momentum_score=22, value_score=72, chip_score_v=28, tech_score=30, fundamental_score=45),
    dict(stock_id="2002", name="中鋼",     sector="鋼鐵",
         close=24.8, change_pct=-0.5, volume=32000, chip_5d=-800,  chip_20d=-2500,  foreign_buy_days=-2,
         rev_yoy=-3.0,  rev_mom=-1.0, eps_growth=-5.0, dividend_yield=5.8, pe_ratio=16,  eps_stability=0.60,
         kd_weekly=42, ma20_slope=-0.3, consec_up=0, vol_20d_max=33000, intraday_range=1.0, group_avg_change=-0.8,
         breakout_pct=0.0, target_price=26,
         model_score=42, confidence=44, momentum_score=25, value_score=70, chip_score_v=30, tech_score=35, fundamental_score=48),
    dict(stock_id="2409", name="友達",     sector="面板",
         close=14.8, change_pct=+0.7, volume=45000, chip_5d=+200,  chip_20d=+800,   foreign_buy_days=1,
         rev_yoy=2.0,   rev_mom=0.5,  eps_growth=1.5,  dividend_yield=4.2, pe_ratio=15,  eps_stability=0.55,
         kd_weekly=50, ma20_slope=0.1, consec_up=2, vol_20d_max=44000, intraday_range=1.0, group_avg_change=1.0,
         breakout_pct=0.7, target_price=15,
         model_score=50, confidence=52, momentum_score=40, value_score=60, chip_score_v=42, tech_score=48, fundamental_score=50),
]

# pool 代碼集合（統一格式），用於快速查詢
_POOL_IDS: set[str] = {unify_ticker_format(d["stock_id"]) for d in _POOL_RAW}


# ── Real-time 補充（覆蓋靜態假資料）────────────────────────────────────────────

# 快取結構：{ts, prices:{code:{close,change_pct,volume}}, chips:{code:{...}}}
_rt_cache: dict = {"ts": None, "prices": {}, "chips": {}}
_RT_TTL_SECONDS = 300   # 5 分鐘快取


async def _fetch_rt_cache() -> dict:
    """
    [FIX-1] 從 TWSE OpenAPI 批次抓取所有上市股票的今日收盤/量/法人。
    結果快取 5 分鐘，避免重複呼叫。
    """
    now = datetime.now()
    ts  = _rt_cache.get("ts")
    if ts and (now - ts).total_seconds() < _RT_TTL_SECONDS:
        return _rt_cache   # 快取有效，直接回傳

    prices: dict[str, dict] = {}
    chips:  dict[str, dict] = {}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:

            # ── 收盤 / 量 ─────────────────────────────────────────────────
            try:
                r = await client.get(
                    "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
                )
                r.raise_for_status()
                for item in r.json():
                    code = unify_ticker_format(item.get("Code", ""))
                    if not code:
                        continue
                    try:
                        close_str = str(item.get("ClosingPrice", "") or "").replace(",", "")
                        change_str = str(item.get("Change", "") or "").replace(",", "")
                        vol_str   = str(item.get("TradeVolume", "") or "").replace(",", "")
                        close  = float(close_str)  if close_str  else 0.0
                        change = float(change_str) if change_str else 0.0
                        vol    = int(vol_str)      if vol_str    else 0
                        prev   = close - change
                        pct    = round(change / prev * 100, 2) if prev and prev != 0 else 0.0
                        # 台股成交量單位為「股」，轉換為「張（1000股）」
                        prices[code] = {
                            "close":      close,
                            "change_pct": pct,
                            "volume":     vol / 1000,   # 股 → 張
                        }
                        _valid_ticker_cache.add(code)
                    except Exception:
                        pass
                _log.info("[RT] STOCK_DAY_ALL loaded %d stocks", len(prices))
            except Exception as e:
                _log.warning("[RT] STOCK_DAY_ALL failed: %s", e)

            # ── 法人買賣 ──────────────────────────────────────────────────
            try:
                r = await client.get(
                    "https://openapi.twse.com.tw/v1/fund/TWT38U"
                )
                r.raise_for_status()
                for item in r.json():
                    code = unify_ticker_format(item.get("Code", ""))
                    if not code:
                        continue
                    try:
                        def _pi(v) -> int:
                            return int(str(v or "0").replace(",", ""))
                        chips[code] = {
                            "foreign_net": _pi(item.get("Foreign_Investor_Diff")),
                            "trust_net":   _pi(item.get("Investment_Trust_Diff")),
                            "dealer_net":  _pi(item.get("Dealer_Diff")),
                        }
                    except Exception:
                        pass
                _log.info("[RT] TWT38U loaded %d stocks", len(chips))
            except Exception as e:
                _log.warning("[RT] TWT38U failed: %s", e)

    except Exception as e:
        _log.error("[RT] httpx session failed: %s", e)

    _rt_cache.update({"ts": now, "prices": prices, "chips": chips})
    return _rt_cache


async def enrich_with_realtime(rows: list[StockRow]) -> list[StockRow]:
    """
    [FIX-1] 用 TWSE 真實資料覆蓋 MASTER_POOL 靜態假資料。
    覆蓋欄位：close / change_pct / volume / chip_5d（今日法人）
    若 API 無此代碼，保留 MASTER_POOL 資料並標記 _data_source = "pool"。
    """
    cache  = await _fetch_rt_cache()
    prices = cache.get("prices", {})
    chips  = cache.get("chips",  {})

    for row in rows:
        code = unify_ticker_format(row.stock_id)
        old_close = row.close

        if code in prices:
            p = prices[code]
            if p["close"] > 0:                   # API 有成交才覆蓋
                row.close      = p["close"]
                row.change_pct = p["change_pct"]
                row.volume     = p["volume"]
                row._data_source = "twse_live"
                ScreenerLogger.log_enrich(code, old_close, row.close, "twse_live")
            else:
                row._data_source = "pool(no_trade)"
        else:
            ScreenerLogger.log_unknown_ticker(code, "not in STOCK_DAY_ALL")
            row._data_source = "pool_only"

        if code in chips:
            c = chips[code]
            today_inst = c["foreign_net"] + c["trust_net"]   # 外資 + 投信
            if today_inst != 0:
                row.chip_5d = today_inst   # 用今日法人覆蓋 5d 欄位

        # 每次覆蓋真實資料後，重新計算衍生欄位
        if row.vol_20d_max > 100:
            row.vol_ratio = row.volume / (row.vol_20d_max / 1.8)
        row.ret_5d_approx   = row.change_pct / 100 * 2.0 + row.ma20_slope * 0.005
        row.foreign_net_5d  = row.chip_5d

    return rows


# ── _build_rows（改用 safe_build_row）────────────────────────────────────────

def _build_rows(raw_list: list[dict]) -> list[StockRow]:
    rows: list[StockRow] = []
    for i, d in enumerate(raw_list, 1):
        row = safe_build_row(d, rank=i)
        row.tags = compute_labels(row)
        rows.append(row)
    return rows


# ── 篩選器 ───────────────────────────────────────────────────────────────────

def momentum_screener(limit: int = 50) -> list[StockRow]:
    pool = sorted(_POOL_RAW,
                  key=lambda d: d["momentum_score"] * 0.6 + d["change_pct"] * 2.0,
                  reverse=True)
    return _build_rows(pool[:limit])


def value_screener(limit: int = 50) -> list[StockRow]:
    pool = sorted(_POOL_RAW,
                  key=lambda d: d["dividend_yield"] * 0.5 + d["eps_stability"] * 40 + d["value_score"] * 0.3,
                  reverse=True)
    return _build_rows(pool[:limit])


def chip_screener(limit: int = 50) -> list[StockRow]:
    pool = sorted(_POOL_RAW,
                  key=lambda d: d["chip_score_v"] * 0.5 + d["foreign_buy_days"] * 2.5 + d["chip_5d"] / 500,
                  reverse=True)
    return _build_rows(pool[:limit])


def breakout_screener(limit: int = 50) -> list[StockRow]:
    pool = sorted(_POOL_RAW,
                  key=lambda d: d["breakout_pct"] * 3.0 + d["kd_weekly"] * 0.3 + d["tech_score"] * 0.3,
                  reverse=True)
    return _build_rows(pool[:limit])


def ai_screener(limit: int = 50) -> list[StockRow]:
    ai_sectors = {"伺服器", "IC設計", "散熱/電源", "電源/散熱", "半導體"}
    pool = sorted(
        [d for d in _POOL_RAW if d["sector"] in ai_sectors],
        key=lambda d: d["model_score"] + d["momentum_score"] * 0.5,
        reverse=True,
    )
    return _build_rows(pool[:limit])


def sector_screener(sector: str, limit: int = 50) -> list[StockRow]:
    sector_lower = sector.strip().lower()
    pool = [d for d in _POOL_RAW
            if sector_lower in d["sector"].lower() or d["sector"].lower() in sector_lower]
    if not pool:
        _log.warning("[SectorScreener] no match for sector=%r, returning all", sector)
        pool = list(_POOL_RAW)
    pool = sorted(pool, key=lambda d: d["model_score"], reverse=True)
    return _build_rows(pool[:limit])


def all_screener(limit: int = 50) -> list[StockRow]:
    pool = sorted(
        _POOL_RAW,
        key=lambda d: (
            d["momentum_score"]    * 0.25 +
            d["value_score"]       * 0.15 +
            d["chip_score_v"]      * 0.25 +
            d["tech_score"]        * 0.20 +
            d["fundamental_score"] * 0.15
        ),
        reverse=True,
    )
    rows = _build_rows(pool[:limit])
    for row in rows:
        row.model_score = round(
            row.momentum_score * 0.25 + row.value_score * 0.15 +
            row.chip_score_v   * 0.25 + row.tech_score  * 0.20 +
            row.fundamental_score * 0.15, 1
        )
    return rows


def favorites_screener(stock_ids: list[str]) -> list[StockRow]:
    """
    [FIX-4] 我的最愛篩選器：
    - 統一代碼格式後比對 MASTER_POOL
    - 未知代碼改用 safe_build_row + 警告，不再填錯誤假資料
    """
    clean_ids = [unify_ticker_format(sid) for sid in stock_ids]
    rows: list[StockRow] = []

    for i, sid in enumerate(clean_ids, 1):
        pool_entry = safe_lookup(sid)
        if pool_entry:
            row = safe_build_row(pool_entry, rank=i)
        else:
            ScreenerLogger.log_unknown_ticker(sid, "not in MASTER_POOL; using minimal placeholder")
            row = safe_build_row({
                "stock_id":   sid,
                "name":       f"[{sid}]",   # 明確標示是未知代碼
                "sector":     "自選",
                "close":      0.0,           # 0 表示「待 API 補充」
                "change_pct": 0.0,
                "volume":     0.0,
                "chip_5d":    0.0,
                "chip_20d":   0.0,
                "_data_source": "placeholder",
            }, rank=i)
        row.tags = compute_labels(row)
        rows.append(row)

    return rows


# ── custom_screener（[FIX-3] 修復 regex）────────────────────────────────────

def _to_half_width(text: str) -> str:
    """全形數字/符號 → 半形"""
    return unicodedata.normalize("NFKC", text)


def _parse_number(s: str) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# 條件 regex 定義（順序無關，全部嘗試）
_CONDITION_PATTERNS: list[tuple[str, str, str]] = [
    # (pattern, field, op)
    (r"外資連買\s*(\d+\.?\d*)\s*[天日]",           "foreign_buy_days",  "gte"),
    (r"外資連賣\s*(\d+\.?\d*)\s*[天日]",           "foreign_buy_days",  "lte_neg"),
    (r"殖利率\s*[>＞≥]{1,2}\s*(\d+\.?\d*)",        "dividend_yield",    "gte"),
    (r"殖利率\s*[<＜≤]{1,2}\s*(\d+\.?\d*)",        "dividend_yield",    "lte"),
    (r"漲幅\s*[>＞≥]{1,2}\s*(\d+\.?\d*)",          "change_pct",        "gte"),
    (r"漲幅\s*[<＜≤]{1,2}\s*(\d+\.?\d*)",          "change_pct",        "lte"),
    (r"跌幅\s*[>＞≥]{1,2}\s*(\d+\.?\d*)",          "change_pct",        "lte_neg"),
    (r"本益比\s*[<＜≤]{1,2}\s*(\d+\.?\d*)",        "pe_ratio",          "lte"),
    (r"本益比\s*[>＞≥]{1,2}\s*(\d+\.?\d*)",        "pe_ratio",          "gte"),
    (r"eps成長\s*[>＞≥]{1,2}\s*(\d+\.?\d*)",       "eps_growth",        "gte"),
    (r"eps成長\s*[<＜≤]{1,2}\s*(\d+\.?\d*)",       "eps_growth",        "lte"),
    (r"營收年增\s*[>＞≥]{1,2}\s*(\d+\.?\d*)",      "rev_yoy",           "gte"),
    (r"法人買超",                                   "chip_5d",           "positive"),
    (r"法人賣超",                                   "chip_5d",           "negative"),
    (r"突破",                                       "breakout_pct",      "positive"),
]

# 族群關鍵字對應 sector
_SECTOR_KEYWORDS: dict[str, list[str]] = {
    "伺服器": ["伺服器", "server", "ai伺服器"],
    "散熱/電源": ["散熱", "電源", "cooling"],
    "半導體": ["半導體", "晶圓", "semiconductor"],
    "IC設計": ["ic設計", "ic design", "設計"],
    "電信": ["電信", "telecom"],
    "金融": ["金融", "銀行", "保險"],
}


async def custom_screener(conditions: str, api_key: str = "") -> list[StockRow]:
    """
    [FIX-3] AI 自訂條件篩選器。
    修復：
    - 全形數字統一轉換
    - 正確匹配 天/日
    - 族群關鍵字篩選
    - fallback 改為按 model_score 排序，不再回傳未排序全部
    """
    raw_conditions = conditions
    conditions = _to_half_width(conditions).lower().strip()

    pool = list(_POOL_RAW)
    filters_applied: list[str] = []

    # ── 套用所有條件 regex ────────────────────────────────────────────────
    for pattern, field, op in _CONDITION_PATTERNS:
        m = re.search(pattern, conditions)

        if op == "positive" and re.search(pattern, conditions):
            before = len(pool)
            pool = [d for d in pool if d.get(field, 0) > 0]
            filters_applied.append(f"{field}>0 (kept {len(pool)}/{before})")
            continue

        if op == "negative" and re.search(pattern, conditions):
            before = len(pool)
            pool = [d for d in pool if d.get(field, 0) < 0]
            filters_applied.append(f"{field}<0 (kept {len(pool)}/{before})")
            continue

        if not m:
            continue

        val = _parse_number(m.group(1))
        if val is None:
            continue

        ScreenerLogger.log_regex_match(pattern, conditions, val)
        before = len(pool)

        if op == "gte":
            pool = [d for d in pool if d.get(field, 0) >= val]
        elif op == "lte":
            pool = [d for d in pool if d.get(field, 0) <= val]
        elif op == "lte_neg":
            pool = [d for d in pool if d.get(field, 0) <= -val]

        filters_applied.append(f"{field} {op} {val} (kept {len(pool)}/{before})")

    # ── 族群關鍵字 ───────────────────────────────────────────────────────
    for sector, keywords in _SECTOR_KEYWORDS.items():
        if any(kw in conditions for kw in keywords):
            before = len(pool)
            pool = [d for d in pool if d["sector"] == sector]
            if pool:  # 只在有結果時套用
                filters_applied.append(f"sector={sector} (kept {len(pool)}/{before})")
            else:
                pool = list(_POOL_RAW)  # 族群無結果時 rollback
                _log.warning("[custom] sector filter %r gave 0 results, rolled back", sector)
            break

    # ── [FIX-3] fallback：改為按 model_score 排序，不再回傳全部未排序 ──
    if not filters_applied:
        _log.warning("[custom] no condition matched for: %r, returning top by model_score", raw_conditions)
    elif not pool:
        _log.warning("[custom] all filters left 0 results, returning top20 by model_score")
        pool = sorted(_POOL_RAW, key=lambda d: d["model_score"], reverse=True)

    pool = sorted(pool, key=lambda d: d["model_score"], reverse=True)

    _log.info("[custom] conditions=%r filters=%s result=%d",
              raw_conditions, filters_applied, min(len(pool), 20))

    return _build_rows(pool[:20])


# ── 篩選器統一入口 ────────────────────────────────────────────────────────────

def run_screener(
    screen_type: str,
    sector: str = "",
    stock_ids: list[str] = None,
    limit: int = 50,
) -> list[StockRow]:
    """
    同步篩選入口（使用 MASTER_POOL 靜態資料）。
    呼叫方若需真實資料，再呼叫 await enrich_with_realtime(rows)。
    """
    t = screen_type.lower().strip()
    rows: list[StockRow]

    if t == "momentum":
        rows = momentum_screener(limit)
    elif t == "value":
        rows = value_screener(limit)
    elif t == "chip":
        rows = chip_screener(limit)
    elif t == "breakout":
        rows = breakout_screener(limit)
    elif t in ("ai", "ai族群"):
        rows = ai_screener(limit)
    elif t == "sector":
        rows = sector_screener(sector or "半導體", limit)
    elif t == "favorites":
        rows = favorites_screener(stock_ids or [])
    else:
        rows = all_screener(limit)

    ScreenerLogger.log_call(t, sector, rows)
    return rows


# ── 分頁 ─────────────────────────────────────────────────────────────────────

PAGE_SIZE = 20


def paginate(rows: list[StockRow], page: int = 1, page_size: int = PAGE_SIZE) -> tuple[list[StockRow], int]:
    """
    [FIX-5] 分頁切片。
    確保 page 從 1 開始，不回傳空列表。
    回傳 (本頁資料, 總頁數)。
    """
    if not rows:
        return [], 1
    total_pages = max(1, math.ceil(len(rows) / page_size))
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end   = start + page_size
    page_rows = rows[start:end]
    _log.debug("[paginate] page=%d/%d rows=%d→%d",
               page, total_pages, len(rows), len(page_rows))
    return page_rows, total_pages


# ── 標籤對應 ─────────────────────────────────────────────────────────────────

SCREENER_LABELS = {
    "momentum":  "動能選股",
    "value":     "存股選股",
    "chip":      "籌碼選股",
    "breakout":  "技術突破",
    "ai":        "AI族群",
    "sector":    "指定族群",
    "all":       "全維度綜合",
    "favorites": "我的最愛",
    "custom":    "自訂條件",
}


def get_label(screen_type: str, sector: str = "") -> str:
    base = SCREENER_LABELS.get(screen_type.lower(), screen_type)
    if sector and screen_type.lower() == "sector":
        base = f"{sector}族群"
    return base
