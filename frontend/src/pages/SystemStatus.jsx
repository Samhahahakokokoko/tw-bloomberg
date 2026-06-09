import React, { useEffect, useState, useCallback } from "react";
import { CheckCircle, AlertTriangle, XCircle, RefreshCw, Wifi } from "lucide-react";

const C = {
  bg: "#0a0f1e", surface: "#0f1629", border: "#1e3a5f",
  accent: "#00d4ff", green: "#00e676", red: "#ff5252",
  yellow: "#ffd740", muted: "#7090b0", white: "#e0f0ff",
};

const STATUS_CFG = {
  ok:       { icon: CheckCircle,   color: C.green,  label: "正常",   dot: "#00e676" },
  warning:  { icon: AlertTriangle, color: C.yellow, label: "警告",   dot: "#ffd740" },
  error:    { icon: XCircle,       color: C.red,    label: "異常",   dot: "#ff5252" },
  degraded: { icon: AlertTriangle, color: C.red,    label: "部分中斷", dot: "#ff5252" },
};

const OVERALL_BG = {
  ok:       "#001a0a",
  warning:  "#1a1400",
  degraded: "#1a0000",
  error:    "#1a0000",
};

const TYPE_LABEL = {
  morning: "早報",
  weekly:  "週報",
  daily:   "每日決策",
  analyst: "分析師共識",
  alert:   "警報",
};

function StatusDot({ status }) {
  const cfg = STATUS_CFG[status] || STATUS_CFG.ok;
  return (
    <span style={{
      display: "inline-block", width: 8, height: 8, borderRadius: "50%",
      background: cfg.dot, boxShadow: `0 0 6px ${cfg.dot}`,
      animation: status === "ok" ? "pulse 2s infinite" : "none",
    }} />
  );
}

function ServiceRow({ name, status, latency_ms, message }) {
  const cfg  = STATUS_CFG[status] || STATUS_CFG.ok;
  const Icon = cfg.icon;
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "space-between",
      padding: "10px 12px", borderBottom: `1px solid ${C.border}`,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <Icon size={14} color={cfg.color} />
        <div>
          <div style={{ fontSize: 13, color: C.white, fontWeight: 500 }}>{name}</div>
          {message && <div style={{ fontSize: 11, color: C.muted, marginTop: 2 }}>{message}</div>}
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        {latency_ms != null && (
          <span style={{ fontSize: 11, color: C.muted, fontFamily: "monospace" }}>
            {latency_ms}ms
          </span>
        )}
        <span style={{
          fontSize: 11, padding: "2px 8px", borderRadius: 4, fontWeight: 600,
          background: `${cfg.color}22`, color: cfg.color,
        }}>
          {cfg.label}
        </span>
      </div>
    </div>
  );
}

function PushStatBadge({ type, count }) {
  return (
    <div style={{
      display: "flex", flexDirection: "column", alignItems: "center",
      padding: "8px 14px", borderRadius: 8,
      background: "#0d1e35", border: `1px solid ${C.border}`,
      minWidth: 80,
    }}>
      <span style={{ fontSize: 20, fontWeight: "bold", color: C.accent, fontFamily: "monospace" }}>
        {count}
      </span>
      <span style={{ fontSize: 10, color: C.muted, marginTop: 2 }}>
        {TYPE_LABEL[type] || type}
      </span>
    </div>
  );
}

const MOCK = {
  overall: "ok",
  updated_at: "--",
  services: {
    railway:  { name: "Railway 服務", status: "ok",      latency_ms: 0,   message: "運行中" },
    database: { name: "資料庫",       status: "ok",      latency_ms: 2,   message: "連線正常" },
    twse:     { name: "TWSE API",     status: "ok",      latency_ms: 320, message: "即時報價正常" },
    line_bot: { name: "LINE Bot",     status: "ok",      latency_ms: 180, message: "Bot 正常" },
  },
  push_today: 0,
  push_by_type: {},
  last_push_time: "",
};

export default function SystemStatus() {
  const [data,    setData]    = useState(MOCK);
  const [loading, setLoading] = useState(false);
  const [isMock,  setIsMock]  = useState(true);
  const [time,    setTime]    = useState(new Date());

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch("/api/system/status");
      if (r.ok) {
        const json = await r.json();
        setData(json);
        setIsMock(false);
      }
    } catch {}
    setLoading(false);
  }, []);

  useEffect(() => {
    reload();
    const tick    = setInterval(() => setTime(new Date()), 1000);
    const refresh = setInterval(reload, 60000);
    return () => { clearInterval(tick); clearInterval(refresh); };
  }, [reload]);

  const overall = data.overall || "ok";
  const cfg     = STATUS_CFG[overall] || STATUS_CFG.ok;
  const Icon    = cfg.icon;
  const services = Object.values(data.services || {});
  const pushTypes = Object.entries(data.push_by_type || {});

  return (
    <div style={{ background: C.bg, minHeight: "100vh", padding: "20px", fontFamily: "system-ui, sans-serif" }}>

      <style>{`
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
      `}</style>

      {/* 頁首 */}
      <div style={{ maxWidth: 720, margin: "0 auto" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 20 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <Wifi size={20} color={C.accent} />
            <div>
              <div style={{ color: C.accent, fontWeight: "bold", fontSize: 18, letterSpacing: "0.05em" }}>
                TW Bloomberg — 系統狀態
              </div>
              <div style={{ color: C.muted, fontSize: 11, marginTop: 2 }}>
                {time.toLocaleString("zh-TW")}
              </div>
            </div>
          </div>
          <button
            onClick={reload}
            style={{ background: "none", border: `1px solid ${C.border}`, borderRadius: 6, padding: "5px 10px", color: C.muted, cursor: "pointer", display: "flex", alignItems: "center", gap: 6 }}
          >
            <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
            <span style={{ fontSize: 12 }}>更新</span>
          </button>
        </div>

        {/* 示範資料提示 */}
        {isMock && (
          <div style={{ background: "#1a1200", border: `1px solid ${C.yellow}66`, borderRadius: 6, padding: "8px 14px", marginBottom: 16, fontSize: 12, color: C.yellow }}>
            ⚠ 正在載入中…
          </div>
        )}

        {/* 整體狀態橫幅 */}
        <div style={{
          background: OVERALL_BG[overall] || OVERALL_BG.ok,
          border: `1px solid ${cfg.color}44`,
          borderRadius: 10,
          padding: "16px 20px",
          marginBottom: 20,
          display: "flex",
          alignItems: "center",
          gap: 14,
        }}>
          <Icon size={28} color={cfg.color} />
          <div>
            <div style={{ fontSize: 16, fontWeight: "bold", color: cfg.color }}>
              {overall === "ok" ? "所有系統正常運作" :
               overall === "warning" ? "部分服務輕微異常" : "系統發生異常"}
            </div>
            <div style={{ fontSize: 12, color: C.muted, marginTop: 3 }}>
              最後更新：{data.updated_at || "--"}
            </div>
          </div>
          <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6 }}>
            <StatusDot status={overall} />
            <span style={{ fontSize: 11, color: cfg.color, fontWeight: 600 }}>{cfg.label}</span>
          </div>
        </div>

        {/* 服務狀態列表 */}
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, marginBottom: 20, overflow: "hidden" }}>
          <div style={{ padding: "12px 16px", borderBottom: `1px solid ${C.border}`, display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: C.accent }}>服務狀態</span>
          </div>
          {services.map(svc => (
            <ServiceRow key={svc.name} {...svc} />
          ))}
        </div>

        {/* 今日推送統計 */}
        <div style={{ background: C.surface, border: `1px solid ${C.border}`, borderRadius: 10, marginBottom: 20 }}>
          <div style={{ padding: "12px 16px", borderBottom: `1px solid ${C.border}` }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: C.accent }}>今日推送統計</span>
            <span style={{ fontSize: 11, color: C.muted, marginLeft: 10 }}>
              合計 {data.push_today || 0} 則
              {data.last_push_time && `  ·  最後推送 ${data.last_push_time}`}
            </span>
          </div>
          <div style={{ padding: "14px 16px" }}>
            {pushTypes.length === 0 ? (
              <div style={{ fontSize: 12, color: C.muted }}>今日尚無推送記錄</div>
            ) : (
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                {pushTypes.map(([type, count]) => (
                  <PushStatBadge key={type} type={type} count={count} />
                ))}
              </div>
            )}
          </div>
        </div>

        {/* 說明 */}
        <div style={{ fontSize: 11, color: C.muted, textAlign: "center", lineHeight: 1.8 }}>
          狀態每 60 秒自動更新 · LINE Bot 輸入 <code style={{ color: C.accent }}>/status</code> 查看摘要
        </div>
      </div>
    </div>
  );
}
