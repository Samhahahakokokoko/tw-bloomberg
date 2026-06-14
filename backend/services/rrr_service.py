"""RRR Service — 風險報酬比計算器 + 凱利公式倉位建議"""
from __future__ import annotations


def calc_rrr(code: str, entry: float, stop: float, target: float,
             win_rate: float = 0.5, account_size: float = 1_000_000) -> dict:
    if entry <= 0 or stop <= 0 or target <= 0:
        return {"error": "價格參數不能為 0"}
    risk   = abs(entry - stop)
    reward = abs(target - entry)
    rrr    = round(reward / risk, 2) if risk > 0 else 0
    risk_pct   = round(risk / entry * 100, 2)
    reward_pct = round(reward / entry * 100, 2)

    # 半凱利公式 f* = (p*b - q) / b / 2
    q = 1 - win_rate
    b = rrr
    kelly_full = (win_rate * b - q) / b if b > 0 else 0
    kelly_half = kelly_full / 2
    kelly_pct  = round(max(0, min(0.30, kelly_half)) * 100, 1)
    position_value = round(account_size * kelly_pct / 100, 0)

    verdict = _ai_verdict(rrr, risk_pct, reward_pct, kelly_pct, win_rate)
    rating  = _trade_rating(rrr, win_rate)

    return {
        "code": code, "entry": entry, "stop": stop, "target": target,
        "risk": round(risk, 1), "reward": round(reward, 1), "rrr": rrr,
        "risk_pct": risk_pct, "reward_pct": reward_pct,
        "win_rate_assumed": win_rate,
        "kelly_pct": kelly_pct,
        "position_value": int(position_value),
        "rating": rating, "verdict": verdict,
    }


def _trade_rating(rrr: float, win_rate: float) -> str:
    ev = win_rate * rrr - (1 - win_rate)
    if rrr >= 3 and ev > 0.5:  return "⭐⭐⭐ 優質交易"
    if rrr >= 2 and ev > 0:    return "⭐⭐ 值得考慮"
    if rrr >= 1.5:             return "⭐ 勉強可接受"
    return "❌ 不建議（RRR 過低）"


def _ai_verdict(rrr: float, risk_pct: float, reward_pct: float,
                kelly_pct: float, win_rate: float) -> str:
    ev = win_rate * rrr - (1 - win_rate)
    wr_pct = win_rate * 100
    if rrr < 1:
        return f"RRR {rrr:.1f} 低於 1，虧損結構不佳，建議重新設定停損或目標。"
    if rrr < 1.5:
        return f"RRR {rrr:.1f} 偏低。若無高勝率（>65%）支撐，不建議進場。"
    if rrr < 2:
        return f"RRR {rrr:.1f} 尚可，期望值 {ev:+.2f}。謹慎可做，建議倉位不超過 {kelly_pct:.0f}%。"
    if rrr < 3:
        return (f"RRR {rrr:.1f}，值得操作。停損 -{risk_pct:.1f}% 控制良好，"
                f"目標 +{reward_pct:.1f}%。建議倉位 {kelly_pct:.0f}%（半凱利）。")
    return (f"RRR {rrr:.1f}（優質），期望值 {ev:+.2f}。"
            f"積極可做，建議倉位 {kelly_pct:.0f}%，嚴守停損 -{risk_pct:.1f}%。")


def format_rrr_report(result: dict) -> str:
    if result.get("error"):
        return f"❌ {result['error']}"
    code    = result["code"]
    entry   = result["entry"];  stop   = result["stop"];   target = result["target"]
    risk    = result["risk"];   reward = result["reward"]; rrr    = result["rrr"]
    rp      = result["risk_pct"]; rwp  = result["reward_pct"]
    kelly   = result["kelly_pct"]; pos_v = result["position_value"]
    rating  = result["rating"]; verdict = result["verdict"]
    wr      = result["win_rate_assumed"]

    def _bar(a, b, w=16):
        total = a + b or 1
        ra = int(a / total * w); rb = w - ra
        return "🔴" * ra + "🟢" * rb

    lines = [
        f"⚖️ 風險報酬比  {code}",
        "─" * 32, "",
        f"進場：{entry:>10,.1f}",
        f"停損：{stop:>10,.1f}  (-{rp:.1f}%)",
        f"目標：{target:>10,.1f}  (+{rwp:.1f}%)",
        "",
        f"RRR：1 : {rrr:.2f}",
        f"  損:{_bar(risk, reward)}:益",
        f"  -{rp:.1f}%  vs  +{rwp:.1f}%",
        "",
        "─" * 28,
        "📐 倉位建議（半凱利公式）",
        f"假設勝率：{wr*100:.0f}%",
        f"建議倉位：{kelly:.1f}%",
        f"建議投入：約 {pos_v:,.0f} 元",
        "",
        "─" * 28,
        f"評級：{rating}",
        "",
        "🤖 AI 研判",
        verdict,
        "",
        "⚠️ 凱利公式基於假設勝率，請依自身策略調整",
    ]
    return "\n".join(lines)
