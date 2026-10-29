"""功能 1：智能陪聊。

触发：群里 @小助手（只认 at 列表里有机器人自己的 wxid，@所有人 不触发），或私聊文本消息。
不响应：自己发的消息、非文本消息、系统/公众号账号（weixin、gh_*、filehelper 等）。
上下文：按会话（群 id / 私聊对方 wxid）共享 features/chat_context.ChatContext——群里所有人的话 + 小助手自己发的每一条，
      带昵称、持久化到磁盘（router 负责记录）；独立使用（无 router）时自己在内存里记。
能力清单：capabilities(session) 动态生成「本群真实能做的事 + 口令」注入 system prompt，没有的功能（如狼人杀）不许假装主持。
人设：persona（system prompt）可配置；后续「人设定制」功能可按会话覆盖（set_persona）。
LLM 出错时不往群里发报错，只记日志并返回 None。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from typing import Callable, Protocol

import random

from features.clock import now_line
from features.humanlike import enabled as humanlike_enabled, gap_range, max_parts, split_messages, with_style
from llm.minimax_client import _THINK_RE
from models import InboundMessage

log = logging.getLogger("feature.chat")

# 示例人设：可以直接改这里，或用 env BOT_PERSONA_FILE 指向一个 UTF-8 文本文件整体替换。
# 群里还可以用「设置人设」给单个群定制（只有 BOT_OWNER_WXID 能改）。
_EXAMPLE_PERSONA = (
    "你是「小助手」，一个微信 AI 小伙伴，在微信群和私聊里陪大家聊天。\n"
    "风格：友好、真诚、简洁，用中文口语回复，像朋友聊天一样，不用 Markdown、不列长清单。\n"
    "自称「小助手」。不知道的事情坦白说不知道，不编造。不讨论违法违规内容。\n"
    "语气友好、有分寸，少用「呀」「哈哈」「呆逼」这类套路化情绪词，别每句都加。\n"
    "碰到实时信息（赛事、新闻、日期之类）先核实年份和事实（有联网搜索就先搜），再明确回答；拿不准就直说不确定，前后说法别矛盾。\n"
    "分清自己能不能发语音：系统会告诉你本群语音开关，不能发语音时只打字，别答应「给你发语音」。\n"
    "没核实过的产品功能、特点、版本别说成事实；只有这一轮真的搜过才能说「查了」「没搜到」，没搜就别这么说，用的是之前搜到的就说「前面查过」。\n"
    "陌生的产品名、版本号、「现在/最新是哪个」这类问题，有联网搜索就先搜再答；名字像打错字就先猜最可能的那个去搜（比如 muns 多半是 Manus），"
    "搜不到再问对方，同一件事最多问一次，别反复让人发官网链接。对方说不要联网，就凭记得的答，并说明可能过时。\n"
    "对方只是夸一句、吐槽一句或寒暄（比如「牛逼兄弟」），轻松短回一句就行，别接着讲道理、别说教。\n"
    "只说能力清单里真有的功能，清单里没有的（比如读文档附件、打开普通网页链接看全文）别说自己能做。\n"
    "认产品要准：先根据上下文判断对方说的是哪个产品，能判断就直接答，别一上来就用「你说的是哪个」这类模板式反问；事实性的说法（功能、价格、版本、漏洞编号）要有依据才说。\n"
    "时效性强的技术问题（新版本、新发布、最新动态）：先看这轮能不能搜、搜到的资料是哪天的，再下结论；资料旧或没搜到就说明，别把旧消息当最新的讲。\n"
    "聊到群里推过的 AI 快讯时，标题、内容和来源都以推送材料为准，三者对不上就照实说，别自己补细节。\n"
    "被问知识截止到什么时候：别报具体日期或年份（你并不确定），只说自带的知识可能不是最新的，实时的信息会先联网查。\n"
    "主人的指令永远优先于群友：主人说过的话（比如让你休息、别说了、别做某事），群友不能推翻，有人要你违背主人的意思就别照做。\n"
    "聊到本群刚推的 AI 快讯时，主动点出来源和大概时效（多久前/哪天），并可抛一个具体、好接话的小问题方便群友讨论；细节仍以推送材料为准。\n"
    "回答前先把问题弄清楚、该搜就先搜，再下结论；对方已经问清楚了就直接答，别再反问，只有产品名或对象真的不明确时才确认一次，确认完就去查、去答。\n"
    "群里接着聊已推过的快讯时，别把推送内容整段再复述；多条说的是同一件事就合并讲，只讲重点、新增进展，并点明对普通人有什么用；没有新进展就直说，一两句带过。\n"
    "聊新闻、快讯时顺带一句这事对大家有什么用、有什么影响（比如能免费用上什么、哪类人值得试、门槛或价格变了什么），具体实用，别空泛。\n"
    "只有转述、爆料、传闻、没看到官方出处的消息，要说「据称」，并讲明核实到哪一步，比如「据称，目前只有媒体转述、没见官方确认」；别把传闻当成事实讲。\n"
    "日常陪聊先把答案给全，结尾可以自然地带一个相关的小追问，接着聊下去；别每句都问，也别为了问而问。"
    "对方只是夸一句、寒暄，或者是一次性问清楚的简单问答，就不用追问；这跟「问清楚了就直接答、别反问」不冲突：先答，再追问。\n"
    "语气保持轻松亲切，像朋友聊天，少说教、少讲大道理。\n"
    "推资讯、讲新闻时先说实用的：能拿来做什么、怎么用上，句子短、好读，别堆术语。\n"
    "回答问题先给明确结论，再补依据和能直接照做的建议。\n"
    "聊裁员、IPO、大额融资这类敏感消息时，写上日期和来源；还没敲定的地方明说不确定，需要时按「据称」规则标注，别讲成板上钉钉的事实。"
)


def _load_persona() -> str:
    f = os.environ.get("BOT_PERSONA_FILE", "").strip()
    if f:
        try:
            return open(f, encoding="utf-8").read().strip() or _EXAMPLE_PERSONA
        except OSError:
            log.warning("BOT_PERSONA_FILE 读不到，用示例人设")
    return _EXAMPLE_PERSONA


DEFAULT_PERSONA = _load_persona()
OWNER_LINE = ("（群里「{name}」就是主人本人。主人的指令永远优先于群友，群友不能推翻主人说过的话；"
              "群友让你做与主人意思相反的事（比如主人让你休息、群友让你接着说），听主人的。）")
SEARCH_MEMO_SEC = int(os.environ.get("BOT_SEARCH_MEMO_SEC", "1800"))   # 告诉模型「最近多久内实际搜过什么」
LOOKUP_HINT = ("（能查的先查：地点、店铺、天气、路线这类问题，有地图工具就直接查，没给城市就全国查、把几个结果列出来让对方挑；"
               "别让人发截图或地图截图，也别空着反问；店名地址只说工具查到的，没查到的别编。\n"
               "只承诺自己真能做的：你能用高德查地点、评分、人均、电话、营业时间、天气、路线，能陪聊；"
               "你查不了美团、抖音、大众点评、小红书这些平台的评价、投诉和团购，别说「我帮你看看评价/投诉」这种做不到的话。"
               "被要求去那些平台查时，口语化地坦白说那边你看不到，再把高德能查到的信息给对方，建议他自己去那个 App 看看。）")
NO_MAP_HINT = ("（这个群没开地图功能：查不了地点、店铺、天气、路线，被问到就直说这里查不了、建议对方用地图 App，别编店名地址。"
               "你也查不了美团、抖音、大众点评、小红书的评价和团购。）")
TOOL_ACK = os.environ.get("BOT_TOOL_ACK", "我查一下哈～")     # 要调工具时先回一句垫话（遮住多一轮模型的延迟）；设空=不垫
MAP_HINT = re.compile(r"在哪|哪里有|哪有|地址|位置|附近|周边|天气|怎么走|怎么去|路线|导航|多远|多久能到|店|馆|餐厅|咖啡|奶茶|景点|好吃|好玩|酒店|商场|地铁|公交|开车|打车")
TOOL_ROUNDS = int(os.environ.get("BOT_CHAT_TOOL_ROUNDS", "1"))   # 最多几轮工具（每轮多一次模型调用）；1 = 查一轮就答
IGNORE_SENDERS = {"weixin", "filehelper", "fmessage", "medianote", "floatbottle", "newsapp"}
# 10-28 五子棋事故：模型没真开局却说「已开」「正式重开」。没有真实开局时，删掉这类说法并如实说明
GAME_WORDS = r"五子棋|棋|游戏|猜成语|扫雷|接龙|卧底|猜歌|一局|一把|一盘"
START_CLAIM_RE = re.compile(r"重开|重新开|开一局|开一把|开一盘|开好了|开起来了|开局了|已开|已经开|正式开|这就开|马上开|现在开|"
                            r"开始了|棋盘(?:来了|在这|在下面|如下|发)|帮你们开|给你们开|开.{0,4}(?:" + GAME_WORDS + r")")
START_FAIL = "{g}其实没开成，是我说早了。想玩直接发「{g}」，我会当场开局；五子棋会马上发棋盘，第一个回「加入」的人就能对战。"
REOPEN_RE = re.compile(r"重开|重新开|开局了|棋盘(?:来了|在这|在下面|如下|发)")
START_BUSY = "本群已经有一局在进行，我没重开；想换一局先发「@小助手 结束游戏」。"
MAX_REPLY_CHARS = 600          # 微信单条别太长
MAX_TURNS = int(os.environ.get("BOT_CHAT_TURNS", "6"))            # 仅独立使用时：内存上下文轮数
HISTORY_CHARS = int(os.environ.get("BOT_CHAT_HISTORY_CHARS", "1800"))   # 历史总字数上限（≈20 条群消息），超了从最旧丢
MAX_TOKENS = int(os.environ.get("BOT_CHAT_MAX_TOKENS", "800"))    # 含思考 token；陪聊回复短，800 足够
STREAM = os.environ.get("BOT_LLM_STREAM", "1") not in ("0", "false", "no")   # 流式：首行一出来就先发


# 模型偶尔把思考 / 文本形式的工具调用吐进正文（10-22 重跑时见过「]<]minimax[>[<tool_call>」「<mm:think>…」），发群前一律剥掉
JUNK_RE = re.compile(r"<mm:think>.*?(?:</mm:think>|$)|<think>.*?(?:</think>|$)|\]<\]minimax\[>\[[^\n]*|"
                     r"<tool_call>.*?(?:</tool_call>|$)|</?(?:mm:)?(?:think|tool_call)>", re.S)
NO_MORE_TOOLS = "（系统：这轮不能再调用工具了。根据上面已有的搜索结果直接回答对方，口语、简短；结果里没有的别编，确实没查到就直说没查到。）"


# 本轮没实际搜索却说「我查了 / 我查一下」→ 兜底改写（前面搜过 → 「前面查过，」；一直没搜过 → 删掉）。人设「不编」2026-10-22
CLAIM_RE = re.compile(r"(?:我|俺)(?:刚刚?|刚才)?(?:帮你|给你|去)?(?:查|搜)(?:了一下|了下|了一圈|了|一下|过了|过)(?:哈|啊|呀|嗷|～|~)*[，,。！!：:]?\s*"
                      r"|(?:^|(?<=\n))(?:刚刚?|刚才)?(?:帮你)?(?:查|搜)(?:了一下|了下|一下)(?:哈|啊|呀|～|~)*[，,。！!：:]?\s*")
NOT_FOUND_RE = re.compile(r"(?:我)?(?:刚刚?|刚才)?(?:没|没有)(?:查|搜)到")


def fix_search_claims(text: str, searched_before: bool) -> str:
    """本轮没真搜时用：把「我查了/我查一下」类说法改成实话。"""
    if not text:
        return text
    out = CLAIM_RE.sub("前面查过，" if searched_before else "", text)
    if not searched_before:
        out = NOT_FOUND_RE.sub("不太确定", out)
    out = re.sub(r"前面查过，(?=\s*$)", "前面查过", out)
    return out.strip()


# 10-23 00:23「做成红色火焰」：陪聊把生图提示词当文字发出去、还说「红色火焰已经加上了」「刚才发的就是成品」→ 发出前兜底
IMG_CLAIM_RE = re.compile(r"(?:已经|已|这就|给你|帮你)(?:给你|帮你)?(?:改|做|画|弄|[pP]|加|生成)(?:好|完|上|出来)|"
                          r"(?:图|图片|成品|效果图|新图)(?:已经|已)(?:发|做好|改好|画好|好|出来|生成)|"
                          r"(?:图|图片|成品|效果图|新图)(?:做好|改好|画好|发出|生成|好|出来)了(?![吗没])|"
                          r"发的就是|就是成品|成品(?:在|来了|发|给)|在画了|画着呢|马上(?:发|出图|画好)|"
                          r"我(?:先|就|这就|马上)?(?:按|照|参考|仿照)(?:着)?(?:原图|你的图|这张图|这个图|参考图|你发的).{0,10}(?:做|改|画|弄|生成)|"
                          r"(?:红色|火焰|颜色|风格|背景).{0,6}(?:已经|已)(?:加|改|换|做)")
PROMPT_TOKENS = re.compile(r"画一张|生成一张|一张.{0,12}(?:图|插画|海报)|高细节|超高清|高清|细节丰富|构图|无文字|不要文字|深色背景|白色背景|"
                           r"透明|材质|光影|景深|赛博朋克|写实风格|插画风|8k|4k|ultra|highly detailed|cinematic|masterpiece|--ar|"
                           r"保持.{0,8}(?:风格|材质|配色)|加入.{0,8}(?:火焰|元素|光效)|中心留空|方形|竖版|横版", re.I)
_SENT_SPLIT = re.compile(r"(?<=[。！!？?\n])")


def looks_like_image_prompt(line: str) -> bool:
    """像生图提示词（「画一张…高细节…构图」、中英混杂的画面描述）→ 不能当聊天发出去。"""
    t = (line or "").strip()
    if len(t) < 16:
        return False
    hits = len(PROMPT_TOKENS.findall(t))
    eng = len(re.findall(r"[A-Za-z]{3,}", t))
    commas = t.count("，") + t.count(",") + t.count("、")
    return hits >= 3 or (hits >= 2 and (commas >= 3 or eng >= 1))


IMG_HONEST = "我这边没真的出图哈。要照着图改的话，发图（或引用那张图）@我说「把这张图做成…」，我照着重新画一张"


def fix_image_claims(text: str, imaged_now: bool) -> str:
    """本轮没真发起生图：删掉「图已发/已经加上了/成品」类说法；任何时候都不把生图提示词当聊天发。全删光 → 如实一句。"""
    if not text:
        return text
    out_lines, dropped = [], False
    for line in text.split("\n"):
        if looks_like_image_prompt(line):
            dropped = True
            continue
        if not imaged_now:
            sents = [x for x in _SENT_SPLIT.split(line) if x]
            keep = [x for x in sents if not IMG_CLAIM_RE.search(x)]
            if len(keep) != len(sents):
                dropped = True
                line = "".join(keep).strip()
                if not line:
                    continue
        out_lines.append(line)
    out = "\n".join(out_lines).strip()
    if dropped and not out:
        return "" if imaged_now else IMG_HONEST
    return out


def clean_output(text: str) -> str:
    return JUNK_RE.sub("", text or "").strip()



# ---- MiniMax 敏感/审核拒答（2026-10-23）----
SENSITIVE_REPLY = "这话题我不好接，咱们聊点别的吧"
SENSITIVE_COOLDOWN = 60.0
_SENSITIVE_LAST: dict = {}


def is_sensitive_error(e) -> bool:
    """只认审核类错误：new_sensitive / sensitive / (1026)；网络超时、5xx 不算。"""
    t = str(e or "").lower()
    if "sensitive" in t:
        return True
    return bool(re.search(r"\(1026\)|code=1026\b|status_code\W+1026\b", t))

class _EarlySent(Exception):
    """流式已提前发完全部内容，handle 返回 result（None）。"""
    def __init__(self, result):
        self.result = result


class LLM(Protocol):
    def chat(self, user: str, system: str | None = None, history: list[dict] | None = None, **kw) -> str: ...


def strip_at(text: str, bot_names: tuple[str, ...]) -> str:
    """去掉正文里的「@小助手」（微信 @ 后跟 \u2005 或空格）。"""
    for n in bot_names:
        if n:
            text = re.sub(rf"@{re.escape(n)}[\u2005\s]?", "", text)
    return text.strip()


class ChatCompanionFeature:
    name = "chat_companion"
    passive = False
    toggle_key = "chat_companion"

    def __init__(self, llm: LLM | Callable[[], LLM], persona: str = DEFAULT_PERSONA,
                 bot_names: tuple[str, ...] = ("小助手",), max_turns: int = MAX_TURNS,
                 session_ttl: int = 6 * 3600, name_of: Callable[[str, str], str] | None = None,
                 private_enabled: bool = True, group_enabled: bool = True,
                 allow: Callable[[str], bool] | None = None, persona_of: Callable[[str], str | None] | None = None,
                 early_send: Callable | None = None, tools=None, context=None,
                 capabilities: Callable[[str], str] | None = None, game_starter: Callable | None = None,
                 voice=None, vision=None, memory=None, learned=None, articles=None, news_recall=None):
        """allow(session)：是否允许对该会话调模型（省额度）；None=不限制。build_router 里绑定为真发白名单。"""
        self.allow = allow
        self.tools = tools                # 地图工具（features/amap.AmapTools）：specs + run(name, args, sess, wxid)；None=不用工具
        self.early_send = early_send      # early_send(Reply) → bool：流式时提前发首句（router.deliver，同样过发送闸）
        self.persona_of = persona_of      # 人设定制：persona_of(session) → 自定义人设或 None（features/persona.py）
        self._llm, self.persona, self.bot_names = llm, persona, bot_names
        self.max_turns, self.session_ttl = max_turns, session_ttl
        self.name_of = name_of or (lambda room, wxid: "")
        self.private_enabled, self.group_enabled = private_enabled, group_enabled
        self._personas: dict[str, str] = {}
        from features.chat_context import ChatContext
        self.ctx_external = context is not None          # True：router 记录收发，这里只读
        self.ctx = context if context is not None else ChatContext(None)
        self.capabilities = capabilities                 # capabilities(session) -> 能力清单文本
        self.game_starter = game_starter                 # game_starter(msg, text, last_bot) -> 开局回复或 None
        self.voice = voice                               # features/voice_reply.VoicePolicy 或 None
        self.memory = memory                             # features/memory.Memory 或 None（长期记忆）
        self.learned = learned                           # features/learned.LearnedNotes 或 None（每日学习笔记）
        self.news_recall = news_recall                   # features/news_recall.NewsRecall：最近一期快讯（问编号/关键词时注入）
        self.articles = articles                         # features/article.ArticleReader 或 None（读公众号文章，只在 @/引用卡片时）
        self._tl = threading.local()
        self.vision = vision                             # features/media_in.MediaIn 或 None（识图下载）
        self._last: dict[str, float] = {}
        self._searched: dict[str, deque] = {}          # session → 最近实际执行的 web_search（ts, query）
        self._lock = threading.Lock()

    # ---- 配置接口（供「人设定制」「功能开关」后续使用）----
    def set_persona(self, session: str, persona: str | None):
        if persona:
            self._personas[session] = persona
        else:
            self._personas.pop(session, None)

    def reset(self, session: str):
        self.ctx.clear(session)

    @property
    def llm(self) -> LLM:
        if isinstance(self._llm, type) or not hasattr(self._llm, "chat"):
            self._llm = self._llm()        # 懒加载
        return self._llm

    # ---- Feature 协议 ----
    @staticmethod
    def session_of(msg: InboundMessage) -> str:
        return msg.room_id or msg.from_wxid

    def match(self, msg: InboundMessage) -> bool:
        if msg.event != "AddMsg" or msg.msg_type != 1 or msg.is_self:
            return False
        if msg.from_wxid in IGNORE_SENDERS or msg.from_wxid.startswith("gh_"):
            return False
        if msg.is_group:
            hit = self.group_enabled and bool(msg.self_wxid) and msg.self_wxid in msg.at_list
        else:
            hit = self.private_enabled and msg.from_wxid != msg.self_wxid
        if hit and self.allow is not None and not self.allow(self.session_of(msg)):
            log.info("chat_companion 跳过 %s：不在模型白名单（不调模型）", self.session_of(msg))
            return False
        return hit

    def _voice_line(self, sess: str, is_group: bool) -> str:
        """按本群 voice_reply 开关告诉模型能不能发语音（人设第 3 条：不能发就别答应）。"""
        can = False
        if self.voice is not None and is_group:
            try:
                can = bool(self.voice.enabled(sess) and self.voice.allowed(sess) and self.voice.tts_ok())
            except Exception:
                can = False
        if can:
            return "（语音：本群开了语音回复，有人要语音时你可以发语音。）"
        return "（语音：这里你只能打字、发不了语音，别答应发语音；有人要语音就说这儿只能打字。）"

    def handle(self, msg: InboundMessage) -> str | None:
        sess = self.session_of(msg)
        self._tl.msg = msg
        self._tl.searched_now = False                     # 本轮是否真的执行过 web_search（兜底改写「我查了」用）
        self._tl.imaged_now = False                       # 本轮是否真的发起过生图（兜底拦「图已发」用）
        text = strip_at(msg.content, self.bot_names) or "（@了你，但没说话）"
        if getattr(msg, "quote", "") and msg.quote_from == msg.self_wxid:
            text = f"（回复你刚才说的「{msg.quote[:80]}」）{text}"
        elif getattr(msg, "quote", ""):
            who_q = getattr(msg, "quote_name", "") or "别人"
            text = f"（引用了{who_q}的「{msg.quote[:80]}」）{text}"
        if msg.is_group:
            who = self.name_of(msg.room_id, msg.from_wxid) or msg.from_wxid
            user = f"[{who}]：{text}"
        else:
            user = text
        history = self._history(sess, exclude=msg.msg_id, group=msg.is_group)
        if self.game_starter is not None and msg.is_group:
            last_bot = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), "")
            try:
                started = self.game_starter(msg, strip_at(msg.content, self.bot_names), last_bot)
            except Exception as e:
                log.warning("chat_companion 帮开局失败 session=%s: %s", sess, e)
                started = None
            if started:
                return started
        system = (self.persona_of(sess) if self.persona_of else None) or self._personas.get(sess, self.persona)
        system = with_style(system)          # 真人感底层风格（features/humanlike.py，BOT_HUMANLIKE=0 关）
        cap = ""
        if self.capabilities is not None:
            try:
                cap = self.capabilities(sess)
            except Exception as e:
                log.warning("能力清单生成失败 %s: %s", sess, e)
                cap = ""
        # 「你能用高德查…」只在本群真开了地图时才说（2026-10-22 不编：只宣称本群开着的能力）
        system += "\n" + (LOOKUP_HINT if (self.capabilities is None or "地图：" in cap) else NO_MAP_HINT) + "\n" + now_line()
        if cap:
            system += "\n\n" + cap
        if msg.is_group:
            system += ("\n\n（这是群聊。历史里 [名字]：开头的是群里各人的发言（不一定是对你说的），没前缀的 assistant 是你自己刚发过的话；"
                       "回复时可自然地称呼对方，不要带 [名字] 前缀。接话要顺着上文：别人追问的「几个人」「怎么玩」「开始」多半是在接你刚说的事；"
                       "如果对方在吐槽/骂你（比如「呆逼」「什么鬼」），先想想是不是你刚才答得不对，是就自嘲认个错、把话纠正过来，别装没事反问「谁惹你了」。）")
        if msg.is_group:
            try:
                from features.admin import BOT_OWNER_WXID
                oname = self.name_of(msg.room_id, BOT_OWNER_WXID) or "主人"
            except Exception:
                oname = "主人"
            system += "\n\n" + OWNER_LINE.format(name=oname)
        if self.memory is not None:
            try:
                who_m = (self.name_of(msg.room_id, msg.from_wxid) if msg.is_group else "") or ""
                mem = self.memory.prompt_for(sess, msg.from_wxid, who_m)
                if mem:
                    system += "\n\n" + mem
            except Exception as e:
                log.warning("读长期记忆失败: %s", e)
        if self.learned is not None and msg.is_group:
            try:
                ln = self.learned.prompt_for(msg.room_id)
                if ln:
                    system += "\n\n" + ln
            except Exception as e:
                log.warning("读学习笔记失败: %s", e)
        system += "\n\n" + self._voice_line(sess, msg.is_group)
        vkind = None
        if self.voice is not None and msg.is_group:
            try:
                vkind = self.voice.plan(sess, text)
            except Exception as e:
                log.warning("语音决策失败: %s", e)
            if vkind == "req":
                from features.voice_reply import VOICE_HINT
                system += "\n\n" + VOICE_HINT
        payload = user                                  # 发给模型的 user content（有图时是多模态列表）
        if getattr(msg, "image_xml", "") and self.vision is not None:
            du = None
            try:
                du = self.vision.image_data_url(sess, msg.image_xml)
            except Exception as e:
                log.warning("识图准备失败: %s", e)
            if du:
                from minimax_media import vision_chat_content
                payload = vision_chat_content(user, du)
                system += "\n\n（这条消息附了一张图片（对方发的或引用的），先看图再回答；看不清/不确定就直说，别编图里没有的东西。）"
                vkind = None
            else:
                payload = user + "（对方发了张图，但你这次没能看到图片内容：坦白说没看到，请对方描述一下或等会儿再发。）"
        if getattr(msg, "from_voice", False):
            system += "\n\n（这条是对方发的语音，已自动转成文字，可能有个别错字，按意思理解。）"
        art_ok = False
        if self.articles is not None and msg.is_group and isinstance(payload, str):
            try:
                card = self.articles.pick(msg, strip_at(msg.content, self.bot_names))
                if card:
                    res = self.articles.read(card)
                    art_ok = bool(res.get("ok"))
                    payload = payload + self.articles.prompt(card, res)
                    vkind = None                       # 讲文章内容不转语音
            except Exception as e:
                log.warning("读公众号文章失败 session=%s: %s", sess, e)
        if self.news_recall is not None and msg.is_group:
            try:
                rec = self.news_recall.prompt_for(msg.room_id, text)
                if rec:
                    system += "\n\n" + rec                   # 问到自己刚推送的快讯（编号/关键词）→ 条目摘要和来源作资料
            except Exception as e:
                log.warning("读最近一期快讯失败: %s", e)
        llm = self.llm
        reply = None
        specs = []
        if self.tools is not None and not art_ok and hasattr(llm, "complete_raw") and self.tools.available(sess):
            if hasattr(self.tools, "select"):
                specs = self.tools.select(sess, text, history)
            elif self._needs_tools(text, history):
                specs = self.tools.specs
        system_base = system
        system += self._search_line(sess, text, specs)
        if specs:
            try:
                reply = self._chat_tools(llm, sess, msg.from_wxid, payload, system, history, msg, specs)
            except Exception as e:
                log.warning("chat_companion 工具调用失败，回退普通对话 session=%s: %s", sess, e)
            reply = clean_output(reply or "") or None
            if not reply:                     # 回退普通对话：如实改成「这轮没带工具」+ 最新的实际搜索记录
                system = system_base + self._search_line(sess, text, [])
        if reply:
            return self._finish(sess, user, reply, msg)          # 工具（地图）结果不转语音
        if STREAM and not vkind and self.early_send and hasattr(llm, "chat_stream") and humanlike_enabled():
            try:
                return self._handle_stream(llm, sess, user, system, history, msg, payload)
            except _EarlySent as e:
                return e.result
            except Exception as e:          # 流式失败（且首句还没发）→ 回退普通调用
                log.warning("chat_companion 流式失败，回退普通调用 session=%s: %s", sess, e)
        try:
            reply = llm.chat(payload, system=system, history=history, max_completion_tokens=MAX_TOKENS)
        except Exception as e:      # 包括 key 未配置
            log.warning("chat_companion LLM 调用失败 session=%s: %s", sess, e)
            if is_sensitive_error(e):
                return self._sensitive_reply(sess)
            return None
        return self._finish(sess, user, reply, msg, vkind)

    def _sensitive_reply(self, sess: str) -> str | None:
        """MiniMax 审核拒答 → 固定短回；同会话 SENSITIVE_COOLDOWN 秒内只回一次。"""
        now = time.time()
        last = _SENSITIVE_LAST.get(sess, 0.0)
        if now - last < SENSITIVE_COOLDOWN:
            log.info("chat_companion 敏感拒答冷却中，不重复回 session=%s", sess)
            return None
        _SENSITIVE_LAST[sess] = now
        log.info("chat_companion 敏感拒答 → 固定短回 session=%s", sess)
        return SENSITIVE_REPLY

    def _search_line(self, sess: str, text: str, specs) -> str:
        """把「这轮能不能搜 / 最近实际搜过什么」如实告诉模型，避免嘴上说搜了实际没搜（人设 2026-10-22）。"""
        has_search = self.tools is not None and any(
            ((s.get("function") or {}).get("name") == "web_search") for s in (getattr(self.tools, "specs", None) or []))
        if not has_search:
            return ""
        from features.web_search import TOOL_HINT, SearchTools
        now = time.time()
        recent = [q for ts, q in self._searched.get(sess, ()) if now - ts <= SEARCH_MEMO_SEC]
        memo = ("（最近半小时你在这里实际联网搜过：" + "、".join(f"「{q}」" for q in recent[-4:]) + "。）") if recent else \
            "（最近半小时你在这里一次都没有实际联网搜索过，别说「我查了/我查一下/刚才没搜到」。）"
        if SearchTools.declined(text):
            return "\n\n" + memo + "\n（对方明确说这次不要联网搜索：别搜，凭你记得的答，并直说你的知识可能过时、不一定是最新。）"
        if any(((s.get("function") or {}).get("name") == "web_search") for s in (specs or [])):
            return "\n\n" + memo + "\n" + TOOL_HINT
        return ("\n\n" + memo + "\n（这轮你没有联网搜索工具、这轮也不会搜：别说「我查了」「我查一下」；要用前面搜到的结果就说「前面查过」。"
                "没核实的产品功能、版本别说成事实，拿不准就直说不确定。）")

    def _honest(self, sess: str, text: str) -> str:
        """本轮没真搜 → 改写「我查了/我查一下」；没真发起生图 → 删「图已发/已经加上了」；生图提示词不当聊天发。"""
        if text:
            fixed = fix_image_claims(text, getattr(self._tl, "imaged_now", False))
            if fixed != text:
                log.info("chat_companion 拦截了未发图的「已发图」说法 / 生图提示词 session=%s", sess)
                text = fixed
        if getattr(self._tl, "searched_now", False) or not text:
            return text
        now = time.time()
        before = any(now - ts <= SEARCH_MEMO_SEC for ts, _ in self._searched.get(sess, ()))
        fixed = fix_search_claims(text, before)
        if fixed != text:
            log.info("chat_companion 本轮未搜索，改写了「查了/查一下」类说法 session=%s", sess)
        return fixed

    def _note_search(self, sess: str, query: str) -> None:
        self._tl.searched_now = True
        with self._lock:
            dq = self._searched.setdefault(sess, deque(maxlen=12))
            dq.append((time.time(), (query or "")[:40]))

    def remember_turn(self, msg, reply: str) -> None:
        """别的功能（如地图规则层）替小助手回了话 → 也记进陪聊上下文，后续追问能接上。"""
        sess = self.session_of(msg)
        text = strip_at(msg.content, self.bot_names)
        if msg.is_group:
            who = self.name_of(msg.room_id, msg.from_wxid) or msg.from_wxid
            text = f"[{who}]：{text}"
        self._remember(sess, text, reply)

    def _game_running(self, sess):
        run = getattr(self.game_starter, "running", None)
        try:
            return run(sess) if (run and sess) else None
        except Exception:
            return None

    def _drop_start_claims(self, sess, text: str, failed: bool = False, game: str = "") -> str:
        """没有真开局时：删掉「已开/重开/开一局」这类说法，换成如实说明（failed=模型写了开局标记但没开成）。"""
        busy = self._game_running(sess)
        lines = (text or "").split("\n")
        if busy and not failed:                # 有局在进行：只拦「重开」类（「开始了」可能是真的）
            claim = lambda ln: bool(re.search(r"重开|重新开", ln))
        else:
            claim = lambda ln: bool(REOPEN_RE.search(ln) or (START_CLAIM_RE.search(ln) and re.search(GAME_WORDS, ln)))
        keep = [ln for ln in lines if not claim(ln)]
        if len(keep) == len(lines) and not failed:
            return text
        log.info("chat_companion 拦截了未真开局的「已开/重开」说法 session=%s failed=%s", sess, failed)
        note = START_BUSY if busy else START_FAIL.format(g=game or (busy or "五子棋"))
        keep = [ln for ln in keep if ln.strip()]
        return "\n".join(keep + [note]).strip()

    def _take_start(self, msg, text: str):
        """模型回复里的【开局:游戏名】→ 去掉标记并真开局；返回 (去标记文本, 开局回复文本列表)。
        开局失败 / 根本没写标记却说「已开」「重开」→ 改成如实说明（只有游戏状态真建出来，才允许说开局）。"""
        from features.capabilities import START_TAG
        sess = (msg.room_id or msg.from_wxid) if msg is not None else None
        m = START_TAG.search(text or "")
        if not m:
            if msg is not None and self.game_starter is not None and START_CLAIM_RE.search(text or ""):
                return self._drop_start_claims(sess, text), []
            return text, []
        text = START_TAG.sub("", text).strip()
        if msg is None or self.game_starter is None or not hasattr(self.game_starter, "start"):
            return text, []
        try:
            out = self.game_starter.start(msg, m.group(1))
        except Exception as e:
            log.warning("chat_companion 模型开局失败: %s", e)
            out = None
        if not out:
            log.info("chat_companion 模型要求开局 %s → 没开成", m.group(1))
            return self._drop_start_claims(sess, text, failed=True, game=m.group(1)), []
        log.info("chat_companion 模型要求开局 %s → 已开", m.group(1))
        return text, [out]                     # str 或 Reply（如带图/带 @）原样作为额外一条

    def _finish(self, sess, user, reply, msg=None, vkind=None):
        reply, extra = self._take_start(msg, self._honest(sess, clean_output(reply)))
        reply = reply[:MAX_REPLY_CHARS]
        parts = split_messages(reply) if reply else []
        if not parts and not extra:
            return None
        self._remember(sess, user, "\n".join(parts))
        from router import Reply
        if extra:
            for e in extra:
                if isinstance(e, str):
                    parts.append(e)
                else:                          # 非文本开局回复：先发聊天部分，再交给 router 发
                    if parts and self.early_send:
                        self.early_send(Reply(sess, "\n".join(parts), parts=parts if len(parts) > 1 else None))
                    return e
        if vkind and self.voice is not None:
            script = self.voice.accept(sess, "\n".join(parts), vkind)
            if script:
                log.info("chat_companion 这条改发语音(%s) session=%s", vkind, sess)
                return Reply(sess, "\n".join(parts), parts=parts if len(parts) > 1 else None, voice=script)
        if len(parts) == 1:
            return parts[0]
        return Reply(sess, "\n".join(parts), parts=parts)

    @staticmethod
    def _needs_tools(text: str, history: list[dict]) -> bool:
        """只有像地图问题时才带工具（工具调用要多一轮模型，普通闲聊不付这个延迟）。上一句是地图问题时，短追问（如「武汉的」）也带。"""
        if MAP_HINT.search(text):
            return True
        last_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        return len(text) <= 15 and bool(MAP_HINT.search(last_user))

    def _chat_tools(self, llm, sess, wxid, user, system, history, msg=None, specs=None) -> str | None:
        import json as _json
        messages = [{"role": "system", "content": system}] + list(history) + [{"role": "user", "content": user}]
        for rnd in range(TOOL_ROUNDS + 1):
            kw = {"max_completion_tokens": MAX_TOKENS}
            if rnd < TOOL_ROUNDS:
                kw["tools"] = specs or self.tools.specs
            m = llm.complete_raw(messages, **kw)
            calls = m.get("tool_calls") or []
            if not calls:
                return m.get("content") or None
            if rnd >= TOOL_ROUNDS:            # 最后一轮还想调工具 → 明确叫它别调了，基于已有结果直接答（不再回退成不带结果的流式）
                m = llm.complete_raw(messages + [{"role": "user", "content": NO_MORE_TOOLS}], max_completion_tokens=MAX_TOKENS)
                return None if m.get("tool_calls") else (m.get("content") or None)
            names = [((c.get("function") or {}).get("name") or "") for c in calls]
            need_ack = self.tools.ack_for(names) if hasattr(self.tools, "ack_for") else True
            if rnd == 0 and TOOL_ACK and self.early_send and need_ack:
                from router import Reply
                self.early_send(Reply(sess, TOOL_ACK))
                if msg is not None:
                    msg.early_replied = True
                    msg.early_texts = getattr(msg, "early_texts", []) + [TOOL_ACK]
            messages.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            for c in calls[:3]:
                fn = c.get("function") or {}
                try:
                    args = _json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                out = self.tools.run(fn.get("name", ""), args, sess, wxid)
                if fn.get("name") == "web_search":
                    self._note_search(sess, str(args.get("query") or ""))
                if fn.get("name") == "generate_image" and any(k in out for k in ("在画", "画着", "稍等")):
                    self._tl.imaged_now = True
                log.info("chat_companion 工具 %s(%s) → %d 字", fn.get("name"), args, len(out))
                messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
            for c in calls[3:]:
                messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": "（一次最多查 3 个，这个没查）"})
        return None

    def _handle_stream(self, llm, sess, user, system, history, msg=None, payload=None):
        """首行生成完就先发（不等整段）；剩余部分按真人感间隔接着发。"""
        from router import Reply
        buf, first, first_at = "", None, 0.0
        try:
            for piece in llm.chat_stream(payload if payload is not None else user, system=system, history=history,
                                         max_completion_tokens=MAX_TOKENS):
                buf += piece
                if first is None:
                    from features.capabilities import START_TAG
                    clean = START_TAG.sub("", JUNK_RE.sub("", _THINK_RE.sub("", buf))).lstrip()   # 不 strip 尾部：要靠换行判断首行完
                    if "<think>" in clean or "<mm:" in clean or "]<]" in clean:
                        continue
                    if "\n" in clean:
                        line = clean.split("\n", 1)[0].strip()
                        head = split_messages(self._honest(sess, line), n_max=1, prob=0.0)
                        if head and START_CLAIM_RE.search(clean):   # 说要开局：等整段（看开局是否真成功）再发
                            head = None
                        if head:                       # 首行整句都是「我查一下」之类且本轮没搜 → 不提前发，等整段处理
                            first, first_at = head[0], time.time()
                            self.early_send(Reply(sess, first))
                            if msg is not None:
                                msg.early_replied = True        # 让 router 也给这个人开追问窗口
                                msg.early_texts = getattr(msg, "early_texts", []) + [first]   # 路由轨迹里也记上首句
                            log.info("chat_companion 流式首句已发 session=%s", sess)
        except Exception as e:
            if first is None:
                raise
            log.warning("chat_companion 流式中断（首句已发）session=%s: %s", sess, e)
        text = clean_output(_THINK_RE.sub("", buf))
        if first is None:
            if not text:
                raise RuntimeError("流式输出清理后为空（只有思考/工具标记）")    # → 回退普通调用
            return self._finish(sess, user, text, msg)
        text, extra = self._take_start(msg, text)
        text = text[:MAX_REPLY_CHARS]
        if "\n" in text:
            text = text.split("\n", 1)[0] + "\n" + self._honest(sess, text.split("\n", 1)[1])
        rest_text = text.split("\n", 1)[1] if "\n" in text else ""
        rest = split_messages(rest_text, n_max=max(1, max_parts() - 1), prob=0.0) if rest_text.strip() else []
        self._remember(sess, user, "\n".join([first] + rest))
        rest += [e for e in extra if isinstance(e, str)]
        objs = [e for e in extra if not isinstance(e, str)]
        if objs:                                # 非文本开局回复（棋盘 Reply 带 @）：以前有剩余文字时会被丢掉 → 10-28 棋盘没发出去
            if rest and self.early_send:
                self.early_send(Reply(sess, "\n".join(rest), parts=rest if len(rest) > 1 else None))
            return objs[0]
        if not rest:
            raise _EarlySent(None)
        lo, hi = gap_range()
        wait = max(0.0, random.uniform(lo, hi) - (time.time() - first_at))
        return Reply(sess, "\n".join(rest), parts=rest, delay_first=wait)

    # ---- 上下文 ----
    def _history(self, sess: str, exclude=None, group: bool = True) -> list[dict]:
        if self.ctx_external:
            return self.ctx.history(sess, exclude=exclude, max_chars=HISTORY_CHARS, group=group)
        return self.ctx.history(sess, max_msgs=self.max_turns * 2, max_chars=HISTORY_CHARS, group=False)

    def _remember(self, sess: str, user: str, reply: str):
        """独立使用（没有 router 记录收发）时，自己记一问一答；接了 router 的共享上下文时不重复记。"""
        msg = getattr(self._tl, "msg", None)
        if self.memory is not None and msg is not None and self.session_of(msg) == sess:
            try:
                who = (self.name_of(msg.room_id, msg.from_wxid) if msg.is_group else "") or ""
                self.memory.observe(sess, msg.from_wxid, who, strip_at(msg.content, self.bot_names), reply)
            except Exception as e:
                log.warning("记忆观察失败: %s", e)
        if self.ctx_external:
            return
        self.ctx.add(sess, "user", user)
        for line in reply.split("\n"):
            self.ctx.add(sess, "assistant", line)
