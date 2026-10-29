"""功能 4：群员动态（进群 / 退群 / 被移出提醒）。listener 型（passive），受开关 member_change 控制。

真实样本（2026-10-20，小助手被拉进测试群，data/inbox raw id=2）：
  TypeName=AddMsg, Data.MsgType=10002, FromUserName=<群>@chatroom,
  Content = "<群>@chatroom:\\n<sysmsg type=\"sysmsgtemplate\"><sysmsgtemplate><content_template type=\"tmpl_type_profile\">
             <template><![CDATA[\"$username$\"邀请你和\"$names$\"加入了群聊]]></template>
             <link_list><link name=\"username\"><memberlist><member><username>wxid..</username><nickname>..</nickname>
             </member></memberlist></link><link name=\"names\">…<separator>、</separator></link></link_list>…"
支持的模板（$xxx$ 占位符 → link_list 里同名 link 的 memberlist）：
  进群："$username$"邀请"$names$"加入了群聊 / "$username$"邀请你和"$names$"加入了群聊 / 你邀请"$names$"加入了群聊 /
        "$adder$"通过扫描"$from$"分享的二维码加入群聊 / 其它含「加入/进入…群聊」的模板
  移出：你将"$kickoutname$"移出了群聊 / "$username$"将"$kickoutname$"移出了群聊   ← 来自第三方文档，待真实事件验证
  兜底：MsgType=10000 纯文本（"X"邀请"Y"加入了群聊 / "Y"通过扫描"X"分享的二维码加入群聊 / X 移出了群聊）← 待验证
升级（welcome / farewell）：同一个 feature 统一解析事件，按本群开关决定说什么——
  进群：welcome 开 → 小助手亲自欢迎（@新人+昵称；白名单群用本群人设调模型生成一句，调不通/没 key/非白名单 → 模板）；
        否则 member_change 开 → 旧的通知文案。
  退群/被移出：farewell 开 → 纯模板送别（不调模型）；否则 member_change 开 → 旧通知。
  三个开关都关 → 不处理。真发仍走 router 发送闸（白名单 + observe.json 观察群硬拦）。
主动退群：微信不会给群成员推系统消息。这里在 ModContacts（群联系人变更，gewe 文档 doc-3599831）时重新拉群成员列表，
  与上次快照比对，少了的人视为退群 ← 待真实事件验证。
"""
from __future__ import annotations

from features.clock import now_line
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable

from features.group_members import parse_member_list

log = logging.getLogger("feature.member_change")


@dataclass
class Person:
    wxid: str
    nickname: str = ""

    @property
    def shown(self) -> str:
        return self.nickname or self.wxid


@dataclass
class MemberEvent:
    kind: str                       # join / leave / kick
    room_id: str
    members: list[Person] = field(default_factory=list)
    inviter: Person | None = None   # join：谁拉的 / 谁分享的二维码；kick：谁移出的（None=你自己或未知）
    template: str = ""


def _strip_room_prefix(content: str) -> str:
    content = (content or "").strip()
    if content.startswith("<"):
        return content
    i = content.find("\n")
    return content[i + 1:].strip() if i != -1 and content[:i].rstrip(":").endswith("@chatroom") else content


def parse_sysmsg_template(content: str, room_id: str) -> MemberEvent | None:
    xml = _strip_room_prefix(content)
    if "sysmsgtemplate" not in xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    tpl = (root.findtext(".//content_template/template") or "").strip()
    links: dict[str, list[Person]] = {}
    for link in root.iter("link"):
        ps = [Person((m.findtext("username") or "").strip(), (m.findtext("nickname") or "").strip())
              for m in link.iter("member")]
        links[link.get("name", "")] = [p for p in ps if p.wxid]
    first = lambda *names: next((links[n][0] for n in names if links.get(n)), None)
    if "移出" in tpl and "群聊" in tpl:
        kicked = links.get("kickoutname") or links.get("names") or []
        return MemberEvent("kick", room_id, kicked, first("username"), tpl) if kicked else None
    if ("加入" in tpl or "进入" in tpl) and "群聊" in tpl:
        if "$adder$" in tpl:       # 扫码进群：adder 是新人，from 是分享二维码的人
            return MemberEvent("join", room_id, links.get("adder", []), first("from"), tpl)
        joined = links.get("names") or []
        inviter = first("username")       # "你邀请…" 时没有 username → inviter=None（机器人自己）
        return MemberEvent("join", room_id, joined, inviter, tpl) if joined else None
    return None


_PLAIN = [
    (re.compile(r'^"(?P<inv>[^"]+)"邀请"(?P<who>[^"]+)"加入了群聊'), "join"),
    (re.compile(r'^"(?P<who>[^"]+)"通过扫描"(?P<inv>[^"]+)"分享的二维码加入群聊'), "join"),
    (re.compile(r'^(?:你将|"(?P<inv>[^"]+)"将)"(?P<who>[^"]+)"移出了群聊'), "kick"),
]


def parse_plain(content: str, room_id: str) -> MemberEvent | None:
    text = _strip_room_prefix(content)
    for rx, kind in _PLAIN:
        m = rx.match(text)
        if m:
            names = [n for n in re.split(r"[、,，]", m.group("who")) if n]
            inv = m.groupdict().get("inv")
            return MemberEvent(kind, room_id, [Person("", n) for n in names], Person("", inv) if inv else None, text)
    return None


def _room_from_modcontacts(raw: dict) -> str | None:
    data = (raw or {}).get("Data") or {}
    for k in ("UserName", "userName"):
        v = data.get(k)
        v = v.get("string") if isinstance(v, dict) else v
        if isinstance(v, str) and v.endswith("@chatroom"):
            return v
    return None


WELCOME_TPL = "{ats}欢迎{names}加入{room}～我是小助手，有事 @我 就好，一起把日子过得亮堂堂 🌙"
FAREWELL_TPL = {"leave": "{names} 退群了，祝 TA 一路顺风！",
                "kick": "{names} 离开了本群，祝 TA 一路顺风！"}
WELCOME_PROMPT = ("群里刚来了新朋友「{names}」{by}。请用你的人设口吻，写一句热情、自然的进群欢迎语，"
                  "不超过 50 字；直接输出这句话本身，不要加 @、不要加引号、不要解释。")


def _clean_line(text: str, limit: int = 80) -> str:
    q = "\"“”「」'‘’"
    t = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "").strip(q).strip()
    t = re.sub(r"@\S+\s*", "", t).strip().strip(q).strip()   # 模型自己加的 @ 去掉，统一由我们 @
    return t[:limit]


class MemberChangeFeature:
    name = "member_change"
    passive = True
    toggle_key = None          # 自己按 member_change / welcome / farewell 三个开关判断

    def __init__(self, fetch_list: Callable[[str], dict] | None = None,
                 welcome: str = "欢迎 {names} 加入本群～{by}", leave: str = "{names} 退出了本群",
                 kick: str = "{names} 被移出了本群", settings=None,
                 llm_for: Callable[[str], object] | None = None, persona_of: Callable[[str], str] | None = None,
                 room_name: Callable[[str], str] | None = None):
        self.fetch_list = fetch_list
        self.tpl = {"join": welcome, "leave": leave, "kick": kick}
        self._snap: dict[str, dict[str, str]] = {}     # room → {wxid: 名字}，用于退群比对
        self.settings, self.llm_for, self.persona_of, self.room_name = settings, llm_for, persona_of, room_name
        self.welcome_voice = None      # features/welcome_voice.WelcomeVoice（app 绑定）：欢迎文字后再补一条语音

    def _on(self, room, key: str) -> bool:
        if self.settings is None:
            return key == "member_change"           # 无开关对象（旧用法）= 只做通知
        return self.settings.enabled(room, key)

    def _active(self, room) -> bool:
        return any(self._on(room, k) for k in ("member_change", "welcome", "farewell"))

    def session_of(self, msg):
        return msg.room_id or _room_from_modcontacts(msg.raw)

    def match(self, msg) -> bool:
        if msg.event == "AddMsg":
            return msg.is_group and msg.msg_type in (10000, 10002) and self._active(msg.room_id)
        room = _room_from_modcontacts(msg.raw)
        return msg.event == "ModContacts" and self.fetch_list is not None and bool(room) and self._active(room)

    def parse(self, msg) -> MemberEvent | None:
        if msg.event == "ModContacts":
            return self._diff(_room_from_modcontacts(msg.raw))
        ev = parse_sysmsg_template(msg.content, msg.room_id) if msg.msg_type == 10002 else parse_plain(msg.content, msg.room_id)
        if ev:
            snap = self._snap.get(ev.room_id)
            if snap is not None:          # 维护快照，避免之后 diff 重复报
                for p in ev.members:
                    if not p.wxid:
                        continue
                    if ev.kind == "join":
                        snap[p.wxid] = p.shown
                    else:
                        snap.pop(p.wxid, None)
        return ev

    def _diff(self, room: str) -> MemberEvent | None:
        try:
            now = {m.wxid: m.shown for m in parse_member_list(self.fetch_list(room) or {})}
        except Exception as e:
            log.warning("拉群成员失败 %s: %s", room, e)
            return None
        if not now:
            return None
        before = self._snap.get(room)
        self._snap[room] = now
        if before is None:
            return None                   # 首次只建基线
        gone = [Person(w, n) for w, n in before.items() if w not in now]
        return MemberEvent("leave", room, gone, None, "member-diff") if gone else None

    def format(self, ev: MemberEvent, self_wxid: str = "") -> str | None:
        ms = [p for p in ev.members if p.wxid != self_wxid]     # 机器人自己进群不欢迎自己
        if not ms:
            return None
        names = "、".join(p.shown for p in ms)
        by = ""
        if ev.kind == "join" and ev.inviter and ev.inviter.wxid != self_wxid:
            by = f"（{ev.inviter.shown} 邀请）" if "$adder$" not in ev.template else f"（扫 {ev.inviter.shown} 的二维码）"
        return self.tpl[ev.kind].format(names=names, by=by)

    def _welcome_tpl(self, room_id: str) -> str | None:
        """群级固定欢迎模板（settings.rooms[room].welcome_tpl）；有则不再调模型。"""
        if self.settings is None or not hasattr(self.settings, "room_get"):
            return None
        tpl = self.settings.room_get(room_id, "welcome_tpl")
        return tpl if isinstance(tpl, str) and tpl.strip() else None

    # ---- 小助手亲自说话 ----
    def welcome(self, ev: MemberEvent, self_wxid: str = ""):
        from router import Reply
        ms = [p for p in ev.members if p.wxid != self_wxid]
        if not ms:
            return None
        names = "、".join(p.shown for p in ms)
        at_ms = [p for p in ms if p.wxid]            # 只有拿到 wxid 的才能真 @
        ats = "".join(f"@{p.shown} " for p in at_ms)
        names_in_body = "" if ats else names          # 有 @ 时正文不再塞昵称，避免「@张三 欢迎张三加入」
        fixed = self._welcome_tpl(ev.room_id)
        if fixed:
            try:
                text = fixed.format(ats=ats, names=names_in_body)
            except (KeyError, ValueError) as e:
                log.warning("欢迎模板 format 失败，回退默认: %s", e)
                room = (self.room_name(ev.room_id) if self.room_name else "") or "本群"
                text = WELCOME_TPL.format(ats=ats, names=names_in_body, room=room)
            return Reply(ev.room_id, text, ats=",".join(p.wxid for p in at_ms) or None, allow_stale=True)
        body = None
        llm = self.llm_for(ev.room_id) if self.llm_for else None
        if llm is not None:
            by = f"（{ev.inviter.shown} 拉进来的）" if ev.inviter and ev.inviter.wxid != self_wxid else ""
            try:
                body = _clean_line(llm.chat(WELCOME_PROMPT.format(names=names, by=by),
                                            system=((self.persona_of(ev.room_id) if self.persona_of else None) or "")
                                            + "\n" + now_line()))
            except Exception as e:
                log.warning("欢迎语调模型失败，回退模板: %s", str(e)[:200])
        if body:
            text = f"{ats}{body}"
        else:
            room = (self.room_name(ev.room_id) if self.room_name else "") or "本群"
            text = WELCOME_TPL.format(ats=ats, names=names_in_body, room=room)
        return Reply(ev.room_id, text, ats=",".join(p.wxid for p in at_ms) or None, allow_stale=True)

    def farewell(self, ev: MemberEvent, self_wxid: str = ""):
        from router import Reply
        ms = [p for p in ev.members if p.wxid != self_wxid]
        if not ms:
            return None
        names = "、".join(p.shown for p in ms)     # 拿不到昵称时 shown = wxid
        return Reply(ev.room_id, FAREWELL_TPL.get(ev.kind, FAREWELL_TPL["leave"]).format(names=names))

    def handle(self, msg):
        ev = self.parse(msg)
        if not ev:
            return None
        log.info("群员动态 %s room=%s members=%s inviter=%s", ev.kind, ev.room_id,
                 [(p.wxid, p.nickname) for p in ev.members], ev.inviter)
        if ev.kind == "join" and self._on(ev.room_id, "welcome"):
            reply = self.welcome(ev, msg.self_wxid)
            if reply is not None and self.welcome_voice is not None:
                try:   # 文字由 router 当场发；语音排队，至少 WINDOW 秒后才发，保证文字在前
                    self.welcome_voice.enqueue(ev.room_id, [p for p in ev.members if p.wxid != msg.self_wxid])
                except Exception:
                    log.exception("欢迎语音排队失败")
            return reply
        if ev.kind in ("leave", "kick") and self._on(ev.room_id, "farewell"):
            return self.farewell(ev, msg.self_wxid)
        if not self._on(ev.room_id, "member_change"):
            return None
        text = self.format(ev, msg.self_wxid)
        if text and msg.event == "ModContacts":
            from router import Reply
            return Reply(ev.room_id, text)
        return text
