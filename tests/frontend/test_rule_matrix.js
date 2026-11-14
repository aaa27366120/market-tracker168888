// 驗證 docs/index.html 的「風險訊號矩陣」。執行：node tests/frontend/test_rule_matrix.js
// 不需要瀏覽器：取出頁面唯一的 inline script，在 vm 中執行 marketAnalysis()。
const vm = require("vm"), fs = require("fs"), path = require("path");
const root = path.join(__dirname, "..", "..");
const html = fs.readFileSync(path.join(root, "docs", "index.html"), "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (scripts.length !== 1) throw new Error("預期只有一個 inline script，實際 " + scripts.length);
const ctx = { document: { querySelector: () => ({}) }, fetch: () => new Promise(() => {}), console };
vm.createContext(ctx);
vm.runInContext(scripts[0], ctx);
const base = JSON.parse(fs.readFileSync(path.join(__dirname, "fixture_market_analysis.json"), "utf8"));
const clone = o => JSON.parse(JSON.stringify(o));
const run = m => { ctx.__m = m; return vm.runInContext("marketAnalysis(__m)", ctx); };
const tableOf = h => h.slice(h.indexOf("<table"), h.indexOf("</table>") + 8);

let fails = 0;
const ok = (cond, msg) => { console.log((cond ? "PASS" : "FAIL") + " - " + msg); if (!cond) fails++; };

// 1) 以 2026-11-13 的實際數值為基準（加權 80、費半 60、標普 80）
let h = run(base);
const head = tableOf(h).match(/<thead>.*?<\/thead>/)[0].replace(/<[^>]+>/g, "|").replace(/\|+/g, "|");
ok(head === "|規則|台灣加權指數|費城半導體|標普 500|", "欄位順序：加權、費半、標普 500（" + head + "）");
ok(/<tfoot><tr><th scope="row">合計<\/th><td>80<\/td><td>60<\/td><td>80<\/td><\/tr><\/tfoot>/.test(h), "畫面重算 80/60/80，與資料檔一致");
ok(!h.includes("不一致"), "分數一致時沒有警示");
ok(h.includes("計分的 3 個指數共 15 條規則，亮燈 11 條"), "亮燈統計 11/15（4+3+4）");
ok(h.includes("所有計分指數都亮燈：收盤低於 MA60、MA20 低於 MA60、20 日動能小於 0"), "列出全部亮燈的規則");
ok(h.includes("所有項目行情日期：2026-11-13"), "行情日期一致時只顯示一個日期");
ok(h.includes("台股技術風險評分") && !h.includes("市場技術風險評分"), "卡片標題已改名");
ok(!h.includes("未計分"), "三個指數都有計分時不顯示「未計分」");
ok(h.includes("分析日期 2026-11-13") && h.includes("評分方法：") && h.includes("三個指數"), "原有欄位保留，方法文字為三個指數");
ok((h.match(/class="analysis-item"/g) || []).length === 3, "下方分析磚塊只有三個");

// 2) 行情日期不同
let m = clone(base); m.assets[1].date = "2026-11-12";
ok(run(m).includes("行情日期多數為 2026-11-13；日期不同：費城半導體 2026-11-12"), "標出行情日期不同的項目");

// 3) 分數與資料檔不一致
m = clone(base); m.scored_indices[0].score = 70;
h = run(m);
ok(h.includes('class="mis">80<small>資料檔 70</small>') && h.includes("不一致"), "分數不一致時顯示警示");

// 4) 費半過期而未計分：兩欄矩陣，費半磚塊標示「未計分」
m = clone(base); m.scored_indices = m.scored_indices.filter(s => s.sym !== "^SOX"); m.coverage = 2;
h = run(m);
const head2 = tableOf(h).match(/<thead>.*?<\/thead>/)[0].replace(/<[^>]+>/g, "|").replace(/\|+/g, "|");
ok(head2 === "|規則|台灣加權指數|標普 500|", "只剩兩欄矩陣（" + head2 + "）");
ok((h.match(/<span class="sess">未計分<\/span>/g) || []).length === 1 && h.includes("有效主要指數 2/3"), "被排除的指數標示「未計分」，且有效指數顯示 2/3");

// 5) 舊格式（沒有 scored_indices）：不顯示矩陣，也不亂標「未計分」
m = clone(base); delete m.scored_indices;
h = run(m);
ok(!h.includes("rule-table") && h.includes("analysis-grid") && !h.includes("未計分"), "沒有 scored_indices 時不顯示矩陣也不標未計分");

// 6) 欄位缺漏
m = clone(base); m.assets[0].rsi14 = null; m.assets[2].macd_direction = "資料不足";
h = run(m);
ok(h.includes('<td class="na">資料不足</td>'), "欄位缺漏的格子顯示資料不足");
ok(/<th scope="row">合計<\/th><td>—<\/td><td>60<\/td><td>—<\/td>/.test(h), "有缺漏的指數合計顯示 —，完整的仍顯示分數");
ok(!h.includes("不一致"), "有缺漏時不做分數核對");

// 7) 找不到對應的資產
m = clone(base); m.assets = m.assets.filter(a => a.sym !== "^SOX");
ok(run(m).includes("<td>—</td><td>80</td></tr></tfoot>"), "找不到對應資產時該欄合計顯示 —");

// 8) 跳脫（只檢查本次新增的矩陣區塊）
m = clone(base); m.scored_indices[0].name = "<img src=x onerror=alert(1)>";
const t = tableOf(run(m));
ok(!t.includes("<img") && t.includes("&lt;img"), "矩陣欄名含 HTML 時已跳脫");

// 9) MACD 中性視為未亮，不是資料不足
m = clone(base); m.assets[0].macd_direction = "中性";
ok(/MACD 柱狀體小於 0<small>\+20 分<\/small><\/th><td class="off">未亮<\/td>/.test(run(m)), "MACD 中性 → 未亮");

// 9b) 剛好等於門檻時一律「未亮」（收盤 = MA60、MA20 = MA60、RSI = 40、MACD 柱狀體 = 0、動能 = 0）
m = clone(base);
Object.assign(m.assets[0], { close: 100, ma20: 100, ma60: 100, rsi14: 40, macd_direction: "中性", momentum20: 0 });
m.scored_indices[0].score = 0;
h = run(m);
ok(/<th scope="row">合計<\/th><td>0<\/td><td>60<\/td><td>80<\/td>/.test(h) && !h.includes("不一致"), "剛好等於門檻時五條規則都不亮（合計 0，且與資料檔一致）");

// 10) 邊界
ok(run(null).includes("市場分析資料準備中"), "m=null 顯示準備中");
ok(typeof run({ risk_score: null, assets: [], scored_indices: [] }) === "string", "空資料不丟例外");

console.log(fails ? `\n${fails} FAILED` : "\nALL PASSED");
process.exit(fails ? 1 : 0);
