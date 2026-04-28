"""
report_screener.py — 多維度選股篩選器

支援類型：momentum / value / chip / breakout / ai / sector / all / custom
每個篩選器回傳 list[StockRow]，依得分排序。
paginate() 支援分頁（預設每頁 20 筆）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np


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


# ── StockRow（共用資料結構）────────────────────────────────────────────────────

@dataclass
class StockRow:
    """選股表單列資料（供 generate_report_image 使用）"""
    stock_id:      str
    name:          str
    sector:        str
    close:         float
    change_pct:    float        # % 漲跌幅
    volume:        float        # 張

    # 籌碼
    chip_5d:       float        # 5日法人淨買（張）
    chip_20d:      float        # 20日法人淨買（張）
    foreign_buy_days: int = 0   # 外資連買天數（負=連賣）

    # 基本面
    rev_yoy:       float = 0.0  # 營收年增率 %
    rev_mom:       float = 0.0  # 營收月增率 %
    eps_growth:    float = 0.0  # EPS成長率 %
    dividend_yield:float = 0.0  # 殖利率 %
    pe_ratio:      float = 0.0  # 本益比
    eps_stability: float = 0.0  # EPS穩定度 0~1

    # 技術面
    kd_weekly:     float = 50.0
    ma20_slope:    float = 0.0  # > 0 表示 MA20 向上
    consec_up:     int   = 0    # 連續收紅天數
    vol_20d_max:   float = 0.0  # 20日最大量
    intraday_range:float = 0.0  # 日內振幅 %
    group_avg_change: float = 0.0  # 同族群平均漲幅 %
    breakout_pct:  float = 0.0  # 突破幅度 % (>0=已突破)
    target_price:  float = 0.0  # 目標價

    # 模型分數
    model_score:   float = 50.0   # 0~100
    confidence:    float = 50.0   # 0~100 信心指數
    day_rank:      int   = 99     # 今日排名

    # 分維度得分（供 ALL 排名與雷達圖）
    momentum_score: float = 50.0
    value_score:    float = 50.0
    chip_score_v:   float = 50.0  # chip_score_v 避免與欄位名稱衝突
    tech_score:     float = 50.0
    fundamental_score: float = 50.0

    # 自動計算標籤
    tags: list[str] = field(default_factory=list)

    def volume_k(self) -> float:
        return self.volume / 1000

    def stars(self) -> str:
        """model_score → ★ 星級（5 顆）"""
        n = round(self.model_score / 20)
        n = max(0, min(5, n))
        return "★" * n + "☆" * (5 - n)


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


# ── 主力股票資料池（25 檔，涵蓋各族群）──────────────────────────────────────

_POOL_RAW: list[dict] = [
    # 半導體
    dict(stock_id="2330", name="台積電",    sector="半導體",
         close=850,   change_pct=+2.3,  volume=35000, chip_5d=+8500,  chip_20d=+32000, foreign_buy_days=8,
         rev_yoy=28.5, rev_mom=5.2,  eps_growth=32.1, dividend_yield=2.5, pe_ratio=22, eps_stability=0.92,
         kd_weekly=72, ma20_slope=1.2,  consec_up=6, vol_20d_max=34000, intraday_range=2.8, group_avg_change=3.5,
         breakout_pct=2.1, target_price=880,
         model_score=88, confidence=87, momentum_score=85, value_score=42, chip_score_v=82, tech_score=78, fundamental_score=86),
    dict(stock_id="2454", name="聯發科",    sector="IC設計",
         close=1180,  change_pct=+3.1,  volume=18000, chip_5d=+3200,  chip_20d=+15000, foreign_buy_days=6,
         rev_yoy=22.0, rev_mom=8.1,  eps_growth=25.0, dividend_yield=3.5, pe_ratio=18, eps_stability=0.78,
         kd_weekly=68, ma20_slope=0.9,  consec_up=3, vol_20d_max=20000, intraday_range=3.8, group_avg_change=3.5,
         breakout_pct=3.5, target_price=1150,
         model_score=85, confidence=83, momentum_score=88, value_score=55, chip_score_v=75, tech_score=82, fundamental_score=78),
    dict(stock_id="2303", name="聯電",      sector="半導體",
         close=46.5,  change_pct=-0.5,  volume=28000, chip_5d=-800,   chip_20d=+2000,  foreign_buy_days=-2,
         rev_yoy=8.0,  rev_mom=-1.5, eps_growth=5.0,  dividend_yield=5.5, pe_ratio=14, eps_stability=0.72,
         kd_weekly=55, ma20_slope=0.1,  consec_up=2, vol_20d_max=27000, intraday_range=1.8, group_avg_change=3.5,
         breakout_pct=0.0, target_price=48,
         model_score=62, confidence=60, momentum_score=52, value_score=68, chip_score_v=48, tech_score=55, fundamental_score=65),
    # IC設計
    dict(stock_id="3711", name="日月光投",  sector="封裝測試",
         close=152,   change_pct=+1.2,  volume=9500,  chip_5d=+550,   chip_20d=+3200,  foreign_buy_days=4,
         rev_yoy=12.0, rev_mom=2.8,  eps_growth=14.2, dividend_yield=4.2, pe_ratio=16, eps_stability=0.80,
         kd_weekly=64, ma20_slope=0.7,  consec_up=4, vol_20d_max=9000, intraday_range=1.5, group_avg_change=3.5,
         breakout_pct=1.2, target_price=155,
         model_score=71, confidence=70, momentum_score=65, value_score=62, chip_score_v=60, tech_score=68, fundamental_score=70),
    dict(stock_id="6770", name="力積電",    sector="半導體",
         close=52.8,  change_pct=+1.8,  volume=22000, chip_5d=+1200,  chip_20d=+4500,  foreign_buy_days=3,
         rev_yoy=15.2, rev_mom=3.0,  eps_growth=18.5, dividend_yield=3.8, pe_ratio=17, eps_stability=0.70,
         kd_weekly=61, ma20_slope=0.5,  consec_up=5, vol_20d_max=21000, intraday_range=2.2, group_avg_change=3.5,
         breakout_pct=1.8, target_price=50,
         model_score=74, confidence=72, momentum_score=72, value_score=58, chip_score_v=65, tech_score=70, fundamental_score=68),
    # 散熱族群
    dict(stock_id="6415", name="矽力-KY",   sector="散熱/電源",
         close=1350,  change_pct=+4.2,  volume=5200,  chip_5d=+2100,  chip_20d=+8500,  foreign_buy_days=7,
         rev_yoy=35.0, rev_mom=9.5,  eps_growth=40.0, dividend_yield=1.8, pe_ratio=28, eps_stability=0.88,
         kd_weekly=78, ma20_slope=1.5,  consec_up=7, vol_20d_max=5100, intraday_range=4.5, group_avg_change=4.2,
         breakout_pct=4.2, target_price=1300,
         model_score=92, confidence=91, momentum_score=92, value_score=35, chip_score_v=88, tech_score=90, fundamental_score=90),
    dict(stock_id="3552", name="同亨",      sector="散熱/電源",
         close=285,   change_pct=+3.8,  volume=3200,  chip_5d=+800,   chip_20d=+3000,  foreign_buy_days=5,
         rev_yoy=28.0, rev_mom=6.0,  eps_growth=33.0, dividend_yield=2.2, pe_ratio=24, eps_stability=0.82,
         kd_weekly=73, ma20_slope=1.1,  consec_up=6, vol_20d_max=3100, intraday_range=3.2, group_avg_change=4.2,
         breakout_pct=3.8, target_price=280,
         model_score=87, confidence=85, momentum_score=88, value_score=38, chip_score_v=80, tech_score=85, fundamental_score=84),
    dict(stock_id="3450", name="聯鈞",      sector="散熱/電源",
         close=95.5,  change_pct=+2.5,  volume=4800,  chip_5d=+400,   chip_20d=+1800,  foreign_buy_days=3,
         rev_yoy=18.5, rev_mom=4.0,  eps_growth=22.0, dividend_yield=3.5, pe_ratio=20, eps_stability=0.75,
         kd_weekly=65, ma20_slope=0.8,  consec_up=5, vol_20d_max=4700, intraday_range=2.8, group_avg_change=4.2,
         breakout_pct=2.5, target_price=95,
         model_score=78, confidence=76, momentum_score=76, value_score=52, chip_score_v=65, tech_score=72, fundamental_score=74),
    # 伺服器/AI
    dict(stock_id="3231", name="緯創",      sector="伺服器",
         close=102,   change_pct=+5.1,  volume=25000, chip_5d=+3500,  chip_20d=+12000, foreign_buy_days=9,
         rev_yoy=42.0, rev_mom=12.0, eps_growth=55.0, dividend_yield=2.8, pe_ratio=20, eps_stability=0.82,
         kd_weekly=80, ma20_slope=2.0,  consec_up=8, vol_20d_max=24000, intraday_range=5.1, group_avg_change=5.0,
         breakout_pct=5.1, target_price=95,
         model_score=95, confidence=93, momentum_score=96, value_score=45, chip_score_v=92, tech_score=94, fundamental_score=92),
    dict(stock_id="2382", name="廣達",      sector="伺服器",
         close=285,   change_pct=+3.5,  volume=32000, chip_5d=+5500,  chip_20d=+22000, foreign_buy_days=10,
         rev_yoy=38.5, rev_mom=10.5, eps_growth=48.0, dividend_yield=3.2, pe_ratio=22, eps_stability=0.85,
         kd_weekly=76, ma20_slope=1.8,  consec_up=7, vol_20d_max=31000, intraday_range=4.0, group_avg_change=5.0,
         breakout_pct=4.2, target_price=270,
         model_score=93, confidence=91, momentum_score=94, value_score=48, chip_score_v=90, tech_score=92, fundamental_score=90),
    dict(stock_id="6669", name="緯穎",      sector="伺服器",
         close=2050,  change_pct=+6.2,  volume=8500,  chip_5d=+4800,  chip_20d=+18000, foreign_buy_days=12,
         rev_yoy=55.0, rev_mom=15.0, eps_growth=70.0, dividend_yield=1.5, pe_ratio=32, eps_stability=0.78,
         kd_weekly=82, ma20_slope=2.5,  consec_up=9, vol_20d_max=8200, intraday_range=6.2, group_avg_change=5.0,
         breakout_pct=6.2, target_price=1900,
         model_score=96, confidence=94, momentum_score=98, value_score=28, chip_score_v=95, tech_score=96, fundamental_score=94),
    # 存股標的
    dict(stock_id="0056", name="元大高股息", sector="ETF",
         close=36.5,  change_pct=+0.3,  volume=85000, chip_5d=+2000,  chip_20d=+8000,  foreign_buy_days=2,
         rev_yoy=8.5,  rev_mom=1.0,  eps_growth=6.0,  dividend_yield=7.5, pe_ratio=13, eps_stability=0.92,
         kd_weekly=58, ma20_slope=0.2,  consec_up=3, vol_20d_max=85000, intraday_range=0.5, group_avg_change=1.5,
         breakout_pct=0.3, target_price=37,
         model_score=68, confidence=72, momentum_score=35, value_score=95, chip_score_v=55, tech_score=50, fundamental_score=88),
    dict(stock_id="2412", name="中華電",    sector="電信",
         close=118,   change_pct=-0.2,  volume=5200,  chip_5d=-100,   chip_20d=+500,   foreign_buy_days=-1,
         rev_yoy=3.5,  rev_mom=0.5,  eps_growth=2.0,  dividend_yield=6.2, pe_ratio=22, eps_stability=0.95,
         kd_weekly=52, ma20_slope=0.0,  consec_up=1, vol_20d_max=5400, intraday_range=0.4, group_avg_change=0.8,
         breakout_pct=0.0, target_price=120,
         model_score=55, confidence=60, momentum_score=28, value_score=88, chip_score_v=42, tech_score=45, fundamental_score=82),
    dict(stock_id="2317", name="鴻海",      sector="電子製造",
         close=118.5, change_pct=+1.2,  volume=55000, chip_5d=+2500,  chip_20d=+9500,  foreign_buy_days=4,
         rev_yoy=12.0, rev_mom=3.5,  eps_growth=15.0, dividend_yield=4.8, pe_ratio=11, eps_stability=0.75,
         kd_weekly=62, ma20_slope=0.6,  consec_up=4, vol_20d_max=54000, intraday_range=1.8, group_avg_change=2.5,
         breakout_pct=1.2, target_price=120,
         model_score=72, confidence=70, momentum_score=65, value_score=78, chip_score_v=68, tech_score=65, fundamental_score=72),
    dict(stock_id="2884", name="玉山金",    sector="金融",
         close=29.8,  change_pct=+0.5,  volume=18000, chip_5d=+350,   chip_20d=+1200,  foreign_buy_days=1,
         rev_yoy=5.0,  rev_mom=1.2,  eps_growth=4.5,  dividend_yield=5.8, pe_ratio=12, eps_stability=0.88,
         kd_weekly=54, ma20_slope=0.1,  consec_up=2, vol_20d_max=17500, intraday_range=0.6, group_avg_change=1.2,
         breakout_pct=0.5, target_price=30,
         model_score=60, confidence=62, momentum_score=38, value_score=85, chip_score_v=48, tech_score=50, fundamental_score=80),
    # 突破族群
    dict(stock_id="8299", name="群聯",      sector="IC設計",
         close=535,   change_pct=+4.8,  volume=8500,  chip_5d=+1500,  chip_20d=+5500,  foreign_buy_days=6,
         rev_yoy=32.0, rev_mom=8.5,  eps_growth=38.0, dividend_yield=4.0, pe_ratio=19, eps_stability=0.80,
         kd_weekly=74, ma20_slope=1.4,  consec_up=5, vol_20d_max=8000, intraday_range=4.8, group_avg_change=4.5,
         breakout_pct=4.8, target_price=500,
         model_score=84, confidence=82, momentum_score=86, value_score=58, chip_score_v=78, tech_score=88, fundamental_score=82),
    dict(stock_id="6269", name="台郡",      sector="電子零件",
         close=128,   change_pct=+3.2,  volume=9200,  chip_5d=+620,   chip_20d=+2500,  foreign_buy_days=5,
         rev_yoy=20.0, rev_mom=5.5,  eps_growth=24.0, dividend_yield=3.2, pe_ratio=18, eps_stability=0.76,
         kd_weekly=70, ma20_slope=1.0,  consec_up=5, vol_20d_max=9000, intraday_range=3.2, group_avg_change=3.8,
         breakout_pct=3.2, target_price=122,
         model_score=79, confidence=77, momentum_score=80, value_score=52, chip_score_v=70, tech_score=82, fundamental_score=76),
    dict(stock_id="3529", name="力旺",      sector="IC設計",
         close=1280,  change_pct=+5.5,  volume=3800,  chip_5d=+1800,  chip_20d=+7200,  foreign_buy_days=7,
         rev_yoy=45.0, rev_mom=12.0, eps_growth=52.0, dividend_yield=2.0, pe_ratio=35, eps_stability=0.72,
         kd_weekly=76, ma20_slope=1.6,  consec_up=6, vol_20d_max=3700, intraday_range=5.5, group_avg_change=5.2,
         breakout_pct=5.5, target_price=1200,
         model_score=89, confidence=87, momentum_score=92, value_score=30, chip_score_v=85, tech_score=90, fundamental_score=88),
    # 生技
    dict(stock_id="6488", name="環球晶",    sector="半導體材料",
         close=380,   change_pct=+2.8,  volume=12000, chip_5d=+2200,  chip_20d=+8800,  foreign_buy_days=5,
         rev_yoy=18.0, rev_mom=4.5,  eps_growth=22.0, dividend_yield=2.8, pe_ratio=20, eps_stability=0.82,
         kd_weekly=66, ma20_slope=0.9,  consec_up=4, vol_20d_max=11800, intraday_range=2.8, group_avg_change=3.2,
         breakout_pct=2.8, target_price=365,
         model_score=80, confidence=78, momentum_score=78, value_score=50, chip_score_v=75, tech_score=76, fundamental_score=78),
    dict(stock_id="2308", name="台達電",    sector="電源/散熱",
         close=358,   change_pct=+2.5,  volume=15000, chip_5d=+1800,  chip_20d=+7500,  foreign_buy_days=4,
         rev_yoy=15.0, rev_mom=3.8,  eps_growth=18.0, dividend_yield=3.5, pe_ratio=21, eps_stability=0.85,
         kd_weekly=67, ma20_slope=0.8,  consec_up=4, vol_20d_max=14500, intraday_range=2.5, group_avg_change=4.2,
         breakout_pct=2.5, target_price=345,
         model_score=78, confidence=76, momentum_score=76, value_score=58, chip_score_v=72, tech_score=74, fundamental_score=78),
    dict(stock_id="2379", name="瑞昱",      sector="IC設計",
         close=565,   change_pct=+3.8,  volume=9800,  chip_5d=+1200,  chip_20d=+4800,  foreign_buy_days=5,
         rev_yoy=25.0, rev_mom=7.0,  eps_growth=30.0, dividend_yield=3.8, pe_ratio=17, eps_stability=0.80,
         kd_weekly=70, ma20_slope=1.1,  consec_up=5, vol_20d_max=9500, intraday_range=3.8, group_avg_change=4.5,
         breakout_pct=3.8, target_price=535,
         model_score=83, confidence=81, momentum_score=84, value_score=60, chip_score_v=76, tech_score=82, fundamental_score=80),
    # 傳產/防禦
    dict(stock_id="1301", name="台塑",      sector="石化",
         close=68.5,  change_pct=-0.8,  volume=8200,  chip_5d=-500,   chip_20d=-1200,  foreign_buy_days=-3,
         rev_yoy=-5.0, rev_mom=-2.0, eps_growth=-8.0, dividend_yield=6.5, pe_ratio=18, eps_stability=0.65,
         kd_weekly=38, ma20_slope=-0.5, consec_up=0, vol_20d_max=8500, intraday_range=1.2, group_avg_change=-1.2,
         breakout_pct=0.0, target_price=72,
         model_score=38, confidence=40, momentum_score=22, value_score=72, chip_score_v=28, tech_score=30, fundamental_score=45),
    dict(stock_id="2002", name="中鋼",      sector="鋼鐵",
         close=24.8,  change_pct=-0.5,  volume=32000, chip_5d=-800,   chip_20d=-2500,  foreign_buy_days=-2,
         rev_yoy=-3.0, rev_mom=-1.0, eps_growth=-5.0, dividend_yield=5.8, pe_ratio=16, eps_stability=0.60,
         kd_weekly=42, ma20_slope=-0.3, consec_up=0, vol_20d_max=33000, intraday_range=1.0, group_avg_change=-0.8,
         breakout_pct=0.0, target_price=26,
         model_score=42, confidence=44, momentum_score=25, value_score=70, chip_score_v=30, tech_score=35, fundamental_score=48),
    dict(stock_id="2409", name="友達",      sector="面板",
         close=14.8,  change_pct=+0.7,  volume=45000, chip_5d=+200,   chip_20d=+800,   foreign_buy_days=1,
         rev_yoy=2.0,  rev_mom=0.5,  eps_growth=1.5,  dividend_yield=4.2, pe_ratio=15, eps_stability=0.55,
         kd_weekly=50, ma20_slope=0.1,  consec_up=2, vol_20d_max=44000, intraday_range=1.0, group_avg_change=1.0,
         breakout_pct=0.7, target_price=15,
         model_score=50, confidence=52, momentum_score=40, value_score=60, chip_score_v=42, tech_score=48, fundamental_score=50),
]


def _build_rows(raw_list: list[dict]) -> list[StockRow]:
    """dict list → StockRow list，自動計算標籤"""
    rows: list[StockRow] = []
    valid_keys = set(StockRow.__dataclass_fields__.keys()) - {"tags"}
    for i, d in enumerate(raw_list, 1):
        kwargs = {k: v for k, v in d.items() if k in valid_keys}
        row = StockRow(**kwargs)
        row.day_rank = i
        row.tags = compute_labels(row)
        rows.append(row)
    return rows


# ── 篩選器 ───────────────────────────────────────────────────────────────────

def momentum_screener(limit: int = 50) -> list[StockRow]:
    """動能選股：依 momentum_score × change_pct 排序"""
    pool = [dict(d) for d in _POOL_RAW]
    sorted_pool = sorted(pool, key=lambda d: d["momentum_score"] * 0.6 + d["change_pct"] * 2.0, reverse=True)
    rows = _build_rows(sorted_pool[:limit])
    return rows


def value_screener(limit: int = 50) -> list[StockRow]:
    """存股選股：依 dividend_yield 和 eps_stability 排序"""
    pool = [dict(d) for d in _POOL_RAW]
    sorted_pool = sorted(pool, key=lambda d: d["dividend_yield"] * 0.5 + d["eps_stability"] * 40 + d["value_score"] * 0.3, reverse=True)
    rows = _build_rows(sorted_pool[:limit])
    return rows


def chip_screener(limit: int = 50) -> list[StockRow]:
    """籌碼選股：依法人買超和連買天數排序"""
    pool = [dict(d) for d in _POOL_RAW]
    sorted_pool = sorted(pool,
        key=lambda d: d["chip_score_v"] * 0.5 + d["foreign_buy_days"] * 2.5 + d["chip_5d"] / 500,
        reverse=True)
    rows = _build_rows(sorted_pool[:limit])
    return rows


def breakout_screener(limit: int = 50) -> list[StockRow]:
    """技術突破選股：依突破幅度、KD 和 MA20 斜率排序"""
    pool = [dict(d) for d in _POOL_RAW]
    sorted_pool = sorted(pool,
        key=lambda d: d["breakout_pct"] * 3.0 + d["kd_weekly"] * 0.3 + d["tech_score"] * 0.3,
        reverse=True)
    rows = _build_rows(sorted_pool[:limit])
    return rows


def ai_screener(limit: int = 50) -> list[StockRow]:
    """AI/伺服器族群：同族群 + 動能高"""
    ai_sectors = {"伺服器", "IC設計", "散熱/電源", "電源/散熱", "半導體"}
    pool = [d for d in _POOL_RAW if d["sector"] in ai_sectors]
    pool = sorted(pool, key=lambda d: d["model_score"] + d["momentum_score"] * 0.5, reverse=True)
    rows = _build_rows(pool[:limit])
    return rows


def sector_screener(sector: str, limit: int = 50) -> list[StockRow]:
    """指定族群選股"""
    sector_lower = sector.lower()
    # 模糊匹配族群名
    pool = [d for d in _POOL_RAW
            if sector_lower in d["sector"].lower() or d["sector"].lower() in sector_lower]
    if not pool:
        pool = sorted(_POOL_RAW, key=lambda d: d["model_score"], reverse=True)
    else:
        pool = sorted(pool, key=lambda d: d["model_score"], reverse=True)
    rows = _build_rows(pool[:limit])
    return rows


def all_screener(limit: int = 50) -> list[StockRow]:
    """全維度綜合排名：五個維度等權加總"""
    pool = [dict(d) for d in _POOL_RAW]
    for d in pool:
        d["_composite"] = (
            d["momentum_score"] * 0.25 +
            d["value_score"]    * 0.15 +
            d["chip_score_v"]   * 0.25 +
            d["tech_score"]     * 0.20 +
            d["fundamental_score"] * 0.15
        )
    pool = sorted(pool, key=lambda d: d["_composite"], reverse=True)
    rows = _build_rows(pool[:limit])
    for row in rows:
        row.model_score = round(
            row.momentum_score * 0.25 + row.value_score * 0.15 +
            row.chip_score_v * 0.25 + row.tech_score * 0.20 +
            row.fundamental_score * 0.15, 1
        )
    return rows


def favorites_screener(stock_ids: list[str]) -> list[StockRow]:
    """我的最愛篩選器：從資料池取得指定股票"""
    id_set = set(stock_ids)
    pool = [d for d in _POOL_RAW if d["stock_id"] in id_set]
    # 補上不在資料池的股票（以預設值產生）
    found_ids = {d["stock_id"] for d in pool}
    for sid in stock_ids:
        if sid not in found_ids:
            pool.append(dict(
                stock_id=sid, name=sid, sector="自選",
                close=100, change_pct=0, volume=1000,
                chip_5d=0, chip_20d=0, foreign_buy_days=0,
                rev_yoy=0, rev_mom=0, eps_growth=0,
                dividend_yield=0, pe_ratio=0, eps_stability=0.5,
                kd_weekly=50, ma20_slope=0, consec_up=0,
                vol_20d_max=1000, intraday_range=1.0, group_avg_change=0,
                breakout_pct=0, target_price=0,
                model_score=50, confidence=50,
                momentum_score=50, value_score=50, chip_score_v=50,
                tech_score=50, fundamental_score=50,
            ))
    return _build_rows(pool)


async def custom_screener(conditions: str, api_key: str = "") -> list[StockRow]:
    """
    AI 解析自然語言篩選條件並回傳符合的 StockRow。
    conditions 範例：「外資連買3天 殖利率>4% 漲幅<5%」
    """
    # 先用規則解析常見條件
    pool = list(_POOL_RAW)

    conditions_lower = conditions.lower()

    # 外資連買 N 天
    import re
    m = re.search(r'外資連買(\d+)天?', conditions)
    if m:
        n = int(m.group(1))
        pool = [d for d in pool if d["foreign_buy_days"] >= n]

    # 殖利率 > N%
    m = re.search(r'殖利率[>＞](\d+\.?\d*)', conditions)
    if m:
        n = float(m.group(1))
        pool = [d for d in pool if d["dividend_yield"] >= n]

    # 殖利率 < N%
    m = re.search(r'殖利率[<＜](\d+\.?\d*)', conditions)
    if m:
        n = float(m.group(1))
        pool = [d for d in pool if d["dividend_yield"] <= n]

    # 漲幅 < N%
    m = re.search(r'漲幅[<＜](\d+\.?\d*)', conditions)
    if m:
        n = float(m.group(1))
        pool = [d for d in pool if d["change_pct"] < n]

    # 漲幅 > N%
    m = re.search(r'漲幅[>＞](\d+\.?\d*)', conditions)
    if m:
        n = float(m.group(1))
        pool = [d for d in pool if d["change_pct"] > n]

    # 法人買超
    if "法人" in conditions and ("買" in conditions or "大買" in conditions):
        pool = [d for d in pool if d["chip_5d"] > 0]

    # 突破
    if "突破" in conditions_lower:
        pool = [d for d in pool if d["breakout_pct"] > 1.5]

    # 如果有 api_key 且篩選後結果很多，可以用 AI 進一步解析
    # 這裡只做基礎規則篩選，sorted by model_score
    if not pool:
        pool = list(_POOL_RAW)  # fallback to all

    pool = sorted(pool, key=lambda d: d["model_score"], reverse=True)
    return _build_rows(pool[:20])


# ── 篩選器分派 ────────────────────────────────────────────────────────────────

def run_screener(
    screen_type: str,
    sector: str = "",
    stock_ids: list[str] = None,
    limit: int = 50,
) -> list[StockRow]:
    """統一入口：依 screen_type 呼叫對應篩選器"""
    t = screen_type.lower().strip()
    if t == "momentum":   return momentum_screener(limit)
    if t == "value":      return value_screener(limit)
    if t == "chip":       return chip_screener(limit)
    if t == "breakout":   return breakout_screener(limit)
    if t in ("ai", "ai族群"): return ai_screener(limit)
    if t == "sector":     return sector_screener(sector or "半導體", limit)
    if t == "favorites":  return favorites_screener(stock_ids or [])
    return all_screener(limit)  # default / "all"


# ── 分頁工具 ─────────────────────────────────────────────────────────────────

PAGE_SIZE = 20


def paginate(rows: list[StockRow], page: int = 1, page_size: int = PAGE_SIZE) -> tuple[list[StockRow], int]:
    """
    回傳 (本頁資料, 總頁數)。
    page 從 1 開始。
    """
    total_pages = max(1, math.ceil(len(rows) / page_size))
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end   = start + page_size
    return rows[start:end], total_pages


# ── 各篩選器標題對應 ─────────────────────────────────────────────────────────

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
