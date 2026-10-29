"""群管理指令（功能 9 的入口）。仅群主 / 群管理员（或 BOT_ADMIN_WXIDS 里的超级管理员）可用，且必须 @小助手：
  @小助手 功能状态                 列出本群各功能开关
  @小助手 开启 发言统计            开启本群某功能（功能名见 settings.ALIASES）
  @小助手 关闭 陪聊                关闭本群某功能
  @小助手 恢复默认 陪聊            清除本群对该功能的设置（回落 全局/默认）
  @小助手 功能帮助                 指令说明
此功能本身不受开关控制（toggle_key=None），回复仍受 router 真发白名单约束。
"""
from __future__ import annotations

import os
import re
from typing import Callable

from features.settings import ALIASES, FEATURES, FeatureSettings, resolve

_CMD = re.compile(r"^(开启|打开|启用|关闭|停用|禁用|恢复默认)\s*(\S+)$")
HELP = ("🛠 功能设置指令（仅群主/管理员，需 @小助手）：\n"
        "· 功能状态\n· 开启 <功能>\n· 关闭 <功能>\n· 恢复默认 <功能>\n"
        "· 查看人设（设置人设 / 恢复默认人设 仅主人本人）\n· 日报（AI 群聊总结，需先开启）\n· 自动发言：进群欢迎 / 退群送别 / 主动插话（默认关，开启 <名字>）\n"
        "玩法：签到 积分榜 我的积分｜娶群友 求婚@ 离婚 现在对象｜拜师@ 收徒@ 师门｜商店 购买 背包｜雇佣@ 赎身 身价\n"
        "地图（需开启 地图）：北京天气｜望京附近的咖啡｜附近的奶茶（先发位置）｜从国贸到望京怎么走（可加 开车/公交/步行）\n"
        "游戏：猜成语 扫雷 成语接龙 谁是卧底 猜歌 五子棋（结束游戏）\n"
        "可用功能：" + "、".join(cn for cn, _ in FEATURES.values()))


BOT_OWNER_WXID = os.environ.get("BOT_OWNER_WXID", "").strip()   # 机器人主人的 wxid（env BOT_OWNER_WXID）；空 = 没有主人


def _env_set(name: str) -> set[str]:
    return {w.strip() for w in os.environ.get(name, "").split(",") if w.strip()}


def super_admins() -> set[str]:
    """超级管理员：主人本人（默认）+ BOT_ADMIN_WXIDS。功能开关等管理指令用。"""
    return ({BOT_OWNER_WXID} - {""}) | _env_set("BOT_ADMIN_WXIDS")


def bot_owners() -> set[str]:
    """机器人主人：只有主人本人（可用 BOT_OWNER_WXIDS 追加主人自己的其他号）。人设改动只认这里，
    不认群主/管理员，也不认 BOT_ADMIN_WXIDS。"""
    return ({BOT_OWNER_WXID} - {""}) | _env_set("BOT_OWNER_WXIDS")


class SettingsAdminFeature:
    name = "admin.settings"
    passive = False
    toggle_key = None

    def __init__(self, settings: FeatureSettings, fetch_list: Callable[[str], dict]):
        self.settings, self.fetch_list = settings, fetch_list

    def match(self, msg) -> bool:
        if not (msg.is_group and msg.msg_type == 1 and not msg.is_self and msg.self_wxid in msg.at_list):
            return False
        t = msg.command_text
        if "人设" in t:            # 「恢复默认人设」等交给 persona 指令
            return False
        return t in ("功能状态", "功能帮助", "功能设置") or bool(_CMD.match(t))

    def is_admin(self, room_id: str, wxid: str) -> bool:
        if wxid in super_admins():
            return True
        try:
            data = self.fetch_list(room_id) or {}
        except Exception:
            return False          # 查不到身份 → 一律拒绝
        owner = data.get("chatroomOwner") or data.get("chatRoomOwner")
        admins = data.get("adminWxid") or []
        if isinstance(admins, str):
            admins = [a for a in admins.split(",") if a]
        return wxid == owner or wxid in admins

    def handle(self, msg):
        t = msg.command_text
        if t in ("功能帮助", "功能设置"):
            return HELP
        if t == "功能状态":
            lines = ["⚙️ 本群功能开关"]
            for _, cn, on, src in self.settings.status(msg.room_id):
                lines.append(f"{'✅' if on else '⛔'} {cn}（{src}）")
            return "\n".join(lines)
        if not self.is_admin(msg.room_id, msg.from_wxid):
            return "只有群主或管理员可以修改功能开关哦～"
        verb, name = _CMD.match(t).groups()
        key = resolve(name)
        if not key:
            return f"没有叫「{name}」的功能。可用：" + "、".join(cn for cn, _ in FEATURES.values())
        cn = FEATURES[key][0]
        if verb == "恢复默认":
            self.settings.set(msg.room_id, key, None)
            on = self.settings.enabled(msg.room_id, key)
            return f"已恢复「{cn}」为默认（当前{'开启' if on else '关闭'}）"
        on = verb in ("开启", "打开", "启用")
        self.settings.set(msg.room_id, key, on)
        return f"已{'开启' if on else '关闭'}本群「{cn}」{'✅' if on else '⛔'}"
