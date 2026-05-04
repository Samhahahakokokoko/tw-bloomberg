import React, { useEffect, useState, useCallback } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell, RadarChart, PolarGrid, PolarAngleAxis, Radar } from "recharts";
import { Users, Star, TrendingUp, AlertTriangle, RefreshCw, Plus } from "lucide-react";

const C = {
  bg: "#0a0f1e", surface: "#0f1629", border: "#1e3a5f",
  accent: "#00d4ff", green: "#00e676", red: "#ff5252",
  yellow: "#ffd740", orange: "#ff9933", muted: "#7090b0", white: "#e0f0ff",
};

const TIER_STYLE = {
  S: { color: "#ffd740", bg: "#332200", label: "S 高可信" },
  A: { color: "#00e676", bg: "#003300", label: "A 穩定"  },
  B: { color: "#7090b0", bg: "#1a2030", label: "B 參考"  },
  C: { color: "#ff5252", bg: "#330011", label: "C 反向"  },
};

const MOCK_ANALYSTS = [
  { id: "tsmc_bull", name: "半導體老王", tier: "S", specialty: "半導體,AI", win_rate: 0.71, avg_return: 0.092, total_calls: 45, enabled: true, quality_score: 0.78 },
  { id: "ai_server", name: "AI伺服器達人", tier: "A", specialty: "AI Server,散熱", win_rate: 0.63, avg_return: 0.058, total_calls: 32, enabled: true, quality_score: 0.64 },
  { id: "value_inv", name: "存股研究室", tier: "A", specialty: "存股,高股息", win_rate: 0.68, avg_return: 0.041, total_calls: 28, enabled: true, quality_score: 0.61 },
  { id: "chip_track", name: "籌碼觀察家", tier: "B", specialty: "籌碼,法人", win_rate: 0.52, avg_return: 0.021, total_calls: 19, enabled: true, quality_score: 0.48 },
  { id: "macro_view", name: "總經視角",  tier: "C", specialty: "總經,ETF",  win_rate: 0.31, avg_return: -0.032, total_calls: 15, enabled: true, quality_score: 0.28 },
];

const MOCK_CONSENSUS = [
  { stock: "3661", name: "世芯-KY",  score: 92, tier_str: "S+A", alpha: true, bullish: 4, bearish: 0 },
  { stock: "2382", name: "廣達",     score: 74, tier_str: "A",   alpha: true, bullish: 3, bearish: 0 },
  { stock: "3443", name: "創意電子", score: 68, tier_str: "A",   alpha: true, bullish: 2, bearish: 0 },
  { stock: "2454", name: "聯發科",   score: 45, tier_str: "A",   alpha: false, bullish: 2, bearish: 2, divergent: true },
  { stock: "6669", name: "緯穎",     score: 61, tier_str: "A",   alpha: true, bullish: 2, bearish: 0 },
];

function Panel({ title, icon: Icon, children, className = "" }) {
  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: "8px", padding: "12px" }} className={className}>
      <div style={{ color: C.accent, fontSize: "11px", fontWeight: "bold", marginBottom: "8px", display: "flex", alignItems: "center", gap: "4px" }}>
        {Icon && <Icon size={11} />}{title}
      </div>
      {children}
    </div>
  );
}

function TierBadge({ tier }) {
  const cfg = TIER_STYLE[tier] || TIER_STYLE.B;
  return (
    <span style={{ fontSize: "10px", fontWeight: "bold", padding: "1px 6px", borderRadius: "3px", color: cfg.color, background: cfg.bg }}>
      {cfg.label}
    </span>
  );
}

function AnalystRow({ a, selected, onSelect }) {
  const wr    = (a.win_rate * 100).toFixed(0);
  const ret   = (a.avg_return * 100).toFixed(1);
  const retC  = a.avg_return >= 0 ? C.red : C.green;

  return (
    <div
      onClick={() => onSelect(a)}
      style={{
        display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "6px 8px", cursor: "pointer", borderRadius: "4px",
        background: selected?.id === a.id ? "#001a2a" : "transparent",
        border: selected?.id === a.id ? `1px solid ${C.accent}` : "1px solid transparent",
        marginBottom: "3px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
        <TierBadge tier={a.tier} />
        <span style={{ fontSize: "11px", color: a.enabled ? C.white : C.muted }}>{a.name}</span>
        <span style={{ fontSize: "9px", color: C.muted }}>{a.specialty?.slice(0, 15)}</span>
      </div>
      <div style={{ display: "flex", gap: "8px", fontSize: "10px" }}>
        <span style={{ color: C.green }}>勝率{wr}%</span>
        <span style={{ color: retC }}>{ret >= 0 ? "+" : ""}{ret}%</span>
      </div>
    </div>
  );
}

function ConsensusRow({ c }) {
  const scoreColor = c.score >= 80 ? C.red : c.score >= 60 ? C.orange : C.muted;
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "4px 0", borderBottom: `1px solid ${C.border}` }}>
      <div>
        <span style={{ fontSize: "11px", color: C.white, fontFamily: "monospace" }}>{c.stock}</span>
        <span style={{ fontSize: "10px", color: C.muted, marginLeft: "4px" }}>{c.name}</span>
        {c.divergent && <span style={{ fontSize: "9px", color: C.orange, marginLeft: "4px" }}>分歧⚠️</span>}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
        <span style={{ fontSize: "9px", color: C.muted }}>{c.tier_str}</span>
        <span style={{ fontSize: "11px", fontWeight: "bold", color: scoreColor }}>{c.score}</span>
        <span style={{ fontSize: "10px" }}>{c.alpha ? "✅" : "❌"}</span>
      </div>
    </div>
  );
}

export default function AnalystDashboard() {
  const [analysts, setAnalysts]   = useState(MOCK_ANALYSTS);
  const [consensus, setConsensus] = useState(MOCK_CONSENSUS);
  const [selected, setSelected]   = useState(null);
  const [filter,   setFilter]     = useState("ALL");
  const [loading,  setLoading]    = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/analysts").then(r => r.ok ? r.json() : null);
      if (r?.analysts) setAnalysts(r.analysts);
      const c = await fetch("/api/analysts/consensus").then(r => r.ok ? r.json() : null);
      if (c?.consensus) setConsensus(c.consensus);
    } catch {}
    setLoading(false);
  }, []);

  useEffect(() => { reload(); }, [reload]);

  const filtered = filter === "ALL" ? analysts : analysts.filter(a => a.tier === filter);
  const sel = selected || (analysts.length > 0 ? analysts[0] : null);

  const radarData = sel ? [
    { subject: "勝率",   value: sel.win_rate * 100 },
    { subject: "平均報酬", value: Math.max(0, sel.avg_return * 100 + 5) * 5 },
    { subject: "品質分",  value: sel.quality_score * 100 },
    { subject: "一致性",  value: 60 },
    { subject: "不追高",  value: 70 },
  ] : [];

  return (
    <div style={{ background: C.bg, minHeight: "100vh", padding: "12px", fontFamily: "monospace" }}>

      <div className="flex items-center justify-between mb-3 pb-2" style={{ borderBottom: `1px solid ${C.border}` }}>
        <div className="flex items-center gap-2">
          <Users size={16} color={C.accent} />
          <span style={{ color: C.accent, fontWeight: "bold", letterSpacing: "0.1em" }}>◈ ANALYST UNIVERSE</span>
        </div>
        <button onClick={reload} style={{ color: C.muted, background: "none", border: "none", cursor: "pointer" }}>
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      {/* Tier 篩選 */}
      <div className="flex gap-2 mb-3">
        {["ALL", "S", "A", "B", "C"].map(t => (
          <button key={t} onClick={() => setFilter(t)}
            style={{
              fontSize: "10px", padding: "3px 10px", borderRadius: "4px", cursor: "pointer",
              border: `1px solid ${filter === t ? C.accent : C.border}`,
              color: filter === t ? C.accent : C.muted,
              background: filter === t ? "#001a2a" : "transparent",
            }}>
            {t === "ALL" ? "全部" : `${t}級`}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-12 gap-3">

        {/* 分析師列表 */}
        <div className="col-span-4">
          <Panel title="追蹤分析師" icon={Users}>
            {filtered.map(a => <AnalystRow key={a.id} a={a} selected={sel} onSelect={setSelected} />)}
            <button style={{
              width: "100%", marginTop: "8px", padding: "4px", fontSize: "10px",
              color: C.accent, background: "transparent", border: `1px dashed ${C.border}`,
              borderRadius: "4px", cursor: "pointer",
            }}>
              <Plus size={10} style={{ display: "inline" }} /> 新增分析師
            </button>
          </Panel>
        </div>

        {/* 選中分析師詳情 */}
        <div className="col-span-4 space-y-3">
          {sel && (
            <>
              <Panel title="績效詳情" icon={Star}>
                <div className="flex justify-between mb-2">
                  <div>
                    <div style={{ fontSize: "14px", fontWeight: "bold", color: C.white }}>{sel.name}</div>
                    <TierBadge tier={sel.tier} />
                  </div>
                  <div style={{ textAlign: "right", fontSize: "11px" }}>
                    <div style={{ color: C.green }}>勝率 {(sel.win_rate * 100).toFixed(0)}%</div>
                    <div style={{ color: sel.avg_return >= 0 ? C.red : C.green }}>
                      平均 {sel.avg_return >= 0 ? "+" : ""}{(sel.avg_return * 100).toFixed(1)}%
                    </div>
                    <div style={{ color: C.muted }}>{sel.total_calls} 次推薦</div>
                  </div>
                </div>
                <div style={{ fontSize: "10px", color: C.muted, marginBottom: "4px" }}>專長：{sel.specialty}</div>
                <ResponsiveContainer width="100%" height={120}>
                  <RadarChart data={radarData}>
                    <PolarGrid stroke={C.border} />
                    <PolarAngleAxis dataKey="subject" tick={{ fill: C.muted, fontSize: 9 }} />
                    <Radar name={sel.name} dataKey="value" stroke={C.accent} fill={C.accent} fillOpacity={0.3} />
                  </RadarChart>
                </ResponsiveContainer>
              </Panel>

              <Panel title="操作" icon={TrendingUp}>
                <div className="flex flex-wrap gap-1">
                  {["S", "A", "B", "C"].map(t => (
                    <button key={t}
                      style={{
                        fontSize: "10px", padding: "3px 8px", borderRadius: "3px",
                        border: `1px solid ${sel.tier === t ? C.accent : C.border}`,
                        color: sel.tier === t ? C.accent : C.muted,
                        background: sel.tier === t ? "#001a2a" : "transparent", cursor: "pointer",
                      }}>
                      調為{t}
                    </button>
                  ))}
                  <button style={{ fontSize: "10px", padding: "3px 8px", borderRadius: "3px", border: `1px solid ${C.red}`, color: C.red, background: "transparent", cursor: "pointer" }}>
                    移除
                  </button>
                </div>
              </Panel>
            </>
          )}
        </div>

        {/* 今日共識 */}
        <div className="col-span-4 space-y-3">
          <Panel title="今日分析師共識" icon={TrendingUp}>
            <div style={{ fontSize: "10px", color: C.muted, marginBottom: "6px" }}>
              共識分/100  Alpha✅=系統確認
            </div>
            {consensus.map(c => <ConsensusRow key={c.stock} c={c} />)}
          </Panel>

          <Panel title="系統說明" icon={AlertTriangle}>
            <div style={{ fontSize: "10px", color: C.muted, lineHeight: "1.6" }}>
              <div>S級：Tier×1.5，高可信加強</div>
              <div>A級：Tier×1.0，正常加權</div>
              <div>B級：Tier×0.5，輕微加權</div>
              <div>C級：Tier×-0.3，<span style={{ color: C.red }}>反向處理</span></div>
              <div style={{ marginTop: "4px" }}>專長符合族群 → 加成×1.2</div>
              <div>S/A分歧 → 標記高分歧</div>
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}
