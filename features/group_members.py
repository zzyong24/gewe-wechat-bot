"""功能：群成员列表。触发词（群内）：「成员列表」「群成员」。
也可直接调用 fetch_members()/format_members() 供其它模块使用。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

TRIGGERS = ("成员列表", "群成员")


@dataclass
class Member:
    wxid: str
    nick_name: str
    display_name: str       # 群昵称，可能为空
    inviter: str | None
    is_owner: bool = False
    is_admin: bool = False

    @property
    def shown(self) -> str:
        return self.display_name or self.nick_name or self.wxid


def parse_member_list(data: dict) -> list[Member]:
    """解析 getChatroomMemberList 的 data（字段见 doc.geweapi.com/api-139908300）。"""
    owner = data.get("chatroomOwner") or data.get("chatRoomOwner")
    admins = data.get("adminWxid") or []
    if isinstance(admins, str):
        admins = [a for a in admins.split(",") if a]
    out = []
    for m in data.get("memberList") or []:
        w = m.get("wxid", "")
        out.append(Member(wxid=w, nick_name=m.get("nickName") or "", display_name=m.get("displayName") or "",
                          inviter=m.get("inviterUserName"), is_owner=(w == owner), is_admin=(w in admins)))
    out.sort(key=lambda x: (not x.is_owner, not x.is_admin, x.shown))
    return out


def format_members(members: list[Member], room_name: str = "", limit: int = 50) -> str:
    head = f"【{room_name or '本群'}】成员 {len(members)} 人"
    lines = [head]
    for i, m in enumerate(members[:limit], 1):
        tag = "（群主）" if m.is_owner else "（管理员）" if m.is_admin else ""
        extra = f" / 昵称:{m.nick_name}" if m.display_name and m.display_name != m.nick_name else ""
        lines.append(f"{i}. {m.shown}{tag}{extra}")
    if len(members) > limit:
        lines.append(f"…… 另有 {len(members) - limit} 人未列出")
    return "\n".join(lines)


class GroupMembersFeature:
    name = "group_members"
    passive = False
    toggle_key = "group_members"

    def __init__(self, fetch_list: Callable[[str], dict], fetch_info: Callable[[str], dict] | None = None):
        """fetch_list/fetch_info 注入：真实运行用 gewe_client，模拟时用假数据。"""
        self.fetch_list, self.fetch_info = fetch_list, fetch_info

    def fetch_members(self, room_id: str) -> tuple[str, list[Member]]:
        name = ""
        if self.fetch_info:
            try:
                name = (self.fetch_info(room_id) or {}).get("nickName", "")
            except Exception:
                name = ""
        return name, parse_member_list(self.fetch_list(room_id) or {})

    def match(self, msg) -> bool:
        return msg.is_group and msg.msg_type == 1 and msg.command_text in TRIGGERS

    def handle(self, msg):
        name, members = self.fetch_members(msg.room_id)
        return format_members(members, name)
