import React, { useState, useEffect } from "react";
import {
  runScreener, nlScreener, getScreenerTop, triggerPipeline, triggerScoring,
} from "../utils/api";
import Card from "../components/Card";
import PriceTag from "../components/PriceTag";
import {
  RadarChart, Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis,
  Tooltip, ResponsiveContainer, Legend,
} from "recharts";
import { Filter, Search, Zap, RefreshCw } from "lucide-react";

const PRESETS = [
  { key: "strong_fundamental",     label: "基本面強", color: "#00ff88" },
  { key: "institutional_favorite", label: "法人偏愛", color: "#00d4ff" },
  { key: "technical_breakout",     label: "技術突破", color: "#ffcc00" },
  { key: "golden_triangle",        label: "三維共振", color: "#ff8844" },
  { key: "high_conviction",        label: "高信心",   color: "#ff4466" },
];

const BOOL_FILTERS = [
  { key: "three_margins_up",  label: "三率齊升" },
  { key: "ma_aligned",        label: "均線多頭排列" },
  { key: "kd_golden_cross",   label: "KD黃金交叉" },
  { key: "vol_breakout",      label: "量能突破×1.5" },
  { key: "bb_breakout",       label: "布林上軌突破" },
  { key: "dual_signal",       label: "外資+投信雙強" },
];

function RadarCard({ stock }) {
  if (!stock) return null;
  const data = [
    { axis: "基本面", value: stock.fundamental_score },
    { axis: "籌碼面", value: stock.chip_score },
    { axis: "技術面", value: stock.technical_score },
    { axis: "信心",   value: stock.confidence },
  ];
  return (
    <div className="bg-terminal-surface/50 border border-terminal-border rounded-lg p-4">
      <div className="text-terminal-accent font-bold mb-1">
        {stock.stock_code} {stock.stock_name}
      </div>
      <div className="text-terminal-muted text-xs mb-3">
        總分: <span className="text-terminal-yellow font-bold text-base">{stock.total_score}</span>
        {" "}| 信心: {stock.confidence}
      </div>
      <ResponsiveContainer width="100%" height={180}>
        <RadarChart data={data}>
          <PolarGrid stroke="#1e3a5f" />
          <PolarAngleAxis dataKey="axis" tick={{ fill: "#4a6080", fontSize: 11 }} />
          <PolarRadiusAxis angle={30} domain={[0, 100]} tick={false} axisLine={false} />
          <Radar dataKey="value" stroke="#00d4ff" fill="#00d4ff" fillOpacity={0.3} />
          <Tooltip
            contentStyle={{ background: "#0f1629", border: "1px solid #1e3a5f", fontSize: 11 }}
          />
        </RadarChart>
      </ResponsiveContainer>
      <div className="grid grid-cols-2 gap-1 mt-2 text-xs">
        {[
          { label: "外資連買", val: `${stock.foreign_consec_buy}日` },
          { label: "投信連買", val: `${stock.trust_consec_buy}日` },
          { label: "EPS成長季", val: `${stock.eps_growth_qtrs}季` },
          { label: "毛利率",   val: stock.gross_margin ? `${stock.gross_margin}%` : "—" },
        ].map(({ label, val }) => (
          <div key={label} className="flex justify-between border-b border-terminal-border/30 py-0.5">
            <span className="text-terminal-muted">{label}</span>
            <span className="text-terminal-text">{val}</span>
          </div>
        ))}
      </div>
      {stock.ai_reason && (
        <div className="mt-2 text-xs text-terminal-muted leading-relaxed">
          💡 {stock.ai_reason.slice(0, 100)}
        </div>
      )}
    </div>
  );
}

function SignalBadge({ active, label }) {
  return (
    <span className={`px-1.5 py-0.5 rounded text-xs ${
      active
        ? "bg-terminal-green/20 text-terminal-green border border-terminal-green/30"
        : "bg-terminal-border/30 text-terminal-muted"
    }`}>
      {active ? "✓" : "✗"} {label}
    </span>
  );
}

export default function Screener() {
  const [results, setResults]     = useState([]);
  const [selected, setSelected]   = useState(null);
  const [loading, setLoading]     = useState(false);
  const [nlQuery, setNlQuery]     = useState("");
  const [nlLoading, setNlLoading] = useState(false);
  const [nlResult, setNlResult]   = useState(null);
  const [msg, setMsg]             = useState("");
  const [activePreset, setPreset] = useState("");

  // 自訂篩選
  const [numFilters, setNumFilters] = useState({
    revenue_yoy_min: "", gross_margin_min: "",
    foreign_consec_buy_min: "", trust_consec_buy_min: "",
    total_score_min: "60",
  });
  const [boolFilters, setBoolFilters] = useState({});

  const flash = (t) => { setMsg(t); setTimeout(() => setMsg(""), 5000); };

  useEffect(() => {
    handlePreset("golden_triangle");
  }, []);

  const handlePreset = async (key) => {
    setPreset(key);
    setLoading(true);
    setNlResult(null);
    try {
      const r = await runScreener({ preset: key, limit: 30 });
      setResults(r.results || []);
      setSelected((r.results || [])[0] || null);
    } catch (e) {
      flash("選股失敗：" + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleCustom = async () => {
    setLoading(true);
    setPreset("");
    setNlResult(null);
    const payload = { limit: 30 };
    Object.entries(numFilters).forEach(([k, v]) => {
      if (v !== "") payload[k] = parseFloat(v);
    });
    Object.entries(boolFilters).forEach(([k, v]) => {
      if (v) payload[k] = true;
    });
    try {
      const r = await runScreener(payload);
      setResults(r.results || []);
      setSelected((r.results || [])[0] || null);
    } catch (e) {
      flash("選股失敗：" + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleNL = async () => {
    if (!nlQuery.trim()) return;
    setNlLoading(true);
    setResults([]);
    setNlResult(null);
    try {
      const r = await nlScreener(nlQuery);
      setNlResult(r);
      setResults(r.results || []);
      setSelected((r.results || [])[0] || null);
    } catch (e) {
      flash("自然語言選股失敗：" + e.message);
    } finally {
      setNlLoading(false);
    }
  };

  const handleUpdateData = async (code) => {
    flash(`更新 ${code || "全量"} 資料中...`);
    try {
      await triggerPipeline(code || undefined);
      if (code) await triggerScoring();
      flash(`✓ ${code || "全量"} 資料已更新，評分約 30s 後完成`);
    } catch (e) {
      flash("更新失敗：" + e.message);
    }
  };

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest flex items-center gap-2">
          <Filter size={16} /> 多維度選股引擎
        </h1>
        <button
          onClick={() => handleUpdateData("")}
          className="px-3 py-1.5 border border-terminal-border text-terminal-muted text-xs rounded hover:text-terminal-text flex items-center gap-1"
        >
          <RefreshCw size={12} /> 更新數據
        </button>
      </div>

      {msg && (
        <div className="text-terminal-accent text-xs px-3 py-2 bg-terminal-accent/10 border border-terminal-accent/30 rounded">
          {msg}
        </div>
      )}

      {/* 自然語言查詢 */}
      <Card title="🤖 自然語言選股">
        <div className="flex gap-2">
          <input
            value={nlQuery}
            onChange={e => setNlQuery(e.target.value)}
            onKeyDown={e => e.key === "Enter" && handleNL()}
            placeholder="例：找外資連續買超3天且營收年增超過20%的股票"
            className="flex-1 bg-terminal-bg border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
          />
          <button
            onClick={handleNL}
            disabled={nlLoading}
            className="px-4 py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-sm rounded hover:bg-terminal-accent/30 flex items-center gap-1"
          >
            <Search size={14} /> {nlLoading ? "分析中..." : "查詢"}
          </button>
        </div>
        <div className="flex gap-2 mt-2 flex-wrap">
          {[
            "找法人雙買超且三率齊升的股票",
            "找均線多頭排列且量能突破的股票",
            "找外資連買5天且毛利率超過40%",
          ].map(q => (
            <button key={q} onClick={() => { setNlQuery(q); }}
              className="text-xs px-2 py-1 border border-terminal-border/50 text-terminal-muted rounded hover:text-terminal-text">
              {q.slice(0, 20)}...
            </button>
          ))}
        </div>
        {nlResult && (
          <div className="mt-3 p-3 bg-terminal-surface/50 rounded border border-terminal-border/30 text-xs">
            <span className="text-terminal-muted">解析條件：</span>
            <span className="text-terminal-accent ml-1">{nlResult.filter_description}</span>
            <span className="text-terminal-muted ml-3">找到 {nlResult.result_count} 檔</span>
            {nlResult.ai_summary && (
              <div className="mt-2 text-terminal-text leading-relaxed">{nlResult.ai_summary}</div>
            )}
          </div>
        )}
      </Card>

      {/* Preset 快選 */}
      <div className="grid grid-cols-5 gap-2">
        {PRESETS.map(p => (
          <button
            key={p.key}
            onClick={() => handlePreset(p.key)}
            className={`py-2 text-xs rounded border transition-all ${
              activePreset === p.key
                ? "border-opacity-100 text-white"
                : "border-terminal-border text-terminal-muted hover:border-opacity-60"
            }`}
            style={activePreset === p.key ? { borderColor: p.color, backgroundColor: p.color + "22", color: p.color } : {}}
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* 自訂篩選 */}
      <Card title="自訂篩選條件">
        <div className="grid grid-cols-3 gap-3 mb-3">
          {[
            { key: "revenue_yoy_min",        label: "營收 YoY 最低 %" },
            { key: "gross_margin_min",        label: "毛利率最低 %" },
            { key: "foreign_consec_buy_min",  label: "外資連買最少天" },
            { key: "trust_consec_buy_min",    label: "投信連買最少天" },
            { key: "total_score_min",         label: "總分最低" },
          ].map(({ key, label }) => (
            <div key={key}>
              <label className="text-terminal-muted text-xs">{label}</label>
              <input
                value={numFilters[key]}
                onChange={e => setNumFilters({ ...numFilters, [key]: e.target.value })}
                type="number" step="any"
                className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
              />
            </div>
          ))}
        </div>
        <div className="flex flex-wrap gap-2 mb-3">
          {BOOL_FILTERS.map(({ key, label }) => (
            <button
              key={key}
              onClick={() => setBoolFilters({ ...boolFilters, [key]: !boolFilters[key] })}
              className={`px-2 py-1 text-xs rounded border transition-all ${
                boolFilters[key]
                  ? "border-terminal-green text-terminal-green bg-terminal-green/10"
                  : "border-terminal-border text-terminal-muted"
              }`}
            >
              {boolFilters[key] ? "✓" : "+"} {label}
            </button>
          ))}
        </div>
        <button
          onClick={handleCustom}
          disabled={loading}
          className="px-4 py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-sm rounded hover:bg-terminal-accent/30 flex items-center gap-2"
        >
          <Zap size={14} /> {loading ? "篩選中..." : "執行篩選"}
        </button>
      </Card>

      {/* 結果 */}
      {results.length > 0 && (
        <div className="grid grid-cols-3 gap-4">
          {/* 左欄：結果列表 */}
          <div className="col-span-2">
            <Card title={`選股結果 (${results.length} 檔)`}>
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-terminal-muted border-b border-terminal-border">
                    {["代碼", "名稱", "總分", "基本", "籌碼", "技術", "信號"].map(h => (
                      <th key={h} className="text-left py-1.5 pr-2">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {results.map(r => (
                    <tr
                      key={r.stock_code}
                      onClick={() => setSelected(r)}
                      className={`border-b border-terminal-border/30 cursor-pointer hover:bg-terminal-border/30 ${
                        selected?.stock_code === r.stock_code ? "bg-terminal-border/40" : ""
                      }`}
                    >
                      <td className="py-2 pr-2 text-terminal-accent font-bold">{r.stock_code}</td>
                      <td className="py-2 pr-2">{r.stock_name}</td>
                      <td className="py-2 pr-2">
                        <span className="text-terminal-yellow font-bold">{r.total_score}</span>
                      </td>
                      <td className={`py-2 pr-2 ${r.fundamental_score >= 70 ? "text-terminal-green" : "text-terminal-text"}`}>
                        {r.fundamental_score}
                      </td>
                      <td className={`py-2 pr-2 ${r.chip_score >= 70 ? "text-terminal-green" : "text-terminal-text"}`}>
                        {r.chip_score}
                      </td>
                      <td className={`py-2 pr-2 ${r.technical_score >= 70 ? "text-terminal-green" : "text-terminal-text"}`}>
                        {r.technical_score}
                      </td>
                      <td className="py-2 pr-2">
                        <div className="flex gap-0.5 flex-wrap">
                          {r.ma_aligned && <span title="均線多頭" className="text-terminal-green text-xs">MA</span>}
                          {r.kd_golden_cross && <span title="KD交叉" className="text-terminal-yellow text-xs">KD</span>}
                          {r.vol_breakout && <span title="量能突破" className="text-terminal-accent text-xs">Vol</span>}
                          {r.three_margins_up && <span title="三率齊升" className="text-terminal-green text-xs">3率</span>}
                          {r.foreign_consec_buy >= 3 && <span title="外資連買3+" className="text-terminal-accent text-xs">外{r.foreign_consec_buy}d</span>}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          </div>

          {/* 右欄：雷達圖 */}
          <div className="space-y-3">
            {selected && <RadarCard stock={selected} />}
          </div>
        </div>
      )}

      {results.length === 0 && !loading && (
        <div className="text-terminal-muted text-sm text-center py-12">
          <div className="text-4xl mb-3">📊</div>
          <div>尚無評分資料</div>
          <div className="text-xs mt-2">評分資料每日 18:30 自動更新</div>
          <div className="text-xs text-terminal-accent mt-1">
            或點選右上「更新數據」手動觸發（約需 3-5 分鐘）
          </div>
        </div>
      )}
    </div>
  );
}
