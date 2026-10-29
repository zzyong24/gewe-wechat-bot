"""联网搜索工具 + 生图（口令/工具/婉拒/上限/异步发图/失败退额度）。全 mock，不联网不发消息。"""
import sys, tempfile, time, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import build_router
from features.settings import FeatureSettings
from features.web_search import SearchTools
from features.image_gen import parse_command, refuse_reason
from features.daily_quota import DailyCounter
from tests.helpers import MEMBERS, ROOM, BOT, raw, all_on
from models import normalize

# ---- 口令解析 / 婉拒规则 ----
assert parse_command("画一张橘猫戴耳机看月亮") == "橘猫戴耳机看月亮"
assert parse_command("帮我画个赛博朋克风的武汉夜景") == "赛博朋克风的武汉夜景"
assert parse_command("生成图片：雪山下的小木屋") == "雪山下的小木屋"
assert parse_command("来张星空的图") == "星空"
assert parse_command("画得好") is None and parse_command("今天天气不错") is None
assert refuse_reason("性感比基尼美女") == "sex" and refuse_reason("血腥的战场尸体") == "violence"
assert refuse_reason("特朗普骑熊") == "politics" and refuse_reason("周杰伦弹钢琴") == "person"
assert refuse_reason("阿离在跳舞", ["阿离", "Bob"]) == "person"          # 群友昵称 = 真人
assert refuse_reason("一只柯基在海边", ["阿离"]) is None
print("口令解析 / 婉拒规则 ✅")

# ---- 搜索工具：开关、何时带、结果格式、日上限 ----
calls = []
def fake_search(q):
    calls.append(q)
    return [{"title": "某某发布新模型", "snippet": "今天上午发布……", "date": "2026-10-21", "link": "https://news.example.com/a", "site": "news.example.com"}]
st = FeatureSettings(None); st.set("g", "web_search", True)
c = DailyCounter(None)
t = SearchTools(lambda s: st.enabled(s, "web_search"), lambda s: s == "g", lambda s: False, c, search_fn=fake_search, configured=lambda: True)
assert t.available("g") and not t.available("h")
assert t.wants("最近AI有什么新闻") and t.wants("iPhone 18 多少钱") and t.wants("这事怎么回事")
assert not t.wants("哈哈哈你好可爱") and not t.wants("讲个笑话")
out = t.run("web_search", {"query": "AI 新闻"}, "g", "w")
assert "某某发布新模型" in out and "最多带 1 个来源" in out and calls == ["AI 新闻"]
t.daily_room = 1
assert "次数用完" in t.run("web_search", {"query": "再查"}, "g", "w") and len(calls) == 1
print("搜索工具：开关 / 触发词 / 格式 / 日上限 ✅")


# ---- 端到端（陪聊工具调用）：新闻问题 → 先垫「我查一下」→ 带来源的简短回答 ----
class ToolLLM:
    def __init__(self, first_call, final):
        self.first_call, self.final, self.seen = first_call, final, []
    def chat(self, user, system=None, history=None, **kw):
        return "普通回复"
    def complete_raw(self, messages, **kw):
        self.seen.append({"tools": [s["function"]["name"] for s in kw.get("tools", [])], "messages": messages})
        if kw.get("tools") and self.first_call:
            return {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": self.first_call}]}
        return {"content": self.final}

def mk(llm, img_fn=None, st=None):
    d = Path(tempfile.mkdtemp()); sent = []
    r = build_router(fetch_list=lambda x: MEMBERS, fetch_info=lambda x: {}, sender=lambda m: sent.append(m), send_enabled=True,
                     db_path=d / "s.db", llm=llm, settings=st or all_on(), allow_targets={ROOM}, amap_tools=False,
                     search_fn=fake_search, image_fn=img_fn or (lambda p, out: _fake_img(out)), image_async=False)
    r.multi_async = False; r.sleep = lambda s: None
    return r, sent

def _fake_img(out):
    out.mkdir(parents=True, exist_ok=True)
    p = out / "gen-test.jpg"; p.write_bytes(b"\xff\xd8\xff" + b"0" * 10); return p

calls.clear()
llm = ToolLLM({"name": "web_search", "arguments": json.dumps({"query": "今天 AI 新闻"})}, "今天某某发了新模型，挺猛的（来源：news.example.com）")
r, sent = mk(llm)
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005今天AI圈有什么新闻", [BOT])))
texts = [m.content for m in sent]
assert texts[0] == "我查一下哈～" and "news.example.com" in texts[-1], texts
assert llm.seen[0]["tools"] == ["web_search", "generate_image"] or llm.seen[0]["tools"] == ["web_search"], llm.seen[0]["tools"]
assert calls == ["今天 AI 新闻"]
# 闲聊不带工具
llm2 = ToolLLM(None, "x"); r2, _ = mk(llm2)
r2.dispatch(normalize(raw("wxid_a", "@小助手\u2005讲个笑话", [BOT])))
assert llm2.seen == []
# 本群关了 web_search → 不带搜索工具
st = all_on(); st.set(ROOM, "web_search", False); st.set(ROOM, "image_gen", False)
llm3 = ToolLLM(None, "x"); r3, _ = mk(llm3, st=st)
r3.dispatch(normalize(raw("wxid_a", "@小助手\u2005今天有什么新闻", [BOT])))
assert llm3.seen == []
assert "联网搜索：" not in r3.chat.capabilities(ROOM) and "联网搜索：" in r.chat.capabilities(ROOM)
print("端到端搜索：垫话 + 来源 / 闲聊不带 / 开关 / 能力清单 ✅")

# ---- 生图口令：先回「在画了」，后台出图 → 发图（带 image_path），上下文记一笔 ----
r, sent = mk(ToolLLM(None, "x"))
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005画一张橘猫看月亮", [BOT])))
assert "画" in sent[0].content and sent[0].image_path is None
assert sent[1].image_path and sent[1].image_path.endswith("gen-test.jpg")
assert any("自己画的图：橘猫看月亮" in e["t"] for e in r.context.entries(ROOM))
# 婉拒（不调接口、不扣额度）
n_before = r.imagegen.counter.get(f"img:{ROOM}")
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005画一张周杰伦的照片", [BOT])))
assert ("真人" in sent[-1].content or "卡通" in sent[-1].content) and sent[-1].image_path is None
assert r.imagegen.counter.get(f"img:{ROOM}") == n_before
# 每群每天上限
r.imagegen.daily = 1
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005画一张柯基", [BOT])))
assert "明天再画" in sent[-1].content
# 失败 → 口语说明 + 退额度
def bad(p, out):
    import minimax_media
    raise minimax_media.MediaError("boom")
r, sent = mk(ToolLLM(None, "x"), img_fn=bad)
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005画一张柯基", [BOT])))
assert "没画出来" in sent[-1].content and r.imagegen.counter.get(f"img:{ROOM}") == 0
# 开关关 → 口令不触发（落到陪聊）
st = all_on(); st.set(ROOM, "image_gen", False)
r, sent = mk(ToolLLM(None, "陪聊回答"), st=st)
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005画一张柯基", [BOT])))
assert all(m.image_path is None for m in sent) and sent[-1].content in ("普通回复", "陪聊回答")
print("生图口令：先回+异步发图 / 婉拒 / 上限 / 失败退额度 / 开关 ✅")

# ---- 生图工具：模型调用 generate_image ----
llm = ToolLLM({"name": "generate_image", "arguments": json.dumps({"prompt": "雪山小木屋"})}, "好嘞，在画了～")
r, sent = mk(llm)
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005能给我整个雪山小木屋的壁纸吗", [BOT])))
txt = [m.content for m in sent]
assert "我查一下哈～" not in txt                                     # 生图不垫「我查一下」
assert any(m.image_path for m in sent) and "在画了" in txt[-1] or "在画了" in " ".join(txt), txt
print("生图工具 ✅")
print("test_search_image 全部通过")
