"""回复引擎：一条买家消息的完整决策流水线。

顺序（先到先停）：
  卖家手动消息 → 人工接管暂停该会话
  黑名单 → 跳过
  会话处于人工接管期 → 跳过
  非工作时间 → 离线回复（限频）或跳过
  单会话冷却中 → 跳过（防刷屏连环回复）
  关键词规则（商品专属 → 全局） → 回复
  议价守卫（买家出价时，确定性决策） → 回复
  AI 回复（带上下文与底价约束；返回 [NO_REPLY] 则转人工） → 回复
  默认回复（同一会话 24 小时只发一次） → 回复
  否则 → 不回复，仅记录

每次决策都写入 JSONL 日志（logs/replies-<账号>.jsonl），后台可查「为什么回/为什么没回」。
"""

from __future__ import annotations

import copy
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from .bargain import TEMPLATES as BARGAIN_TEMPLATES
from .bargain import BargainGuard, extract_offer, has_bargain_intent
from .rules import RuleEngine


@dataclass
class IncomingMessage:
    account: str = ""
    chat_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    text: str = ""
    item_id: str | None = None
    is_self: bool = False
    ts: float = field(default_factory=time.time)


def _fmt_price(v) -> str:
    return ("%g" % float(v)) if v is not None else ""


class BotEngine:
    def __init__(self, config: dict, rules: list, transport=None, ai_client=None,
                 card_pool=None, notifier=None, account_name: str = ""):
        self.cfg = config or {}
        self.g = self.cfg.get("global", {})
        self.ai_cfg = self.cfg.get("ai", {})
        self.rules = RuleEngine(rules or [])
        self.transport = transport
        self.ai = ai_client
        self.cards = card_pool
        self.notifier = notifier
        self.account = account_name
        self.items = {str(k): v for k, v in self.cfg.get("items", {}).items()}
        self.blacklist = set(str(x) for x in self.cfg.get("blacklist", []))
        self.fast = False  # 演示/测试时不真实等待随机延迟
        self._lock = threading.RLock()
        self._paused_until: dict[str, float] = {}
        self._last_reply: dict[str, float] = {}
        self._default_sent: dict[str, float] = {}
        self._offhours_sent: dict[str, float] = {}
        self._history: dict[str, list[dict]] = {}
        self._bargain_rounds: dict[tuple, int] = {}
        self.stats = {"received": 0, "replied": 0, "rule": 0, "ai": 0,
                      "bargain": 0, "default": 0, "skipped": 0, "delivered": 0}
        log_dir = self.g.get("log_dir", "logs")
        os.makedirs(log_dir, exist_ok=True)
        self.log_path = os.path.join(log_dir, f"replies-{account_name or 'default'}.jsonl")

    # ---------------- 基础设施 ----------------
    def _log(self, entry: dict):
        entry = {"time": datetime.now().isoformat(timespec="seconds"),
                 "account": self.account, **entry}
        with self._lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def _in_work_hours(self, now: float | None = None) -> bool:
        wh = self.g.get("work_hours")
        if not wh:
            return True
        h = datetime.fromtimestamp(now or time.time()).hour
        a, b = int(wh[0]), int(wh[1])
        return a <= h < b if a <= b else (h >= a or h < b)

    def _ctx(self, msg: IncomingMessage) -> dict:
        item = self.items.get(str(msg.item_id), {}) if msg.item_id else {}
        return {
            "nickname": msg.sender_name or "亲",
            "item_title": item.get("title", ""),
            "price": _fmt_price(item.get("price")) if item.get("price") is not None else "",
            "floor_price": _fmt_price(item.get("floor_price")) if item.get("floor_price") is not None else "",
            "item_id": msg.item_id or "",
        }

    def _send(self, msg: IncomingMessage, text: str, source: str, extra: dict | None = None) -> dict:
        lo, hi = self.g.get("delay_seconds", [0, 0]) or [0, 0]
        delay = random.uniform(float(lo), float(hi)) if hi > lo else float(lo)
        if delay > 0 and not self.fast:
            time.sleep(delay)
        if self.transport:
            self.transport.send_text(msg.chat_id, text, item_id=msg.item_id)
        now = time.time()
        with self._lock:
            self._last_reply[msg.chat_id] = now
            self.stats["replied"] += 1
            self.stats[source] = self.stats.get(source, 0) + 1
            hist = self._history.setdefault(msg.chat_id, [])
            hist.append({"role": "assistant", "content": text})
            del hist[:-20]
        decision = {"action": "reply", "source": source, "reply": text,
                    "chat_id": msg.chat_id, "delay": round(delay, 1), **(extra or {})}
        self._log({**decision, "incoming": msg.text, "sender": msg.sender_name})
        return decision

    def _skip(self, msg: IncomingMessage, reason: str) -> dict:
        with self._lock:
            self.stats["skipped"] += 1
        decision = {"action": "skip", "reason": reason, "chat_id": msg.chat_id}
        self._log({**decision, "incoming": msg.text, "sender": msg.sender_name})
        return decision

    # ---------------- 主流水线 ----------------
    def handle(self, msg: IncomingMessage, commit: bool = True) -> dict:
        if not commit:  # 干跑：快照状态，跑完还原（用于后台在线测试）
            snap = (dict(self._paused_until), dict(self._last_reply), dict(self._default_sent),
                    dict(self._offhours_sent), copy.deepcopy(self._history),
                    dict(self._bargain_rounds), dict(self.stats))
            saved_transport, self.transport = self.transport, None
            saved_fast, self.fast = self.fast, True
            try:
                return self.handle(msg, commit=True)
            finally:
                (self._paused_until, self._last_reply, self._default_sent,
                 self._offhours_sent, self._history, self._bargain_rounds,
                 self.stats) = snap
                self.transport, self.fast = saved_transport, saved_fast

        now = msg.ts or time.time()
        with self._lock:
            self.stats["received"] += 1

        if msg.is_self:  # 卖家在别处手动回了 → 本会话暂停自动回复
            pause = float(self.g.get("manual_pause_minutes", 10)) * 60
            self._paused_until[msg.chat_id] = now + pause
            self._log({"action": "manual_pause", "chat_id": msg.chat_id,
                       "minutes": pause / 60, "incoming": msg.text})
            return {"action": "manual_pause", "chat_id": msg.chat_id}

        if str(msg.sender_id) in self.blacklist:
            return self._skip(msg, "blacklist")
        if now < self._paused_until.get(msg.chat_id, 0):
            return self._skip(msg, "manual_takeover")

        if not self._in_work_hours(now):
            off = self.cfg.get("off_hours_reply")
            if off and now - self._offhours_sent.get(msg.chat_id, 0) > 6 * 3600:
                self._offhours_sent[msg.chat_id] = now
                return self._send(msg, off, "default")
            return self._skip(msg, "off_hours")

        cooldown = float(self.g.get("cooldown_seconds", 0))
        if cooldown and now - self._last_reply.get(msg.chat_id, 0) < cooldown:
            return self._skip(msg, "cooldown")

        with self._lock:
            hist = self._history.setdefault(msg.chat_id, [])
            hist.append({"role": "user", "content": msg.text})
            del hist[:-20]
        ctx = self._ctx(msg)

        # 1) 关键词规则
        hit = self.rules.match(msg.text, msg.item_id, ctx)
        if hit:
            rule, reply = hit
            return self._send(msg, reply, "rule", {"rule_id": rule.id})

        # 2) 议价守卫
        item = self.items.get(str(msg.item_id), {}) if msg.item_id else {}
        bconf = item.get("bargain") or {}
        if bconf.get("enabled") and item.get("price") is not None and item.get("floor_price") is not None:
            offer = extract_offer(msg.text)
            if offer is not None and (has_bargain_intent(msg.text) or offer < float(item["price"])):
                guard = BargainGuard(float(item["price"]), float(item["floor_price"]),
                                     int(bconf.get("max_rounds", 3)))
                key = (msg.chat_id, str(msg.item_id))
                rounds = self._bargain_rounds.get(key, 0)
                d = guard.decide(offer, rounds)
                self._bargain_rounds[key] = d.rounds_used
                text = BARGAIN_TEMPLATES[d.action].replace("{price}", _fmt_price(d.price))
                return self._send(msg, text, "bargain",
                                  {"bargain_action": d.action, "offer": offer, "price": d.price})

        # 3) AI 回复
        if self.ai_cfg.get("enabled", True) and self.ai and self.ai.available():
            system = self.ai.render_system(ctx)
            with self._lock:
                history = list(self._history.get(msg.chat_id, []))
            reply = self.ai.chat(system, history)
            if reply and "[NO_REPLY]" not in reply:
                return self._send(msg, reply, "ai")
            if reply and self.notifier:
                self.notifier.notify("转人工", f"买家「{msg.sender_name}」的消息需要人工处理：{msg.text}")

        # 4) 默认回复（同会话 24h 一次）
        default = self.cfg.get("default_reply")
        if default and now - self._default_sent.get(msg.chat_id, 0) > 24 * 3600:
            self._default_sent[msg.chat_id] = now
            return self._send(msg, default, "default")
        return self._skip(msg, "no_match")

    # ---------------- 订单事件 ----------------
    def handle_order_paid(self, order: dict) -> bool:
        """付款 → 自动发货。order: {chat_id, sender_name, item_id, order_id}"""
        chat_id = str(order.get("chat_id", ""))
        item_id = str(order.get("item_id", ""))
        buyer = order.get("sender_name", "")
        if not self.cards:
            self._log({"action": "deliver_failed", "reason": "no_card_pool", "order": order})
            return False
        code = self.cards.take(item_id, buyer)
        if not code:
            if self.notifier:
                self.notifier.notify("卡密不足", f"商品 {item_id} 有新订单但卡密池已空，请补货！订单 {order.get('order_id', '')}")
            self._log({"action": "deliver_failed", "reason": "out_of_stock", "order": order})
            return False
        tpl = self.cfg.get("delivery_template") or "亲，付款成功啦！这是您的卡密：{code}，请妥善保管，祝使用愉快～"
        text = tpl.replace("{code}", code).replace("{item_title}", self.items.get(item_id, {}).get("title", ""))
        fake = IncomingMessage(chat_id=chat_id, item_id=item_id, sender_name=buyer)
        self._send(fake, text, "default", {"delivery": True})
        with self._lock:
            self.stats["delivered"] += 1
        left = self.cards.stock(item_id)
        threshold = int(self.g.get("low_stock_alert", 3))
        if left <= threshold and self.notifier:
            self.notifier.notify("卡密预警", f"商品 {item_id} 卡密仅剩 {left} 条，请及时补货。")
        self._log({"action": "delivered", "order": order, "stock_left": left})
        return True

    def handle_order_completed(self, order: dict) -> bool:
        """确认收货 → 致谢消息（配置了 thanks_reply 才发）。"""
        thanks = self.cfg.get("thanks_reply")
        if not thanks:
            return False
        fake = IncomingMessage(chat_id=str(order.get("chat_id", "")),
                               item_id=str(order.get("item_id", "")),
                               sender_name=order.get("sender_name", ""))
        self._send(fake, thanks, "default", {"thanks": True})
        return True
