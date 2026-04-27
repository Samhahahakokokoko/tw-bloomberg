import React, { useEffect, useState } from "react";
import { getAlerts, createAlert, deleteAlert } from "../utils/api";
import Card from "../components/Card";

const ALERT_TYPES = [
  { value: "price_above",      label: "▲ 突破價格（停利）", color: "text-terminal-green" },
  { value: "price_below",      label: "▼ 跌破價格（停損）", color: "text-terminal-red" },
  { value: "change_pct_above", label: "📈 當日漲幅 % 達到",  color: "text-terminal-green" },
  { value: "change_pct_below", label: "📉 當日跌幅 % 達到",  color: "text-terminal-red" },
];

const TYPE_LABEL = Object.fromEntries(ALERT_TYPES.map(t => [t.value, t.label]));
const TYPE_COLOR = Object.fromEntries(ALERT_TYPES.map(t => [t.value, t.color]));

function thresholdLabel(type, val) {
  if (type.includes("pct")) return `${val >= 0 ? "+" : ""}${val}%`;
  return `${val} 元`;
}

export default function Alerts() {
  const [alerts, setAlerts] = useState([]);
  const [form, setForm] = useState({
    stock_code:   "",
    alert_type:   "price_above",
    threshold:    "",
    line_user_id: "",
  });
  const [loading, setLoading] = useState(false);

  const load = () => getAlerts().then(setAlerts).catch(console.error);
  useEffect(() => { load(); }, []);

  const handleCreate = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      await createAlert({ ...form, threshold: parseFloat(form.threshold) });
      setForm({ stock_code: "", alert_type: "price_above", threshold: "", line_user_id: "" });
      load();
    } finally {
      setLoading(false);
    }
  };

  // 依類型分組
  const stopLossAlerts = alerts.filter(a => a.alert_type === "price_below" || a.alert_type === "change_pct_below");
  const takeProfitAlerts = alerts.filter(a => a.alert_type === "price_above" || a.alert_type === "change_pct_above");

  return (
    <div className="p-4 space-y-4">
      <div className="border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest">◈ 股價警報</h1>
        <p className="text-terminal-muted text-xs mt-1">
          設定停損停利、漲跌幅警報，觸發時推播 LINE 通知
        </p>
      </div>

      {/* 統計列 */}
      <div className="grid grid-cols-3 gap-3">
        <Card title="停利警報">
          <div className="text-2xl font-bold text-terminal-green">{takeProfitAlerts.length}</div>
          <div className="text-terminal-muted text-xs mt-1">啟用中</div>
        </Card>
        <Card title="停損警報">
          <div className="text-2xl font-bold text-terminal-red">{stopLossAlerts.length}</div>
          <div className="text-terminal-muted text-xs mt-1">啟用中</div>
        </Card>
        <Card title="總計">
          <div className="text-2xl font-bold text-terminal-text">{alerts.length}</div>
          <div className="text-terminal-muted text-xs mt-1">個警報</div>
        </Card>
      </div>

      {/* 警報清單 */}
      <Card title="啟用中的警報">
        {alerts.length === 0 ? (
          <div className="text-terminal-muted text-sm">尚無警報 — 於下方新增</div>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-terminal-muted border-b border-terminal-border">
                {["代碼", "類型", "門檻", "LINE 用戶", ""].map(h => (
                  <th key={h} className="text-left py-1.5 pr-4">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {alerts.map(a => (
                <tr key={a.id} className="border-b border-terminal-border/30 hover:bg-terminal-border/20">
                  <td className="py-2 pr-4 text-terminal-accent font-bold">{a.stock_code}</td>
                  <td className={`py-2 pr-4 ${TYPE_COLOR[a.alert_type] || "text-terminal-text"}`}>
                    {TYPE_LABEL[a.alert_type] || a.alert_type}
                  </td>
                  <td className="py-2 pr-4 text-terminal-yellow font-mono font-bold">
                    {thresholdLabel(a.alert_type, a.threshold)}
                  </td>
                  <td className="py-2 pr-4 text-terminal-muted">{a.line_user_id || "—"}</td>
                  <td className="py-2">
                    <button
                      onClick={() => deleteAlert(a.id).then(load)}
                      className="text-terminal-red hover:text-red-400 text-xs"
                    >✕</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {/* 新增警報 */}
      <Card title="新增警報">
        <form onSubmit={handleCreate} className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-terminal-muted text-xs">股票代碼 *</label>
              <input
                value={form.stock_code}
                onChange={e => setForm({ ...form, stock_code: e.target.value })}
                placeholder="例：2330"
                required
                className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
              />
            </div>
            <div>
              <label className="text-terminal-muted text-xs">警報類型 *</label>
              <select
                value={form.alert_type}
                onChange={e => setForm({ ...form, alert_type: e.target.value })}
                className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
              >
                {ALERT_TYPES.map(t => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-terminal-muted text-xs">
                {form.alert_type.includes("pct") ? "漲跌幅 %（負數為跌）" : "門檻價格（元）"} *
              </label>
              <input
                value={form.threshold}
                onChange={e => setForm({ ...form, threshold: e.target.value })}
                placeholder={form.alert_type.includes("pct") ? "例：-5（跌5%）" : "例：850"}
                type="number"
                step="0.01"
                required
                className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
              />
            </div>
            <div>
              <label className="text-terminal-muted text-xs">LINE User ID（觸發時推播）</label>
              <input
                value={form.line_user_id}
                onChange={e => setForm({ ...form, line_user_id: e.target.value })}
                placeholder="U... （選填）"
                className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
              />
            </div>
          </div>

          {/* 快速停損停利範本 */}
          <div className="border-t border-terminal-border/50 pt-3">
            <div className="text-terminal-muted text-xs mb-2">快速設定範本</div>
            <div className="flex gap-2 flex-wrap">
              {[
                { label: "停損 -8%",   type: "change_pct_below", val: -8 },
                { label: "停損 -10%",  type: "change_pct_below", val: -10 },
                { label: "停利 +15%",  type: "change_pct_above", val: 15 },
                { label: "停利 +20%",  type: "change_pct_above", val: 20 },
              ].map(({ label, type, val }) => (
                <button
                  key={label}
                  type="button"
                  onClick={() => setForm({ ...form, alert_type: type, threshold: String(val) })}
                  className={`px-2 py-1 text-xs rounded border ${
                    val < 0
                      ? "border-terminal-red/50 text-terminal-red hover:bg-terminal-red/10"
                      : "border-terminal-green/50 text-terminal-green hover:bg-terminal-green/10"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          <button
            type="submit"
            disabled={loading}
            className="px-6 py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-sm rounded hover:bg-terminal-accent/30 transition-colors"
          >
            {loading ? "新增中..." : "+ 新增警報"}
          </button>
        </form>
      </Card>
    </div>
  );
}
