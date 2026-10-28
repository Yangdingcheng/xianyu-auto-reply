"""传输层：引擎只依赖 send_text() 与消息回调，传输实现可替换。

- DemoTransport：离线演示/测试用，把「发送」记录下来并打印。
- XianyuTransport：真实接入。流程依据公开开源实现整理：
    Cookie → MTOP(mtop.taobao.idlemessage.pc.login.token) 换 IM token
    → WebSocket wss://wss-goofish.dingtalk.com/ 以 /reg 注册 → 15s 心跳 /! →
    同步包解析出买家消息 → /r/MessageSend/sendByReceiverScope 发送。
  平台协议会变：解析层做了多路兜底（递归找字段），原始帧可开 debug 落盘便于联调修正。
  Cookie 只从本地文件读取；触发风控/登录失效会回调 on_event 并停止重连狂试。
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import urllib.parse
import urllib.request
import uuid

APP_KEY_SIGN = "34839810"
TOKEN_API = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
WS_URL = "wss://wss-goofish.dingtalk.com/"
IM_APP_KEY = "444e9908a51d1cb236a27862abc769c9"


class Transport:
    def send_text(self, chat_id: str, text: str, item_id: str | None = None):
        raise NotImplementedError


class DemoTransport(Transport):
    def __init__(self):
        self.sent: list[dict] = []

    def send_text(self, chat_id: str, text: str, item_id: str | None = None):
        self.sent.append({"chat_id": chat_id, "text": text, "item_id": item_id})
        print(f"  [发送→{chat_id}] {text}")


# ---------------- MTOP ----------------

def _cookie_get(cookie: str, name: str) -> str:
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith(name + "="):
            return part[len(name) + 1:]
    return ""


def mtop_sign(cookie: str, t: str, data: str) -> str:
    token = _cookie_get(cookie, "_m_h5_tk").split("_")[0]
    raw = f"{token}&{t}&{APP_KEY_SIGN}&{data}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def fetch_im_token(cookie: str, device_id: str) -> str:
    """用 Cookie 调 MTOP 换取 IM 接入 token；失败抛异常（含平台返回原文便于排查）。"""
    data = json.dumps({"appKey": IM_APP_KEY, "deviceId": device_id}, separators=(",", ":"))
    t = str(int(time.time() * 1000))
    sign = mtop_sign(cookie, t, data)
    url = (f"{TOKEN_API}?jsv=2.7.2&appKey={APP_KEY_SIGN}&t={t}&sign={sign}"
           f"&api=mtop.taobao.idlemessage.pc.login.token&v=1.0&type=originaljson")
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode({"data": data}).encode("utf-8"),
        headers={"cookie": cookie, "content-type": "application/x-www-form-urlencoded",
                 "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    ret = (body.get("ret") or [""])[0]
    if "SUCCESS" not in ret:
        raise RuntimeError(f"MTOP token 获取失败: {ret} {body.get('data')}")
    return body["data"]["accessToken"]


# ---------------- 入站解析（多路兜底） ----------------

def _walk(obj, depth=0):
    """递归遍历 JSON 对象图，产出所有 dict。"""
    if depth > 8:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, depth + 1)


def _decode_layers(raw: str):
    """尝试把字符串解析为 JSON；若是 base64 再解一层。返回对象或 None。"""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        return None


def extract_messages(frame: dict, my_id: str) -> list[dict]:
    """从一帧 WS 同步数据里尽量提取消息。返回 [{chat_id, sender_id, sender_name, text, item_id, is_self}]。"""
    out = []
    seen = set()
    for node in _walk(frame):
        text = None
        for key in ("reminderContent",):
            if isinstance(node.get(key), str) and node[key].strip():
                text = node[key].strip()
        if not text and isinstance(node.get("content"), dict):
            t = node["content"].get("text")
            if isinstance(t, dict) and isinstance(t.get("text"), str):
                text = t["text"]
            elif isinstance(t, str):
                text = t
        if not text and isinstance(node.get("text"), str) and ("sender" in json.dumps(node)[:200] or "chat" in node or "session" in node):
            text = node["text"]
        if not text:
            continue
        sender = str(node.get("senderUserId") or node.get("fromUserId") or node.get("senderId") or "")
        chat = str(node.get("sessionId") or node.get("conversationId") or node.get("cid") or node.get("chatId") or "")
        if not chat and sender:
            chat = sender
        key = (chat, sender, text)
        if key in seen or not text:
            continue
        seen.add(key)
        out.append({
            "chat_id": chat.replace("@goofish", ""),
            "sender_id": sender,
            "sender_name": str(node.get("senderNick") or node.get("nick") or node.get("senderName") or ""),
            "text": text,
            "item_id": str(node.get("itemId") or node.get("item_id") or "") or None,
            "is_self": bool(my_id and sender == my_id),
        })
    return out


# ---------------- 真实传输 ----------------

class XianyuTransport(Transport):
    def __init__(self, cookie: str, on_message, on_event=None, log=print):
        self.cookie = cookie
        self.on_message = on_message      # callback(dict) → 引擎处理
        self.on_event = on_event or (lambda ev, detail: None)
        self.log = log
        self.my_id = _cookie_get(cookie, "unb") or _cookie_get(cookie, "munb")
        self.device_id = uuid.uuid4().hex
        self._ws = None
        self._loop = None
        self._stop = False
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()

    # ---- 对外 ----
    def start(self):
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True

    def send_text(self, chat_id: str, text: str, item_id: str | None = None):
        if not self._ws or not self._loop:
            self.log("[xianyu] WS 未连接，消息未发送")
            return
        import asyncio
        asyncio.run_coroutine_threadsafe(self._send_text_async(chat_id, text), self._loop)

    # ---- 内部 ----
    def _run_thread(self):
        import asyncio
        asyncio.run(self._main())

    async def _main(self):
        import asyncio
        backoff = 5
        while not self._stop:
            try:
                await self._session()
                backoff = 5
            except Exception as e:
                self.log(f"[xianyu] 连接异常: {e}")
                self.on_event("connection_error", str(e))
                if "FAIL_SYS_TOKEN" in str(e) or "令牌" in str(e) or "登录" in str(e):
                    self.on_event("login_expired", "Cookie 可能已失效，请更新后重启")
                    return  # 登录失效不狂试，避免触发风控
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    async def _session(self):
        import asyncio

        import websockets

        token = fetch_im_token(self.cookie, self.device_id)
        headers = {"cookie": self.cookie,
                   "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                   "origin": "https://www.goofish.com"}
        async with websockets.connect(WS_URL, additional_headers=headers) as ws:
            self._ws = ws
            self._loop = asyncio.get_running_loop()
            reg = {"lwp": "/reg", "headers": {
                "cache-header": "app-key token ua wv", "app-key": IM_APP_KEY,
                "token": token, "ua": headers["user-agent"], "dt": "j",
                "wv": "im:3,au:3,sy:6", "sync": "0,0;0;0;",
                "did": self.device_id, "mid": f"{uuid.uuid4().hex} 0"}}
            await ws.send(json.dumps(reg))
            self.on_event("connected", "闲鱼消息通道已连接")
            hb = asyncio.create_task(self._heartbeat(ws))
            try:
                async for raw in ws:
                    await self._on_frame(ws, raw)
            finally:
                hb.cancel()
                self._ws = None

    async def _heartbeat(self, ws):
        import asyncio
        while not self._stop:
            await asyncio.sleep(15)
            try:
                await ws.send(json.dumps({"lwp": "/!", "headers": {"mid": f"{uuid.uuid4().hex} 0"}}))
            except Exception:
                return

    async def _on_frame(self, ws, raw):
        try:
            frame = json.loads(raw)
        except Exception:
            return
        # 解开 syncPushPackage 的多层编码
        candidates = [frame]
        body = frame.get("body") if isinstance(frame, dict) else None
        if isinstance(body, dict):
            pkg = body.get("syncPushPackage") or {}
            for item in pkg.get("data") or []:
                decoded = _decode_layers(item.get("data")) if isinstance(item, dict) else None
                if decoded is not None:
                    candidates.append(decoded)
        for cand in candidates:
            for m in extract_messages(cand, self.my_id):
                try:
                    self.on_message(m)
                except Exception as e:
                    self.log(f"[xianyu] 消息处理异常: {e}")
        # ACK 同步包，避免平台重复推送
        hdrs = frame.get("headers") if isinstance(frame, dict) else None
        if isinstance(hdrs, dict) and hdrs.get("mid"):
            try:
                await ws.send(json.dumps({"code": 200, "headers": {
                    "mid": hdrs["mid"], "sid": hdrs.get("sid", "")}}))
            except Exception:
                pass

    async def _send_text_async(self, chat_id: str, text: str):
        content = {"contentType": 1, "text": {"text": text}}
        data_b64 = base64.b64encode(json.dumps(content, ensure_ascii=False).encode("utf-8")).decode()
        body = [{
            "uuid": uuid.uuid4().hex,
            "cid": f"{chat_id}@goofish",
            "conversationType": 1,
            "content": {"contentType": 101, "custom": {"type": 1, "data": data_b64}},
            "actualReceivers": [f"{chat_id}@goofish", f"{self.my_id}@goofish"],
        }]
        msg = {"lwp": "/r/MessageSend/sendByReceiverScope",
               "headers": {"mid": f"{uuid.uuid4().hex} 0"}, "body": body}
        with self._send_lock:
            await self._ws.send(json.dumps(msg, ensure_ascii=False))
