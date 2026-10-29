"""联网搜索工具（给陪聊模型用，和高德工具同一套机制）。按群开关 web_search。
只在像需要实时信息的问题时带上这个工具（新闻、价格、最新、某事怎么回事…），闲聊不带（省延迟和额度）。
每日上限：全局 BOT_SEARCH_DAILY=200 次、每群 BOT_SEARCH_DAILY_ROOM=60 次。
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("feature.search")

HINT = re.compile(
    r"新闻|最新|最近.{0,6}(怎么|如何|消息|情况|动态|进展|发布|出了)|今天.{0,8}(发生|消息|新闻|热搜|大事)|热搜|头条|"
    r"怎么回事|什么情况|发生了什么|咋回事|出什么事|官宣|宣布|发布会|刚发布|上市了|开售|"
    r"价格|多少钱|报价|股价|涨了|跌了|汇率|金价|油价|币价|比分|赛况|赛程|谁赢了|夺冠|票房|排名|榜单|"
    r"查一下|查查|搜一下|搜搜|搜下|帮我查|百度一下|谷歌|google|上网查|网上说|是真的吗|真的假的|辟谣|"
    r"现任|是谁|什么时候(上映|发布|开始|举办|结束)|几号(上映|发布|开始)|多少(人|钱|岁)|政策|规定|新规|"
    # 2026-10-22：陌生产品/版本/「现在」类问题也带上搜索（之前只认新闻价格类，导致 Muse/DeepSeek 那段一次没搜）
    r"版本|型号|现在是|目前是|现在.{0,4}(哪个|什么|多少)|目前.{0,4}(哪个|什么|多少)|截止|"
    r"是什么|是啥|啥是|什么是|干嘛的|做什么的|有什么(优势|区别|特点|用)|优势|闪光点|区别|对比|相比|比呢|哪个好|"
    r"知道.{0,12}(吗|嘛|么)|听说过|打错了|我说的是")
# 拉丁字母的词（产品名/品牌/型号，如 muse、DeepSeek、codex、dsh）：常见口头语除外
LATIN = re.compile(r"[A-Za-z][A-Za-z0-9.+\-]{2,}")
LATIN_STOP = {"haha", "hhh", "hhhh", "hah", "emm", "emmm", "lol", "lmao", "wow", "yes", "yeah", "okk", "okay", "nice",
              "good", "bye", "yyds", "xswl", "nmsl", "awsl", "omg", "wtf", "orz", "www", "http", "https", "com", "thx",
              "thanks", "sorry", "plz", "pls", "hello", "hey", "cool", "bro"}
# 对方明确不让联网
NO_SEARCH = re.compile(r"(不|别|不要|不用|无需|禁止|关掉)(使用|用|开)?(联网|上网|搜索|网络搜索|web ?search|搜)")
SPEC = {"type": "function", "function": {
    "name": "web_search",
    "description": ("联网搜索实时信息（新闻、价格、最新动态、某事怎么回事、事实核实），以及你没把握的产品/项目/公司名、版本号、"
                    "「现在/最新是哪个」这类问题。名字疑似打错时用你猜的最可能的写法去搜（可一次并行搜 2~3 个写法）。"
                    "闲聊、常识、地点天气路线别用它。"),
    "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "搜索词，简短具体，必要时带日期"}},
                   "required": ["query"]}}}
# 带了搜索工具时拼进 system（人设 2026-10-22：先搜再答、猜错字、最多反问一次）
TOOL_HINT = ("（这轮你带了 web_search 联网搜索工具。碰到没把握的产品/项目名、版本号、「现在/最新是哪个」这类问题，先搜再答，别凭印象说功能特点；"
             "名字像打错字的（比如 muns、mues 多半是 Manus），先猜最可能的名字去搜，可以同时搜两三个写法；"
             "搜到了就直接答，搜不到才问对方，同一件事最多问一次，别反复让人发官网链接。纯闲聊、夸你、吐槽就不用搜。"
             "答的时候照样像聊天：最多 3 条、总共两三百字以内，挑重点说，别列长清单。）")
GUIDE = ("（以下是联网搜索结果，按需要挑重点：用口语简短回答（两三句），别念标题清单；"
         "最多带 1 个来源（网站名或链接，挑最权威/最新的那条）；结果里没有的别编，搜不到就直说没查到。）")


class SearchTools:
    names = {"web_search"}
    ack = True

    def __init__(self, enabled, allow, blocked, counter=None, search_fn=None, configured=None):
        self.enabled, self.allow, self.blocked = enabled, allow, blocked
        self.counter = counter
        if search_fn is None:
            import minimax_media
            search_fn, configured = minimax_media.search, configured or minimax_media.configured
        self.search_fn = search_fn
        self.configured = configured or (lambda: True)
        self.daily = int(os.environ.get("BOT_SEARCH_DAILY", "200"))
        self.daily_room = int(os.environ.get("BOT_SEARCH_DAILY_ROOM", "60"))
        self.specs = [SPEC]

    def available(self, sess: str) -> bool:
        return bool(self.configured() and self.enabled(sess) and self.allow(sess) and not self.blocked(sess))

    @staticmethod
    def declined(text: str) -> bool:
        """对方明确说了不要联网搜索。"""
        return bool(NO_SEARCH.search((text or "").lower()))

    @staticmethod
    def _hit(text: str) -> bool:
        text = text or ""
        if HINT.search(text):
            return True
        return any(w.lower() not in LATIN_STOP for w in LATIN.findall(text))

    @classmethod
    def wants(cls, text: str, last_user: str = "") -> bool:
        if cls.declined(text):
            return False
        return cls._hit(text) or (len(text or "") <= 12 and cls._hit(last_user))

    def run(self, name: str, args: dict, sess: str, wxid: str) -> str:
        if not self.available(sess):
            return "搜索在这里不可用"
        q = str((args or {}).get("query") or "").strip()
        if not q:
            return "没给搜索词，没搜"
        if self.counter is not None and not self.counter.take({"search:all": self.daily, f"search:{sess}": self.daily_room}):
            return "今天联网搜索次数用完了，跟对方说明天再帮他查"
        try:
            items = self.search_fn(q)
        except Exception as e:
            log.warning("搜索失败 %s", str(e)[:200])
            return "搜索出错了，没查到"
        if not items:
            return f"搜「{q}」没有结果"
        lines = [GUIDE]
        for i, it in enumerate(items[:5], 1):
            lines.append(f"{i}. {it.get('title', '')}（{it.get('site', '')}{(' ' + it['date']) if it.get('date') else ''}）"
                         f"{it.get('snippet', '')} 链接：{it.get('link', '')}")
        return "\n".join(lines)
