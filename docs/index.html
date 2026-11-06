<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1f2a36">
<title>市場觀察</title>
<style>
:root{--bg:#eef1f4;--card:#fff;--ink:#17212b;--mute:#667584;--line:#dbe1e7;--up:#c8352b;--down:#1f8a5b;--ok:#667584;--near:#b7791f;--break:#c8352b;--trigger:#2b6cb0;--spark:#2b3a4a;--sup:#b7791f}
@media (prefers-color-scheme:dark){:root{--bg:#10161d;--card:#18212b;--ink:#e6ebf0;--mute:#93a1af;--line:#27333f;--up:#ef6a5f;--down:#4cc38a;--near:#e0a84a;--break:#ef6a5f;--trigger:#6aa9e8;--spark:#b8c4d0;--sup:#e0a84a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif;padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
main{max-width:640px;margin:0 auto;padding:16px}
h1{font-size:1.35rem;margin:8px 0 2px}
.sub{color:var(--mute);font-size:.88rem;margin:0 0 14px}
.stale{background:#fff3cd;color:#664d03;padding:8px 12px;border-radius:8px;font-size:.9rem;margin-bottom:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;min-width:0}
.wide{grid-column:1/-1}
.top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.pr{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
.name{font-weight:650;font-size:.95rem}
.price{font-size:1.25rem;font-weight:700;font-variant-numeric:tabular-nums}
.chg{font-size:.85rem;font-variant-numeric:tabular-nums}
.meta{display:flex;justify-content:space-between;font-size:.72rem;color:var(--mute);margin-top:2px}
.up{color:var(--up)}.dn{color:var(--down)}
.chip{display:inline-block;font-size:.8rem;padding:1px 9px;border-radius:99px;border:1px solid currentColor;margin:6px 0 2px}
.ok{color:var(--ok)}.near{color:var(--near)}.break{color:var(--break)}.trigger{color:var(--trigger)}
.text{margin:2px 0 8px;font-size:.84rem;line-height:1.45}
.facts{font-size:.8rem;color:var(--mute)}
.facts div{display:flex;justify-content:space-between}
.facts b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
svg{width:100%;height:56px;margin-top:8px}
.bars{display:flex;align-items:flex-end;gap:3px;height:56px;margin-top:6px}
.bars i{flex:1;border-radius:2px 2px 0 0;min-height:2px}
details{font-size:.88rem;color:var(--mute);margin:14px 0}
summary{cursor:pointer;color:var(--ink)}
footer{color:var(--mute);font-size:.8rem;padding:6px 0 20px}
</style>
</head>
<body>
<main>
<h1>市場觀察</h1>
<p class="sub" id="sub">載入中…</p>
<div id="warn"></div>
<div id="list" class="grid"></div>
<details><summary>規則說明</summary>
<p>支撐:近 60 個交易日(不含今天)的最低價。量比:今日成交量 ÷ 前 20 日平均量。回落:現價相對一年內最高收盤價的跌幅,達 15% 或 25% 時標示「已達第一批/第二批條件」,門檻可在 fetch_data.py 調整。外資資料為證交所公布的外資及陸資買賣超(億元)。台股習慣:紅漲綠跌。</p></details>
<footer>資料來自證交所與公開行情來源,可能有延遲或誤差,以官方公告為準。本頁僅為資訊整理,不構成任何投資建議;每個人的資金與風險承受度不同,請自行判斷。</footer>
</main>
<script>
const $=s=>document.querySelector(s);
const LV={ok:"一般",near:"接近支撐",break:"低於支撐",trigger:"達進場條件"};
const f=(n,d=2)=>n==null?"—":Number(n).toLocaleString("en-US",{minimumFractionDigits:d,maximumFractionDigits:d});
const sg=n=>(n>0?"+":"")+f(n)+"%";
function spark(a,sup){
  const lo=Math.min(...a,sup),hi=Math.max(...a),r=hi-lo||1,w=300,h=56;
  const y=v=>h-4-((v-lo)/r)*(h-8),x=i=>i*w/(a.length-1);
  const p=a.map((v,i)=>x(i).toFixed(1)+","+y(v).toFixed(1)).join(" ");
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="近 60 日走勢與支撐線"><line x1="0" x2="${w}" y1="${y(sup)}" y2="${y(sup)}" stroke="var(--sup)" stroke-dasharray="4 3" stroke-width="1.2"/><polyline points="${p}" fill="none" stroke="var(--spark)" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>`;
}
function card(i){
  if(i.error)return `<div class="card"><div class="name">${i.name}</div><p class="text">${i.error},稍後會再試。</p></div>`;
  const c=i.chg>=0?"up":"dn";
  return `<div class="card"><div class="name">${i.name}</div>
  <div class="pr"><span class="price">${f(i.close)}</span><span class="chg ${c}">${sg(i.chg)}</span></div>
  <span class="chip ${i.level}">${LV[i.level]}</span><p class="text">${i.text}</p>
  <div class="facts"><div>距支撐<b>${sg(i.dist)}</b></div><div>量比<b>${i.vol_ratio==null?"—":f(i.vol_ratio)}</b></div><div>距季線<b>${sg(i.vs_ma60)}</b></div></div>
  ${spark(i.spark,i.support)}<div class="meta"><span>支撐 ${f(i.support)}</span><span>${i.date.slice(5)}</span></div></div>`;
}
function foreign(a){
  if(!a||!a.length)return "";
  const m=Math.max(...a.map(x=>Math.abs(x.net_yi)))||1,l=a[a.length-1];
  const bars=a.map(x=>`<i title="${x.date}: ${x.net_yi} 億" style="height:${Math.max(4,Math.abs(x.net_yi)/m*100)}%;background:var(--${x.net_yi>=0?"up":"down"})"></i>`).join("");
  return `<div class="card wide"><div class="top"><span class="name">外資買賣超(台股)</span><span class="chg ${l.net_yi>=0?"up":"dn"}">${l.net_yi>0?"+":""}${f(l.net_yi,1)} 億</span></div><div class="bars">${bars}</div><div class="meta" style="margin-top:4px"><span>近 ${a.length} 筆</span><span>${l.date}</span></div></div>`;
}
fetch("data.json?"+Date.now()).then(r=>r.json()).then(d=>{
  $("#sub").textContent="最後更新 "+d.updated+"(台灣時間)";
  const age=(Date.now()-new Date(d.updated.replace(" ","T")+"+08:00"))/864e5;
  if(age>3)$("#warn").innerHTML='<div class="stale">資料已超過 3 天沒更新,數字可能過時。</div>';
  $("#list").innerHTML=d.items.map(card).join("")+foreign(d.foreign);
}).catch(()=>{$("#sub").textContent="資料載入失敗,請稍後再試。"});
</script>
</body>
</html>
