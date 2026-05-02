"""
news_pipeline.py — 新聞完整 Pipeline

流程：
  爬蟲 → 摘要 → 情緒分析 → 生成圖片 → 上傳公開 URL → 推送 LINE → 記錄日誌

關鍵設計：
  - 每個步驟獨立 try/except，單步失敗不中斷整體
  - 圖片存入 static/reports/，透過 BASE_URL 公開服務
  - LINE push payload 控制在 5 KB 以內（最多 6 則新聞）
  - 所有步驟結果記錄到 pipeline_log 資料表
  - 無 BASE_URL 時自動降級為純文字推送

使用方式：
    from backend.services.news_pipeline import run_news_pipeline
    await run_news_pipeline()         # 完整 pipeline
    await run_news_pipeline(limit=3)  # 只推前 3 則
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL     = os.getenv("BASE_URL", "")
STATIC_DIR   = Path(os.getenv("STATIC_DIR", "./static/reports"))
MAX_NEWS     = 6        # LINE 推送最多 N 則
MAX_TITLE_LEN = 36      # 每則新聞標題截斷長度（控制 payload 大小）


# ── 步驟函式 ──────────────────────────────────────────────────────────────────

async def _step_scrape(run_id: str) -> list[dict]:
    """步驟 1：爬取最新新聞"""
    try:
        from scraper.news_scraper import scrape_all, get_recent_news
        saved = await scrape_all()
        news  = await get_recent_news(limit=MAX_NEWS)
        await _log(run_id, "scrape", "ok", f"爬取 {len(saved)} 篇，取用 {len(news)} 則", len(news))
        return news
    except Exception as e:
        logger.error("[pipeline] scrape 失敗: %s", e)
        await _log(run_id, "scrape", "fail", str(e)[:200])
        return []


async def _step_summarize(run_id: str, news: list[dict]) -> list[dict]:
    """
    步驟 2：為每則新聞生成 ≤ 30 字摘要（Claude API）。
    若 API key 未設定，直接使用標題。
    """
    from backend.models.database import settings

    api_key = getattr(settings, "anthropic_api_key", "") or ""
    if not api_key:
        await _log(run_id, "summarize", "skip", "無 API key，使用標題")
        return news  # 直接用原標題

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        enriched: list[dict] = []
        for item in news[:MAX_NEWS]:
            try:
                raw    = item.get("title", "") + "\n" + (item.get("content") or "")[:200]
                msg    = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=60,
                    messages=[{"role": "user",
                               "content": f"一句話總結（15字以內，不加標點符號）：\n{raw[:300]}"}],
                )
                summary = (msg.content[0].text if msg.content else "").strip()[:30]
                if summary:
                    item = dict(item, summary=summary)
            except Exception:
                pass
            enriched.append(item)
        await _log(run_id, "summarize", "ok", f"摘要 {len(enriched)} 則")
        return enriched
    except Exception as e:
        logger.warning("[pipeline] summarize 失敗: %s", e)
        await _log(run_id, "summarize", "fail", str(e)[:200])
        return news


async def _step_gen_image(run_id: str, news: list[dict]) -> Optional[Path]:
    """
    步驟 3：生成新聞摘要圖片（matplotlib 深色風格）。
    失敗時回傳 None（降級到純文字）。
    """
    if not news:
        return None
    try:
        STATIC_DIR.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = STATIC_DIR / f"news_{ts}.png"

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch

        BG = "#0A0F1E"; HDR = "#060B14"; ROW_ODD = "#0D1525"; ROW_EVEN = "#0A1020"
        WHITE = "#E8EEF8"; MUTED = "#6A7E9C"; BORDER = "#1C2E48"
        CLR_POS = "#4ADE80"; CLR_NEG = "#FF4455"; CLR_NEU = "#7A8FA8"
        SENTI_COLOR = {"positive": CLR_POS, "negative": CLR_NEG, "neutral": CLR_NEU}
        SENTI_ARROW = {"positive": "📈", "negative": "📉", "neutral": "📊"}

        n     = min(len(news), MAX_NEWS)
        fig_w = 12.0
        row_h = 0.80
        hdr_h = 0.70
        fig_h = hdr_h + n * row_h + 0.25

        fig, ax = plt.subplots(figsize=(fig_w, fig_h))
        ax.set_xlim(0, fig_w); ax.set_ylim(0, fig_h); ax.axis("off")
        fig.patch.set_facecolor(BG)

        y = fig_h
        ax.add_patch(FancyBboxPatch((0, y - hdr_h), fig_w, hdr_h,
                                    boxstyle="square,pad=0", lw=0, facecolor=HDR))
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        ax.text(fig_w / 2, y - hdr_h / 2,
                f"📰 財經新聞摘要  ▏  {date_str}",
                ha="center", va="center", fontsize=11,
                fontweight="bold", color=WHITE)
        y -= hdr_h

        for i, item in enumerate(news[:n]):
            bg = ROW_ODD if i % 2 == 0 else ROW_EVEN
            ry = y - i * row_h
            ax.add_patch(FancyBboxPatch((0, ry - row_h), fig_w, row_h,
                                        boxstyle="square,pad=0", lw=0, facecolor=bg))
            senti  = item.get("sentiment", "neutral")
            icon   = SENTI_ARROW.get(senti, "📊")
            clr    = SENTI_COLOR.get(senti, CLR_NEU)
            title  = item.get("title", "")[:MAX_TITLE_LEN]
            source = item.get("source", "")
            pub    = (item.get("published") or "")[:10]
            stocks = item.get("stocks", "") or ""
            stock_tag = f" [{stocks}]" if stocks else ""

            ax.text(0.20, ry - 0.25, icon, ha="left", va="center", fontsize=11, color=clr)
            ax.text(0.55, ry - 0.25, f"{title}{stock_tag}",
                    ha="left", va="center", fontsize=8.5, color=WHITE)
            ax.text(0.55, ry - 0.58, f"{source}  {pub}",
                    ha="left", va="center", fontsize=7.5, color=MUTED)
            ax.plot([0, fig_w], [ry - row_h, ry - row_h], color=BORDER, lw=0.4)

        plt.tight_layout(pad=0)
        fig.savefig(str(path), dpi=130, bbox_inches="tight",
                    facecolor=BG, edgecolor="none")
        plt.close(fig)
        await _log(run_id, "image", "ok", str(path.name))
        return path
    except Exception as e:
        logger.warning("[pipeline] gen_image 失敗: %s", e)
        await _log(run_id, "image", "fail", str(e)[:200])
        return None


def _build_text_payload(news: list[dict]) -> str:
    """生成純文字新聞摘要（< 5 KB）"""
    if not news:
        return "📰 今日暫無新聞\n\n財經新聞每 30 分鐘自動更新"

    emoji_map = {"positive": "📈", "negative": "📉", "neutral": "📊"}
    lines = [f"📰 最新財經新聞 ({len(news[:MAX_NEWS])} 則)", "─" * 20]

    for n in news[:MAX_NEWS]:
        e      = emoji_map.get(n.get("sentiment", "neutral"), "📊")
        title  = n.get("title", "")[:MAX_TITLE_LEN]
        pub    = (n.get("published") or "")[:10]
        stocks = n.get("stocks", "") or ""
        tag    = f" [{stocks}]" if stocks else ""
        lines.append(f"{e} {title}{tag}")
        if pub:
            lines.append(f"   {n.get('source', '')}  {pub}")

    text = "\n".join(lines)
    # 確保 < 5000 bytes（LINE 限制）
    return text[:4800]


async def _step_push(
    run_id:   str,
    news:     list[dict],
    img_path: Optional[Path],
    user_ids: list[str],
    token:    str,
) -> int:
    """
    步驟 4：推送 LINE 訊息。
    若有圖片且 BASE_URL 有效 → 推送圖片；否則推純文字。
    回傳成功推送人數。
    """
    if not user_ids or not token:
        await _log(run_id, "push", "skip", "無訂閱者或無 token")
        return 0

    success = 0
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type":  "application/json"}

    # 決定訊息格式
    messages: list[dict] = []

    if img_path and BASE_URL:
        image_url = f"{BASE_URL.rstrip('/')}/static/reports/{img_path.name}"
        messages.append({
            "type": "image",
            "originalContentUrl": image_url,
            "previewImageUrl":    image_url,
        })
        # 附加文字（簡短，1~2 則摘要）
        summary = _build_text_payload(news[:2])
        if len(summary.encode("utf-8")) < 4800:
            messages.append({"type": "text", "text": summary})
    else:
        # 純文字 fallback（payload < 5 KB）
        messages.append({"type": "text", "text": _build_text_payload(news)})

    async with httpx.AsyncClient(timeout=20) as client:
        for uid in user_ids:
            try:
                r = await client.post(
                    "https://api.line.me/v2/bot/message/push",
                    json={"to": uid, "messages": messages[:5]},
                    headers=headers,
                )
                if r.status_code == 200:
                    success += 1
                else:
                    logger.warning("[pipeline] push %s HTTP %d", uid[:8], r.status_code)
            except Exception as e:
                logger.error("[pipeline] push error uid=%s: %s", uid[:8], e)

    await _log(run_id, "push", "ok", f"推送 {success}/{len(user_ids)} 人")
    return success


# ── 主 Pipeline ───────────────────────────────────────────────────────────────

async def run_news_pipeline(
    limit:     int  = MAX_NEWS,
    push_line: bool = True,
) -> dict:
    """
    執行完整新聞 Pipeline。
    回傳執行摘要字典。
    """
    run_id  = uuid.uuid4().hex[:12]
    started = datetime.utcnow()
    result  = {"run_id": run_id, "started": str(started)[:19]}

    logger.info("[pipeline] 開始 run_id=%s", run_id)

    # ── Step 1: 爬蟲 ─────────────────────────────────────────────────
    news = await _step_scrape(run_id)
    result["scraped"] = len(news)
    if not news:
        result["status"] = "no_news"
        await _log(run_id, "done", "skip", "無新聞，pipeline 結束")
        return result

    # ── Step 2: 摘要 ─────────────────────────────────────────────────
    news = await _step_summarize(run_id, news[:limit])

    # ── Step 3: 生成圖片 ─────────────────────────────────────────────
    img_path = await _step_gen_image(run_id, news)
    result["image"] = str(img_path.name) if img_path else None

    # ── Step 4: 推送 LINE ─────────────────────────────────────────────
    pushed = 0
    if push_line:
        try:
            from backend.models.database import settings, AsyncSessionLocal
            from backend.models.models import Subscriber
            from sqlalchemy import select

            async with AsyncSessionLocal() as db:
                r = await db.execute(select(Subscriber))
                subs = r.scalars().all()
            user_ids = [s.line_user_id for s in subs if s.line_user_id]

            if user_ids:
                pushed = await _step_push(
                    run_id, news, img_path, user_ids,
                    settings.line_channel_access_token,
                )
            else:
                await _log(run_id, "push", "skip", "無訂閱者")
        except Exception as e:
            logger.error("[pipeline] push 步驟失敗: %s", e)
            await _log(run_id, "push", "fail", str(e)[:200])

    result["pushed"] = pushed

    elapsed = (datetime.utcnow() - started).total_seconds()
    result["elapsed_s"] = round(elapsed, 1)
    result["status"] = "ok"
    await _log(run_id, "done", "ok",
               f"完成 scrape={result['scraped']} pushed={pushed} {elapsed:.1f}s")

    logger.info("[pipeline] 完成 run_id=%s elapsed=%.1fs", run_id, elapsed)
    return result


# ── 日誌輔助 ──────────────────────────────────────────────────────────────────

async def _log(run_id: str, step: str, status: str,
               detail: str = "", articles: int = 0) -> None:
    """寫入 pipeline_log 資料表；失敗靜默處理"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import PipelineLog
        async with AsyncSessionLocal() as db:
            db.add(PipelineLog(
                run_id=run_id, step=step, status=status,
                detail=detail[:1000], articles=articles,
            ))
            await db.commit()
    except Exception as e:
        logger.debug("[pipeline] log write failed: %s", e)
