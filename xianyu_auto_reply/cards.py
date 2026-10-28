"""卡密池：按商品存放虚拟商品（卡密/兑换码/文本发货内容），付款后原子取用。

存储为 JSON 文件：{item_id: [{"code": "...", "used": false, "used_by": "", "used_at": ""}]}
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime


class CardPool:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, list[dict]] = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def _save(self):
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, item_id: str, codes: list[str]) -> int:
        with self._lock:
            pool = self._data.setdefault(str(item_id), [])
            existing = {c["code"] for c in pool}
            added = 0
            for code in codes:
                code = str(code).strip()
                if code and code not in existing:
                    pool.append({"code": code, "used": False, "used_by": "", "used_at": ""})
                    added += 1
            self._save()
            return added

    def stock(self, item_id: str) -> int:
        return sum(1 for c in self._data.get(str(item_id), []) if not c["used"])

    def take(self, item_id: str, buyer: str = "") -> str | None:
        """取一条未用卡密并标记已用；池空返回 None。"""
        with self._lock:
            for c in self._data.get(str(item_id), []):
                if not c["used"]:
                    c["used"] = True
                    c["used_by"] = buyer
                    c["used_at"] = datetime.now().isoformat(timespec="seconds")
                    self._save()
                    return c["code"]
            return None

    def overview(self) -> dict[str, dict]:
        out = {}
        for item_id, pool in self._data.items():
            used = sum(1 for c in pool if c["used"])
            out[item_id] = {"total": len(pool), "left": len(pool) - used, "used": used}
        return out
