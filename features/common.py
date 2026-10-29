"""功能间共用的小工具：指令解析、@目标、名字解析。"""
from __future__ import annotations

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
_PUNCT = re.compile(r"[\s\u2005,，。.!！?？、~～·\-—_'\"“”‘’《》()（）]+")


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def now() -> float:
    return time.time()


def is_group_text(msg) -> bool:
    return msg.event == "AddMsg" and msg.is_group and msg.msg_type == 1 and not msg.is_self


def at_me(msg) -> bool:
    return bool(msg.self_wxid) and msg.self_wxid in msg.at_list


def at_command(msg, names) -> str | None:
    """群文本 + @机器人 + 指令名开头 → 返回去掉指令名后的参数串；否则 None。names 按长度降序匹配。"""
    if not (is_group_text(msg) and at_me(msg)):
        return None
    t = msg.command_text
    for n in sorted(names, key=len, reverse=True):
        if t == n or t.startswith(n):
            return t[len(n):].strip()
    return None


def at_targets(msg) -> list[str]:
    """消息里 @ 的其他人（去掉机器人自己和 @所有人）。"""
    return [w for w in msg.at_list if w not in (msg.self_wxid, "notify@all", msg.from_wxid)]


def norm(s: str) -> str:
    return _PUNCT.sub("", s or "").lower()


def strip_ats(text: str) -> str:
    """去掉正文里所有 @xxx（@ 后到 \\u2005/空格为止）。"""
    return re.sub(r"@[^\s\u2005]+[\u2005\s]?", "", text or "").strip()
