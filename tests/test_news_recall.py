"""主动推送进上下文 + 最近一期快讯回忆（10-22 17:33「七猫？」复现）+ 长轮询看门狗。全 mock，不联网不发群。"""
import sys, json, tempfile, time, threading, os
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["BOT_AIHOT_LLM_DEDUP"] = "0"
from app import build_router
from models import normalize
from router import Reply
from features import ai_news, flash_page
from features.news_recall import NewsRecall, save_issue
from features.settings import FeatureSettings
from tests.helpers import MEMBERS, ROOM, BOT, raw, all_on

ITEMS = [{"n": 1, "title": "黄仁勋：AI将创造未来工作", "sum": "英伟达CEO黄仁勋称AI将帮助年轻人创造新工作。", "source": "Hesamation（X）",
          "src_url": "https://x.com/Hesamation/status/1", "pub": "10-22 15:21"},
         {"n": 3, "title": "OpenAI CLI新增REST API调用", "sum": "OpenAI官方命令行工具支持调用REST API。", "source": "Hacker News",
          "src_url": "https://github.com/openai/openai-cli", "pub": "10-22 13:23"},
         {"n": 7, "title": "4K花园与七猫合作拓展海外AI短剧", "sum": "4K花园与百度旗下七猫中文网签署联合投资协议，由4K花园与蝶变AI创作者联盟共同承制海外AI短剧。",
          "source": "36氪", "src_url": "https://36kr.com/newsflashes/4036569140728704", "pub": "10-22 12:50"},
         {"n": 9, "title": "DeepSeek据报完成150亿美元融资", "sum": "DeepSeek据报以约748亿美元投前估值完成约150亿美元融资。", "source": "Techmeme",
          "src_url": "https://www.techmeme.com/x", "pub": "10-22 11:00"}]
NOW = datetime(2026, 10, 22, 17, 33, tzinfo=ai_news.TZ).timestamp()
d = Path(tempfile.mkdtemp()); lp = d / "ai_news_last.json"
save_issue(lp, ROOM, {"issue": "20261022-17-a54ac9", "url": "https://aihot.example.com/flash/20261022-17-a54ac9.html",
                      "intro": "", "items": ITEMS}, clock=lambda: NOW - 32 * 60)
assert oct(lp.stat().st_mode & 0o777) == "0o600"
rc = NewsRecall(lp, clock=lambda: NOW)

# ---- 匹配：关键词 / 编号 / 问推送本身 / 无关不注入 / 过期不注入 / 别的群不注入 ----
_, hit, _ = rc.match(ROOM, "七猫？"); assert [h["n"] for h in hit] == [7], hit
_, hit, _ = rc.match(ROOM, "第3条怎么回事"); assert [h["n"] for h in hit] == [3]
_, hit, _ = rc.match(ROOM, "第七条呢"); assert [h["n"] for h in hit] == [7]
_, hit, _ = rc.match(ROOM, "07 是啥"); assert [h["n"] for h in hit] == [7]
_, hit, _ = rc.match(ROOM, "deepseek 融了多少"); assert [h["n"] for h in hit] == [9]
_, hit, _ = rc.match(ROOM, "蝶变是什么"); assert [h["n"] for h in hit] == [7]
_, hit, ask = rc.match(ROOM, "你刚发的消息啊"); assert hit == [] and ask
p = rc.prompt_for(ROOM, "七猫？")
assert "07. 4K花园与七猫合作拓展海外AI短剧" in p and "百度旗下七猫中文网" in p and "36氪" in p and "36kr.com" in p and "别说看不到" in p
assert "这期的条目" in rc.prompt_for(ROOM, "你刚发的消息啊") and "01. 黄仁勋" in rc.prompt_for(ROOM, "你刚发的消息啊")
for q in ("今天吃啥", "哈哈哈", "你是谁", "什么意思", "这个公司怎么样"):
    assert rc.prompt_for(ROOM, q) == "", q
assert rc.prompt_for("other@chatroom", "七猫？") == ""
assert NewsRecall(lp, clock=lambda: NOW + 25 * 3600).prompt_for(ROOM, "七猫？") == ""
print("快讯回忆：关键词/编号/中文编号/问推送本身/无关不注入/过期/别群 ✅")

# ---- 端到端：@小助手 七猫？ → 陪聊 system 里有第 07 条的摘要和来源 ----
class Mock:
    def __init__(self): self.calls = []
    def chat(self, user, system=None, history=None, **k):
        self.calls.append({"user": user, "system": system, "history": history}); return "第07条说的是4K花园和七猫合作做海外AI短剧"
sent = []; llm = Mock()
r = build_router(fetch_list=lambda x: MEMBERS, fetch_info=lambda x: {}, sender=sent.append, send_enabled=True,
                 db_path=d / "s.db", llm=llm, settings=all_on(), allow_targets={ROOM}, amap_tools=False,
                 search_fn=lambda q: [], image_async=False)
r.multi_async = False; r.sleep = lambda s: None
r.chat.news_recall = NewsRecall(lp, clock=lambda: NOW)   # 固定时钟：记录写于 NOW-32min，用真实时钟过了 25h TTL 就会误判过期
# 快讯推送走 router.deliver：整条进上下文（以前 200 字截断，第 3 条以后全丢）
push = "📡 AI 快讯 | 10 月 22 日 17 时\n\n导语。\n\n" + "\n".join(f"{i}. 很长的一条快讯标题第{i}条凑字数凑字数凑字数" for i in range(1, 8)) \
       + "\n7. 4K花园与七猫合作拓展海外AI短剧\n\n详情: https://aihot.example.com/flash/20261022-17-a54ac9.html"
assert len(push) > 250
assert r.deliver(Reply(ROOM, push))
ent = [e for e in r.context.entries(ROOM) if e["r"] == "a"]
assert ent and "七猫" in ent[-1]["t"] and ent[-1]["t"].endswith(".html")
# 日报这类长文字同样整条记
rep = "📝 群聊日报\n" + "今天大家聊了很多话题。" * 40
r.deliver(Reply(ROOM, rep)); assert len(r.context.entries(ROOM)[-1]["t"]) >= 400
r.dispatch(normalize(raw("wxid_owner", "@小助手\u2005七猫？", at=[BOT])))
assert llm.calls, "没调模型"
sysmsg = llm.calls[-1]["system"]
assert "4K花园与七猫合作拓展海外AI短剧" in sysmsg and "百度旗下七猫中文网" in sysmsg and "36氪" in sysmsg
assert sent and "七猫" in sent[-1].content
print("端到端：推送整条进上下文 / 日报整条进上下文 / 问「七猫？」注入第 07 条摘要与来源 ✅")

# ---- ai_news 推送成功后写 ai_news_last.json（结构化：编号/短标题/摘要/来源/原文/详情页）----
class LLM:
    def chat(self, prompt, system=None, **kw):
        import re
        items = json.loads(re.search(r"\[\{\"i\".*?\}\](?=\s|$)", prompt, re.S).group(0))
        return json.dumps({"intro": "导语。第二句。", "items": [{"i": it["i"], "title": f"短标题{it['title']}的一条完整快讯", "summary": f"我们改写的{it['title']}摘要"}
                                                          for it in items]}, ensure_ascii=False)
iso = lambda h: datetime.fromtimestamp(NOW - h * 3600, ai_news.TZ).isoformat()
evs = [{"id": f"x{i}", "rank": i, "title": f"新闻{'甲乙丙'[i]}七猫" if i == 2 else f"新闻{'甲乙丙'[i]}", "summary": "原摘要。",
        "url": f"https://aihot.example.com/story/x{i}",
        "sources": [{"name": f"媒体{i}", "url": f"https://m{i}.example/a", "published_at": iso(1 + i)}]} for i in range(3)]
class NR:
    allow_targets, block_targets = {ROOM}, set()
    def __init__(self): self.sent = []
    def deliver(self, rep): self.sent.append(rep); return True
st = FeatureSettings(None); st.set(ROOM, "ai_news", True)
sp = d / "news" / "sent.json"; sp.parent.mkdir()
sx = ai_news.AiNewsScheduler(NR(), settings=st, fetch=lambda day: {"date": day, "events": evs}, summarize=LLM(), state_path=sp,
                             clock=lambda: NOW - 32 * 60, publish=lambda issue, page: flash_page.page_url(issue))
assert sx.tick() == 1
rec = json.loads((sp.parent / "ai_news_last.json").read_text())[ROOM]
assert rec["url"].startswith("https://aihot.example.com/flash/") and len(rec["items"]) == 3
it = rec["items"][0]
assert it["n"] == 1 and it["title"].startswith("短") and it["sum"].startswith("我们改写的") and it["source"] and it["src_url"] and it["pub"]
assert NewsRecall(sp.parent / "ai_news_last.json", clock=lambda: NOW).match(ROOM, "七猫那条")[1][0]["orig_title"] == "新闻丙七猫"
print("快讯推送成功 → 结构化记录最近一期 ✅")

# ---- 长轮询看门狗：卡住超总时限放弃；机器挂起（墙钟跳变）立即放弃；正常/异常照常 ----
import poller
resets = []
poller.reset_session = lambda: resets.append(1)
t0 = time.monotonic()
try:
    poller.pull_guarded({}, 0, deadline=1.5, _pull=lambda c, a, l: time.sleep(30)); raise AssertionError("没超时")
except poller.PullStuck as e:
    assert "总时限" in str(e) and time.monotonic() - t0 < 5 and resets
assert poller.pull_guarded({}, 0, _pull=lambda c, a, l: [{"id": 1}]) == [{"id": 1}]
try:
    poller.pull_guarded({}, 0, _pull=lambda c, a, l: (_ for _ in ()).throw(RuntimeError("pull http 502"))); raise AssertionError
except RuntimeError as e:
    assert "502" in str(e)
real = time.time; base = [real()]
def fake_time():                       # 模拟虚拟机挂起 22 分钟后恢复：墙钟一下多走 1320s，单调钟没走
    return real() + (1320 if real() - base[0] > 1.2 else 0)
poller.time.time = fake_time
try:
    t0 = time.monotonic()
    poller.pull_guarded({}, 0, deadline=60, _pull=lambda c, a, l: time.sleep(30)); raise AssertionError
except poller.PullStuck as e:
    assert "挂起" in str(e) and time.monotonic() - t0 < 5
finally:
    poller.time.time = real
print("长轮询看门狗：总时限 / 挂起检测 / 正常与异常透传 ✅")
