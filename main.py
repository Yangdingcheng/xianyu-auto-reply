#!/usr/bin/env python3
"""闲鱼自动回复助手 · 入口

用法：
  python main.py --demo                离线演示（模拟买家全流程，无需账号）
  python main.py --test-reply "150卖不卖" --item 1001   单条消息决策测试
  python main.py --web [--port 8765]   打开本地管理后台（内置演示引擎）
  python main.py --live                真实连接闲鱼（读取 config.json 里启用的账号与本地 Cookie 文件）

配置：复制 config.example.json 为 config.json 改成你的；规则同理 rules.example.json → rules.json。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xianyu_auto_reply.ai_client import DEFAULT_SYSTEM_PROMPT, AIClient
from xianyu_auto_reply.cards import CardPool
from xianyu_auto_reply.engine import BotEngine, IncomingMessage
from xianyu_auto_reply.notify import Notifier
from xianyu_auto_reply.transport import DemoTransport

BASE = os.path.dirname(os.path.abspath(__file__))


def load_json(path: str, default):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def pick(base: str, *names: str) -> str:
    for n in names:
        p = os.path.join(base, n)
        if os.path.exists(p):
            return p
    return os.path.join(base, names[0])


def build_engine(cfg: dict, rules: list, transport, account_name: str = "") -> BotEngine:
    ai_cfg = cfg.get("ai", {})
    ai = None
    if ai_cfg.get("enabled", True):
        ai = AIClient(
            base_url=ai_cfg.get("base_url", ""),
            api_key=ai_cfg.get("api_key", ""),
            model=ai_cfg.get("model", ""),
            system_prompt=ai_cfg.get("system_prompt") or DEFAULT_SYSTEM_PROMPT,
            temperature=float(ai_cfg.get("temperature", 0.7)),
        )
    cards_file = cfg.get("global", {}).get("cards_file", "data/cards.json")
    if not os.path.isabs(cards_file):
        cards_file = os.path.join(BASE, cards_file)
    cards = CardPool(cards_file)
    notifier = Notifier(cfg.get("notify"))
    return BotEngine(cfg, rules, transport=transport, ai_client=ai,
                     card_pool=cards, notifier=notifier, account_name=account_name)


def run_demo(cfg: dict, rules: list):
    cfg = copy.deepcopy(cfg)
    cfg.setdefault("global", {})["work_hours"] = None  # 演示不被工作时间拦
    # 演示用临时卡密池（种入示例卡密），不消耗真实库存
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    example_cards = load_json(os.path.join(BASE, "cards.example.json"), {})
    json.dump(example_cards, tmp, ensure_ascii=False)
    tmp.close()
    cfg["global"]["cards_file"] = tmp.name

    transport = DemoTransport()
    engine = build_engine(cfg, rules, transport, account_name="演示")
    engine.fast = True

    clock = [time.time()]

    def say(chat, name, text, item=None, is_self=False):
        clock[0] += 30  # 演示时钟：每条消息间隔 30 秒，避免触发防刷冷却
        print(f"\n[买家{'' if not is_self else '(卖家本人)'}·{name}] {text}")
        d = engine.handle(IncomingMessage(chat_id=chat, sender_id="u_" + name,
                                          sender_name=name, text=text,
                                          item_id=item, is_self=is_self, ts=clock[0]))
        print(f"  → 决策：{d.get('action')}" +
              (f"（{d.get('source') or d.get('reason')}）" if d.get('action') != 'manual_pause' else ""))

    print("=" * 60)
    print("闲鱼自动回复助手 · 离线演示（商品 1001=K380 键盘 标价199/底价160；2001=会员月卡·自动发货）")
    print("=" * 60)
    say("c1", "小明", "在吗", "1001")
    say("c1", "小明", "包邮吗", "1001")
    say("c1", "小明", "能便宜点吗？150卖不卖", "1001")
    say("c1", "小明", "180行不行", "1001")
    say("c1", "小明", "185，诚心要", "1001")
    say("c2", "阿 May", "质量怎么样，是正品吗", "1001")
    say("c2", "阿 May", "加个微信聊吧", "1001")
    say("c3", "卡密买家", "买了怎么发货", "2001")
    print("\n[系统事件] 买家已付款，订单来了 → 自动发货")
    ok = engine.handle_order_paid({"chat_id": "c3", "sender_name": "卡密买家",
                                   "item_id": "2001", "order_id": "DEMO-0001"})
    print(f"  → 发货{'成功' if ok else '失败'}，卡密池剩余：{engine.cards.stock('2001')}")
    print("\n[系统事件] 卖家本人在手机上手动回复了 c1 → 该会话进入人工接管")
    say("c1", "店主", "这个我亲自跟你说", "1001", is_self=True)
    say("c1", "小明", "在吗", "1001")
    say("c4", "路人", "这个键盘用了多久了", "1001")
    print("\n" + "=" * 60)
    print("演示结束 · 统计：", engine.stats)
    print("决策日志已写入 logs/ 目录。")


def run_live(cfg: dict, rules: list):
    from xianyu_auto_reply.transport import XianyuTransport

    accounts = [a for a in cfg.get("accounts", []) if a.get("enabled", True)]
    if not accounts:
        print("config.json 里没有启用的账号。")
        return
    notifier = Notifier(cfg.get("notify"))
    transports = []
    for acc in accounts:
        name = acc.get("name", "账号")
        cookie_path = acc.get("cookie_file", "")
        if not os.path.isabs(cookie_path):
            cookie_path = os.path.join(BASE, cookie_path)
        if not os.path.exists(cookie_path):
            print(f"[{name}] 找不到 Cookie 文件：{cookie_path}（请把 Cookie 存到这个本地文件里）")
            continue
        cookie = open(cookie_path, encoding="utf-8").read().strip()
        transport = XianyuTransport(cookie, on_message=None)  # 占位，下面接引擎
        engine = build_engine(cfg, rules, transport, account_name=name)

        def make_cb(eng):
            def cb(m):
                eng.handle(IncomingMessage(account=eng.account, **m))
            return cb

        def make_ev(nm):
            def ev(event, detail):
                print(f"[{nm}] 事件 {event}: {detail}")
                notifier.notify(event, f"账号 {nm}: {detail}")
            return ev

        transport.on_message = make_cb(engine)
        transport.on_event = make_ev(name)
        transport.start()
        transports.append(transport)
        print(f"[{name}] 已启动，正在连接闲鱼消息通道…")
    if not transports:
        return
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        for t in transports:
            t.stop()
        print("已退出。")


def main():
    ap = argparse.ArgumentParser(description="闲鱼自动回复助手")
    ap.add_argument("--demo", action="store_true", help="离线演示")
    ap.add_argument("--live", action="store_true", help="真实连接闲鱼")
    ap.add_argument("--web", action="store_true", help="打开管理后台")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--test-reply", type=str, default="", help="单条消息决策测试")
    ap.add_argument("--item", type=str, default=None, help="配合 --test-reply 的商品ID")
    ap.add_argument("--config", type=str, default="")
    ap.add_argument("--rules", type=str, default="")
    args = ap.parse_args()

    cfg_path = args.config or pick(BASE, "config.json", "config.example.json")
    rules_path = args.rules or pick(BASE, "rules.json", "rules.example.json")
    cfg = load_json(cfg_path, {})
    rules = load_json(rules_path, [])
    print(f"配置：{os.path.basename(cfg_path)} · 规则：{os.path.basename(rules_path)}（{len(rules)} 条）")

    if args.demo:
        run_demo(cfg, rules)
    elif args.test_reply:
        cfg2 = copy.deepcopy(cfg)
        cfg2.setdefault("global", {})["work_hours"] = None
        engine = build_engine(cfg2, rules, DemoTransport())
        engine.fast = True
        d = engine.handle(IncomingMessage(chat_id="cli-test", sender_id="u-test",
                                           sender_name="测试买家", text=args.test_reply,
                                           item_id=args.item), commit=False)
        print(json.dumps(d, ensure_ascii=False, indent=2))
    elif args.web:
        from xianyu_auto_reply.webui import WebUI
        engine = build_engine(cfg, rules, DemoTransport(), account_name="后台演示")
        engine.fast = True
        WebUI(engine, rules_path=rules_path).serve(port=args.port)
    elif args.live:
        try:
            import websockets  # noqa: F401
        except ImportError:
            print("真实模式需要 websockets：pip install -r requirements.txt")
            return
        run_live(cfg, rules)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
