import React, { useEffect, useState, useRef } from "react";
import { Activity, TrendingUp, TrendingDown, Zap, Bell } from "lucide-react";

const C = {
  bg: "#0a0f1e", surface: "#0f1629", border: "#1e3a5f",
  accent: "#00d4ff", green: "#00e676", red: "#ff5252",
  yellow: "#ffd740", muted: "#7090b0", white: "#e0f0ff",
};

const MOCK_SECTORS = [
  { name: "散熱",    chg: 3.2, flow: "+28億" },
  { name: "AI伺服器", chg: 2.8, flow: "+22億" },
  { name: "半導體",  chg: 1.1, flow: "+18億" },
  { name: "PCB",     chg: 0.5, flow: "+8億"  },
  { name: "金融",    chg: -0.3, flow: "-3億"  },
  { name: "航運",    chg: -1.8, flow: "-15億" },
];

function HeatCell({ name, chg, flow }) {
  const bg = chg >= 3 ? "#CC0022" : chg >= 1 ? "#882222" : chg <= -3 ? "#006633" : chg <= -1 ? "#224422" : "#1A2A40";
  const color = "#FFFFFF";
  const sign  = chg >= 0 ? "+" : "";
  return (
    <div className="rounded p-2 text-center" style={{ background: bg, border: "1px solid #1e3a5f" }}>
      <div className="text-[11px] font-bold" style={{ color }}>{name}</div>
      <div className="text-[13px] font-mono font-bold" style={{ color }}>{sign}{chg.toFixed(1)}%</div>
      <div className="text-[9px]" style={{ color: "rgba(255,255,255,0.7)" }}>{flow}</div>
    </div>
  );
}

function TopStockRow({ rank, code, name, chg, rs }) {
  const color = chg >= 0 ? C.red : C.green;
  const sign  = chg >= 0 ? "+" : "";
  return (
    <div className="flex items-center justify-between py-1 border-b" style={{ borderColor: C.border }}>
      <span className="text-[11px]" style={{ color: C.muted }}>#{rank}</span>
      <span className="text-[11px] font-mono flex-1 ml-2" style={{ color: C.white }}>
        {code} <span style={{ color: C.muted }}>{name}</span>
      </span>
      <span className="text-[11px] font-mono" style={{ color }}>
        {sign}{chg.toFixed(1)}%
      </span>
      <span className="text-[10px] ml-2" style={{ color: C.accent }}>RS {rs}</span>
    </div>
  );
}

const MOCK_TOP = [
  { code: "3686", name: "旭隼",   chg: 9.8, rs: 3.2 },
  { code: "3443", name: "創意",   chg: 5.2, rs: 2.8 },
  { code: "6669", name: "緯穎",   chg: 3.8, rs: 2.5 },
  { code: "2330", name: "台積電", chg: 1.6, rs: 2.1 },
  { code: "2454", name: "聯發科", chg: 1.1, rs: 1.8 },
];

export default function LiveDashboard() {
  const [market,   setMarket]   = useState(null);
  const [news,     setNews]     = useState([]);
  const [time,     setTime]     = useState(new Date());
  const [alerts,   setAlerts]   = useState([]);
  const [wsStatus, setWsStatus] = useState("connecting");
  const wsRef = useRef(null);

  useEffect(() => {
    const tick = setInterval(() => setTime(new Date()), 1000);

    // 載入新聞
    fetch("/api/news?limit=6")
      .then(r => r.ok ? r.json() : [])
      .then(d => setNews(Array.isArray(d) ? d : []))
      .catch(() => {});

    // WebSocket 連線
    const wsUrl = `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/ws/market`;
    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      ws.onopen  = () => setWsStatus("connected");
      ws.onclose = () => setWsStatus("disconnected");
      ws.onerror = () => setWsStatus("error");
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === "market") setMarket(msg.data);
          if (msg.type === "alert")  setAlerts(prev => [msg.data, ...prev].slice(0, 5));
        } catch {}
      };
    } catch {}

    return () => {
      clearInterval(tick);
      wsRef.current?.close();
    };
  }, []);

  const mktChg  = market?.change_pct || 0;
  const mktColor = mktChg >= 0 ? C.red : C.green;

  return (
    <div style={{ background: C.bg, minHeight: "100vh", padding: "12px", fontFamily: "monospace" }}>

      {/* 頂部 */}
      <div className="flex items-center justify-between mb-3 pb-2" style={{ borderBottom: `1px solid ${C.border}` }}>
        <div className="flex items-center gap-2">
          <Activity size={16} color={C.accent} />
          <span style={{ color: C.accent, fontWeight: "bold", letterSpacing: "0.1em" }}>◈ LIVE MARKET</span>
          <span style={{
            fontSize: "10px", padding: "2px 6px", borderRadius: "4px",
            background: wsStatus === "connected" ? "#003300" : "#330000",
            color: wsStatus === "connected" ? C.green : C.red,
          }}>
            {wsStatus === "connected" ? "● LIVE" : "● OFFLINE"}
          </span>
        </div>
        <span style={{ color: C.muted, fontSize: "11px" }}>{time.toLocaleString("zh-TW")}</span>
      </div>

      <div className="grid grid-cols-2 gap-3">

        {/* 大盤指數 */}
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: "8px", padding: "12px" }}>
          <div style={{ color: C.accent, fontSize: "11px", fontWeight: "bold", marginBottom: "8px" }}>
            📊 加權指數（每分鐘更新）
          </div>
          <div style={{ fontSize: "28px", fontWeight: "bold", color: mktColor }}>
            {market?.value?.toLocaleString("zh-TW", { maximumFractionDigits: 0 }) || "--"}
          </div>
          <div style={{ fontSize: "14px", color: mktColor }}>
            {mktChg >= 0 ? "▲+" : "▼"}{Math.abs(mktChg).toFixed(2)}%
          </div>
          <div style={{ fontSize: "10px", color: C.muted, marginTop: "4px" }}>
            {mktChg >= 1 ? "多頭格局" : mktChg >= 0 ? "偏多整理" : mktChg >= -1 ? "偏弱整理" : "空頭走勢"}
          </div>
        </div>

        {/* 即時警報 */}
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: "8px", padding: "12px" }}>
          <div style={{ color: C.accent, fontSize: "11px", fontWeight: "bold", marginBottom: "8px" }}>
            <Bell size={11} style={{ display: "inline", marginRight: "4px" }} />
            即時警報
          </div>
          {alerts.length > 0 ? (
            alerts.map((a, i) => (
              <div key={i} style={{ fontSize: "11px", color: C.yellow, marginBottom: "4px" }}>
                ⚠️ {typeof a === "string" ? a : JSON.stringify(a).slice(0, 50)}
              </div>
            ))
          ) : (
            <div style={{ fontSize: "11px", color: C.muted }}>✅ 無異常警報</div>
          )}
        </div>

        {/* 族群熱力圖 */}
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: "8px", padding: "12px" }}>
          <div style={{ color: C.accent, fontSize: "11px", fontWeight: "bold", marginBottom: "8px" }}>
            🌡️ 族群熱度（每15分鐘更新）
          </div>
          <div className="grid grid-cols-3 gap-1">
            {MOCK_SECTORS.map(s => <HeatCell key={s.name} {...s} />)}
          </div>
        </div>

        {/* 今日強勢股 */}
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: "8px", padding: "12px" }}>
          <div style={{ color: C.accent, fontSize: "11px", fontWeight: "bold", marginBottom: "8px" }}>
            🔥 今日強勢 Top 10
          </div>
          {MOCK_TOP.map((s, i) => <TopStockRow key={s.code} rank={i + 1} {...s} />)}
        </div>

        {/* 即時新聞 */}
        <div className="col-span-2" style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: "8px", padding: "12px" }}>
          <div style={{ color: C.accent, fontSize: "11px", fontWeight: "bold", marginBottom: "8px" }}>
            📰 即時新聞 + 情緒
          </div>
          <div className="grid grid-cols-2 gap-2">
            {news.slice(0, 6).map((n, i) => (
              <div key={i} style={{ fontSize: "10px", color: C.muted, borderBottom: `1px solid ${C.border}`, paddingBottom: "4px" }}>
                <span style={{ color: C.accent }}>{n.related_stocks?.[0] || ""}</span>
                {" "}{n.title?.slice(0, 40)}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
