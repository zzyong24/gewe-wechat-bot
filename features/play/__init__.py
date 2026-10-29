"""功能 7：签到养成。所有玩法基于 features/economy.py（积分 + 关系），纯规则计算，不调模型。"""
from __future__ import annotations

import random
from typing import Callable

from features.common import at_command


class PlayContext:
    """玩法共用依赖：经济库、名字解析、群成员列表、随机数（测试可注入）。"""

    def __init__(self, eco, name_of: Callable[[str, str], str], members_of: Callable[[str], list[str]],
                 rng: random.Random | None = None, settings=None):
        self.eco, self._name_of, self.members_of = eco, name_of, members_of
        self.rng = rng or random.Random()
        self.settings = settings

    def name(self, room, wxid) -> str:
        try:
            return self._name_of(room, wxid) or wxid
        except Exception:
            return wxid

    def enabled(self, room, key) -> bool:
        return self.settings.enabled(room, key) if self.settings else True


class CommandFeature:
    """@小助手 <指令> 型功能基类。子类定义 COMMANDS = {指令名: 方法名}。"""
    name = "play"
    passive = False
    toggle_key: str | None = None
    COMMANDS: dict[str, str] = {}

    def __init__(self, ctx: PlayContext):
        self.ctx = ctx

    def match(self, msg) -> bool:
        return self._parse(msg) is not None

    def _parse(self, msg):
        for cmd in sorted(self.COMMANDS, key=len, reverse=True):
            arg = at_command(msg, (cmd,))
            if arg is not None:
                return cmd, arg
        return None

    def handle(self, msg):
        cmd, arg = self._parse(msg)
        return getattr(self, self.COMMANDS[cmd])(msg, arg)
