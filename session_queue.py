"""按会话并发：每个群（私聊=对方 wxid）一条 FIFO 队列 + 一个工作线程。
同一个群严格保序（上一条处理完才处理下一条），不同群并行——一个群在调模型/工具时不再堵其它群。
参考 CoW/DoW channel/chat_channel.py 的 produce/consume（每 session 一个队列 + 线程池），按我们的需要简化：
  - 工作线程按需创建，空闲 IDLE_EXIT 秒自动退出；同时活跃的会话线程数上限 MAX_SESSIONS（超出的会话排队等空位）。
  - 每条任务记录排队耗时（入队→开始）和处理耗时，交给回调写日志。
  - drain(timeout)：退出前等队列清空。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Callable

log = logging.getLogger("queue")
MAX_SESSIONS = int(os.environ.get("BOT_MAX_SESSIONS", "8"))
IDLE_EXIT = float(os.environ.get("BOT_SESSION_IDLE_EXIT", "120"))


class SessionQueues:
    def __init__(self, max_sessions: int = MAX_SESSIONS, idle_exit: float = IDLE_EXIT, clock=time.time):
        self.max_sessions, self.idle_exit, self.clock = max_sessions, idle_exit, clock
        self._q: dict[str, deque] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._cv = threading.Condition()
        self._slots = threading.BoundedSemaphore(max_sessions)
        self._busy = 0

    def submit(self, key: str, fn: Callable[[float], None]) -> int:
        """fn(queued_sec) 在该会话的工作线程里执行。返回提交时该会话队列长度（含本条）。"""
        key = key or "-"
        with self._cv:
            q = self._q.setdefault(key, deque())
            q.append((self.clock(), fn))
            n = len(q)
            w = self._workers.get(key)
            if w is None or not w.is_alive():
                w = threading.Thread(target=self._run, args=(key,), daemon=True, name=f"sess-{key[:20]}")
                self._workers[key] = w
                w.start()
            self._cv.notify_all()
        return n

    def _run(self, key: str):
        with self._slots:                                   # 活跃会话数上限
            while True:
                with self._cv:
                    q = self._q.get(key)
                    end = self.clock() + self.idle_exit
                    while not q:
                        left = end - self.clock()
                        if left <= 0:
                            if self._workers.get(key) is threading.current_thread():
                                self._workers.pop(key, None)
                                self._q.pop(key, None)
                            return
                        self._cv.wait(timeout=min(left, 1.0))
                        q = self._q.get(key)
                    t_in, fn = q.popleft()
                    self._busy += 1
                try:
                    fn(self.clock() - t_in)
                except Exception:
                    log.exception("会话 %s 任务失败（不影响后续）", key)
                finally:
                    with self._cv:
                        self._busy -= 1
                        self._cv.notify_all()

    def pending(self) -> int:
        with self._cv:
            return sum(len(q) for q in self._q.values()) + self._busy

    def drain(self, timeout: float = 20.0) -> bool:
        end = self.clock() + timeout
        with self._cv:
            while sum(len(q) for q in self._q.values()) + self._busy > 0:
                left = end - self.clock()
                if left <= 0:
                    return False
                self._cv.wait(timeout=min(left, 0.5))
        return True
