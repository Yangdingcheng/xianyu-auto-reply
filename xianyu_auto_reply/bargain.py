"""议价守卫：从买家消息里提取出价，按「底价 + 轮数 + 让价阶梯」做确定性决策。

设计原则：钱的事不交给大模型自由发挥。AI 只能润色话术，能不能卖、卖多少钱由这里说了算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BARGAIN_WORDS = ["便宜", "少点", "优惠", "刀", "砍价", "议价", "最低", "能少", "包邮吗", "出价", "卖不卖"]
_PRICE_RE = re.compile(r"(?:¥|￥)?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块钱|块|rmb|RMB)?")


def has_bargain_intent(text: str) -> bool:
    return any(w in (text or "") for w in BARGAIN_WORDS)


def extract_offer(text: str) -> float | None:
    """提取买家出价：取消息里最后一个合理的金额数字（5~100000）。"""
    nums = [float(m.group(1)) for m in _PRICE_RE.finditer(text or "")]
    nums = [n for n in nums if 5 <= n <= 100000]
    return nums[-1] if nums else None


@dataclass
class BargainDecision:
    action: str            # accept | counter | hold
    price: float | None    # accept/counter 时的价格
    rounds_used: int


class BargainGuard:
    def __init__(self, list_price: float, floor_price: float, max_rounds: int = 3,
                 step_pct: float = 0.03):
        self.list_price = float(list_price)
        self.floor_price = float(floor_price)
        self.max_rounds = int(max_rounds)
        self.step_pct = float(step_pct)

    def decide(self, offer: float, rounds_used: int) -> BargainDecision:
        # 本轮可接受的最低价（让价阶梯）：随轮次下降，但永不低于底价
        ask = max(self.floor_price,
                  round(self.list_price * (1 - self.step_pct * (rounds_used + 1))))
        if offer >= ask:
            return BargainDecision("accept", offer, rounds_used + 1)
        if rounds_used >= self.max_rounds:
            # 最后一轮：高于底价就成交，低于底价就守住
            if offer >= self.floor_price:
                return BargainDecision("accept", offer, rounds_used)
            return BargainDecision("hold", self.floor_price, rounds_used)
        return BargainDecision("counter", ask, rounds_used + 1)


TEMPLATES = {
    "accept": "行，就按你说的 {price} 元，拍下吧，我马上给你改价/发货～",
    "counter": "这个价真卖不了啦，最低 {price} 元，诚心要就拍，咱都爽快点。",
    "hold": "{price} 元是我的底线了亲，再低就亏本了，这个价全网都难找，考虑一下哈。",
}
