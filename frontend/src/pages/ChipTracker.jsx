import React, { useState } from "react";
import { getChipHistory, getMainForceCost, getInstitutional } from "../utils/api";
import Card from "../components/Card";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, Cell,
} from "recharts";

export default function ChipTracker() {
  const [input, setInput]       = useState("2330");
  const [chips, setChips]       = useState([]);
  const [cost, setCost]         = useState(null);
  const [today, setToday]       = useState(null);
  const [loading, setLoading]   = useState(false);
  const [error, setError]       = useState("");
  const [days, setDays]         = useState(20);

  const search = async () => {
    const code = input.trim();
    if (!code) return;
    setLoading(true);
    setError("");
    setChips([]);
    setCost(null);
    setToday(null);
    try {
      const [h, c, t] = await Promise.allSettled([
        getChipHistory(code, days),
        getMainForceCost(code),
        getInstitutional(code),
      ]);
      if (h.status === "fulfilled") setChips(h.value);
      if (c.status === "fulfilled") setCost(c.value);
      if (t.status === "fulfilled") setToday(t.value);
      if (h.status === "rejected" && c.status === "rejected") {
        setError(`查無籌碼資料: ${code}`);
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  // 計算近 N 日連續買超/賣超天數
  const getConsecDays = (data, field) => {
    let count = 0;
    for (let i = data.length - 1; i >= 0; i--) {
      if (data[i][field] > 0) count++;
      else break;
    }
    return count;
  };

  const totalForeign = chips.reduce((s, c) => s + (c.foreign_net || 0), 0);
  const totalTrust   = chips.reduce((s, c) => s + (c.trust_net || 0), 0);
  const totalNet     = chips.reduce((s, c) => s + (c.total_net || 0), 0);

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest">◈ 籌碼追蹤</h1>
      </div>

      {/* Search */}
      <div className="flex gap-2">
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === "Enter" && search()}
          placeholder="輸入股票代碼"
          className="flex-1 bg-terminal-surface border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
        />
        <select
          value={days}
          onChange={e => setDays(Number(e.target.value))}
          className="bg-terminal-surface border border-terminal-border rounded px-2 py-2 text-sm text-terminal-text focus:outline-none"
        >
          <option value={10}>10 日</option>
          <option value={20}>20 日</option>
          <option value={40}>40 日</option>
          <option value={60}>60 日</option>
        </select>
        <button
          onClick={search}
          disabled={loading}
          className="px-4 py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-sm rounded hover:bg-terminal-accent/30"
        >
          {loading ? "載入..." : "查詢 ▶"}
        </button>
      </div>

      {error && <div className="text-terminal-red text-sm">{error}</div>}

      {/* 今日三大法人 */}
      {today && (
        <div className="grid grid-cols-4 gap-3">
          {[
            { label: "外資今日", value: today.foreign_net, key: "foreign" },
            { label: "投信今日", value: today.investment_trust_net, key: "trust" },
            { label: "自營今日", value: today.dealer_net, key: "dealer" },
            { label: "合計今日", value: today.total_net, key: "total" },
          ].map(({ label, value }) => (
            <Card key={label} title={label}>
              <div className={`text-lg font-bold ${value >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                {value >= 0 ? "+" : ""}{value?.toLocaleString()}
              </div>
              <div className="text-terminal-muted text-xs mt-1">張</div>
            </Card>
          ))}
        </div>
      )}

      {/* 主力成本估算 */}
      {cost && cost.estimated_cost !== null && (
        <Card title="◈ 主力成本估算">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <div className="text-terminal-muted text-xs">估算成本</div>
              <div className="text-terminal-accent text-xl font-bold">{cost.estimated_cost}</div>
              <div className="text-terminal-muted text-xs">({cost.cost_range_low} ~ {cost.cost_range_high})</div>
            </div>
            <div>
              <div className="text-terminal-muted text-xs">現價</div>
              <div className="text-terminal-text text-xl font-bold">{cost.current_price}</div>
            </div>
            <div>
              <div className="text-terminal-muted text-xs">估算損益</div>
              <div className={`text-xl font-bold ${cost.profit_loss_pct >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                {cost.profit_loss_pct >= 0 ? "+" : ""}{cost.profit_loss_pct}%
              </div>
              <div className="text-terminal-muted text-xs">{cost.status}</div>
            </div>
            <div>
              <div className="text-terminal-muted text-xs">連續買超</div>
              <div className="text-terminal-yellow text-xl font-bold">{cost.consecutive_buy_days} 日</div>
              <div className="text-terminal-muted text-xs">共分析 {cost.analysis_days} 日</div>
            </div>
          </div>
        </Card>
      )}

      {cost && cost.estimated_cost === null && (
        <Card title="主力成本估算">
          <div className="text-terminal-muted text-sm">{cost.message || "近期無法人淨買超，無法估算"}</div>
        </Card>
      )}

      {/* 近期統計 */}
      {chips.length > 0 && (
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: `外資近${chips.length}日合計`, value: totalForeign },
            { label: `投信近${chips.length}日合計`, value: totalTrust },
            { label: `三大法人合計`, value: totalNet },
          ].map(({ label, value }) => (
            <Card key={label} title={label}>
              <div className={`text-xl font-bold ${value >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                {value >= 0 ? "+" : ""}{value?.toLocaleString()}
              </div>
              <div className="text-terminal-muted text-xs mt-1">張</div>
            </Card>
          ))}
        </div>
      )}

      {/* 籌碼走勢圖 */}
      {chips.length > 0 && (
        <Card title={`三大法人買賣超 (近${chips.length}日)`}>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={chips} margin={{ top: 5, right: 5, left: 0, bottom: 5 }}>
              <XAxis
                dataKey="date"
                tick={{ fill: "#4a6080", fontSize: 9 }}
                tickLine={false}
                axisLine={{ stroke: "#1e3a5f" }}
                tickFormatter={v => v.slice(-5)}
                interval={Math.floor(chips.length / 5)}
              />
              <YAxis tick={{ fill: "#4a6080", fontSize: 9 }} tickLine={false} axisLine={false} />
              <Tooltip
                contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
                formatter={(v, name) => [`${v >= 0 ? "+" : ""}${v?.toLocaleString()} 張`, name]}
              />
              <ReferenceLine y={0} stroke="#1e3a5f" />
              <Bar dataKey="foreign_net" name="外資" radius={[2,2,0,0]}>
                {chips.map((c, i) => (
                  <Cell key={i} fill={c.foreign_net >= 0 ? "#00ff88" : "#ff4466"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>

          {/* 投信 */}
          <div className="mt-2 text-xs text-terminal-muted mb-1">投信買賣超</div>
          <ResponsiveContainer width="100%" height={120}>
            <BarChart data={chips} margin={{ top: 0, right: 5, left: 0, bottom: 5 }}>
              <XAxis dataKey="date" hide />
              <YAxis tick={{ fill: "#4a6080", fontSize: 9 }} tickLine={false} axisLine={false} />
              <Tooltip
                contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
                formatter={(v) => [`${v >= 0 ? "+" : ""}${v?.toLocaleString()} 張`, "投信"]}
              />
              <ReferenceLine y={0} stroke="#1e3a5f" />
              <Bar dataKey="trust_net" name="投信" radius={[2,2,0,0]}>
                {chips.map((c, i) => (
                  <Cell key={i} fill={c.trust_net >= 0 ? "#00d4ff" : "#ff8844"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </Card>
      )}

      {/* 明細表 */}
      {chips.length > 0 && (
        <Card title="籌碼明細">
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-terminal-muted border-b border-terminal-border">
                  {["日期","外資買","外資賣","外資淨","投信淨","自營淨","合計"].map(h => (
                    <th key={h} className="text-right py-1.5 pr-3 first:text-left">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {[...chips].reverse().map((c, i) => (
                  <tr key={i} className="border-b border-terminal-border/30 hover:bg-terminal-border/20">
                    <td className="py-1.5 pr-3 text-terminal-muted">{c.date}</td>
                    <td className="py-1.5 pr-3 text-right text-terminal-green">{c.foreign_buy?.toLocaleString()}</td>
                    <td className="py-1.5 pr-3 text-right text-terminal-red">{c.foreign_sell?.toLocaleString()}</td>
                    <td className={`py-1.5 pr-3 text-right font-bold ${c.foreign_net >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                      {c.foreign_net >= 0 ? "+" : ""}{c.foreign_net?.toLocaleString()}
                    </td>
                    <td className={`py-1.5 pr-3 text-right ${c.trust_net >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                      {c.trust_net >= 0 ? "+" : ""}{c.trust_net?.toLocaleString()}
                    </td>
                    <td className={`py-1.5 pr-3 text-right ${c.dealer_net >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                      {c.dealer_net >= 0 ? "+" : ""}{c.dealer_net?.toLocaleString()}
                    </td>
                    <td className={`py-1.5 pr-3 text-right font-bold ${c.total_net >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                      {c.total_net >= 0 ? "+" : ""}{c.total_net?.toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}
