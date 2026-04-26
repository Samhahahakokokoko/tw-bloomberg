import React from "react";
import { Routes, Route, NavLink } from "react-router-dom";
import { BarChart2, TrendingUp, BookOpen, Bell, Newspaper, Activity } from "lucide-react";
import Dashboard from "./pages/Dashboard";
import Quote from "./pages/Quote";
import Portfolio from "./pages/Portfolio";
import Alerts from "./pages/Alerts";
import News from "./pages/News";
import Backtest from "./pages/Backtest";

const NAV = [
  { to: "/", label: "DASHBOARD", icon: Activity },
  { to: "/quote", label: "QUOTE", icon: TrendingUp },
  { to: "/portfolio", label: "PORTFOLIO", icon: BarChart2 },
  { to: "/alerts", label: "ALERTS", icon: Bell },
  { to: "/news", label: "NEWS", icon: Newspaper },
  { to: "/backtest", label: "BACKTEST", icon: BookOpen },
];

export default function App() {
  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      <nav className="w-48 bg-terminal-surface border-r border-terminal-border flex flex-col flex-shrink-0">
        <div className="p-4 border-b border-terminal-border">
          <div className="text-terminal-accent font-mono text-sm font-bold tracking-widest">
            ◈ TW BLOOMBERG
          </div>
          <div className="text-terminal-muted text-xs mt-1">Terminal v1.0</div>
        </div>
        <div className="flex-1 py-2">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-2 px-4 py-2.5 text-xs tracking-widest transition-colors ${
                  isActive
                    ? "bg-terminal-border text-terminal-accent border-l-2 border-terminal-accent"
                    : "text-terminal-muted hover:text-terminal-text hover:bg-terminal-border/30"
                }`
              }
            >
              <Icon size={14} />
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
          <Route path="/" element={<Dashboard />} />
          <Route path="/quote" element={<Quote />} />
          <Route path="/portfolio" element={<Portfolio />} />
          <Route path="/alerts" element={<Alerts />} />
          <Route path="/news" element={<News />} />
          <Route path="/backtest" element={<Backtest />} />
        </Routes>
      </main>
    </div>
  );
}
