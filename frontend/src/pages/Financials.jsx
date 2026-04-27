import React, { useState } from "react";
import { getFinancials, getRevenue, getStockScoreV2, triggerPipeline } from "../utils/api";
import Card from "../components/Card";
import {
  BarChart, Bar, LineChart, Line, XAxis, YAxis, Tooltip,
  ResponsiveContainer, ReferenceLine, CartesianGrid,
} from "recharts";
import { TrendingUp, RefreshCw } from "lucide-react";

function MetricBar({ label, value, max = 60, color = "#00ff88" }) {
  const pct = Math.min(100, Math.max(0, (value / max) * 100));
  return (
    <div className="space-y-0.5">
      <div className="flex justify-between text-xs">
        <span className="text-terminal-muted">{label}</span>
        <span className="text-terminal-text font-mono">{value?.toFixed(1)}%</span>
      </div>
      <div className="h-1.5 bg-terminal-border rounded-full overflow-hidden">
        <div className="h-full rounded-full" style={{ width: `${pct}%`, backgroundColor: color }} />
      </div>
    </div>
  );
}

export default function Financials() {
  const [input, setInput]     = useState("2330");
  const [loading, setLoading] = useState(false);
  const [fin, setFin]         = useState(null);
  const [rev, setRev]         = useState(null);
  const [score, setScore]     = useState(null);
  const [error, setError]     = useState("");
  const [msg, setMsg]         = useState("");

  const search = async (code = input.trim()) => {
    if (!code) return;
    setLoading(true);
    setError("");
    setFin(null); setRev(null); setScore(null);
    try {
      const [f, r, s] = await Promise.allSettled([
        getFinancials(code, 8),
        getRevenue(code, 13),
        getStockScoreV2(code),
      ]);
      if (f.status === "fulfilled") setFin(f.value);
      if (r.status === "fulfilled") setRev(r.value);
      if (s.status === "fulfilled") setScore(s.value);
      if (f.status === "rejected" && r.status === "rejected") {
        setError(`查無 ${code} 財務資料。可點選「抓取資料」先更新。`);
      }
    } finally {
      setLoading(false);
    }
  };

  const handleFetch = async () => {
    const code = input.trim();
    if (!code) return;
    setMsg("正在從 FinMind 抓取資料，約需 10-30 秒...");
    try {
      await triggerPipeline(code);
      setMsg(`✓ ${code} 資料更新完成，請稍候再查詢`);
      setTimeout(() => search(code), 15000);
    } catch (e) {
      setMsg("抓取失敗：" + e.message);
    }
    setTimeout(() => setMsg(""), 10000);
  };

  const finData  = fin?.data  || [];
  const revData  = rev?.data  || [];

  // 月營收圖表資料
  const revChartData = revData.map(r => ({
    label:   `${r.year}-${String(r.month).padStart(2, "0")}`,
    revenue: Math.round((r.revenue || 0) / 1000),  // 千元→百萬
    yoy:     r.yoy,
  }));

  // 三率趨勢圖
  const marginData = finData.map(f => ({
    label:   `${f.year}Q${f.quarter}`,
    gross:   f.gross_margin,
    op:      f.operating_margin,
    net:     f.net_margin,
    eps:     f.eps,
  }));

  const latestFin = finData.at(-1);
  const latestRev = revData.at(-1);

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest flex items-center gap-2">
          <TrendingUp size={16} /> 財務報表分析
        </h1>
      </div>

      <div className="flex gap-2">
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === "Enter" && search()}
          placeholder="股票代碼 (e.g. 2330)"
          className="flex-1 bg-terminal-surface border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
        />
        <button onClick={() => search()} disabled={loading}
          className="px-4 py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-sm rounded hover:bg-terminal-accent/30">
          {loading ? "載入..." : "查詢 ▶"}
        </button>
        <button onClick={handleFetch}
          className="px-3 py-2 border border-terminal-border text-terminal-muted text-sm rounded hover:text-terminal-text flex items-center gap-1">
          <RefreshCw size={13} /> 抓取資料
        </button>
      </div>

      {msg && <div className="text-terminal-accent text-xs px-3 py-2 bg-terminal-accent/10 border border-terminal-accent/30 rounded">{msg}</div>}
      {error && <div className="text-terminal-red text-sm">{error}</div>}

      {/* 評分概覽 */}
      {score && (
        <div className="grid grid-cols-4 gap-3">
          {[
            { label: "基本面評分", val: score.fundamental_score, color: "#00ff88" },
            { label: "籌碼面評分", val: score.chip_score,        color: "#00d4ff" },
            { label: "技術面評分", val: score.technical_score,   color: "#ffcc00" },
            { label: "總分",       val: score.total_score,       color: "#ff8844" },
          ].map(({ label, val, color }) => (
            <Card key={label} title={label}>
              <div className="text-2xl font-bold" style={{ color }}>{val}</div>
              <div className="mt-1.5">
                <div className="h-1.5 bg-terminal-border rounded-full overflow-hidden">
                  <div className="h-full rounded-full transition-all"
                    style={{ width: `${val}%`, backgroundColor: color }} />
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* 最新財務指標 */}
      {latestFin && (
        <Card title={`最新財務指標 (${latestFin.year}Q${latestFin.quarter})`}>
          <div className="grid grid-cols-2 gap-6">
            <div className="space-y-3">
              <MetricBar label="毛利率"   value={latestFin.gross_margin}     max={70} color="#00ff88" />
              <MetricBar label="營業利益率" value={latestFin.operating_margin} max={40} color="#00d4ff" />
              <MetricBar label="淨利率"   value={latestFin.net_margin}       max={35} color="#ffcc00" />
            </div>
            <div className="space-y-2 text-xs">
              {[
                ["EPS",   latestFin.eps !== null ? `${latestFin.eps}` : "—"],
                ["營收 (億)", latestFin.revenue ? `${(latestFin.revenue / 100000).toFixed(1)}` : "—"],
              ].map(([label, val]) => (
                <div key={label} className="flex justify-between border-b border-terminal-border/30 py-1.5">
                  <span className="text-terminal-muted">{label}</span>
                  <span className="text-terminal-text font-mono font-bold">{val}</span>
                </div>
              ))}
              {score && (
                <div className="mt-2 space-y-1">
                  {[
                    ["三率齊升",      score.three_margins_up ? "✓ 是" : "✗ 否"],
                    ["連續EPS成長",  `${score.eps_growth_qtrs}季`],
                    ["外資連買",     `${score.foreign_consec_buy}日`],
                  ].map(([label, val]) => (
                    <div key={label} className="flex justify-between border-b border-terminal-border/30 py-1">
                      <span className="text-terminal-muted">{label}</span>
                      <span className={`${val.startsWith("✓") ? "text-terminal-green" : val.startsWith("✗") ? "text-terminal-red" : "text-terminal-text"} font-bold`}>{val}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </Card>
      )}

      {/* 三率趨勢圖 */}
      {marginData.length > 1 && (
        <Card title="三率趨勢（季度）">
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={marginData}>
              <CartesianGrid stroke="#1e3a5f" strokeDasharray="3 3" />
              <XAxis dataKey="label" tick={{ fill: "#4a6080", fontSize: 9 }} />
              <YAxis tick={{ fill: "#4a6080", fontSize: 9 }} unit="%" />
              <Tooltip
                contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
                formatter={(v, name) => [`${v?.toFixed(1)}%`, name]}
              />
              <ReferenceLine y={0} stroke="#1e3a5f" />
              <Line type="monotone" dataKey="gross" name="毛利率" stroke="#00ff88" dot strokeWidth={2} />
              <Line type="monotone" dataKey="op"    name="營益率" stroke="#00d4ff" dot strokeWidth={2} />
              <Line type="monotone" dataKey="net"   name="淨利率" stroke="#ffcc00" dot strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
          <div className="flex gap-4 justify-center mt-1 text-xs">
            {[["毛利率","#00ff88"],["營益率","#00d4ff"],["淨利率","#ffcc00"]].map(([n,c]) => (
              <span key={n} className="flex items-center gap-1">
                <span style={{ color: c }}>●</span>{n}
              </span>
            ))}
          </div>
        </Card>
      )}

      {/* 月營收趨勢圖 */}
      {revChartData.length > 1 && (
        <Card title="月營收趨勢（百萬元）">
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={revChartData}>
              <CartesianGrid stroke="#1e3a5f" strokeDasharray="3 3" />
              <XAxis dataKey="label" tick={{ fill: "#4a6080", fontSize: 9 }}
                tickFormatter={v => v.slice(5)} />
              <YAxis yAxisId="rev" tick={{ fill: "#4a6080", fontSize: 9 }} />
              <YAxis yAxisId="yoy" orientation="right" tick={{ fill: "#4a6080", fontSize: 9 }} unit="%" />
              <Tooltip
                contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
                formatter={(v, name) => [
                  name === "yoy" ? `${v?.toFixed(1)}%` : `${v?.toLocaleString()}M`,
                  name === "yoy" ? "年增率" : "營收",
                ]}
              />
              <ReferenceLine yAxisId="yoy" y={0} stroke="#1e3a5f" />
              <Bar yAxisId="rev" dataKey="revenue" name="revenue" fill="#1e3a5f" radius={[2,2,0,0]} />
              <Line yAxisId="yoy" type="monotone" dataKey="yoy" name="yoy"
                stroke="#ffcc00" dot={false} strokeWidth={2} />
            </BarChart>
          </ResponsiveContainer>
          {latestRev && (
            <div className="flex gap-4 mt-2 text-xs text-terminal-muted">
              <span>最新月營收：<span className="text-terminal-text font-bold">
                {latestRev.revenue ? `${(latestRev.revenue / 1000).toFixed(0)}百萬` : "—"}
              </span></span>
              <span>年增率：<span className={`font-bold ${(latestRev.yoy || 0) >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                {latestRev.yoy !== null ? `${(latestRev.yoy || 0) >= 0 ? "+" : ""}${latestRev.yoy?.toFixed(1)}%` : "—"}
              </span></span>
            </div>
          )}
        </Card>
      )}

      {/* EPS 季度圖 */}
      {marginData.length > 1 && marginData.some(d => d.eps !== null) && (
        <Card title="EPS 季度趨勢">
          <ResponsiveContainer width="100%" height={150}>
            <BarChart data={marginData}>
              <XAxis dataKey="label" tick={{ fill: "#4a6080", fontSize: 9 }} />
              <YAxis tick={{ fill: "#4a6080", fontSize: 9 }} />
              <Tooltip contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
                formatter={v => [`$${v}`, "EPS"]} />
              <ReferenceLine y={0} stroke="#1e3a5f" />
              <Bar dataKey="eps" name="EPS" fill="#00d4ff" radius={[2,2,0,0]} />
            </BarChart>
          </ResponsiveContainer>
        </Card>
      )}
    </div>
  );
}
