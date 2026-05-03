import React, { useEffect, useState, useCallback } from "react";
import { RefreshCw, Activity, TrendingUp, TrendingDown, Minus } from "lucide-react";

const C = {
  bg:      "#0a0f1e",
  surface: "#0f1629",
  border:  "#1e3a5f",
  accent:  "#00d4ff",
  green:   "#00e676",
  red:     "#ff5252",
  yellow:  "#ffd740",
  muted:   "#7090b0",
  white:   "#e0f0ff",
};

const STATUS_CONFIG = {
  ACTIVE: { color: C.green,  bg: "#00331a", label: "ACTIVE" },
  WEAK:   { color: C.yellow, bg: "#332200", label: "WEAK"   },
  DEAD:   { color: C.red,    bg: "#330011", label: "DEAD"   },
  UNKNOWN:{ color: C.muted,  bg: "#1a2030", label: "N/A"    },
};

// 模擬 Alpha 資料（實際從 /api/quant/alpha_status 取）
const MOCK_ALPHAS = [
  { name: "momentum_20d",   status: "ACTIVE", ic_30d: 0.062, ic_7d: 0.071, weight: 0.28, trend: "up" },
  { name: "chip_flow",      status: "ACTIVE", ic_30d: 0.055, ic_7d: 0.061, weight: 0.25, trend: "up" },
  { name: "breakout_vol",   status: "ACTIVE", ic_30d: 0.048, ic_7d: 0.052, weight: 0.20, trend: "stable" },
  { name: "eps_momentum",   status: "WEAK",   ic_30d: 0.021, ic_7d: 0.015, weight: 0.12, trend: "down" },
  { name: "sector_rotation",status: "ACTIVE", ic_30d: 0.038, ic_7d: 0.044, weight: 0.15, trend: "stable" },
  { name: "foreign_net",    status: "WEAK",   ic_30d: 0.018, ic_7d: 0.009, weight: 0.08, trend: "down" },
  { name: "value_mean_rev", status: "DEAD",   ic_30d: -0.005,ic_7d:-0.012, weight: 0.00, trend: "dead" },
  { name: "earnings_surp",  status: "ACTIVE", ic_30d: 0.033, ic_7d: 0.038, weight: 0.10, trend: "up" },
];

function TrendIcon({ trend }) {
  if (trend === "up")     return <TrendingUp size={12} color={C.green} />;
  if (trend === "down")   return <TrendingDown size={12} color={C.red} />;
  if (trend === "dead")   return <Minus size={12} color={C.muted} />;
  return <Minus size={12} color={C.muted} />;
}

function StatusBadge({ status }) {
  const cfg = STATUS_CONFIG[status] || STATUS_CONFIG.UNKNOWN;
  return (
    <span
      className="text-[10px] font-bold px-1.5 py-0.5 rounded"
      style={{ color: cfg.color, background: cfg.bg }}
    >
      {cfg.label}
    </span>
  );
}

function IcBar({ value }) {
  const pct   = Math.min(Math.abs(value) / 0.1 * 100, 100);
  const color = value >= 0.04 ? C.green : value >= 0.02 ? C.yellow : C.red;
  return (
    <div className="flex items-center gap-1">
      <div className="w-16 h-2 bg-[#0a0f1e] rounded overflow-hidden">
        <div className="h-full rounded" style={{ width: `${pct}%`, background: color }} />
      </div>
      <span className="text-[10px] font-mono" style={{ color }}>
        {value >= 0 ? "+" : ""}{value.toFixed(3)}
      </span>
    </div>
  );
}

export default function AlphaMonitor() {
  const [alphas,    setAlphas]    = useState(MOCK_ALPHAS);
  const [loading,   setLoading]   = useState(false);
  const [filter,    setFilter]    = useState("ALL");
  const [editMode,  setEditMode]  = useState(false);
  const [weights,   setWeights]   = useState({});
  const [time,      setTime]      = useState(new Date());

  useEffect(() => {
    const tick = setInterval(() => setTime(new Date()), 1000);
    // 嘗試從 API 取真實資料
    fetch("/api/quant/alpha_status")
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (data?.alphas) setAlphas(data.alphas); })
      .catch(() => {});
    return () => clearInterval(tick);
  }, []);

  const filtered = filter === "ALL" ? alphas : alphas.filter(a => a.status === filter);
  const active   = alphas.filter(a => a.status === "ACTIVE").length;
  const weak     = alphas.filter(a => a.status === "WEAK").length;
  const dead     = alphas.filter(a => a.status === "DEAD").length;
  const totalW   = alphas.reduce((s, a) => s + (a.weight || 0), 0);

  const handleWeightChange = (name, val) => {
    setWeights(prev => ({ ...prev, [name]: parseFloat(val) || 0 }));
  };

  const saveWeights = async () => {
    try {
      await fetch("/api/quant/alpha_weights", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(weights),
      });
      setEditMode(false);
    } catch (e) {
      console.error(e);
    }
  };

  return (
    <div
      className="p-4 space-y-4 overflow-auto"
      style={{ background: C.bg, minHeight: "100vh", fontFamily: "monospace" }}
    >
      {/* 標題列 */}
      <div className="flex items-center justify-between border-b border-[#1e3a5f] pb-3">
        <div className="flex items-center gap-2">
          <Activity size={16} className="text-[#00d4ff]" />
          <span className="text-[#00d4ff] font-bold tracking-widest text-sm">◈ ALPHA MONITOR</span>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-[#7090b0]">
          <span>{time.toLocaleString("zh-TW")}</span>
          <button onClick={() => setEditMode(!editMode)}
            className={`px-2 py-0.5 rounded border text-[10px] ${editMode ? "border-[#00d4ff] text-[#00d4ff]" : "border-[#1e3a5f] text-[#7090b0]"}`}>
            {editMode ? "✎ 編輯中" : "✎ 調整權重"}
          </button>
          {editMode && (
            <button onClick={saveWeights}
              className="px-2 py-0.5 rounded bg-[#00d4ff] text-black text-[10px] font-bold">
              儲存
            </button>
          )}
        </div>
      </div>

      {/* 統計卡片 */}
      <div className="grid grid-cols-4 gap-3">
        {[
          { label: "ACTIVE", value: active, color: C.green },
          { label: "WEAK",   value: weak,   color: C.yellow },
          { label: "DEAD",   value: dead,   color: C.red },
          { label: "總權重", value: `${(totalW * 100).toFixed(0)}%`, color: C.accent },
        ].map(({ label, value, color }) => (
          <div key={label} className="bg-[#0f1629] border border-[#1e3a5f] rounded-lg p-3 text-center">
            <div className="text-[10px] text-[#7090b0] tracking-wider">{label}</div>
            <div className="text-xl font-bold mt-1" style={{ color }}>{value}</div>
          </div>
        ))}
      </div>

      {/* 篩選器 */}
      <div className="flex gap-2">
        {["ALL", "ACTIVE", "WEAK", "DEAD"].map(f => (
          <button key={f} onClick={() => setFilter(f)}
            className={`text-[11px] px-3 py-1 rounded border transition-colors ${
              filter === f
                ? "border-[#00d4ff] text-[#00d4ff] bg-[#001a2a]"
                : "border-[#1e3a5f] text-[#7090b0] hover:border-[#7090b0]"
            }`}>
            {f}
          </button>
        ))}
      </div>

      {/* Alpha 表格 */}
      <div className="bg-[#0f1629] border border-[#1e3a5f] rounded-lg overflow-hidden">
        <table className="w-full text-[11px]">
          <thead>
            <tr className="border-b border-[#1e3a5f] text-[#7090b0]">
              <th className="text-left p-3">Alpha 名稱</th>
              <th className="p-3">狀態</th>
              <th className="p-3">近30日 IC</th>
              <th className="p-3">近7日 IC</th>
              <th className="p-3">趨勢</th>
              <th className="p-3">權重</th>
              <th className="p-3">操作</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map(a => {
              const cfg = STATUS_CONFIG[a.status] || STATUS_CONFIG.UNKNOWN;
              return (
                <tr key={a.name}
                  className="border-b border-[#0a0f1e] hover:bg-[#0a1020] transition-colors"
                  style={{ opacity: a.status === "DEAD" ? 0.5 : 1 }}
                >
                  <td className="p-3 font-mono text-[#e0f0ff]">{a.name}</td>
                  <td className="p-3 text-center"><StatusBadge status={a.status} /></td>
                  <td className="p-3"><IcBar value={a.ic_30d} /></td>
                  <td className="p-3"><IcBar value={a.ic_7d} /></td>
                  <td className="p-3 text-center"><TrendIcon trend={a.trend} /></td>
                  <td className="p-3 text-center">
                    {editMode ? (
                      <input
                        type="number" step="0.01" min="0" max="1"
                        defaultValue={a.weight}
                        onChange={e => handleWeightChange(a.name, e.target.value)}
                        className="w-16 bg-[#0a0f1e] border border-[#1e3a5f] rounded px-1 text-[#00d4ff] text-center text-[10px]"
                      />
                    ) : (
                      <span className="text-[#00d4ff]">
                        {a.status === "DEAD" ? "--" : `${(a.weight * 100).toFixed(0)}%`}
                      </span>
                    )}
                  </td>
                  <td className="p-3 text-center">
                    {a.status !== "DEAD" ? (
                      <button
                        className="text-[10px] border border-[#1e3a5f] text-[#7090b0] hover:text-[#ff5252] hover:border-[#ff5252] px-2 py-0.5 rounded transition-colors"
                        onClick={() => console.log("disable", a.name)}
                      >
                        停用
                      </button>
                    ) : (
                      <button
                        className="text-[10px] border border-[#1e3a5f] text-[#7090b0] hover:text-[#00e676] hover:border-[#00e676] px-2 py-0.5 rounded transition-colors"
                        onClick={() => console.log("enable", a.name)}
                      >
                        啟用
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* 說明 */}
      <div className="text-[10px] text-[#7090b0] space-y-0.5">
        <div>• IC（Information Coefficient）：預測相關性，越高越好，建議 &gt; 0.03</div>
        <div>• ACTIVE：IC 穩定，正常使用 ｜ WEAK：IC 下降，降低權重 ｜ DEAD：IC 轉負，停用</div>
        <div>• 調整權重後，下次排程執行時生效</div>
      </div>
    </div>
  );
}
