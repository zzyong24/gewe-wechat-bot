"""成语接龙。自己实现：词库复用猜成语的 idioms.txt（29k 条，本地校验，不调模型）。开关 game_idiom_chain（默认开）。

  @小助手 成语接龙        小助手出第一个成语
  直接发成语             首字与上一个成语末字同音（不计声调）即可接上；每接一个 +1 分
  提示 / 跳过            小助手给出一个能接的成语（提示不计分；跳过=小助手替大家接一个）
  90 秒没人接上自动结束，按接龙数结算积分（每个 +5 分，写入签到积分账户）。
"""
from __future__ import annotations

import random
import re
from collections import Counter, defaultdict
from functools import lru_cache

from pypinyin import Style, lazy_pinyin

from features.common import at_command
from features.games import Game
from features.games.handle import idioms

_HAN4 = re.compile(r"^[\u4e00-\u9fff]{4}$")
COIN_PER = 5


def py(ch: str) -> str:
    return lazy_pinyin(ch, style=Style.NORMAL)[0]


@lru_cache(1)
def by_first() -> dict[str, list[str]]:
    d = defaultdict(list)
    for w in sorted(idioms()):
        if len(w) == 4:
            d[py(w[0])].append(w)
    return d


def candidates(word: str, used=()) -> list[str]:
    return [w for w in by_first().get(py(word[-1]), []) if w not in used]


class IdiomChainGame(Game):
    kind = "game_idiom_chain"
    title = "成语接龙"
    idle_timeout = 90

    def __init__(self, room, rng=None, economy=None, name_of=None, first: str | None = None):
        super().__init__(room)
        self.rng = rng or random.Random()
        self.eco, self.name_of = economy, name_of or (lambda r, w: w)
        from features.games.handle import answers
        pool = [a["word"] for a in answers() if len(a["word"]) == 4 and candidates(a["word"])]
        self.cur = first or self.rng.choice(pool)
        self.used, self.score = {self.cur}, Counter()

    def intro(self):
        return (f"🐉 成语接龙开始！第一个：【{self.cur}】\n请接「{self.cur[-1]}」（{py(self.cur[-1])}，同音即可）。"
                "发「提示」看可接的成语，90 秒没人接上自动结束。")

    def on_message(self, msg):
        t = msg.command_text.strip()
        if t in ("提示", "跳过"):
            c = candidates(self.cur, self.used)
            if not c:
                self.over = True
                return f"「{self.cur[-1]}」字开头的成语被用完了！" + self.settle()
            pick = self.rng.choice(c)
            if t == "提示":
                return f"💡 可以试试：{pick[0]}＿＿＿（首字 {pick[0]}）"
            self._accept(pick)
            return f"⏭ 小助手替大家接：【{pick}】→ 请接「{pick[-1]}」（{py(pick[-1])}）"
        if not _HAN4.match(t):
            return None
        if py(t[0]) != py(self.cur[-1]):
            return None                       # 普通四字聊天，不打扰
        if t not in idioms():
            return f"「{t}」不在成语词库里～"
        if t in self.used:
            return f"「{t}」已经用过啦"
        self._accept(t)
        self.score[msg.from_wxid] += 1
        nxt = candidates(t, self.used)
        name = self.name_of(self.room, msg.from_wxid)
        if not nxt:
            self.over = True
            return f"✅ {name} 接【{t}】！「{t[-1]}」已经没有能接的成语了，绝杀！" + self.settle()
        return f"✅ {name} 接【{t}】+1（共 {self.score[msg.from_wxid]}）→ 请接「{t[-1]}」（{py(t[-1])}）"

    def _accept(self, w):
        self.cur = w
        self.used.add(w)

    def settle(self) -> str:
        if not self.score:
            return "\n本局没人接上，下次加油～"
        lines = [f"\n🏁 本局共接 {sum(self.score.values())} 个"]
        for w, c in self.score.most_common(10):
            coins = c * COIN_PER
            if self.eco:
                self.eco.add(self.room, w, coins, "成语接龙奖励")
            lines.append(f"· {self.name_of(self.room, w)} {c} 个（+{coins} 分）")
        return "\n".join(lines)

    def on_timeout(self):
        return f"⏰ 90 秒没人接上「{self.cur[-1]}」，接龙结束！" + self.settle()

    def stop(self, by):
        self.over = True
        return "🛑 成语接龙结束。" + self.settle()


class IdiomChainStartFeature:
    name = "games.idiom_chain"
    passive = False
    toggle_key = "game_idiom_chain"

    def __init__(self, manager, economy=None, name_of=None, rng=None):
        self.m, self.eco, self.name_of, self.rng = manager, economy, name_of, rng

    def match(self, msg):
        return at_command(msg, ("成语接龙", "接龙")) is not None

    def handle(self, msg):
        g = IdiomChainGame(msg.room_id, self.rng, self.eco, self.name_of)
        return self.m.start(g) or g.intro()
