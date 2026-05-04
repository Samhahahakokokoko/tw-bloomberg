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
export const getNews       = (params) => api.get("/api/news", { params }).then(r => r.data);
export const getDataStatus    = ()       => api.get("/api/data-status").then(r => r.data);
export const getSystemHealth  = ()       => api.get("/api/system/health").then(r => r.data);
export const getKillSwitch    = ()       => api.get("/api/system/kill-switch").then(r => r.data);
export const activateKillSwitch   = (reason) => api.post(`/api/system/kill-switch/activate?reason=${encodeURIComponent(reason)}`).then(r => r.data);
export const deactivateKillSwitch = ()       => api.post("/api/system/kill-switch/deactivate").then(r => r.data);

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

// ── Screener v2 ───────────────────────────────────────────────────────────────
export const runScreener     = (data) => api.post("/api/screener", data).then(r => r.data);
export const nlScreener      = (query) => api.post("/api/screener/nl", { query }).then(r => r.data);
export const getScreenerTop  = (limit = 20) => api.get("/api/screener/top", { params: { limit } }).then(r => r.data);
export const getScreenerPresets = () => api.get("/api/screener/presets").then(r => r.data);

// ── Scores ────────────────────────────────────────────────────────────────────
export const getStockScoreV2 = (code) => api.get(`/api/scores/${code}`).then(r => r.data);

// ── Financials / Revenue ──────────────────────────────────────────────────────
export const getFinancials   = (code, limit = 8) => api.get(`/api/financials/${code}`, { params: { limit } }).then(r => r.data);
export const getRevenue      = (code, months = 13) => api.get(`/api/revenue/${code}`, { params: { months } }).then(r => r.data);

// ── Industry Sentiment v2 ─────────────────────────────────────────────────────
export const getIndustrySentiments = () => api.get("/api/industry/sentiment").then(r => r.data);
export const getSingleIndustrySentiment = (industry) => api.get(`/api/industry/sentiment/${encodeURIComponent(industry)}`).then(r => r.data);
export const refreshIndustrySentiment = () => api.post("/api/industry/sentiment/refresh").then(r => r.data);

// ── Pipeline ──────────────────────────────────────────────────────────────────
export const triggerPipeline = (code) => api.post("/api/pipeline/run", null, { params: code ? { stock_code: code } : {} }).then(r => r.data);
export const triggerScoring  = () => api.post("/api/pipeline/score").then(r => r.data);

// ── Recommendation Tracker ────────────────────────────────────────────────────
export const getAccuracyStats   = (days = 30) => api.get("/api/accuracy", { params: { days } }).then(r => r.data);
export const getWeightHistory   = (limit = 20) => api.get("/api/accuracy/weights", { params: { limit } }).then(r => r.data);
export const getCurrentWeights  = () => api.get("/api/accuracy/weights/current").then(r => r.data);
export const triggerBackfill    = () => api.post("/api/accuracy/backfill").then(r => r.data);
export const triggerWeightAdjust= () => api.post("/api/accuracy/adjust-weights").then(r => r.data);

// ── Broker Tracker ────────────────────────────────────────────────────────────
export const getTopBrokers      = (code, days = 10) => api.get(`/api/broker/${code}`, { params: { days } }).then(r => r.data);
export const trackBroker        = (name, days = 5) => api.get(`/api/broker/track/${encodeURIComponent(name)}`, { params: { days } }).then(r => r.data);
export const getSmartMoney      = () => api.get("/api/broker/smart-money/signals").then(r => r.data);
export const fetchBrokerData    = (code, days = 10) => api.post(`/api/broker/${code}/fetch`, null, { params: { days } }).then(r => r.data);

// ── Portfolio Optimizer ───────────────────────────────────────────────────────
export const optimizePortfolio  = (userId = "") => api.get("/api/portfolio/optimize", { params: { user_id: userId } }).then(r => r.data);
export const getPortfolioVar    = (userId = "") => api.get("/api/portfolio/var", { params: { user_id: userId } }).then(r => r.data);
export const getCorrelation     = (userId = "") => api.get("/api/portfolio/correlation", { params: { user_id: userId } }).then(r => r.data);

export default api;
