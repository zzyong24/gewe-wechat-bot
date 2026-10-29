"""10-22 复盘（示例群B Muse/DeepSeek 那段）四点修复：该搜就搜 / 不编（如实告诉模型搜没搜过）/ 拆条保留换行 / 夸一句短回（人设）。
全 mock，不联网、不发消息。"""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import build_router
from features.chat_companion import DEFAULT_PERSONA
from features.humanlike import split_messages
from features.web_search import SearchTools
from models import normalize
from tests.helpers import MEMBERS, ROOM, BOT, raw, all_on

# ---- 1. 触发面：陌生产品名 / 版本 / 对比 / 打错字都带搜索；夸一句、闲聊、明确不联网不带 ----
W = SearchTools.wants
for t in ["muns的优势和闪光点是什么，相比于其他agent 客户端有什么优势，特别适合哪些行业或赛道", "打错了，我说的是mues",
          "muse不都不知道吗？那你知道codex ，dsh 是什么吗", "deepseek harnese", "这两个和muse比呢",
          "DeepSeek 现在是哪个版本", "最近AI有什么新闻", "iPhone 18 多少钱"]:
    assert W(t), t
for t in ["牛逼兄弟", "哈哈哈你好可爱", "讲个笑话", "ok", "hhh 笑死", "今天吃啥",
          "不使用联网搜索功能，告诉我deepseek大模型现在是哪个版本", "别搜了，直接说 GPT 是啥"]:
    assert not W(t), t
assert SearchTools.declined("不使用联网搜索功能，告诉我deepseek") and not SearchTools.declined("帮我搜一下 Manus")
print("搜索触发面 ✅")

# ---- 3. 拆条：超过上限时尾巴用换行合并，不粘连 ----
p = split_messages("我按同一套维度对比：\nCodex：偏专业编程\nHarness：偏可扩展的 Agent 执行框架\nMuse：看任务执行", n_max=2, prob=0)
assert p == ["我按同一套维度对比：", "Codex：偏专业编程\nHarness：偏可扩展的 Agent 执行框架\nMuse：看任务执行"], p
assert "编程Harness" not in "".join(p)
assert split_messages("a\nb\nc\nd", n_max=3, prob=0) == ["a", "b", "c\nd"]
assert split_messages("1. 你好。\n- 再见", prob=0) == ["你好", "再见"]
print("拆条保留换行 ✅")

# ---- 2/4. 人设 ----
for k in ("真的搜过", "前面查过", "最多问一次", "muns", "牛逼兄弟", "别说教", "可能过时", "别报具体日期"):
    assert k in DEFAULT_PERSONA, k
print("人设新增三条 ✅")


# ---- 端到端：带搜索工具 → 调用 → system 里如实记「最近搜过」；没带工具时 system 明说没搜过 ----
calls = []
def fake_search(q):
    calls.append(q)
    return [{"title": "Manus 官网", "snippet": "通用 AI Agent", "date": "2026-10-01", "link": "https://manus.im", "site": "manus.im"}]


class ToolLLM:
    def __init__(self):
        self.seen = []
    def chat(self, user, system=None, history=None, **kw):
        self.seen.append({"tools": [], "system": system, "user": user}); return "过奖了哈"
    def complete_raw(self, messages, **kw):
        tools = [s["function"]["name"] for s in kw.get("tools", [])]
        self.seen.append({"tools": tools, "system": messages[0]["content"], "user": messages[-1]["content"]})
        if "web_search" in tools:
            return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                   "function": {"name": "web_search", "arguments": json.dumps({"query": "Manus AI agent"})}}]}
        return {"content": "你说的应该是 Manus\n它主打自己干活的通用 Agent（来源：manus.im）"}


d = Path(tempfile.mkdtemp()); sent = []
llm = ToolLLM()
r = build_router(fetch_list=lambda x: MEMBERS, fetch_info=lambda x: {}, sender=lambda m: sent.append(m), send_enabled=True,
                 db_path=d / "s.db", llm=llm, settings=all_on(), allow_targets={ROOM}, amap_tools=False,
                 search_fn=fake_search, image_async=False)
r.multi_async = False; r.sleep = lambda s: None
# 先夸一句：不带工具，system 告诉它最近没搜过
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005牛逼兄弟", [BOT])))
s0 = llm.seen[-1]["system"]
assert llm.seen[-1]["tools"] == [] and "一次都没有实际联网搜索过" in s0 and "没有联网搜索工具" in s0, s0[-300:]
# 陌生产品名：带工具、真搜、system 里有先搜再答提示
tr = r.dispatch(normalize(raw("wxid_a", "@小助手\u2005muns的优势是什么", [BOT])))
first = llm.seen[-2]
assert "web_search" in first["tools"] and "先猜最可能的名字去搜" in first["system"], first["tools"]
assert calls == ["Manus AI agent"]
texts = [m.content for m in sent]
assert "我查一下哈～" in texts and any("manus.im" in t for t in texts), texts
item = [x for x in tr if x.get("feature") == "chat_companion"][0]
assert item.get("early") == ["我查一下哈～"], item
# 下一句不带工具：system 如实记「最近搜过 Manus AI agent」
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005牛逼兄弟", [BOT])))
assert "实际联网搜过：「Manus AI agent」" in llm.seen[-1]["system"]
# 明确不联网：不带工具，提示说明可能过时
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005不使用联网搜索功能，告诉我deepseek大模型现在是哪个版本", [BOT])))
assert llm.seen[-1]["tools"] == [] and "知识可能过时" in llm.seen[-1]["system"]
print("端到端：真搜 + 如实告知搜没搜过 + 轨迹记垫话 + 不联网 ✅")

# ---- 最后一轮还想调工具 → 追加「别再调工具」直接基于结果答；模型吐出的思考/工具标记不进群 ----
from features.chat_companion import clean_output
assert clean_output("]<]minimax[>[<tool_call>\n<mm:think>Need no actual tool") == ""
assert clean_output("你好<think>x</think>\n世界") == "你好\n世界"


class Greedy(ToolLLM):
    def complete_raw(self, messages, **kw):
        tools = [s["function"]["name"] for s in kw.get("tools", [])]
        self.seen.append({"tools": tools, "system": messages[0]["content"], "user": messages[-1]["content"]})
        if "不能再调用工具" in messages[-1]["content"]:
            return {"content": "<mm:think>嗯</mm:think>查到了：Mues 是个小众工具"}
        return {"content": "", "tool_calls": [{"id": "c%d" % len(self.seen), "type": "function",
                                               "function": {"name": "web_search", "arguments": json.dumps({"query": "mues"})}}]}


class Junky(ToolLLM):
    def chat_stream(self, user, system=None, history=None, **kw):
        yield from ["]<]minimax[>[<tool_call>\n", "<mm:think>Need no actual tool"]
    def chat(self, user, system=None, history=None, **kw):
        self.seen.append({"tools": [], "system": system, "user": user}); return "哈哈客气了"


for L, q, want in ((Greedy(), "打错了，我说的是mues", "查到了：Mues 是个小众工具"), (Junky(), "牛逼兄弟", "哈哈客气了")):
    sent.clear()
    r = build_router(fetch_list=lambda x: MEMBERS, fetch_info=lambda x: {}, sender=lambda m: sent.append(m), send_enabled=True,
                     db_path=Path(tempfile.mkdtemp()) / "s.db", llm=L, settings=all_on(), allow_targets={ROOM}, amap_tools=False,
                     search_fn=fake_search, image_async=False)
    r.multi_async = False; r.sleep = lambda s: None
    r.dispatch(normalize(raw("wxid_a", "@小助手\u2005" + q, [BOT])))
    texts = [m.content for m in sent]
    assert texts and texts[-1] == want, texts
    assert not any(("<" in t and "think" in t) or "minimax" in t or "tool_call" in t for t in texts), texts
print("最后一轮不再调工具 / 思考与工具标记不进群 ✅")

# ---- 1145：本轮没搜却说「我查了一下」→ 兜底改写；前面搜过 → 「前面查过」；预告只在真调搜索时发 ----
from features.chat_companion import fix_search_claims as F
assert F("我查了一下哈，简单说", True) == "前面查过，简单说"
assert F("我查了一下哈，简单说", False) == "简单说"
assert F("我帮你查一下哈～", False) == ""
assert F("刚才没搜到就直说了", False) == "不太确定就直说了"
keep = "现在有联网搜索，刚才查 Codex、DSH、Muse 都是实时搜的"
assert F(keep, True) == keep and F("我能联网搜，能查天气", False) == "我能联网搜，能查天气"


class Claimy(ToolLLM):
    """不调工具，流式回复里却说「我查了一下」。"""
    def complete_raw(self, messages, **kw):
        self.seen.append({"tools": [s["function"]["name"] for s in kw.get("tools", [])], "system": messages[0]["content"]})
        return {"content": "我查了一下哈，简单说\nMuse 偏个人助理"}
    def chat_stream(self, user, system=None, history=None, **kw):
        yield from ["我查了一下哈，简单说\n", "Muse 偏个人助理\n我查了，Codex 偏编程"]


def mk2(llm):
    sent2 = []
    r = build_router(fetch_list=lambda x: MEMBERS, fetch_info=lambda x: {}, sender=lambda m: sent2.append(m), send_enabled=True,
                     db_path=Path(tempfile.mkdtemp()) / "s.db", llm=llm, settings=all_on(), allow_targets={ROOM}, amap_tools=False,
                     search_fn=fake_search, image_async=False)
    r.multi_async = False; r.sleep = lambda s: None
    return r, sent2


# 一直没搜过：流式首句、后续条都删掉「我查了」，不发「我查一下哈～」
r, sent2 = mk2(Claimy())
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005哈哈你说说看", [BOT])))       # 不触发搜索工具 → 走流式
texts = [m.content for m in sent2]
assert texts == ["简单说", "Muse 偏个人助理", "Codex 偏编程"], texts
# 带搜索工具但模型没调用（非流式路径）：同样改写；前面搜过 → 「前面查过」
r.chat._note_search(ROOM, "Muse AI"); r.chat._tl.searched_now = False
sent2.clear()
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005这两个和muse比呢", [BOT])))
texts = [m.content for m in sent2]
assert "我查一下哈～" not in texts and texts[0] == "前面查过，简单说", texts
assert "前面查过" in r.chat.llm.seen[-1]["system"] or "前面查过" in DEFAULT_PERSONA
# 本轮真搜了：「我查了」保留
class Real(ToolLLM):
    def complete_raw(self, messages, **kw):
        if kw.get("tools"):
            return {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": json.dumps({"query": "muse"})}}]}
        return {"content": "我查了一下，Muse 是 Meta 的个人助理"}
r, sent2 = mk2(Real())
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005muse 是什么", [BOT])))
assert [m.content for m in sent2] == ["我查一下哈～", "我查了一下，Muse 是 Meta 的个人助理"], [m.content for m in sent2]
print("没搜不许说「我查了」/ 前面搜过说「前面查过」/ 预告只在真搜时发 ✅")

# ---- 能力清单：只列真实存在且本群开着的；没有的（读文档等）明确说做不了；人设补一句 ----
from features.capabilities import describe
ON = {"web_search", "vision", "asr", "image_gen", "voice_reply", "amap", "memory", "ai_news"}
full = describe(lambda s, k: k in ON, "g")
for k in ("联网搜索：", "看图：", "听语音：", "画图：", "语音：", "地图：", "长期记忆：", "整点"):
    assert k in full, k
assert "08:30" not in full and "本群没开" not in full
assert "读文档" in full and "打开普通网页链接读全文" in full and "别说「发给我我能看」" in full
bare = describe(lambda s, k: False, "g")
for k in ("联网搜索：", "看图：", "听语音：", "画图：", "语音：你能", "地图：", "长期记忆：", "AI 快讯："):
    assert k not in bare, k
assert "本群没开" in bare and "联网搜索" in bare.split("本群没开")[1] and "读文档" in bare
assert "能力清单里真有" in DEFAULT_PERSONA and "读文档" in DEFAULT_PERSONA
# 「你能用高德查…」只在本群开了地图时出现
from features.chat_companion import LOOKUP_HINT, NO_MAP_HINT
st2 = all_on(); st2.set(ROOM, "amap", False)
r, _ = mk2(Claimy()); r.chat._llm = ToolLLM()
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005在吗", [BOT])))
assert LOOKUP_HINT in r.chat.llm.seen[-1]["system"]
L2 = ToolLLM()
r = build_router(fetch_list=lambda x: MEMBERS, fetch_info=lambda x: {}, sender=lambda m: None, send_enabled=True,
                 db_path=Path(tempfile.mkdtemp()) / "s.db", llm=L2, settings=st2, allow_targets={ROOM}, amap_tools=False,
                 search_fn=fake_search, image_async=False)
r.multi_async = False; r.sleep = lambda s: None
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005在吗", [BOT])))
assert NO_MAP_HINT in L2.seen[-1]["system"] and LOOKUP_HINT not in L2.seen[-1]["system"]
print("能力清单只列真实且本群开着的 / 读文档等明确做不了 / 地图提示按开关 ✅")
print("test_search_honesty 全部通过")
