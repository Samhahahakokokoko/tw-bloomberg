import React, { useEffect, useState } from "react";
import { getWatchlist, addWatchlist, deleteWatchlist } from "../utils/api";
import Card from "../components/Card";
import PriceTag from "../components/PriceTag";
import { Star, Trash2, Plus, AlertTriangle, TrendingUp } from "lucide-react";

const USER_ID = "";

export default function Watchlist() {
  const [items, setItems]       = useState([]);
  const [loading, setLoading]   = useState(false);
  const [form, setForm]         = useState({
    stock_code: "", target_price: "", stop_loss: "", note: "",
  });
  const [adding, setAdding]     = useState(false);
  const [showForm, setShowForm] = useState(false);

  const load = () => {
    setLoading(true);
    getWatchlist(USER_ID).then(setItems).catch(console.error).finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, []);

  const handleAdd = async (e) => {
    e.preventDefault();
    if (!form.stock_code.trim()) return;
    setAdding(true);
    try {
      await addWatchlist({
        user_id:      USER_ID,
        stock_code:   form.stock_code.trim(),
        target_price: form.target_price ? parseFloat(form.target_price) : null,
        stop_loss:    form.stop_loss    ? parseFloat(form.stop_loss)    : null,
        note:         form.note,
      });
      setForm({ stock_code: "", target_price: "", stop_loss: "", note: "" });
      setShowForm(false);
      load();
    } catch (e) {
      alert("新增失敗：" + (e.response?.data?.detail || e.message));
    } finally {
      setAdding(false);
    }
  };

  const handleDelete = async (id) => {
    await deleteWatchlist(id, USER_ID);
    load();
  };

  const triggered  = items.filter(i => i.sl_triggered || i.tp_triggered);
  const normal     = items.filter(i => !i.sl_triggered && !i.tp_triggered);

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest flex items-center gap-2">
          <Star size={16} /> 自選股清單
        </h1>
        <button
          onClick={() => setShowForm(!showForm)}
          className="flex items-center gap-1 px-3 py-1.5 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-xs rounded hover:bg-terminal-accent/30"
        >
          <Plus size={12} /> 新增自選
        </button>
      </div>

      {/* 新增表單 */}
      {showForm && (
        <Card title="新增自選股">
          <form onSubmit={handleAdd} className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-terminal-muted text-xs">股票代碼 *</label>
                <input
                  value={form.stock_code}
                  onChange={e => setForm({ ...form, stock_code: e.target.value })}
                  placeholder="例：2330"
                  required
                  className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
                />
              </div>
              <div>
                <label className="text-terminal-muted text-xs">目標價</label>
                <input
                  value={form.target_price}
                  onChange={e => setForm({ ...form, target_price: e.target.value })}
                  placeholder="例：1000"
                  type="number" step="0.01"
                  className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
                />
              </div>
              <div>
                <label className="text-terminal-muted text-xs">停損價</label>
                <input
                  value={form.stop_loss}
                  onChange={e => setForm({ ...form, stop_loss: e.target.value })}
                  placeholder="例：700"
                  type="number" step="0.01"
                  className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
                />
              </div>
              <div>
                <label className="text-terminal-muted text-xs">備註</label>
                <input
                  value={form.note}
                  onChange={e => setForm({ ...form, note: e.target.value })}
                  placeholder="自訂備註"
                  className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
                />
              </div>
            </div>
            <div className="flex gap-2 justify-end">
              <button type="button" onClick={() => setShowForm(false)} className="px-3 py-1.5 text-xs text-terminal-muted border border-terminal-border rounded">取消</button>
              <button type="submit" disabled={adding} className="px-4 py-1.5 text-xs bg-terminal-accent/20 border border-terminal-accent text-terminal-accent rounded hover:bg-terminal-accent/30">
                {adding ? "新增中..." : "確認新增"}
              </button>
            </div>
          </form>
        </Card>
      )}

      {/* 警報觸發列 */}
      {triggered.length > 0 && (
        <Card title="⚠️ 停損停利觸發">
          <div className="space-y-2">
            {triggered.map(item => (
              <div key={item.id} className={`flex items-center justify-between p-2 rounded border ${
                item.sl_triggered ? "border-terminal-red/50 bg-terminal-red/10" : "border-terminal-green/50 bg-terminal-green/10"
              }`}>
                <div className="flex items-center gap-3">
                  <AlertTriangle size={14} className={item.sl_triggered ? "text-terminal-red" : "text-terminal-green"} />
                  <div>
                    <span className="text-terminal-accent text-sm font-bold">{item.stock_code}</span>
                    <span className="text-terminal-muted text-xs ml-2">{item.stock_name}</span>
                  </div>
                  <div className="text-xs">
                    {item.sl_triggered && <span className="text-terminal-red">觸及停損 {item.stop_loss}</span>}
                    {item.tp_triggered && <span className="text-terminal-green">觸及目標 {item.target_price}</span>}
                  </div>
                </div>
                <div className="text-right">
                  <div className="text-terminal-text font-bold">{item.current_price}</div>
                  <PriceTag value={item.change} pct={item.change_pct} />
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* 自選股主表 */}
      <Card title={`自選股 (${items.length} 檔)`}>
        {loading && items.length === 0 ? (
          <div className="text-terminal-muted text-sm text-center py-8">載入中...</div>
        ) : items.length === 0 ? (
          <div className="text-terminal-muted text-sm text-center py-8">尚無自選股 — 點選右上「新增自選」</div>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-terminal-muted border-b border-terminal-border">
                {["代碼", "名稱", "現價", "漲跌", "目標價", "停損價", "備註", ""].map(h => (
                  <th key={h} className="text-left py-1.5 pr-2">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map(item => (
                <tr key={item.id} className={`border-b border-terminal-border/30 hover:bg-terminal-border/20 ${
                  item.sl_triggered ? "bg-terminal-red/5" : item.tp_triggered ? "bg-terminal-green/5" : ""
                }`}>
                  <td className="py-2 pr-2 text-terminal-accent font-bold">{item.stock_code}</td>
                  <td className="py-2 pr-2">{item.stock_name || "—"}</td>
                  <td className="py-2 pr-2 font-mono">{item.current_price || "—"}</td>
                  <td className="py-2 pr-2">
                    {item.current_price ? <PriceTag value={item.change} pct={item.change_pct} /> : "—"}
                  </td>
                  <td className="py-2 pr-2">
                    {item.target_price ? (
                      <span className="text-terminal-green">{item.target_price}</span>
                    ) : <span className="text-terminal-muted">—</span>}
                  </td>
                  <td className="py-2 pr-2">
                    {item.stop_loss ? (
                      <span className="text-terminal-red">{item.stop_loss}</span>
                    ) : <span className="text-terminal-muted">—</span>}
                  </td>
                  <td className="py-2 pr-2 text-terminal-muted max-w-24 truncate">{item.note || "—"}</td>
                  <td className="py-2">
                    <button onClick={() => handleDelete(item.id)} className="text-terminal-red hover:text-red-400">
                      <Trash2 size={12} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
