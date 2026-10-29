"""进群欢迎语音。

流程：固定欢迎文字（welcome_tpl）照常先发 → 本模块排队，稍后补一条固定模板语音。
- 开关：群级 welcome_voice（默认关；只给示例AI交流群开）。还要求 welcome 开、会话在发送白名单且不是观察群。
- 模板：settings.rooms[room].welcome_voice_tpl（固定文案，不调模型），缺省用 DEFAULT_TPL；{name} = 新人名字。
- 名字：群昵称优先，其次微信昵称；清掉 emoji/特殊符号，清完为空或超过 12 字 → 「新朋友」。
- 合并：同一群首个进群事件后等 WINDOW 秒（默认 10）再发，期间进来的人并成一条；
  两条欢迎语音至少间隔 GAP 秒（默认 60），间隔内进来的人并到下一条。名字最多列 3 个，再多说「A、B、C等几位新朋友」。
- 上限：每群每天 DAILY_CAP 条（默认 30），状态落盘 data/welcome_voice_state.json。
- 静音：跟欢迎文字一致——静音群里进群事件本来就不进功能（router._dispatch_muted），发送前再查一次静音。
- 合成失败：只记日志，不回退文字、不重发欢迎文字（Reply.voice_only）。成功后记进该群聊天上下文。
环境变量：BOT_WELCOME_VOICE_WINDOW / BOT_WELCOME_VOICE_GAP / BOT_WELCOME_VOICE_DAILY_CAP。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("feature.welcome_voice")

DEFAULT_TPL = ("欢迎{name}加入示例AI交流群！我是小助手，主人的AI助手。群里白天每小时会推AI快讯，"
               "AI工具怎么选、怎么用，有问题直接@我就行。先跟大家打个招呼吧！")
FALLBACK_NAME = "新朋友"
NAME_MAX = 12

# 保留：汉字、字母、数字、空格、间隔号；其余（emoji、符号、颜文字、零宽字符等）一律去掉
_KEEP = re.compile(r"[^0-9A-Za-z\u00C0-\u024F\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3040-\u30FF\uAC00-\uD7AF ·]")


def _env_f(k, d):
    try:
        return float(os.environ.get(k, d))
    except ValueError:
        return float(d)


def clean_name(raw: str) -> str:
    t = _KEEP.sub("", raw or "")
    t = re.sub(r"\s+", " ", t).strip(" ·")
    if not t or len(t) > NAME_MAX:
        return FALLBACK_NAME
    return t


def compose_names(names: list[str]) -> str:
    """清洗后的名字列表 → 模板里的 {name}。"""
    real = []
    for n in names:
        if n != FALLBACK_NAME and n not in real:
            real.append(n)
    unnamed = len(names) - sum(1 for n in names if n != FALLBACK_NAME)
    total = len(real) + unnamed
    if not real:
        return FALLBACK_NAME if total <= 1 else "几位新朋友"
    if total <= 3 and not unnamed:
        return "、".join(real)
    return "、".join(real[:3]) + "等几位新朋友"


class WelcomeVoice:
    def __init__(self, settings, deliver: Callable, allowed: Callable[[str], bool],
                 is_muted: Callable[[str], bool] | None = None, name_of: Callable[[str, str], str] | None = None,
                 state_path: Path | None = None, clock: Callable[[], float] = time.time,
                 start_timer: Callable[[float, Callable], object] | None = None):
        self.settings, self.deliver, self.allowed = settings, deliver, allowed
        self.is_muted = is_muted or (lambda room: False)
        self.name_of = name_of
        self.path = Path(state_path) if state_path else None
        self.clock = clock
        self.start_timer = start_timer or self._thread_timer
        self.window = _env_f("BOT_WELCOME_VOICE_WINDOW", 10)
        self.gap = _env_f("BOT_WELCOME_VOICE_GAP", 60)
        self.daily_cap = int(_env_f("BOT_WELCOME_VOICE_DAILY_CAP", 30))
        self._lock = threading.RLock()
        self._pending: dict[str, list[tuple[str, str]]] = {}   # room → [(wxid, 名字)]
        self._timer: dict[str, object] = {}
        self._st = self._load()

    @staticmethod
    def _thread_timer(delay: float, fn: Callable):
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.name = "welcome-voice"
        t.start()
        return t

    # ---- 状态 ----
    def _load(self) -> dict:
        if self.path and self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        return {}

    def _today(self) -> dict:
        day = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        if self._st.get("day") != day:
            last = self._st.get("last", {})
            self._st = {"day": day, "count": {}, "last": last}
        return self._st

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._st, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            log.exception("写欢迎语音状态失败")

    # ---- 开关 ----
    def on(self, room: str) -> bool:
        s = self.settings
        return bool(room and s is not None and s.enabled(room, "welcome") and s.enabled(room, "welcome_voice")
                    and self.allowed(room))

    def template(self, room: str) -> str:
        tpl = self.settings.room_get(room, "welcome_voice_tpl") if hasattr(self.settings, "room_get") else None
        return tpl if isinstance(tpl, str) and "{name}" in tpl else DEFAULT_TPL

    def _name(self, room: str, wxid: str, nickname: str) -> str:
        group_nick = ""
        if self.name_of and wxid:
            try:
                group_nick = self.name_of(room, wxid) or ""
            except Exception:
                group_nick = ""
        if group_nick == wxid:
            group_nick = ""
        c = clean_name(group_nick) if group_nick else FALLBACK_NAME
        return c if c != FALLBACK_NAME else clean_name(nickname)

    # ---- 排队 / 发送 ----
    def enqueue(self, room: str, members) -> bool:
        """members：[Person(wxid, nickname)]（已去掉机器人自己）。返回是否排上队。"""
        if not members or not self.on(room):
            return False
        with self._lock:
            q = self._pending.setdefault(room, [])
            seen = {w for w, _ in q if w}
            for p in members:
                if p.wxid and p.wxid in seen:
                    continue
                q.append((p.wxid, p.nickname))
                seen.add(p.wxid)
            if room in self._timer:
                return True
            last = float(self._today()["last"].get(room, 0))
            delay = max(self.window, last + self.gap - self.clock())
            self._timer[room] = self.start_timer(delay, lambda: self.flush(room))
        log.info("欢迎语音排队 %s %d 人，%.0fs 后发", room, len(q), delay)
        return True

    def flush(self, room: str) -> bool:
        with self._lock:
            self._timer.pop(room, None)
            people = self._pending.pop(room, [])
        if not people:
            return False
        if not self.on(room):
            log.info("欢迎语音取消（开关已关/不在白名单）%s", room)
            return False
        if self.is_muted(room):
            log.info("欢迎语音取消（主人静音中）%s", room)
            return False
        with self._lock:
            st = self._today()
            if st["count"].get(room, 0) >= self.daily_cap:
                log.info("欢迎语音今日已达上限 %d，跳过 %s", self.daily_cap, room)
                return False
            st["count"][room] = st["count"].get(room, 0) + 1
            st["last"][room] = self.clock()
            self._save()
        names = [self._name(room, w, n) for w, n in people]
        script = self.template(room).replace("{name}", compose_names(names))
        from router import Reply
        try:
            self.deliver(Reply(room, "", voice=script, voice_only=True))
        except Exception:
            log.exception("欢迎语音发送异常 %s", room)
            return False
        log.info("欢迎语音已交发送 %s %d 人 %d 字", room, len(people), len(script))
        return True
