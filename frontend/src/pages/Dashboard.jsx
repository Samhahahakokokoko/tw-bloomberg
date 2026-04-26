import React, { useEffect, useState } from "react";
import { getMarketOverview, getPortfolio, getNews } from "../utils/api";
import Card from "../components/Card";
import PriceTag from "../components/PriceTag";

export default function Dashboard() {
  const [market, setMarket] = useState(null);
  const [portfolio, setPortfolio] = useState([]);
  const [news, setNews] = useState([]);
  const [time, setTime] = useState(new Date());

  useEffect(() => {
    const load = async () => {
      const [m, p, n] = await Promise.allSettled([
        getMarketOverview(),
        getPortfolio(),
        getNews({ limit: 6 }),
      ]);
      if (m.status === "fulfilled") setMarket(m.value);
      if (p.status === "fulfilled") setPortfolio(p.value);
      if (n.status === "fulfilled") setNews(n.value);
    };
    load();
    const tick = setInterval(() => setTime(new Date()), 1000);
    const refresh = setInterval(load, 60000);
    return () => { clearInterval(tick); clearInterval(refresh); };
  }, []);

  const totalMV = portfolio.reduce((s, h) => s + h.market_value, 0);
  const totalPnL = portfolio.reduce((s, h) => s + h.pnl, 0);

  return (
    <div className="p-4 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest">◈ MARKET DASHBOARD</h1>
        <div className="text-terminal-muted text-xs font-mono">{time.toLocaleString("zh-TW")}</div>
      </div>

      {/* Market Overview */}
      <div className="grid grid-cols-3 gap-3">
        <Card title="加權指數 TAIEX">
          {market ? (
            <div>
              <div className="text-2xl font-bold text-terminal-text">
                {market.value?.toLocaleString()}
              </div>
              <PriceTag value={market.change || 0} pct={market.change_pct} />
            </div>
          ) : (
            <div className="text-terminal-muted text-sm">載入中...</div>
          )}
        </Card>

        <Card title="總市值">
          <div className="text-2xl font-bold text-terminal-text">
            {totalMV ? `$${(totalMV / 10000).toFixed(1)}萬` : "—"}
          </div>
          {totalPnL !== 0 && <PriceTag value={totalPnL} />}
        </Card>

        <Card title="持股數量">
          <div className="text-2xl font-bold text-terminal-text">{portfolio.length}</div>
          <div className="text-terminal-muted text-xs mt-1">檔股票</div>
        </Card>
      </div>

      {/* Holdings Summary */}
      <Card title="庫存概覽">
        {portfolio.length === 0 ? (
          <div className="text-terminal-muted text-sm">尚無持股 — 前往 PORTFOLIO 新增</div>
        ) : (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-terminal-muted border-b border-terminal-border">
                <th className="text-left py-1">代碼</th>
                <th className="text-left py-1">名稱</th>
                <th className="text-right py-1">現價</th>
                <th className="text-right py-1">市值</th>
                <th className="text-right py-1">損益</th>
              </tr>
            </thead>
            <tbody>
              {portfolio.slice(0, 8).map((h) => (
                <tr key={h.id} className="border-b border-terminal-border/30 hover:bg-terminal-border/20">
                  <td className="py-1.5 text-terminal-accent">{h.stock_code}</td>
                  <td className="py-1.5">{h.stock_name}</td>
                  <td className="py-1.5 text-right">{h.current_price}</td>
                  <td className="py-1.5 text-right">{h.market_value.toLocaleString()}</td>
                  <td className="py-1.5 text-right">
                    <PriceTag value={h.pnl} pct={h.pnl_pct} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {/* News */}
      <Card title="最新新聞">
        <div className="space-y-2">
          {news.map((n, i) => (
            <div key={i} className="flex items-start gap-2 border-b border-terminal-border/30 pb-2">
              <span className={`text-xs px-1 rounded flex-shrink-0 ${
                n.sentiment === "positive" ? "bg-terminal-green/20 text-terminal-green" :
                n.sentiment === "negative" ? "bg-terminal-red/20 text-terminal-red" :
                "bg-terminal-yellow/20 text-terminal-yellow"
              }`}>
                {n.sentiment === "positive" ? "+" : n.sentiment === "negative" ? "−" : "○"}
              </span>
              <div>
                <div className="text-xs text-terminal-text leading-snug">{n.title}</div>
                <div className="text-terminal-muted text-xs mt-0.5">{n.source}</div>
              </div>
            </div>
          ))}
          {news.length === 0 && <div className="text-terminal-muted text-sm">尚無新聞資料</div>}
        </div>
      </Card>
    </div>
  );
}
