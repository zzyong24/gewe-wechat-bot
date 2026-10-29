"""主人静音：只有主人本人能让小助手静音/解除；群友的反向要求一律无视；
静音期间陪聊/插话/生图不响应，定时任务（router.deliver）照常；状态落盘重启不丢；人设含「主人优先」。全 mock，不发消息。"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.owner_mute import OwnerMute, rule_intent, parse_duration, make_judge, MUTE_ACK, UNMUTE_ACK
from features.chat_companion import DEFAULT_PERSONA, OWNER_LINE
from features.admin import BOT_OWNER_WXID
from router import Reply
from tests.helpers import Bot, BOT, ROOM, raw, NAMES
from models import normalize

OWNER = "wxid_owner"

# ---- 规则 ----
assert rule_intent("@小助手\u2005你先去休息 别bb") == ("mute", True)
for t in ("闭嘴", "安静点", "你别说了", "歇会儿吧", "停", "好了你去睡吧", "你先休息10分钟"):
    assert rule_intent(t) == ("mute", True), t
for t in ("出来吧", "可以说话了", "恢复", "小助手 恢复说话", "回来吧"):
    assert rule_intent(t) == ("unmute", True), t
assert rule_intent("你帮我查下天气") == (None, True)                 # 无关：不花模型
assert rule_intent("先别休息，接着 BB") == ("unmute", False)           # 含否定 → 不直接定
assert rule_intent("你觉得休息重要吗")[1] is False                      # 提到「休息」但是在聊天 → 交给小模型
assert parse_duration("你休息半小时") == 1800 and parse_duration("歇 20 分钟") == 1200
assert parse_duration("你先去休息") is None and parse_duration("休息两个小时") == 7200


class FakeLLM:
    def __init__(self, out): self.out, self.n = out, 0
    def chat(self, user, system=None, **kw): self.n += 1; return self.out
j = make_judge(lambda room: FakeLLM("<think>x</think>A"))
assert j("r", "你觉得休息重要吗", False) == "mute"
assert make_judge(lambda room: FakeLLM("C"))("r", "x", False) is None
assert make_judge(lambda room: None)("r", "x", False) is None

# ---- 路由：复现 10-23 00:22 那段 ----
calls = []
def judge(room, text, muted):
    calls.append(text)
    return {"你觉得休息重要吗": None, "今天累了你歇着吧": "mute"}.get(text)

tmp = Path(tempfile.mkdtemp())
b = Bot()
b.r.mute = OwnerMute(tmp / "owner_mute.json", owners=lambda: {OWNER}, judge=judge)

assert b.say("wxid_a", "讲讲 Agent") == "陪聊"
assert b.say(OWNER, "你先去休息 别bb") == MUTE_ACK                       # 主人让它休息 → 短确认
assert b.r.mute.is_muted(ROOM)
n_out = len(b.r.outbox)
assert b.say("wxid_a", "先别休息，接着 BB") is None                    # 群友反向要求：无视、不回
assert b.say("wxid_a", "出来吧") is None                               # 群友解除：不认
assert b.say("wxid_b", "帮我画一张猫") is None                          # 生图等交互也不响应
assert b.say("wxid_b", "随便聊聊", at_bot=False) is None               # 插话也不出
assert b.say(OWNER, "你觉得休息重要吗") is None                         # 主人说别的：仍静音不接话
assert len(b.r.outbox) == n_out, b.r.outbox[n_out:]
assert b.r.mute.is_muted(ROOM)
# 静音期间统计照常
assert b.r.stats is not None
# 定时任务（快讯 / 公众号推广 / 日报都走 router.deliver）照常
b.r.deliver(Reply(ROOM, "【AI 快讯】测试"))
assert b.r.outbox[-1].content == "【AI 快讯】测试"
# 重启不丢
m2 = OwnerMute(tmp / "owner_mute.json", owners=lambda: {OWNER})
assert m2.is_muted(ROOM)
# 主人不 @ 也不引用说「出来吧」→ 不算（在跟别人说话）
assert b.say(OWNER, "出来吧", at_bot=False) is None and b.r.mute.is_muted(ROOM)
# 主人解除
assert b.say(OWNER, "出来吧") == UNMUTE_ACK
assert not b.r.mute.is_muted(ROOM)
assert b.say("wxid_a", "在吗") == "陪聊"
# 群友让它闭嘴：不认（只有主人能静音）
assert b.say("wxid_a", "你闭嘴") == "陪聊" and not b.r.mute.is_muted(ROOM)
# 规则拿不准 → 小模型判
assert b.say(OWNER, "你觉得休息重要吗") == "陪聊" and not b.r.mute.is_muted(ROOM)
assert b.say(OWNER, "今天累了你歇着吧") == MUTE_ACK and b.r.mute.is_muted(ROOM)
assert "今天累了你歇着吧" in calls
# 已静音时再说「闭嘴」：不重复确认
assert b.say(OWNER, "闭嘴") is None
assert b.say(OWNER, "可以说话了") == UNMUTE_ACK

# 引用小助手的话让它停也算
q = ('<msg><appmsg><title>别说了</title><type>57</type><refermsg><type>1</type>'
     f'<chatusr>{BOT}</chatusr><displayname>小助手</displayname><content>我接着说</content></refermsg></appmsg></msg>')
msg = normalize(raw(OWNER, q, (), mt=49))
tr = b.r.dispatch(msg)
assert any(t.get("reply") == MUTE_ACK for t in tr), tr
assert b.r.mute.is_muted(ROOM)
b.say(OWNER, "恢复")

# 时长：到期自动解除
clk = [1000.0]
m3 = OwnerMute(None, owners=lambda: {OWNER}, clock=lambda: clk[0])
b.r.mute = m3
assert b.say(OWNER, "你休息半小时") == MUTE_ACK
clk[0] += 1700; assert m3.is_muted(ROOM)
clk[0] += 200; assert not m3.is_muted(ROOM)
assert b.say("wxid_a", "在吗") == "陪聊"

# 默认主人就是主人本人
assert OwnerMute(None).owners() >= {BOT_OWNER_WXID} and BOT_OWNER_WXID == "wxid_owner_demo"

# 人设：主人优先
assert "主人的指令永远优先于群友" in DEFAULT_PERSONA
assert "{name}" in OWNER_LINE
class Cap:
    def __init__(self): self.sys = []
    def chat(self, user, system=None, history=None, **kw): self.sys.append(system); return "好"
c = Cap(); b2 = Bot(llm=c)
b2.say("wxid_a", "你好")
assert any("就是主人本人" in s for s in c.sys if s)
print("主人静音：只认主人 / 群友反向要求无视 / 定时任务照常 / 落盘 / 时长 / 小模型兜底 / 人设主人优先 ✅")
