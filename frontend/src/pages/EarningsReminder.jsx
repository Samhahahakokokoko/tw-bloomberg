import React, { useEffect, useState } from "react";
import {
  getEarnings, createEarnings, deleteEarnings,
  updateEarningsEps, getLatestEps, triggerEarningsCheck,
} from "../utils/api";
import Card from "../components/Card";
import { Calendar, Bell, Trash2, Plus, RefreshCw, TrendingUp } from "lucide-react";

const USER_ID = "";

// 台股財報公布截止日說明
const PERIOD_HELP = [
  { period: "Q1 (1-3月)", deadline: "5月15日前" },
  { period: "Q2 (4-6月)", deadline: "8月14日前" },
  { period: "Q3 (7-9月)", deadline: "11月14日前" },
  { period: "年報 (全年)", deadline: "隔年3月31日前" },
];

const PERIOD_OPTIONS = [
  "2025Q1", "2025Q2", "2025Q3", "2025Q4",
  "2025Annual", "2024Q4", "2024Annual",
];

function DaysChip({ days }) {
  if (days === null) return null;
  if (days < 0) return <span className="text-terminal-muted text-xs">已過期</span>;
  if (days === 0) return <span className="px-1.5 py-0.5 rounded text-xs bg-terminal-red/20 text-terminal-red font-bold">今天！</span>;
  if (days <= 3) return <span className="px-1.5 py-0.5 rounded text-xs bg-terminal-yellow/20 text-terminal-yellow font-bold">{days}天後</span>;
  return <span className="text-terminal-muted text-xs">{days}天後</span>;
}

export default function EarningsReminder() {
  const [reminders, setReminders]     = useState([]);
  const [showForm, setShowForm]       = useState(false);
  const [form, setForm]               = useState({
    stock_code: "", period: "2025Q2", announce_date: "",
    remind_days_before: 3, line_user_id: "", expected_eps: "",
  });
  const [loading, setLoading]         = useState(false);
  const [epsModal, setEpsModal]       = useState(null);   // { id, stock_code }
  const [epsInput, setEpsInput]       = useState("");
  const [latestEps, setLatestEps]     = useState(null);
  const [latestCode, setLatestCode]   = useState("");
  const [msg, setMsg]                 = useState("");

  const load = () =>
    getEarnings(USER_ID).then(setReminders).catch(console.error);

  useEffect(() => { load(); }, []);

  const flash = (text) => { setMsg(text); setTimeout(() => setMsg(""), 5000); };

  const handleAdd = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      await createEarnings({
        user_id:             USER_ID,
        stock_code:          form.stock_code.trim(),
        period:              form.period,
        announce_date:       form.announce_date,
        remind_days_before:  Number(form.remind_days_before),
        line_user_id:        form.line_user_id,
        expected_eps:        form.expected_eps ? parseFloat(form.expected_eps) : null,
      });
      setForm({ stock_code: "", period: "2025Q2", announce_date: "", remind_days_before: 3, line_user_id: "", expected_eps: "" });
      setShowForm(false);
      load();
      flash("✓ 財報提醒已新增");
    } catch (err) {
      flash("新增失敗：" + (err.response?.data?.detail || err.message));
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (id) => {
    await deleteEarnings(id, USER_ID);
    load();
  };

  const handleUpdateEps = async () => {
    if (!epsModal || !epsInput) return;
    await updateEarningsEps(epsModal.id, parseFloat(epsInput));
    setEpsModal(null);
    setEpsInput("");
    load();
    flash("✓ 實際 EPS 已更新");
  };

  const handleFetchLatestEps = async () => {
    if (!latestCode.trim()) return;
    try {
      const data = await getLatestEps(latestCode.trim());
      setLatestEps(data);
    } catch {
      flash(`查無 ${latestCode} 的 EPS 資料`);
    }
  };

  const upcoming  = reminders.filter(r => r.days_until !== null && r.days_until >= 0 && r.days_until <= 7);
  const all       = reminders;

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest flex items-center gap-2">
          <Calendar size={16} /> 財報提醒
        </h1>
        <div className="flex gap-2">
          <button
            onClick={async () => { await triggerEarningsCheck(); flash("✓ 提醒推播已觸發"); }}
            className="px-3 py-1.5 border border-terminal-border text-terminal-muted text-xs rounded hover:text-terminal-text flex items-center gap-1"
          >
            <Bell size={12} /> 立即推播
          </button>
          <button
            onClick={() => setShowForm(!showForm)}
            className="px-3 py-1.5 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-xs rounded hover:bg-terminal-accent/30 flex items-center gap-1"
          >
            <Plus size={12} /> 新增提醒
          </button>
        </div>
      </div>

      {msg && (
        <div className="text-terminal-accent text-xs px-3 py-2 bg-terminal-accent/10 border border-terminal-accent/30 rounded">
          {msg}
        </div>
      )}

      {/* 即將公布 */}
      {upcoming.length > 0 && (
        <Card title="⚡ 近7天即將公布">
          <div className="space-y-2">
            {upcoming.map(r => (
              <div key={r.id} className="flex items-center justify-between border-b border-terminal-border/30 pb-2">
                <div className="flex items-center gap-3">
                  <Bell size={14} className="text-terminal-yellow" />
                  <div>
                    <span className="text-terminal-accent font-bold">{r.stock_code}</span>
                    <span className="text-terminal-muted text-xs ml-2">{r.stock_name}</span>
                    <span className="text-terminal-text text-xs ml-2">{r.period}</span>
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <span className="text-terminal-muted text-xs">{r.announce_date}</span>
                  <DaysChip days={r.days_until} />
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* 新增表單 */}
      {showForm && (
        <Card title="新增財報提醒">
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
                <label className="text-terminal-muted text-xs">財報期別</label>
                <select
                  value={form.period}
                  onChange={e => setForm({ ...form, period: e.target.value })}
                  className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
                >
                  {PERIOD_OPTIONS.map(p => <option key={p} value={p}>{p}</option>)}
                  <option value="">自訂</option>
                </select>
              </div>
              <div>
                <label className="text-terminal-muted text-xs">預計公布日（留空自動估算）</label>
                <input
                  value={form.announce_date}
                  onChange={e => setForm({ ...form, announce_date: e.target.value })}
                  placeholder="YYYY-MM-DD"
                  type="date"
                  className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
                />
              </div>
              <div>
                <label className="text-terminal-muted text-xs">提前幾天提醒</label>
                <input
                  value={form.remind_days_before}
                  onChange={e => setForm({ ...form, remind_days_before: e.target.value })}
                  type="number" min={0} max={30}
                  className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
                />
              </div>
              <div>
                <label className="text-terminal-muted text-xs">市場預期 EPS（選填）</label>
                <input
                  value={form.expected_eps}
                  onChange={e => setForm({ ...form, expected_eps: e.target.value })}
                  placeholder="例：5.5"
                  type="number" step="0.01"
                  className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
                />
              </div>
              <div>
                <label className="text-terminal-muted text-xs">LINE User ID（推播用，選填）</label>
                <input
                  value={form.line_user_id}
                  onChange={e => setForm({ ...form, line_user_id: e.target.value })}
                  placeholder="U..."
                  className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
                />
              </div>
            </div>

            {/* 公布時程參考 */}
            <div className="bg-terminal-surface/50 rounded p-3 border border-terminal-border/30">
              <div className="text-terminal-muted text-xs mb-2">台股財報公布時程（參考）</div>
              <div className="grid grid-cols-2 gap-1">
                {PERIOD_HELP.map(({ period, deadline }) => (
                  <div key={period} className="flex justify-between text-xs">
                    <span className="text-terminal-text">{period}</span>
                    <span className="text-terminal-muted">{deadline}</span>
                  </div>
                ))}
              </div>
            </div>

            <div className="flex gap-2 justify-end">
              <button type="button" onClick={() => setShowForm(false)} className="px-3 py-1.5 text-xs text-terminal-muted border border-terminal-border rounded">取消</button>
              <button type="submit" disabled={loading} className="px-4 py-1.5 text-xs bg-terminal-accent/20 border border-terminal-accent text-terminal-accent rounded hover:bg-terminal-accent/30">
                {loading ? "新增中..." : "確認新增"}
              </button>
            </div>
          </form>
        </Card>
      )}

      {/* 查詢最新 EPS */}
      <Card title="查詢最新 EPS（TWSE）">
        <div className="flex gap-2 mb-3">
          <input
            value={latestCode}
            onChange={e => setLatestCode(e.target.value)}
            onKeyDown={e => e.key === "Enter" && handleFetchLatestEps()}
            placeholder="輸入股票代碼"
            className="flex-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
          />
          <button
            onClick={handleFetchLatestEps}
            className="px-3 py-1.5 bg-terminal-surface border border-terminal-border text-terminal-text text-xs rounded hover:bg-terminal-border/50 flex items-center gap-1"
          >
            <TrendingUp size={12} /> 查詢
          </button>
        </div>
        {latestEps && (
          <div className="grid grid-cols-3 gap-3 text-center">
            {[
              { label: "年度/季別", value: `${latestEps.year} ${latestEps.season}` },
              { label: "基本 EPS", value: latestEps.eps !== null ? `${latestEps.eps}` : "—" },
              { label: "公司", value: latestEps.stock_name || "—" },
            ].map(({ label, value }) => (
              <div key={label} className="bg-terminal-surface/50 rounded p-2">
                <div className="text-terminal-muted text-xs">{label}</div>
                <div className="text-terminal-text font-bold mt-1">{value}</div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* 所有提醒列表 */}
      <Card title={`所有財報提醒 (${all.length})`}>
        {all.length === 0 ? (
          <div className="text-terminal-muted text-sm text-center py-6">尚無提醒 — 點右上角新增</div>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-terminal-muted border-b border-terminal-border">
                {["代碼", "名稱", "期別", "公布日", "距今", "預期EPS", "實際EPS", ""].map(h => (
                  <th key={h} className="text-left py-1.5 pr-3">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {all.map(r => (
                <tr key={r.id} className={`border-b border-terminal-border/30 hover:bg-terminal-border/20 ${r.days_until !== null && r.days_until <= 3 && r.days_until >= 0 ? "bg-terminal-yellow/5" : ""}`}>
                  <td className="py-2 pr-3 text-terminal-accent font-bold">{r.stock_code}</td>
                  <td className="py-2 pr-3">{r.stock_name || "—"}</td>
                  <td className="py-2 pr-3 text-terminal-text">{r.period || "—"}</td>
                  <td className="py-2 pr-3 font-mono text-terminal-muted">{r.announce_date || "—"}</td>
                  <td className="py-2 pr-3"><DaysChip days={r.days_until} /></td>
                  <td className="py-2 pr-3 text-terminal-muted">
                    {r.expected_eps !== null ? r.expected_eps : "—"}
                  </td>
                  <td className="py-2 pr-3">
                    {r.actual_eps !== null ? (
                      <span className="text-terminal-green font-bold">{r.actual_eps}</span>
                    ) : (
                      <button
                        onClick={() => { setEpsModal(r); setEpsInput(""); }}
                        className="text-terminal-accent text-xs underline hover:text-terminal-text"
                      >
                        填入
                      </button>
                    )}
                  </td>
                  <td className="py-2">
                    <button onClick={() => handleDelete(r.id)} className="text-terminal-red hover:text-red-400">
                      <Trash2 size={12} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {/* 填入實際 EPS Modal */}
      {epsModal && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={() => setEpsModal(null)}>
          <div className="bg-terminal-surface border border-terminal-border rounded-lg p-6 w-80" onClick={e => e.stopPropagation()}>
            <div className="text-terminal-accent font-bold mb-3">填入實際 EPS</div>
            <div className="text-terminal-muted text-sm mb-3">
              {epsModal.stock_code} {epsModal.period}
            </div>
            <input
              value={epsInput}
              onChange={e => setEpsInput(e.target.value)}
              placeholder="例：4.2"
              type="number" step="0.01"
              autoFocus
              className="w-full bg-terminal-bg border border-terminal-border rounded px-3 py-2 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
            />
            <div className="flex gap-2 mt-4 justify-end">
              <button onClick={() => setEpsModal(null)} className="px-3 py-1.5 text-xs text-terminal-muted border border-terminal-border rounded">取消</button>
              <button onClick={handleUpdateEps} className="px-4 py-1.5 text-xs bg-terminal-green/20 border border-terminal-green text-terminal-green rounded hover:bg-terminal-green/30">確認</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
