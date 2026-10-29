"""发送判定 + 省额度校验：仅白名单会话调模型、真发。sender/LLM 都是假的，不打 gewe、不打模型。"""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["BOT_SEND_ALLOW"] = "20000000002@chatroom"
from app import build_router
from features.settings import FeatureSettings
from models import normalize

BOT, TEST, OVERSEA = "wxid_bot_demo", "20000000002@chatroom", "20000000005@chatroom"


class Mock:
    def __init__(self): self.calls = []
    def chat(self, user, system=None, history=None, **kw):
        self.calls.append(user); return "mock"


def at_msg(room, mid):
    return {"TypeName": "AddMsg", "Appid": "wx_sim", "Wxid": BOT, "Data": {
        "NewMsgId": mid, "MsgType": 1, "FromUserName": {"string": room}, "ToUserName": {"string": BOT},
        "Content": {"string": "wxid_x:\n@小助手\u2005你好"}, "CreateTime": 1792493289,
        "MsgSource": f"<msgsource><atuserlist><![CDATA[{BOT}]]></atuserlist></msgsource>"}}


def make(settings, llm, sent):
    return build_router(fetch_list=lambda x: {}, fetch_info=lambda x: {}, sender=sent.append, send_enabled=True,
                        db_path=Path(tempfile.mkdtemp()) / "t.db", llm=llm, settings=settings)


st = FeatureSettings(None)
for room in (TEST, OVERSEA, "wxid_someone"):   # 开关都打开，只验证白名单这一层
    st.set(room, "chat_companion", True)
sent, llm = [], Mock()
r = make(st, llm, sent)
assert r.allow_targets == {TEST}, r.allow_targets
t1 = r.dispatch(normalize(at_msg(OVERSEA, 1)))
t2 = r.dispatch(normalize(at_msg(TEST, 2)))
t3 = r.dispatch(normalize({"TypeName": "AddMsg", "Appid": "wx_sim", "Wxid": BOT, "Data": {"NewMsgId": 3, "MsgType": 1,
     "FromUserName": {"string": "wxid_someone"}, "ToUserName": {"string": BOT}, "Content": {"string": "在吗"}}}))
print("海外群:", t1); print("测试群:", t2[-1]); print("私聊:", t3)
assert not any(x["feature"] == "chat_companion" for x in t1 + t3), "非白名单会话不应进入陪聊"
assert len(llm.calls) == 1, f"模型只应被调 1 次（测试群），实际 {llm.calls}"
assert t2[-1] == {"feature": "chat_companion", "reply": "mock", "sent": True}
assert [x.to_wxid for x in sent] == [TEST]

# 开关关掉 → 测试群也不调模型
st.set(TEST, "chat_companion", False)
assert not any(x["feature"] == "chat_companion" for x in r.dispatch(normalize(at_msg(TEST, 4))))
assert len(llm.calls) == 1

# 未设白名单 → 空集合 → 一律不发、不调模型
del os.environ["BOT_SEND_ALLOW"]
st.set(TEST, "chat_companion", True)
llm2 = Mock(); r2 = make(st, llm2, sent)
assert r2.allow_targets == set() and r2.dispatch(normalize(at_msg(TEST, 9)))[-1]["sent"] is False and not llm2.calls
print("发送判定 + 省额度校验通过 ✅")


def test_stale_passive_only():
    from features.settings import FeatureSettings as FS
    st = FS(None); st.set(TEST, "chat_companion", True)
    llm, sent = Mock(), []
    os.environ["BOT_SEND_ALLOW"] = TEST
    r = make(st, llm, sent)
    tr = r.dispatch(normalize(at_msg(TEST, 990001)), passive_only=True)
    assert llm.calls == [] and sent == [] and all(t["feature"] != "chat_companion" for t in tr)
    tr = r.dispatch(normalize(at_msg(TEST, 990002)))
    assert len(llm.calls) == 1 and len(sent) == 1
    print("积压旧消息不补发 ✅")


test_stale_passive_only()
