"""陪聊共享上下文 + 能力清单 + 帮开局（全 mock，不发消息）。
复现 2026-10-21 15:49 狼人杀对话：上下文要带小助手自己的话（全部分条）、别人的话（带昵称）、重启不丢；
模型不许假装主持没有的游戏；有的游戏「来一局 / ok 开始」直接开局。"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.helpers import Bot, ROOM, all_on
from app import build_router
from features.chat_context import ChatContext
from features.capabilities import describe, wanted_game
from features.settings import FeatureSettings


class Rec:
    def __init__(self, replies=None):
        self.calls, self.replies = [], list(replies or [])
    def chat(self, user, system=None, history=None, **kw):
        self.calls.append({"user": user, "system": system, "history": list(history or [])})
        return self.replies.pop(0) if self.replies else "嗯嗯"


def hist_text(call):
    return "\n".join(f"{m['role']}:{m['content']}" for m in call["history"])


# ---- ChatContext：合并、排除当前、字数上限、落盘 ----
tmp = Path(tempfile.mkdtemp())
c = ChatContext(tmp / "ctx.json")
c.add("r", "user", "狼人杀怎么玩", name="主人", mid=1)
c.add("r", "assistant", "晚上狼人商量杀谁")
c.add("r", "assistant", "想玩的话我可以当主持")
c.add("r", "user", "1", name="主人", mid=2)
c.add("r", "user", "2", name="陈亚洁", mid=3)
c.add("r", "user", "需要几个人？", name="主人", mid=4)
c.add("r", "user", "需要几个人？", name="主人", mid=4)          # 同 id 去重
h = ChatContext(tmp / "ctx.json").history("r", exclude=4)        # 新实例 = 重启后
assert [m["role"] for m in h] == ["user", "assistant", "user"]
assert h[1]["content"] == "晚上狼人商量杀谁\n想玩的话我可以当主持"      # 分条全部保留
assert h[2]["content"] == "[主人]：1\n[陈亚洁]：2" and "需要几个人" not in str(h)
for i in range(100):
    c.add("r2", "user", "字" * 150, name="x", mid=100 + i)
assert sum(len(m["content"]) for m in c.history("r2")) <= 1900
print("ChatContext 合并/去重/排除当前/落盘 ✅")

# ---- 端到端：router 记录别人的话 + 小助手的分条；@ 时模型看得到 ----
llm = Rec(["晚上狼人商量杀谁，白天投票放逐\n想玩的话我可以当主持", "好", "要 6 个人起"])
b = Bot(llm=llm)
b.say("wxid_owner", "狼人杀怎么玩")
b.say("wxid_owner", "1", at_bot=False)            # 追问窗口内 → 也会进陪聊（第 2 次调用）
b.say("wxid_a", "2", at_bot=False)                # 别人不 @：不回，但进上下文
b.say("wxid_owner", "需要几个人？")
last = llm.calls[-1]
ht = hist_text(last)
assert "晚上狼人商量杀谁" in ht and "想玩的话我可以当主持" in ht, ht       # 自己的两条都在
assert "[阿离]：2" in ht and "[主人]：1" in ht, ht                           # 别人的话带昵称
assert "需要几个人" not in ht and last["user"] == "[主人]：需要几个人？"     # 当前句不重复
assert "狼人杀" in last["system"] and "做不了" in last["system"]              # 能力清单：没有的不许答应
assert "谁是卧底（口令：@小助手 谁是卧底" in last["system"]
print("群共享上下文（他人 + 自己全部分条 + 昵称）✅")

# ---- 重启不丢：同一 context_path 新建 router ----
d = Path(tempfile.mkdtemp())
def mk(llm):
    return build_router(fetch_list=lambda r: {"memberList": [{"wxid": "wxid_owner", "nickName": "主人"}]},
                        fetch_info=lambda r: {}, db_path=d / "s.db", llm=llm, settings=all_on(),
                        allow_targets={ROOM}, send_enabled=False, context_path=d / "ctx.json",
                        amap_tools=False)
from tests.helpers import raw, BOT
from models import normalize
r1 = mk(Rec(["我在呢\n有啥事"]))
r1.dispatch(normalize(raw("wxid_owner", "@小助手\u2005狼人杀怎么玩", [BOT])))
llm2 = Rec(["6 个人"])
r2 = mk(llm2)                                       # 模拟重启
r2.dispatch(normalize(raw("wxid_owner", "@小助手\u2005需要几个人？", [BOT])))
ht = hist_text(llm2.calls[-1])
assert "狼人杀怎么玩" in ht and "我在呢" in ht and "有啥事" in ht, ht
print("重启不丢上下文 ✅")

# ---- 非白名单 / 观察群不记 ----
r1.dispatch(normalize(raw("wxid_owner", "别的群的话", room="999@chatroom")))
assert r1.context.entries("999@chatroom") == []
print("非白名单不记 ✅")

# ---- 能力清单按开关动态生成 ----
st = FeatureSettings(None)
st.set("g", "game_undercover", True); st.set("g", "game_handle", False); st.set("g", "game_minesweeper", False)
st.set("g", "game_idiom_chain", False); st.set("g", "game_guess_song", False)
txt = describe(st.enabled, "g")
assert "谁是卧底" in txt and "扫雷（口令" not in txt and "猜成语（口令" not in txt
st.set("g", "game_undercover", False)
assert "没开任何游戏" in describe(st.enabled, "g")
print("能力清单按开关 ✅")

# ---- 帮开局 ----
names = ["谁是卧底", "成语接龙", "猜成语", "猜歌", "扫雷"]
assert wanted_game("来一局谁是卧底吧", "", names) == "谁是卧底"
assert wanted_game("玩接龙", "", names) == "成语接龙"
assert wanted_game("来一局狼人杀", "", names) is None
assert wanted_game("ok 开始", "狼人杀我还不会～要不玩谁是卧底？", names) == "谁是卧底"
assert wanted_game("ok 开始", "要不玩谁是卧底或者成语接龙？", names) is None     # 推荐了两个 → 不替人选
assert wanted_game("ok 开始", "", names) is None
assert wanted_game("好的我知道了谢谢", "要不玩谁是卧底？", names) is None
llm = Rec(["狼人杀我还不会哈\n要不玩谁是卧底？回我一句开始就开"])
b = Bot(llm=llm)
b.say("wxid_owner", "玩狼人杀")
r = b.say("wxid_owner", "ok 开始", at_bot=False)   # 追问窗口内，接上推荐 → 真开局
assert r and "谁是卧底招募中" in r, r
assert len(llm.calls) == 1                           # 开局不调模型
b2 = Bot(llm=Rec())
assert "成语接龙" in (b2.say("wxid_a", "来一局成语接龙") or "") or b2.r.games.get(ROOM) is not None
print("帮开局 ✅")
print("test_chat_context 全部通过")

# ---- 模型回复里的【开局:名】→ 真开局；进行中不重复开、追问窗口暂停、提示模型别自己出题 ----
llm = Rec(["好嘞，来猜成语\n【开局:猜成语】"])
b = Bot(llm=llm)
r = b.say("wxid_owner", "那就玩第一个吧")
assert b.r.games.get(ROOM) is not None and b.r.games.get(ROOM).title == "猜成语"
txt = " ".join(o.content for o in b.r.outbox)
assert "开局:" not in txt and "好嘞" in txt, txt
n = len(llm.calls)
assert b.say("wxid_owner", "1", at_bot=False) is None or len(llm.calls) == n      # 游戏中：不 @ 不当追问
llm.replies = ["规则是直接发四字成语\n【开局:扫雷】"]
b.say("wxid_owner", "怎么玩")
assert "正在进行「猜成语」" in llm.calls[-1]["system"]
assert b.r.games.get(ROOM).title == "猜成语"                                       # 没被换成扫雷
assert "一次只能玩一局" not in " ".join(o.content for o in b.r.outbox[-3:])
print("模型开局标记 / 游戏中不抢话 ✅")
print("test_chat_context 追加全部通过")
