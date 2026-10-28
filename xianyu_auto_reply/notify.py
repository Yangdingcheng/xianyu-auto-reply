"""消息通知：钉钉 / 企业微信 Webhook（标准库实现）。

用于把「新订单已发货、卡密不足、登录失效、AI 转人工」等事件推给卖家本人。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request


def _post(url: str, payload: dict) -> bool:
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def send_dingtalk(webhook: str, text: str, secret: str = "") -> bool:
    url = webhook
    if secret:
        ts = str(round(time.time() * 1000))
        sign_raw = f"{ts}\n{secret}".encode("utf-8")
        sign = urllib.parse.quote_plus(
            base64.b64encode(hmac.new(secret.encode("utf-8"), sign_raw, hashlib.sha256).digest()))
        url = f"{webhook}&timestamp={ts}&sign={sign}" if "?" in webhook else f"{webhook}?timestamp={ts}&sign={sign}"
    return _post(url, {"msgtype": "text", "text": {"content": text}})


def send_wecom(webhook: str, text: str) -> bool:
    return _post(webhook, {"msgtype": "text", "text": {"content": text}})


class Notifier:
    def __init__(self, cfg: dict | None):
        cfg = cfg or {}
        self.dingtalk = cfg.get("dingtalk_webhook", "")
        self.dingtalk_secret = cfg.get("dingtalk_secret", "")
        self.wecom = cfg.get("wecom_webhook", "")

    def notify(self, event: str, text: str) -> None:
        msg = f"[闲鱼助手·{event}] {text}"
        if self.dingtalk:
            send_dingtalk(self.dingtalk, msg, self.dingtalk_secret)
        if self.wecom:
            send_wecom(self.wecom, msg)
