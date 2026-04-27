import React from "react";
import { Routes, Route, NavLink } from "react-router-dom";
import {
  BarChart2, TrendingUp, BookOpen, Bell, Newspaper, Activity,
  Star, Cpu, Heart, Map, Trophy, Users,
} from "lucide-react";
import Dashboard    from "./pages/Dashboard";
import Quote        from "./pages/Quote";
import Portfolio    from "./pages/Portfolio";
import Alerts       from "./pages/Alerts";
import News         from "./pages/News";
import Backtest     from "./pages/Backtest";
import Watchlist    from "./pages/Watchlist";
import ChipTracker  from "./pages/ChipTracker";
import StockHealth  from "./pages/StockHealth";
import Industry     from "./pages/Industry";
import Performance  from "./pages/Performance";
import CopyTrade    from "./pages/CopyTrade";

const NAV = [
  { to: "/",           label: "DASHBOARD",  icon: Activity },
  { to: "/quote",      label: "QUOTE",      icon: TrendingUp },
  { to: "/portfolio",  label: "PORTFOLIO",  icon: BarChart2 },
  { to: "/watchlist",  label: "自選股",      icon: Star },
  { to: "/chip",       label: "籌碼",        icon: Cpu },
  { to: "/health",     label: "健診",        icon: Heart },
  { to: "/industry",   label: "產業鏈",      icon: Map },
  { to: "/alerts",     label: "ALERTS",     icon: Bell },
  { to: "/news",       label: "NEWS",       icon: Newspaper },
  { to: "/performance",label: "排行榜",      icon: Trophy },
  { to: "/copytrade",  label: "跟單",        icon: Users },
  { to: "/backtest",   label: "BACKTEST",   icon: BookOpen },
];

export default function App() {
  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      <nav className="w-40 bg-terminal-surface border-r border-terminal-border flex flex-col flex-shrink-0">
        <div className="p-3 border-b border-terminal-border">
          <div className="text-terminal-accent font-mono text-xs font-bold tracking-widest">
            ◈ TW BLOOMBERG
          </div>
          <div className="text-terminal-muted text-xs mt-0.5">Terminal v2.0</div>
        </div>
        <div className="flex-1 py-1 overflow-y-auto">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-2 px-3 py-2 text-xs tracking-wide transition-colors ${
                  isActive
                    ? "bg-terminal-border text-terminal-accent border-l-2 border-terminal-accent"
                    : "text-terminal-muted hover:text-terminal-text hover:bg-terminal-border/30"
                }`
              }
            >
              <Icon size={12} />
              {label}
            </NavLink>
          ))}
        </div>
        <div className="p-3 border-t border-terminal-border">
          <div className="text-terminal-muted text-xs">
            <span className="up">●</span> LIVE
          </div>
        </div>
      </nav>

      {/* Main */}
      <main className="flex-1 overflow-auto bg-terminal-bg">
        <Routes>
          <Route path="/"            element={<Dashboard />} />
          <Route path="/quote"       element={<Quote />} />
          <Route path="/portfolio"   element={<Portfolio />} />
          <Route path="/watchlist"   element={<Watchlist />} />
          <Route path="/chip"        element={<ChipTracker />} />
          <Route path="/health"      element={<StockHealth />} />
          <Route path="/industry"    element={<Industry />} />
          <Route path="/alerts"      element={<Alerts />} />
          <Route path="/news"        element={<News />} />
          <Route path="/performance" element={<Performance />} />
          <Route path="/copytrade"   element={<CopyTrade />} />
          <Route path="/backtest"    element={<Backtest />} />
        </Routes>
      </main>
    </div>
  );
}
