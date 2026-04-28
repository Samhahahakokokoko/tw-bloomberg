import React, { useState, useEffect } from "react";
import { runBacktest } from "../utils/api";
import api from "../utils/api";
import Card from "../components/Card";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  ReferenceLine, CartesianGrid,
} from "recharts";

const STRATEGIES = [
  { value: "ma_cross",       label: "MA 均線交叉",        regime: "all"      },
  { value: "rsi",            label: "RSI 超買超賣",        regime: "sideways" },
  { value: "macd",           label: "MACD 黃金死叉",       regime: "bull"     },
  { value: "kd",             label: "KD 隨機指標",         regime: "all"      },
  { value: "bollinger",      label: "布林通道突破",         regime: "sideways" },
  { value: "pvd",            label: "價量背離",             regime: "all"      },
  { value: "institutional",  label: "籌碼面（外資連買）",   regime: "bull"     },
  { value: "momentum",       label: "動能追漲 📈多頭",      regime: "bull"     },
  { value: "mean_reversion", label: "均值回歸 ↔️盤整",      regime: "sideways" },
  { value: "defensive",      label: "防禦型 📉空頭",        regime: "bear"     },
];

const REGIME_COLOR = { bull: "#00ff88", bear: "#ff4466", sideways: "#ffcc00", unknown: "#4a6080" };
const REGIME_LABEL = { bull: "多頭行情 📈", bear: "空頭行情 📉", sideways: "盤整行情 ↔️", unknown: "未知" };

const inputCls = "w-full mt-0.5 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 font-mono text-terminal-text focus:outline-none focus:border-terminal-accent text-xs";

export default function Backtest() {
  const [form, setForm] = useState({
    stock_code: "2330", strategy: "ma_cross", initial_capital: 1000000,
    short: 5, long_: 20,
    period: 14, overbought: 70, oversold: 30,
    fast: 12, slow: 26, signal: 9,
    k_period: 3, d_period: 3,
    std_mult: 2.0, pvd_period: 10,
    consec_buy: 3, consec_sell: 2,
    lookback: 20, threshold: 0.05,
    save_result: true,
  });
  const [result, setResult]   = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState("");
  const [regime, setRegime]   = useState(null);
  const [regimeLoading, setRL]= useState(false);

  const f = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  // 查詢代碼時偵測盤態
  const fetchRegime = async (code = form.stock_code) => {
    if (!code || code.length < 4) return;
    setRL(true);
    try {
      const r = await api.get(`/api/backtest/regime/${code}`).then(r => r.data);
      setRegime(r);
      // 自動選推薦策略
      if (r.recommended_strategy) {
        setForm(prev => ({ ...prev, strategy: r.recommended_strategy }));
      }
    } catch { setRegime(null); }
    finally { setRL(false); }
  };

  useEffect(() => { fetchRegime(); }, []);

  const handleRun = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const r = await runBacktest({ ...form,
        initial_capital: Number(form.initial_capital),
        short: Number(form.short), long_: Number(form.long_),
        period: Number(form.period),
        fast: Number(form.fast), slow: Number(form.slow), signal: Number(form.signal),
      });
      setResult(r);
    } catch (e) {
      setError(e.response?.data?.detail || "回測失敗");
    } finally {
      setLoading(false);
    }
  };

  const regimeColor = REGIME_COLOR[regime?.current] || "#4a6080";

  return (
    <div className="p-4 space-y-4">
      <div className="border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest">◈ BACKTEST ENGINE v2</h1>
        <div className="text-terminal-muted text-xs mt-0.5">含真實交易成本：手續費0.1425% + 稅0.3% + 滑價0.05% + 漲跌停限制</div>
      </div>

      <div className="grid grid-cols-3 gap-4">
        {/* 設定欄 */}
        <form onSubmit={handleRun} className="col-span-1 space-y-3">
          {/* 盤態偵測 */}
          {regime && (
            <div className="p-2 rounded border text-xs" style={{ borderColor: regimeColor + "60", backgroundColor: regimeColor + "12" }}>
              <div className="font-bold mb-0.5" style={{ color: regimeColor }}>
                {REGIME_LABEL[regime.current]}
              </div>
              {regime.recommended_strategy && (
                <div className="text-terminal-muted">推薦策略：{STRATEGIES.find(s => s.value === regime.recommended_strategy)?.label}</div>
              )}
              {regime.ma200 && (
                <div className="text-terminal-muted mt-0.5">MA5={regime.ma5} MA20={regime.ma20} MA200={regime.ma200}</div>
              )}
            </div>
          )}

          <Card title="策略設定">
            <div className="space-y-2 text-xs">
              <div className="flex gap-1">
                <div className="flex-1">
                  <label className="text-terminal-muted">股票代碼</label>
                  <input value={form.stock_code} onChange={f("stock_code")}
                    onBlur={() => fetchRegime(form.stock_code)}
                    className={inputCls} />
                </div>
                <button type="button" onClick={() => fetchRegime(form.stock_code)}
                  className="mt-4 px-2 text-terminal-muted border border-terminal-border rounded text-xs hover:text-terminal-text">
                  {regimeLoading ? "⟳" : "盤態"}
                </button>
              </div>

              <div>
                <label className="text-terminal-muted">策略</label>
                <select value={form.strategy} onChange={f("strategy")} className={inputCls}>
                  {STRATEGIES.map(s => (
                    <option key={s.value} value={s.value}>{s.label}</option>
                  ))}
                </select>
              </div>

              <div>
                <label className="text-terminal-muted">初始資金</label>
                <input value={form.initial_capital} onChange={f("initial_capital")} type="number" className={inputCls} />
              </div>

              {/* 策略參數 */}
              {form.strategy === "ma_cross" && (<>
                <div><label className="text-terminal-muted">短期MA</label><input value={form.short} onChange={f("short")} type="number" className={inputCls} /></div>
                <div><label className="text-terminal-muted">長期MA</label><input value={form.long_} onChange={f("long_")} type="number" className={inputCls} /></div>
              </>)}
              {form.strategy === "rsi" && (<>
                <div><label className="text-terminal-muted">RSI週期</label><input value={form.period} onChange={f("period")} type="number" className={inputCls} /></div>
                <div><label className="text-terminal-muted">超買</label><input value={form.overbought} onChange={f("overbought")} type="number" className={inputCls} /></div>
                <div><label className="text-terminal-muted">超賣</label><input value={form.oversold} onChange={f("oversold")} type="number" className={inputCls} /></div>
              </>)}
              {form.strategy === "macd" && (<>
                <div><label className="text-terminal-muted">快線</label><input value={form.fast} onChange={f("fast")} type="number" className={inputCls} /></div>
                <div><label className="text-terminal-muted">慢線</label><input value={form.slow} onChange={f("slow")} type="number" className={inputCls} /></div>
                <div><label className="text-terminal-muted">訊號</label><input value={form.signal} onChange={f("signal")} type="number" className={inputCls} /></div>
              </>)}
              {form.strategy === "kd" && (<>
                <div><label className="text-terminal-muted">K平滑</label><input value={form.k_period} onChange={f("k_period")} type="number" className={inputCls} /></div>
                <div><label className="text-terminal-muted">D平滑</label><input value={form.d_period} onChange={f("d_period")} type="number" className={inputCls} /></div>
              </>)}
              {["bollinger", "mean_reversion"].includes(form.strategy) && (<>
                <div><label className="text-terminal-muted">MA週期</label><input value={form.period} onChange={f("period")} type="number" className={inputCls} /></div>
                <div><label className="text-terminal-muted">標準差倍數</label><input value={form.std_mult} onChange={f("std_mult")} type="number" step="0.1" className={inputCls} /></div>
              </>)}
              {form.strategy === "pvd" && (
                <div><label className="text-terminal-muted">觀察週期</label><input value={form.pvd_period} onChange={f("pvd_period")} type="number" className={inputCls} /></div>
              )}
              {form.strategy === "institutional" && (<>
                <div><label className="text-terminal-muted">連續買超N日</label><input value={form.consec_buy} onChange={f("consec_buy")} type="number" className={inputCls} /></div>
                <div><label className="text-terminal-muted">連續賣超M日</label><input value={form.consec_sell} onChange={f("consec_sell")} type="number" className={inputCls} /></div>
              </>)}
              {form.strategy === "momentum" && (<>
                <div><label className="text-terminal-muted">回望期（日）</label><input value={form.lookback} onChange={f("lookback")} type="number" className={inputCls} /></div>
                <div><label className="text-terminal-muted">門檻（%）</label><input value={form.threshold * 100} onChange={(e) => setForm({ ...form, threshold: e.target.value / 100 })} type="number" step="0.5" className={inputCls} /></div>
              </>)}
              {["defensive", "rsi"].includes(form.strategy) && (
                <div><label className="text-terminal-muted">RSI週期</label><input value={form.period} onChange={f("period")} type="number" className={inputCls} /></div>
              )}

              <div className="flex items-center gap-2 pt-1">
                <input type="checkbox" checked={form.save_result}
                  onChange={e => setForm({ ...form, save_result: e.target.checked })}
                  className="accent-terminal-accent" />
                <label className="text-terminal-muted">存入 Feedback DB</label>
              </div>

              <button type="submit" disabled={loading}
                className="w-full py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent rounded hover:bg-terminal-accent/30 text-xs">
                {loading ? "回測中..." : "▶ 執行回測"}
              </button>
            </div>
          </Card>
        </form>

        {/* 結果欄 */}
        <div className="col-span-2 space-y-3">
          {error && <div className="text-terminal-red text-sm bg-terminal-red/10 border border-terminal-red/30 rounded p-3">{error}</div>}

          {result && (
            <>
              {/* 盤態標記 */}
              {result.regime && result.regime !== "unknown" && (
                <div className="text-xs px-3 py-2 rounded border" style={{
                  borderColor: REGIME_COLOR[result.regime] + "60",
                  backgroundColor: REGIME_COLOR[result.regime] + "10",
                  color: REGIME_COLOR[result.regime],
                }}>
                  回測期間市場盤態：{REGIME_LABEL[result.regime]}
                </div>
              )}

              {/* 績效指標 */}
              <Card title="回測結果（含真實成本）">
                <div className="grid grid-cols-4 gap-2 text-xs">
                  {[
                    ["總報酬", `${result.total_return > 0 ? "+" : ""}${result.total_return}%`, result.total_return >= 0],
                    ["年化報酬", `${result.annualized_return > 0 ? "+" : ""}${result.annualized_return}%`, result.annualized_return >= 0],
                    ["最大回撤", `${result.max_drawdown}%`, false],
                    ["Sharpe",  result.sharpe_ratio, result.sharpe_ratio >= 1],
                    ["交易次數", result.total_trades, null],
                    ["勝率",    `${result.win_rate}%`, result.win_rate >= 50],
                    ["初始資金", (result.initial_capital / 10000).toFixed(0) + "萬", null],
                    ["最終資金", (result.final_capital / 10000).toFixed(0) + "萬", result.final_capital >= result.initial_capital],
                  ].map(([label, val, up]) => (
                    <div key={label} className="bg-terminal-bg rounded p-2">
                      <div className="text-terminal-muted">{label}</div>
                      <div className={`text-sm font-bold mt-0.5 ${up === null ? "text-terminal-text" : up ? "text-terminal-green" : "text-terminal-red"}`}>
                        {val}
                      </div>
                    </div>
                  ))}
                </div>

                {/* 成本明細 */}
                {(result.total_commission || result.total_tax) && (
                  <div className="mt-3 pt-3 border-t border-terminal-border grid grid-cols-4 gap-2 text-xs">
                    <div className="text-terminal-muted col-span-4 mb-1">成本明細</div>
                    {[
                      ["手續費", result.total_commission?.toLocaleString()],
                      ["證交稅", result.total_tax?.toLocaleString()],
                      ["滑價",   result.total_slippage?.toLocaleString()],
                      ["成本影響", `${result.total_cost_impact?.toFixed(2)}%`],
                    ].map(([label, val]) => (
                      <div key={label} className="bg-terminal-red/5 rounded p-1.5">
                        <div className="text-terminal-muted">{label}</div>
                        <div className="text-terminal-red font-mono">{val}</div>
                      </div>
                    ))}
                  </div>
                )}
              </Card>

              {/* 資產曲線 */}
              {result.equity_curve?.length > 0 && (
                <Card title="資產曲線">
                  <ResponsiveContainer width="100%" height={200}>
                    <LineChart data={result.equity_curve}>
                      <CartesianGrid stroke="#1e3a5f" strokeDasharray="3 3" />
                      <XAxis dataKey="date" tick={{ fill: "#4a6080", fontSize: 9 }} tickLine={false}
                        interval={Math.floor(result.equity_curve.length / 8)}
                        tickFormatter={v => v.slice(5)} />
                      <YAxis tick={{ fill: "#4a6080", fontSize: 9 }} tickLine={false} axisLine={false}
                        tickFormatter={(v) => (v / 10000).toFixed(0) + "萬"} />
                      <Tooltip contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 10 }}
                        formatter={(v) => [v.toLocaleString(), "資產"]} />
                      <ReferenceLine y={result.initial_capital} stroke="#4a6080" strokeDasharray="3 3" />
                      <Line type="monotone" dataKey="value" stroke="#00d4ff" dot={false} strokeWidth={1.5} />
                    </LineChart>
                  </ResponsiveContainer>
                </Card>
              )}

              {/* 交易紀錄 */}
              {result.trades?.length > 0 && (
                <Card title={`交易紀錄 (${result.trades.filter(t => t.action === "SELL").length} round-trips)`}>
                  <div className="max-h-48 overflow-y-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-terminal-muted border-b border-terminal-border sticky top-0 bg-terminal-surface">
                          {["日期", "動作", "股價", "股數", "淨損益", "手續費+稅", "持有天"].map(h => (
                            <th key={h} className="text-left py-1 pr-2">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {result.trades.map((t, i) => (
                          <tr key={i} className="border-b border-terminal-border/20">
                            <td className="py-1 pr-2 text-terminal-muted">{t.date}</td>
                            <td className={`py-1 pr-2 font-bold ${t.action === "BUY" ? "text-terminal-green" : "text-terminal-red"}`}>
                              {t.action}
                            </td>
                            <td className="py-1 pr-2">{t.price}</td>
                            <td className="py-1 pr-2">{t.shares?.toLocaleString()}</td>
                            <td className={`py-1 pr-2 ${(t.pnl || 0) >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                              {t.pnl != null ? `${t.pnl >= 0 ? "+" : ""}${Math.round(t.pnl).toLocaleString()}` : "—"}
                            </td>
                            <td className="py-1 pr-2 text-terminal-muted">
                              {t.commission ? Math.round((t.commission || 0) + (t.tax || 0)).toLocaleString() : "—"}
                            </td>
                            <td className="py-1 text-terminal-muted">{t.holding_days ?? "—"}</td>
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
            <div className="flex flex-col items-center justify-center h-48 text-terminal-muted text-sm border border-terminal-border/30 rounded gap-2">
              <div>設定策略參數後點擊「執行回測」</div>
              <div className="text-xs">系統會自動偵測股票盤態並推薦最適策略</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
