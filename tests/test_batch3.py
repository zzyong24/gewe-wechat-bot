"""第三批：消息防抖 / 插话判断 / 长期记忆 / 掉线提醒 / AI 快讯。全 mock，不联网、不发消息。"""
import sys, tempfile, time, json, random
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from models import normalize
from tests.helpers import MEMBERS, ROOM, BOT, raw, private, all_on
from app import build_router
from features.settings import FeatureSettings

# ---------- 1. 防抖 ----------
from features.debounce import Debouncer


class FakeTimer:
    pending = []
    def __init__(self, sec, fn):
        self.sec, self.fn, self.cancelled = sec, fn, False
    def start(self):
        FakeTimer.pending.append(self)
    def cancel(self):
        self.cancelled = True

def fire():
    ts = [t for t in FakeTimer.pending if not t.cancelled]; FakeTimer.pending.clear()
    for t in ts:
        t.fn()

out = []
now = [1000.0]
deb = Debouncer(out.append, lambda m: m.self_wxid in m.at_list or not m.is_group, delay=2.5, max_wait=8,
                clock=lambda: now[0], timer_factory=FakeTimer)
a1 = normalize(raw("wxid_a", "@小助手\u2005帮我查下", [BOT]))
a2 = normalize(raw("wxid_a", "明天北京天气"))
b1 = normalize(raw("wxid_b", "哈哈哈"))
assert deb.offer(a1) and deb.offer(a2)            # 第二条没 @，但紧跟着补的 → 并进来
assert not deb.offer(b1)                          # 别人的普通消息照常处理
assert out == []
fire()
assert len(out) == 1 and out[0].content == "@小助手\u2005帮我查下\n明天北京天气" and out[0].merged_ids == [a2.msg_id]
# 非候选（图片）来了 → 先放行这一批，保证顺序
out.clear()
assert deb.offer(normalize(raw("wxid_a", "@小助手\u2005看这个", [BOT])))
img = normalize(raw("wxid_a", "<msg><img/></msg>", mt=3))
assert not deb.offer(img) and len(out) == 1 and out[0].content.endswith("看这个")
# 最长等待：截止时间不超过第一条 + max_wait
out.clear(); FakeTimer.pending.clear()
deb.offer(normalize(raw("wxid_c", "@小助手\u20051", [BOT]))); now[0] += 7
deb.offer(normalize(raw("wxid_c", "2")))
assert abs(FakeTimer.pending[-1].sec - 1.0) < 1e-6, FakeTimer.pending[-1].sec
fire(); assert out[0].content.endswith("1\n2")
# 私聊候选；BOT_DEBOUNCE_SEC=0 关闭
assert deb.offer(normalize(private("wxid_p", "在吗"))); fire()
assert not Debouncer(out.append, lambda m: True, delay=0).offer(normalize(private("wxid_p", "x")))
print("防抖：合并 / 别人不受影响 / 非候选先放行 / 最长等待 / 可关闭 ✅")

# poller 集成：合并后只调一次 dispatch
import poller, logging
class R:
    ignore_senders, allow_targets, block_targets = set(), {ROOM}, set()
    followup = None
    def __init__(self): self.seen = []
    def dispatch(self, m, passive_only=False):
        self.seen.append(m.content); return []
    class cache:
        @staticmethod
        def room_name(r): return "测试群"
r = R(); lg = logging.getLogger("t")
d2 = poller.make_debouncer(r, lg, None); d2.delay = 0.2
for t in ("@小助手\u2005第一句", "第二句"):
    p = raw("wxid_a", t, [BOT] if "@" in t else ())
    poller.handle_row(r, {"id": 1, "raw_json": json.dumps(p)}, lg, None, d2)
time.sleep(0.5)
assert r.seen == ["@小助手\u2005第一句\n第二句"], r.seen
print("poller 防抖集成 ✅")

# ---------- 2. 插话判断 ----------
from features.chime import ChimeFeature
class ChimeLLM:
    def __init__(self, judge): self.judge, self.calls = judge, []
    def chat(self, prompt, system=None, **kw):
        self.calls.append(prompt)
        if "要不要主动插一句话" in prompt:
            return self.judge
        return "我也想去"

def run_chime(judge_on, judge, last_bot=0.0, last_text="周末去爬山吧"):
    llm = ChimeLLM(judge)
    c = ChimeFeature(lambda room: llm, lambda room: "人设", rng=random.Random(1), cooldown_min=0, daily_max=10,
                     min_speakers=2, min_msgs=3, prob=1.0, judge_on=lambda room: judge_on, last_bot_ts=lambda room: last_bot)
    res = None
    for i, (w, t) in enumerate([("wxid_a", "周末干嘛"), ("wxid_b", "想出去玩"), ("wxid_c", last_text)]):
        res = c.handle(normalize(raw(w, t)))
    return res, llm

res, llm = run_chime(False, "")
assert res and res.content == "我也想去" and len(llm.calls) == 1          # 开关关：原逻辑不变
res, llm = run_chime(True, '{"related": 8, "waiting": false, "should": true}')
assert res and len(llm.calls) == 2
res, llm = run_chime(True, '{"related": 6, "waiting": false, "should": true}')
assert res is None and len(llm.calls) == 1                                # 相关度不够 → 不生成
res, llm = run_chime(True, '{"related": 4, "waiting": true, "should": true}')
assert res is None
res, llm = run_chime(True, '{"related": 5, "waiting": true, "should": true}')
assert res is not None                                                    # 有人在等回答 → 门槛降到 5
res, llm = run_chime(True, '{"related": 9, "should": false}')
assert res is None
res, llm = run_chime(True, '{"related": 9, "should": true}', last_bot=time.time() - 60)
assert res is None and llm.calls == []                                    # 刚说过话：连判断都不调
res, llm = run_chime(True, '{"related": 9, "should": true}', last_text="@小C 你去不去")
assert res is None and llm.calls == []                                    # 在等特定的人回答
print("插话判断：默认关不变 / 相关度门槛 / should / 刚说过话 / 在等别人回答 ✅")

# ---------- 3. 长期记忆 ----------
from features.memory import MemoryStore, Memory, clean_facts, ForgetMeFeature
assert clean_facts(["喜欢叫他阿离", "喜欢叫他阿离。", "手机号13800138000", "x" * 80], 8) == ["喜欢叫他阿离", "x" * 40]
d = Path(tempfile.mkdtemp())
class MemLLM:
    def __init__(self): self.n = 0; self.prompts = []
    def chat(self, user, system=None, history=None, **kw):
        if isinstance(user, str) and "整理长期记忆" in user:
            self.n += 1; self.prompts.append(user)
            return json.dumps({"person": ["希望被叫阿离", "喜欢喝冰美式", "希望被叫阿离"], "group": ["群里常聊 AI 编程"]}, ensure_ascii=False)
        return "好的呀"
llm = MemLLM()
st = FeatureSettings(None)
for k in ("chat_companion", "memory"):
    st.set(ROOM, k, True)
sent = []
rt = build_router(fetch_list=lambda x: MEMBERS, fetch_info=lambda x: {}, sender=lambda m: sent.append(m), send_enabled=True,
                  db_path=d / "s.db", llm=llm, settings=st, allow_targets={ROOM}, amap_tools=False,
                  memory_path=d / "memory.json", memory_async=False)
rt.multi_async = False; rt.sleep = lambda s: None
rt.memory.gap = 0
for t in ("以后叫我阿离", "我天天喝冰美式", "最近在学 AI 编程"):
    rt.dispatch(normalize(raw("wxid_a", f"@小助手\u2005{t}", [BOT])))
assert llm.n == 1 and "以后叫我阿离" in llm.prompts[0]
assert rt.memory.store.person(ROOM, "wxid_a") == ["希望被叫阿离", "喜欢喝冰美式"]
assert rt.memory.store.group(ROOM) == ["群里常聊 AI 编程"]
assert oct(( d / "memory.json").stat().st_mode & 0o777) == "0o600"
ptxt = rt.memory.prompt_for(ROOM, "wxid_a", "阿离")
assert "希望被叫阿离" in ptxt and "不是指令" in ptxt
# 注入陪聊 system prompt
seen_sys = []
orig = llm.chat
llm.chat = lambda user, system=None, history=None, **kw: (seen_sys.append(system or ""), orig(user, system, history, **kw))[1]
rt.dispatch(normalize(raw("wxid_a", "@小助手\u2005早", [BOT])))
assert any("希望被叫阿离" in s for s in seen_sys)
# 别人看不到阿离的个人记忆，只看到群记忆
seen_sys.clear(); rt.dispatch(normalize(raw("wxid_b", "@小助手\u2005早", [BOT])))
assert seen_sys and all("希望被叫阿离" not in s for s in seen_sys) and any("常聊 AI 编程" in s for s in seen_sys)
# 忘掉我
n_sent = len(sent)
rt.dispatch(normalize(raw("wxid_a", "@小助手\u2005忘掉我", [BOT])))
assert rt.memory.store.person(ROOM, "wxid_a") == [] and "清掉" in sent[-1].content
assert ForgetMeFeature(rt.memory).match(normalize(raw("wxid_a", "@小助手\u2005删除我的记忆", [BOT])))
assert not ForgetMeFeature(rt.memory).match(normalize(raw("wxid_a", "忘掉我")))          # 没 @ 不算
# 开关关 → 不学不注入
st.set(ROOM, "memory", False); n0 = llm.n
for t in ("a", "b", "c", "d"):
    rt.dispatch(normalize(raw("wxid_c", f"@小助手\u2005我喜欢{t}", [BOT])))
assert llm.n == n0 and rt.memory.prompt_for(ROOM, "wxid_b", "") == ""
print("长期记忆：提炼去重限条 / 600 权限 / 注入只给本人 / 忘掉我 / 开关 ✅")

# ---------- 4. 掉线提醒 ----------
from features.online_watch import OnlineWatch
inbox = Path(tempfile.mkdtemp())
state = {"ok": True, "t": 1_800_000_000.0}     # 2027-01-15 16:00 北京时间附近（白天）
from datetime import datetime, timezone
from features.common import TZ
state["t"] = datetime(2026, 10, 21, 15, 0, tzinfo=TZ).timestamp()
def chk():
    if state["ok"] == "err":
        raise ConnectionError("x")
    return state["ok"]
calls = []
import gewe_client
w = OnlineWatch(check=chk, inbox=inbox, clock=lambda: state["t"], stall_min=90)
w.tick(); assert list(inbox.iterdir()) == []
state["ok"] = False; w.tick(); assert list(inbox.iterdir()) == []                   # 一次离线不报
w.tick(); files = sorted(inbox.iterdir()); assert len(files) == 1
body = files[0].read_text(); assert "掉线" in body and "对象：我（主人）" in body and "类型：todo" in body
w.tick(); assert len(list(inbox.iterdir())) == 1                                     # 同一事件只投一次
todo_f = files[0]
state["ok"] = True; w.tick(); files = sorted(inbox.iterdir()); assert len(files) == 2
assert any("更新" in f.name and todo_f.name in f.read_text() for f in files)
state["ok"] = "err"; w.tick(); w.tick(); assert len(list(inbox.iterdir())) == 2; w.tick(); assert len(list(inbox.iterdir())) == 3
# 断流：白天 90 分钟没回调
inbox2 = Path(tempfile.mkdtemp()); state["ok"] = True
w2 = OnlineWatch(check=chk, inbox=inbox2, clock=lambda: state["t"], stall_min=90)
state["t"] += 91 * 60; w2.tick(); assert len(list(inbox2.iterdir())) == 1
w2.saw_callback(); assert len(list(inbox2.iterdir())) == 2
# 夜里不报断流
state["t"] = datetime(2026, 10, 22, 3, 0, tzinfo=TZ).timestamp()
w3 = OnlineWatch(check=chk, inbox=Path(tempfile.mkdtemp()), clock=lambda: state["t"], stall_min=90)
state["t"] += 100 * 60; w3.tick(); assert list(w3.inbox.iterdir()) == []
import inspect, features.online_watch as ow
src = inspect.getsource(ow)
assert "reconnection" not in src.split('"""', 2)[2] and "login/" not in src.split('"""', 2)[2]   # 代码里不调登录/重连
print("掉线提醒：连续两次离线才报 / 只投一次 / 恢复投更新 / 查询失败 / 白天断流 / 夜里不报 / 不调登录重连 ✅")

# ---------- 5. AI 快讯 ----------
import os as _os; _os.environ["BOT_AIHOT_LLM_DEDUP"] = "0"; _os.environ["BOT_AIHOT_LLM_VALUE"] = "0"
from features import ai_news
data = {"date": "2026-10-21", "timezone": "Asia/Shanghai", "summary": None, "events": [
    {"rank": 2, "id": "b", "title": "标题二", "summary": "第二条摘要第一句。第二句不要。", "url": "http://<your-server-ip>/story/b",
     "sources": [{"name": "X", "url": "https://x.com/2", "published_at": "…"}]},
    {"rank": 1, "id": "a", "title": "标题一", "summary": "很长" * 50, "url": "http://<your-server-ip>/story/a", "sources": []},
]}
txt = ai_news.format_news(data, summarize=lambda titles: "今天大家都在卷 Agent")
lines = txt.splitlines()
assert lines[0] == "🔥 AI 快讯｜10月21日 周三" and lines[1] == "今日一句话：今天大家都在卷 Agent"
assert txt.index("1. 标题一") < txt.index("2. 标题二")
assert "第二条摘要第一句。" in txt and "第二句不要" not in txt and "🔗 https://x.com/2" in txt
assert "<your-server-ip>" not in txt                                   # 内测期不用 story 链接；没 source 就不带
assert "…" in txt                                                    # 长摘要截断
assert "今日一句话" not in ai_news.format_news(data, summarize=None)  # summary null 且不总结 → 省略
data2 = dict(data, summary={"title": "T", "text": "官方一句话"})
assert "今日一句话：官方一句话" in ai_news.format_news(data2)
import os
os.environ["BOT_AIHOT_LINK"] = "story"
assert "🔗 https://aihot.example.com/story/a" in ai_news.format_news(data)
del os.environ["BOT_AIHOT_LINK"]

# ---- AGI Hunt 格式 ----
for _k in ("BOT_AIHOT_DETAIL_URL", "BOT_AIHOT_BRAND"):
    os.environ.pop(_k, None)          # import poller 时会加载 bot.env，这里先按默认（空）测
class HuntLLM:
    def __init__(self, out): self.out, self.n = out, 0
    def chat(self, prompt, system=None, **kw):
        self.n += 1
        if isinstance(self.out, Exception): raise self.out
        return self.out
evs2 = [{"rank": i + 1, "id": f"x{i}", "title": f"原标题{i}", "summary": "摘要", "sources": [{"name": "S", "url": "https://s"}]}
        for i in range(12)]
now18 = datetime(2026, 10, 21, 17, 0, tzinfo=TZ)
good = json.dumps({"intro": "A 公司发布 X，参数 400B。B 获 1 亿美元融资。",
                   "discuss": "A 的 400B 参数你会优先用在哪类任务？",
                   "titles": [f"短标题{i}号公司发布新模型" for i in range(10)][:9] + ["这是一个超过二十八个字的非常非常非常非常非常长的标题会被回退掉的"]}, ensure_ascii=False)
h = HuntLLM(good)
txt = ai_news.format_hunt({"events": evs2}, now18, h)
L = txt.splitlines()
assert L[0] == "📡 AI 快讯 | 10 月 21 日 17 时" and L[2] == "A 公司发布 X，参数 400B。B 获 1 亿美元融资。"
assert "1. 短标题0号公司发布新模型（S）" in txt and "9. 短标题8号公司发布新模型（S）" in txt and "10. 原标题9（S）" in txt and "11." not in txt
assert "💬 A 的 400B 参数你会优先用在哪类任务？" in txt
assert "详情" not in txt and "http" not in txt and h.n == 2                                    # 默认不带链接；批量改写 1 次 + 不完整标题重写 1 次
txt = ai_news.format_hunt({"events": evs2}, now18, HuntLLM(RuntimeError("down")))
assert "1. 原标题0（S）" in txt and txt.splitlines()[2] == "1. 原标题0（S）"                        # 模型失败 → 原标题+来源、无导语
os.environ["BOT_AIHOT_BRAND"] = "AGI 雷达"; os.environ["BOT_AIHOT_DETAIL_URL"] = "https://x.example/d/{ymd}{hour}"
txt = ai_news.format_hunt({"events": evs2}, now18, None)
assert txt.startswith("📡 AGI 雷达 | ") and txt.endswith("详情:https://x.example/d/2026102117")
assert ai_news.format_hunt({"events": evs2, "digest_url": "https://d/1"}, now18, None).endswith("详情:https://d/1")   # 接口给链接优先
del os.environ["BOT_AIHOT_BRAND"], os.environ["BOT_AIHOT_DETAIL_URL"]
assert ai_news.source_stats({"events": evs2[:3] + [{"sources": []}]}) == [("S", 3), ("（无来源）", 1)]
print("AGI Hunt 格式：标题行 / 导语 / ≤22 字短标题（超长回退）/ 失败回退 / 品牌与详情链接配置 / 接口链接优先 / 来源统计 ✅")

# ---- 每小时调度 ----
class NewsRouter:
    allow_targets, block_targets = {ROOM, "g2", "obs"}, {"obs"}
    def __init__(self): self.sent = []
    def deliver(self, rep): self.sent.append(rep); return True

def evs(ids):
    return [{"rank": i + 1, "id": x, "title": f"T{x}", "summary": f"S{x}。", "sources": [{"name": f"媒体{x}", "url": f"https://s/{x}",
                         "published_at": datetime.fromtimestamp(clock[0] - 3600, timezone.utc).isoformat()}]}
            for i, x in enumerate(ids)]
feed = {"ids": [f"e{i}" for i in range(15)], "fail": 0}
fetched = []
def fetch(day):
    fetched.append(day)
    if feed["fail"]:
        feed["fail"] -= 1; raise RuntimeError("HTTP 502")
    return {"date": day, "summary": None, "events": evs(feed["ids"])}
st = FeatureSettings(None)
for r in (ROOM, "g2", "obs"):
    st.set(r, "ai_news", True)
clock = [datetime(2026, 10, 22, 7, 30, tzinfo=TZ).timestamp()]
nr = NewsRouter(); sp = Path(tempfile.mkdtemp()) / "sent.json"
mk = lambda: ai_news.AiNewsScheduler(nr, settings=st, fetch=fetch, summarize=HuntLLM('{"intro": "今天卷 Agent。"}'), state_path=sp,
                                     clock=lambda: clock[0])
sch = mk()
assert sch.tick() == 0 and fetched == []                              # 夜里 / 早上 8 点前不推
clock[0] = datetime(2026, 10, 22, 8, 3, tzinfo=TZ).timestamp()
assert sch.tick() == 2 and {r.to_wxid for r in nr.sent} == {ROOM, "g2"}     # 观察群不推
t0 = nr.sent[0].content
assert "10 月 22 日 8 时" in t0 and "今天卷 Agent。" in t0 and "10. Te9" in t0 and "Te10" not in t0   # 最多 10 条、按热度
assert sch.tick() == 0 and len(fetched) == 1                          # 同一时段不重复
# 重启后同一时段不重复
sch = mk(); assert sch.tick() == 0
# 下个整点：只有 e10..e14 是新的 5 条 → 短标题、不带总结、重新编号
clock[0] = datetime(2026, 10, 22, 9, 1, tzinfo=TZ).timestamp(); nr.sent.clear()
assert sch.tick() == 2
t1 = nr.sent[0].content
assert t1.startswith("📡 AI 快讯 | 10 月 22 日 9 时") and "今天卷 Agent。" in t1 and "1. Te10" in t1 and "Te9" not in t1   # 每批都有导语
# 再下个整点没有新事件 → 不发
clock[0] = datetime(2026, 10, 22, 10, 2, tzinfo=TZ).timestamp(); nr.sent.clear()
assert sch.tick() == 0 and nr.sent == []
# 新来 2 条（<3）→ 也发，短标题
feed["ids"] = ["n1", "n2"] + feed["ids"]
clock[0] = datetime(2026, 10, 22, 11, 0, 30, tzinfo=TZ).timestamp()
assert sch.tick() == 2 and nr.sent[-1].content.startswith("📡 AI 快讯 | 10 月 22 日 11 时") and "1. Tn1" in nr.sent[-1].content
# 宽限期过了（整点后 >10 分钟）不推；23 点后不推
feed["ids"] = ["m1"] + feed["ids"]; nr.sent.clear()
clock[0] = datetime(2026, 10, 22, 12, 15, tzinfo=TZ).timestamp(); assert sch.tick() == 0
clock[0] = datetime(2026, 10, 22, 23, 5, tzinfo=TZ).timestamp(); assert sch.tick() == 0
# 第二天 8 点：夜里的不补，只推最新 ≤10 条，当天首推带总结
feed["ids"] = [f"d{i}" for i in range(30)]
clock[0] = datetime(2026, 10, 23, 8, 0, 10, tzinfo=TZ).timestamp()
assert sch.tick() == 2
assert "10 月 23 日 8 时" in nr.sent[-1].content and "10. Td9" in nr.sent[-1].content and "http" not in nr.sent[-1].content
# 拉取失败：2 分钟后重试，宽限期内成功
feed["ids"] = ["f1", "f2", "f3"]; feed["fail"] = 1; nr.sent.clear()
clock[0] = datetime(2026, 10, 23, 9, 0, 10, tzinfo=TZ).timestamp()
assert sch.tick() == 0
clock[0] += 60; assert sch.tick() == 0                                # 还没到重试时间
clock[0] += 70; assert sch.tick() == 2
# 间隔可配：30 分钟一次
s30 = ai_news.AiNewsScheduler(NewsRouter(), settings=st, fetch=fetch, summarize=False, state_path=Path(tempfile.mkdtemp()) / "s.json",
                              clock=lambda: clock[0], every_min=30)
assert s30.slot_of(datetime(2026, 10, 23, 9, 40, tzinfo=TZ)) == ("2026-10-23#19", 10.0)
# 手动推送记账：mark_sent 后同一批不再推
s2 = ai_news.AiNewsScheduler(NewsRouter(), settings=st, fetch=fetch, summarize=False, state_path=Path(tempfile.mkdtemp()) / "s.json",
                             clock=lambda: clock[0])
s2.mark_sent("g2", ["f1", "f2", "f3"], "2026-10-23", first=True)
assert s2.compose("g2", fetch("2026-10-23"), datetime(2026, 10, 23, 10, 0, tzinfo=TZ)) is None
print("AI 快讯：格式 / 短标题 / 每小时调度 / id 去重 / 首推带总结 / 无新不发 / 夜间不推不补 / 重试 / 重启不重复 / 间隔可配 ✅")
print("test_batch3 全部通过")

# ---- bot.env：详情链接持久配置 ----
import poller as _p
saved = {k: os.environ.pop(k, None) for k in ("BOT_AIHOT_DETAIL_URL", "BOT_AIHOT_API")}
_p._load_bot_env(Path(__file__).resolve().parent.parent / "bot.env.example")
assert os.environ["BOT_AIHOT_DETAIL_URL"] == "https://aihot.example.com/"
txt = ai_news.format_hunt({"events": evs2}, now18, None)
assert txt.endswith("\n\n详情:https://aihot.example.com/")
assert ai_news.format_hunt({"events": evs2, "share_url": "https://aihot.example.com/s/1"}, now18, None).endswith("详情:https://aihot.example.com/s/1")
os.environ["BOT_AIHOT_DETAIL_URL"] = "keep"; _p._load_bot_env(Path(__file__).resolve().parent.parent / "bot.env.example")
assert os.environ["BOT_AIHOT_DETAIL_URL"] == "keep"                  # 已有环境变量不被覆盖
for k, v in saved.items():
    os.environ.pop(k, None)
    if v is not None:
        os.environ[k] = v
print("bot.env 持久配置 / 详情链接 / share_url 优先 ✅")

# ---- digest：一小时一次、共用 share_url、过半重合才用 lead、失败回退首页、没新事件不 POST ----
posts = []
def dig_ok(day):
    posts.append(day)
    return {"id": "abc", "share_url": "https://aihot.example.com/s/abc", "lead": "服务端导语：A 发布 B。",
            "events": evs(feed_ids[:10])}
feed_ids = [f"g{i}" for i in range(14)]
def fetch2(day): return {"date": day, "events": evs(feed_ids)}
st2 = FeatureSettings(None); st2.set(ROOM, "ai_news", True); st2.set("g2", "ai_news", True)
nr2 = NewsRouter(); sp2 = Path(tempfile.mkdtemp()) / "s.json"
clock = [datetime(2026, 10, 24, 9, 1, tzinfo=TZ).timestamp()]
sch = ai_news.AiNewsScheduler(nr2, settings=st2, fetch=fetch2, summarize=HuntLLM('{"intro": "本地导语。"}'), state_path=sp2,
                              clock=lambda: clock[0], digest=dig_ok)
sch.mark_sent("g2", [f"g{i}" for i in range(8)], "2026-10-24")      # g2 已推过前 8 条 → 全局去重：两群都从 g8 起
assert sch.tick() == 2 and len(posts) == 1                            # 两个群只 POST 一次
by = {r.to_wxid: r.content for r in nr2.sent}
assert by[ROOM] == by["g2"]                                           # 所有群推同一份（10-22 主人）
assert by["g2"].endswith("详情:https://aihot.example.com/s/abc") and "本地导语。" in by["g2"]       # g8..g13，和 digest 不过半重合
assert "1. Tg8" in by["g2"] and "Tg7" not in by["g2"]
clock[0] = datetime(2026, 10, 24, 10, 1, tzinfo=TZ).timestamp(); nr2.sent.clear()
assert sch.tick() == 0 and len(posts) == 1                            # 全局已推完 → 两群都不推、不 POST
clock[0] = datetime(2026, 10, 24, 12, 1, tzinfo=TZ).timestamp(); nr2.sent.clear()
assert sch.tick() == 0 and len(posts) == 1                            # 没新事件 → 不 POST
def dig_bad(day): raise RuntimeError("HTTP 503")
feed_ids = ["h1", "h2", "h3"] + feed_ids
sch.digest = dig_bad
clock[0] = datetime(2026, 10, 24, 13, 1, tzinfo=TZ).timestamp()
assert sch.tick() == 2 and all(r.content.endswith("详情:https://aihot.example.com/") for r in nr2.sent)   # 失败回退首页
assert ai_news.lead_text({"lead": {"text": " x  y "}}) == "x y" and ai_news.lead_text({"lead": None}) == ""
print("digest：一小时一次 / 共用 share_url / 过半重合才用 lead / 无新不 POST / 失败回退首页 ✅")

# ---- 跨境丢包：gewe 发送只在建连超时时重试（防重复发），查询类连接错误都重试 ----
import requests, gewe_client as G
class FakeSess:
    def __init__(self, errs): self.errs, self.n = list(errs), 0
    def post(self, *a, **k):
        self.n += 1
        assert isinstance(k["timeout"], tuple) and k["timeout"][0] <= 5     # 连接超时短
        if self.errs:
            raise self.errs.pop(0)
        class R:
            status_code = 200
            def json(self): return {"ret": 200, "data": {"ok": 1}}
        return R()
_orig = (G._session, G.get_app_id, G._token, G.requests.Session, G.time.sleep)
try:
    G.get_app_id = lambda: "app"; G._token = lambda: "t"; G.time.sleep = lambda s: None
    holder = {}
    def new_sess():
        return holder["s"]
    G.requests.Session = new_sess
    holder["s"] = G._session = FakeSess([requests.exceptions.ConnectTimeout(), requests.exceptions.ConnectTimeout()])
    assert G.call("/gewe/v2/api/message/postText", {}) == {"ok": 1} and holder["s"].n == 3
    holder["s"] = G._session = FakeSess([requests.exceptions.ReadTimeout()])
    try:
        G.call("/gewe/v2/api/message/postText", {}); assert False
    except G.GeweError:
        assert holder["s"].n == 1                                      # 读超时：可能已发出 → 不重试
    holder["s"] = G._session = FakeSess([requests.ConnectionError()])
    try:
        G.call("/gewe/v2/api/message/postText", {}); assert False
    except G.GeweError:
        assert holder["s"].n == 1                                      # 发送类非建连超时的连接错误也不重试
    holder["s"] = G._session = FakeSess([requests.ConnectionError()])
    assert G.call("/gewe/v2/api/login/checkOnline", {}) == {"ok": 1}   # 查询类重试
finally:
    G._session, G.get_app_id, G._token, G.requests.Session, G.time.sleep = _orig
print("gewe 调用：短连接超时 / 发送防重复重试 / 查询重试 ✅")
