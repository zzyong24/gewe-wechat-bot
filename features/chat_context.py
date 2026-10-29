"""群聊共享上下文：按会话（群 id / 私聊 wxid）保存最近若干条消息——群里所有人的文字 + 小助手自己发出的每一条（含拆条），
带发言人昵称和时间，持久化到磁盘（重启不丢）。陪聊调模型时把它转成 messages 历史。

为什么：之前上下文只在陪聊内存里按「@小助手 的那句 + 小助手的回复」成对保存——重启就清空；别的群友的话、
小助手其它功能发的话、不 @ 的追问都看不到，导致「需要几个人？」接不上刚说过的狼人杀。

记录点（都在 router 里）：dispatch 收到白名单会话的消息 → add(user)；_deliver 发出（或 dry-run）白名单会话的回复 → add(assistant)。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

KEEP = int(os.environ.get("BOT_CONTEXT_KEEP", "40"))            # 每会话磁盘上最多留几条
USE_MSGS = int(os.environ.get("BOT_CONTEXT_MSGS", "20"))        # 给模型最多几条
USE_CHARS = int(os.environ.get("BOT_CHAT_HISTORY_CHARS", "1800"))   # 给模型的历史总字数上限（控延迟）
TTL = int(os.environ.get("BOT_CONTEXT_TTL", str(6 * 3600)))     # 超过这么久的旧消息不再给模型
ONE_CHARS = 200                                                  # 单条截断
PUSH_CHARS = int(os.environ.get("BOT_CONTEXT_PUSH_CHARS", "1200"))   # 小助手自己发的长消息（快讯/日报等主动推送）别截成 200 字

TYPE_TEXT = {3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[表情]", 48: "[位置]"}


class ChatContext:
    def __init__(self, path: Path | str | None = None, clock=time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self._d: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        if self.path and self.path.exists():
            try:
                self._d = {k: list(v) for k, v in json.loads(self.path.read_text(encoding="utf-8")).items()}
            except (OSError, ValueError):
                self._d = {}

    def add(self, sess: str, role: str, text: str, name: str = "", mid: str | int | None = None, ts: float | None = None,
            limit: int | None = None):
        text = (text or "").replace("\u2005", " ").strip()
        if not sess or not text:
            return
        e = {"r": "a" if role == "assistant" else "u", "t": text[:limit or ONE_CHARS], "ts": ts or self.clock()}
        if name:
            e["n"] = name
        if mid not in (None, ""):
            e["id"] = str(mid)
        with self._lock:
            lst = self._d.setdefault(sess, [])
            if e.get("id") and any(x.get("id") == e["id"] for x in lst[-10:]):
                return
            lst.append(e)
            del lst[:-KEEP]
            self._save()

    def clear(self, sess: str):
        with self._lock:
            self._d.pop(sess, None)
            self._save()

    def entries(self, sess: str) -> list[dict]:
        with self._lock:
            return list(self._d.get(sess, ()))

    def history(self, sess: str, exclude: str | int | None = None, max_msgs: int | None = None,
                max_chars: int | None = None, group: bool = True) -> list[dict]:
        """→ [{"role","content"}]：小助手自己的话是 assistant；别人的话是 user，群里带「[昵称]：」。连续同角色合并。"""
        max_msgs = max_msgs or USE_MSGS
        max_chars = max_chars or USE_CHARS
        now = self.clock()
        ex = str(exclude) if exclude not in (None, "") else None
        items = [e for e in self.entries(sess) if now - e.get("ts", 0) <= TTL and not (ex and e.get("id") == ex)]
        items = items[-max_msgs:]
        keep, total = [], 0
        for e in reversed(items):                      # 从最新往前塞，超字数就停
            line = e["t"] if e["r"] == "a" or not group else f"[{e.get('n') or '某人'}]：{e['t']}"
            if keep and total + len(line) > max_chars:
                break
            total += len(line)
            keep.append(("assistant" if e["r"] == "a" else "user", line))
        out: list[dict] = []
        for role, line in reversed(keep):
            if out and out[-1]["role"] == role:
                out[-1]["content"] += "\n" + line
            else:
                out.append({"role": role, "content": line})
        return out

    def _save(self):
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._d, ensure_ascii=False), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass
