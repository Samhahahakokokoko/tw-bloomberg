import React, { useEffect, useState } from "react";
import {
  getAccuracyStats, getWeightHistory, getCurrentWeights,
  triggerBackfill, triggerWeightAdjust,
} from "../utils/api";
import Card from "../components/Card";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, ReferenceLine, BarChart, Bar, Legend,
} from "recharts";
import { TrendingUp, RefreshCw, Target, Award } from "lucide-react";

const WIN_COLOR = "#00ff88";
const LOSE_COLOR = "#ff4466";

function StatCard({ label, value, sub, color = "text-terminal-text" }) {
  return (
    <Card title={label}>
      <div className={`text-2xl font-bold ${color}`}>{value}</div>
      {sub && <div className="text-terminal-muted text-xs mt-1">{sub}</div>}
    </Card>
  );
}

function PickCard({ pick, type }) {
  const ret = pick.return_5d;
  const positive = (ret || 0) >= 0;
  return (
    <div className={`p-3 rounded border text-xs ${
      type === "best"
        ? "border-terminal-green/40 bg-terminal-green/5"
        : "border-terminal-red/40 bg-terminal-red/5"
    }`}>
      <div className="flex justify-between mb-1">
        <span className="text-terminal-accent font-bold">{pick.stock_code} {pick.stock_name}</span>
        <span className={`font-bold ${positive ? "text-terminal-green" : "text-terminal-red"}`}>
          {(ret || 0) >= 0 ? "+" : ""}{(ret || 0).toFixed(2)}%
        </span>
      </div>
      <div className="text-terminal-muted">{pick.recommend_date}</div>
      <div className="text-terminal-muted mt-1">推薦價：{pick.recommend_price} → 5日後：{pick.price_5d}</div>
      {pick.ai_reason && (
        <div className="text-terminal-text mt-1 leading-relaxed">{pick.ai_reason.slice(0, 80)}...</div>
      )}
    </div>
  );
}

export default function RecommendationPerformance() {
  const [stats, setStats]         = useState(null);
  const [weights, setWeights]     = useState([]);
  const [currW, setCurrW]         = useState(null);
  const [loading, setLoading]     = useState(false);
  const [msg, setMsg]             = useState("");
  const [days, setDays]           = useState(30);

  const flash = (t) => { setMsg(t); setTimeout(() => setMsg(""), 5000); };

  const load = async () => {
    setLoading(true);
    try {
      const [s, wh, cw] = await Promise.allSettled([
        getAccuracyStats(days),
        getWeightHistory(20),
        getCurrentWeights(),
      ]);
      if (s.status === "fulfilled")  setStats(s.value);
      if (wh.status === "fulfilled") setWeights(wh.value);
      if (cw.status === "fulfilled") setCurrW(cw.value);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [days]);

  const handleBackfill = async () => {
    flash("回填中，請稍候...");
    try { await triggerBackfill(); flash("✓ 回填任務已啟動"); }
    catch (e) { flash("失敗：" + e.message); }
  };

  const handleAdjust = async () => {
    flash("調整權重中...");
    try { await triggerWeightAdjust(); flash("✓ 權重已調整"); load(); }
    catch (e) { flash("失敗：" + e.message); }
  };

  const daily = stats?.daily_win_rates || [];

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest flex items-center gap-2">
          <Target size={16} /> AI 推薦績效
        </h1>
        <div className="flex gap-2 items-center">
          <select
            value={days}
            onChange={e => setDays(Number(e.target.value))}
            className="bg-terminal-surface border border-terminal-border rounded px-2 py-1 text-xs text-terminal-text"
          >
            {[14, 30, 60, 90].map(d => <option key={d} value={d}>{d} 日</option>)}
          </select>
          <button onClick={handleBackfill}
            className="px-3 py-1.5 border border-terminal-border text-terminal-muted text-xs rounded hover:text-terminal-text flex items-center gap-1">
            <RefreshCw size={12} /> 回填股價
          </button>
          <button onClick={handleAdjust}
            className="px-3 py-1.5 bg-terminal-yellow/10 border border-terminal-yellow text-terminal-yellow text-xs rounded hover:bg-terminal-yellow/20">
            ⚖️ 調整權重
          </button>
        </div>
      </div>

      {msg && <div className="text-terminal-accent text-xs px-3 py-2 bg-terminal-accent/10 border border-terminal-accent/30 rounded">{msg}</div>}

      {/* 統計概覽 */}
      {stats && stats.total > 0 ? (
        <>
          <div className="grid grid-cols-4 gap-3">
            <StatCard label="總推薦筆數"  value={stats.total}           color="text-terminal-text" />
            <StatCard label="5日勝率"     value={`${stats.win_rate}%`}  color={stats.win_rate >= 60 ? WIN_COLOR : stats.win_rate >= 40 ? "text-terminal-yellow" : LOSE_COLOR} />
            <StatCard label="平均5日報酬" value={`${stats.avg_return >= 0 ? "+" : ""}${stats.avg_return}%`} color={stats.avg_return >= 0 ? WIN_COLOR : LOSE_COLOR} />
            <StatCard label="成功門檻"    value={`+${stats.threshold}%`} color="text-terminal-muted" />
          </div>

          {/* 每日勝率折線圖 */}
          {daily.length > 1 && (
            <Card title="每日推薦勝率趨勢">
              <ResponsiveContainer width="100%" height={200}>
                <LineChart data={daily}>
                  <CartesianGrid stroke="#1e3a5f" strokeDasharray="3 3" />
                  <XAxis dataKey="date" tick={{ fill: "#4a6080", fontSize: 9 }}
                    tickFormatter={v => v.slice(5)} />
                  <YAxis tick={{ fill: "#4a6080", fontSize: 9 }} unit="%" domain={[0, 100]} />
                  <Tooltip
                    contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
                    formatter={(v, name) => [
                      name === "win_rate" ? `${v}%` : `${v >= 0 ? "+" : ""}${v}%`,
                      name === "win_rate" ? "勝率" : "平均報酬",
                    ]}
                  />
                  <ReferenceLine y={50} stroke="#ffcc00" strokeDasharray="4 2" />
                  <Line type="monotone" dataKey="win_rate" name="win_rate"
                    stroke={WIN_COLOR} dot strokeWidth={2} />
                  <Line type="monotone" dataKey="avg_ret" name="avg_ret"
                    stroke="#00d4ff" dot={false} strokeWidth={1.5} strokeDasharray="4 2" />
                  <Legend wrapperStyle={{ fontSize: 10, color: "#4a6080" }} />
                </LineChart>
              </ResponsiveContainer>
            </Card>
          )}

          {/* 最佳/最差案例 */}
          <div className="grid grid-cols-2 gap-4">
            {stats.best_picks?.length > 0 && (
              <Card title="🏆 最佳推薦">
                <div className="space-y-2">
                  {stats.best_picks.map(p => <PickCard key={p.stock_code + p.recommend_date} pick={p} type="best" />)}
                </div>
              </Card>
            )}
            {stats.worst_picks?.length > 0 && (
              <Card title="💔 最差推薦">
                <div className="space-y-2">
                  {stats.worst_picks.map(p => <PickCard key={p.stock_code + p.recommend_date} pick={p} type="worst" />)}
                </div>
              </Card>
            )}
          </div>
        </>
      ) : (
        <div className="text-center py-16 text-terminal-muted">
          <div className="text-4xl mb-3">📊</div>
          <div>{loading ? "載入中..." : "尚無推薦績效資料"}</div>
          <div className="text-xs mt-2">推薦後需 5 個交易日回填才有統計</div>
          <div className="text-xs text-terminal-accent mt-1">可點選「回填股價」手動觸發</div>
        </div>
      )}

      {/* 當前動態權重 */}
      {currW && (
        <Card title="當前評分動態權重">
          <div className="grid grid-cols-3 gap-4 text-center">
            {[
              { label: "基本面", key: "fundamental", color: "#00ff88" },
              { label: "籌碼面", key: "chip",        color: "#00d4ff" },
              { label: "技術面", key: "technical",   color: "#ffcc00" },
            ].map(({ label, key, color }) => (
              <div key={key}>
                <div className="text-terminal-muted text-xs">{label}</div>
                <div className="text-2xl font-bold mt-1" style={{ color }}>
                  {((currW[key] || 0) * 100).toFixed(0)}%
                </div>
                <div className="mt-2 h-2 bg-terminal-border rounded-full overflow-hidden">
                  <div className="h-full rounded-full" style={{ width: `${(currW[key] || 0) * 100}%`, backgroundColor: color }} />
                </div>
              </div>
            ))}
          </div>
          <div className="text-terminal-muted text-xs mt-3 text-center">
            每週一根據推薦準確率自動調整（勝率 &gt;70% → +5%，&lt;40% → -5%）
          </div>
        </Card>
      )}

      {/* 權重歷史變化圖 */}
      {weights.length > 1 && (
        <Card title="評分權重歷史變化">
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={weights}>
              <CartesianGrid stroke="#1e3a5f" strokeDasharray="3 3" />
              <XAxis dataKey="date" tick={{ fill: "#4a6080", fontSize: 9 }} tickFormatter={v => v.slice(5)} />
              <YAxis tick={{ fill: "#4a6080", fontSize: 9 }} domain={[0, 0.7]}
                tickFormatter={v => `${(v * 100).toFixed(0)}%`} />
              <Tooltip
                contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
                formatter={(v) => [`${(v * 100).toFixed(1)}%`]}
              />
              <Line type="monotone" dataKey="fundamental_weight" name="基本面" stroke="#00ff88" dot strokeWidth={2} />
              <Line type="monotone" dataKey="chip_weight"        name="籌碼面" stroke="#00d4ff" dot strokeWidth={2} />
              <Line type="monotone" dataKey="technical_weight"   name="技術面" stroke="#ffcc00" dot strokeWidth={2} />
              <Legend wrapperStyle={{ fontSize: 10, color: "#4a6080" }} />
            </LineChart>
          </ResponsiveContainer>
          {weights.length > 0 && weights[weights.length - 1].overall_win_rate && (
            <div className="mt-2 text-xs text-terminal-muted text-center">
              最新整體勝率：
              <span className={`font-bold ml-1 ${(weights[weights.length-1].overall_win_rate || 0) >= 60 ? "text-terminal-green" : "text-terminal-red"}`}>
                {weights[weights.length - 1].overall_win_rate}%
              </span>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
