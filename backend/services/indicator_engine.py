"""Agent B — 技術指標計算 + 三維度評分引擎

評分體系：
  基本面 (0-100)：營收 YoY、毛利率、三率齊升、EPS 成長
  籌碼面 (0-100)：外資/投信連續買超、雙強訊號、買超量能
  技術面 (0-100)：均線多頭、KD 交叉、量能突破、布林突破
  總分    = 基本面×0.35 + 籌碼面×0.35 + 技術面×0.30
"""
from __future__ import annotations
import math
from datetime import date, timedelta
from loguru import logger


# ── 技術指標計算（純 Python，不依賴 pandas）──────────────────────────────────

def calc_mas(closes: list[float], periods: list[int]) -> dict[int, float | None]:
    """計算多週期移動平均線"""
    result = {}
    for p in periods:
        if len(closes) >= p:
            result[p] = sum(closes[-p:]) / p
        else:
            result[p] = None
    return result


def calc_kd(highs: list[float], lows: list[float], closes: list[float],
            period: int = 9, smooth: int = 3) -> tuple[float, float]:
    """計算 KD 值（隨機震盪指標）"""
    if len(closes) < period:
        return 50.0, 50.0

    rsv_list = []
    for i in range(len(closes) - period + 1):
        hi  = max(highs[i:i + period])
        lo  = min(lows[i:i + period])
        cl  = closes[i + period - 1]
        rsv = (cl - lo) / (hi - lo) * 100 if hi != lo else 50.0
        rsv_list.append(rsv)

    # EMA of RSV → K; EMA of K → D
    k = d = 50.0
    for rsv in rsv_list:
        k = (smooth - 1) / smooth * k + rsv / smooth
        d = (smooth - 1) / smooth * d + k / smooth
    return round(k, 2), round(d, 2)


def calc_bollinger(closes: list[float], period: int = 20,
                   std_mult: float = 2.0) -> tuple[float, float, float]:
    """計算布林通道 (上軌, 中軌, 下軌)"""
    if len(closes) < period:
        c = closes[-1] if closes else 0
        return c, c, c
    window = closes[-period:]
    mid    = sum(window) / period
    std    = math.sqrt(sum((x - mid) ** 2 for x in window) / period)
    return round(mid + std_mult * std, 2), round(mid, 2), round(mid - std_mult * std, 2)


def calc_volume_ratio(volumes: list[int], window: int = 20) -> float:
    """最新成交量 / 近 window 日均量"""
    if len(volumes) < window + 1:
        return 1.0
    avg = sum(volumes[-window - 1:-1]) / window
    return round(volumes[-1] / avg, 2) if avg else 1.0


def detect_kd_cross(
    highs: list[float], lows: list[float], closes: list[float]
) -> tuple[bool, float, float]:
    """偵測 KD 黃金交叉（K 上穿 D）"""
    if len(closes) < 11:
        k, d = calc_kd(highs, lows, closes)
        return False, k, d
    k_now, d_now   = calc_kd(highs, lows, closes)
    k_prev, d_prev = calc_kd(highs, lows, closes[:-1])
    golden_cross    = (k_prev <= d_prev) and (k_now > d_now)
    return golden_cross, k_now, d_now


# ── 基本面評分 ────────────────────────────────────────────────────────────────

def score_fundamental(
    revenues: list[dict],      # [{year, month, yoy, revenue}, ...] 近 N 月，升序
    financials: list[dict],    # [{year, quarter, gross_margin, operating_margin, net_margin, eps}, ...] 升序
) -> tuple[float, dict]:
    """
    回傳 (score 0-100, detail dict)
    """
    detail: dict = {}
    score = 0.0

    # 1. 最新月營收 YoY（30分）
    rev_yoy = 0.0
    if revenues:
        rev_yoy = revenues[-1].get("yoy", 0) or 0
        detail["revenue_yoy"] = round(rev_yoy, 2)
        if rev_yoy >= 30:   score += 30
        elif rev_yoy >= 20: score += 22
        elif rev_yoy >= 10: score += 12
        elif rev_yoy >= 0:  score += 5
    else:
        detail["revenue_yoy"] = None

    # 2. 毛利率（25分）
    gm = 0.0
    if financials:
        gm = financials[-1].get("gross_margin", 0) or 0
        detail["gross_margin"] = round(gm, 2)
        # 趨勢加分
        gm_trend = 0
        if len(financials) >= 2:
            prev_gm = financials[-2].get("gross_margin", 0) or 0
            gm_trend = 5 if gm > prev_gm else 0
        if gm >= 50:   score += 20 + gm_trend
        elif gm >= 40: score += 15 + gm_trend
        elif gm >= 30: score += 10 + gm_trend
        elif gm >= 20: score += 5
    else:
        detail["gross_margin"] = None

    # 3. 三率齊升（25分）
    three_up = False
    if len(financials) >= 2:
        cur, prev = financials[-1], financials[-2]
        g_up = (cur.get("gross_margin", 0) or 0) > (prev.get("gross_margin", 0) or 0)
        o_up = (cur.get("operating_margin", 0) or 0) > (prev.get("operating_margin", 0) or 0)
        n_up = (cur.get("net_margin", 0) or 0) > (prev.get("net_margin", 0) or 0)
        rising_count = sum([g_up, o_up, n_up])
        three_up = rising_count == 3
        if three_up:    score += 25
        elif rising_count == 2: score += 15
        elif rising_count == 1: score += 5
        detail["three_margins_up"] = three_up
        detail["rising_margins_count"] = rising_count
    else:
        detail["three_margins_up"] = False

    # 4. EPS 連續成長季數（20分）
    eps_growth_qtrs = 0
    if len(financials) >= 2:
        for i in range(len(financials) - 1, 0, -1):
            cur_eps  = financials[i].get("eps") or 0
            prev_eps = financials[i - 1].get("eps") or 0
            if cur_eps > prev_eps:
                eps_growth_qtrs += 1
            else:
                break
        detail["eps_growth_qtrs"] = eps_growth_qtrs
        if eps_growth_qtrs >= 3:   score += 20
        elif eps_growth_qtrs == 2: score += 15
        elif eps_growth_qtrs == 1: score += 8
    else:
        detail["eps_growth_qtrs"] = 0

    return min(100.0, round(score, 1)), detail


# ── 籌碼面評分 ────────────────────────────────────────────────────────────────

def score_chip(chip_data: list[dict]) -> tuple[float, dict]:
    """
    chip_data: [{date, foreign_net, trust_net, total_net}, ...] 升序
    """
    detail: dict = {}
    score = 0.0

    if not chip_data:
        return 50.0, {"foreign_consec_buy": 0, "trust_consec_buy": 0}

    # 外資連續買超天數
    foreign_consec = 0
    for c in reversed(chip_data):
        if (c.get("foreign_net") or 0) > 0:
            foreign_consec += 1
        else:
            break
    detail["foreign_consec_buy"] = foreign_consec

    if foreign_consec >= 5:   score += 40
    elif foreign_consec >= 3: score += 30
    elif foreign_consec >= 1: score += 15

    # 投信連續買超
    trust_consec = 0
    for c in reversed(chip_data):
        if (c.get("trust_net") or 0) > 0:
            trust_consec += 1
        else:
            break
    detail["trust_consec_buy"] = trust_consec

    if trust_consec >= 5:   score += 20
    elif trust_consec >= 3: score += 15
    elif trust_consec >= 1: score += 8

    # 外資+投信雙強訊號（各買超 1 日以上）
    dual_signal = foreign_consec >= 1 and trust_consec >= 1
    if dual_signal:
        score += 20
    detail["dual_signal"] = dual_signal

    # 近 5 日外資淨買量（量能大小）
    recent5 = chip_data[-5:]
    foreign_5d = sum((c.get("foreign_net") or 0) for c in recent5)
    detail["foreign_net_5d"] = foreign_5d
    if foreign_5d >= 5000:   score += 20
    elif foreign_5d >= 1000: score += 12
    elif foreign_5d >= 500:  score += 6
    elif foreign_5d >= 0:    score += 0
    else:                     score -= 5

    return min(100.0, max(0.0, round(score, 1))), detail


# ── 技術面評分 ────────────────────────────────────────────────────────────────

def score_technical(
    closes: list[float],
    highs:  list[float],
    lows:   list[float],
    volumes: list[int],
) -> tuple[float, dict]:
    """
    price_data: 升序日線資料
    """
    detail: dict = {}
    score  = 0.0

    if len(closes) < 20:
        return 50.0, {"error": "資料不足（需 20 日）"}

    current = closes[-1]

    # 1. 均線多頭排列（30分）：5MA > 20MA > 60MA
    mas = calc_mas(closes, [5, 20, 60])
    ma5, ma20, ma60 = mas.get(5), mas.get(20), mas.get(60)
    detail.update({"ma5": ma5, "ma20": ma20, "ma60": ma60, "current": current})

    aligned_5_20  = ma5 is not None and ma20 is not None and ma5 > ma20
    aligned_20_60 = ma20 is not None and ma60 is not None and ma20 > ma60
    above_ma5     = ma5 is not None and current > ma5

    if aligned_5_20 and aligned_20_60:
        score += 30
    elif aligned_5_20:
        score += 15
    elif above_ma5:
        score += 5
    detail["ma_aligned"] = aligned_5_20 and aligned_20_60

    # 2. KD 黃金交叉且 K<80（25分）
    golden_cross, k, d = detect_kd_cross(highs, lows, closes)
    detail.update({"kd_k": k, "kd_d": d, "kd_golden_cross": golden_cross})
    if golden_cross and k < 80:
        score += 25
    elif k < 80 and k > d:
        score += 12
    elif k < 30:    # 超賣反彈機會
        score += 8

    # 3. 量能突破 20 日均量 1.5 倍（25分）
    vol_ratio = calc_volume_ratio(volumes, 20)
    detail["vol_ratio"] = vol_ratio
    vol_breakout = vol_ratio >= 1.5
    if vol_ratio >= 2.0:   score += 25
    elif vol_ratio >= 1.5: score += 18
    elif vol_ratio >= 1.2: score += 8
    detail["vol_breakout"] = vol_breakout

    # 4. 布林通道突破上軌（20分）
    upper, mid, lower = calc_bollinger(closes)
    detail.update({"bb_upper": upper, "bb_mid": mid, "bb_lower": lower})
    bb_breakout = current >= upper
    bb_pos      = (current - lower) / (upper - lower) if upper != lower else 0.5
    if bb_breakout:
        score += 20
    elif bb_pos >= 0.75:
        score += 10
    elif bb_pos >= 0.5:
        score += 5
    detail["bb_breakout"] = bb_breakout

    return min(100.0, max(0.0, round(score, 1))), detail


# ── 整合評分 ─────────────────────────────────────────────────────────────────

def calc_total_score(
    fundamental: float,
    chip: float,
    technical: float,
    weights: tuple[float, float, float] = (0.35, 0.35, 0.30),
) -> float:
    """加權總分"""
    return round(
        fundamental * weights[0] + chip * weights[1] + technical * weights[2],
        1,
    )


def calc_confidence(total: float, detail: dict) -> float:
    """
    信心指數（0-100）：綜合分數 + 訊號強度加成
    """
    base = total
    bonus = 0.0

    # 三率齊升 + 高 YoY 加成
    if detail.get("three_margins_up") and (detail.get("revenue_yoy") or 0) >= 20:
        bonus += 10
    # 外資+投信雙強
    if detail.get("dual_signal") and detail.get("foreign_consec_buy", 0) >= 3:
        bonus += 8
    # 均線+量能+KD 三技術共振
    if (detail.get("ma_aligned") and detail.get("vol_breakout")
            and detail.get("kd_golden_cross")):
        bonus += 8

    return min(100.0, round(base + bonus, 1))
