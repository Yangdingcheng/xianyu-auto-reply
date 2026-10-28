"""AI 客户端：OpenAI 兼容协议（DeepSeek / 通义千问 / OpenAI / 硅基流动等皆可）。

只用标准库实现，零依赖。调用失败返回空字符串，由引擎走默认回复兜底，绝不因 AI 挂掉而卡死。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_SYSTEM_PROMPT = """你是一位闲鱼卖家的客服助手，正在平台内和买家聊天。
当前商品：{item_title}，标价 {price} 元。
回复要求：
- 简短、口语化，像真人卖家，一般不超过 60 字，不用书面语和客服腔套话连篇。
- 只回答与当前商品、交易、发货、售后有关的问题；不知道的细节就说“我看一下，稍后回复你”，不要编造。
- 议价时可以 friendly 地周旋，但成交价绝不能低于 {floor_price} 元；低于这个价就礼貌表示卖不了。
- 遇到投诉、纠纷、退款争议、辱骂，或买家索要微信/电话等站外联系方式时，只回复 [NO_REPLY]，转人工处理。"""


class AIClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 system_prompt: str = DEFAULT_SYSTEM_PROMPT,
                 temperature: float = 0.7, timeout: int = 30):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def render_system(self, ctx: dict) -> str:
        out = self.system_prompt
        for k, v in (ctx or {}).items():
            out = out.replace("{" + k + "}", str(v))
        return out

    def chat(self, system: str, history: list[dict]) -> str:
        """history: [{role: user|assistant, content: str}]，返回回复文本，失败返回 ''。"""
        if not self.available():
            return ""
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + history,
            "temperature": self.temperature,
        }
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return str(data["choices"][0]["message"]["content"]).strip()
        except Exception:
            return ""
