"""按天计数（北京时间零点重置），落盘、加锁。用于搜索/生图等每日上限。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class DailyCounter:
    def __init__(self, path: Path | str | None = None, clock=time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self._lock = threading.Lock()
        self._d = {"day": "", "n": {}}
        if self.path and self.path.exists():
            try:
                self._d = json.loads(self.path.read_text())
            except (OSError, ValueError):
                pass

    def _today(self):
        day = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        if self._d.get("day") != day:
            self._d = {"day": day, "n": {}}
        return self._d["n"]

    def get(self, key: str) -> int:
        with self._lock:
            return self._today().get(key, 0)

    def take(self, limits: dict[str, int]) -> bool:
        """所有 key 都没超上限才一起 +1；否则不动并返回 False。"""
        with self._lock:
            n = self._today()
            if any(n.get(k, 0) >= lim for k, lim in limits.items()):
                return False
            for k in limits:
                n[k] = n.get(k, 0) + 1
            self._save()
            return True

    def refund(self, keys) -> None:
        with self._lock:
            n = self._today()
            for k in keys:
                if n.get(k, 0) > 0:
                    n[k] -= 1
            self._save()

    def _save(self):
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._d)); os.replace(tmp, self.path)
        except OSError:
            pass
