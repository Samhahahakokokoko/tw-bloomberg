import React, { useState } from "react";
import { getQuote, getKline, getInstitutional } from "../utils/api";
import Card from "../components/Card";
import PriceTag from "../components/PriceTag";
import { ComposedChart, Bar, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";

export default function Quote() {
  const [input, setInput] = useState("2330");
  const [quote, setQuote] = useState(null);
  const [kline, setKline] = useState([]);
  const [inst, setInst] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const search = async () => {
    setLoading(true);
    setError("");
    try {
      const [q, k, i] = await Promise.allSettled([
        getQuote(input),
        getKline(input),
        getInstitutional(input),
      ]);
      if (q.status === "fulfilled") setQuote(q.value);
      else setError(`查無資料: ${input}`);
      if (k.status === "fulfilled") setKline(k.value.slice(-60));
      if (i.status === "fulfilled") setInst(i.value);
    } finally {
      setLoading(false);
    }
  };

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
          <Card title="三大法人">
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
                      {val >= 0 ? "+" : ""}{val?.toLocaleString()}
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

      {/* K-Line Chart */}
      {kline.length > 0 && (
        <Card title="K線圖 (近60日)">
          <ResponsiveContainer width="100%" height={280}>
            <ComposedChart data={kline}>
              <XAxis
                dataKey="date"
                tick={{ fill: "#4a6080", fontSize: 10 }}
                tickLine={false}
                axisLine={{ stroke: "#1e3a5f" }}
                tickFormatter={(v) => v.slice(-5)}
                interval={9}
              />
              <YAxis
                tick={{ fill: "#4a6080", fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                domain={["auto", "auto"]}
                yAxisId="price"
              />
              <YAxis yAxisId="vol" orientation="right" hide />
              <Tooltip
                contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
                labelStyle={{ color: "#c8d8e8" }}
              />
              <Bar dataKey="volume" yAxisId="vol" fill="#1e3a5f" opacity={0.6} />
              <Line
                type="monotone"
                dataKey="close"
                yAxisId="price"
                stroke="#00d4ff"
                dot={false}
                strokeWidth={1.5}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </Card>
      )}
    </div>
  );
}
