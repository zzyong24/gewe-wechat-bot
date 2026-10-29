"""消息防抖：同一人在同一会话 BOT_DEBOUNCE_SEC（默认 2.5）秒内连发多条、且都是对小助手说的 → 合并成一条再处理。

在 poller 拉取线程里做（进会话队列之前），这样后面的消息能在等待期间并进来：
  - 候选：文字消息（type 1）、非自己、非忽略名单、白名单非观察会话，且：私聊 / @小助手 / 发言人在追问窗口内 /
    此人已有一批在等（紧接着补的话）。
  - 每来一条候选就把这批的截止时间往后推 DEBOUNCE 秒；从第一条算最多等 BOT_DEBOUNCE_MAX（默认 8）秒，防止一直不回。
  - 合并：以第一条为底（msg_id/raw 不变），content 用换行拼接；@ 列表取并集；其余各条 id 记在 merged_ids。
  - 同一人同一会话来了非候选消息（图片/语音/引用卡片等）→ 先把这批立刻放行，保证顺序。
  - BOT_DEBOUNCE_SEC=0 关闭。退出前 flush_all()。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

log = logging.getLogger("debounce")


class Debouncer:
    def __init__(self, emit: Callable, is_candidate: Callable, delay: float | None = None, max_wait: float | None = None,
                 clock=time.time, timer_factory=None):
        """emit(msg) 放行（合并后的）消息；is_candidate(msg) → 这条是不是对小助手说的。"""
        self.emit, self.is_candidate, self.clock = emit, is_candidate, clock
        self.delay = float(os.environ.get("BOT_DEBOUNCE_SEC", "2.5")) if delay is None else delay
        self.max_wait = float(os.environ.get("BOT_DEBOUNCE_MAX", "8")) if max_wait is None else max_wait
        self.timer_factory = timer_factory or (lambda sec, fn: threading.Timer(sec, fn))
        self._b: dict[tuple, dict] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(msg) -> tuple:
        return (msg.room_id or msg.from_wxid, msg.from_wxid)

    def offer(self, msg) -> bool:
        """True = 收下了（之后由 emit 放行）；False = 调用方照常处理（若此人有一批在等，已先放行那批）。"""
        if self.delay <= 0:
            return False
        k = self.key(msg)
        with self._lock:
            has = k in self._b
        cand = msg.event == "AddMsg" and not msg.is_self and msg.msg_type == 1 and (has or self._safe_cand(msg))
        if not cand:
            if has:
                self.flush(k)
            return False
        with self._lock:
            b = self._b.get(k)
            now = self.clock()
            if b is None:
                b = self._b[k] = {"msgs": [], "first": now, "timer": None}
            b["msgs"].append(msg)
            if b["timer"] is not None:
                b["timer"].cancel()
            wait = max(0.0, min(self.delay, b["first"] + self.max_wait - now))
            t = self.timer_factory(wait, lambda: self.flush(k))
            b["timer"] = t
            try:
                t.daemon = True
            except Exception:
                pass
            t.start()
        return True

    def _safe_cand(self, msg) -> bool:
        try:
            return bool(self.is_candidate(msg))
        except Exception:
            return False

    def flush(self, k) -> None:
        with self._lock:
            b = self._b.pop(k, None)
        if not b:
            return
        if b["timer"] is not None:
            b["timer"].cancel()
        msgs = b["msgs"]
        m = merge(msgs)
        if len(msgs) > 1:
            log.info("防抖合并 %s %s：%d 条 → 1 条", k[0], k[1], len(msgs))
        try:
            self.emit(m)
        except Exception:
            log.exception("防抖放行失败")

    def flush_all(self) -> None:
        with self._lock:
            keys = list(self._b)
        for k in keys:
            self.flush(k)

    def pending(self) -> int:
        with self._lock:
            return len(self._b)


def merge(msgs: list):
    m = msgs[0]
    if len(msgs) == 1:
        return m
    m.content = "\n".join((x.content or "").strip() for x in msgs if (x.content or "").strip())
    seen = list(m.at_list)
    for x in msgs[1:]:
        for w in x.at_list:
            if w not in seen:
                seen.append(w)
    m.at_list = seen
    m.merged_ids = [x.msg_id for x in msgs[1:]]
    return m
