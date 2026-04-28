"""Agent C — 決策員：綜合評分後，用 Claude 產生推薦理由與信心指數

每日 19:00 對前 30 高分股票產生 AI 推薦理由，並寫回 stock_scores。
"""
import asyncio
from datetime import date, datetime
from loguru import logger
from sqlalchemy import select

from ..models.database import AsyncSessionLocal, settings
from ..models.models import StockScore
from .screener_engine import get_top_scores


BATCH_SIZE = 5       # 每次批次送給 Claude（節省 tokens）
MAX_STOCKS = 30      # 最多產生 AI 理由的股票數


async def _generate_batch_reasons(stocks: list[dict]) -> dict[str, tuple[str, float]]:
    """
    對一批股票（最多 BATCH_SIZE）請 Claude 產生推薦理由。
    回傳 {stock_code: (ai_reason, confidence)}
    """
    if not settings.anthropic_api_key:
        return {}

    lines = []
    for s in stocks:
        lines.append(
            f"{s['stock_code']} {s['stock_name']} "
            f"總分:{s['total_score']} "
            f"基本:{s['fundamental_score']} 籌碼:{s['chip_score']} 技術:{s['technical_score']}\n"
            f"  營收YoY:{s['revenue_yoy']}% 毛利率:{s['gross_margin']}% "
            f"三率齊升:{s['three_margins_up']}\n"
            f"  外資連買:{s['foreign_consec_buy']}日 投信:{s['trust_consec_buy']}日\n"
            f"  均線多頭:{s['ma_aligned']} KD交叉:{s['kd_golden_cross']} "
            f"量能突破:{s['vol_breakout']}"
        )

    prompt = (
        "以下是今日台股選股分析結果，請為每檔股票用繁體中文寫出：\n"
        "1. 2-3句推薦理由（強調最突出的指標）\n"
        "2. 操作建議（買進/觀察/等待回測）\n"
        "格式：代碼: [理由] | 建議: [操作]\n\n"
        + "\n---\n".join(lines)
    )

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text

        # 解析 Claude 回傳格式
        results: dict[str, tuple[str, float]] = {}
        for line in raw.split("\n"):
            line = line.strip()
            for s in stocks:
                code = s["stock_code"]
                if line.startswith(f"{code}:") or line.startswith(f"{code} "):
                    # 簡單提取理由
                    reason_part = line[len(code):].strip().lstrip(":").strip()
                    # 信心指數：總分 + bonus
                    conf = min(99.0, s["total_score"] * 1.1)
                    results[code] = (reason_part, round(conf, 1))
                    break
        return results

    except Exception as e:
        logger.error(f"[AgentC] Claude error: {e}")
        return {}


async def _save_ai_reasons(reasons: dict[str, tuple[str, float]], today: str):
    # Bug fix: commit 必須在 async with 區塊內，否則 session 已關閉
    async with AsyncSessionLocal() as db:
        for code, (reason, conf) in reasons.items():
            r = await db.execute(
                select(StockScore).where(
                    StockScore.stock_code == code,
                    StockScore.score_date == today,
                )
            )
            rec = r.scalar_one_or_none()
            if rec:
                rec.ai_reason  = reason
                rec.confidence = conf
                rec.updated_at = datetime.utcnow()
        await db.commit()  # 在 with 內 — 正確


async def run_ai_decision():
    """每日 19:00 執行：為高分股票產生 AI 推薦理由，並存入 recommendation_results"""
    today = date.today().strftime("%Y-%m-%d")
    logger.info("[AgentC] 決策員啟動...")

    top = await get_top_scores(limit=MAX_STOCKS)
    if not top:
        logger.info("[AgentC] 無評分資料，跳過")
        return

    # 分批產生 AI 推薦理由
    all_reasons: dict[str, tuple[str, float]] = {}
    for i in range(0, len(top), BATCH_SIZE):
        batch = top[i:i + BATCH_SIZE]
        reasons = await _generate_batch_reasons(batch)
        all_reasons.update(reasons)
        if i + BATCH_SIZE < len(top):
            await asyncio.sleep(3)

    await _save_ai_reasons(all_reasons, today)

    # 把 AI 理由回填進 top 資料後存入推薦追蹤表
    for s in top:
        code = s["stock_code"]
        if code in all_reasons:
            s["ai_reason"], s["confidence"] = all_reasons[code]

    try:
        from .recommendation_tracker import save_recommendations
        await save_recommendations(top, today)
    except Exception as e:
        logger.error(f"[AgentC] save_recommendations error: {e}")

    logger.info(f"[AgentC] 完成：產生 {len(all_reasons)} 檔 AI 理由")


async def generate_nl_recommendation(query: str, results: list[dict]) -> str:
    """
    針對自然語言查詢結果，讓 Claude 產生一段總結說明。
    """
    if not settings.anthropic_api_key or not results:
        return ""
    try:
        import anthropic
        top5 = results[:5]
        summary = "\n".join(
            f"- {r['stock_code']} {r['stock_name']}: 總分{r['total_score']} "
            f"(基:{r['fundamental_score']} 籌:{r['chip_score']} 技:{r['technical_score']})"
            for r in top5
        )
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": (
                    f"用戶查詢：「{query}」\n\n"
                    f"篩選結果（前5名）：\n{summary}\n\n"
                    "請用繁體中文說明：\n"
                    "1. 這批標的共同特徵\n"
                    "2. 最值得關注的 1-2 檔及原因\n"
                    "3. 操作注意事項（限 3-4句）"
                ),
            }],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.error(f"[AgentC] NL recommendation error: {e}")
        return ""
