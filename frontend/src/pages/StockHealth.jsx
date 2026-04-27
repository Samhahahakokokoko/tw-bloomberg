import React, { useState } from "react";
import { getStockHealth } from "../utils/api";
import Card from "../components/Card";
import { Activity } from "lucide-react";

const GRADE_COLOR = { A: "text-terminal-green", B: "text-terminal-green", C: "text-terminal-yellow", D: "text-terminal-red", F: "text-terminal-red" };
const GRADE_BG    = { A: "bg-terminal-green/20 border-terminal-green/50", B: "bg-terminal-green/10 border-terminal-green/30", C: "bg-terminal-yellow/10 border-terminal-yellow/30", D: "bg-terminal-red/10 border-terminal-red/30", F: "bg-terminal-red/20 border-terminal-red/50" };

function ScoreBar({ score, label, color = "#00d4ff" }) {
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs">
        <span className="text-terminal-muted">{label}</span>
        <span className="text-terminal-text font-bold">{score}</span>
      </div>
      <div className="h-2 bg-terminal-border rounded-full overflow-hidden">
        <div
          className="h-full rounded-full transition-all duration-700"
          style={{ width: `${score}%`, backgroundColor: color }}
        />
      </div>
    </div>
  );
}

export default function StockHealth() {
  const [input, setInput]   = useState("2330");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError]   = useState("");

  const search = async () => {
    const code = input.trim();
    if (!code) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const data = await getStockHealth(code);
      setResult(data);
    } catch (e) {
      setError("查詢失敗：" + (e.response?.data?.detail || e.message));
    } finally {
      setLoading(false);
    }
  };

  const tech = result?.details?.technical || {};
  const chip = result?.details?.chip || {};
  const val  = result?.details?.valuation || {};

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest flex items-center gap-2">
          <Activity size={16} /> 股票健診
        </h1>
      </div>

      {/* Search */}
      <div className="flex gap-2">
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => e.key === "Enter" && search()}
          placeholder="輸入股票代碼 (e.g. 2330)"
          className="flex-1 bg-terminal-surface border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
        />
        <button
          onClick={search}
          disabled={loading}
          className="px-4 py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-sm rounded hover:bg-terminal-accent/30"
        >
          {loading ? "分析中..." : "健診 ▶"}
        </button>
      </div>

      {error && <div className="text-terminal-red text-sm">{error}</div>}

      {result && (
        <div className="space-y-4">
          {/* 總評 */}
          <div className={`p-4 rounded-lg border ${GRADE_BG[result.grade] || "bg-terminal-surface border-terminal-border"}`}>
            <div className="flex items-center justify-between">
              <div>
                <div className="text-terminal-muted text-xs tracking-widest">綜合評分</div>
                <div className={`text-5xl font-bold mt-1 ${GRADE_COLOR[result.grade]}`}>
                  {result.grade}
                </div>
                <div className="text-terminal-muted text-sm mt-1">{result.grade_label}</div>
              </div>
              <div className="text-right">
                <div className="text-terminal-muted text-xs">股票代碼</div>
                <div className="text-terminal-accent text-2xl font-bold">{result.stock_code}</div>
                <div className="text-terminal-text text-3xl font-bold mt-1">{result.overall_score}</div>
                <div className="text-terminal-muted text-xs">/ 100 分</div>
              </div>
            </div>
          </div>

          {/* 分項評分 */}
          <Card title="分項評分">
            <div className="space-y-4">
              <ScoreBar score={result.scores.technical} label="技術面（40%）" color="#00d4ff" />
              <ScoreBar score={result.scores.chip}      label="籌碼面（30%）" color="#00ff88" />
              <ScoreBar score={result.scores.valuation} label="估值面（30%）" color="#ffcc00" />
            </div>
          </Card>

          {/* 技術指標 */}
          {Object.keys(tech).length > 0 && !tech.error && (
            <Card title="技術指標">
              <div className="grid grid-cols-2 gap-x-8 gap-y-2 text-xs">
                {[
                  ["現價", tech.current],
                  ["MA5", tech.ma5],
                  ["MA10", tech.ma10],
                  ["MA20", tech.ma20],
                  ["RSI(14)", tech.rsi],
                  ["5日漲跌", tech.chg5d !== undefined ? `${tech.chg5d >= 0 ? "+" : ""}${tech.chg5d}%` : "—"],
                  ["20日漲跌", tech.chg20d !== undefined ? `${tech.chg20d >= 0 ? "+" : ""}${tech.chg20d}%` : "—"],
                ].map(([label, val]) => (
                  <div key={label} className="flex justify-between border-b border-terminal-border/30 py-1">
                    <span className="text-terminal-muted">{label}</span>
                    <span className="text-terminal-text font-mono">{val ?? "—"}</span>
                  </div>
                ))}
                <div className="flex gap-4 col-span-2 pt-1">
                  {[
                    { label: "站上MA5", ok: tech.above_ma5 },
                    { label: "站上MA10", ok: tech.above_ma10 },
                    { label: "站上MA20", ok: tech.above_ma20 },
                  ].map(({ label, ok }) => (
                    <div key={label} className={`flex items-center gap-1 ${ok ? "text-terminal-green" : "text-terminal-red"}`}>
                      <span>{ok ? "✓" : "✗"}</span>
                      <span>{label}</span>
                    </div>
                  ))}
                </div>
              </div>
            </Card>
          )}

          {/* 籌碼面 */}
          {Object.keys(chip).length > 0 && (
            <Card title="籌碼面（今日）">
              <div className="grid grid-cols-2 gap-x-8 gap-y-2 text-xs">
                {[
                  ["外資", chip.foreign_net],
                  ["投信", chip.trust_net],
                  ["自營", chip.dealer_net],
                  ["三大合計", chip.total_net],
                ].map(([label, val]) => (
                  <div key={label} className="flex justify-between border-b border-terminal-border/30 py-1">
                    <span className="text-terminal-muted">{label}</span>
                    <span className={`font-mono font-bold ${val >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                      {val >= 0 ? "+" : ""}{val?.toLocaleString()} 張
                    </span>
                  </div>
                ))}
              </div>
              {chip.date && <div className="text-terminal-muted text-xs mt-2">資料日期: {chip.date}</div>}
            </Card>
          )}

          {/* 估值面 */}
          {Object.keys(val).length > 0 && (
            <Card title="估值面">
              <div className="grid grid-cols-3 gap-3 text-center">
                {[
                  { label: "本益比", value: val.pe_ratio, suffix: "x", good: v => v > 0 && v <= 20 },
                  { label: "股價淨值比", value: val.pb_ratio, suffix: "x", good: v => v > 0 && v <= 2 },
                  { label: "殖利率", value: val.dividend_yield, suffix: "%", good: v => v >= 3 },
                ].map(({ label, value, suffix, good }) => (
                  <div key={label} className="bg-terminal-surface/50 rounded p-3">
                    <div className="text-terminal-muted text-xs">{label}</div>
                    <div className={`text-xl font-bold mt-1 ${value && good(value) ? "text-terminal-green" : "text-terminal-text"}`}>
                      {value ? `${value}${suffix}` : "—"}
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          )}

          {/* AI 建議 */}
          {result.suggestions && result.suggestions.length > 0 && (
            <Card title="◈ 健診建議">
              <ul className="space-y-1.5">
                {result.suggestions.map((tip, i) => (
                  <li key={i} className="flex items-start gap-2 text-sm">
                    <span className="text-terminal-accent mt-0.5">›</span>
                    <span className="text-terminal-text">{tip}</span>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}
