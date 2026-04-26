import React, { useEffect, useState } from "react";
import { getNews } from "../utils/api";
import Card from "../components/Card";

const SENTIMENT_LABEL = {
  positive: { text: "利多", cls: "bg-terminal-green/20 text-terminal-green border-terminal-green/30" },
  negative: { text: "利空", cls: "bg-terminal-red/20 text-terminal-red border-terminal-red/30" },
  neutral: { text: "中立", cls: "bg-terminal-yellow/20 text-terminal-yellow border-terminal-yellow/30" },
};

export default function News() {
  const [articles, setArticles] = useState([]);
  const [filter, setFilter] = useState("all");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    getNews({ limit: 50, ...(filter !== "all" ? { sentiment: filter } : {}) })
      .then(setArticles)
      .finally(() => setLoading(false));
  }, [filter]);

  const counts = articles.reduce((acc, a) => {
    acc[a.sentiment] = (acc[a.sentiment] || 0) + 1;
    return acc;
  }, {});

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest">◈ NEWS FEED</h1>
        <div className="flex gap-2 text-xs">
          {[
            ["all", "全部"],
            ["positive", `利多 (${counts.positive || 0})`],
            ["negative", `利空 (${counts.negative || 0})`],
            ["neutral", `中立 (${counts.neutral || 0})`],
          ].map(([v, label]) => (
            <button
              key={v}
              onClick={() => setFilter(v)}
              className={`px-3 py-1 rounded border text-xs transition-colors ${
                filter === v
                  ? "bg-terminal-accent/20 border-terminal-accent text-terminal-accent"
                  : "border-terminal-border text-terminal-muted hover:border-terminal-text"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="text-terminal-muted text-sm">載入中...</div>
      ) : (
        <div className="space-y-3">
          {articles.map((a, i) => {
            const sent = SENTIMENT_LABEL[a.sentiment] || SENTIMENT_LABEL.neutral;
            return (
              <div
                key={i}
                className="bg-terminal-surface border border-terminal-border rounded p-3 hover:border-terminal-accent/50 transition-colors"
              >
                <div className="flex items-start gap-3">
                  <span className={`text-xs px-2 py-0.5 rounded border flex-shrink-0 ${sent.cls}`}>
                    {sent.text}
                  </span>
                  <div className="flex-1 min-w-0">
                    <a
                      href={a.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-sm text-terminal-text hover:text-terminal-accent transition-colors block"
                    >
                      {a.title}
                    </a>
                    {a.content && (
                      <p className="text-xs text-terminal-muted mt-1 line-clamp-2">{a.content}</p>
                    )}
                    <div className="flex gap-3 mt-2 text-xs text-terminal-muted">
                      <span>{a.source}</span>
                      {a.published_at && <span>{new Date(a.published_at).toLocaleString("zh-TW")}</span>}
                      {a.related_stocks && (
                        <span>
                          相關：{a.related_stocks.split(",").map((c) => (
                            <span key={c} className="text-terminal-accent ml-1">#{c}</span>
                          ))}
                        </span>
                      )}
                    </div>
                  </div>
                  {a.sentiment_score != null && (
                    <div className="text-xs text-terminal-muted flex-shrink-0">
                      {(a.sentiment_score * 100).toFixed(0)}%
                    </div>
                  )}
                </div>
              </div>
            );
          })}
          {articles.length === 0 && (
            <div className="text-terminal-muted text-sm text-center py-8">
              尚無新聞 — 等待爬蟲排程執行
            </div>
          )}
        </div>
      )}
    </div>
  );
}
