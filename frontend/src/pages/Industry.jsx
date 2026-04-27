import React, { useState } from "react";
import Card from "../components/Card";
import { getQuote } from "../utils/api";
import PriceTag from "../components/PriceTag";

// 台灣主要產業鏈靜態資料
const INDUSTRY_CHAINS = [
  {
    id: "semi",
    name: "半導體",
    color: "#00d4ff",
    emoji: "🔵",
    description: "IC設計 → 晶圓代工 → 封測",
    categories: [
      { name: "IC設計", stocks: [{ code: "2454", name: "聯發科" }, { code: "2379", name: "瑞昱" }, { code: "3034", name: "聯詠" }, { code: "2408", name: "南亞科" }] },
      { name: "晶圓代工", stocks: [{ code: "2330", name: "台積電" }, { code: "2303", name: "聯電" }, { code: "5347", name: "世界" }] },
      { name: "封裝測試", stocks: [{ code: "2449", name: "京元電子" }, { code: "2325", name: "矽品" }, { code: "3711", name: "日月光投控" }] },
      { name: "矽晶圓", stocks: [{ code: "5288", name: "環球晶" }, { code: "6191", name: "精材" }] },
    ],
  },
  {
    id: "ai",
    name: "AI / 伺服器",
    color: "#00ff88",
    emoji: "🟢",
    description: "AI晶片 → 散熱 → 伺服器 → ODM",
    categories: [
      { name: "AI晶片", stocks: [{ code: "2330", name: "台積電" }, { code: "2454", name: "聯發科" }] },
      { name: "散熱模組", stocks: [{ code: "3017", name: "奇鋐" }, { code: "8499", name: "勤誠" }, { code: "3491", name: "昇業" }] },
      { name: "PCB/電路板", stocks: [{ code: "3037", name: "欣興" }, { code: "2383", name: "台光電" }, { code: "6269", name: "台郡" }] },
      { name: "伺服器/ODM", stocks: [{ code: "2317", name: "鴻海" }, { code: "3231", name: "緯創" }, { code: "2356", name: "英業達" }, { code: "2382", name: "廣達" }] },
    ],
  },
  {
    id: "ev",
    name: "電動車",
    color: "#ffcc00",
    emoji: "🟡",
    description: "電池 → 馬達 → 車電 → 充電樁",
    categories: [
      { name: "電池/材料", stocks: [{ code: "1590", name: "亞德客" }, { code: "6770", name: "力智" }] },
      { name: "馬達/控制", stocks: [{ code: "1504", name: "東元" }, { code: "1537", name: "廣隆" }] },
      { name: "車用電子", stocks: [{ code: "2399", name: "映泰" }, { code: "6213", name: "聯茂" }] },
      { name: "充電/電源", stocks: [{ code: "3052", name: "夆典" }, { code: "6285", name: "彩晶" }] },
    ],
  },
  {
    id: "panel",
    name: "面板",
    color: "#ff8844",
    emoji: "🟠",
    description: "面板 → 驅動IC → 背光",
    categories: [
      { name: "面板廠", stocks: [{ code: "2409", name: "友達" }, { code: "3481", name: "群創" }] },
      { name: "驅動IC", stocks: [{ code: "3034", name: "聯詠" }, { code: "4966", name: "譜瑞" }] },
      { name: "玻璃基板", stocks: [{ code: "2406", name: "國碩" }] },
    ],
  },
  {
    id: "finance",
    name: "金融",
    color: "#8888ff",
    emoji: "🔷",
    description: "銀行 → 壽險 → 券商",
    categories: [
      { name: "金控", stocks: [{ code: "2882", name: "國泰金" }, { code: "2891", name: "中信金" }, { code: "2884", name: "玉山金" }, { code: "2886", name: "兆豐金" }] },
      { name: "壽險", stocks: [{ code: "2823", name: "中壽" }, { code: "2832", name: "台產" }] },
      { name: "券商", stocks: [{ code: "2885", name: "元大金" }, { code: "2890", name: "永豐金" }] },
    ],
  },
  {
    id: "trad",
    name: "傳產/電子",
    color: "#ff4466",
    emoji: "🔴",
    description: "機械 → 工具機 → 精密零件",
    categories: [
      { name: "工具機", stocks: [{ code: "2049", name: "上銀" }, { code: "1560", name: "中砂" }] },
      { name: "精密機械", stocks: [{ code: "1515", name: "力山" }, { code: "2059", name: "川湖" }] },
      { name: "自動化", stocks: [{ code: "2404", name: "漢唐" }, { code: "4919", name: "新唐" }] },
    ],
  },
];

export default function Industry() {
  const [selected, setSelected]   = useState(null);
  const [quotes, setQuotes]       = useState({});
  const [loadingCode, setLoading] = useState("");

  const fetchQuote = async (code) => {
    if (quotes[code]) return;
    setLoading(code);
    try {
      const q = await getQuote(code);
      setQuotes(prev => ({ ...prev, [code]: q }));
    } catch (e) {
      // ignore
    } finally {
      setLoading("");
    }
  };

  const handleSelectIndustry = (ind) => {
    setSelected(selected?.id === ind.id ? null : ind);
    if (selected?.id !== ind.id) {
      // 預載所有股票報價
      ind.categories.forEach(cat =>
        cat.stocks.forEach(s => fetchQuote(s.code))
      );
    }
  };

  return (
    <div className="p-4 space-y-4">
      <div className="border-b border-terminal-border pb-3">
        <h1 className="text-terminal-accent text-lg font-bold tracking-widest">◈ 產業鏈地圖</h1>
        <div className="text-terminal-muted text-xs mt-1">點選產業查看上下游供應鏈標的</div>
      </div>

      {/* 產業格格 */}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        {INDUSTRY_CHAINS.map(ind => (
          <button
            key={ind.id}
            onClick={() => handleSelectIndustry(ind)}
            className={`p-4 rounded-lg border text-left transition-all ${
              selected?.id === ind.id
                ? "border-opacity-100 bg-opacity-20"
                : "border-terminal-border bg-terminal-surface hover:border-opacity-50"
            }`}
            style={selected?.id === ind.id ? {
              borderColor: ind.color,
              backgroundColor: ind.color + "22",
            } : {}}
          >
            <div className="text-2xl mb-1">{ind.emoji}</div>
            <div className="font-bold text-terminal-text text-sm">{ind.name}</div>
            <div className="text-terminal-muted text-xs mt-1 leading-snug">{ind.description}</div>
            <div className="text-terminal-muted text-xs mt-2">
              {ind.categories.reduce((n, c) => n + c.stocks.length, 0)} 檔
            </div>
          </button>
        ))}
      </div>

      {/* 產業詳情 */}
      {selected && (
        <div className="space-y-3">
          <div className="flex items-center gap-2 border-b border-terminal-border pb-2">
            <span className="text-xl">{selected.emoji}</span>
            <span className="text-terminal-accent font-bold text-base">{selected.name} 供應鏈</span>
          </div>

          {selected.categories.map(cat => (
            <Card key={cat.name} title={cat.name}>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                {cat.stocks.map(s => {
                  const q = quotes[s.code];
                  return (
                    <div
                      key={s.code}
                      className="bg-terminal-bg rounded p-2 border border-terminal-border/50"
                    >
                      <div className="text-terminal-accent text-sm font-bold">{s.code}</div>
                      <div className="text-terminal-text text-xs">{s.name}</div>
                      {loadingCode === s.code && (
                        <div className="text-terminal-muted text-xs mt-1">載入中...</div>
                      )}
                      {q && (
                        <div className="mt-1">
                          <div className="text-terminal-text font-mono text-sm">{q.price}</div>
                          <PriceTag value={q.change} pct={q.change_pct} />
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
