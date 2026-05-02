"""生成 Rich Menu 圖片 — 深色主題，6格 3×2 佈局
上排：📊大盤行情 / 💼我的庫存 / 🔍今日選股
下排：📰市場新聞 / 🤖AI分析 / ⚙️更多功能
"""
from __future__ import annotations
from PIL import Image, ImageDraw, ImageFont
import os, math

W, H  = 2500, 1686
ROWS  = 2
COLS  = 3
CW    = W // COLS   # 833
CH    = H // ROWS   # 843

# ── 配色 ─────────────────────────────────────────────────────────────────────
BG      = (8,   13,  24)
SURFACE = (12,  19,  36)
BORDER  = (24,  42,  72)
WHITE   = (220, 235, 252)
MUTED   = (72,  96,  128)
DARK    = (6,   10,  18)

PALETTE = [
    (0,   210, 255),   # 大盤行情 — cyan
    (0,   230, 118),   # 我的庫存 — green
    (0,   180, 255),   # 今日選股 — blue
    (255, 180,  50),   # 市場新聞 — amber
    (200, 120, 255),   # AI分析   — purple
    (100, 140, 200),   # 更多功能 — steel
]

# (標題, 副標, icon_key, row, col, action)
BUTTONS = [
    ("大盤行情", "指數・法人・族群",   "chart",  0, 0),
    ("我的庫存", "損益・健康・AI診斷", "wallet", 0, 1),
    ("今日選股", "動能・籌碼・突破",   "search", 0, 2),
    ("市場新聞", "熱門・情緒・產業",   "news",   1, 0),
    ("AI分析",   "個股・決策・建議",   "ai",     1, 1),
    ("更多功能", "回測・風控・零股",   "menu",   1, 2),
]


def _find_font() -> str:
    candidates = [
        "C:/Windows/Fonts/msjh.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
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

def _draw_chart(draw, cx, cy, c, s=80):
    bars  = [0.42, 0.68, 1.0, 0.58, 0.80, 0.45, 0.72]
    bw    = int(s * 0.20); gap = int(s * 0.09)
    total = len(bars) * bw + (len(bars) - 1) * gap
    x0    = cx - total // 2; bl = cy + s // 2
    for i, r in enumerate(bars):
        bh = int(s * r); x = x0 + i * (bw + gap)
        draw.rectangle([x, bl - bh, x + bw, bl], fill=c)
    draw.rectangle([x0 - 4, bl + 2, x0 + total + 4, bl + 7], fill=c)


def _draw_wallet(draw, cx, cy, c, s=80):
    w, h = int(s * 1.4), int(s * 0.9)
    draw.rounded_rectangle([cx-w//2, cy-h//2, cx+w//2, cy+h//2], radius=14,
                            fill=SURFACE, outline=c, width=6)
    pw, ph = int(s*0.38), int(s*0.30)
    draw.rounded_rectangle([cx+w//2-pw-10, cy-ph//2, cx+w//2-10, cy+ph//2],
                            radius=9, fill=DARK, outline=c, width=5)
    draw.ellipse([cx+w//2-pw//2-30, cy-10, cx+w//2-pw//2-4, cy+10], fill=c)
    draw.line([cx-w//2+20, cy-15, cx-w//2+20+int(s*0.45), cy-15], fill=c, width=5)
    draw.line([cx-w//2+20, cy+5,  cx-w//2+20+int(s*0.30), cy+5],  fill=c, width=5)


def _draw_search(draw, cx, cy, c, s=80):
    r = int(s * 0.50)
    draw.ellipse([cx-r-6, cy-r-6, cx+r+6-s//3, cy+r+6-s//3], outline=c, width=7)
    handle_len = int(s * 0.55)
    x1 = cx + int((r+6-s//3) * 0.707) - 4
    y1 = cy + int((r+6-s//3) * 0.707) - 4
    draw.line([x1, y1, x1+handle_len, y1+handle_len], fill=c, width=10)
    # magnifier cross lines
    mr = int(s * 0.28)
    mx, my = cx - s // 6, cy - s // 6
    draw.line([mx-mr, my, mx+mr, my], fill=BORDER, width=4)
    draw.line([mx, my-mr, mx, my+mr], fill=BORDER, width=4)


def _draw_news(draw, cx, cy, c, s=80):
    pw, ph = int(s*1.20), int(s*1.05)
    draw.rounded_rectangle([cx-pw//2, cy-ph//2, cx+pw//2, cy+ph//2],
                            radius=10, fill=SURFACE, outline=c, width=5)
    draw.rectangle([cx-pw//2+14, cy-ph//2+14, cx+pw//2-14, cy-ph//2+50], fill=c)
    for i, (ratio, dy) in enumerate([(0.85,68),(0.60,108),(0.85,148),(0.50,188)]):
        lw  = int((pw-28) * ratio)
        col = c if i % 2 == 0 else MUTED
        draw.rectangle([cx-pw//2+14, cy-ph//2+dy, cx-pw//2+14+lw, cy-ph//2+dy+7], fill=col)


def _draw_ai(draw, cx, cy, c, s=80):
    r = int(s * 0.52)
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], outline=c, width=7)
    for dx in [-r//2, r//2]:
        draw.line([cx+dx, cy-r+8, cx+dx, cy+r-8], fill=BORDER, width=4)
    for dy in [-r//2, r//2]:
        draw.line([cx-r+8, cy+dy, cx+r-8, cy+dy], fill=BORDER, width=4)
    for nx, ny in [(0,-r//2),(0,r//2),(-r//2,0),(r//2,0)]:
        draw.ellipse([cx+nx-7, cy+ny-7, cx+nx+7, cy+ny+7], fill=c)
    draw.ellipse([cx-13, cy-13, cx+13, cy+13], fill=c)
    for angle in [0, 90, 180, 270]:
        rad = math.radians(angle)
        x1  = cx + int(r * math.cos(rad)); y1 = cy + int(r * math.sin(rad))
        x2  = cx + int((r+24) * math.cos(rad)); y2 = cy + int((r+24) * math.sin(rad))
        draw.line([x1,y1,x2,y2], fill=c, width=6)
        draw.ellipse([x2-9, y2-9, x2+9, y2+9], fill=c)


def _draw_menu(draw, cx, cy, c, s=80):
    widths = [1.0, 0.70, 0.85]
    for i, ratio in enumerate(widths):
        y  = cy - s//3 + i * (s//3); lw = int(s * ratio)
        draw.rounded_rectangle([cx-lw//2, y-7, cx+lw//2, y+7], radius=7, fill=c)


ICON_FN = {
    "chart":  _draw_chart,
    "wallet": _draw_wallet,
    "search": _draw_search,
    "news":   _draw_news,
    "ai":     _draw_ai,
    "menu":   _draw_menu,
}


def _cell_bounds(row: int, col: int) -> tuple[int, int, int, int]:
    x0 = col * CW
    y0 = row * CH
    return x0, y0, x0 + CW, y0 + CH


def create_rich_menu_image(output_path: str = None) -> str:
    if output_path is None:
        output_path = os.path.join(os.path.dirname(__file__), "..", "data", "rich_menu.png")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    f_title = _font(80)
    f_sub   = _font(42)
    f_badge = _font(36)

    for idx, (title, subtitle, icon_key, row, col) in enumerate(BUTTONS):
        color          = PALETTE[idx]
        x0, y0, x1, y1 = _cell_bounds(row, col)
        cw_ = x1 - x0; ch_ = y1 - y0

        # 底色
        draw.rectangle([x0, y0, x1, y1], fill=SURFACE)

        # 頂部漸層暈染
        for depth in range(12):
            fade = tuple(max(0, int(c * (1 - depth * 0.08))) for c in color)
            draw.rectangle([x0, y0 + depth*5, x1, y0 + (depth+1)*5], fill=fade)
        draw.rectangle([x0, y0 + 62, x1, y1], fill=SURFACE)

        # 圖示
        icon_cx = x0 + cw_ // 2
        icon_cy = y0 + int(ch_ * 0.36)
        ICON_FN[icon_key](draw, icon_cx, icon_cy, color)

        # 標題
        title_y = y0 + int(ch_ * 0.66)
        draw.text((x0 + cw_ // 2, title_y),     title,    font=f_title, fill=WHITE,  anchor="mm")
        draw.text((x0 + cw_ // 2, title_y + 64), subtitle, font=f_sub,   fill=MUTED,  anchor="mm")

        # 左上角序號 badge
        draw.text((x0 + 22, y0 + 20), f"0{idx+1}", font=f_badge, fill=color, anchor="lt")

        # 右下角箭頭
        ax, ay = x1 - 32, y1 - 32
        draw.polygon([(ax-12, ay+4), (ax+4, ay+4), (ax+4, ay-12)], fill=color)

    # ── 格線 ─────────────────────────────────────────────────────────────────
    # 水平中線
    draw.rectangle([0, CH-2, W, CH+2], fill=BORDER)
    # 垂直線（上排）
    draw.rectangle([CW-2,   0, CW+2,   CH], fill=BORDER)
    draw.rectangle([CW*2-2, 0, CW*2+2, CH], fill=BORDER)
    # 垂直線（下排）
    draw.rectangle([CW-2,   CH, CW+2,   H], fill=BORDER)
    draw.rectangle([CW*2-2, CH, CW*2+2, H], fill=BORDER)
    # 外框
    draw.rectangle([0, 0, W-1, H-1], outline=BORDER, width=6)

    # 頂部色條
    for idx, (_, _, _, row, col) in enumerate(BUTTONS):
        color   = PALETTE[idx]
        x0, y0, x1, _ = _cell_bounds(row, col)
        draw.rectangle([x0+3, y0+3, x1-3, y0+10], fill=color)

    img.save(output_path, "PNG", optimize=True)
    print(f"Rich menu image saved: {output_path}  ({W}x{H})  6-button 3x2 layout")
    return output_path


if __name__ == "__main__":
    create_rich_menu_image()
