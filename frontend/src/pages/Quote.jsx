import React, { useState } from "react";
import { getQuote, getKline, getInstitutional, getChipHistory } from "../utils/api";
import Card from "../components/Card";
import PriceTag from "../components/PriceTag";
import {
  ComposedChart, Bar, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  ReferenceLine, CartesianGrid, Legend,
} from "recharts";

export default function Quote() {
  const [input, setInput]     = useState("2330");
  const [quote, setQuote]     = useState(null);
  const [kline, setKline]     = useState([]);
  const [inst, setInst]       = useState(null);
  const [chips, setChips]     = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState("");
  const [showInst, setShowInst] = useState(true);

  const search = async () => {
    const code = input.trim();
    if (!code) return;
    setLoading(true);
    setError("");
    try {
      const [q, k, i, c] = await Promise.allSettled([
        getQuote(code),
        getKline(code),
        getInstitutional(code),
        getChipHistory(code, 40),
      ]);
      if (q.status === "fulfilled") setQuote(q.value);
      else setError(`查無資料: ${code}`);
      if (k.status === "fulfilled") setKline(k.value.slice(-60));
      if (i.status === "fulfilled") setInst(i.value);
      if (c.status === "fulfilled") setChips(c.value);
    } finally {
      setLoading(false);
    }
  };

  // 合併 K線資料 + 法人買賣超（依日期 join）
  const chipMap = Object.fromEntries(chips.map(c => [c.date, c]));
  const chartData = kline.map(k => ({
    ...k,
    foreign_net: chipMap[k.date]?.foreign_net ?? null,
    trust_net:   chipMap[k.date]?.trust_net   ?? null,
    total_net:   chipMap[k.date]?.total_net   ?? null,
  }));

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest">◈ STOCK QUOTE</h1>
      </div>

      {/* Search */}
      <div className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()}
          placeholder="輸入股票代碼 (e.g. 2330)"
          className="flex-1 bg-terminal-surface border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
        />
        <button
          onClick={search}
          disabled={loading}
          className="px-4 py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-sm rounded hover:bg-terminal-accent/30 transition-colors"
        >
          {loading ? "載入..." : "查詢 ▶"}
        </button>
      </div>

      {error && <div className="text-terminal-red text-sm">{error}</div>}

      {/* Quote Info */}
      {quote && (
        <div className="grid grid-cols-2 gap-3">
          <Card title={`${quote.name} (${quote.code})`}>
            <div className="text-4xl font-bold text-terminal-text mb-2">{quote.price}</div>
            <PriceTag value={quote.change || 0} pct={quote.change_pct} />
            <div className="grid grid-cols-2 gap-x-4 mt-4 text-xs text-terminal-muted">
              {[
                ["開盤", quote.open],
                ["最高", quote.high],
                ["最低", quote.low],
                ["成交量", quote.volume?.toLocaleString()],
              ].map(([label, val]) => (
                <div key={label} className="flex justify-between py-1 border-b border-terminal-border/30">
                  <span>{label}</span>
                  <span className="text-terminal-text">{val}</span>
                </div>
              ))}
            </div>
          </Card>

          {/* Institutional */}
          <Card title="三大法人（今日）">
            {inst ? (
              <div className="space-y-2 text-sm">
                {[
                  ["外資", inst.foreign_net],
                  ["投信", inst.investment_trust_net],
                  ["自營商", inst.dealer_net],
                  ["合計", inst.total_net],
                ].map(([label, val]) => (
                  <div key={label} className="flex justify-between border-b border-terminal-border/30 pb-1">
                    <span className="text-terminal-muted">{label}</span>
                    <span className={val >= 0 ? "text-terminal-green" : "text-terminal-red"}>
                      {val >= 0 ? "+" : ""}{val?.toLocaleString()} 張
                    </span>
                  </div>
                ))}
                <div className="text-terminal-muted text-xs mt-2">{inst.date}</div>
              </div>
            ) : (
              <div className="text-terminal-muted text-sm">搜尋後顯示</div>
            )}
          </Card>
        </div>
      )}

      {/* K-Line + 法人疊加圖 */}
      {chartData.length > 0 && (
        <Card title={
          <div className="flex items-center justify-between w-full">
            <span>K線圖 + 法人買賣超 (近60日)</span>
            <button
              onClick={() => setShowInst(!showInst)}
              className={`text-xs px-2 py-0.5 rounded border transition-all ${
                showInst
                  ? "border-terminal-green text-terminal-green"
                  : "border-terminal-border text-terminal-muted"
              }`}
            >
              {showInst ? "✓ 法人疊加" : "法人疊加"}
            </button>
          </div>
        }>
          {/* 股價 + 成交量 */}
          <ResponsiveContainer width="100%" height={260}>
            <ComposedChart data={chartData} margin={{ top: 5, right: 5, left: 0, bottom: 0 }}>
              <CartesianGrid stroke="#1e3a5f" strokeDasharray="3 3" />
              <XAxis
                dataKey="date"
                tick={{ fill: "#4a6080", fontSize: 9 }}
                tickLine={false}
                axisLine={{ stroke: "#1e3a5f" }}
                tickFormatter={(v) => v.slice(-5)}
                interval={9}
              />
              <YAxis
                yAxisId="price"
                tick={{ fill: "#4a6080", fontSize: 9 }}
                tickLine={false}
                axisLine={false}
                domain={["auto", "auto"]}
              />
              <YAxis yAxisId="vol" orientation="right" hide />
              <Tooltip
                contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
                labelStyle={{ color: "#c8d8e8" }}
                formatter={(v, name) => {
                  if (name === "volume") return [`${v?.toLocaleString()} 股`, "成交量"];
                  return [v, name];
                }}
              />
              <Bar dataKey="volume" yAxisId="vol" fill="#1e3a5f" opacity={0.5} name="volume" />
              <Line
                type="monotone" dataKey="close" yAxisId="price"
                stroke="#00d4ff" dot={false} strokeWidth={1.5} name="收盤價"
              />
            </ComposedChart>
          </ResponsiveContainer>

          {/* 法人買賣超（疊加在下方獨立圖） */}
          {showInst && chips.length > 0 && (
            <>
              <div className="text-terminal-muted text-xs mt-2 mb-1">法人買賣超（張）</div>
              <ResponsiveContainer width="100%" height={120}>
                <ComposedChart data={chartData.filter(d => d.foreign_net !== null)}>
                  <XAxis dataKey="date" hide />
                  <YAxis tick={{ fill: "#4a6080", fontSize: 9 }} tickLine={false} axisLine={false} />
                  <Tooltip
                    contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
                    formatter={(v, name) => [`${v >= 0 ? "+" : ""}${v?.toLocaleString()} 張`, name]}
                  />
                  <ReferenceLine y={0} stroke="#1e3a5f" />
                  <Bar dataKey="total_net" name="三大法人合計" radius={[1,1,0,0]}>
                    {chartData.filter(d => d.foreign_net !== null).map((entry, i) => (
                      <rect key={i} fill={entry.total_net >= 0 ? "#00ff88" : "#ff4466"} />
                    ))}
                  </Bar>
                  <Line type="monotone" dataKey="foreign_net" name="外資"
                    stroke="#00d4ff" dot={false} strokeWidth={1.5} />
                  <Line type="monotone" dataKey="trust_net" name="投信"
                    stroke="#ffcc00" dot={false} strokeWidth={1} strokeDasharray="4 2" />
                  <Legend
                    wrapperStyle={{ fontSize: 10, color: "#4a6080" }}
                  />
                </ComposedChart>
              </ResponsiveContainer>
            </>
          )}
        </Card>
      )}
    </div>
  );
}
