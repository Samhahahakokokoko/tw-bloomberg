"""生成 Rich Menu 圖片 — 深色主題，5格非對稱佈局
上排 2 格：📊市場 / ⭐自選股
下排 3 格：🤖AI策略 / 📈選股 / ⚙️工具
"""
from __future__ import annotations
from PIL import Image, ImageDraw, ImageFont
import os, math

W, H = 2500, 1686
CH   = H // 2        # 行高 843

# 上排：2 格，各 1250px
TOP_COLS   = 2
TOP_CW     = W // TOP_COLS   # 1250

# 下排：3 格，各 833/834/833px
BOT_COLS   = 3
BOT_WIDTHS = [833, 834, 833]

# ── 配色 ─────────────────────────────────────────────────────────────────────
BG      = (8,   13,  24)
SURFACE = (12,  19,  36)
BORDER  = (24,  42,  72)
WHITE   = (220, 235, 252)
MUTED   = (72,  96,  128)
DARK    = (6,   10,  18)

PALETTE = [
    (0,   210, 255),   # 01 cyan   — 市場
    (0,   230, 118),   # 02 green  — 自選股
    (200, 120, 255),   # 03 purple — AI策略
    (0,   180, 255),   # 04 blue   — 選股
    (100, 140, 200),   # 05 steel  — 工具
]

# (標題, 副標, icon_key, row, col)
BUTTONS = [
    ("市場",   "大盤・外資・情緒",   "chart",  0, 0),
    ("自選股", "庫存・損益・AI診斷", "wallet", 0, 1),
    ("AI策略", "推薦・多頭・空頭",   "ai",     1, 0),
    ("選股",   "動能・籌碼・突破",   "news",   1, 1),
    ("工具",   "回測・零股・風控",   "menu",   1, 2),
]

SHORTCUTS = ["", "/p", "", "/r", ""]


def _find_font() -> str:
    candidates = [
        "C:/Windows/Fonts/msjh.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return ""


FONT_PATH = _find_font()


def _font(size: int) -> ImageFont.FreeTypeFont:
    if FONT_PATH:
        return ImageFont.truetype(FONT_PATH, size)
    return ImageFont.load_default()


# ── 圖示繪製函式 ──────────────────────────────────────────────────────────────

def _draw_chart(draw, cx, cy, c, s=90):
    bars  = [0.42, 0.68, 1.0, 0.58, 0.80, 0.45, 0.72]
    bw    = int(s * 0.22); gap = int(s * 0.10)
    total = len(bars) * bw + (len(bars) - 1) * gap
    x0    = cx - total // 2; bl = cy + s // 2
    for i, r in enumerate(bars):
        bh = int(s * r); x = x0 + i * (bw + gap)
        draw.rectangle([x, bl - bh, x + bw, bl], fill=c)
    draw.rectangle([x0 - 4, bl + 2, x0 + total + 4, bl + 8], fill=c)
    pts = [(x0 + i*(bw+gap) + bw//2, bl - int(s*r)) for i, r in enumerate(bars)]
    for i in range(len(pts)-1):
        draw.line([pts[i], pts[i+1]], fill=(*c[:3], 160), width=4)


def _draw_wallet(draw, cx, cy, c, s=90):
    w, h = int(s * 1.4), int(s * 0.9)
    draw.rounded_rectangle([cx-w//2, cy-h//2, cx+w//2, cy+h//2], radius=16,
                            fill=SURFACE, outline=c, width=6)
    pw, ph = int(s*0.40), int(s*0.32)
    draw.rounded_rectangle([cx+w//2-pw-12, cy-ph//2, cx+w//2-12, cy+ph//2],
                            radius=10, fill=DARK, outline=c, width=5)
    draw.ellipse([cx+w//2-pw//2-32, cy-11, cx+w//2-pw//2-4, cy+13], fill=c)
    for dy in [-int(s*0.22), 0]:
        draw.rectangle([cx-w//2+22, cy+dy-4, cx-w//2+22+int(s*0.5), cy+dy+5], fill=c)


def _draw_ai(draw, cx, cy, c, s=90):
    r = int(s * 0.58)
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], outline=c, width=7)
    for dx in [-r//2, r//2]:
        draw.line([cx+dx, cy-r, cx+dx, cy+r], fill=BORDER, width=4)
    for dy in [-r//2, r//2]:
        draw.line([cx-r, cy+dy, cx+r, cy+dy], fill=BORDER, width=4)
    for nx, ny in [(-r//2,-r//2),(r//2,-r//2),(-r//2,r//2),(r//2,r//2),
                   (0,-r//2),(0,r//2),(-r//2,0),(r//2,0)]:
        draw.ellipse([cx+nx-8, cy+ny-8, cx+nx+8, cy+ny+8], fill=c)
    draw.ellipse([cx-15, cy-15, cx+15, cy+15], fill=c)
    for angle in [0, 90, 180, 270]:
        rad = math.radians(angle)
        x1 = cx + int(r * math.cos(rad)); y1 = cy + int(r * math.sin(rad))
        x2 = cx + int((r+30) * math.cos(rad)); y2 = cy + int((r+30) * math.sin(rad))
        draw.line([x1,y1,x2,y2], fill=c, width=6)
        draw.ellipse([x2-10, y2-10, x2+10, y2+10], fill=c)


def _draw_news(draw, cx, cy, c, s=90):
    pw, ph = int(s*1.30), int(s*1.15)
    draw.rounded_rectangle([cx-pw//2, cy-ph//2, cx+pw//2, cy+ph//2],
                            radius=12, fill=SURFACE, outline=c, width=5)
    draw.rectangle([cx-pw//2+16, cy-ph//2+16, cx+pw//2-16, cy-ph//2+56], fill=c)
    for i, (ratio, dy) in enumerate([(0.88,76),(0.62,118),(0.88,158),(0.52,198),(0.78,238)]):
        lw  = int((pw-32) * ratio)
        col = c if i % 2 == 0 else MUTED
        draw.rectangle([cx-pw//2+16, cy-ph//2+dy, cx-pw//2+16+lw, cy-ph//2+dy+8], fill=col)


def _draw_menu(draw, cx, cy, c, s=90):
    widths = [1.0, 0.72, 0.88]
    for i, ratio in enumerate(widths):
        y  = cy - s//3 + i * (s//3); lw = int(s * ratio)
        draw.rounded_rectangle([cx-lw//2, y-8, cx+lw//2, y+8], radius=8, fill=c)
    for i, ratio in enumerate(widths):
        y  = cy - s//3 + i * (s//3); lw = int(s * ratio)
        draw.ellipse([cx+lw//2+12, y-10, cx+lw//2+32, y+10], outline=c, width=4)


ICON_FN = {
    "chart":  _draw_chart,
    "wallet": _draw_wallet,
    "ai":     _draw_ai,
    "news":   _draw_news,
    "menu":   _draw_menu,
}


# ── 格子座標計算 ──────────────────────────────────────────────────────────────

def _cell_bounds(row: int, col: int) -> tuple[int, int, int, int]:
    """回傳 (x0, y0, x1, y1)"""
    if row == 0:
        x0 = col * TOP_CW
        return x0, 0, x0 + TOP_CW, CH
    else:
        x0 = sum(BOT_WIDTHS[:col])
        return x0, CH, x0 + BOT_WIDTHS[col], H


def create_rich_menu_image(output_path: str = None) -> str:
    if output_path is None:
        output_path = os.path.join(os.path.dirname(__file__), "..", "data", "rich_menu.png")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    f_title = _font(82)
    f_sub   = _font(44)
    f_short = _font(38)

    for idx, (title, subtitle, icon_key, row, col) in enumerate(BUTTONS):
        color       = PALETTE[idx]
        x0, y0, x1, y1 = _cell_bounds(row, col)
        cw = x1 - x0; ch_cell = y1 - y0

        # 底色
        draw.rectangle([x0, y0, x1, y1], fill=SURFACE)

        # 頂部暈染
        for depth in range(10):
            fade = tuple(max(0, int(c * (1 - depth * 0.09))) for c in color)
            draw.rectangle([x0, y0 + depth*5, x1, y0 + (depth+1)*5], fill=fade)
        draw.rectangle([x0, y0 + 56, x1, y1], fill=SURFACE)

        # 圖示
        icon_cx = x0 + cw // 2
        icon_cy = y0 + int(ch_cell * 0.38)
        ICON_FN[icon_key](draw, icon_cx, icon_cy, color)

        # 標題
        title_y = y0 + int(ch_cell * 0.68)
        draw.text((x0 + cw // 2, title_y), title,
                  font=f_title, fill=WHITE, anchor="mm")
        draw.text((x0 + cw // 2, title_y + 68), subtitle,
                  font=f_sub, fill=MUTED, anchor="mm")

        # 快捷標籤（右下角）
        shortcut = SHORTCUTS[idx]
        if shortcut:
            draw.text((x1 - 18, y1 - 18), shortcut,
                      font=f_short, fill=(*color, 180), anchor="rb")

        # 左上角編號
        draw.text((x0 + 20, y0 + 18), f"0{idx+1}",
                  font=f_short, fill=color, anchor="lt")

        # 右下角箭頭
        ax, ay = x1 - 36, y1 - 36
        draw.polygon([(ax-14, ay+4), (ax+4, ay+4), (ax+4, ay-14)], fill=color)

    # ── 格線 ─────────────────────────────────────────────────────────────────
    # 水平中線
    draw.rectangle([0, CH-2, W, CH+2], fill=BORDER)

    # 上排垂直線
    draw.rectangle([TOP_CW-2, 0, TOP_CW+2, CH], fill=BORDER)

    # 下排垂直線
    x = BOT_WIDTHS[0]
    draw.rectangle([x-2, CH, x+2, H], fill=BORDER)
    x += BOT_WIDTHS[1]
    draw.rectangle([x-2, CH, x+2, H], fill=BORDER)

    # 外框
    draw.rectangle([0, 0, W-1, H-1], outline=BORDER, width=6)

    # 頂部色條
    for idx, (_, _, _, row, col) in enumerate(BUTTONS):
        color   = PALETTE[idx]
        x0, y0, x1, _ = _cell_bounds(row, col)
        draw.rectangle([x0+3, y0+3, x1-3, y0+10], fill=color)

    img.save(output_path, "PNG", optimize=True)
    print(f"Rich menu image saved: {output_path}  ({W}x{H})  5-button layout")
    return output_path


if __name__ == "__main__":
    create_rich_menu_image()
