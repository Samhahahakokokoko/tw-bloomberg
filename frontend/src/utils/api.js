import axios from "axios";

// 本機用 proxy (vite.config.js)，生產環境用 VITE_API_BASE_URL
const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "",
  timeout: 30000,
});

// ── Market ──────────────────────────────────────────────────────────────────
export const getMarketOverview  = () => api.get("/api/market/overview").then(r => r.data);
export const getMarketAnomaly   = () => api.get("/api/market/anomaly").then(r => r.data);

// ── Quote ───────────────────────────────────────────────────────────────────
export const getQuote         = (code) => api.get(`/api/quote/${code}`).then(r => r.data);
export const getKline         = (code, date) => api.get(`/api/quote/${code}/kline`, { params: { date } }).then(r => r.data);
export const getInstitutional = (code) => api.get(`/api/quote/${code}/institutional`).then(r => r.data);
export const getValuation     = (code) => api.get(`/api/quote/${code}/valuation`).then(r => r.data);

// ── Portfolio ───────────────────────────────────────────────────────────────
export const getPortfolio  = (userId) => api.get("/api/portfolio", { params: { user_id: userId } }).then(r => r.data);
export const addHolding    = (data)   => api.post("/api/portfolio", data).then(r => r.data);
export const deleteHolding = (id)     => api.delete(`/api/portfolio/${id}`).then(r => r.data);

// ── Alerts ───────────────────────────────────────────────────────────────────
export const getAlerts    = () => api.get("/api/alerts").then(r => r.data);
export const createAlert  = (data) => api.post("/api/alerts", data).then(r => r.data);
export const deleteAlert  = (id)   => api.delete(`/api/alerts/${id}`).then(r => r.data);

// ── News ─────────────────────────────────────────────────────────────────────
export const getNews = (params) => api.get("/api/news", { params }).then(r => r.data);

// ── Backtest ─────────────────────────────────────────────────────────────────
export const runBacktest = (data) => api.post("/api/backtest/run", data).then(r => r.data);

// ── AI ───────────────────────────────────────────────────────────────────────
export const aiAsk               = (question) => api.post("/api/ai/ask", { question }).then(r => r.data);
export const aiPortfolioAnalysis = () => api.get("/api/ai/portfolio-analysis").then(r => r.data);

// ── Dividend / Margin ────────────────────────────────────────────────────────
export const getDividends = (code) => api.get(`/api/dividend/${code}`).then(r => r.data);
export const getMargin    = (code) => api.get(`/api/margin/${code}`).then(r => r.data);

// ── Reports ──────────────────────────────────────────────────────────────────
export const triggerMorningReport = () => api.post("/api/report/morning").then(r => r.data);
export const triggerWeeklyReport  = () => api.post("/api/report/weekly").then(r => r.data);

// ── Earnings Reminders ────────────────────────────────────────────────────────
export const getEarnings          = (userId = "") => api.get("/api/earnings", { params: { user_id: userId } }).then(r => r.data);
export const createEarnings       = (data) => api.post("/api/earnings", data).then(r => r.data);
export const deleteEarnings       = (id, userId = "") => api.delete(`/api/earnings/${id}`, { params: { user_id: userId } }).then(r => r.data);
export const updateEarningsEps    = (id, actual_eps) => api.put(`/api/earnings/${id}/eps`, { actual_eps }).then(r => r.data);
export const getLatestEps         = (code) => api.get(`/api/earnings/${code}/latest-eps`).then(r => r.data);
export const triggerEarningsCheck = () => api.post("/api/earnings/check-now").then(r => r.data);

// ── Watchlist ─────────────────────────────────────────────────────────────────
export const getWatchlist    = (userId = "") => api.get("/api/watchlist", { params: { user_id: userId } }).then(r => r.data);
export const addWatchlist    = (data) => api.post("/api/watchlist", data).then(r => r.data);
export const deleteWatchlist = (id, userId = "") => api.delete(`/api/watchlist/${id}`, { params: { user_id: userId } }).then(r => r.data);

// ── Chip Tracker ──────────────────────────────────────────────────────────────
export const getChipHistory     = (code, days = 20) => api.get(`/api/chip/${code}/history`, { params: { days } }).then(r => r.data);
export const getMainForceCost   = (code) => api.get(`/api/chip/${code}/main-force-cost`).then(r => r.data);

// ── Stock Health ──────────────────────────────────────────────────────────────
export const getStockHealth = (code) => api.get(`/api/health/${code}`).then(r => r.data);

// ── Performance ───────────────────────────────────────────────────────────────
export const getLeaderboard        = () => api.get("/api/performance/leaderboard").then(r => r.data);
export const getPerformanceHistory = (userId = "", days = 30) => api.get("/api/performance/history", { params: { user_id: userId, days } }).then(r => r.data);
export const triggerSnapshot       = () => api.post("/api/performance/snapshot").then(r => r.data);

// ── Weekly Picks ──────────────────────────────────────────────────────────────
export const getWeeklyPicks = (topN = 5) => api.get("/api/picks/weekly", { params: { top_n: topN } }).then(r => r.data);

// ── Copy Trade ────────────────────────────────────────────────────────────────
export const publishPortfolio   = (data) => api.post("/api/copytrade/publish", data).then(r => r.data);
export const viewSharedPortfolio= (shareCode) => api.get(`/api/copytrade/view/${shareCode}`).then(r => r.data);
export const followTrader       = (data) => api.post("/api/copytrade/follow", data).then(r => r.data);
export const unfollowTrader     = (data) => api.post("/api/copytrade/unfollow", data).then(r => r.data);
export const getFollowing       = (followerId = "") => api.get("/api/copytrade/following", { params: { follower_id: followerId } }).then(r => r.data);

export default api;
