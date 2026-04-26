import React, { useEffect, useState } from "react";
import { getAlerts, createAlert, deleteAlert } from "../utils/api";
import Card from "../components/Card";

export default function Alerts() {
  const [alerts, setAlerts] = useState([]);
  const [form, setForm] = useState({
    stock_code: "",
    alert_type: "price_above",
    threshold: "",
    line_user_id: "",
  });

  const load = () => getAlerts().then(setAlerts).catch(console.error);
  useEffect(() => { load(); }, []);

  const handleCreate = async (e) => {
    e.preventDefault();
    await createAlert({ ...form, threshold: parseFloat(form.threshold) });
    setForm({ stock_code: "", alert_type: "price_above", threshold: "", line_user_id: "" });
    load();
  };

  return (
    <div className="p-4 space-y-4">
      <div className="border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest">◈ PRICE ALERTS</h1>
      </div>

      {/* Active Alerts */}
      <Card title="啟用中的警報">
        {alerts.length === 0 ? (
          <div className="text-terminal-muted text-sm">尚無警報</div>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-terminal-muted border-b border-terminal-border">
                {["代碼", "類型", "門檻價", "LINE 用戶", ""].map((h) => (
                  <th key={h} className="text-left py-1 pr-4">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {alerts.map((a) => (
                <tr key={a.id} className="border-b border-terminal-border/30 hover:bg-terminal-border/20">
                  <td className="py-2 pr-4 text-terminal-accent">{a.stock_code}</td>
                  <td className="py-2 pr-4">
                    <span className={a.alert_type === "price_above" ? "text-terminal-green" : "text-terminal-red"}>
                      {a.alert_type === "price_above" ? "▲ 突破" : "▼ 跌破"}
                    </span>
                  </td>
                  <td className="py-2 pr-4 text-terminal-yellow">{a.threshold}</td>
                  <td className="py-2 pr-4 text-terminal-muted">{a.line_user_id || "—"}</td>
                  <td className="py-2">
                    <button
                      onClick={() => deleteAlert(a.id).then(load)}
                      className="text-terminal-red hover:text-red-400 text-xs"
                    >✕ 刪除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {/* Create Alert */}
      <Card title="新增警報">
        <form onSubmit={handleCreate} className="grid grid-cols-2 gap-3">
          <div>
            <label className="text-terminal-muted text-xs">股票代碼</label>
            <input
              value={form.stock_code}
              onChange={(e) => setForm({ ...form, stock_code: e.target.value })}
              placeholder="2330"
              required
              className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
            />
          </div>
          <div>
            <label className="text-terminal-muted text-xs">警報類型</label>
            <select
              value={form.alert_type}
              onChange={(e) => setForm({ ...form, alert_type: e.target.value })}
              className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
            >
              <option value="price_above">突破價格</option>
              <option value="price_below">跌破價格</option>
            </select>
          </div>
          <div>
            <label className="text-terminal-muted text-xs">門檻價格</label>
            <input
              value={form.threshold}
              onChange={(e) => setForm({ ...form, threshold: e.target.value })}
              placeholder="850.0"
              type="number"
              step="0.01"
              required
              className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
            />
          </div>
          <div>
            <label className="text-terminal-muted text-xs">LINE User ID (選填)</label>
            <input
              value={form.line_user_id}
              onChange={(e) => setForm({ ...form, line_user_id: e.target.value })}
              placeholder="U..."
              className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
            />
          </div>
          <div className="col-span-2">
            <button
              type="submit"
              className="px-6 py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-sm rounded hover:bg-terminal-accent/30 transition-colors"
            >
              + 新增警報
            </button>
          </div>
        </form>
      </Card>
    </div>
  );
}
