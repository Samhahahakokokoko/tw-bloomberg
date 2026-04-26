import axios from "axios";

// 本機用 proxy (vite.config.js)，生產環境用 VITE_API_BASE_URL
const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || "",
  timeout: 30000,
});

export const getQuote         = (code) => api.get(`/api/quote/${code}`).then(r => r.data);
export const getKline         = (code, date) => api.get(`/api/quote/${code}/kline`, { params: { date } }).then(r => r.data);
export const getInstitutional = (code) => api.get(`/api/quote/${code}/institutional`).then(r => r.data);
export const getValuation     = (code) => api.get(`/api/quote/${code}/valuation`).then(r => r.data);
export const getMarketOverview= () => api.get("/api/market/overview").then(r => r.data);

export const getPortfolio  = (userId) => api.get("/api/portfolio", { params: { user_id: userId } }).then(r => r.data);
export const addHolding    = (data)   => api.post("/api/portfolio", data).then(r => r.data);
export const deleteHolding = (id)     => api.delete(`/api/portfolio/${id}`).then(r => r.data);

export const getAlerts    = () => api.get("/api/alerts").then(r => r.data);
export const createAlert  = (data) => api.post("/api/alerts", data).then(r => r.data);
export const deleteAlert  = (id)   => api.delete(`/api/alerts/${id}`).then(r => r.data);

export const getNews = (params) => api.get("/api/news", { params }).then(r => r.data);

export const runBacktest = (data) => api.post("/api/backtest/run", data).then(r => r.data);

export const aiAsk              = (question) => api.post("/api/ai/ask", { question }).then(r => r.data);
export const aiPortfolioAnalysis= () => api.get("/api/ai/portfolio-analysis").then(r => r.data);

export const getDividends = (code) => api.get(`/api/dividend/${code}`).then(r => r.data);
export const getMargin    = (code) => api.get(`/api/margin/${code}`).then(r => r.data);

export const triggerMorningReport = () => api.post("/api/report/morning").then(r => r.data);

export default api;
