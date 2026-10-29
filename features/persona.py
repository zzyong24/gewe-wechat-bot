"""功能 2：人设定制。按会话（群 id / 私聊对方 wxid）保存自定义 system prompt，持久化 data/personas.json（600）。
未设置的会话用默认人设（chat_companion.DEFAULT_PERSONA）。chat_companion 每次调用时实时读取，改完立即生效。

指令（群内需 @小助手）。「查看人设」谁都能看；「设置人设」「恢复默认人设」只有主人本人
（admin.bot_owners()，env BOT_OWNER_WXID / BOT_OWNER_WXIDS）能用，群主/管理员也不行，其他人回「人设由主人统一管理」：
  @小助手 查看人设
  @小助手 设置人设 <一段文字，可多行，≤1500 字>
  @小助手 恢复默认人设
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

MAX_LEN = 1500


class PersonaStore:
    def __init__(self, path: str | Path | None = None, default: str = ""):
        self.path, self.default = (Path(path) if path else None), default
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                self._data = dict(json.loads(self.path.read_text(encoding="utf-8")).get("sessions") or {})
            except (ValueError, OSError):
                pass

    def custom(self, session: str) -> str | None:
        v = self._data.get(session or "")
        return v.get("prompt") if v else None

    def get(self, session: str) -> str:
        return self.custom(session) or self.default

    def set(self, session: str, prompt: str | None, by: str = "") -> None:
        with self._lock:
            if prompt:
                self._data[session] = {"prompt": prompt[:MAX_LEN], "by": by}
            else:
                self._data.pop(session, None)
            self._save()

    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sessions": self._data}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


_SET = re.compile(r"^(?:设置人设|设定人设|修改人设)[\s:：]*(.*)$", re.S)
VIEW = ("查看人设", "当前人设", "人设")
RESET = ("恢复默认人设", "重置人设", "默认人设")


class PersonaAdminFeature:
    name = "admin.persona"
    passive = False
    toggle_key = None

    DENY = "人设由主人统一管理"

    def __init__(self, store: PersonaStore, is_owner=None):
        from features.admin import bot_owners
        self.store = store
        self.is_owner = is_owner or (lambda wxid: wxid in bot_owners())   # is_owner(wxid) -> bool

    def match(self, msg) -> bool:
        if msg.msg_type != 1 or msg.is_self or msg.event != "AddMsg":
            return False
        if msg.is_group and msg.self_wxid not in msg.at_list:
            return False
        t = msg.command_text
        return t in VIEW or t in RESET or bool(_SET.match(t))

    def handle(self, msg):
        sess = msg.room_id or msg.from_wxid
        t = msg.command_text
        where = "本群" if msg.is_group else "我们私聊"
        if t in VIEW:
            cur = self.store.custom(sess)
            return (f"🎭 {where}当前人设（自定义）：\n{cur}" if cur
                    else f"🎭 {where}当前用的是默认人设：\n{self.store.default}")
        if not self.is_owner(msg.from_wxid):
            return self.DENY
        if t in RESET:
            self.store.set(sess, None)
            return f"已把{where}的人设恢复为默认 ✅"
        body = _SET.match(t).group(1).strip()
        if not body:
            return "请在「设置人设」后面写上人设内容，例如：@小助手 设置人设 你是一只爱吐槽的猫娘，说话带喵～"
        self.store.set(sess, body, by=msg.from_wxid)
        tip = f"（已截断到 {MAX_LEN} 字）" if len(body) > MAX_LEN else ""
        return f"已更新{where}人设 ✅{tip}接下来 @我 聊天就按新人设来～"
