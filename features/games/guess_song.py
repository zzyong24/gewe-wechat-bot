"""猜歌（通用华语流行）。开关 game_guess_song（默认关）。
权衡：原 nonebot-plugin-guess-song 曲库是舞萌 DX 专用、且要音频；我们只发文字，又要避免歌词版权和模型「编歌词」，
所以用 **本地曲库（歌名/歌手/年份，事实信息）+ 分级提示**：
  提示 1（开局）：白名单群调 MiniMax 写一句「意境/故事」描述（禁止出现歌名、歌手、歌词，生成后再校验，不合格改用本地提示）；
                 非白名单群或模型失败 → 本地提示「年代 + 歌名字数」
  提示 2（40 秒）：歌手；提示 3（80 秒）：歌名拼音首字母；120 秒公布答案
  直接在群里发歌名即可抢答，猜中 +20 积分。
  @小助手 猜歌 开局；@小助手 结束游戏
"""
from __future__ import annotations

import random
import time

from pypinyin import Style, lazy_pinyin

from features.common import at_command, norm
from features.games import Game

SONGS = [  # (歌名, 歌手, 年份)
    ("晴天", "周杰伦", 2003), ("七里香", "周杰伦", 2004), ("稻香", "周杰伦", 2008), ("青花瓷", "周杰伦", 2007),
    ("夜曲", "周杰伦", 2005), ("简单爱", "周杰伦", 2001), ("告白气球", "周杰伦", 2016), ("双截棍", "周杰伦", 2001),
    ("江南", "林俊杰", 2004), ("她说", "林俊杰", 2010), ("修炼爱情", "林俊杰", 2013), ("小幸运", "田馥甄", 2015),
    ("后来", "刘若英", 2000), ("十年", "陈奕迅", 2003), ("浮夸", "陈奕迅", 2005), ("好久不见", "陈奕迅", 2007),
    ("红豆", "王菲", 1998), ("传奇", "王菲", 2010), ("匆匆那年", "王菲", 2014), ("月亮代表我的心", "邓丽君", 1977),
    ("甜蜜蜜", "邓丽君", 1979), ("童年", "罗大佑", 1982), ("光阴的故事", "罗大佑", 1981), ("恋曲1990", "罗大佑", 1989),
    ("朋友", "周华健", 1997), ("海阔天空", "Beyond", 1993), ("光辉岁月", "Beyond", 1990), ("真的爱你", "Beyond", 1989),
    ("吻别", "张学友", 1993), ("爱如潮水", "张信哲", 1993), ("倔强", "五月天", 2004), ("突然好想你", "五月天", 2008),
    ("平凡之路", "朴树", 2014), ("那些花儿", "朴树", 1999), ("生如夏花", "朴树", 2003), ("老男孩", "筷子兄弟", 2010),
    ("小苹果", "筷子兄弟", 2014), ("成都", "赵雷", 2016), ("演员", "薛之谦", 2015), ("丑八怪", "薛之谦", 2013),
    ("最炫民族风", "凤凰传奇", 2009), ("遇见", "孙燕姿", 2003), ("天黑黑", "孙燕姿", 2000), ("绿光", "孙燕姿", 2001),
    ("同桌的你", "老狼", 1994), ("光年之外", "邓紫棋", 2016), ("泡沫", "邓紫棋", 2012), ("默", "那英", 2015),
    ("征服", "那英", 1998), ("常回家看看", "陈红", 1999), ("时间都去哪儿了", "王铮亮", 2014),
    ("夜空中最亮的星", "逃跑计划", 2011), ("南山南", "马頔", 2014), ("安和桥", "宋冬野", 2013), ("小情歌", "苏打绿", 2006),
    ("新不了情", "万芳", 1993), ("千千阙歌", "陈慧娴", 1989), ("大海", "张雨生", 1992), ("我的未来不是梦", "张雨生", 1988),
]
HINT2_SEC, HINT3_SEC, END_SEC, REWARD = 40, 80, 120, 20
HINT_PROMPT = ("为猜歌游戏写一句提示（40 字以内），描述歌曲《{t}》（{a}，{y} 年）的意境、情绪或讲的故事。"
               "严禁出现歌名、歌手名字，严禁引用歌词原句。只输出这一句提示。")


def initials(title: str) -> str:
    return "".join(p[0].upper() if p and p[0].isalpha() else p for p in lazy_pinyin(title, style=Style.NORMAL))


def safe_hint(text: str, title: str, artist: str) -> bool:
    """提示不能泄底：不得含歌手名、不得含歌名里任意连续 2 字（单字歌名则不得含该字）。"""
    t = (text or "").strip()
    k = min(2, len(title))
    grams = {title[i:i + k] for i in range(len(title) - k + 1)}
    return bool(t) and len(t) <= 80 and artist not in t and not any(g in t for g in grams)


class GuessSongGame(Game):
    kind = "game_guess_song"
    title = "猜歌"
    idle_timeout = END_SEC + 30

    def __init__(self, room, llm=None, rng=None, economy=None, name_of=None, song=None, clock=time.time):
        super().__init__(room)
        self.rng, self.clock = rng or random.Random(), clock
        self.song = song or self.rng.choice(SONGS)
        self.eco, self.name_of = economy, name_of or (lambda r, w: w)
        self.t0, self.hints_sent = clock(), 1
        t, a, y = self.song
        local = f"{y // 10 * 10 % 100:02d} 年代的歌，歌名 {len(t)} 个字"
        self.hint1, self.hint_src = local, "本地"
        if llm is not None:
            try:
                h = llm.chat(HINT_PROMPT.format(t=t, a=a, y=y), system="你是猜歌游戏主持人。", max_completion_tokens=1024)
                if safe_hint(h, t, a):
                    self.hint1, self.hint_src = h.strip() + f"（歌名 {len(t)} 个字）", "MiniMax"
            except Exception:
                pass

    def intro(self):
        return f"🎵 猜歌开始！直接发歌名抢答（猜中 +{REWARD} 分）\n提示 1：{self.hint1}"

    def on_message(self, msg):
        t = norm(msg.command_text)
        if not t or len(t) > 12:
            return None
        if t == norm(self.song[0]):
            self.over = True
            name = self.name_of(self.room, msg.from_wxid)
            if self.eco:
                self.eco.add(self.room, msg.from_wxid, REWARD, "猜歌奖励")
            return f"🎉 {name} 猜中了！就是 {self.song[1]} 的《{self.song[0]}》（{self.song[2]}）+{REWARD} 分"
        return None

    def tick(self, now):
        el = now - self.t0
        if el >= END_SEC:
            self.over = True
            return f"⏰ 时间到！答案是 {self.song[1]} 的《{self.song[0]}》（{self.song[2]}）"
        if el >= HINT3_SEC and self.hints_sent < 3:
            self.hints_sent = 3
            return f"💡 提示 3：歌名拼音首字母 {initials(self.song[0])}"
        if el >= HINT2_SEC and self.hints_sent < 2:
            self.hints_sent = 2
            return f"💡 提示 2：歌手是 {self.song[1]}"
        return None

    def stop(self, by):
        self.over = True
        return f"🛑 猜歌结束，答案是 {self.song[1]} 的《{self.song[0]}》"


class GuessSongStartFeature:
    name = "games.guess_song"
    passive = False
    toggle_key = "game_guess_song"

    def __init__(self, manager, llm_for=None, economy=None, name_of=None, rng=None):
        self.m, self.llm_for, self.eco, self.name_of, self.rng = manager, llm_for, economy, name_of, rng

    def match(self, msg):
        return at_command(msg, ("猜歌", "猜歌名")) is not None

    def handle(self, msg):
        if self.m.get(msg.room_id):
            return self.m.start(self.m.get(msg.room_id))    # 返回「正在进行」提示
        g = GuessSongGame(msg.room_id, self.llm_for(msg.room_id) if self.llm_for else None, self.rng, self.eco,
                          self.name_of)
        return self.m.start(g) or g.intro()
