import React, { useEffect, useState } from "react";
import { getLeaderboard, getPerformanceHistory, triggerSnapshot, getWeeklyPicks } from "../utils/api";
import Card from "../components/Card";
import PriceTag from "../components/PriceTag";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import { Trophy, TrendingUp } from "lucide-react";

const MEDAL = ["🥇", "🥈", "🥉"];

export default function Performance() {
  const [board, setBoard]       = useState([]);
  const [history, setHistory]   = useState([]);
  const [picks, setPicks]       = useState(null);
  const [loading, setLoading]   = useState(false);
  const [pickLoading, setPickLoading] = useState(false);
  const [snapMsg, setSnapMsg]   = useState("");
  const userId = "";

  useEffect(() => {
    setLoading(true);
    Promise.allSettled([
      getLeaderboard(),
      getPerformanceHistory(userId, 30),
    ]).then(([b, h]) => {
      if (b.status === "fulfilled") setBoard(b.value);
      if (h.status === "fulfilled") setHistory(h.value);
    }).finally(() => setLoading(false));
  }, []);

  const handleSnapshot = async () => {
    setSnapMsg("快照中...");
    try {
      await triggerSnapshot();
      setSnapMsg("✓ 績效快照完成");
      const h = await getPerformanceHistory(userId, 30);
      setHistory(h);
    } catch (e) {
      setSnapMsg("快照失敗：" + e.message);
    }
    setTimeout(() => setSnapMsg(""), 5000);
  };

  const handleLoadPicks = async () => {
    setPickLoading(true);
    setPicks(null);
    try {
      const data = await getWeeklyPicks(5);
      setPicks(data);
    } catch (e) {
      setPicks({ error: e.message });
    } finally {
      setPickLoading(false);
    }
  };

  const myRecord = board.find(b => b.user_id === (userId || "匿名"));

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest flex items-center gap-2">
          <Trophy size={16} /> 績效排行榜
        </h1>
        <div className="flex gap-2">
          <button
            onClick={handleLoadPicks}
            disabled={pickLoading}
            className="px-3 py-1.5 bg-terminal-yellow/10 border border-terminal-yellow text-terminal-yellow text-xs rounded hover:bg-terminal-yellow/20"
          >
            {pickLoading ? "載入..." : "🎯 每週選股"}
          </button>
          <button
            onClick={handleSnapshot}
            className="px-3 py-1.5 bg-terminal-accent/10 border border-terminal-accent text-terminal-accent text-xs rounded hover:bg-terminal-accent/20"
          >
            📸 拍績效快照
          </button>
        </div>
      </div>

      {snapMsg && (
        <div className="text-terminal-accent text-xs px-3 py-2 bg-terminal-accent/10 border border-terminal-accent/30 rounded">
          {snapMsg}
        </div>
      )}

      {/* 每週選股 */}
      {picks && !picks.error && (
        <Card title={`🎯 每週選股報告 ${picks.date || ""}`}>
          <div className="text-terminal-muted text-xs mb-3">篩選條件：{picks.criteria}</div>
          <div className="space-y-2">
            {picks.picks?.map((p, i) => (
              <div key={p.code} className="flex items-center justify-between border-b border-terminal-border/30 pb-2">
                <div className="flex items-center gap-3">
                  <span className="text-lg">{MEDAL[i] || `${i+1}.`}</span>
                  <div>
                    <span className="text-terminal-accent font-bold text-sm">{p.code}</span>
                    <span className="text-terminal-text text-sm ml-2">{p.name}</span>
                  </div>
                </div>
                <div className="text-right">
                  <div className="text-terminal-text font-mono">{p.price}</div>
                  <div className="text-xs text-terminal-green">外資淨買 +{p.foreign_net?.toLocaleString()} 張</div>
                </div>
              </div>
            ))}
          </div>
          {picks.ai_analysis && (
            <div className="mt-3 p-3 bg-terminal-surface/50 rounded border border-terminal-border/30">
              <div className="text-terminal-accent text-xs mb-1">◈ AI 分析</div>
              <pre className="whitespace-pre-wrap text-xs text-terminal-text leading-relaxed font-mono">
                {picks.ai_analysis}
              </pre>
            </div>
          )}
        </Card>
      )}

      {/* 排行榜 */}
      <Card title={`排行榜 (${board.length} 位投資人)`}>
        {loading ? (
          <div className="text-terminal-muted text-sm text-center py-8">載入中...</div>
        ) : board.length === 0 ? (
          <div className="text-terminal-muted text-sm text-center py-8">尚無投資人資料 — 先在 Portfolio 新增持股</div>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-terminal-muted border-b border-terminal-border">
                {["名次", "用戶", "持股", "總成本", "總市值", "損益", "損益%", "最佳股", "最差股"].map(h => (
                  <th key={h} className="text-left py-1.5 pr-3">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {board.map((item) => (
                <tr
                  key={item.user_id}
                  className={`border-b border-terminal-border/30 hover:bg-terminal-border/20 ${
                    item.user_id === (userId || "匿名") ? "bg-terminal-accent/5" : ""
                  }`}
                >
                  <td className="py-2 pr-3 text-xl">{MEDAL[item.rank - 1] || item.rank}</td>
                  <td className="py-2 pr-3 text-terminal-accent font-bold">{item.user_id}</td>
                  <td className="py-2 pr-3">{item.holdings_count}</td>
                  <td className="py-2 pr-3 font-mono">{item.total_cost?.toLocaleString()}</td>
                  <td className="py-2 pr-3 font-mono">{item.total_mv?.toLocaleString()}</td>
                  <td className="py-2 pr-3">
                    <span className={item.total_pnl >= 0 ? "text-terminal-green" : "text-terminal-red"}>
                      {item.total_pnl >= 0 ? "+" : ""}{item.total_pnl?.toLocaleString()}
                    </span>
                  </td>
                  <td className="py-2 pr-3">
                    <span className={`font-bold ${item.total_pnl_pct >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                      {item.total_pnl_pct >= 0 ? "+" : ""}{item.total_pnl_pct}%
                    </span>
                  </td>
                  <td className="py-2 pr-3">
                    {item.best_stock && (
                      <span className="text-terminal-green">
                        {item.best_stock.code} (+{item.best_stock.pct}%)
                      </span>
                    )}
                  </td>
                  <td className="py-2 pr-3">
                    {item.worst_stock && (
                      <span className="text-terminal-red">
                        {item.worst_stock.code} ({item.worst_stock.pct}%)
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {/* 績效歷史圖 */}
      {history.length > 1 && (
        <Card title="績效走勢 (近30日)">
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={history}>
              <XAxis
                dataKey="date"
                tick={{ fill: "#4a6080", fontSize: 9 }}
                tickLine={false}
                axisLine={{ stroke: "#1e3a5f" }}
                tickFormatter={v => v.slice(5)}
                interval={4}
              />
              <YAxis
                tick={{ fill: "#4a6080", fontSize: 9 }}
                tickLine={false}
                axisLine={false}
                domain={["auto", "auto"]}
              />
              <Tooltip
                contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
                formatter={(v, name) => [
                  name === "daily_return" ? `${v >= 0 ? "+" : ""}${v?.toFixed(2)}%` : v?.toLocaleString(),
                  name === "daily_return" ? "損益%" : name === "total_mv" ? "市值" : "損益",
                ]}
              />
              <Line type="monotone" dataKey="total_mv" stroke="#00d4ff" dot={false} strokeWidth={1.5} />
              <Line type="monotone" dataKey="total_pnl" stroke="#00ff88" dot={false} strokeWidth={1} strokeDasharray="4 2" />
            </LineChart>
          </ResponsiveContainer>
        </Card>
      )}
    </div>
  );
}
