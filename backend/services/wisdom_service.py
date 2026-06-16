"""Wisdom Service — 每日投資智慧（大師名言 + AI 市場解讀）"""
from __future__ import annotations

import time
from datetime import date
from loguru import logger

_cache: dict = {}
_cache_ts: float = 0.0
_TTL = 3600 * 6  # 6 小時

# 大師名言資料庫
_WISDOM_DB: list[dict] = [
    {"author": "Warren Buffett", "quote": "Be fearful when others are greedy, and greedy when others are fearful.",
     "zh": "別人恐懼時我貪婪，別人貪婪時我恐懼。",
     "category": "心理", "tags": ["情緒管理", "逆向思維"]},
    {"author": "Peter Lynch", "quote": "Know what you own, and know why you own it.",
     "zh": "了解你持有的東西，以及你持有它的原因。",
     "category": "選股", "tags": ["基本面", "持股邏輯"]},
    {"author": "Benjamin Graham", "quote": "The stock market is a voting machine in the short run and a weighing machine in the long run.",
     "zh": "股市短期是投票機，長期是體重計。",
     "category": "長線", "tags": ["長期投資", "價值"]},
    {"author": "George Soros", "quote": "It's not whether you're right or wrong that's important, but how much money you make when you're right and how much you lose when you're wrong.",
     "zh": "重要的不是你對或錯，而是對時賺多少、錯時賠多少。",
     "category": "風控", "tags": ["風險管理", "期望值"]},
    {"author": "Jesse Livermore", "quote": "There is only one side to the stock market; and it is not the bull side or the bear side, but the right side.",
     "zh": "市場只有一個方向：不是多方也不是空方，而是正確的那方。",
     "category": "操作", "tags": ["趨勢", "判斷力"]},
    {"author": "Howard Marks", "quote": "Risk comes from not knowing what you're doing.",
     "zh": "風險來自於不知道自己在做什麼。",
     "category": "風控", "tags": ["風險認知", "研究"]},
    {"author": "Charlie Munger", "quote": "Invert, always invert. Turn a situation or problem upside down. Look at it backward.",
     "zh": "反轉，永遠反轉。把問題翻轉過來看，換個角度思考。",
     "category": "心理", "tags": ["思維方式", "逆向"]},
    {"author": "John Templeton", "quote": "The time of maximum pessimism is the best time to buy, and the time of maximum optimism is the best time to sell.",
     "zh": "最大悲觀時是最好的買點，最大樂觀時是最好的賣點。",
     "category": "時機", "tags": ["情緒週期", "進出場"]},
    {"author": "Philip Fisher", "quote": "The stock market is filled with individuals who know the price of everything, but the value of nothing.",
     "zh": "股市充滿了知道一切價格、卻不知道任何價值的人。",
     "category": "選股", "tags": ["價值投資", "基本面"]},
    {"author": "Paul Tudor Jones", "quote": "Don't focus on making money; focus on protecting what you have.",
     "zh": "不要專注於賺錢；要專注於保護你已擁有的。",
     "category": "風控", "tags": ["資本保護", "守業"]},
    {"author": "William O'Neil", "quote": "The whole secret to winning and losing in the stock market is to lose the least amount possible when you're not right.",
     "zh": "股市致勝的秘訣是：在你錯的時候，把損失降到最低。",
     "category": "風控", "tags": ["停損", "紀律"]},
    {"author": "Ray Dalio", "quote": "He who lives by the crystal ball will eat shattered glass.",
     "zh": "靠預測維生的人，終將吞食碎玻璃。",
     "category": "心理", "tags": ["謙遜", "不確定性"]},
    {"author": "Seth Klarman", "quote": "The stock market is the story of cycles and of the human behavior that is responsible for overreactions in both directions.",
     "zh": "股市是週期的故事，也是人類情緒在兩個方向過度反應的故事。",
     "category": "心理", "tags": ["週期", "行為偏差"]},
    {"author": "Stanley Druckenmiller", "quote": "Concentrate your investments. If you want to make really serious money, you have to take big bets when you have high conviction.",
     "zh": "集中投資。如果你想賺大錢，就要在高度確信時下大注。",
     "category": "操作", "tags": ["集中持倉", "確信度"]},
    {"author": "Joel Greenblatt", "quote": "If you are going to do it on your own, you have to have a real edge.",
     "zh": "如果你要自己操作，你必須有真正的優勢。",
     "category": "選股", "tags": ["競爭優勢", "差異化"]},
    {"author": "Michael Burry", "quote": "I always look for the objective information first, then use subjective interpretation to build the investment thesis.",
     "zh": "我總是先找客觀資料，再用主觀判斷建立投資論點。",
     "category": "選股", "tags": ["研究方法", "系統化"]},
    {"author": "David Tepper", "quote": "If you're not confused about markets right now, you don't understand what's going on.",
     "zh": "如果你現在對市場不感到困惑，代表你還沒搞清楚狀況。",
     "category": "心理", "tags": ["謙遜", "複雜性"]},
    {"author": "Carl Icahn", "quote": "In life and business, there are two cardinal sins. The first is to act precipitously without thought. The second is to not act at all.",
     "zh": "人生和商業中有兩大原罪：一是不假思索衝動行事，二是完全不行動。",
     "category": "操作", "tags": ["決策", "行動力"]},
    {"author": "John Bogle", "quote": "The greatest enemy of a good plan is the dream of a perfect plan.",
     "zh": "好計畫的最大敵人，是追求完美計畫的夢想。",
     "category": "操作", "tags": ["執行", "完美主義"]},
    {"author": "Ed Seykota", "quote": "Win or lose, everybody gets what they want out of the market.",
     "zh": "不論輸贏，每個人都從市場得到了他想要的東西。",
     "category": "心理", "tags": ["自我認知", "動機"]},
    {"author": "Mark Douglas", "quote": "The best traders are not right more often, they just lose less when they're wrong.",
     "zh": "最好的交易者不是更常對，只是錯的時候輸更少。",
     "category": "風控", "tags": ["勝率", "期望值"]},
    {"author": "Linda Raschke", "quote": "The market will teach you everything you need to know, if you are willing to listen.",
     "zh": "如果你願意傾聽，市場會教你所有你需要知道的事。",
     "category": "心理", "tags": ["學習", "謙遜"]},
    {"author": "Nicolas Darvas", "quote": "I buy whenever a stock behaves correctly and is in a strong industry, and I cut my losses quickly without hesitation.",
     "zh": "我在個股表現正確且行業強勁時買進，並且毫不猶豫地快速停損。",
     "category": "操作", "tags": ["動能", "停損"]},
    {"author": "Richard Dennis", "quote": "I always say that you could publish my trading rules in the newspaper and no one would follow them.",
     "zh": "我說過，就算把我的交易規則登在報紙上，也沒人會遵守。",
     "category": "紀律", "tags": ["系統", "執行力"]},
    {"author": "Jim Rogers", "quote": "If you want to buy stocks, look at the most depressed stocks in the most depressed industries.",
     "zh": "如果你想買股票，去看看最低迷行業中最被壓制的股票。",
     "category": "時機", "tags": ["逆向", "景氣低谷"]},
    {"author": "Nassim Taleb", "quote": "The most important thing is to survive the bad times, then let the good times take care of themselves.",
     "zh": "最重要的是熬過壞時代，然後讓好時代自然到來。",
     "category": "風控", "tags": ["存活", "長期"]},
    {"author": "Bruce Kovner", "quote": "If you personalize losses, you can't trade.",
     "zh": "如果你把虧損個人化，你就無法交易了。",
     "category": "心理", "tags": ["情緒", "客觀"]},
    {"author": "Steve Cohen", "quote": "Most traders make the mistake of trying to be right. Successful traders focus on making money.",
     "zh": "大多數交易者的錯誤是試圖證明自己對。成功的交易者專注於賺錢。",
     "category": "心理", "tags": ["自我", "目標"]},
]

# 每日格言主題（對應市場情境）
_MARKET_WISDOM: dict[str, list[str]] = {
    "strong_bull": [
        "大漲時最需要問自己：買進的理由是否仍然成立？",
        "多頭行情往往在樂觀聲浪最高時結束；貪婪是最危險的時刻。",
        "強勢時不追高，等回測支撐確認後再加碼，是老手的基本功。",
    ],
    "mild_bull": [
        "市場偏多但不追高，精選個股、耐心等待好的進場位置。",
        "小漲的日子適合做功課：重新審視持股的基本面是否改變。",
        "多頭時期最忌「賺小錢就跑、虧大錢就撐」的不對稱行為。",
    ],
    "flat": [
        "震盪盤整期間，紀律比預測更重要。",
        "整理是為了走更遠。盤整期間保持現金靈活度，等待突破確認。",
        "市場沉默時，往往是下一波方向蓄積能量的時刻。",
    ],
    "mild_bear": [
        "下跌時的勇氣，是指按計畫執行停損，而不是硬撐不走。",
        "偏弱的市場需要更嚴格的進場條件，不要因為股票便宜就進場。",
        "用現金等待好機會，本身就是一種最重要的進攻準備。",
    ],
    "strong_bear": [
        "大跌時：先問能否保本，再問如何獲利。",
        "市場恐慌時，下跌速度往往遠超想像；保留現金比任何策略都重要。",
        "真正的低點只有過後才知道；分批承接、控制倉位，是應對大跌的不二法門。",
    ],
}


async def get_wisdom() -> dict:
    global _cache, _cache_ts
    now = time.time()
    today = date.today().isoformat()
    if _cache and _cache.get("date") == today and now - _cache_ts < _TTL:
        return _cache
    result = await _build_wisdom()
    _cache = result
    _cache_ts = now
    return result


async def _build_wisdom() -> dict:
    today = date.today()
    day_of_year = today.timetuple().tm_yday

    # 選取今日大師名言
    wisdom = _WISDOM_DB[day_of_year % len(_WISDOM_DB)]
    wisdom2 = _WISDOM_DB[(day_of_year + 13) % len(_WISDOM_DB)]  # 第二句

    # 取得市場背景
    market = await _get_market_context()
    chg = market.get("chg_pct", 0.0)

    # 根據市場決定情境
    if chg > 2:
        tone, tone_key = "大漲日", "strong_bull"
    elif chg > 0.5:
        tone, tone_key = "偏多日", "mild_bull"
    elif chg < -2:
        tone, tone_key = "大跌日", "strong_bear"
    elif chg < -0.5:
        tone, tone_key = "偏弱日", "mild_bear"
    else:
        tone, tone_key = "震盪日", "flat"

    # 選取市場智慧
    pool = _MARKET_WISDOM[tone_key]
    market_insight = pool[day_of_year % len(pool)]

    # 本週思考問題
    week_questions = [
        "你這週最大的錯誤決策是什麼？下次如何改進？",
        "你的投資組合反映了你真正的信念嗎？",
        "如果只能持有一檔股票 5 年，你會選什麼？",
        "你有沒有在恐懼的時候買進、在貪婪的時候賣出？",
        "你的停損紀律上週有沒有被破壞？為什麼？",
        "你學習市場的速度是否跟上市場變化的速度？",
        "你的倉位大小是否反映了你的信心程度？",
    ]
    week_q = week_questions[today.weekday()]  # 0=週一 ... 6=週日

    return {
        "date":           today.isoformat(),
        "wisdom":         wisdom,
        "wisdom2":        wisdom2,
        "market":         market,
        "tone":           tone,
        "market_insight": market_insight,
        "week_question":  week_q,
    }


async def _get_market_context() -> dict:
    import httpx
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(url, params={"interval": "1d", "range": "3d"},
                            headers={"User-Agent": "Mozilla/5.0"})
        result = r.json()["chart"]["result"][0]
        closes = [x for x in result["indicators"]["quote"][0].get("close", []) if x is not None]
        if len(closes) >= 2:
            chg = (closes[-1] - closes[-2]) / closes[-2] * 100
            return {"twii": round(closes[-1], 0), "chg_pct": round(chg, 2)}
    except Exception as e:
        pass
    return {"twii": 0, "chg_pct": 0.0}


def format_wisdom_report(data: dict) -> str:
    today   = data.get("date", date.today().isoformat())
    w       = data.get("wisdom", {})
    w2      = data.get("wisdom2", {})
    market  = data.get("market", {})
    tone    = data.get("tone", "")
    insight = data.get("market_insight", "")
    week_q  = data.get("week_question", "")

    twii    = market.get("twii", 0)
    chg     = market.get("chg_pct", 0.0)
    chg_icon = "📈" if chg >= 0 else "📉"

    lines = [
        "🧭 每日投資智慧",
        f"── {today}（{tone}）──────────────",
        "",
        f"大盤：{twii:.0f}  {chg_icon}{chg:+.1f}%",
        "",
        "═" * 30,
        f"💬 大師語錄 一",
        f'   「{w.get("zh", "")}」',
        f'   —— {w.get("author", "")}',
        "",
        f'   EN: {w.get("quote", "")}',
        "",
        f'   🏷 類別：{w.get("category", "")}  #{" #".join(w.get("tags", []))}',
        "═" * 30,
        "",
        f"💬 大師語錄 二",
        f'   「{w2.get("zh", "")}」',
        f'   —— {w2.get("author", "")}',
        "",
        "─" * 28,
        "",
        f"📊 今日市場智慧（{tone}）",
        f"   {insight}",
        "",
        "─" * 28,
        "",
        "🤔 本週反思問題",
        f"   {week_q}",
        "",
        "   （建議：把答案寫進投資日記 /journal add）",
        "",
        "─" * 28,
        "輸入 /qa 每日問答 | /journal 投資日記 | /feargreed 恐慌指數",
    ]
    return "\n".join(lines)


async def push_wisdom_to_admin() -> bool:
    import os
    from .line_push import push_line_messages
    admin_uid = os.getenv("ADMIN_LINE_UID", "")
    if not admin_uid:
        return False
    try:
        data = await get_wisdom()
        text = format_wisdom_report(data)
        ok = await push_line_messages(
            admin_uid,
            [{"type": "text", "text": text[:4000]}],
            context="wisdom.push",
        )
        logger.info(f"[wisdom] pushed: {ok}")
        return ok
    except Exception as e:
        logger.error(f"[wisdom] push error: {e}")
        return False
