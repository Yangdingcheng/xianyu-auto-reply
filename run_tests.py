#!/usr/bin/env python3
"""核心逻辑测试（纯标准库，直接运行：python run_tests.py）。"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xianyu_auto_reply.ai_client import AIClient
from xianyu_auto_reply.bargain import BargainGuard, extract_offer, has_bargain_intent
from xianyu_auto_reply.cards import CardPool
from xianyu_auto_reply.engine import BotEngine, IncomingMessage
from xianyu_auto_reply.rules import RuleEngine
from xianyu_auto_reply.transport import DemoTransport

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name)
    assert cond, name

NOW = time.time()

def base_cfg():
    return {
        "global": {"work_hours": None, "delay_seconds": [0, 0], "cooldown_seconds": 15,
                   "manual_pause_minutes": 10, "log_dir": tempfile.mkdtemp(),
                   "cards_file": os.path.join(tempfile.mkdtemp(), "cards.json"),
                   "low_stock_alert": 1},
        "ai": {"enabled": False},
        "items": {"1001": {"title": "K380 键盘", "price": 199, "floor_price": 160,
                           "bargain": {"enabled": True, "max_rounds": 3}}},
        "default_reply": "默认回复内容",
        "blacklist": ["bad-guy"],
    }

RULES = [
    {"id": "hello", "match_type": "contains", "keywords": ["在吗"], "reply": "在的", "priority": 10},
    {"id": "postage", "match_type": "contains", "keywords": ["包邮"], "reply": "包邮的", "priority": 20},
    {"id": "wechat", "match_type": "regex", "keywords": ["微信|v信"], "reply": "平台内聊", "priority": 90},
    {"id": "spec", "item_id": "1001", "match_type": "contains", "keywords": ["蓝牙"], "reply": "蓝牙款", "priority": 60},
]

# ---------- rules ----------
eng = RuleEngine(RULES)
check("规则-包含匹配", eng.match("请问包邮吗")[1] == "包邮的")
check("规则-正则匹配", eng.match("加个微信")[1] == "平台内聊")
check("规则-商品专属优先", eng.match("是蓝牙的吗", item_id="1001")[1] == "蓝牙款")
check("规则-商品专属不串台", eng.match("是蓝牙的吗", item_id="9999") is None)
check("规则-变量替换", RuleEngine([{"id": "v", "keywords": ["价"], "reply": "{item_title}只要{price}元"}]).match("啥价", ctx={"item_title": "键盘", "price": 199})[1] == "键盘只要199元")

# ---------- bargain ----------
check("出价提取", extract_offer("150卖不卖") == 150.0)
check("无出价", extract_offer("用了多久了") is None)
check("议价意图", has_bargain_intent("能便宜点吗"))
g = BargainGuard(199, 160, max_rounds=3)
d1 = g.decide(150, 0); check("议价-第一轮还价193", d1.action == "counter" and d1.price == 193)
d2 = g.decide(180, 1); check("议价-第二轮还价187", d2.action == "counter" and d2.price == 187)
d3 = g.decide(185, 2); check("议价-第三轮成交185", d3.action == "accept" and d3.price == 185)
d4 = g.decide(100, 3); check("议价-轮数用尽守底价", d4.action == "hold" and d4.price == 160)
d5 = g.decide(170, 0); check("议价-高于底价但低于本轮价仍还价", d5.action == "counter" and d5.price == 193)
d6 = g.decide(195, 0); check("议价-达到本轮价直接成交", d6.action == "accept")

# ---------- cards ----------
pool_path = os.path.join(tempfile.mkdtemp(), "cards.json")
pool = CardPool(pool_path)
pool.add("2001", ["A-1", "A-2"])
check("卡密-库存", pool.stock("2001") == 2)
check("卡密-取用", pool.take("2001", "买家甲") == "A-1")
pool2 = CardPool(pool_path)
check("卡密-持久化", pool2.stock("2001") == 1)
pool.take("2001"); check("卡密-取空", pool.take("2001") is None)

# ---------- engine pipeline ----------
def make_engine(ai=None, cfg=None):
    cfg = cfg or base_cfg()
    t = DemoTransport()
    e = BotEngine(cfg, RULES, transport=t, ai_client=ai, card_pool=CardPool(cfg["global"]["cards_file"]))
    e.fast = True
    return e, t

def msg(chat, text, ts, sender="u1", name="买家", item="1001", is_self=False):
    return IncomingMessage(chat_id=chat, sender_id=sender, sender_name=name,
                           text=text, item_id=item, ts=ts, is_self=is_self)

e, t = make_engine()
r = e.handle(msg("c1", "在吗", NOW))
check("引擎-规则回复", r["action"] == "reply" and r["source"] == "rule" and len(t.sent) == 1)
r = e.handle(msg("c1", "包邮吗", NOW + 5))
check("引擎-冷却跳过", r["action"] == "skip" and r["reason"] == "cooldown")
r = e.handle(msg("c1", "能便宜点吗？150卖不卖", NOW + 20))
check("引擎-议价还价", r["source"] == "bargain" and "193" in r["reply"])
r = e.handle(msg("c1", "185，诚心要", NOW + 40))
# 第二轮：rounds=1 → counter=187 > 185? 187>185 → counter 187（还没成交）
check("引擎-议价第二轮", r["source"] == "bargain" and "187" in r["reply"])
r = e.handle(msg("c1", "185，诚心要", NOW + 60))
check("引擎-议价第三轮成交", r["source"] == "bargain" and "185" in r["reply"])
e.handle(msg("c1", "我手动说一句", NOW + 80, is_self=True))
r = e.handle(msg("c1", "在吗", NOW + 90))
check("引擎-人工接管暂停", r["action"] == "skip" and r["reason"] == "manual_takeover")
r = e.handle(msg("c9", "在吗", NOW, sender="bad-guy"))
check("引擎-黑名单", r["action"] == "skip" and r["reason"] == "blacklist")
r = e.handle(msg("c10", "这个用了多久了", NOW))
check("引擎-默认回复", r["source"] == "default")
r = e.handle(msg("c10", "还有别的颜色吗", NOW + 30))
check("引擎-默认回复只发一次", r["action"] == "skip" and r["reason"] == "no_match")

# 干跑不落状态
e2, t2 = make_engine()
dry = e2.handle(msg("cx", "在吗", NOW), commit=False)
real = e2.handle(msg("cx", "在吗", NOW))
check("引擎-干跑无副作用", dry["action"] == "reply" and len(t2.sent) == 1 and real["action"] == "reply")

# ---------- order delivery ----------
e3, t3 = make_engine()
e3.cards.add("2001", ["VIP-1", "VIP-2"])
e3.cfg["delivery_template"] = "卡密：{code}"
ok = e3.handle_order_paid({"chat_id": "cd", "sender_name": "买家", "item_id": "2001", "order_id": "O1"})
check("发货-成功", ok and any("VIP-1" in s["text"] for s in t3.sent))
ok2 = e3.handle_order_paid({"chat_id": "cd", "sender_name": "买家", "item_id": "2001", "order_id": "O2"})
ok3 = e3.handle_order_paid({"chat_id": "cd", "sender_name": "买家", "item_id": "2001", "order_id": "O3"})
check("发货-库存耗尽后失败", ok2 and not ok3)

# ---------- AI client (stub server) ----------
captured = {}
class StubAI(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        captured["body"] = body
        out = json.dumps({"choices": [{"message": {"content": "用了大概半年，功能都正常。"}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

srv = HTTPServer(("127.0.0.1", 0), StubAI)
threading.Thread(target=srv.serve_forever, daemon=True).start()
ai = AIClient(f"http://127.0.0.1:{srv.server_address[1]}/v1", "test-key", "test-model")
cfg_ai = base_cfg(); cfg_ai["ai"] = {"enabled": True}
e4, t4 = make_engine(ai=ai, cfg=cfg_ai)
r = e4.handle(msg("cai", "这个用了多久了", NOW))
check("AI-兜底回复", r["source"] == "ai" and "半年" in r["reply"])
sysmsg = captured["body"]["messages"][0]["content"]
check("AI-系统提示含底价约束", "160" in sysmsg and "K380" in sysmsg)
srv.shutdown()

# ---------- webui smoke ----------
proc = subprocess.Popen([sys.executable, "main.py", "--web", "--port", "8799"],
                        cwd=os.path.dirname(os.path.abspath(__file__)),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    import urllib.request
    ok = False
    for _ in range(40):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8799/", timeout=2) as resp:
                html = resp.read().decode("utf-8")
            ok = "管理后台" in html
            break
        except Exception:
            time.sleep(0.25)
    check("后台-首页可打开", ok)
    req = urllib.request.Request("http://127.0.0.1:8799/api/test",
                                 data=json.dumps({"text": "包邮吗", "item_id": "1001"}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        decision = json.loads(resp.read())
    check("后台-在线测试走规则", decision.get("source") == "rule")
finally:
    proc.terminate()

print(f"\n全部 {len(PASS)} 项测试通过 ✅" if all(c for _, c in PASS) else "\n存在失败 ❌")
