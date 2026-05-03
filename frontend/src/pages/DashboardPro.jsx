import React, { useEffect, useState, useCallback } from "react";
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis,
  Tooltip, ResponsiveContainer, Cell,
} from "recharts";
import {
  Activity, TrendingUp, TrendingDown, AlertTriangle,
  Eye, Zap, DollarSign, BarChart2, RefreshCw,
} from "lucide-react";
import { getMarketOverview, getPortfolio, getNews, getMarketAnomaly } from "../utils/api";

// ── 顏色常數 ─────────────────────────────────────────────────────────────────
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

// ── RS 排行假資料（實際從 /api/rs 取）────────────────────────────────────────
const MOCK_RS = [
  { code: "2330", name: "台積電", rs: 2.1, chg: 1.6 },
  { code: "3686", name: "旭隼",   rs: 1.8, chg: 9.8 },
  { code: "6669", name: "緯穎",   rs: 1.7, chg: 3.2 },
  { code: "2454", name: "聯發科", rs: 1.5, chg: 1.1 },
  { code: "3443", name: "創意",   rs: 1.4, chg: 2.3 },
];

const MOCK_SECTORS = [
  { name: "散熱",   chg: 3.2 },
  { name: "AI伺服器", chg: 2.8 },
  { name: "PCB",    chg: 1.5 },
  { name: "半導體", chg: 1.1 },
  { name: "電商",   chg: 0.3 },
  { name: "金融",   chg: -0.5 },
  { name: "航運",   chg: -1.8 },
  { name: "鋼鐵",   chg: -2.1 },
];

// ── Sub-components ───────────────────────────────────────────────────────────

function Panel({ title, icon: Icon, children, className = "" }) {
  return (
    <div className={`bg-[#0f1629] border border-[#1e3a5f] rounded-lg p-3 ${className}`}>
      <div className="flex items-center gap-1.5 mb-2 border-b border-[#1e3a5f] pb-1.5">
        {Icon && <Icon size={13} className="text-[#00d4ff]" />}
        <span className="text-[#00d4ff] text-xs font-bold tracking-wider uppercase">{title}</span>
      </div>
      {children}
    </div>
  );
}

function Stat({ label, value, sub, color = C.white }) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] text-[#7090b0] tracking-wider">{label}</span>
      <span className="text-sm font-mono font-bold" style={{ color }}>{value}</span>
      {sub && <span className="text-[10px] text-[#7090b0]">{sub}</span>}
    </div>
  );
}

function SectorBar({ name, chg }) {
  const color = chg >= 3 ? "#CC0022" : chg >= 1 ? "#ff5252" :
                chg <= -3 ? "#006633" : chg <= -1 ? "#00e676" : "#7090b0";
  const sign  = chg >= 0 ? "+" : "";
  return (
    <div className="flex items-center gap-2 py-0.5">
      <span className="text-[11px] text-[#7090b0] w-16 truncate">{name}</span>
      <div className="flex-1 bg-[#0a0f1e] h-3 rounded overflow-hidden">
        <div
          className="h-full rounded transition-all"
          style={{
            width:      `${Math.min(Math.abs(chg) / 5 * 100, 100)}%`,
            background: color,
          }}
        />
      </div>
      <span className="text-[11px] font-mono w-10 text-right" style={{ color }}>
        {sign}{chg.toFixed(1)}%
      </span>
    </div>
  );
}

function RSRow({ code, name, rs, chg }) {
  const color = chg >= 0 ? C.red : C.green;
  const sign  = chg >= 0 ? "+" : "";
  return (
    <div className="flex items-center justify-between py-0.5 border-b border-[#1e3a5f] last:border-0">
      <span className="text-[11px] text-[#e0f0ff] font-mono">
        {code} <span className="text-[#7090b0]">{name}</span>
      </span>
      <div className="flex gap-3">
        <span className="text-[11px] font-mono text-[#00d4ff]">RS {rs.toFixed(1)}</span>
        <span className="text-[11px] font-mono" style={{ color }}>
          {sign}{chg.toFixed(1)}%
        </span>
      </div>
    </div>
  );
}

// ── Main Page ────────────────────────────────────────────────────────────────

export default function DashboardPro() {
  const [market,    setMarket]    = useState(null);
  const [portfolio, setPortfolio] = useState([]);
  const [news,      setNews]      = useState([]);
  const [anomaly,   setAnomaly]   = useState(null);
  const [time,      setTime]      = useState(new Date());
  const [loading,   setLoading]   = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const [m, p, n, a] = await Promise.allSettled([
      getMarketOverview(),
      getPortfolio(),
      getNews({ limit: 5 }),
      getMarketAnomaly(),
    ]);
    if (m.status === "fulfilled") setMarket(m.value);
    if (p.status === "fulfilled") setPortfolio(Array.isArray(p.value) ? p.value : []);
    if (n.status === "fulfilled") setNews(Array.isArray(n.value) ? n.value : []);
    if (a.status === "fulfilled") setAnomaly(a.value);
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
    const tick    = setInterval(() => setTime(new Date()), 1000);
    const refresh = setInterval(load, 60000);
    return () => { clearInterval(tick); clearInterval(refresh); };
  }, [load]);

  const totalMV  = portfolio.reduce((s, h) => s + (h.market_value || 0), 0);
  const totalPnL = portfolio.reduce((s, h) => s + (h.pnl || 0), 0);
  const pnlPct   = totalMV > 0 ? totalPnL / (totalMV - totalPnL) * 100 : 0;
  const pnlColor = totalPnL >= 0 ? C.red : C.green;

  const mktChg    = market?.change_pct || 0;
  const mktColor  = mktChg >= 0 ? C.red : C.green;
  const mktSign   = mktChg >= 0 ? "+" : "";

  return (
    <div
      className="p-3 space-y-3 overflow-auto"
      style={{ background: C.bg, minHeight: "100vh", fontFamily: "monospace" }}
    >
      {/* ── 頂部標題列 ─────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between border-b border-[#1e3a5f] pb-2">
        <div className="flex items-center gap-2">
          <Activity size={16} className="text-[#00d4ff]" />
          <span className="text-[#00d4ff] font-bold tracking-widest text-sm">
            ◈ PRO DASHBOARD
          </span>
          {anomaly?.has_anomaly && (
            <span className="text-[10px] bg-yellow-800 text-yellow-300 px-2 py-0.5 rounded-full">
              ⚠ ANOMALY
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[#7090b0] text-[11px]">{time.toLocaleTimeString("zh-TW")}</span>
          <button onClick={load} className="text-[#7090b0] hover:text-[#00d4ff]">
            <RefreshCw size={12} />
          </button>
        </div>
      </div>

      {/* ── 3 欄佈局 ───────────────────────────────────────────────────── */}
      <div className="grid grid-cols-12 gap-3">

        {/* ── 左側欄 ──────────────────────────────────────────────────── */}
        <div className="col-span-3 space-y-3">

          {/* 市場狀態 */}
          <Panel title="市場狀態" icon={Activity}>
            <div className="space-y-2">
              <Stat
                label="加權指數"
                value={market ? market.value?.toLocaleString("zh-TW", { maximumFractionDigits: 0 }) : "--"}
                sub={market ? `${mktSign}${mktChg.toFixed(2)}%` : "--"}
                color={mktColor}
              />
              <Stat label="成交量" value={market?.volume ? `${(market.volume / 1e8).toFixed(0)}億` : "--"} />
              <div className="text-[10px]" style={{ color: mktColor }}>
                {mktChg >= 1 ? "🟢 強勢多頭" : mktChg >= 0 ? "🟡 盤整偏多" : mktChg >= -1 ? "🟡 盤整偏弱" : "🔴 弱勢空頭"}
              </div>
            </div>
          </Panel>

          {/* 族群熱度 */}
          <Panel title="族群熱度" icon={TrendingUp}>
            <div className="space-y-0.5">
              {MOCK_SECTORS.map(s => <SectorBar key={s.name} {...s} />)}
            </div>
          </Panel>

          {/* AI Feed */}
          <Panel title="AI Feed" icon={Zap}>
            <div className="space-y-1 text-[11px]">
              <div className="text-[#e0f0ff]">📈 主流族群：散熱 AI伺服器</div>
              <div className="text-[#ff5252]">🔥 最強：2330 +1.6%</div>
              {anomaly?.has_anomaly && (
                <div className="text-[#ffd740]">⚠️ {anomaly.alert_msg?.slice(0, 40)}</div>
              )}
              <div className="text-[#7090b0]">情緒：偏多 72/100</div>
            </div>
          </Panel>
        </div>

        {/* ── 中間主區 ─────────────────────────────────────────────────── */}
        <div className="col-span-6 space-y-3">

          {/* 庫存總覽 */}
          <Panel title="投組總覽" icon={BarChart2}>
            <div className="grid grid-cols-3 gap-3 mb-3">
              <Stat label="市值" value={`$${(totalMV / 1e4).toFixed(0)}萬`} />
              <Stat
                label="未實現損益"
                value={`${totalPnL >= 0 ? "+" : ""}${(totalPnL / 1e4).toFixed(1)}萬`}
                color={pnlColor}
              />
              <Stat label="報酬率" value={`${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%`} color={pnlColor} />
            </div>
            {portfolio.length > 0 ? (
              <ResponsiveContainer width="100%" height={80}>
                <BarChart data={portfolio.slice(0, 8)} margin={{ top: 0, right: 0, left: -30, bottom: 0 }}>
                  <XAxis dataKey="stock_code" tick={{ fontSize: 9, fill: C.muted }} />
                  <YAxis tick={{ fontSize: 9, fill: C.muted }} />
                  <Tooltip
                    contentStyle={{ background: C.surface, border: `1px solid ${C.border}`, fontSize: 10 }}
                    formatter={(v) => [`${v >= 0 ? "+" : ""}${v?.toFixed(1)}%`, "損益"]}
                  />
                  <Bar dataKey="pnl_pct" radius={[2, 2, 0, 0]}>
                    {portfolio.slice(0, 8).map((h, i) => (
                      <Cell key={i} fill={(h.pnl_pct || 0) >= 0 ? C.red : C.green} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <div className="text-center text-[#7090b0] text-xs py-4">尚無持股資料</div>
            )}
          </Panel>

          {/* 持股列表 */}
          <Panel title="持股明細" icon={Eye}>
            <div className="overflow-auto max-h-48">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-[#7090b0] border-b border-[#1e3a5f]">
                    <th className="text-left pb-1">代碼</th>
                    <th className="text-right pb-1">成本</th>
                    <th className="text-right pb-1">現價</th>
                    <th className="text-right pb-1">損益%</th>
                  </tr>
                </thead>
                <tbody>
                  {portfolio.slice(0, 8).map((h) => {
                    const pct   = h.pnl_pct || 0;
                    const color = pct >= 0 ? C.red : C.green;
                    return (
                      <tr key={h.id} className="border-b border-[#0a0f1e] hover:bg-[#0a0f1e]">
                        <td className="py-0.5 text-[#e0f0ff]">{h.stock_code} <span className="text-[#7090b0]">{h.stock_name?.slice(0, 3)}</span></td>
                        <td className="text-right text-[#7090b0]">{h.cost_price?.toFixed(0)}</td>
                        <td className="text-right text-[#e0f0ff]">{h.current_price?.toFixed(0) || "--"}</td>
                        <td className="text-right font-bold" style={{ color }}>
                          {pct >= 0 ? "+" : ""}{pct.toFixed(1)}%
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Panel>
        </div>

        {/* ── 右側欄 ───────────────────────────────────────────────────── */}
        <div className="col-span-3 space-y-3">

          {/* RS Ranking */}
          <Panel title="RS 強勢排行" icon={TrendingUp}>
            {MOCK_RS.map(r => <RSRow key={r.code} {...r} />)}
            <div className="text-[10px] text-[#7090b0] mt-1">輸入 /rs 查看完整排行</div>
          </Panel>

          {/* 即時警報 */}
          <Panel title="即時警報" icon={AlertTriangle}>
            {anomaly?.has_anomaly ? (
              <div className="text-[11px] text-[#ffd740] space-y-1">
                <div>⚠️ {anomaly.alert_msg?.slice(0, 60)}</div>
              </div>
            ) : (
              <div className="text-[11px] text-[#7090b0]">✅ 無異常警報</div>
            )}
          </Panel>

          {/* 最新消息 */}
          <Panel title="市場新聞" icon={DollarSign}>
            <div className="space-y-1.5 max-h-48 overflow-auto">
              {news.slice(0, 5).map((n, i) => (
                <div key={i} className="text-[10px] text-[#7090b0] border-b border-[#1e3a5f] pb-1 last:border-0">
                  <span className="text-[#00d4ff]">{n.related_stocks?.[0] || ""}</span>
                  {" "}{n.title?.slice(0, 35)}
                </div>
              ))}
            </div>
          </Panel>
        </div>
      </div>

      {/* ── 底部廣度 + 市場狀態 ─────────────────────────────────────────── */}
      <div className="grid grid-cols-2 gap-3 border-t border-[#1e3a5f] pt-3">
        <Panel title="市場廣度" icon={BarChart2}>
          <div className="flex gap-6 text-[11px]">
            <Stat label="上漲" value={market?.advances || "--"} color={C.red} />
            <Stat label="下跌" value={market?.declines || "--"} color={C.green} />
            <Stat label="廣度評分" value="72/100" color={C.accent} />
          </div>
        </Panel>
        <Panel title="AI 今日操作建議" icon={Zap}>
          <div className="text-[11px] text-[#e0f0ff] space-y-0.5">
            <div>➕ 買進：2330 散熱族群</div>
            <div>⚖️ 觀察：2454 等方向確認</div>
            <div className="text-[#7090b0]">輸入 /manage 查看完整建議</div>
          </div>
        </Panel>
      </div>
    </div>
  );
}
