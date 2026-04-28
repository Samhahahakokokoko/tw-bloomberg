import React, { useState } from "react";
import { optimizePortfolio } from "../utils/api";
import Card from "../components/Card";
import {
  ScatterChart, Scatter, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, Cell, ReferenceDot,
} from "recharts";
import { Layers, AlertTriangle } from "lucide-react";

const USER_ID = "";

// ── 相關性熱力圖（純 CSS Grid）────────────────────────────────────────────────
function CorrelationHeatmap({ codes, names, matrix }) {
  if (!codes || codes.length === 0) return null;

  const cellColor = (val) => {
    const abs = Math.abs(val);
    if (val >= 0.8)  return "#ff4466";
    if (val >= 0.5)  return "#ff884460";
    if (val >= 0.2)  return "#ffcc0040";
    if (val <= -0.5) return "#00ff8860";
    if (val <= -0.2) return "#00d4ff40";
    return "#1e3a5f";
  };

  return (
    <div className="overflow-x-auto">
      <div style={{ display: "inline-grid", gridTemplateColumns: `80px repeat(${codes.length}, 64px)`, gap: 2 }}>
        {/* Header row */}
        <div />
        {codes.map((c, i) => (
          <div key={c} className="text-xs text-terminal-muted text-center py-1 truncate" title={names[i]}>{c}</div>
        ))}
        {/* Data rows */}
        {codes.map((rowCode, i) => (
          <React.Fragment key={rowCode}>
            <div className="text-xs text-terminal-muted text-right pr-2 py-1 truncate" title={names[i]}>{rowCode}</div>
            {matrix[i]?.map((val, j) => (
              <div
                key={j}
                className="flex items-center justify-center text-xs rounded font-mono"
                style={{ height: 32, backgroundColor: cellColor(val), color: Math.abs(val) > 0.5 ? "#fff" : "#8898a8" }}
                title={`${codes[i]} & ${codes[j]}: ${val.toFixed(2)}`}
              >
                {val.toFixed(2)}
              </div>
            ))}
          </React.Fragment>
        ))}
      </div>
      <div className="flex gap-4 mt-2 text-xs text-terminal-muted">
        {[
          { color: "#ff4466", label: "高度正相關(>0.8)" },
          { color: "#00ff88", label: "負相關" },
          { color: "#1e3a5f", label: "低相關" },
        ].map(({ color, label }) => (
          <span key={label} className="flex items-center gap-1">
            <span className="w-3 h-3 rounded inline-block" style={{ backgroundColor: color }} />
            {label}
          </span>
        ))}
      </div>
    </div>
  );
}

// ── 效率前緣散點圖 ────────────────────────────────────────────────────────────
function EfficientFrontierChart({ frontier, currentPerf, optimal, minVar }) {
  if (!frontier || frontier.length === 0) return null;

  const frontierData = frontier.map(p => ({
    x: p.volatility,
    y: p.return,
    sharpe: p.sharpe,
  }));

  const curr    = currentPerf ? { x: currentPerf.volatility, y: currentPerf.return } : null;
  const optPt   = optimal     ? { x: optimal.volatility,     y: optimal.return }     : null;
  const minVPt  = minVar      ? { x: minVar.volatility,      y: minVar.return }      : null;

  return (
    <ResponsiveContainer width="100%" height={300}>
      <ScatterChart margin={{ top: 10, right: 20, left: 0, bottom: 10 }}>
        <CartesianGrid stroke="#1e3a5f" strokeDasharray="3 3" />
        <XAxis
          type="number" dataKey="x" name="波動率"
          tick={{ fill: "#4a6080", fontSize: 9 }} unit="%" domain={["auto", "auto"]}
          label={{ value: "年化波動率 %", position: "insideBottom", offset: -5, fill: "#4a6080", fontSize: 9 }}
        />
        <YAxis
          type="number" dataKey="y" name="報酬率"
          tick={{ fill: "#4a6080", fontSize: 9 }} unit="%"
          label={{ value: "年化報酬率 %", angle: -90, position: "insideLeft", fill: "#4a6080", fontSize: 9 }}
        />
        <Tooltip
          cursor={{ strokeDasharray: "3 3" }}
          contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
          formatter={(v, name) => [`${v.toFixed(2)}%`, name]}
        />
        {/* 效率前緣曲線 */}
        <Scatter data={frontierData} name="效率前緣" fill="#1e3a5f">
          {frontierData.map((p, i) => (
            <Cell key={i} fill={p.sharpe > 0.5 ? "#00d4ff" : "#1e3a5f"} opacity={0.7} />
          ))}
        </Scatter>
        {/* 當前組合 */}
        {curr && <ReferenceDot x={curr.x} y={curr.y} r={8} fill="#ff8844" stroke="#fff" strokeWidth={2} label={{ value: "現況", fill: "#ff8844", fontSize: 10 }} />}
        {/* 最佳 Sharpe */}
        {optPt && <ReferenceDot x={optPt.x} y={optPt.y} r={8} fill="#00ff88" stroke="#fff" strokeWidth={2} label={{ value: "最佳", fill: "#00ff88", fontSize: 10 }} />}
        {/* 最低波動 */}
        {minVPt && <ReferenceDot x={minVPt.x} y={minVPt.y} r={6} fill="#ffcc00" stroke="#fff" strokeWidth={2} label={{ value: "低波", fill: "#ffcc00", fontSize: 10 }} />}
      </ScatterChart>
    </ResponsiveContainer>
  );
}

export default function PortfolioOptimizer() {
  const [result, setResult]   = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState("");

  const run = async () => {
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const data = await optimizePortfolio(USER_ID);
      setResult(data);
    } catch (e) {
      setError(e.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  };

  const corr    = result?.correlation   || {};
  const varData = result?.var           || {};
  const currP   = result?.current_performance;
  const opt     = result?.optimal_portfolio;
  const minV    = result?.min_var_portfolio;
  const suggs   = result?.rebalance_suggestions || [];
  const frontier = result?.frontier     || [];

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest flex items-center gap-2">
          <Layers size={16} /> 投資組合最佳化
        </h1>
        <button
          onClick={run}
          disabled={loading}
          className="px-4 py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-sm rounded hover:bg-terminal-accent/30 flex items-center gap-2"
        >
          {loading ? "計算中..." : "▶ 執行分析"}
        </button>
      </div>

      {error && <div className="text-terminal-red text-sm px-3 py-2 bg-terminal-red/10 border border-terminal-red/30 rounded">{error}</div>}

      {!result && !loading && (
        <div className="text-center py-16 text-terminal-muted">
          <div className="text-4xl mb-3">📐</div>
          <div className="mb-2">馬可維茲效率前緣分析</div>
          <div className="text-xs">需要至少 2 檔持股 + FinMind 歷史股價</div>
          <button onClick={run} className="mt-4 px-4 py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-sm rounded hover:bg-terminal-accent/30">
            點此開始分析
          </button>
        </div>
      )}

      {result && (
        <>
          {/* 績效比較 */}
          <div className="grid grid-cols-3 gap-3">
            {[
              { label: "現有組合", data: currP,  color: "#ff8844" },
              { label: "最佳Sharpe", data: opt,  color: "#00ff88" },
              { label: "最低波動",  data: minV,  color: "#ffcc00" },
            ].map(({ label, data, color }) => data && (
              <Card key={label} title={label}>
                <div className="space-y-1 text-xs">
                  {[
                    ["年化報酬", `${(data.return || 0) >= 0 ? "+" : ""}${(data.return || 0).toFixed(1)}%`],
                    ["年化波動", `${(data.volatility || 0).toFixed(1)}%`],
                    ["Sharpe",   (data.sharpe || 0).toFixed(3)],
                  ].map(([k, v]) => (
                    <div key={k} className="flex justify-between border-b border-terminal-border/30 py-1">
                      <span className="text-terminal-muted">{k}</span>
                      <span className="font-bold" style={{ color }}>{v}</span>
                    </div>
                  ))}
                </div>
              </Card>
            ))}
          </div>

          {/* 效率前緣圖 */}
          {frontier.length > 0 && (
            <Card title="效率前緣曲線 (藍點=高Sharpe | 橘=現況 | 綠=最佳 | 黃=低波動)">
              <EfficientFrontierChart
                frontier={frontier}
                currentPerf={currP}
                optimal={opt}
                minVar={minV}
              />
            </Card>
          )}

          {/* 調倉建議 */}
          {suggs.length > 0 && (
            <Card title="調倉建議（差異 ≥ 2%）">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-terminal-muted border-b border-terminal-border">
                    {["代碼", "名稱", "現有%", "最佳%", "調整", "建議"].map(h => (
                      <th key={h} className="text-left py-1.5 pr-3">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {suggs.map(s => (
                    <tr key={s.stock_code} className="border-b border-terminal-border/30">
                      <td className="py-2 pr-3 text-terminal-accent font-bold">{s.stock_code}</td>
                      <td className="py-2 pr-3">{s.name}</td>
                      <td className="py-2 pr-3">{s.current}%</td>
                      <td className="py-2 pr-3">{s.optimal}%</td>
                      <td className={`py-2 pr-3 font-bold ${s.change > 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                        {s.change > 0 ? "+" : ""}{s.change}%
                      </td>
                      <td className={`py-2 pr-3 font-bold ${s.action === "加碼" ? "text-terminal-green" : "text-terminal-red"}`}>
                        {s.action}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}

          {/* VaR 風險值 */}
          {varData && Object.keys(varData).length > 0 && (
            <Card title="VaR 風險值（95% 信心水準）">
              <div className="grid grid-cols-2 gap-6">
                <div>
                  <div className="text-terminal-muted text-xs mb-2">歷史模擬法</div>
                  <div className="text-terminal-red text-2xl font-bold">
                    -{(varData.hist_var_amount || 0).toLocaleString()} 元
                  </div>
                  <div className="text-terminal-muted text-xs">{varData.hist_var_pct}% 日損失上限</div>
                </div>
                <div>
                  <div className="text-terminal-muted text-xs mb-2">參數法（常態假設）</div>
                  <div className="text-terminal-red text-2xl font-bold">
                    -{(varData.param_var_amount || 0).toLocaleString()} 元
                  </div>
                  <div className="text-terminal-muted text-xs">{varData.param_var_pct}% 日損失上限</div>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-3 mt-4 text-xs">
                {[
                  ["CVaR 極端損失", `${(varData.cvar_amount || 0).toLocaleString()} 元`],
                  ["歷史最差日", `${varData.worst_day_pct}%`],
                  ["日均報酬率", `${(varData.avg_daily_return_pct || 0) >= 0 ? "+" : ""}${varData.avg_daily_return_pct}%`],
                ].map(([label, val]) => (
                  <div key={label} className="text-center p-2 bg-terminal-surface/50 rounded">
                    <div className="text-terminal-muted">{label}</div>
                    <div className="text-terminal-text font-bold mt-1">{val}</div>
                  </div>
                ))}
              </div>
            </Card>
          )}

          {/* 相關性矩陣熱力圖 */}
          {corr.codes && corr.codes.length > 0 && (
            <Card title="相關性矩陣熱力圖">
              {corr.warnings?.length > 0 && (
                <div className="flex items-start gap-2 mb-3 p-2 bg-terminal-red/10 border border-terminal-red/30 rounded text-xs">
                  <AlertTriangle size={14} className="text-terminal-red flex-shrink-0 mt-0.5" />
                  <div>
                    <div className="text-terminal-red font-bold mb-1">高度相關警示（&gt;0.8）</div>
                    {corr.warnings.map((w, i) => (
                      <div key={i} className="text-terminal-text">{w}</div>
                    ))}
                  </div>
                </div>
              )}
              {!corr.warnings?.length && (
                <div className="text-terminal-green text-xs mb-3">✓ 持股相關性良好，分散效果佳</div>
              )}
              <CorrelationHeatmap
                codes={corr.codes}
                names={corr.names}
                matrix={corr.matrix}
              />
            </Card>
          )}
        </>
      )}
    </div>
  );
}
