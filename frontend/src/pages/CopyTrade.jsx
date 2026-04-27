import React, { useEffect, useState } from "react";
import {
  publishPortfolio, viewSharedPortfolio, followTrader, unfollowTrader, getFollowing,
} from "../utils/api";
import Card from "../components/Card";
import PriceTag from "../components/PriceTag";
import { Users, Share2, Eye, UserPlus, UserMinus } from "lucide-react";

const USER_ID = "";

export default function CopyTrade() {
  const [myShare, setMyShare]       = useState(null);
  const [following, setFollowing]   = useState([]);
  const [viewCode, setViewCode]     = useState("");
  const [viewData, setViewData]     = useState(null);
  const [displayName, setDisplay]   = useState("");
  const [description, setDesc]      = useState("");
  const [followCode, setFollowCode] = useState("");
  const [loading, setLoading]       = useState(false);
  const [viewLoading, setViewLoad]  = useState(false);
  const [msg, setMsg]               = useState("");

  const loadFollowing = () =>
    getFollowing(USER_ID).then(setFollowing).catch(console.error);

  useEffect(() => { loadFollowing(); }, []);

  const flash = (text) => { setMsg(text); setTimeout(() => setMsg(""), 5000); };

  const handlePublish = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      const r = await publishPortfolio({ user_id: USER_ID, display_name: displayName, description });
      setMyShare(r);
      flash(`✓ 分享碼：${r.share_code}`);
    } catch (e) {
      flash("發布失敗：" + (e.response?.data?.detail || e.message));
    } finally {
      setLoading(false);
    }
  };

  const handleView = async () => {
    if (!viewCode.trim()) return;
    setViewLoad(true);
    setViewData(null);
    try {
      const r = await viewSharedPortfolio(viewCode.trim().toUpperCase());
      setViewData(r);
    } catch (e) {
      flash("找不到此分享碼，或持倉未公開。");
    } finally {
      setViewLoad(false);
    }
  };

  const handleFollow = async () => {
    if (!followCode.trim()) return;
    try {
      const r = await followTrader({ follower_id: USER_ID, share_code: followCode.trim().toUpperCase() });
      flash(`✓ 已追蹤 ${r.display_name}`);
      setFollowCode("");
      loadFollowing();
    } catch (e) {
      flash("追蹤失敗：" + (e.response?.data?.detail || e.message));
    }
  };

  const handleUnfollow = async (leaderId) => {
    await unfollowTrader({ follower_id: USER_ID, leader_id: leaderId });
    loadFollowing();
  };

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest flex items-center gap-2">
          <Users size={16} /> 跟單功能
        </h1>
      </div>

      {msg && (
        <div className="text-terminal-accent text-xs px-3 py-2 bg-terminal-accent/10 border border-terminal-accent/30 rounded">
          {msg}
        </div>
      )}

      <div className="grid grid-cols-2 gap-4">
        {/* 發佈自己的組合 */}
        <Card title="發佈我的投資組合">
          <p className="text-terminal-muted text-xs mb-3">發佈後取得分享碼，讓其他人查看你的持倉與績效。</p>
          <form onSubmit={handlePublish} className="space-y-3">
            <div>
              <label className="text-terminal-muted text-xs">顯示名稱</label>
              <input
                value={displayName}
                onChange={e => setDisplay(e.target.value)}
                placeholder="例：台積電達人"
                className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
              />
            </div>
            <div>
              <label className="text-terminal-muted text-xs">簡介</label>
              <input
                value={description}
                onChange={e => setDesc(e.target.value)}
                placeholder="例：專注半導體/AI選股"
                className="w-full mt-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent"
              />
            </div>
            <button
              type="submit"
              disabled={loading}
              className="w-full py-2 bg-terminal-accent/20 border border-terminal-accent text-terminal-accent text-sm rounded hover:bg-terminal-accent/30 flex items-center justify-center gap-2"
            >
              <Share2 size={14} />
              {loading ? "發布中..." : "發布/更新分享碼"}
            </button>
          </form>

          {myShare && (
            <div className="mt-3 p-3 bg-terminal-surface rounded border border-terminal-accent/30">
              <div className="text-terminal-muted text-xs">你的分享碼</div>
              <div className="text-terminal-accent text-2xl font-bold font-mono mt-1">{myShare.share_code}</div>
              <div className="text-terminal-muted text-xs mt-1">把此碼給朋友即可查看你的持倉</div>
            </div>
          )}
        </Card>

        {/* 追蹤別人 */}
        <Card title="追蹤他人投資組合">
          <p className="text-terminal-muted text-xs mb-3">輸入分享碼查看他人持倉，或追蹤後持續關注。</p>
          <div className="flex gap-2">
            <input
              value={viewCode}
              onChange={e => setViewCode(e.target.value)}
              onKeyDown={e => e.key === "Enter" && handleView()}
              placeholder="輸入分享碼 (8碼)"
              className="flex-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-accent uppercase"
            />
            <button
              onClick={handleView}
              disabled={viewLoading}
              className="px-3 py-1.5 bg-terminal-surface border border-terminal-border text-terminal-text text-xs rounded hover:bg-terminal-border/50 flex items-center gap-1"
            >
              <Eye size={12} /> {viewLoading ? "..." : "查看"}
            </button>
          </div>

          <div className="flex gap-2 mt-2">
            <input
              value={followCode}
              onChange={e => setFollowCode(e.target.value)}
              placeholder="追蹤分享碼"
              className="flex-1 bg-terminal-bg border border-terminal-border rounded px-2 py-1.5 text-sm font-mono text-terminal-text focus:outline-none focus:border-terminal-green uppercase"
            />
            <button
              onClick={handleFollow}
              className="px-3 py-1.5 bg-terminal-green/10 border border-terminal-green text-terminal-green text-xs rounded hover:bg-terminal-green/20 flex items-center gap-1"
            >
              <UserPlus size={12} /> 追蹤
            </button>
          </div>

          {/* 我的追蹤清單 */}
          {following.length > 0 && (
            <div className="mt-3">
              <div className="text-terminal-muted text-xs mb-2">我的追蹤 ({following.length})</div>
              <div className="space-y-1.5">
                {following.map(f => (
                  <div key={f.leader_id} className="flex items-center justify-between border-b border-terminal-border/30 pb-1.5">
                    <div>
                      <span className="text-terminal-accent text-sm">{f.display_name}</span>
                      {f.total_pnl_pct !== undefined && (
                        <span className={`ml-2 text-xs ${f.total_pnl_pct >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                          {f.total_pnl_pct >= 0 ? "+" : ""}{f.total_pnl_pct}%
                        </span>
                      )}
                    </div>
                    <button
                      onClick={() => handleUnfollow(f.leader_id)}
                      className="text-terminal-red hover:text-red-400 text-xs flex items-center gap-1"
                    >
                      <UserMinus size={11} /> 取消
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
      </div>

      {/* 查看結果 */}
      {viewData && (
        <Card title={`${viewData.display_name} 的投資組合`}>
          <div className="flex items-center justify-between mb-3">
            <div className="text-terminal-muted text-xs">{viewData.description}</div>
            <div className="text-right">
              <div className="text-terminal-muted text-xs">總市值</div>
              <div className="text-terminal-text font-bold">{viewData.total_mv?.toLocaleString()}</div>
              <div className={`text-sm font-bold ${viewData.total_pnl_pct >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                {viewData.total_pnl_pct >= 0 ? "+" : ""}{viewData.total_pnl_pct}%
              </div>
            </div>
          </div>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-terminal-muted border-b border-terminal-border">
                {["代碼", "名稱", "股數", "成本", "現價", "損益%"].map(h => (
                  <th key={h} className="text-left py-1.5 pr-3">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {viewData.holdings?.map(h => (
                <tr key={h.stock_code} className="border-b border-terminal-border/30 hover:bg-terminal-border/20">
                  <td className="py-1.5 pr-3 text-terminal-accent font-bold">{h.stock_code}</td>
                  <td className="py-1.5 pr-3">{h.stock_name}</td>
                  <td className="py-1.5 pr-3">{h.shares?.toLocaleString()}</td>
                  <td className="py-1.5 pr-3">{h.cost_price}</td>
                  <td className="py-1.5 pr-3">{h.current_price}</td>
                  <td className={`py-1.5 pr-3 font-bold ${h.pnl_pct >= 0 ? "text-terminal-green" : "text-terminal-red"}`}>
                    {h.pnl_pct >= 0 ? "+" : ""}{h.pnl_pct}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-3 flex justify-end">
            <button
              onClick={() => { setFollowCode(viewData.share_code); }}
              className="px-3 py-1.5 bg-terminal-green/10 border border-terminal-green text-terminal-green text-xs rounded hover:bg-terminal-green/20 flex items-center gap-1"
            >
              <UserPlus size={12} /> 追蹤此投資人
            </button>
          </div>
        </Card>
      )}
    </div>
  );
}
