"""生成 Rich Menu 圖片 — 終端機深色風格，2500x1686 六格佈局"""
from __future__ import annotations
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import os, math

W, H   = 2500, 1686
COLS   = 2
ROWS   = 3
CW, CH = W // COLS, H // ROWS      # 1250 × 562

# ── 配色 ──────────────────────────────────────────────────────────────────────
BG      = (10,  14,  26)
SURFACE = (14,  20,  38)
BORDER  = (28,  52,  88)
WHITE   = (210, 228, 248)
MUTED   = (70,  92,  124)
DARK    = (8,   12,  22)

PALETTE = [
    (0,   210, 255),   # 01 cyan   — 大盤行情
    (0,   230, 118),   # 02 green  — 我的庫存
    (255, 200,   0),   # 03 yellow — 設定警報
    (82,  140, 255),   # 04 blue   — 市場新聞
    (190, 120, 255),   # 05 purple — AI 分析
    (80,  110, 160),   # 06 steel  — 更多指令
]

BUTTONS = [
    ("大盤行情", "即時指數  三大法人",  "chart",  0, 0),
    ("我的庫存", "持股損益  倉位總覽",  "wallet", 1, 0),
    ("設定警報", "到價通知  漲跌警報",  "bell",   0, 1),
    ("市場新聞", "財經快訊  情緒分析",  "news",   1, 1),
    ("AI  分析", "智能建議  策略回測",  "ai",     0, 2),
    ("更多指令", "查看完整操作手冊",     "menu",   1, 2),
]

FONT_PATH = "C:/Windows/Fonts/msjh.ttc"


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size)


# ── 圖示 ─────────────────────────────────────────────────────────────────────

def _draw_chart(draw: ImageDraw.ImageDraw, cx, cy, c, s=76):
    bars  = [0.42, 0.68, 1.0, 0.58, 0.80]
    bw    = int(s * 0.28)
    gap   = int(s * 0.14)
    total = len(bars) * bw + (len(bars) - 1) * gap
    x0    = cx - total // 2
    baseline = cy + s // 2
    for i, ratio in enumerate(bars):
        bh = int(s * ratio)
        x  = x0 + i * (bw + gap)
        draw.rectangle([x, baseline - bh, x + bw, baseline], fill=c)
    draw.rectangle([x0 - 4, baseline + 2, x0 + total + 4, baseline + 7], fill=c)


def _draw_wallet(draw: ImageDraw.ImageDraw, cx, cy, c, s=76):
    w, h = int(s * 1.35), int(s * 0.88)
    r    = 14
    draw.rounded_rectangle([cx-w//2, cy-h//2, cx+w//2, cy+h//2], radius=r, fill=SURFACE, outline=c, width=5)
    # 錢包口袋
    pw, ph = int(s * 0.38), int(s * 0.30)
    draw.rounded_rectangle(
        [cx + w//2 - pw - 10, cy - ph//2, cx + w//2 - 10, cy + ph//2],
        radius=8, fill=DARK, outline=c, width=4,
    )
    draw.ellipse([cx+w//2-pw//2-28, cy-10, cx+w//2-pw//2-4, cy+14], fill=c)
    # 卡片線條
    for dy in [-int(s*0.22), 0]:
        draw.rectangle([cx-w//2+20, cy+dy-3, cx-w//2+20+int(s*0.5), cy+dy+4], fill=c)


def _draw_bell(draw: ImageDraw.ImageDraw, cx, cy, c, s=76):
    # 鐘身
    pts = []
    for deg in range(0, 181, 10):
        rad = math.radians(deg)
        pts.append((cx + int(s*0.55*math.cos(rad)), cy - int(s*0.5*math.sin(rad))))
    flat_y = cy + s // 4
    pts = [(cx - int(s*0.55), flat_y)] + pts + [(cx + int(s*0.55), flat_y)]
    draw.polygon(pts, fill=c)
    draw.rectangle([cx - int(s*0.55), flat_y, cx + int(s*0.55), flat_y + 10], fill=c)
    # 手柄
    draw.arc([cx-int(s*0.2), cy-int(s*0.7), cx+int(s*0.2), cy-int(s*0.1)],
             start=200, end=340, fill=c, width=6)
    # 錘頭
    draw.ellipse([cx-int(s*0.16), flat_y+10, cx+int(s*0.16), flat_y+10+int(s*0.28)], fill=c)
    # 通知小點
    draw.ellipse([cx+int(s*0.3), cy-int(s*0.38), cx+int(s*0.56), cy-int(s*0.12)], fill=(220, 50, 50))


def _draw_news(draw: ImageDraw.ImageDraw, cx, cy, c, s=76):
    pw, ph = int(s*1.25), int(s*1.1)
    # 紙張
    draw.rounded_rectangle([cx-pw//2, cy-ph//2, cx+pw//2, cy+ph//2], radius=10, fill=SURFACE, outline=c, width=4)
    # 標題列
    draw.rectangle([cx-pw//2+14, cy-ph//2+14, cx+pw//2-14, cy-ph//2+52], fill=c)
    # 文字行
    for i, (ratio, dy) in enumerate([(0.85, 72), (0.6, 112), (0.85, 152), (0.5, 192), (0.75, 232)]):
        lw = int((pw-28) * ratio)
        color = c if i == 0 else (*c[:3],) if ratio > 0.7 else MUTED
        draw.rectangle([cx-pw//2+14, cy-ph//2+dy, cx-pw//2+14+lw, cy-ph//2+dy+7], fill=color)


def _draw_ai(draw: ImageDraw.ImageDraw, cx, cy, c, s=76):
    r = int(s * 0.56)
    # 外圓
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], outline=c, width=6)
    # 內部電路格線
    for dx in [-r//2, r//2]:
        draw.line([cx+dx, cy-r, cx+dx, cy+r], fill=BORDER, width=3)
    for dy in [-r//2, r//2]:
        draw.line([cx-r, cy+dy, cx+r, cy+dy], fill=BORDER, width=3)
    # 節點
    for nx, ny in [(-r//2,-r//2),(r//2,-r//2),(-r//2,r//2),(r//2,r//2),(0,-r//2),(0,r//2),(-r//2,0),(r//2,0)]:
        draw.ellipse([cx+nx-7, cy+ny-7, cx+nx+7, cy+ny+7], fill=c)
    # 中心
    draw.ellipse([cx-14, cy-14, cx+14, cy+14], fill=c)
    # 外接腳
    for angle in [0, 90, 180, 270]:
        rad = math.radians(angle)
        x1 = cx + int(r * math.cos(rad))
        y1 = cy + int(r * math.sin(rad))
        x2 = cx + int((r + 26) * math.cos(rad))
        y2 = cy + int((r + 26) * math.sin(rad))
        draw.line([x1, y1, x2, y2], fill=c, width=5)
        draw.ellipse([x2-9, y2-9, x2+9, y2+9], fill=c)


def _draw_menu(draw: ImageDraw.ImageDraw, cx, cy, c, s=76):
    widths = [1.0, 0.7, 0.88]
    for i, ratio in enumerate(widths):
        y  = cy - s//3 + i * (s//3)
        lw = int(s * ratio)
        draw.rounded_rectangle([cx-lw//2, y-7, cx+lw//2, y+7], radius=7, fill=c)
    # 小圓點裝飾
    for i, ratio in enumerate(widths):
        y  = cy - s//3 + i * (s//3)
        lw = int(s * ratio)
        draw.ellipse([cx+lw//2+10, y-9, cx+lw//2+28, y+9], outline=c, width=3)


ICON_FN = {
    "chart":  _draw_chart,
    "wallet": _draw_wallet,
    "bell":   _draw_bell,
    "news":   _draw_news,
    "ai":     _draw_ai,
    "menu":   _draw_menu,
}


# ── 主函式 ────────────────────────────────────────────────────────────────────

def create_rich_menu_image(output_path: str = None) -> str:
    if output_path is None:
        output_path = os.path.join(os.path.dirname(__file__), "..", "data", "rich_menu.png")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    f_title  = _font(94)
    f_sub    = _font(50)
    f_num    = _font(40)

    # Pass 1：底色 + 圖示 + 文字
    for idx, (title, subtitle, icon_key, col, row) in enumerate(BUTTONS):
        color  = PALETTE[idx]
        x0, y0 = col * CW, row * CH
        x1, y1 = x0 + CW, y0 + CH

        draw.rectangle([x0, y0, x1, y1], fill=SURFACE)

        # 暈染色彩從頂部向下淡出（8 層）
        for depth in range(8):
            fade = tuple(max(0, int(c * (1 - depth * 0.1))) for c in color)
            draw.rectangle([x0, y0 + depth*6, x1, y0 + (depth+1)*6], fill=fade)
        # 恢復中下部底色
        draw.rectangle([x0, y0 + 56, x1, y1], fill=SURFACE)

        # 圖示
        icon_cx = x0 + CW // 2
        icon_cy = y0 + int(CH * 0.37)
        ICON_FN[icon_key](draw, icon_cx, icon_cy, color)

        # 標題
        title_y = y0 + int(CH * 0.67)
        draw.text((x0 + CW // 2, title_y), title,
                  font=f_title, fill=WHITE, anchor="mm")
        draw.text((x0 + CW // 2, title_y + 78), subtitle,
                  font=f_sub, fill=MUTED, anchor="mm")

        # 角落編號
        draw.text((x0 + 22, y0 + 20), f"0{idx+1}",
                  font=f_num, fill=color, anchor="lt")

        # 右下小箭頭
        ax, ay = x1 - 34, y1 - 34
        draw.polygon([(ax-14, ay+4), (ax+4, ay+4), (ax+4, ay-14)], fill=color)

    # Pass 2：格線 + 外框（蓋在所有內容上）
    draw.rectangle([CW-2, 0, CW+2, H],     fill=BORDER)
    draw.rectangle([0, CH-2,   W, CH+2],   fill=BORDER)
    draw.rectangle([0, CH*2-2, W, CH*2+2], fill=BORDER)
    draw.rectangle([0, 0, W-1, H-1], outline=BORDER, width=5)

    # Pass 3：頂部色條最後畫（確保不被蓋住）
    for idx, (_, _, _, col, row) in enumerate(BUTTONS):
        color = PALETTE[idx]
        x0, y0 = col * CW, row * CH
        x1     = x0 + CW
        # 內縮 3px 避開格線
        draw.rectangle([x0 + 3, y0 + 3, x1 - 3, y0 + 9], fill=color)

    img.save(output_path, "PNG", optimize=True)
    print(f"Rich menu image: {output_path}  ({W}x{H})")
    return output_path


if __name__ == "__main__":
    create_rich_menu_image()
