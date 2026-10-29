"""进群欢迎 / 退群送别测试（不发微信、不打 gewe；模型用 Mock）。"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import build_router
from features.persona import PersonaStore
from features.settings import FeatureSettings
from models import normalize
from test_admin_member_change import BOT, ROOM, OWNER, ADMIN, USER, MEMBERS, raw, sysmsg, last

OTHER, OBS = "11111111111@chatroom", "20000000003@chatroom"


class MockLLM:
    def __init__(self, reply="欢迎欢迎～这里有好多有趣的人，常来冒泡呀", fail=False):
        self.calls, self.reply, self.fail = [], reply, fail
    def chat(self, user, system=None, history=None, **k):
        self.calls.append({"user": user, "system": system})
        if self.fail:
            raise RuntimeError("boom")
        return self.reply


def in_room(m, room):
    m["Data"]["FromUserName"]["string"] = room
    m["Data"]["Content"]["string"] = m["Data"]["Content"]["string"].replace(ROOM, room)
    return m


def setup(llm, allow=(ROOM, OBS), on=True):
    tmp = Path(tempfile.mkdtemp())
    st = FeatureSettings(tmp / "settings.json")
    if on:
        for room in (ROOM, OTHER, OBS):
            st.set(room, "welcome", True); st.set(room, "farewell", True)
    sent = []
    members = {"v": MEMBERS}
    personas = PersonaStore(tmp / "p.json", default="你是「小助手」")
    r = build_router(fetch_list=lambda room: members["v"], fetch_info=lambda room: {"nickName": "示例测试群"},
                     db_path=tmp / "s.db", llm=llm, settings=st, allow_targets=set(allow), send_enabled=True,
                     sender=sent.append, personas=personas, block_targets={OBS})
    return r, st, sent, members, personas, (lambda m: r.dispatch(normalize(m)))


JOIN = lambda: sysmsg('"$username$"邀请"$names$"加入了群聊', {"username": [(OWNER, "示例公众号")], "names": [("wxid_new", "群友小A")]})


def test_defaults_and_toggle():
    llm = MockLLM()
    r, st, sent, *_ , d = setup(llm, on=False)
    assert not st.enabled(ROOM, "welcome") and not st.enabled(ROOM, "farewell")       # 默认关
    t = last(d(JOIN()))
    assert t["reply"] == "欢迎 群友小A 加入本群～（示例公众号 邀请）" and llm.calls == []  # 走旧通知，不调模型
    assert "只有群主或管理员" in last(d(raw(USER, "@小助手 开启 进群欢迎", [BOT])))["reply"]
    assert not st.enabled(ROOM, "welcome")
    assert "已开启本群「进群欢迎」" in last(d(raw(OWNER, "@小助手 开启 进群欢迎", [BOT])))["reply"]
    assert "已开启本群「退群送别」" in last(d(raw(ADMIN, "@小助手 开启 送别", [BOT])))["reply"]
    assert st.enabled(ROOM, "welcome") and st.enabled(ROOM, "farewell")
    t = last(d(JOIN()))
    assert t["reply"].startswith("@群友小A 欢迎欢迎") and len(llm.calls) == 1
    d(raw(OWNER, "@小助手 关闭 欢迎", [BOT]))
    assert last(d(JOIN()))["reply"].startswith("欢迎 群友小A 加入本群")              # 关掉回到通知
    for k in ("member_change", "welcome", "farewell"):
        st.set(ROOM, k, False)
    assert last(d(JOIN())) is None                                                     # 三个都关 → 不说话
    print("开关 ✅")


def test_welcome_at_and_persona():
    llm = MockLLM(reply="「@某某 嗨～欢迎来玩，我是小助手！」\n第二行不要")
    r, st, sent, members, personas, d = setup(llm)
    personas.set(ROOM, "你是一只爱吐槽的猫娘", by=OWNER)
    t = last(d(JOIN()))
    assert t["sent"] is True and t["reply"] == "@群友小A 嗨～欢迎来玩，我是小助手！", t
    assert sent[-1].ats == "wxid_new" and sent[-1].to_wxid == ROOM
    assert llm.calls[-1]["system"].startswith("你是一只爱吐槽的猫娘\n（现在是北京时间") and "群友小A" in llm.calls[-1]["user"]
    # 多人进群：一起 @
    t = last(d(sysmsg('"$username$"邀请"$names$"加入了群聊', {"username": [(OWNER, "主人")],
                                                            "names": [("wxid_a", "阿离"), ("wxid_b", "Bob")]})))
    assert t["reply"].startswith("@阿离 @Bob ") and sent[-1].ats == "wxid_a,wxid_b"
    # 只拉了机器人自己 → 不欢迎
    assert last(d(sysmsg('"$username$"邀请你加入了群聊', {"username": [(OWNER, "主人")]}))) is None
    print("欢迎@新人 ✅", t["reply"])


def test_welcome_fallback_template():
    llm = MockLLM(fail=True)
    r, st, sent, *_, d = setup(llm)
    t = last(d(JOIN()))
    assert len(llm.calls) == 1 and t["sent"] is True
    assert t["reply"] == "@群友小A 欢迎加入示例测试群～我是小助手，有事 @我 就好，一起把日子过得亮堂堂 🌙", t
    assert sent[-1].ats == "wxid_new"
    llm2 = MockLLM(reply="   ")                                     # 空回复也回退
    r, st, sent, *_, d = setup(llm2)
    assert "欢迎加入示例测试群" in last(d(JOIN()))["reply"]
    print("欢迎回退模板 ✅")


def test_farewell_template():
    llm = MockLLM()
    r, st, sent, members, _, d = setup(llm)
    t = last(d(sysmsg('你将"$kickoutname$"移出了群聊', {"kickoutname": [("wxid_b", "Bob")]})))
    assert t["reply"] == "Bob 离开了本群，祝 TA 一路顺风！" and t["sent"] is True
    mod = lambda: {"TypeName": "ModContacts", "Appid": "wx_sim", "Wxid": BOT, "Data": {"UserName": {"string": ROOM}}}
    assert last(d(mod())) is None                                    # 建基线
    members["v"] = {**MEMBERS, "memberList": [m for m in MEMBERS["memberList"] if m["wxid"] != USER]
                    + [{"wxid": "wxid_noname", "nickName": ""}]}
    d(mod())                                                         # 新基线（多了无名者）
    members["v"] = {**MEMBERS, "memberList": [m for m in MEMBERS["memberList"] if m["wxid"] != USER]}
    t = last(d(mod()))
    assert t["reply"] == "wxid_noname 退群了，祝 TA 一路顺风！", t    # 无昵称 → wxid
    members["v"] = {**MEMBERS, "memberList": [m for m in MEMBERS["memberList"] if m["wxid"] not in (USER, ADMIN)]}
    t = last(d(mod()))
    assert t["reply"] == "管理 退群了，祝 TA 一路顺风！" and t["sent"] is True   # 已知昵称
    assert llm.calls == []                                           # 送别不调模型
    print("退群送别 ✅")


def test_gate():
    llm = MockLLM()
    r, st, sent, *_, d = setup(llm)
    # 非白名单群：不调模型（模型白名单 = 发送白名单）、只模拟
    t = last(d(in_room(JOIN(), OTHER)))
    assert t["sent"] is False and llm.calls == [] and "欢迎加入示例测试群" in t["reply"] and sent == []
    t = last(d(in_room(sysmsg('你将"$kickoutname$"移出了群聊', {"kickoutname": [("wxid_b", "Bob")]}), OTHER)))
    assert t["sent"] is False and sent == []
    # 观察群：即使被误加进白名单，也绝不真发
    t = last(d(in_room(JOIN(), OBS)))
    assert t["sent"] is False and sent == [] and r.outbox[-1].to_wxid == OBS
    # observe.json 里 allow_send=true 的观察群：不再硬拦，改由 send_policy 约束
    import json
    from app import observe_rooms, observe_policies
    op = Path(tempfile.mkdtemp()) / "observe.json"
    op.write_text(json.dumps({"rooms": {OBS: {"name": "示例大群", "allow_send": True}, OTHER: {"name": "只观察"}}}), encoding="utf-8")
    assert OBS not in observe_rooms(op) and OTHER in observe_rooms(op) and observe_policies(op)[OBS]["daily_reply_cap"] == 60
    print("发送闸 ✅")


if __name__ == "__main__":
    test_defaults_and_toggle(); test_welcome_at_and_persona(); test_welcome_fallback_template()
    test_farewell_template(); test_gate()
    print("全部通过 ✅")
