"""可插拔消息路由。每个功能是一个 Feature，注册到 Router；
Router 对每条入站消息依次询问 feature.match()，命中则调用 handle()，handle 可返回回复文本。

回复默认 dry-run（只记录不发送）。真正发送需要 Router(send_enabled=True)，
且 allow_targets 白名单限制可发对象（默认空，一律不真发）——符合"未经主人确认不对外发消息"的铁律。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from models import InboundMessage

log = logging.getLogger("router")


@dataclass
class Reply:
    to_wxid: str
    content: str
    ats: str | None = None
    image_path: str | None = None    # 有值则发图（content 作为图片说明/降级文字）
    parts: list[str] | None = None   # 真人感：≥2 条时逐条发，条间随机间隔（content = 各条用换行拼起来，便于日志/轨迹）
    delay_first: float = 0.0         # 第一条前先等几秒（流式已提前发出首句时，接着发剩余部分用）
    note: str | None = None          # 记进群上下文的说明（如「[发了一张自己画的图：xx]」），不发出去
    voice: str | None = None         # 语音稿：有值则整条改发语音（features/voice_reply.py），失败回退发 content/parts 文字
    allow_stale: bool = False        # True：积压旧消息（passive_only）也照发——进群欢迎用，陪聊等主动回复仍不补发
    voice_only: bool = False         # True：只发语音，失败不回退文字（进群欢迎语音）；发成功才记上下文


class Feature(Protocol):
    name: str
    passive: bool   # True：所有消息都会经过（如统计），不阻断后续功能

    def match(self, msg: InboundMessage) -> bool: ...
    def handle(self, msg: InboundMessage) -> str | Reply | None: ...


@dataclass
class Router:
    send_enabled: bool = False
    allow_targets: set[str] = field(default_factory=set)   # 空 = 一律不真发
    block_targets: set[str] = field(default_factory=set)   # 观察群（data/observe.json）：即使在白名单也绝不真发
    sender: Callable[[Reply], object] | None = None
    # 功能开关：gate(session, toggle_key) -> bool；功能声明 toggle_key 才受控（None=不受开关控制）
    gate: Callable[[str | None, str], bool] | None = None
    features: list = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)
    outbox: list[Reply] = field(default_factory=list)   # dry-run 时的待发回复
    multi_async: bool = True                             # 多条连发放后台线程（不阻塞轮询）；测试里设 False
    sleep: Callable[[float], None] = time.sleep
    gap: Callable[[], float] | None = None               # 条间间隔秒数；None = humanlike.gap_range() 内随机
    ignore_senders: set[str] = field(default_factory=set)   # 别的机器人：只跑被动功能，绝不回复（防互触发）
    room_policy: dict = field(default_factory=dict)          # {room: {daily_reply_cap, user_cooldown_sec}}（allow_send 观察群）
    policy_state_path: object = None
    now: Callable[[], float] = time.time
    _pstate: dict = field(default_factory=dict)
    _lock: object = field(default_factory=threading.RLock)   # 按群并发：去重集合、房间配额状态加锁
    context: object = None          # features/chat_context.ChatContext：白名单会话的收发都记进去（陪聊共享上下文）
    name_of: Callable | None = None  # name_of(room, wxid) → 群昵称
    voice_sender: Callable | None = None   # voice_sender(to_wxid, script) → bool（app 绑定 make_voice_sender）

    def _ctx_ok(self, sess) -> bool:
        return self.context is not None and sess in self.allow_targets and sess not in self.block_targets

    def _ctx_in(self, msg):
        sess = msg.room_id or msg.from_wxid
        if msg.event != "AddMsg" or msg.is_self or not self._ctx_ok(sess):
            return
        from features.chat_context import TYPE_TEXT
        text = msg.content if msg.msg_type == 1 else TYPE_TEXT.get(msg.msg_type)
        if getattr(msg, "article", None):
            text = f"[转发了公众号文章《{msg.article.get('title', '')}》]"
        if not text:
            return
        if msg.msg_type == 1 and getattr(msg, "from_voice", False):
            text = f"（语音）{text}"
        if msg.msg_type == 1 and getattr(msg, "quote", ""):
            text = f"（引用「{msg.quote[:40]}」）{text}"
        name = ""
        if msg.is_group:
            try:
                name = (self.name_of(msg.room_id, msg.from_wxid) if self.name_of else "") or ""
            except Exception:
                name = ""
            name = name or msg.from_wxid
        try:
            self.context.add(sess, "user", text, name=name, mid=msg.msg_id)
        except Exception:
            log.exception("记上下文失败")

    def _ctx_out(self, reply):
        if not self._ctx_ok(reply.to_wxid):
            return
        try:
            if reply.voice:
                self.context.add(reply.to_wxid, "assistant", f"（发了条语音）{reply.voice}")
                return
            if reply.note:
                self.context.add(reply.to_wxid, "assistant", reply.note)
                return
            if reply.image_path and not reply.content:
                self.context.add(reply.to_wxid, "assistant", "[发了一张图]")
            from features.chat_context import PUSH_CHARS
            for p in (reply.parts or [reply.content]):
                self.context.add(reply.to_wxid, "assistant", p, limit=PUSH_CHARS)   # 快讯/日报整条记下，小助手才知道自己发过什么
        except Exception:
            log.exception("记上下文失败")

    def _policy_block(self, msg) -> str | None:
        """该群日上限 / 同一人冷却：返回拦截原因或 None。"""
        pol = self.room_policy.get(msg.room_id or "")
        if not pol:
            return None
        with self._lock:
            return self._policy_block_locked(msg, pol)

    def _policy_block_locked(self, msg, pol):
        st = self._policy_load()
        day = st.get("day")
        today = time.strftime("%Y-%m-%d", time.localtime(self.now()))
        if day != today:
            st.clear(); st.update(day=today, count={}, last={})
        room = msg.room_id
        if st["count"].get(room, 0) >= int(pol.get("daily_reply_cap", 60)):
            return "daily_cap"
        last = st["last"].get(f"{room}|{msg.from_wxid}", 0)
        if self.now() - last < float(pol.get("user_cooldown_sec", 60)):
            return "cooldown"
        return None

    def _policy_count(self, msg):
        if (msg.room_id or "") not in self.room_policy:
            return
        with self._lock:
            self._policy_count_locked(msg)

    def _policy_count_locked(self, msg):
        st = self._policy_load()
        st["count"][msg.room_id] = st["count"].get(msg.room_id, 0) + 1
        st["last"][f"{msg.room_id}|{msg.from_wxid}"] = self.now()
        if self.policy_state_path:
            import json, os
            from pathlib import Path
            p = Path(self.policy_state_path); tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(st)); os.replace(tmp, p)

    def _policy_load(self) -> dict:
        if not self._pstate:
            if self.policy_state_path:
                import json
                from pathlib import Path
                try:
                    self._pstate.update(json.loads(Path(self.policy_state_path).read_text()))
                except (OSError, ValueError):
                    pass
            self._pstate.setdefault("day", ""); self._pstate.setdefault("count", {}); self._pstate.setdefault("last", {})
        return self._pstate

    def register(self, feature) -> "Router":
        self.features.append(feature)
        return self

    def dispatch(self, msg: InboundMessage, passive_only: bool = False) -> list[dict]:
        """返回本条消息的处理轨迹 [{feature, reply, sent}]。
        passive_only=True：只跑被动功能（计数等）且不发任何回复——用于掉线恢复后积压的旧消息，不补发。"""
        key = f"{msg.app_id}:{msg.msg_id}"
        with self._lock:
            if msg.msg_id and key in self._seen:
                return [{"feature": "<dedup>", "reply": None, "sent": False}]
            if msg.msg_id:
                self._seen.add(key)

        if msg.from_wxid in self.ignore_senders:
            passive_only = True             # 别的机器人：只计数，不回复（即使 @ 小助手）
        # 主人静音（features/owner_mute.py）：静音群里非主人本人的消息 → 只做被动统计，不花额度、不回复
        mute = getattr(self, "mute", None)
        is_owner = False
        muted_quiet = False
        if mute is not None and msg.is_group and msg.event == "AddMsg" and not msg.is_self:
            is_owner = msg.from_wxid in mute.owners()
            if not is_owner and mute.is_muted(msg.room_id):
                muted_quiet = True
                passive_only = True
        fu = getattr(self, "followup", None)
        mi = getattr(self, "media_in", None)
        arts = getattr(self, "articles", None)
        if arts is not None:
            try:
                arts.observe(msg)                               # 公众号文章卡片 / 引用卡片（features/article.py），要在 prepare 改写引用之前
            except Exception:
                log.exception("文章卡片预处理失败")
        if mi is not None and not passive_only:
            mi.pre(msg)                                        # 窗口内的语音/图片先换成文本占位（features/media_in.py）
        if fu is not None:
            fu.prepare(msg, allow_window=not passive_only)      # 引用→文本、追问窗口视同 @（features/followup.py）
            if hasattr(fu, "observe"):
                fu.observe(msg)                                 # 记下谁说了话（判断有没有人插话）
        if mi is not None and not passive_only:
            try:
                mi.post(msg)                                   # 被接住的语音→转写；@/引用/窗口的图→记下要看的图
            except Exception:
                log.exception("语音/图片预处理失败")
        mute_action = "quiet" if muted_quiet else None
        if mute is not None and is_owner and not passive_only:
            try:
                it = mute.intent(msg)
            except Exception:
                log.exception("静音判断失败")
                it = None
            was = mute.is_muted(msg.room_id)
            if it == "mute" and not was:
                from features.owner_mute import parse_duration
                mute.mute(msg.room_id, msg.from_wxid, parse_duration(msg.content), msg.content)
                mute_action = "mute"
            elif it == "unmute" and was:
                mute.unmute(msg.room_id, msg.from_wxid, msg.content)
                mute_action = "unmute"
            elif was:
                mute_action = "quiet"             # 静音中主人说别的：也不接话（只有他说解除才恢复）
        self._ctx_in(msg)
        if mute_action:
            return self._dispatch_muted(msg, mute_action)
        trace = []
        for f in self.features:
            if passive_only and not getattr(f, "passive", False):
                continue
            try:
                key = getattr(f, "toggle_key", None)
                if key and self.gate:
                    sess = f.session_of(msg) if hasattr(f, "session_of") else (msg.room_id or msg.from_wxid)
                    if not self.gate(sess, key):
                        continue
                if not f.match(msg):
                    continue
                if not getattr(f, "passive", False) and self.room_policy:
                    why = self._policy_block(msg)
                    if why:
                        log.info("房间策略拦截 %s %s from=%s feature=%s", msg.room_id, why, msg.from_wxid, f.name)
                        trace.append({"feature": f.name, "reply": None, "sent": False, "skipped": why})
                        break
                out = f.handle(msg)
            except Exception as e:  # 单个功能报错不影响其它功能
                log.exception("feature %s failed", f.name)
                trace.append({"feature": f.name, "error": repr(e)})
                continue
            reply = None
            if out:
                reply = out if isinstance(out, Reply) else Reply(msg.room_id or msg.from_wxid, str(out))
            if reply and passive_only and not getattr(reply, "allow_stale", False):
                trace.append({"feature": f.name, "reply": reply.content, "sent": False, "skipped": "stale"})
                continue
            sent = self._deliver(reply) if reply else False
            item = {"feature": f.name, "reply": reply.content if reply else None, "sent": sent}
            if getattr(msg, "early_texts", None):
                item["early"] = list(msg.early_texts)          # 流式首句 / 垫话（已提前发出，不在 reply 里）
            trace.append(item)
            if (reply or getattr(msg, "early_replied", False)) and not getattr(f, "passive", False):
                if fu is not None:
                    fu.note_reply(msg, f.name, reply.content if reply else "")
                self._policy_count(msg)
            if not getattr(f, "passive", False):
                break   # 第一个主动功能命中后停止
        return trace

    MUTE_PASSIVE_OK = ("speech_stats.recorder", "gzh_promo")   # 静音时照跑的被动功能（只计数/入队，不说话）

    def _dispatch_muted(self, msg, action: str) -> list[dict]:
        """静音群：只跑计数类被动功能；mute/unmute 时回一句短确认，其余一律不回。"""
        trace = []
        for f in self.features:
            if not getattr(f, "passive", False) or f.name not in self.MUTE_PASSIVE_OK:
                continue
            try:
                key = getattr(f, "toggle_key", None)
                if key and self.gate and not self.gate(msg.room_id or msg.from_wxid, key):
                    continue
                if f.match(msg):
                    f.handle(msg)
                    trace.append({"feature": f.name, "reply": None, "sent": False})
            except Exception as e:
                log.exception("feature %s failed", f.name)
                trace.append({"feature": f.name, "error": repr(e)})
        if action in ("mute", "unmute"):
            from features.owner_mute import MUTE_ACK, UNMUTE_ACK
            reply = Reply(msg.room_id, MUTE_ACK if action == "mute" else UNMUTE_ACK)
            trace.append({"feature": f"owner_mute.{action}", "reply": reply.content, "sent": self._deliver(reply)})
        else:
            trace.append({"feature": "owner_mute", "reply": None, "sent": False, "skipped": "muted"})
        return trace

    def deliver(self, reply: Reply) -> bool:
        """供后台任务（异步日报、定时任务）发送：同样受 send_enabled + 白名单约束。"""
        sent = self._deliver(reply)
        log.info("deliver -> %s sent=%s image=%s", reply.to_wxid, sent, bool(reply.image_path))
        return sent

    def _allowed(self, to_wxid: str) -> bool:
        return bool(self.send_enabled and self.sender and to_wxid in self.allow_targets
                    and to_wxid not in self.block_targets)

    def _deliver(self, reply: Reply) -> bool:
        if reply.voice:
            if not reply.voice_only:
                self._ctx_out(reply)
            return self._deliver_voice(reply)
        ok = self._deliver_text(reply)          # 发送抛异常（如 gewe ret=500）→ 直接抛出、不记上下文，免得以为自己说过
        self._ctx_out(reply)                    # 发出去了（或 dry-run 进 outbox）才记
        return ok

    def _deliver_voice(self, reply: Reply) -> bool:
        """整条语音（异步：TTS+编码+上传不阻塞轮询）；失败回退发文字。一条回复要么全语音要么全文字。"""
        import dataclasses
        text = dataclasses.replace(reply, voice=None)
        if not self._allowed(reply.to_wxid) or self.voice_sender is None:
            if reply.voice_only:
                self.outbox.append(reply)
                log.info("[语音不可发，voice_only 不回退文字] -> %s: %s", reply.to_wxid, reply.voice[:80])
                return False
            if self._allowed(reply.to_wxid):
                return self._deliver_text(text)
            self.outbox.append(reply)
            log.info("[dry-run] -> %s: [语音] %s", reply.to_wxid, reply.voice[:80])
            return False

        def run():
            ok = False
            try:
                ok = self.voice_sender(reply.to_wxid, reply.voice)
            except Exception:
                log.exception("语音发送异常")
            if reply.voice_only:
                if ok:
                    self._ctx_out(reply)
                else:
                    log.warning("语音发送失败（voice_only，不回退文字）-> %s", reply.to_wxid)
                return
            if not ok:
                if not self._allowed(reply.to_wxid):
                    return
                try:
                    self._deliver_text(text)
                except Exception:
                    log.exception("语音回退文字失败")
        if self.multi_async:
            threading.Thread(target=run, daemon=True, name="voice-send").start()
        else:
            run()
        return True

    def _deliver_text(self, reply: Reply) -> bool:
        if reply.parts and (len(reply.parts) > 1 or reply.delay_first > 0) and not reply.image_path:
            return self._deliver_multi(reply)
        if not self._allowed(reply.to_wxid):
            self.outbox.append(reply)
            log.info("[dry-run] -> %s: %s%s", reply.to_wxid, "[图片] " if reply.image_path else "", reply.content[:80])
            return False
        self.sender(reply)
        return True

    def _deliver_multi(self, reply: Reply) -> bool:
        """真人感连发：每条单独 postText，条间 sleep 随机 2~5 秒（可配）。第一条带原 ats。"""
        msgs = [Reply(reply.to_wxid, p, ats=reply.ats if i == 0 else None) for i, p in enumerate(reply.parts)]
        if not self._allowed(reply.to_wxid):
            self.outbox.extend(msgs)
            log.info("[dry-run] -> %s: %d 条连发 %s", reply.to_wxid, len(msgs), reply.content[:80])
            return False

        def gap() -> float:
            if self.gap:
                return self.gap()
            from features.humanlike import gap_range
            lo, hi = gap_range()
            return random.uniform(lo, hi)

        def run():
            for i, m in enumerate(msgs):
                if i:
                    self.sleep(gap())
                elif reply.delay_first > 0:
                    self.sleep(reply.delay_first)
                if not self._allowed(m.to_wxid):        # 期间被关掉就停
                    return
                try:
                    self.sender(m)
                except Exception:
                    log.exception("连发第 %d 条失败，停止后续", i + 1)
                    return
        if self.multi_async:
            threading.Thread(target=run, daemon=True, name="multi-send").start()
        else:
            run()
        return True
