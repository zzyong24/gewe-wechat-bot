"""小助手「真实能做的事」清单：按本群功能开关动态生成，注入陪聊 system prompt；以及「帮开局」。

目的：模型不许答应自己没有的功能（例：狼人杀「我来当主持、大家报到」——其实没有这个游戏，报到后什么也不会发生）。
有的游戏 → 给口令或直接帮开局；没有的 → 坦白说还不会，推荐现有的。
"""
from __future__ import annotations

import dataclasses
import re

# toggle_key → (名字, 开局口令, 一句说明)
GAMES = {
    "game_undercover": ("谁是卧底", "谁是卧底", "4~10 人；开局后大家发「加入」报名，人够了发「开始」；身份词我在群里给每人一个只点自己的词卡链接（不私聊）"),
    "game_idiom_chain": ("成语接龙", "成语接龙", "我先出一个，大家直接发成语接"),
    "game_handle": ("猜成语", "猜成语", "汉字版 Wordle，直接发四字成语猜，10 次机会"),
    "game_guess_song": ("猜歌", "猜歌", "我给提示，大家直接发歌名抢答"),
    "game_minesweeper": ("扫雷", "扫雷", "可加 初级/中级/高级，发「挖开 A1」「标记 B2」"),
    "game_gomoku": ("五子棋", "五子棋", "两个群友对下，我只当裁判摆棋盘；不 @ 我也行，发「五子棋」等人加入，或「下五子棋 @某人」点名邀请，"
                    "对方回「加入」开始；开局人执黑⚫先下，轮到谁谁发两位数落子（55=第5行第5列）；「认输」「结束五子棋」"),
}
START_TAG = re.compile(r"[【\[]\s*开局\s*[:：]\s*([^】\]\s]+)\s*[】\]]")
NOT_SUPPORTED = "狼人杀、剧本杀、真心话大冒险、你画我猜、斗地主/麻将等牌类、发红包、私聊给每个人发身份"
# 不编：没有的非游戏能力，模型常顺口答应（如「发文档给我能基于它回答」）——明确列出来
NO_ABILITIES = ("读文档/文件/PDF/表格/压缩包等附件、打开普通网页链接读全文（公众号文章除外）、看视频、听歌或音乐、发文件、"
                "设提醒/闹钟/定时任务、私聊别人、拉人进群/加好友、转账发红包、接入外部知识库/上传资料库")

# 「来一局谁是卧底」「玩扫雷吧」「开个成语接龙」→ 直接开局
_START = re.compile(r"(?:来|玩|开|整|搞|开始|下)(?:一|个|一个|一把|一局|局|把|一盘|盘)?\s*(谁是卧底|卧底|成语接龙|接龙|猜成语|猜歌|扫雷|五子棋)")
_ALIAS = {"卧底": "谁是卧底", "接龙": "成语接龙"}
# 小助手上一句推荐了某个游戏，对方回「ok/好/开始/来」→ 开那个
_YES = re.compile(r"^(?:ok|OK|Ok|好|好的|好呀|好啊|行|可以|来|来吧|来呀|开|开始|开始吧|开吧|走|走起|冲|嗯|嗯嗯|安排)(?:\s*(?:开始|开|来|吧|呀|啊|！|!|。))*$")


def describe(enabled, sess: str, play_features=(), running=None) -> str:
    """enabled(sess, key) → bool；running(sess) → 进行中的游戏名或 None。返回给模型看的能力清单。"""
    games = [(v[0], v[1], v[2]) for k, v in GAMES.items() if enabled(sess, k)]
    lines = ["【你（小助手）在这个群里真实能做的事——只有下面这些，口令要 @小助手 才生效】"]
    cur = running(sess) if running else None
    if cur:
        lines.append(f"⚠️ 本群现在正在进行「{cur}」：题目、答案、轮次都由游戏系统管，你看不到——绝不自己出题、报答案或编进度；"
                     "有人问怎么玩就简单说规则，想换游戏让他先「@小助手 结束游戏」。别写【开局:…】。")
    if games:
        lines.append("游戏：" + "；".join(f"{n}（口令：@小助手 {c}；{d}）" for n, c, d in games)
                     + "；随时「@小助手 结束游戏」。同一时间一个群只能有一局。")
    else:
        lines.append("游戏：这个群目前没开任何游戏。")
    cmds = []
    for f in play_features:
        k = getattr(f, "toggle_key", None)
        if k and enabled(sess, k):
            cmds.extend(list(getattr(f, "COMMANDS", {}))[:3])
    if cmds:
        lines.append("签到玩法口令：" + "、".join(dict.fromkeys(cmds)))
    if enabled(sess, "chat_companion"):
        lines.append("读公众号文章：有人在群里转发公众号文章后 @你、或引用那张文章卡片跟你说话，系统会把正文给你，你据此总结或回答；"
                     "只在被 @/引用时读，不主动读；读不到（验证页、已删除、付费）就如实说，绝不编；其它网站的链接打不开。")
    if enabled(sess, "amap"):
        lines.append("地图：查天气、地点/店铺、附近、路线（你自己有工具可以直接查）。")
    if enabled(sess, "web_search"):
        lines.append("联网搜索：新闻、价格、最新动态、陌生的产品名/版本号这类问题，系统会在需要时给你联网搜索工具"
                     "（只拿到搜索结果摘要，打不开链接读全文）；查完简短说，最多带 1 个来源；闲聊别查。")
    if enabled(sess, "image_gen"):
        lines.append("画图：口令「@小助手 画一张XX」，你也可以直接帮人画（每群每天 10 张，约 20 秒出图；不画真人、色情、暴力、政治）。")
    if enabled(sess, "memory"):
        lines.append("长期记忆：你会记住群友的少量稳定信息（称呼、偏好、常聊话题），系统会附给你；谁说「@小助手 忘掉我」就清掉他的。")
    if enabled(sess, "ai_news"):
        lines.append("AI 快讯：本群白天（大约 8 点到 23 点）每个整点自动推一期 AI 快讯（定时推送，不能临时点播）。")
    if enabled(sess, "vision"):
        lines.append("看图：对你说话时附的图（@你并引用图片、或刚发图后 @你问）你能看懂；系统会把图一起给你，没给就说没看到。")
    if enabled(sess, "asr"):
        lines.append("听语音：对你说的语音（跟你聊着时发的语音）会自动转成文字给你。")
    if enabled(sess, "voice_reply"):
        lines.append("语音：你能发语音——有人让你用语音说时，系统会把你这条回复转成语音发出去（你照常写要说的话就行），平时也会偶尔自动用语音。")
    off = [n for k, n in (("web_search", "联网搜索"), ("vision", "看图"), ("asr", "听语音"), ("image_gen", "画图"),
                          ("voice_reply", "发语音"), ("amap", "查地图/天气/路线"), ("memory", "长期记忆")) if not enabled(sess, k)]
    if off:
        lines.append("本群没开（你在这里做不了，别说能做）：" + "、".join(off) + "。")
    lines.append("另外就是陪聊。上面没列的能力你都没有，比如" + NO_ABILITIES
                 + "——有人问就直说做不了（文档可以让对方把关键内容直接打字或截图发来），别说「发给我我能看」。")
    lines.append(f"没有的功能（比如{NOT_SUPPORTED}）你都做不了：绝不答应、不说「我来当主持/发身份/大家先报到」这种话。"
                 "有人想玩没有的游戏时，口语化坦白说这个还不会，然后推荐上面有的游戏并告诉口令（或说「回我一句开始我就开」）。"
                 "游戏只有真正开局才会开始——你光在聊天里说「好，开始」不会开局。"
                 + ("对方明确想玩上面某个游戏时（点名了、或你只推荐了一个他说好/开始、或你列了几个他回 1/2/3 选了一个），"
                    "在回复最后单独一行写【开局:游戏名】（例：【开局:猜成语】），系统会马上真开局，你就别再让他自己发口令了；"
                    "没确定玩哪个就别写，先问。" if games else ""))
    return "\n".join(lines)


def wanted_game(text: str, last_bot: str, enabled_names: list[str]) -> str | None:
    """从这句话（及小助手上一句）推断要开哪个游戏；返回游戏名（=口令）或 None。"""
    t = (text or "").strip()
    m = _START.search(t)
    if m and (t[max(0, m.start() - 1):m.start()] in ("不", "别", "没") or re.search(r"吗|[?？]$", t)):
        m = None                                  # 「不下五子棋」「会下五子棋吗」不算开局
    if m:
        name = _ALIAS.get(m.group(1), m.group(1))
        return name if name in enabled_names else None
    if _YES.match(t) and last_bot:
        offered = [n for n in enabled_names if n in last_bot]
        if len(offered) == 1:
            return offered[0]
    return None


class GameStarter:
    """陪聊里识别开局意图 → 调对应游戏的开局功能（同样受本群开关约束）。"""

    def __init__(self, enabled, start_features: dict, self_name: str = "小助手", running=None):
        self.enabled, self.start_features, self.self_name = enabled, start_features, self_name
        self.running = running or (lambda room: None)        # running(room) → 进行中的游戏名或 None

    def __call__(self, msg, text: str, last_bot: str):
        sess = msg.room_id or msg.from_wxid
        names = {GAMES[k][1]: k for k in GAMES if k in self.start_features and self.enabled(sess, k)}
        name = wanted_game(text, last_bot, list(names))
        return self.start(msg, name) if name else None

    def start(self, msg, name: str):
        """按游戏名开局（模型回复里的【开局:名】也走这里）；本群没开 / 不认识 → None。"""
        sess = msg.room_id or msg.from_wxid
        if self.running(sess):
            return None                                       # 已有一局：不重复开（也不回「正在进行」）
        name = _ALIAS.get(name, name)
        names = {GAMES[k][1]: k for k in GAMES if k in self.start_features and self.enabled(sess, k)}
        if name not in names:
            return None
        f = self.start_features[names[name]]
        if f.match(msg):                                      # 原话本身就是开局口令：原样交给它（保留 @ 的人和昵称）
            return f.handle(msg)
        clone = dataclasses.replace(msg, content=f"@{self.self_name}\u2005{name}",
                                    at_list=list(dict.fromkeys(list(msg.at_list) + [msg.self_wxid])))
        if not f.match(clone):
            return None
        return f.handle(clone)
