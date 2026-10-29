"""allow_send 观察群策略：取消硬屏蔽但只认 @、日上限、同一人冷却、追问窗口仅主人；全局忽略别的机器人（全 mock，不发消息）。"""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import observe_rooms, observe_policies, ignore_senders
from features.settings import FeatureSettings
from tests.helpers import Bot, BOT, raw
from tests.test_followup import quote_raw, Clock
from models import normalize

BIG, OTHER_OBS = "20000000003@chatroom", "11111@chatroom"
YM, FEIBI, MAOBING = "wxid_owner_demo", "wxid_peerbot_a", "wxid_peerbot_b"

# ---- observe.json 解析 ----
tmp = Path(tempfile.mkdtemp()) / "observe.json"
tmp.write_text(json.dumps({"rooms": {BIG: {"allow_send": True, "send_policy": {"daily_reply_cap": 3, "user_cooldown_sec": 60}},
                                     OTHER_OBS: {"name": "另一个观察群"}}}))
assert observe_rooms(tmp) == {OTHER_OBS}                       # 只有 allow_send 的不硬拦，其它观察群照旧
assert observe_policies(tmp) == {BIG: {"daily_reply_cap": 3, "user_cooldown_sec": 60, "followup": "owner_only"}}
assert ignore_senders() == {FEIBI, MAOBING}
print("observe.json 策略解析 ✅")


class LLM:
    def __init__(self): self.n = 0
    def chat(self, user, system=None, history=None, **kw):
        self.n += 1; return "好的"


def make(cap=3, cooldown=60):
    st = FeatureSettings(None)
    for k in ("chat_companion", "amap"):
        st.set(BIG, k, True)
    llm = LLM()
    b = Bot(llm=llm, settings=st, allow=(BIG, OTHER_OBS))
    r = b.r
    r.block_targets = {OTHER_OBS}
    r.room_policy = {BIG: {"daily_reply_cap": cap, "user_cooldown_sec": cooldown, "followup": "owner_only"}}
    clk = Clock(); r.now = clk
    r.followup.owner_only_rooms = {BIG}; r.followup.now = clk
    return b, llm, clk

say = lambda b, who, text, at=True, room=BIG: b.say(who, text, at_bot=at, room=room)

# ---- 只认 @ / 引用；冷却；日上限 ----
b, llm, clk = make()
assert say(b, "wxid_a", "你好") == "好的" and llm.n == 1
assert say(b, "wxid_a", "再问一句") is None and llm.n == 1          # 同一人 60 秒冷却：不调模型
assert say(b, "wxid_b", "我也问") == "好的"                         # 别人不受影响
clk.t += 61
assert say(b, "wxid_a", "冷却过了") == "好的"                      # 第 3 条
clk.t += 61
assert say(b, "wxid_c", "第四个") is None and llm.n == 3            # 日上限 3 → 拦
print("冷却 / 日上限 ✅")

b, llm, clk = make(cap=60)
assert say(b, "wxid_a", "随便聊聊", at=False) is None                # 不 @ 不回
assert say(b, "wxid_a", "小助手 北京天气", at=False) is None         # 大群不认前缀
tr = b.r.dispatch(normalize(quote_raw("wxid_d", "你说啥", BOT, room=BIG)))
assert any(t.get("reply") for t in tr)                               # 引用小助手 = @
print("只认 @ / 引用 ✅")

# ---- 追问窗口：仅主人本人 ----
b, llm, clk = make(cap=60, cooldown=0)
say(b, "wxid_a", "推荐个电影")
assert say(b, "wxid_a", "那另一部呢", at=False) is None              # 普通群友：没有追问窗口
say(b, YM, "推荐个电影")
assert say(b, YM, "那另一部呢", at=False) == "好的"                  # 主人本人：窗口内接
print("追问窗口仅主人 ✅")

# ---- 全局忽略机器人甲 / 机器人乙（即使 @ 小助手）----
b, llm, clk = make(cap=60, cooldown=0)
n = llm.n
assert say(b, FEIBI, "小助手你好呀") is None and say(b, MAOBING, "在吗") is None and llm.n == n
tr = b.r.dispatch(normalize(quote_raw(FEIBI, "你说得对", BOT, room=BIG)))
assert not any(t.get("reply") for t in tr)
assert say(b, FEIBI, "测试群也不理", room="20000000002@chatroom") is None   # 全局生效
print("忽略别的机器人 ✅")

# ---- 其它观察群仍硬拦 ----
b, llm, clk = make(cap=60, cooldown=0)
b.r.settings.set(OTHER_OBS, "chat_companion", True)
b.r.send_enabled = True; sent = []; b.r.sender = sent.append
say(b, "wxid_a", "你好", room=OTHER_OBS)
assert sent == []
say(b, "wxid_a", "你好", room=BIG)
assert len(sent) == 1
print("其它观察群仍硬拦 ✅")
print("test_room_policy 全部通过")
