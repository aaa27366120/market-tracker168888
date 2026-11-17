// 驗證 docs/index.html 的行情卡片：XSS 跳脫與台指期狀態文字。執行：node tests/frontend/test_cards.js
const vm = require("vm"), fs = require("fs"), path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "..", "docs", "index.html"), "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const ctx = { document: { querySelector: () => ({}) }, fetch: () => new Promise(() => {}), console };
vm.createContext(ctx);
vm.runInContext(scripts[0], ctx);
const call = (fn, ...args) => { ctx.__a = args; return vm.runInContext(`${fn}(...__a)`, ctx); };

let fails = 0;
const ok = (cond, msg) => { console.log((cond ? "PASS" : "FAIL") + " - " + msg); if (!cond) fails++; };
const EVIL = '<img src=x onerror=alert(1)>';

// 1) 外部伺服器回應（live_error、fetch_error 等）不能變成 HTML
const twii = {
  sym: "^TWII", name: "台灣加權指數", date: "2026-11-17", close: 1, chg: 0, level: "ok",
  text: EVIL, live_status: "failed", live_error: EVIL, source: EVIL, close_status_note: EVIL,
  session: EVIL, links: { tv: 'javascript:"><script>' }, spark: [], stale: true,
};
let h = call("card", twii);
ok(!h.includes("<img"), "card() 跳脫 text / live_error / source / note");
ok(!h.includes('"><script>'), "連結網址屬性已跳脫");
h = call("card", { sym: "X", name: EVIL, error: EVIL });
ok(!h.includes("<img"), "錯誤卡片跳脫 name / error");
h = call("foreign", [{ date: EVIL, net_yi: 1 }], { status: "success", message: EVIL });
ok(!h.includes("<img"), "foreign() 跳脫 date / message");
h = call("foreign", [], { message: EVIL });
ok(!h.includes("<img"), "foreign() 無資料時也跳脫 message");

// 2) 台指期狀態：成功、尚未公布、失敗要說人話，並顯示上次實際查詢時間
const fut = (extra) => ({
  sym: "TX", name: "台指期（近月）・日盤", date: "2026-11-16", close: 40885, chg: -6.74, level: "ok",
  text: "一般", spark: [], checked_at: "2026-11-17 16:10", checked_this_run: true,
  publish_note: "官方資料於下一個交易日公布，非即時報價", ...extra,
});
h = call("card", fut({ data_status: "success", stale: false }));
ok(h.includes("最新官方資料（11/16 收盤）"), "成功：最新官方資料（11/16 收盤）");
ok(h.includes("上次查詢期交所：2026-11-17 16:10"), "顯示上次實際查詢期交所的時間");
ok(h.includes("非即時報價"), "說明官方資料晚一個交易日");
ok(!h.includes("擷取 "), "台指期不再顯示每次排程都會變的「擷取」時間");
h = call("card", fut({ data_status: "waiting", stale: false, date: "2026-11-13",
  status_note: "期交所尚未公布 2026-11-16 資料，目前顯示 2026-11-13；之後每次排程都會自動再檢查。" }));
ok(h.includes("尚未公布 2026-11-16") && h.includes("var(--near)"), "尚未公布：黃色提示");
ok(!h.includes("行情更新失敗"), "尚未公布時不誤報「更新失敗」");
h = call("card", fut({ data_status: "failed", stale: true, fetch_error: EVIL,
  status_note: "期交所連線或解析失敗，顯示上一筆資料（2026-11-13）。" }));
ok(h.includes("連線或解析失敗") && h.includes("var(--break)") && !h.includes("<img"), "抓取失敗：紅字，錯誤內容已跳脫");
h = call("card", fut({ data_status: "success", checked_this_run: false }));
ok(h.includes("本次排程未查詢"), "本次沒查詢時明確標示");

console.log(fails ? `\n${fails} FAILED` : "\nALL PASSED");
process.exit(fails ? 1 : 0);
