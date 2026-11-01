"""Static HTML for the web chat demo and the Otari usage dashboard (no build step, no JS deps)."""

CHAT_HTML = r"""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>ShopPal on Otari · {{STORE}}</title>
<style>
:root{--bg:#f6f4ef;--ink:#1d1b18;--mut:#6b665e;--acc:#c2410c;--card:#fff;--line:#e6e1d6}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
header b{font-size:18px}header small{color:var(--mut)}header a{color:var(--acc);text-decoration:none;font-weight:600}
main{max-width:760px;margin:0 auto;padding:12px 14px 150px}
.msg{margin:10px 0;display:flex;flex-direction:column}.msg.u{align-items:flex-end}
.bub{max-width:85%;padding:10px 13px;border-radius:14px;white-space:pre-wrap;line-height:1.4;background:var(--card);border:1px solid var(--line)}
.u .bub{background:#1d1b18;color:#fff;border:0}
.meta{font-size:12px;color:var(--mut);margin-top:4px;max-width:85%}
.meta span{display:inline-block;background:#ece7dc;border-radius:8px;padding:1px 7px;margin:2px 3px 0 0}
.meta span.bad{background:#fde2e1;color:#991b1b}.meta span.ok{background:#dcfce7;color:#166534}
.pay{display:inline-block;margin-top:8px;background:#ffc439;color:#111;padding:9px 14px;border-radius:20px;font-weight:700;text-decoration:none}
footer{position:fixed;bottom:0;left:0;right:0;background:var(--bg);border-top:1px solid var(--line);padding:10px 14px}
.chips{max-width:760px;margin:0 auto 8px;display:flex;gap:6px;flex-wrap:wrap}
.chips button{border:1px solid var(--line);background:#fff;border-radius:16px;padding:5px 10px;font-size:13px;cursor:pointer}
form{max-width:760px;margin:0 auto;display:flex;gap:8px}
input{flex:1;padding:12px;border-radius:12px;border:1px solid var(--line);font-size:16px}
form button{padding:0 18px;border:0;border-radius:12px;background:var(--acc);color:#fff;font-weight:700;font-size:15px}
.typing{color:var(--mut);font-style:italic}
</style>
<header><div><b>🛍️ ShopPal</b> <small>AI sales agent for {{STORE}} · powered by the Otari gateway · PayPal Sandbox</small></div>
<div><a href="/stats">Otari usage &amp; cost →</a></div></header>
<main id=log></main>
<footer><div class=chips id=chips></div>
<form id=f><input id=q autocomplete=off placeholder="Ask for a kurta, saree, a Diwali look…" maxlength=500><button>Send</button></form></footer>
<script>
const log=document.getElementById('log'),q=document.getElementById('q');
let sid=localStorage.getItem('shoppal_sid');
if(!sid||!/^web-[a-z0-9]{6,32}$/.test(sid)){sid='web-'+Math.random().toString(36).slice(2,12);localStorage.setItem('shoppal_sid',sid)}
const chips=["Red kurta under $30 in size M","What's trending for Diwali this year?","Quote for 12 Red Cotton Kurtas (K101) in rupees","Add 1 K101 in M and checkout","reset"];
chips.forEach(c=>{const b=document.createElement('button');b.type='button';b.textContent=c;b.onclick=()=>send(c);document.getElementById('chips').appendChild(b)});
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function md(s){return esc(s).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')}
function add(role,html,meta){const d=document.createElement('div');d.className='msg '+role;d.innerHTML='<div class=bub>'+html+'</div>'+(meta||'');log.appendChild(d);window.scrollTo(0,document.body.scrollHeight);return d}
function metaHtml(m){if(!m||!m.calls)return '';let h='<div class=meta>';
 const g=m.guard||'';if(g)h+='<span class="'+(g.startsWith('safe')?'ok':(g.startsWith('unsafe')||g.startsWith('budget')?'bad':''))+'">🛡️ '+esc(g)+'</span>';
 m.calls.forEach(c=>{h+='<span title="'+esc(c.request_id||'')+'">'+esc(c.role)+': '+esc(c.model)+(c.provider?' @'+esc(c.provider):'')+(c.requested&&c.requested!==c.model?' (asked '+esc(c.requested)+')':'')+(c.otari_fallback||c.app_fallback?' ↪ fallback':'')+' · $'+Number(c.cost_usd).toFixed(6)+'</span>'});
 if(m.tools&&m.tools.length)h+='<span>🔧 '+esc(m.tools.join(', '))+'</span>';
 h+='<span><b>turn cost $'+Number(m.cost_usd||0).toFixed(6)+'</b></span></div>';return h}
async function send(t){t=(t||q.value).trim();if(!t)return;q.value='';add('u',esc(t));const w=add('b','<span class=typing>ShopPal is typing…</span>');
 try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid,message:t})});const j=await r.json();
  let h=md(j.reply||j.detail||'…');if(j.pay_url)h+='<br><a class=pay target=_blank rel=noopener href="'+esc(j.pay_url)+'">Pay with PayPal (Sandbox)</a>';
  w.innerHTML='<div class=bub>'+h+'</div>'+metaHtml(j.meta)}catch(e){w.innerHTML='<div class=bub>Network error, please retry.</div>'}
 window.scrollTo(0,document.body.scrollHeight)}
document.getElementById('f').onsubmit=e=>{e.preventDefault();send()};
add('b',"👋 Namaste! I'm ShopPal, the shopping assistant for {{STORE}}. Every reply below shows which open-weight model answered through Otari, the safety verdict, the tools used and the exact cost.");
</script></html>"""

STATS_HTML = r"""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>ShopPal · Otari usage</title>
<style>body{font-family:system-ui,sans-serif;background:#f6f4ef;color:#1d1b18;max-width:1000px;margin:0 auto;padding:16px}
h1{font-size:22px}h2{font-size:16px;margin-top:22px}table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
td,th{border:1px solid #e6e1d6;padding:5px 7px;text-align:left}th{background:#ece7dc}.k{display:inline-block;background:#fff;border:1px solid #e6e1d6;border-radius:10px;padding:10px 14px;margin:4px}
.k b{display:block;font-size:20px}a{color:#c2410c}</style>
<h1>📊 ShopPal × Otari: usage, cost and routing</h1><p><a href="/">← back to chat</a> · every number below comes from Otari's per-request <code>usage.cost_usd</code> and <code>Otari-Model / Otari-Provider / Otari-Fallback</code> response headers.</p>
<div id=k></div><h2>By role (each role is a different open-weight model)</h2><div id=r></div>
<h2>By model actually served</h2><div id=m></div><h2>Per-customer spend, last 24h (cap enforced per customer)</h2><div id=c></div>
<h2>Last 25 Otari calls</h2><div id=l></div><h2>Configuration</h2><pre id=cfg></pre>
<script>
function t(rows,cols){if(!rows.length)return '<p>No calls yet.</p>';return '<table><tr>'+cols.map(c=>'<th>'+c+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+cols.map(c=>{let v=r[c];if(c==='cost_usd')v='$'+Number(v||0).toFixed(6);if(c==='ts')v=new Date(v*1000).toLocaleString();return '<td>'+String(v??'').replace(/</g,'&lt;')+'</td>'}).join('')+'</tr>').join('')+'</table>'}
fetch('/api/stats').then(r=>r.json()).then(s=>{
document.getElementById('k').innerHTML=[['Otari calls',s.total_calls],['Total cost','$'+Number(s.total_cost_usd).toFixed(6)],['App spend 24h','$'+Number(s.limits.app_spend_24h).toFixed(6)+' / $'+s.limits.app_daily_usd],['Per-customer cap / 24h','$'+s.limits.per_customer_daily_usd]].map(x=>'<span class=k>'+x[0]+'<b>'+x[1]+'</b></span>').join('');
document.getElementById('r').innerHTML=t(s.by_role,['role','calls','tokens','cost_usd','otari_fallbacks','app_fallbacks','avg_ms']);
document.getElementById('m').innerHTML=t(s.by_model,['model','provider','calls','cost_usd']);
document.getElementById('c').innerHTML=t(s.by_customer_24h,['end_user','calls','cost_usd']);
document.getElementById('l').innerHTML=t(s.recent,['ts','end_user','role','requested','model','provider','otari_fallback','app_fallback','tools','cost_usd','latency_ms']);
document.getElementById('cfg').textContent=JSON.stringify(s.config,null,2)});
</script></html>"""
