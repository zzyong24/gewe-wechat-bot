"""固定欢迎模板 + 进群欢迎 stale 仍发（不发微信、不打 gewe；模型用 Mock）。"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import build_router
from features.persona import PersonaStore
from features.settings import FeatureSettings
from models import normalize
from test_admin_member_change import BOT, ROOM, OWNER, MEMBERS, raw, sysmsg, last

AI = "20000000001@chatroom"
OTHER = "11111111111@chatroom"
TPL = "{ats}欢迎{names}加入！在这里，我们一起学习、交流、用好 AI。"


class MockLLM:
    def __init__(self, reply="模型欢迎语～"):
        self.calls, self.reply = [], reply
    def chat(self, user, system=None, history=None, **k):
        self.calls.append({"user": user, "system": system})
        return self.reply


def in_room(m, room):
    m["Data"]["FromUserName"]["string"] = room
    m["Data"]["Content"]["string"] = m["Data"]["Content"]["string"].replace(ROOM, room)
    return m


def setup(llm, rooms=(ROOM, AI), tpl_room=None):
    tmp = Path(tempfile.mkdtemp())
    st = FeatureSettings(tmp / "settings.json")
    for room in rooms:
        st.set(room, "welcome", True)
        st.set(room, "farewell", True)
    if tpl_room:
        st.room_set(tpl_room, "welcome_tpl", TPL)
    sent = []
    personas = PersonaStore(tmp / "p.json", default="你是「小助手」")
    r = build_router(fetch_list=lambda room: MEMBERS, fetch_info=lambda room: {"nickName": "示例测试群"},
                     db_path=tmp / "s.db", llm=llm, settings=st, allow_targets=set(rooms), send_enabled=True,
                     sender=sent.append, personas=personas)
    return r, st, sent, (lambda m, **kw: r.dispatch(normalize(m), **kw))


JOIN = lambda: sysmsg('"$username$"邀请"$names$"加入了群聊',
                      {"username": [(OWNER, "示例公众号")], "names": [("wxid_new", "群友小A")]})


def test_fixed_tpl_with_ats():
    llm = MockLLM()
    r, st, sent, d = setup(llm, rooms=(AI,), tpl_room=AI)
    t = last(d(in_room(JOIN(), AI)))
    expect = "@群友小A 欢迎加入！在这里，我们一起学习、交流、用好 AI。"
    assert t["reply"] == expect, t
    assert t["sent"] is True and sent[-1].ats == "wxid_new"
    assert llm.calls == []   # 有固定模板不调模型
    print("固定模板有@ ✅")


def test_fixed_tpl_without_wxid():
    """拿不到 wxid 时正文塞昵称，不带 @。"""
    llm = MockLLM()
    r, st, sent, d = setup(llm, rooms=(AI,), tpl_room=AI)
    m = sysmsg('"$username$"邀请"$names$"加入了群聊',
               {"username": [(OWNER, "主人")], "names": [("", "路人甲")]})
    # Person with empty wxid is filtered in parse — use plain text path instead
    from features.member_change import parse_plain, MemberChangeFeature, Person, MemberEvent
    feat = next(f for f in r.features if f.name == "member_change")
    ev = MemberEvent("join", AI, [Person("", "路人甲")], Person(OWNER, "主人"), "plain")
    reply = feat.welcome(ev, BOT)
    assert reply.content == "欢迎路人甲加入！在这里，我们一起学习、交流、用好 AI。"
    assert reply.ats is None and llm.calls == []
    print("固定模板无@塞昵称 ✅")


def test_other_room_still_llm():
    llm = MockLLM(reply="嗨～来玩呀")
    r, st, sent, d = setup(llm, rooms=(ROOM, AI), tpl_room=AI)  # 只给 AI 群写模板
    t = last(d(JOIN()))  # ROOM 无模板 → 走 LLM
    assert t["reply"] == "@群友小A 嗨～来玩呀" and len(llm.calls) == 1
    t2 = last(d(in_room(JOIN(), AI)))
    assert "用好 AI" in t2["reply"] and len(llm.calls) == 1  # AI 群不追加调用
    print("其它群仍走 LLM ✅")


def test_welcome_allow_stale():
    llm = MockLLM()
    r, st, sent, d = setup(llm, rooms=(AI,), tpl_room=AI)
    # 积压旧消息：passive_only=True，欢迎仍发
    t = last(d(in_room(JOIN(), AI), passive_only=True))
    assert t["sent"] is True and "skipped" not in t and "用好 AI" in t["reply"]
    assert len(sent) == 1 and sent[0].allow_stale is True
    # 无模板的普通欢迎（其它群）也带 allow_stale
    llm2 = MockLLM(reply="欢迎呀")
    r2, st2, sent2, d2 = setup(llm2, rooms=(ROOM,))
    t2 = last(d2(JOIN(), passive_only=True))
    assert t2["sent"] is True and t2["reply"].startswith("@群友小A 欢迎呀")
    assert sent2[-1].allow_stale is True
    # 陪聊在 stale 时仍不发
    st2.set(ROOM, "chat_companion", True)
    from test_send_gate import at_msg
    import os
    os.environ["BOT_SEND_ALLOW"] = ROOM
    # rebuild with chat on — use existing router: chat is already registered via settings at build?
    # build_router reads settings at match time via gate; chat feature always registered
    n_before = len(sent2)
    tr = r2.dispatch(normalize(at_msg(ROOM, 88001)), passive_only=True)
    assert all(x.get("skipped") == "stale" or x.get("feature") != "chat_companion" or not x.get("sent")
               for x in tr)
    assert len(sent2) == n_before  # 没多发陪聊
    print("stale 欢迎仍发、陪聊仍跳过 ✅")


def test_default_fallback_unchanged():
    llm = MockLLM(reply="   ")
    r, st, sent, d = setup(llm, rooms=(ROOM,))  # 无模板
    t = last(d(JOIN()))
    assert "欢迎加入示例测试群" in t["reply"] and llm.calls  # 空回复回退默认 WELCOME_TPL
    print("默认模板回退 ✅")


if __name__ == "__main__":
    test_fixed_tpl_with_ats()
    test_fixed_tpl_without_wxid()
    test_other_room_still_llm()
    test_welcome_allow_stale()
    test_default_fallback_unchanged()
    print("全部通过 ✅")
