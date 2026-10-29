"""10-21 漏回复现（771 补处理后 773 没接）+ 窗口从回复时刻起算 / 10 分钟续期 / 语义判断 / 插话不接 / SI 维持原规则。"""
import sys, json, tempfile, time
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import features.followup as F
from features.followup import Followup

R, SI, YM, B, O = "20000000004@chatroom", "20000000003@chatroom", "wxid_owner_demo", "wxid_bot", "wxid_other"
B0 = time.time() - 2800                         # 「23:42:20」（相对真实时钟，落盘恢复按真实时间判断）
clock = [B0]
def m(text, who=YM, room=R, at=(), t=None, quote_from=""):
    return NS(event="AddMsg", is_self=False, is_group=True, msg_type=1, content=text, room_id=room, from_wxid=who,
              self_wxid=B, at_list=list(at), quote_from=quote_from, addressed="", create_time=t if t is not None else clock[0])
calls = []
def judge(room, wxid, text, bot_text, lines):
    calls.append((room, wxid, text, bot_text, lines)); return judge.ans
judge.ans = True
fp = Path(tempfile.mkdtemp()) / "followup.json"

# ---- 复现：771（23:42:20 发的）在 23:46:11 补处理回复 → 窗口从 23:46:11 起算；773（23:47:44）要接 ----
f = Followup(path=str(fp), owners=lambda: {YM})                 # 默认 600s / 上限 20，用真实时钟分支
assert f.sec == 600 and f.max_n == 20
f.now = lambda: clock[0]; f.judge = judge
m771 = m("国家有啥政策扶持", t=B0); m771.at_list = [B]; m771.addressed = "followup"
clock[0] = B0 + 231                                          # 23:46:11 实际回复时刻
f.note_reply(m771, "chat_companion", "具身智能这块，国家政策主要是场景开放……揭榜挂帅项目落地")
assert f.win[(R, YM)]["until"] == B0 + 231 + 600
clock[0] = B0 + 324                                          # 23:47:44
f.now = None                                                     # 线上不注入时钟：按消息时间判断
m773 = m("揭榜挂帅项目啥意思啊", t=B0 + 324)
f.prepare(m773)
assert m773.addressed == "followup" and B in m773.at_list, m773.addressed
f.now = lambda: clock[0]; f.judge = judge
print("复现 773：补处理后窗口从回复时刻起算 ✅")

# ---- 连续聊：每回一次续期，不因 4 条截断；长消息也接 ----
for i in range(8):
    clock[0] += 300
    x = m("那这个呢再展开说说" + "很长" * 100 if i == 5 else f"第{i}问"); f.prepare(x)
    assert x.addressed == "followup", i
    f.note_reply(x, "chat_companion", f"答{i}")
print("连续聊续期、不被 4 条截断、长消息接 ✅")

# ---- 窗口过期但 30 分钟内聊过 → 语义判断 ----
clock[0] += 900
x = m("那报名入口在哪"); f.prepare(x)
assert calls and calls[-1][3] == "答7" and x.addressed == "followup_judged" and B in x.at_list
judge.ans = False; y = m("今晚吃啥"); f.prepare(y)
assert y.addressed == "" and B not in y.at_list
judge.ans = True
# 中间有别人插话 → 不判、不接
f.observe(m("我觉得不行", who=O))
n = len(calls); z = m("详细说说"); f.prepare(z)
assert len(calls) == n and B not in z.at_list
print("过期后语义判断 / 判否不接 / 有人插话不接 ✅")

# ---- @ 别人 / 引用别人 → 不接 ----
f2 = Followup(owners=lambda: {YM}, now=lambda: clock[0]); f2.judge = judge
f2.note_reply(m("q"), "chat_companion", "答"); clock[0] += 700
a = m("啥意思", at=[O]); f2.prepare(a); assert B not in a.at_list
f2.note_reply(m("q"), "chat_companion", "答"); clock[0] += 700
n = len(calls); q = m("啥意思", quote_from=O); f2.prepare(q); assert B not in q.at_list and len(calls) == n
# 跟别人聊过的话题，别人问「啥意思」（明显追问）→ 判断后接
f2.note_reply(m("q", who=O), "chat_companion", "揭榜挂帅是……"); clock[0] += 60
c = m("揭榜挂帅啥意思", who="wxid_c"); f2.prepare(c); assert c.addressed == "followup_judged"
d = m("今天好热", who="wxid_d"); n = len(calls); f2.prepare(d); assert len(calls) == n and B not in d.at_list   # 不像追问不判
print("@别人 / 引用别人不接，追问别人的话题判断后接 ✅")

# ---- SI：维持原规则（窗口只对主人，不做语义判断）----
f3 = Followup(owners=lambda: {YM}, owner_only_rooms={SI}, now=lambda: clock[0]); f3.judge = judge
f3.note_reply(m("q", who="wxid_s", room=SI), "chat_companion", "答"); clock[0] += 10
s1 = m("啥意思", who="wxid_s", room=SI); n = len(calls); f3.prepare(s1)
assert B not in s1.at_list and len(calls) == n
f3.note_reply(m("q", room=SI), "chat_companion", "答"); clock[0] += 10
s2 = m("那呢", room=SI); f3.prepare(s2); assert s2.addressed == "followup"
clock[0] += 900; s3 = m("啥意思", room=SI); n = len(calls); f3.prepare(s3)
assert B not in s3.at_list and len(calls) == n
print("SI 维持原规则 ✅")

# ---- 落盘：过期窗口的 last/text 也保留 30 分钟，重启后还能语义判断 ----
f4 = Followup(path=str(fp), owners=lambda: {YM}); f4.judge = judge
assert f4.win[(R, YM)]["text"]
print("追问窗口语义判断全部通过 ✅")
