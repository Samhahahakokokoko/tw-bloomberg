"""
theme_propagation_engine.py — 主題擴散鏈追蹤引擎

主題鏈範例：
  CoWoS → ABF → PCB → 散熱 → 機殼 → 電源供應器
  AI Agent → Edge AI → 車用AI → 工業AI
  HBM → 先進封裝 → 基板 → 化學材料

propagation_score: 0-1，代表主題擴散到該節點的強度
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


THEME_CHAINS: list[dict] = [
    {
        "theme": "AI伺服器供應鏈",
        "chain": ["CoWoS封裝", "ABF基板", "PCB", "散熱模組", "機殼", "電源供應器"],
        "stocks": {
            "CoWoS封裝":  ["2330", "2454"],
            "ABF基板":    ["3037", "8046"],
            "PCB":        ["2383", "6269"],
            "散熱模組":   ["3324", "8097"],
            "機殼":       ["2317"],
            "電源供應器": ["6220", "1598"],
        },
        "propagation_delay": [0, 3, 5, 7, 10, 12],
    },
    {
        "theme": "HBM記憶體鏈",
        "chain": ["HBM需求", "先進封裝", "基板材料", "化學材料", "設備"],
        "stocks": {
            "HBM需求":  ["2408"],
            "先進封裝": ["2330", "6547"],
            "基板材料": ["3037"],
            "化學材料": ["4763"],
            "設備":     ["3450", "6683"],
        },
        "propagation_delay": [0, 2, 4, 6, 9],
    },
    {
        "theme": "車用電子鏈",
        "chain": ["電動車需求", "車用MCU", "車用MOSFET", "車用PCB", "車用連接器"],
        "stocks": {
            "電動車需求":   ["2308"],
            "車用MCU":     ["2454", "3711"],
            "車用MOSFET":  ["2449"],
            "車用PCB":     ["2383"],
            "車用連接器":  ["2317"],
        },
        "propagation_delay": [0, 3, 5, 7, 10],
    },
    {
        "theme": "邊緣AI",
        "chain": ["雲端AI訓練", "推論晶片", "邊緣裝置", "IoT模組", "工業應用"],
        "stocks": {
            "雲端AI訓練": ["2330"],
            "推論晶片":   ["2454", "3711"],
            "邊緣裝置":   ["2308", "2317"],
            "IoT模組":    ["3413"],
            "工業應用":   ["2049"],
        },
        "propagation_delay": [0, 2, 5, 8, 12],
    },
]


@dataclass
class ChainNode:
    name:              str
    stocks:            list[str]
    propagation_score: float   # 0-1
    delay_days:        int
    activated:         bool = False
    return_5d:         float = 0.0


@dataclass
class ThemePropagationResult:
    theme:         str
    nodes:         list[ChainNode]
    front_node:    str               # 目前擴散到哪個節點
    lag_nodes:     list[str]         # 尚未跟進的節點
    total_score:   float             # 0-100，整體擴散程度
    ts:            str = field(default_factory=lambda: datetime.now().isoformat())

    def to_line_text(self) -> str:
        lines = [f"🔗 {self.theme} 擴散進度"]
        chain_str = ""
        for i, node in enumerate(self.nodes):
            arrow = " → " if i < len(self.nodes) - 1 else ""
            status = "🟢" if node.activated else "⚪"
            chain_str += f"{status}{node.name}{arrow}"
        lines.append(chain_str)
        lines.append(f"前沿節點：{self.front_node}")
        if self.lag_nodes:
            lines.append(f"待跟進：{'、'.join(self.lag_nodes[:3])}")
        lines.append(f"擴散強度：{self.total_score:.0f}/100")
        return "\n".join(lines)


def _score_node(node_stocks: list[str], stock_returns: dict[str, float], threshold: float = 0.03) -> tuple[bool, float]:
    """根據節點個股近期報酬判斷是否已激活"""
    if not node_stocks:
        return False, 0.0
    rets = [stock_returns.get(s, 0.0) for s in node_stocks]
    avg_ret = sum(rets) / len(rets)
    activated = avg_ret > threshold
    score = min(1.0, max(0.0, (avg_ret - threshold) / 0.10 + 0.5 if activated else avg_ret / threshold * 0.5))
    return activated, score


async def analyze_theme_propagation(
    theme_cfg: dict,
    stock_returns: dict[str, float] | None = None,
) -> ThemePropagationResult:
    """分析單一主題鏈的擴散狀態"""
    if stock_returns is None:
        stock_returns = {}

    chain      = theme_cfg["chain"]
    stocks_map = theme_cfg["stocks"]
    delays     = theme_cfg.get("propagation_delay", list(range(len(chain))))

    nodes: list[ChainNode] = []
    last_activated_idx = -1

    for i, node_name in enumerate(chain):
        node_stocks = stocks_map.get(node_name, [])
        activated, score = _score_node(node_stocks, stock_returns)
        avg_ret = sum(stock_returns.get(s, 0.0) for s in node_stocks) / max(len(node_stocks), 1)

        node = ChainNode(
            name              = node_name,
            stocks            = node_stocks,
            propagation_score = score,
            delay_days        = delays[i] if i < len(delays) else i * 3,
            activated         = activated,
            return_5d         = avg_ret,
        )
        nodes.append(node)
        if activated:
            last_activated_idx = i

    # 前沿節點
    front_idx = last_activated_idx if last_activated_idx >= 0 else 0
    front_node = chain[front_idx] if chain else ""

    # 滯後節點
    lag_nodes = [chain[i] for i in range(front_idx + 1, len(chain)) if i < len(chain)]

    # 整體擴散分數
    activated_count = sum(1 for n in nodes if n.activated)
    total_score = (activated_count / len(nodes)) * 100 if nodes else 0.0

    return ThemePropagationResult(
        theme      = theme_cfg["theme"],
        nodes      = nodes,
        front_node = front_node,
        lag_nodes  = lag_nodes,
        total_score = total_score,
    )


async def run_theme_propagation(stock_returns: dict[str, float] | None = None) -> list[ThemePropagationResult]:
    """掃描所有主題鏈"""
    if stock_returns is None:
        stock_returns = {
            "2330": 0.05, "2454": 0.06, "3037": 0.04,
            "8046": 0.03, "2383": 0.01, "3324": 0.01,
            "8097": 0.00, "2317": 0.00, "6220": 0.00,
            "2408": 0.08, "6547": 0.07, "4763": 0.02,
        }

    results = []
    for theme_cfg in THEME_CHAINS:
        try:
            r = await analyze_theme_propagation(theme_cfg, stock_returns)
            results.append(r)
        except Exception as e:
            logger.warning("[theme] skip %s: %s", theme_cfg.get("theme"), e)

    results.sort(key=lambda r: -r.total_score)
    return results


def format_theme_summary(results: list[ThemePropagationResult]) -> str:
    if not results:
        return "⚠️ 暫無主題擴散資料"
    lines = ["🔗 主題擴散地圖"]
    for r in results[:4]:
        bar = "▓" * int(r.total_score / 10) + "░" * (10 - int(r.total_score / 10))
        lines.append(f"{bar} {r.theme} {r.total_score:.0f}%→{r.front_node}")
    return "\n".join(lines)
