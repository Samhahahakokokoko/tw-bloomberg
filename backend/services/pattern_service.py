"""pattern_service.py — K線型態辨識"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Optional
logger = logging.getLogger(__name__)

@dataclass
class PatternResult:
    stock_id: str
    stock_name: str
    patterns: list           # [{name, confidence, description, bullish}]
    ma_status: str           # MA均線狀態
    support: float           # 支撐位
    resistance: float        # 壓力位
    target_price: Optional[float]
    ai_interpretation: str
    current_price: float


async def detect_patterns(code: str) -> Optional[PatternResult]:
    """偵測K線型態"""
    try:
        from backend.services.twse_service import fetch_realtime_quote, fetch_kline
        import asyncio as _asyncio
        import numpy as np

        quote, klines = await _asyncio.gather(
            fetch_realtime_quote(code),
            fetch_kline(code, period="daily", limit=120),
            return_exceptions=True
        )

        if isinstance(klines, Exception) or not klines or len(klines) < 20:
            return None

        name = code
        current = 0.0
        if isinstance(quote, dict):
            name = quote.get("name", code)
            current = float(quote.get("price", 0) or 0)

        closes  = [float(k.get("close", 0) or 0) for k in klines]
        highs   = [float(k.get("high", 0) or 0) for k in klines]
        lows    = [float(k.get("low", 0) or 0) for k in klines]
        volumes = [float(k.get("volume", 0) or 0) for k in klines]

        if not closes or max(closes) == 0:
            return None

        patterns = []

        # 1. 頭肩頂 / 頭肩底
        hs = _detect_head_shoulders(closes[-60:], highs[-60:], lows[-60:])
        if hs:
            patterns.append(hs)

        # 2. 雙頂 / 雙底
        dt = _detect_double_top_bottom(closes[-40:], highs[-40:], lows[-40:])
        if dt:
            patterns.append(dt)

        # 3. 三角收斂
        tri = _detect_triangle(closes[-30:], highs[-30:], lows[-30:])
        if tri:
            patterns.append(tri)

        # 4. 均線突破
        ma_pattern, ma_status = _detect_ma_break(closes)

        # 5. 52週高低點
        high_52w = max(highs[-252:]) if len(highs) >= 252 else max(highs)
        low_52w  = min(lows[-252:])  if len(lows)  >= 252 else min(lows)
        if current > 0 and current >= high_52w * 0.98:
            patterns.append({
                "name": "52週新高", "confidence": 90,
                "description": f"股價逼近52週高點{high_52w:.1f}，突破意義重大", "bullish": True
            })
        if current > 0 and current <= low_52w * 1.02:
            patterns.append({
                "name": "52週新低", "confidence": 85,
                "description": f"股價逼近52週低點{low_52w:.1f}，注意支撐", "bullish": False
            })

        if ma_pattern:
            patterns.append(ma_pattern)

        # 計算支撐與壓力
        recent_lows  = sorted(lows[-20:])
        recent_highs = sorted(highs[-20:], reverse=True)
        support    = round(float(np.mean(recent_lows[:3])),  1) if recent_lows  else 0.0
        resistance = round(float(np.mean(recent_highs[:3])), 1) if recent_highs else 0.0

        # 目標價：依型態計算
        target = _calc_target(patterns, current, support, resistance, closes)

        # AI 解讀
        ai_interp = _ai_interpret(patterns, ma_status, current, support, resistance, target)

        return PatternResult(
            stock_id=code,
            stock_name=name,
            patterns=patterns,
            ma_status=ma_status,
            support=support,
            resistance=resistance,
            target_price=target,
            ai_interpretation=ai_interp,
            current_price=current,
        )
    except Exception as e:
        logger.error("[pattern_service] %s: %s", code, e)
        return None


def _detect_head_shoulders(closes, highs, lows):
    """偵測頭肩頂/底"""
    if len(closes) < 30:
        return None
    # 找3個局部高點（頭肩頂）
    peaks = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            peaks.append((i, highs[i]))
    if len(peaks) >= 3:
        p1, p2, p3 = peaks[-3], peaks[-2], peaks[-1]
        if p2[1] > p1[1] * 1.02 and p2[1] > p3[1] * 1.02 and abs(p1[1] - p3[1]) / p2[1] < 0.05:
            return {"name": "頭肩頂", "confidence": 75,
                    "description": f"頸線附近頭肩頂型態，目標跌幅約頭部至頸線距離", "bullish": False}
    # 找3個局部低點（頭肩底）
    troughs = []
    for i in range(2, len(lows) - 2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            troughs.append((i, lows[i]))
    if len(troughs) >= 3:
        t1, t2, t3 = troughs[-3], troughs[-2], troughs[-1]
        if t2[1] < t1[1] * 0.98 and t2[1] < t3[1] * 0.98 and abs(t1[1] - t3[1]) / t2[1] < 0.05:
            return {"name": "頭肩底", "confidence": 75,
                    "description": "頭肩底型態成形，若突破頸線可望反彈", "bullish": True}
    return None


def _detect_double_top_bottom(closes, highs, lows):
    """偵測雙頂/雙底"""
    if len(closes) < 20:
        return None
    peaks = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            peaks.append((i, highs[i]))
    if len(peaks) >= 2:
        p1, p2 = peaks[-2], peaks[-1]
        if abs(p1[1] - p2[1]) / p1[1] < 0.03 and p2[0] - p1[0] > 5:
            return {"name": "雙頂", "confidence": 70,
                    "description": f"雙頂型態，壓力約{max(p1[1],p2[1]):.1f}，需注意跌破支撐", "bullish": False}
    troughs = []
    for i in range(2, len(lows) - 2):
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            troughs.append((i, lows[i]))
    if len(troughs) >= 2:
        t1, t2 = troughs[-2], troughs[-1]
        if abs(t1[1] - t2[1]) / t1[1] < 0.03 and t2[0] - t1[0] > 5:
            return {"name": "雙底", "confidence": 70,
                    "description": f"雙底支撐約{min(t1[1],t2[1]):.1f}，突破頸線可望上漲", "bullish": True}
    return None


def _detect_triangle(closes, highs, lows):
    """偵測三角收斂"""
    if len(closes) < 15:
        return None
    import numpy as np
    n = len(closes)
    x = list(range(n))
    high_slope = float(np.polyfit(x, highs, 1)[0])
    low_slope  = float(np.polyfit(x, lows,  1)[0])
    if high_slope < -0.05 and low_slope > 0.05:
        return {"name": "對稱三角收斂", "confidence": 65,
                "description": "高低點收斂，即將選擇方向突破", "bullish": None}
    if high_slope < -0.05 and abs(low_slope) < 0.03:
        return {"name": "下降三角", "confidence": 65,
                "description": "高點持續下降，低點平坦，偏空型態", "bullish": False}
    if abs(high_slope) < 0.03 and low_slope > 0.05:
        return {"name": "上升三角", "confidence": 65,
                "description": "低點持續上升，高點平坦，偏多型態", "bullish": True}
    return None


def _detect_ma_break(closes):
    """偵測均線突破"""
    if len(closes) < 60:
        return None, "資料不足"
    ma5  = sum(closes[-5:])  / 5
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    cur  = closes[-1]
    if cur > ma5 > ma20 > ma60:
        status = f"多頭排列（均線全部上彎）"
        return {"name": "多頭排列", "confidence": 80,
                "description": f"5MA>{ma5:.1f} > 20MA>{ma20:.1f} > 60MA>{ma60:.1f}，趨勢向上", "bullish": True}, status
    if cur < ma5 < ma20 < ma60:
        status = f"空頭排列（均線全部下彎）"
        return {"name": "空頭排列", "confidence": 80,
                "description": f"股價跌破所有均線，趨勢向下", "bullish": False}, status
    if cur > ma20 and closes[-5] < ma20:
        return {"name": "突破20日均線", "confidence": 72,
                "description": f"剛突破20MA({ma20:.1f})，短線偏多", "bullish": True}, f"剛突破20MA"
    if cur < ma20 and closes[-5] > ma20:
        return {"name": "跌破20日均線", "confidence": 72,
                "description": f"剛跌破20MA({ma20:.1f})，短線偏空", "bullish": False}, f"剛跌破20MA"
    return None, f"MA5:{ma5:.1f} MA20:{ma20:.1f} MA60:{ma60:.1f}"


def _calc_target(patterns, current, support, resistance, closes):
    """計算目標價"""
    for p in patterns:
        if p.get("bullish") and resistance > current:
            height = resistance - support
            return round(resistance + height * 0.5, 1)
        if p.get("bullish") is False and support < current:
            height = resistance - support
            return round(support - height * 0.3, 1)
    return None


def _ai_interpret(patterns, ma_status, current, support, resistance, target):
    """AI 解讀型態"""
    if not patterns:
        return f"目前無明顯型態，股價在支撐{support:.1f}~壓力{resistance:.1f}區間震盪"
    bullish = [p for p in patterns if p.get("bullish") is True]
    bearish = [p for p in patterns if p.get("bullish") is False]
    if len(bullish) > len(bearish):
        main = bullish[0]["name"]
        tip  = f"目標看{target:.1f}" if target else f"留意壓力{resistance:.1f}"
        return f"偏多！{main}型態，{tip}，跌破{support:.1f}停損"
    elif len(bearish) > len(bullish):
        main = bearish[0]["name"]
        tip  = f"目標看{target:.1f}" if target else f"注意支撐{support:.1f}"
        return f"偏空！{main}型態，{tip}，反彈至{resistance:.1f}可減碼"
    return f"型態混沌，等待方向確認（支撐{support:.1f}、壓力{resistance:.1f}）"


def format_pattern(result: PatternResult) -> str:
    """格式化型態報告"""
    lines = [
        f"📐 K線型態｜{result.stock_name}（{result.stock_id}）",
        f"現價：{result.current_price:.1f}",
        "─" * 22,
    ]
    if result.patterns:
        lines.append("🔍 偵測到的型態：")
        for p in result.patterns[:4]:
            icon = "📈" if p.get("bullish") else ("📉" if p.get("bullish") is False else "📊")
            lines.append(f"  {icon} {p['name']}（信心{p['confidence']}%）")
            lines.append(f"     {p['description']}")
    else:
        lines.append("目前無明顯K線型態")
    lines += [
        "",
        f"📊 均線狀態：{result.ma_status}",
        f"🛡️ 支撐：{result.support:.1f}",
        f"🎯 壓力：{result.resistance:.1f}",
    ]
    if result.target_price:
        lines.append(f"🚀 目標價：{result.target_price:.1f}")
    lines += [
        "",
        f"🤖 AI解讀：{result.ai_interpretation}",
    ]
    return "\n".join(lines)
