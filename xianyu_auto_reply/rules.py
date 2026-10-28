"""关键词规则引擎。

规则结构（JSON 数组，每条）：
{
  "id": "postage",
  "name": "包邮问题",
  "item_id": null,                # null=全局规则；填商品ID则仅对该商品生效
  "match_type": "contains",       # exact | contains | regex
  "keywords": ["包邮", "邮费"],
  "reply": "亲，本店默认包邮哦" ,   # 字符串或字符串数组（数组时随机选一条）
  "priority": 50,                 # 越大越先匹配
  "enabled": true
}

回复文本支持变量：{nickname} {item_title} {price} {item_id}，由调用方传入 ctx。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field


@dataclass
class Rule:
    id: str
    name: str = ""
    item_id: str | None = None
    match_type: str = "contains"
    keywords: list[str] = field(default_factory=list)
    reply: str | list[str] = ""
    priority: int = 0
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        return cls(
            id=str(d.get("id") or d.get("name") or "rule"),
            name=d.get("name", ""),
            item_id=(str(d["item_id"]) if d.get("item_id") else None),
            match_type=d.get("match_type", "contains"),
            keywords=[str(k) for k in d.get("keywords", [])],
            reply=d.get("reply", ""),
            priority=int(d.get("priority", 0)),
            enabled=bool(d.get("enabled", True)),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "item_id": self.item_id,
            "match_type": self.match_type, "keywords": self.keywords,
            "reply": self.reply, "priority": self.priority, "enabled": self.enabled,
        }

    def hit(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        for kw in self.keywords:
            if self.match_type == "exact":
                if t == kw:
                    return True
            elif self.match_type == "regex":
                try:
                    if re.search(kw, t):
                        return True
                except re.error:
                    continue
            else:  # contains
                if kw and kw in t:
                    return True
        return False

    def render(self, ctx: dict | None = None) -> str:
        r = random.choice(self.reply) if isinstance(self.reply, list) and self.reply else self.reply
        out = str(r or "")
        for k, v in (ctx or {}).items():
            out = out.replace("{" + k + "}", str(v))
        return out


class RuleEngine:
    def __init__(self, rules: list[dict | Rule]):
        self.rules: list[Rule] = [
            r if isinstance(r, Rule) else Rule.from_dict(r) for r in rules
        ]
        self.rules.sort(key=lambda r: -r.priority)

    def match(self, text: str, item_id: str | None = None,
              ctx: dict | None = None) -> tuple[Rule, str] | None:
        """先查商品专属规则，再查全局规则；同层按 priority 高者优先。"""
        item_rules = [r for r in self.rules if r.item_id and item_id and r.item_id == str(item_id)]
        global_rules = [r for r in self.rules if not r.item_id]
        for rule in item_rules + global_rules:
            if rule.enabled and rule.hit(text):
                return rule, rule.render(ctx)
        return None

    def add(self, rule: dict) -> Rule:
        r = Rule.from_dict(rule)
        self.rules.append(r)
        self.rules.sort(key=lambda x: -x.priority)
        return r

    def remove(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.id != rule_id]
        return len(self.rules) != before

    def to_list(self) -> list[dict]:
        return [r.to_dict() for r in self.rules]
