"""入站消息标准化。兼容 gewe 回调 v1（TypeName/Data 嵌套）与 v2（扁平 msgType/fromUser）。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any

MSG_TYPE_NAMES = {1: "text", 3: "image", 34: "voice", 37: "friend_request", 42: "card", 43: "video",
                  47: "emoji", 48: "location", 49: "app", 10000: "system", 10002: "sysmsg"}
V2_TYPE_MAP = {"TEXT": 1, "IMAGE": 3, "VOICE": 34, "VIDEO": 43, "EMOJI": 47, "LOCATION": 48, "APP": 49}


@dataclass
class InboundMessage:
    event: str                 # TypeName：AddMsg / ModContacts / DelContacts / Offline ...
    app_id: str
    self_wxid: str             # 当前机器人账号
    msg_id: str                # NewMsgId（去重键 app_id+msg_id）
    msg_type: int
    from_wxid: str             # 真实发言人 wxid（群消息已从 content 前缀拆出）
    to_wxid: str
    room_id: str | None        # 群 id（xxx@chatroom），私聊为 None
    content: str               # 文本正文（群消息已去掉 "wxid:\n" 前缀）
    create_time: int
    is_self: bool
    at_list: list[str] = field(default_factory=list)   # MsgSource <atuserlist>
    push_content: str = ""
    raw: dict = field(default_factory=dict, repr=False)
    quote: str = ""            # 引用回复：被引用那条的文字（features/followup.py 填）
    quote_from: str = ""       # 引用回复：被引用那条的发送人 wxid
    quote_name: str = ""       # 引用回复：被引用那条的发送人昵称（refermsg displayname）
    image_xml: str = ""        # 要看的图片 XML（features/media_in.py 填：@/引用图片/窗口内发图），陪聊据此下载识图
    from_voice: bool = False   # 内容是语音转写（features/media_in.py）
    addressed: str = ""        # 非 @ 但视同对小助手说："quote"（引用小助手）/ "followup"（追问窗口内接话）

    @property
    def is_group(self) -> bool:
        return self.room_id is not None

    @property
    def type_name(self) -> str:
        return MSG_TYPE_NAMES.get(self.msg_type, str(self.msg_type))

    def mentions_me(self) -> bool:
        return self.self_wxid in self.at_list or "notify@all" in self.at_list

    @property
    def command_text(self) -> str:
        """去掉开头的 @某人（微信 @ 后跟 \u2005 或空格），用于触发词匹配，如「@小助手 成员列表」→「成员列表」。"""
        return re.sub(r"^(?:@[^\s\u2005]+[\u2005\s]+)+", "", (self.content or "").strip()).strip()

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        return d


def _s(v: Any) -> str:
    if isinstance(v, dict):
        return str(v.get("string", "") or "")
    return "" if v is None else str(v)


_AT_RE = re.compile(r"<atuserlist>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</atuserlist>", re.S)


def _parse_at(msg_source: str) -> list[str]:
    m = _AT_RE.search(msg_source or "")
    if not m:
        return []
    return [w.strip() for w in m.group(1).split(",") if w.strip()]


def normalize(payload: dict) -> InboundMessage | None:
    """把 gewe 原始回调转成 InboundMessage；无法识别（如回调地址验证包）返回 None。"""
    if not isinstance(payload, dict):
        return None
    if "TypeName" in payload:          # v1
        data = payload.get("Data") or {}
        event = payload.get("TypeName", "")
        app_id = payload.get("Appid", "")
        self_wxid = payload.get("Wxid", "")
        from_u, to_u = _s(data.get("FromUserName")), _s(data.get("ToUserName"))
        content = _s(data.get("Content"))
        msg_type = int(data.get("MsgType") or 0)
        msg_id = str(data.get("NewMsgId") or data.get("MsgId") or "")
        ctime = int(data.get("CreateTime") or time.time())
        at_list = _parse_at(data.get("MsgSource", ""))
        push = _s(data.get("PushContent"))
        is_self = bool(self_wxid) and from_u == self_wxid
    elif "msgType" in payload:         # v2 扁平结构
        event = "AddMsg"
        app_id, self_wxid = payload.get("appid", ""), payload.get("wxid", "")
        from_u, to_u = payload.get("fromUser", ""), payload.get("toUser", "")
        content = payload.get("content", "") or ""
        mt = payload.get("msgType")
        msg_type = V2_TYPE_MAP.get(mt, mt if isinstance(mt, int) else 0)
        msg_id = str(payload.get("newMsgId") or payload.get("msgId") or "")
        ctime = int(payload.get("createTime") or time.time())
        at_list, push = [], ""
        is_self = bool(payload.get("isSelf", from_u == self_wxid))
    else:
        return None

    room_id, sender = None, from_u
    if from_u.endswith("@chatroom"):            # 别人在群里发
        room_id = from_u
        if ":\n" in content:
            sender, content = content.split(":\n", 1)
    elif to_u.endswith("@chatroom"):            # 自己(手机端)在群里发
        room_id, sender = to_u, self_wxid or from_u

    return InboundMessage(event=event, app_id=app_id, self_wxid=self_wxid, msg_id=msg_id,
                          msg_type=msg_type, from_wxid=sender, to_wxid=to_u, room_id=room_id,
                          content=content, create_time=ctime, is_self=is_self, at_list=at_list,
                          push_content=push, raw=payload)
