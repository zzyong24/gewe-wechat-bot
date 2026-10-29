"""全局发送限速（开源版新增）：所有真发都经过 gewe_client.call 的发送类接口，在这里统一排队。

- 全局：任意 60 秒滑动窗口内最多 BOT_RATE_PER_MIN 条（默认 40；0 = 不限）。
- 群消息：同一个群相邻两条之间随机间隔 BOT_RATE_GROUP_GAP_MIN ~ BOT_RATE_GROUP_GAP_MAX 秒（默认 2~5；都设 0 = 不限）。
超限时阻塞等待（不丢消息）；单条最长等 BOT_RATE_MAX_WAIT 秒（默认 120），超过抛 RateLimitTimeout，调用方按发送失败处理。
线程安全：poller 的按群并发队列会多线程同时发送。
"""
from __future__ import annotations

import os
import random
import threading
import time
from collections import deque


class RateLimitTimeout(RuntimeError):
    pass


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class SendLimiter:
    def __init__(self, per_min: int | None = None, gap_min: float | None = None, gap_max: float | None = None,
                 max_wait: float | None = None, clock=time.monotonic, sleep=time.sleep, rand=random.uniform):
        self.per_min = int(_f("BOT_RATE_PER_MIN", 40) if per_min is None else per_min)
        self.gap_min = _f("BOT_RATE_GROUP_GAP_MIN", 2) if gap_min is None else gap_min
        self.gap_max = _f("BOT_RATE_GROUP_GAP_MAX", 5) if gap_max is None else gap_max
        if self.gap_max < self.gap_min:
            self.gap_max = self.gap_min
        self.max_wait = _f("BOT_RATE_MAX_WAIT", 120) if max_wait is None else max_wait
        self.clock, self.sleep, self.rand = clock, sleep, rand
        self._lock = threading.Lock()
        self._sent: deque[float] = deque()          # 最近 60 秒内的发送时间
        self._next_ok: dict[str, float] = {}         # 群 → 下一条最早可发时间

    def _wait_needed(self, to: str, now: float) -> float:
        while self._sent and now - self._sent[0] >= 60:
            self._sent.popleft()
        w = 0.0
        if self.per_min > 0 and len(self._sent) >= self.per_min:
            w = max(w, 60 - (now - self._sent[0]))
        if to.endswith("@chatroom"):
            w = max(w, self._next_ok.get(to, 0) - now)
        return w

    def acquire(self, to: str) -> float:
        """阻塞到可以发给 to 为止，返回实际等了多少秒。"""
        start = self.clock()
        while True:
            with self._lock:
                now = self.clock()
                w = self._wait_needed(to, now)
                if w <= 0:
                    self._sent.append(now)
                    if to.endswith("@chatroom") and self.gap_max > 0:
                        self._next_ok[to] = now + self.rand(self.gap_min, self.gap_max)
                    return now - start
            if now - start + w > self.max_wait:
                raise RateLimitTimeout(f"发送限速：等待超过 {self.max_wait:.0f} 秒")
            self.sleep(min(w, 1.0))


_default: SendLimiter | None = None
_dlock = threading.Lock()


def default() -> SendLimiter:
    global _default
    with _dlock:
        if _default is None:
            _default = SendLimiter()
        return _default


def reset_default() -> None:
    """测试用：环境变量改了之后重建。"""
    global _default
    with _dlock:
        _default = None
