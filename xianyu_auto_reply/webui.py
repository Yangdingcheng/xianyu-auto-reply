"""本地管理后台（标准库 http.server，零依赖）。

功能：运行状态与统计、关键词规则增删（落盘 rules.json）、卡密库存与补货、
回复日志查看、在线测试（干跑：只看决策不真发）、模拟发送（演示引擎出站箱）。
仅监听本机，请勿直接暴露公网。
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import IncomingMessage

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>闲鱼自动回复助手 · 管理后台</title>
<style>
body{font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f6f8;margin:0;color:#222}
header{background:#ffb400;padding:14px 20px;font-weight:700;font-size:18px}
main{max-width:1020px;margin:0 auto;padding:16px}
.card{background:#fff;border-radius:12px;padding:16px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.stats{display:flex;gap:10px;flex-wrap:wrap}
.stat{flex:1;min-width:100px;background:#fff8e6;border-radius:10px;padding:10px;text-align:center}
.stat b{display:block;font-size:22px}
input,select,textarea{padding:7px 9px;border:1px solid #ddd;border-radius:8px;font-size:14px}
button{background:#ffb400;border:0;border-radius:8px;padding:8px 14px;font-weight:600;cursor:pointer}
button.ghost{background:#eee}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{border-bottom:1px solid #eee;padding:6px 8px;text-align:left;vertical-align:top}
pre{background:#111;color:#9fe8a9;padding:10px;border-radius:8px;max-height:260px;overflow:auto;font-size:12px;white-space:pre-wrap}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.small{color:#888;font-size:12px}
</style></head>
<body>
<header>🐟 闲鱼自动回复助手 · 管理后台 <span class="small" id="mode"></span></header>
<main>
  <div class="card"><h3>运行状态</h3><div class="stats" id="stats"></div>
  <p class="small">卡密库存：<span id="cards"></span></p></div>

  <div class="card"><h3>在线测试（干跑，只看决策不真发）</h3>
    <div class="row">
      <input id="t_text" placeholder="输入买家消息，如：能便宜点吗，150卖不卖" style="flex:2;min-width:240px">
      <input id="t_item" placeholder="商品ID（如 1001）" style="width:120px">
      <button onclick="testReply(false)">测试决策</button>
      <button class="ghost" onclick="testReply(true)">模拟发送</button>
    </div>
    <pre id="t_out">（决策结果会显示在这里）</pre>
    <h4>演示出站箱</h4><pre id="outbox">（暂无）</pre>
  </div>

  <div class="card"><h3>关键词规则</h3>
    <table><thead><tr><th>ID</th><th>名称</th><th>类型</th><th>关键词</th><th>回复</th><th>商品</th><th>优先级</th><th></th></tr></thead>
    <tbody id="rules"></tbody></table>
    <h4>新增规则</h4>
    <div class="row">
      <input id="r_id" placeholder="ID" style="width:90px">
      <input id="r_name" placeholder="名称" style="width:110px">
      <select id="r_type"><option value="contains">包含</option><option value="exact">精确</option><option value="regex">正则</option></select>
      <input id="r_kw" placeholder="关键词，逗号分隔" style="flex:1;min-width:160px">
      <input id="r_item" placeholder="商品ID(可空)" style="width:110px">
      <input id="r_pri" placeholder="优先级" type="number" value="10" style="width:80px">
    </div>
    <div class="row" style="margin-top:8px">
      <input id="r_reply" placeholder="回复内容" style="flex:1;min-width:240px">
      <button onclick="addRule()">添加规则</button>
    </div>
  </div>

  <div class="card"><h3>卡密补货</h3>
    <div class="row">
      <input id="c_item" placeholder="商品ID，如 2001" style="width:140px">
      <input id="c_codes" placeholder="卡密，逗号分隔" style="flex:1;min-width:240px">
      <button onclick="addCards()">补货</button>
    </div>
  </div>

  <div class="card"><h3>回复日志（最近 30 条）</h3>
    <button class="ghost" onclick="loadLogs()">刷新日志</button>
    <pre id="logs">…</pre>
  </div>
</main>
<script>
const $=id=>document.getElementById(id);
async function j(url,opt){const r=await fetch(url,opt);return r.json()}
async function loadStats(){const d=await j('/api/stats');
  const s=d.stats;const names={received:'收到消息',replied:'已回复',rule:'规则命中',ai:'AI回复',bargain:'议价回复',default:'默认/发货',skipped:'跳过',delivered:'已发货'};
  $('stats').innerHTML=Object.keys(names).map(k=>`<div class="stat"><b>${s[k]||0}</b>${names[k]}</div>`).join('');
  $('cards').textContent=Object.entries(d.cards||{}).map(([k,v])=>`${k}: 剩 ${v.left}/${v.total}`).join('；')||'（无）';
  $('mode').textContent='· 账号：'+(d.account||'演示');
  const ob=await j('/api/outbox');
  $('outbox').textContent=ob.sent.length?ob.sent.map(m=>`→ ${m.chat_id}：${m.text}`).join('\n'):'（暂无）';}
async function loadRules(){const d=await j('/api/rules');
  $('rules').innerHTML=d.rules.map(r=>`<tr><td>${r.id}</td><td>${r.name||''}</td><td>${r.match_type}</td><td>${(r.keywords||[]).join(', ')}</td><td>${Array.isArray(r.reply)?r.reply.join(' / '):r.reply}</td><td>${r.item_id||'全局'}</td><td>${r.priority}</td><td><button class="ghost" onclick="delRule('${r.id}')">删</button></td></tr>`).join('');}
async function loadLogs(){const d=await j('/api/logs?n=30');
  $('logs').textContent=d.logs.map(l=>{try{const o=JSON.parse(l);return `${o.time} [${o.action}${o.source?'/'+o.source:''}${o.reason?'/'+o.reason:''}] ${o.incoming||''} ${o.reply?'→ '+o.reply:''}`}catch(e){return l}}).join('\n')||'（暂无）';}
async function testReply(send){const body={text:$('t_text').value,item_id:$('t_item').value||null,sender_name:'测试买家'};
  const d=await j(send?'/api/simulate':'/api/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  $('t_out').textContent=JSON.stringify(d,null,2);loadStats();loadLogs();}
async function addRule(){const body={id:$('r_id').value,name:$('r_name').value,match_type:$('r_type').value,
  keywords:$('r_kw').value.split(',').map(s=>s.trim()).filter(Boolean),reply:$('r_reply').value,
  item_id:$('r_item').value||null,priority:parseInt($('r_pri').value||'10')};
  await j('/api/rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});loadRules();}
async function delRule(id){await j('/api/rules/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});loadRules();}
async function addCards(){const body={item_id:$('c_item').value,codes:$('c_codes').value.split(',').map(s=>s.trim()).filter(Boolean)};
  const d=await j('/api/cards/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  alert('已补货 '+d.added+' 条');loadStats();}
loadStats();loadRules();loadLogs();setInterval(loadStats,5000);
</script>
</body></html>"""


class WebUI:
    def __init__(self, engine, rules_path: str | None = None):
        self.engine = engine
        self.rules_path = rules_path

    def _save_rules(self):
        if self.rules_path:
            with open(self.rules_path, "w", encoding="utf-8") as f:
                json.dump(self.engine.rules.to_list(), f, ensure_ascii=False, indent=1)

    def serve(self, host: str = "127.0.0.1", port: int = 8765):
        ui = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    return json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    return {}

            def do_GET(self):
                if self.path == "/":
                    body = HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path.startswith("/api/stats"):
                    self._json({"stats": ui.engine.stats, "account": ui.engine.account,
                                "cards": ui.engine.cards.overview() if ui.engine.cards else {}})
                elif self.path.startswith("/api/rules"):
                    self._json({"rules": ui.engine.rules.to_list()})
                elif self.path.startswith("/api/outbox"):
                    sent = getattr(ui.engine.transport, "sent", [])
                    self._json({"sent": sent})
                elif self.path.startswith("/api/logs"):
                    n = 30
                    if "n=" in self.path:
                        try:
                            n = int(self.path.split("n=")[1].split("&")[0])
                        except Exception:
                            pass
                    lines = []
                    if os.path.exists(ui.engine.log_path):
                        with open(ui.engine.log_path, encoding="utf-8") as f:
                            lines = f.read().splitlines()[-n:]
                    self._json({"logs": lines})
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):
                data = self._body()
                if self.path == "/api/test" or self.path == "/api/simulate":
                    msg = IncomingMessage(
                        chat_id=data.get("chat_id") or "web-test",
                        sender_id=data.get("sender_id") or "web-buyer",
                        sender_name=data.get("sender_name") or "测试买家",
                        text=data.get("text") or "",
                        item_id=data.get("item_id") or None)
                    decision = ui.engine.handle(msg, commit=(self.path == "/api/simulate"))
                    self._json(decision)
                elif self.path == "/api/rules":
                    r = ui.engine.rules.add(data)
                    ui._save_rules()
                    self._json({"ok": True, "rule": r.to_dict()})
                elif self.path == "/api/rules/delete":
                    ok = ui.engine.rules.remove(str(data.get("id", "")))
                    ui._save_rules()
                    self._json({"ok": ok})
                elif self.path == "/api/cards/add":
                    added = 0
                    if ui.engine.cards:
                        added = ui.engine.cards.add(str(data.get("item_id", "")),
                                                    data.get("codes") or [])
                    self._json({"ok": True, "added": added})
                else:
                    self._json({"error": "not found"}, 404)

        server = ThreadingHTTPServer((host, port), H)
        print(f"管理后台已启动：http://{host}:{port} （Ctrl+C 退出）")
        server.serve_forever()
