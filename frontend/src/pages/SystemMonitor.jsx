import React, { useEffect, useState, useCallback } from "react";
import { Activity, RefreshCw, CheckCircle, AlertTriangle, XCircle, Clock } from "lucide-react";

const C = {
  bg: "#0a0f1e", surface: "#0f1629", border: "#1e3a5f",
  accent: "#00d4ff", green: "#00e676", red: "#ff5252",
  yellow: "#ffd740", muted: "#7090b0", white: "#e0f0ff",
};

const STATUS_ICON = {
  ok:      { icon: CheckCircle,   color: "#00e676" },
  warning: { icon: AlertTriangle, color: "#ffd740" },
  error:   { icon: XCircle,       color: "#ff5252" },
  unknown: { icon: Clock,         color: "#7090b0" },
};

// 模擬模組狀態（實際從 /api/system/health 取）
const MOCK_MODULES = [
  { name: "morning_report",    status: "ok",      last_run: "08:31", error_count: 0 },
  { name: "ai_feed",           status: "ok",      last_run: "08:31", error_count: 0 },
  { name: "news_scraper",      status: "ok",      last_run: "09:00", error_count: 0 },
  { name: "smart_alert_v2",    status: "warning", last_run: "11:30", error_count: 2 },
  { name: "market_breadth",    status: "ok",      last_run: "11:45", error_count: 0 },
  { name: "autonomous_research",status: "ok",     last_run: "17:30", error_count: 0 },
  { name: "hedge_fund_agent",  status: "ok",      last_run: "18:00", error_count: 0 },
  { name: "sector_heatmap",    status: "ok",      last_run: "18:30", error_count: 0 },
  { name: "watchlist_daily",   status: "ok",      last_run: "19:00", error_count: 0 },
  { name: "portfolio_manager", status: "ok",      last_run: "19:30", error_count: 0 },
  { name: "pipeline_movers",   status: "error",   last_run: "18:00", error_count: 5, message: "API timeout" },
  { name: "database",          status: "ok",      last_run: "--",    error_count: 0 },
];

const MOCK_STATS = {
  total_users:    42,
  active_today:   18,
  total_commands: 1284,
  avg_response_ms: 320,
  push_success_rate: 98.2,
};

function ModuleRow({ name, status, last_run, error_count, message }) {
  const cfg   = STATUS_ICON[status] || STATUS_ICON.unknown;
  const Icon  = cfg.icon;
  return (
    <div className="flex items-center justify-between py-1.5" style={{ borderBottom: `1px solid ${C.bg}` }}>
      <div className="flex items-center gap-2">
        <Icon size={12} color={cfg.color} />
        <span style={{ fontSize: "11px", color: C.white, fontFamily: "monospace" }}>{name}</span>
        {message && <span style={{ fontSize: "9px", color: C.red }}>({message})</span>}
      </div>
      <div className="flex items-center gap-3">
        {error_count > 0 && (
          <span style={{ fontSize: "9px", color: C.red }}>錯誤×{error_count}</span>
        )}
        <span style={{ fontSize: "10px", color: C.muted }}>{last_run}</span>
      </div>
    </div>
  );
}

function StatCard({ label, value, color = C.accent, sub }) {
  return (
    <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: "8px", padding: "10px", textAlign: "center" }}>
      <div style={{ fontSize: "10px", color: C.muted }}>{label}</div>
      <div style={{ fontSize: "20px", fontWeight: "bold", color, fontFamily: "monospace" }}>{value}</div>
      {sub && <div style={{ fontSize: "9px", color: C.muted }}>{sub}</div>}
    </div>
  );
}

export default function SystemMonitor() {
  const [modules,     setModules]     = useState(MOCK_MODULES);
  const [stats,       setStats]       = useState(MOCK_STATS);
  const [health,      setHealth]      = useState(null);
  const [killSwitch,  setKillSwitch]  = useState(null);
  const [isMock,      setIsMock]      = useState(true);
  const [time,        setTime]        = useState(new Date());
  const [loading,     setLoading]     = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [mRes, sRes, ksRes] = await Promise.allSettled([
        fetch("/api/system/health").then(r => r.ok ? r.json() : null),
        fetch("/api/system/stats").then(r => r.ok ? r.json() : null),
        fetch("/api/system/kill-switch").then(r => r.ok ? r.json() : null),
      ]);
      if (mRes.status === "fulfilled" && mRes.value?.modules) {
        setModules(mRes.value.modules);
        setHealth(mRes.value);
        setIsMock(false);
      }
      if (sRes.status === "fulfilled" && sRes.value) setStats(sRes.value);
      if (ksRes.status === "fulfilled" && ksRes.value) setKillSwitch(ksRes.value);
    } catch {}
    setLoading(false);
  }, []);

  useEffect(() => {
    const tick    = setInterval(() => setTime(new Date()), 1000);
    const refresh = setInterval(reload, 60000);
    return () => { clearInterval(tick); clearInterval(refresh); };
  }, [reload]);

  const okCount   = modules.filter(m => m.status === "ok").length;
  const warnCount = modules.filter(m => m.status === "warning").length;
  const errCount  = modules.filter(m => m.status === "error").length;

  const healthPct = Math.round(okCount / modules.length * 100);

  return (
    <div style={{ background: C.bg, minHeight: "100vh", padding: "12px", fontFamily: "monospace" }}>

      {/* Kill Switch 警示 */}
      {killSwitch?.kill_switch_active && (
        <div style={{ background: "#330000", border: `1px solid ${C.red}`, borderRadius: 6, padding: "8px 12px", marginBottom: 8, fontSize: 11, color: C.red }}>
          ⛔ KILL SWITCH 啟動中 — {killSwitch.reason}
          <span style={{ color: C.muted, marginLeft: 8 }}>交易訊號已停止</span>
        </div>
      )}

      {/* 示範資料提示 */}
      {isMock && (
        <div style={{ background: "#1a1200", border: `1px solid ${C.yellow}66`, borderRadius: 6, padding: "5px 12px", marginBottom: 6, fontSize: 10, color: C.yellow }}>
          ⚠️ 示範資料 — 尚未從後端載入真實模組狀態
        </div>
      )}

      {/* 資料品質指標 */}
      {health && (
        <div className="flex gap-4 mb-2" style={{ fontSize: 10, color: C.muted }}>
          <span>全系統可信度：<span style={{ color: health.global_data_quality >= 0.85 ? C.green : C.yellow }}>{(health.global_data_quality * 100).toFixed(0)}%</span></span>
          <span>Mock 比例：<span style={{ color: health.mock_ratio > 0 ? C.red : C.green }}>{(health.mock_ratio * 100).toFixed(0)}%</span></span>
          <span>API 成功率：<span style={{ color: C.accent }}>{(health.api_success_rate * 100).toFixed(0)}%</span></span>
        </div>
      )}

      {/* 標題 */}
      <div className="flex items-center justify-between mb-3 pb-2" style={{ borderBottom: `1px solid ${C.border}` }}>
        <div className="flex items-center gap-2">
          <Activity size={16} color={C.accent} />
          <span style={{ color: C.accent, fontWeight: "bold", letterSpacing: "0.1em" }}>◈ SYSTEM MONITOR</span>
        </div>
        <div className="flex items-center gap-2">
          <span style={{ color: C.muted, fontSize: "11px" }}>{time.toLocaleString("zh-TW")}</span>
          <button onClick={reload} style={{ color: C.muted, background: "none", border: "none", cursor: "pointer" }}>
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {/* 統計卡片 */}
      <div className="grid grid-cols-5 gap-2 mb-3">
        <StatCard label="系統健康"  value={`${healthPct}%`} color={healthPct >= 90 ? C.green : C.yellow} />
        <StatCard label="活躍用戶"  value={stats.active_today} sub="今日" />
        <StatCard label="總用戶"    value={stats.total_users} />
        <StatCard label="平均回應"  value={`${stats.avg_response_ms}ms`} color={C.accent} />
        <StatCard label="推送成功率" value={`${stats.push_success_rate}%`} color={C.green} />
      </div>

      {/* 狀態摘要 */}
      <div className="flex gap-3 mb-3" style={{ fontSize: "11px" }}>
        <span style={{ color: C.green }}>✅ OK：{okCount}</span>
        <span style={{ color: C.yellow }}>⚠️ WARNING：{warnCount}</span>
        <span style={{ color: C.red }}>❌ ERROR：{errCount}</span>
      </div>

      <div className="grid grid-cols-2 gap-3">

        {/* 模組狀態 */}
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: "8px", padding: "12px" }}>
          <div style={{ color: C.accent, fontSize: "11px", fontWeight: "bold", marginBottom: "8px" }}>
            模組健康狀態
          </div>
          {modules.map(m => <ModuleRow key={m.name} {...m} />)}
        </div>

        {/* 使用統計 */}
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: "8px", padding: "12px" }}>
          <div style={{ color: C.accent, fontSize: "11px", fontWeight: "bold", marginBottom: "8px" }}>
            使用量統計
          </div>
          <div style={{ fontSize: "11px", color: C.muted, marginBottom: "12px" }}>
            總指令執行：{stats.total_commands.toLocaleString()} 次
          </div>

          {/* 熱門功能 */}
          <div style={{ color: C.accent, fontSize: "10px", fontWeight: "bold", marginBottom: "6px" }}>最常用功能</div>
          {[
            { name: "/report",    count: 342, pct: 85 },
            { name: "/portfolio", count: 278, pct: 69 },
            { name: "/ai",        count: 215, pct: 54 },
            { name: "/market",    count: 198, pct: 49 },
            { name: "/smart",     count: 156, pct: 39 },
          ].map(f => (
            <div key={f.name} className="flex items-center gap-2 mb-1">
              <span style={{ fontSize: "10px", color: C.white, width: "80px" }}>{f.name}</span>
              <div style={{ flex: 1, height: "6px", background: C.bg, borderRadius: "3px", overflow: "hidden" }}>
                <div style={{ width: `${f.pct}%`, height: "100%", background: C.accent, borderRadius: "3px" }} />
              </div>
              <span style={{ fontSize: "10px", color: C.muted }}>{f.count}</span>
            </div>
          ))}

          {/* 熱門股票 */}
          <div style={{ color: C.accent, fontSize: "10px", fontWeight: "bold", margin: "12px 0 6px" }}>最常查詢股票</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "4px" }}>
            {["2330", "2454", "3686", "6669", "2382", "2317"].map(c => (
              <span key={c} style={{
                fontSize: "10px", padding: "2px 6px", borderRadius: "4px",
                background: "#001a2a", border: `1px solid ${C.border}`, color: C.accent,
              }}>{c}</span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
