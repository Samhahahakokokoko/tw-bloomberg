"""
Rich Menu 建立腳本 — 6格 3×2 佈局 v5
上排 3 格（各 833px）：📊大盤行情  💼我的庫存  🔍今日選股
下排 3 格（各 833px）：📰市場新聞  🤖AI分析   ⚙️更多功能

用法：python -m line_webhook.setup_rich_menu
"""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from loguru import logger
from backend.models.database import settings
from line_webhook.rich_menu_image import create_rich_menu_image

IMAGE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "rich_menu.png")

CW = 833   # 每格寬（2500 / 3）
CH = 843   # 每格高（1686 / 2）

RICH_MENU_DEF = {
    "size": {"width": 2500, "height": 1686},
    "selected": True,
    "name": "TW Bloomberg 主選單 v5",
    "chatBarText": "📊 開啟操作選單",
    "areas": [
        # ── 上排（各 833px 寬）────────────────────────────────────────
        # 📊 大盤行情
        {
            "bounds": {"x": 0, "y": 0, "width": CW, "height": CH},
            "action": {
                "type": "postback",
                "data": "act=market_card",
                "displayText": "📊 大盤行情",
            },
        },
        # 💼 我的庫存
        {
            "bounds": {"x": CW, "y": 0, "width": CW, "height": CH},
            "action": {
                "type": "postback",
                "data": "act=portfolio_view",
                "displayText": "💼 我的庫存",
            },
        },
        # 🔍 今日選股
        {
            "bounds": {"x": CW * 2, "y": 0, "width": CW, "height": CH},
            "action": {
                "type": "postback",
                "data": "act=screener_qr",
                "displayText": "🔍 今日選股",
            },
        },
        # ── 下排（各 833px 寬）────────────────────────────────────────
        # 📰 市場新聞
        {
            "bounds": {"x": 0, "y": CH, "width": CW, "height": CH},
            "action": {"type": "message", "text": "/n"},
        },
        # 🤖 AI分析
        {
            "bounds": {"x": CW, "y": CH, "width": CW, "height": CH},
            "action": {
                "type": "postback",
                "data": "act=ai_menu",
                "displayText": "🤖 AI 分析選單",
            },
        },
        # ⚙️ 更多功能
        {
            "bounds": {"x": CW * 2, "y": CH, "width": CW, "height": CH},
            "action": {
                "type": "postback",
                "data": "act=more_menu_v2",
                "displayText": "⚙️ 更多功能",
            },
        },
    ],
}


async def setup():
    token = settings.line_channel_access_token
    if not token:
        logger.error("LINE_CHANNEL_ACCESS_TOKEN not set")
        return

    headers = {"Authorization": f"Bearer {token}"}

    # 1. 刪除舊的 rich menu
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get("https://api.line.me/v2/bot/richmenu/list", headers=headers)
        for menu in r.json().get("richmenus", []):
            mid = menu["richMenuId"]
            await client.delete(f"https://api.line.me/v2/bot/richmenu/{mid}",
                                 headers=headers)
            logger.info(f"Deleted old rich menu: {mid}")

    # 2. 生成圖片
    logger.info("Generating rich menu image (6-button 3×2 layout)...")
    img_path = create_rich_menu_image(IMAGE_PATH)

    # 3. 建立 rich menu
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.line.me/v2/bot/richmenu",
            json=RICH_MENU_DEF,
            headers={**headers, "Content-Type": "application/json"},
        )
        r.raise_for_status()
        rich_menu_id = r.json()["richMenuId"]
        logger.info(f"Created rich menu: {rich_menu_id}")

    # 4. 上傳圖片
    async with httpx.AsyncClient(timeout=60) as client:
        with open(img_path, "rb") as f:
            img_data = f.read()
        r = await client.post(
            f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
            content=img_data,
            headers={**headers, "Content-Type": "image/png"},
        )
        r.raise_for_status()
        logger.info(f"Uploaded rich menu image ({len(img_data)//1024} KB)")

    # 5. 設為預設
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
            headers=headers,
        )
        r.raise_for_status()
        logger.success(f"Rich menu set as default: {rich_menu_id}")

    print(f"\nRich Menu 6格佈局建立完成 ID: {rich_menu_id}")
    print("上排: 大盤行情 / 我的庫存 / 今日選股")
    print("下排: 市場新聞 / AI分析   / 更多功能")


if __name__ == "__main__":
    asyncio.run(setup())
