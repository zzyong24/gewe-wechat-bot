"""谁是卧底。参考 Cat-bl/undercover-plugin「用大模型动态出词」的思路自己实现；开关 game_undercover（默认关）。
出词：模型白名单群调 MiniMax 生成一对相近词（JSON），失败/非白名单用本地词库；其余全是规则计算。

流程：
  @小助手 谁是卧底                 开始招募（发起人自动加入），3 分钟内其他人发「加入」报名（4~10 人），「退出」取消报名
  开始（任一已报名者，≥4 人）        分配身份：≤6 人 1 个卧底，7~10 人 2 个
  查词：⚠️ 铁律不主动私信。替代方案：
      ① 群内「词卡链接」：每人一个不可猜的图片链接（图床），只点自己的——君子协定，技术上不保密；
      ② 玩家主动私聊小助手发「查词」，小助手回复其词（这是被动回复，但目前私聊不在真发白名单，需主人放行后才会真发）。
  发言：按顺序轮流，轮到谁谁在群里发一句描述（60 秒超时跳过；说出自己的词判犯规出局）
  投票：发「投 3」或「投 @某人」（90 秒，全员投完提前结算）；票最多者出局，平票无人出局
  胜负：卧底全出局→平民胜；卧底人数 ≥ 平民→卧底胜；最多 6 轮
  @小助手 结束游戏 可随时结束（公布词）
"""
from __future__ import annotations

import json
import random
import re
import time
from collections import Counter

from features.common import at_command, at_targets, norm
from features.games import Game

MIN_P, MAX_P = 4, 10
RECRUIT_SEC, SPEAK_SEC, VOTE_SEC, MAX_ROUNDS = 180, 60, 90, 6
LOCAL_PAIRS = [("饺子", "馄饨"), ("牛奶", "豆浆"), ("眉毛", "胡子"), ("麦当劳", "肯德基"), ("微信", "QQ"),
               ("口红", "唇膏"), ("火锅", "麻辣烫"), ("地铁", "公交"), ("西瓜", "哈密瓜"), ("猫", "老虎"),
               ("篮球", "排球"), ("雨伞", "雨衣"), ("包子", "馒头"), ("手机", "平板"), ("咖啡", "奶茶"),
               ("蜘蛛侠", "蝙蝠侠"), ("高铁", "飞机"), ("月饼", "粽子"), ("婚纱", "旗袍"), ("双胞胎", "龙凤胎")]
THEMES = ["食物", "饮料", "动物", "职业", "交通工具", "运动", "电器", "水果", "影视角色", "节日", "地名", "文具", "衣物", "乐器"]
WORD_PROMPT = ('出一组「谁是卧底」词语，主题：{theme}。两个中文词，同类、相近但不同，2~4 个字，日常常见，避免敏感内容，'
               '不要用「饺子/馄饨」这类烂大街的组合。只返回 JSON：{{"civilian": "平民词", "undercover": "卧底词"}}')


def gen_words(llm=None, rng=None) -> tuple[str, str, str]:
    """返回 (平民词, 卧底词, 来源)。"""
    rng = rng or random
    if llm is not None:
        try:
            raw = llm.chat(WORD_PROMPT.format(theme=rng.choice(THEMES)), system="你是聚会游戏主持人，只输出 JSON。", max_completion_tokens=1024)
            d = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            c, u = str(d["civilian"]).strip(), str(d["undercover"]).strip()
            if c and u and c != u and len(c) <= 6 and len(u) <= 6:
                return c, u, "MiniMax"
        except Exception:
            pass
    c, u = rng.choice(LOCAL_PAIRS)
    if rng.random() < 0.5:
        c, u = u, c
    return c, u, "本地词库"


class UndercoverGame(Game):
    kind = "game_undercover"
    title = "谁是卧底"
    idle_timeout = 1800

    def __init__(self, room, host, name_of=None, llm=None, rng=None, word_link=None, clock=time.time):
        super().__init__(room)
        self.rng, self.llm, self.clock = rng or random.Random(), llm, clock
        self.name_of = name_of or (lambda r, w: w)
        self.word_link = word_link        # word_link(word, tag) -> URL 或 None（图床词卡）
        self.players: list[str] = [host]
        self.phase, self.deadline = "recruit", clock() + RECRUIT_SEC
        self.words, self.spies, self.alive = {}, set(), []
        self.round, self.order, self.turn, self.votes = 0, [], 0, {}
        self.source = ""

    def n(self, w):
        return self.name_of(self.room, w)

    # ---- 招募 ----
    def intro(self):
        return (f"🕵️ 谁是卧底招募中！发起人：{self.n(self.players[0])}\n发「加入」报名（{MIN_P}~{MAX_P} 人），"
                f"人够了任一玩家发「开始」。{RECRUIT_SEC // 60} 分钟内人不够自动取消。")

    def _start(self):
        k = len(self.players)
        self.civ_word, self.spy_word, self.source = gen_words(self.llm, self.rng)
        self.spies = set(self.rng.sample(self.players, 1 if k <= 6 else 2))
        self.words = {p: (self.spy_word if p in self.spies else self.civ_word) for p in self.players}
        self.alive = list(self.players)
        self.rng.shuffle(self.alive)
        lines = [f"🎬 游戏开始！{k} 人，卧底 {len(self.spies)} 个（出词：{self.source}）", "👉 查看自己的词："]
        for i, p in enumerate(self.alive, 1):
            url = None
            if self.word_link:
                try:
                    url = self.word_link(self.words[p], f"{i}")
                except Exception:
                    url = None
            lines.append(f"{i}. {self.n(p)}：{url}" if url else f"{i}. {self.n(p)}：私聊小助手发「查词」")
        lines.append("（词卡链接请只点自己的哦，君子协定～）")
        return ["\n".join(lines), self._new_round()]

    def _new_round(self):
        self.round += 1
        self.phase, self.order, self.turn = "speak", list(self.alive), 0
        self.deadline = self.clock() + SPEAK_SEC
        return f"🗣 第 {self.round} 轮发言（每人 {SPEAK_SEC} 秒）：请 {self.n(self.order[0])} 描述你的词（别直接说出来）"

    def _next_speaker(self, prefix=""):
        self.turn += 1
        if self.turn >= len(self.order):
            self.phase, self.votes, self.deadline = "vote", {}, self.clock() + VOTE_SEC
            roster = "\n".join(f"{i}. {self.n(p)}" for i, p in enumerate(self.alive, 1))
            return prefix + f"🗳 发言结束，开始投票（{VOTE_SEC} 秒）！发「投 序号」或「投 @某人」：\n{roster}"
        self.deadline = self.clock() + SPEAK_SEC
        return prefix + f"请 {self.n(self.order[self.turn])} 描述"

    def _tally(self):
        c = Counter(self.votes.values())
        detail = "、".join(f"{self.n(p)} {v} 票" for p, v in c.most_common()) or "没人投票"
        top = c.most_common(2)
        if not top or (len(top) > 1 and top[0][1] == top[1][1]):
            msg = f"📊 投票结果：{detail}\n平票，本轮无人出局。"
        else:
            out = top[0][0]
            self.alive.remove(out)
            msg = f"📊 投票结果：{detail}\n{self.n(out)} 出局，TA 是{'卧底' if out in self.spies else '平民'}！"
        return self._judge(msg)

    def _check_end(self, msg):
        spies = [p for p in self.alive if p in self.spies]
        civ = [p for p in self.alive if p not in self.spies]
        if not spies:
            return self._end(msg + "\n🎉 卧底全部出局，平民胜利！")
        if len(spies) >= len(civ):
            return self._end(msg + "\n😈 卧底人数追平平民，卧底胜利！")
        return None

    def _judge(self, msg):
        e = self._check_end(msg)
        if e:
            return e
        if self.round >= MAX_ROUNDS:
            return self._end(msg + f"\n⌛ 已到 {MAX_ROUNDS} 轮，卧底坚持到最后，卧底胜利！")
        return msg + "\n" + self._new_round()

    def _end(self, msg):
        self.over = True
        return msg + "\n" + self.reveal()

    def reveal(self):
        if not self.words:
            return ""
        return (f"🔍 平民词：{self.civ_word}｜卧底词：{self.spy_word}\n卧底是："
                + "、".join(self.n(p) for p in self.players if p in self.spies))

    # ---- 消息 ----
    def on_message(self, msg):
        t, me = msg.command_text.strip(), msg.from_wxid
        if self.phase == "recruit":
            if t in ("加入", "报名", "上车", "+1"):
                if me in self.players:
                    return f"{self.n(me)} 已经报名啦"
                if len(self.players) >= MAX_P:
                    return f"已满 {MAX_P} 人"
                self.players.append(me)
                self.deadline = max(self.deadline, self.clock() + 60)
                return f"✅ {self.n(me)} 加入（{len(self.players)} 人）：" + "、".join(self.n(p) for p in self.players)
            if t in ("退出", "不玩了") and me in self.players:
                self.players.remove(me)
                if not self.players:
                    self.over = True
                    return "大家都退出了，游戏取消～"
                return f"{self.n(me)} 退出报名（{len(self.players)} 人）"
            if t in ("开始", "开始游戏") and me in self.players:
                if len(self.players) < MIN_P:
                    return f"至少 {MIN_P} 人才能开始，现在 {len(self.players)} 人"
                return self._start()
            return None
        if me not in self.alive:
            return None
        if self.phase == "speak":
            if me != self.order[self.turn] or t.startswith(("投", "加入")):
                return None
            if self.words[me] in t or norm(self.words[me]) in norm(t):
                self.alive.remove(me)
                self.order.remove(me)
                self.turn -= 1
                foul = f"🚫 {self.n(me)} 说出了自己的词，犯规出局！TA 是{'卧底' if me in self.spies else '平民'}"
                return self._check_end(foul) or self._next_speaker(foul + "\n")
            return self._next_speaker(f"📝 {self.n(me)}：{t}\n")
        if self.phase == "vote":
            m = re.match(r"^投(?:票)?\s*(\d+)?", t)
            if not m:
                return None
            target = None
            tg = at_targets(msg)
            if tg and tg[0] in self.alive:
                target = tg[0]
            elif m.group(1) and 1 <= int(m.group(1)) <= len(self.alive):
                target = self.alive[int(m.group(1)) - 1]
            else:
                rest = norm(t[1:].lstrip("票"))
                target = next((p for p in self.alive if rest and norm(self.n(p)) == rest), None)
            if not target:
                return "没找到这个人，发「投 序号」试试"
            if target == me:
                return "不能投自己哦～"
            self.votes[me] = target
            if len(self.votes) >= len(self.alive):
                return self._tally()
            return f"🗳 {self.n(me)} 已投票（{len(self.votes)}/{len(self.alive)}）"
        return None

    def private_word(self, wxid):
        return self.words.get(wxid)

    # ---- 超时推进 ----
    def tick(self, now):
        if self.over:
            return None
        if now - self.last_active > self.idle_timeout:
            return self._end("⏰ 太久没动静，游戏结束。")
        if now < self.deadline:
            return None
        self.touch()
        if self.phase == "recruit":
            if len(self.players) >= MIN_P:
                return self._start()
            self.over = True
            return f"⏰ 招募时间到，只有 {len(self.players)} 人，谁是卧底取消～"
        if self.phase == "speak":
            return self._next_speaker(f"⏰ {self.n(self.order[self.turn])} 超时，跳过。\n")
        if self.phase == "vote":
            return self._tally()
        return None

    def stop(self, by):
        self.over = True
        return "🛑 谁是卧底已结束。\n" + self.reveal()


class UndercoverStartFeature:
    name = "games.undercover"
    passive = False
    toggle_key = "game_undercover"

    def __init__(self, manager, name_of=None, llm_for=None, rng=None, word_link=None):
        """llm_for(room) -> llm 或 None（非模型白名单群返回 None → 用本地词库）。"""
        self.m, self.name_of, self.llm_for, self.rng, self.word_link = manager, name_of, llm_for, rng, word_link

    def match(self, msg):
        return at_command(msg, ("谁是卧底", "卧底")) is not None

    def handle(self, msg):
        g = UndercoverGame(msg.room_id, msg.from_wxid, self.name_of,
                           self.llm_for(msg.room_id) if self.llm_for else None, self.rng, self.word_link)
        return self.m.start(g) or g.intro()


class UndercoverPrivateWordFeature:
    """私聊「查词」：被动回复玩家自己的词。回复对象是私聊 wxid，仍受真发白名单约束（目前私聊不在白名单 → simulate）。"""
    name = "games.undercover.word"
    passive = False
    toggle_key = None

    def __init__(self, manager):
        self.m = manager

    def match(self, msg):
        return (msg.event == "AddMsg" and not msg.is_group and msg.msg_type == 1 and not msg.is_self
                and msg.content.strip() in ("查词", "我的词"))

    def handle(self, msg):
        for g in list(self.m.games.values()):
            if isinstance(g, UndercoverGame) and not g.over and g.private_word(msg.from_wxid):
                return f"🤫 你在「谁是卧底」里的词是：{g.private_word(msg.from_wxid)}"
        return "你现在没有参加进行中的谁是卧底～"
