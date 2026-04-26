import React, { useEffect, useState } from "react";
import { getPortfolio, addHolding, deleteHolding, aiPortfolioAnalysis } from "../utils/api";
import Card from "../components/Card";
import PriceTag from "../components/PriceTag";
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer } from "recharts";

const COLORS = ["#00d4ff", "#00ff88", "#ffcc00", "#ff4466", "#8888ff", "#ff8844"];

export default function Portfolio() {
  const [holdings, setHoldings] = useState([]);
  const [form, setForm] = useState({ stock_code: "", shares: "", cost_price: "" });
  const [loading, setLoading] = useState(false);
  const [aiAnalysis, setAiAnalysis] = useState("");
  const [aiLoading, setAiLoading] = useState(false);

  const load = () => getPortfolio().then(setHoldings).catch(console.error);
  useEffect(() => { load(); }, []);

  const handleAdd = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      await addHolding({
        stock_code: form.stock_code,
        shares: parseInt(form.shares),
        cost_price: parseFloat(form.cost_price),
      });
      setForm({ stock_code: "", shares: "", cost_price: "" });
      load();
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (id) => {
    await deleteHolding(id);
    load();
  };

  const handleAiAnalysis = async () => {
    setAiLoading(true);
    setAiAnalysis("");
    try {
      const r = await aiPortfolioAnalysis();
      setAiAnalysis(r.analysis);
    } catch (e) {
      setAiAnalysis("AI 分析失敗：" + (e.response?.data?.detail || e.message));
    } finally {
      setAiLoading(false);
    }
  };

  const totalMV = holdings.reduce((s, h) => s + h.market_value, 0);
  const totalPnL = holdings.reduce((s, h) => s + h.pnl, 0);
  const pieData = holdings.map((h) => ({ name: h.stock_code, value: h.market_value }));

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest">◈ PORTFOLIO</h1>
        <div className="text-right text-sm">
          <div className="text-terminal-muted text-xs">總市值</div>
          <div className="text-terminal-text font-bold">{totalMV.toLocaleString()}</div>
          <PriceTag value={totalPnL} />
        </div>
      </div>

      {/* AI Analysis Button */}
      <div className="flex justify-end">
        <button
          onClick={handleAiAnalysis}
          disabled={aiLoading || holdings.length === 0}
          className="px-4 py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-xs rounded hover:bg-terminal-accent/30 transition-colors disabled:opacity-40"
        >
          {aiLoading ? "⟳ AI 分析中..." : "◈ AI 投資組合分析"}
        </button>
      </div>

      {/* AI Result */}
      {aiAnalysis && (
        <Card title="◈ AI 投資建議">
          <pre className="whitespace-pre-wrap text-xs text-terminal-text leading-relaxed font-mono max-h-96 overflow-y-auto">
            {aiAnalysis}
          </pre>
        </Card>
      )}

      <div className="grid grid-cols-3 gap-4">
        {/* Holdings Table */}
        <div className="col-span-2">
          <Card title="持股清單">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-terminal-muted border-b border-terminal-border">
                  {["代碼", "名稱", "股數", "成本", "現價", "市值", "損益", ""].map((h) => (
                    <th key={h} className="text-left py-1 pr-2">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {holdings.map((h) => (
                  <tr key={h.id} className="border-b border-terminal-border/30 hover:bg-terminal-border/20">
                    <td className="py-1.5 pr-2 text-terminal-accent">{h.stock_code}</td>
                    <td className="py-1.5 pr-2">{h.stock_name}</td>
                    <td className="py-1.5 pr-2">{h.shares.toLocaleString()}</td>
                    <td className="py-1.5 pr-2">{h.cost_price}</td>
                    <td className="py-1.5 pr-2">{h.current_price}</td>
                    <td className="py-1.5 pr-2">{h.market_value.toLocaleString()}</td>
                    <td className="py-1.5 pr-2">
                      <PriceTag value={h.pnl} pct={h.pnl_pct} />
                    </td>
                    <td className="py-1.5">
                      <button
                        onClick={() => handleDelete(h.id)}
                        className="text-terminal-red hover:text-red-400 text-xs"
                      >✕</button>
                    </td>
                  </tr>
                ))}
                {holdings.length === 0 && (
                  <tr><td colSpan={8} className="text-terminal-muted text-center py-4">尚無持股</td></tr>
                )}
              </tbody>
            </table>
          </Card>

          {/* Add Form */}
          <Card title="新增持股" className="mt-4">
            <form onSubmit={handleAdd} className="flex gap-2 items-end">
              {[
                { key: "stock_code", label: "代碼", placeholder: "2330" },
                { key: "shares", label: "股數", placeholder: "1000" },
                { key: "cost_price", label: "成本", placeholder: "800.0" },
              ].map(({ key, label, placeholder }) => (
                <div key={key} className="flex-1">
                  <label className="text-terminal-muted text-xs">{label}</label>
                  <input
                    value={form[key]}
                    onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                    placeholder={placeholder}
                    required
                    className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
                  />
                </div>
              ))}
              <button
                type="submit"
                disabled={loading}
                className="px-4 py-1.5 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-sm rounded hover:bg-terminal-accent/30"
              >
                {loading ? "..." : "新增"}
              </button>
            </form>
          </Card>
        </div>

        {/* Pie Chart */}
        <Card title="持倉分佈">
          {pieData.length > 0 ? (
            <ResponsiveContainer width="100%" height={240}>
              <PieChart>
                <Pie data={pieData} cx="50%" cy="50%" innerRadius={60} outerRadius={90} dataKey="value">
                  {pieData.map((_, i) => (
                    <Cell key={i} fill={COLORS[i % COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip
                  contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
                  formatter={(v) => v.toLocaleString()}
                />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <div className="text-terminal-muted text-sm text-center py-16">尚無資料</div>
          )}
          <div className="mt-2 space-y-1">
            {pieData.map((d, i) => (
              <div key={d.name} className="flex items-center gap-2 text-xs">
                <span style={{ color: COLORS[i % COLORS.length] }}>●</span>
                <span className="text-terminal-text">{d.name}</span>
                <span className="ml-auto text-terminal-muted">
                  {totalMV ? ((d.value / totalMV) * 100).toFixed(1) : 0}%
                </span>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
