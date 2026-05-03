import React, { useState } from "react";
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis,
  Tooltip, ResponsiveContainer, Cell, Legend,
} from "recharts";
import { TrendingUp, BarChart2, Activity } from "lucide-react";

const C = {
  bg: "#0a0f1e", surface: "#0f1629", border: "#1e3a5f",
  accent: "#00d4ff", green: "#00e676", red: "#ff5252",
  yellow: "#ffd740", muted: "#7090b0", white: "#e0f0ff",
};

// 模擬資料
function genCurve(start, vol, n) {
  let v = start;
  return Array.from({ length: n }, (_, i) => {
    v += (Math.random() - 0.45) * vol;
    return { date: `W${i + 1}`, value: Math.round(v * 100) / 100 };
  });
}

const STRATEGIES = [
  { name: "動能策略",   color: C.red,    data: genCurve(100, 3, 24), wr: 62, sharpe: 1.84, mdd: -12.3 },
  { name: "AI籌碼追蹤", color: C.accent, data: genCurve(100, 4, 24), wr: 58, sharpe: 1.52, mdd: -15.8 },
  { name: "高股息存股", color: C.green,  data: genCurve(100, 1.5, 24), wr: 71, sharpe: 2.1,  mdd: -6.4  },
  { name: "技術突破",   color: C.yellow, data: genCurve(100, 5, 24), wr: 55, sharpe: 1.3,  mdd: -19.2 },
];

const MONTHLY_RETURNS = [
  { month: "1月", momentum: 4.2, ai_chip: 3.1, dividend: 1.2, breakout: 6.5 },
  { month: "2月", momentum: -2.1, ai_chip: 1.5, dividend: 0.8, breakout: -4.2 },
  { month: "3月", momentum: 7.3, ai_chip: 5.8, dividend: 1.5, breakout: 9.1 },
  { month: "4月", momentum: 3.1, ai_chip: 2.2, dividend: 1.0, breakout: 4.8 },
  { month: "5月", momentum: 1.5, ai_chip: 3.7, dividend: 1.3, breakout: 2.1 },
  { month: "6月", momentum: -1.8, ai_chip: -0.5, dividend: 0.9, breakout: -3.2 },
];

const RANGES = ["1個月", "3個月", "6個月", "1年"];

function Panel({ title, icon: Icon, children }) {
  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: "8px", padding: "12px" }}>
      <div style={{ color: C.accent, fontSize: "11px", fontWeight: "bold", marginBottom: "8px", display: "flex", alignItems: "center", gap: "4px" }}>
        {Icon && <Icon size={11} />}
        {title}
      </div>
      {children}
    </div>
  );
}

export default function StrategyPerformance() {
  const [range,    setRange]    = useState("6個月");
  const [selected, setSelected] = useState(["動能策略", "AI籌碼追蹤", "高股息存股"]);

  const toggle = (name) => {
    setSelected(prev =>
      prev.includes(name) ? prev.filter(n => n !== name) : [...prev, name]
    );
  };

  const latestVals = STRATEGIES.reduce((m, s) => {
    m[s.name] = s.data[s.data.length - 1].value - 100;
    return m;
  }, {});

  return (
    <div style={{ background: C.bg, minHeight: "100vh", padding: "12px", fontFamily: "monospace" }}>

      {/* 標題 */}
      <div className="flex items-center justify-between mb-3 pb-2" style={{ borderBottom: `1px solid ${C.border}` }}>
        <div className="flex items-center gap-2">
          <BarChart2 size={16} color={C.accent} />
          <span style={{ color: C.accent, fontWeight: "bold", letterSpacing: "0.1em" }}>◈ STRATEGY PERFORMANCE</span>
        </div>
        <div className="flex gap-1">
          {RANGES.map(r => (
            <button key={r} onClick={() => setRange(r)}
              style={{
                fontSize: "10px", padding: "2px 8px", borderRadius: "4px",
                border: `1px solid ${range === r ? C.accent : C.border}`,
                color: range === r ? C.accent : C.muted,
                background: range === r ? "#001a2a" : "transparent",
                cursor: "pointer",
              }}>{r}</button>
          ))}
        </div>
      </div>

      {/* 策略選擇 */}
      <div className="flex gap-2 mb-3">
        {STRATEGIES.map(s => (
          <button key={s.name} onClick={() => toggle(s.name)}
            style={{
              fontSize: "10px", padding: "3px 10px", borderRadius: "4px",
              border: `1px solid ${selected.includes(s.name) ? s.color : C.border}`,
              color: selected.includes(s.name) ? s.color : C.muted,
              background: selected.includes(s.name) ? `${s.color}15` : "transparent",
              cursor: "pointer",
            }}>
            {s.name}
            <span style={{ marginLeft: "4px" }}>
              {latestVals[s.name] >= 0 ? "+" : ""}{latestVals[s.name].toFixed(1)}%
            </span>
          </button>
        ))}
      </div>

      <div className="grid grid-cols-2 gap-3">

        {/* 績效曲線 */}
        <div className="col-span-2">
          <Panel title="策略累積報酬曲線" icon={TrendingUp}>
            <ResponsiveContainer width="100%" height={180}>
              <LineChart data={STRATEGIES[0].data}>
                <XAxis dataKey="date" tick={{ fontSize: 9, fill: C.muted }} />
                <YAxis tick={{ fontSize: 9, fill: C.muted }} domain={["auto", "auto"]} />
                <Tooltip contentStyle={{ background: C.surface, border: `1px solid ${C.border}`, fontSize: 10 }} />
                {STRATEGIES.filter(s => selected.includes(s.name)).map(s => (
                  <Line key={s.name} type="monotone"
                    data={s.data} dataKey="value"
                    stroke={s.color} strokeWidth={1.5} dot={false} name={s.name}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </Panel>
        </div>

        {/* 策略績效表 */}
        <Panel title="策略指標比較" icon={BarChart2}>
          <table style={{ width: "100%", fontSize: "11px", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ borderBottom: `1px solid ${C.border}`, color: C.muted }}>
                <th style={{ textAlign: "left", padding: "4px 0" }}>策略</th>
                <th style={{ textAlign: "right" }}>勝率</th>
                <th style={{ textAlign: "right" }}>夏普</th>
                <th style={{ textAlign: "right" }}>最大回撤</th>
              </tr>
            </thead>
            <tbody>
              {STRATEGIES.map(s => (
                <tr key={s.name} style={{ borderBottom: `1px solid ${C.bg}`, opacity: selected.includes(s.name) ? 1 : 0.4 }}>
                  <td style={{ padding: "3px 0", color: s.color }}>{s.name}</td>
                  <td style={{ textAlign: "right", color: C.green }}>{s.wr}%</td>
                  <td style={{ textAlign: "right", color: C.accent }}>{s.sharpe}</td>
                  <td style={{ textAlign: "right", color: C.red }}>{s.mdd}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        {/* 月度報酬分佈 */}
        <Panel title="月度報酬分佈" icon={Activity}>
          <ResponsiveContainer width="100%" height={120}>
            <BarChart data={MONTHLY_RETURNS}>
              <XAxis dataKey="month" tick={{ fontSize: 9, fill: C.muted }} />
              <YAxis tick={{ fontSize: 9, fill: C.muted }} />
              <Tooltip contentStyle={{ background: C.surface, border: `1px solid ${C.border}`, fontSize: 10 }} />
              {selected.includes("動能策略") && <Bar dataKey="momentum" name="動能" fill={C.red} radius={[2,2,0,0]} />}
              {selected.includes("AI籌碼追蹤") && <Bar dataKey="ai_chip" name="AI籌碼" fill={C.accent} radius={[2,2,0,0]} />}
              {selected.includes("高股息存股") && <Bar dataKey="dividend" name="高股息" fill={C.green} radius={[2,2,0,0]} />}
            </BarChart>
          </ResponsiveContainer>
        </Panel>
      </div>
    </div>
  );
}
