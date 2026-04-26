import React, { useState } from "react";
import { runBacktest } from "../utils/api";
import Card from "../components/Card";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from "recharts";

const STRATEGIES = [
  { value: "ma_cross",     label: "MA 均線交叉" },
  { value: "rsi",          label: "RSI 超買超賣" },
  { value: "macd",         label: "MACD 黃金死叉" },
  { value: "kd",           label: "KD 隨機指標" },
  { value: "bollinger",    label: "布林通道突破" },
  { value: "pvd",          label: "價量背離" },
  { value: "institutional",label: "籌碼面（外資連買）" },
];

export default function Backtest() {
  const [form, setForm] = useState({
    stock_code: "2330",
    strategy: "ma_cross",
    initial_capital: 1000000,
    short: 5,
    long_: 20,
    period: 14,
    overbought: 70,
    oversold: 30,
    fast: 12,
    slow: 26,
    signal: 9,
    k_period: 3,
    d_period: 3,
    std_mult: 2.0,
    pvd_period: 10,
    consec_buy: 3,
    consec_sell: 2,
  });
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleRun = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const r = await runBacktest({ ...form });
      setResult(r);
    } catch (e) {
      setError(e.response?.data?.detail || "回測失敗");
    } finally {
      setLoading(false);
    }
  };

  const f = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  return (
    <div className="p-4 space-y-4">
      <div className="border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest">◈ BACKTEST ENGINE</h1>
      </div>

      <div className="grid grid-cols-3 gap-4">
        {/* Config */}
        <form onSubmit={handleRun} className="col-span-1 space-y-3">
          <Card title="策略設定">
            <div className="space-y-3 text-xs">
              <div>
                <label className="text-terminal-muted">股票代碼</label>
                <input value={form.stock_code} onChange={f("stock_code")} className={inputCls} />
              </div>
              <div>
                <label className="text-terminal-muted">策略</label>
                <select value={form.strategy} onChange={f("strategy")} className={inputCls}>
                  {STRATEGIES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                </select>
              </div>
              <div>
                <label className="text-terminal-muted">初始資金</label>
                <input value={form.initial_capital} onChange={f("initial_capital")} type="number" className={inputCls} />
              </div>

              {form.strategy === "ma_cross" && (
                <>
                  <div><label className="text-terminal-muted">短期 MA</label><input value={form.short} onChange={f("short")} type="number" className={inputCls} /></div>
                  <div><label className="text-terminal-muted">長期 MA</label><input value={form.long_} onChange={f("long_")} type="number" className={inputCls} /></div>
                </>
              )}
              {form.strategy === "rsi" && (
                <>
                  <div><label className="text-terminal-muted">RSI 週期</label><input value={form.period} onChange={f("period")} type="number" className={inputCls} /></div>
                  <div><label className="text-terminal-muted">超買</label><input value={form.overbought} onChange={f("overbought")} type="number" className={inputCls} /></div>
                  <div><label className="text-terminal-muted">超賣</label><input value={form.oversold} onChange={f("oversold")} type="number" className={inputCls} /></div>
                </>
              )}
              {form.strategy === "macd" && (
                <>
                  <div><label className="text-terminal-muted">快線</label><input value={form.fast} onChange={f("fast")} type="number" className={inputCls} /></div>
                  <div><label className="text-terminal-muted">慢線</label><input value={form.slow} onChange={f("slow")} type="number" className={inputCls} /></div>
                  <div><label className="text-terminal-muted">訊號</label><input value={form.signal} onChange={f("signal")} type="number" className={inputCls} /></div>
                </>
              )}
              {form.strategy === "kd" && (
                <>
                  <div><label className="text-terminal-muted">K 平滑</label><input value={form.k_period} onChange={f("k_period")} type="number" className={inputCls} /></div>
                  <div><label className="text-terminal-muted">D 平滑</label><input value={form.d_period} onChange={f("d_period")} type="number" className={inputCls} /></div>
                </>
              )}
              {form.strategy === "bollinger" && (
                <>
                  <div><label className="text-terminal-muted">MA 週期</label><input value={form.period} onChange={f("period")} type="number" className={inputCls} /></div>
                  <div><label className="text-terminal-muted">標準差倍數</label><input value={form.std_mult} onChange={f("std_mult")} type="number" step="0.1" className={inputCls} /></div>
                </>
              )}
              {form.strategy === "pvd" && (
                <div><label className="text-terminal-muted">觀察週期</label><input value={form.pvd_period} onChange={f("pvd_period")} type="number" className={inputCls} /></div>
              )}
              {form.strategy === "institutional" && (
                <>
                  <div><label className="text-terminal-muted">連續買超 N 日</label><input value={form.consec_buy} onChange={f("consec_buy")} type="number" className={inputCls} /></div>
                  <div><label className="text-terminal-muted">連續賣超 M 日</label><input value={form.consec_sell} onChange={f("consec_sell")} type="number" className={inputCls} /></div>
                </>
              )}

              <button
                type="submit"
                disabled={loading}
                className="w-full py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent rounded hover:bg-terminal-accent/30 transition-colors text-xs"
              >
                {loading ? "回測中..." : "▶ 執行回測"}
              </button>
            </div>
          </Card>
        </form>

        {/* Results */}
        <div className="col-span-2 space-y-3">
          {error && <div className="text-terminal-red text-sm bg-terminal-red/10 border border-terminal-red/30 rounded p-3">{error}</div>}
          {result && (
            <>
              <Card title="回測結果">
                <div className="grid grid-cols-4 gap-3 text-xs">
                  {[
                    ["總報酬", `${result.total_return > 0 ? "+" : ""}${result.total_return}%`, result.total_return >= 0],
                    ["年化報酬", `${result.annualized_return > 0 ? "+" : ""}${result.annualized_return}%`, result.annualized_return >= 0],
                    ["最大回撤", `${result.max_drawdown}%`, false],
                    ["夏普比率", result.sharpe_ratio, result.sharpe_ratio >= 1],
                    ["總交易次數", result.total_trades, null],
                    ["勝率", `${result.win_rate}%`, result.win_rate >= 50],
                    ["初始資金", result.initial_capital.toLocaleString(), null],
                    ["最終資金", result.final_capital.toLocaleString(), null],
                  ].map(([label, val, up]) => (
                    <div key={label} className="bg-terminal-bg rounded p-2">
                      <div className="text-terminal-muted">{label}</div>
                      <div className={`text-base font-bold mt-0.5 ${up === null ? "text-terminal-text" : up ? "text-terminal-green" : "text-terminal-red"}`}>
                        {val}
                      </div>
                    </div>
                  ))}
                </div>
              </Card>

              {result.equity_curve?.length > 0 && (
                <Card title="資產曲線">
                  <ResponsiveContainer width="100%" height={220}>
                    <LineChart data={result.equity_curve}>
                      <XAxis dataKey="date" tick={{ fill: "#4a6080", fontSize: 9 }} tickLine={false}
                        axisLine={{ stroke: "#1e3a5f" }} interval={Math.floor(result.equity_curve.length / 8)} />
                      <YAxis tick={{ fill: "#4a6080", fontSize: 9 }} tickLine={false} axisLine={false}
                        tickFormatter={(v) => (v / 10000).toFixed(0) + "萬"} />
                      <Tooltip contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 10 }}
                        formatter={(v) => v.toLocaleString()} />
                      <ReferenceLine y={result.initial_capital} stroke="#4a6080" strokeDasharray="3 3" />
                      <Line type="monotone" dataKey="value" stroke="#00d4ff" dot={false} strokeWidth={1.5} />
                    </LineChart>
                  </ResponsiveContainer>
                </Card>
              )}

              {result.trades?.length > 0 && (
                <Card title="交易紀錄">
                  <div className="max-h-48 overflow-y-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-terminal-muted border-b border-terminal-border">
                          {["日期", "動作", "股價", "股數", "損益"].map((h) => (
                            <th key={h} className="text-left py-1 pr-3">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {result.trades.map((t, i) => (
                          <tr key={i} className="border-b border-terminal-border/20">
                            <td className="py-1 pr-3 text-terminal-muted">{t.date}</td>
                            <td className={`py-1 pr-3 ${t.action === "BUY" ? "text-terminal-green" : "text-terminal-red"}`}>
                              {t.action}
                            </td>
                            <td className="py-1 pr-3">{t.price}</td>
                            <td className="py-1 pr-3">{t.shares?.toLocaleString()}</td>
                            <td className={`py-1 ${t.pnl >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                              {t.pnl != null ? `${t.pnl >= 0 ? "+" : ""}${Math.round(t.pnl).toLocaleString()}` : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Card>
              )}
            </>
          )}
          {!result && !loading && (
            <div className="flex items-center justify-center h-48 text-terminal-muted text-sm border border-terminal-border/30 rounded">
              設定策略參數後點擊「執行回測」
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

const inputCls = "w-full mt-0.5 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 font-mono text-terminal-text focus:outline-none focus:border-terminal-accent text-xs";
