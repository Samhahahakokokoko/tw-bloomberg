"""
Rich Menu 建立腳本 — 執行一次即可
用法：python -m line_webhook.setup_rich_menu
"""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from loguru import logger
from backend.models.database import settings
from line_webhook.rich_menu_image import create_rich_menu_image

IMAGE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "rich_menu.png")

RICH_MENU_DEF = {
    "size": {"width": 2500, "height": 1686},
    "selected": True,
    "name": "TW Bloomberg 主選單",
    "chatBarText": "📊 開啟操作選單",
    "areas": [
        # Row 0 ─ 大盤行情 (左上)
        {
            "bounds": {"x": 0, "y": 0, "width": 1250, "height": 562},
            "action": {"type": "message", "text": "/market"},
        },
        # Row 0 ─ 我的庫存 (右上)
        {
            "bounds": {"x": 1250, "y": 0, "width": 1250, "height": 562},
            "action": {"type": "message", "text": "/portfolio"},
        },
        # Row 1 ─ 設定警報 (左中)
        {
            "bounds": {"x": 0, "y": 562, "width": 1250, "height": 562},
            "action": {"type": "message", "text": "/alert_guide"},
        },
        # Row 1 ─ 市場新聞 (右中)
        {
            "bounds": {"x": 1250, "y": 562, "width": 1250, "height": 562},
            "action": {"type": "message", "text": "/news_guide"},
        },
        # Row 2 ─ AI 分析 (左下)
        {
            "bounds": {"x": 0, "y": 1124, "width": 1250, "height": 562},
            "action": {"type": "message", "text": "/ai_guide"},
        },
        # Row 2 ─ 更多指令 (右下)
        {
            "bounds": {"x": 1250, "y": 1124, "width": 1250, "height": 562},
            "action": {"type": "message", "text": "/help"},
        },
    ],
}


async def setup():
    token = settings.line_channel_access_token
    if not token:
        logger.error("LINE_CHANNEL_ACCESS_TOKEN not set")
        return

    headers = {"Authorization": f"Bearer {token}"}

    # 1. 先刪除舊的 rich menu
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get("https://api.line.me/v2/bot/richmenu/list", headers=headers)
        existing = r.json().get("richmenus", [])
        for menu in existing:
            mid = menu["richMenuId"]
            await client.delete(f"https://api.line.me/v2/bot/richmenu/{mid}", headers=headers)
            logger.info(f"Deleted old rich menu: {mid}")

    # 2. 生成圖片
    logger.info("Generating rich menu image...")
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

    # 5. 設為預設選單
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
            headers=headers,
        )
        r.raise_for_status()
        logger.success(f"✅ Rich menu set as default: {rich_menu_id}")

    print(f"\n✅ Rich Menu 建立完成！ID: {rich_menu_id}")
    print("所有 LINE Bot 使用者底部將出現六格選單。")


if __name__ == "__main__":
    asyncio.run(setup())
