"""AI 日报 + 人设定制测试（simulate；--mock 不调真模型；不发微信、不打 gewe）。"""
import json, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import build_router
from features.daily_report import parse_json, report_text
from features.persona import PersonaStore
from features.settings import FeatureSettings
from models import normalize

BOT, ROOM, OTHER = "wxid_bot_demo", "20000000002@chatroom", "20000000005@chatroom"
OWNER, USER = "wxid_owner", "wxid_user"
YM = "wxid_owner_demo"   # 主人本人
MEMBERS = {"chatroomOwner": OWNER, "adminWxid": [], "memberList": [
    {"wxid": OWNER, "nickName": "示例公众号"}, {"wxid": USER, "nickName": "群友小A"}, {"wxid": "wxid_c", "nickName": "阿离"}]}
_mid = 0
BASE_TS = int(time.time()) - 3600


def raw(sender, text, at=False, room=ROOM, mt=1, dt=0):
    global _mid; _mid += 1
    src = f"<msgsource><atuserlist><![CDATA[{BOT}]]></atuserlist></msgsource>" if at else "<msgsource/>"
    return {"TypeName": "AddMsg", "Appid": "wx_sim", "Wxid": BOT, "Data": {
        "NewMsgId": 8000 + _mid, "MsgType": mt, "FromUserName": {"string": room}, "ToUserName": {"string": BOT},
        "Content": {"string": f"{sender}:\n{text}"}, "CreateTime": BASE_TS + dt, "MsgSource": src}}


AI_JSON = {"summary": "今天大家在聊周末去哪爬山，气氛轻松。",
           "topics": [{"topic": "周末爬山", "contributors": ["群友小A", "阿离"], "detail": "群友小A提议去香山，阿离担心人多，最后定了凤凰岭。"}],
           "titles": [{"name": "群友小A", "title": "龙王", "reason": "发言最多"}],
           "quotes": [{"content": "爬山不累，累的是排队", "sender": "阿离", "reason": "精辟"}]}


class MockLLM:
    def __init__(self): self.calls = []
    def chat(self, user, system=None, history=None, **kw):
        self.calls.append({"user": user, "system": system, "kw": kw})
        if "群聊日报" in user:
            return "```json\n" + json.dumps(AI_JSON, ensure_ascii=False) + "\n```"
        return "陪聊回复"


def make(tmp, llm, personas=None, settings=None):
    r = build_router(fetch_list=lambda r: MEMBERS, fetch_info=lambda r: {"nickName": "示例测试群"},
                        db_path=tmp / "s.db", llm=llm, settings=settings or FeatureSettings(None),
                        personas=personas or PersonaStore(tmp / "personas.json"),
                        allow_targets={ROOM}, send_enabled=False)
    r.reporter.out_dir = tmp / "reports"          # 测试图不落到 data/
    return r


def last(tr):
    return next((t for t in reversed(tr) if t.get("reply")), None)


def test_daily_report():
    tmp = Path(tempfile.mkdtemp()); llm = MockLLM(); st = FeatureSettings(None)
    r = make(tmp, llm, settings=st)
    d = lambda m: r.dispatch(normalize(m))
    chat = [(USER, "周末去爬山吧"), ("wxid_c", "去哪"), (USER, "香山？"), ("wxid_c", "人太多了"),
            (OWNER, "凤凰岭呢"), (USER, "可以！"), ("wxid_c", "爬山不累，累的是排队")]
    for i, (s, t) in enumerate(chat):
        d(raw(s, t, dt=i * 60))
    d(raw(USER, "<emoji/>", mt=47, dt=500))
    assert len(r.stats.texts(ROOM, r.stats.daily(ROOM, time.strftime('%Y-%m-%d'))["day"])) == 8

    assert last(d(raw(USER, "@小助手 日报", at=True))) is None, "默认关，不应触发"
    assert not any("群聊日报" in c["user"] for c in llm.calls)
    st.set(ROOM, "daily_report", True); st.set(OTHER, "daily_report", True)
    assert last(d(raw(USER, "@小助手 日报", at=True, room=OTHER))) is None          # 非白名单群：不调模型
    assert not any("群聊日报" in c["user"] for c in llm.calls)
    t = last(d(raw(USER, "@小助手\u2005日报", at=True)))
    assert t and t["feature"] == "daily_report", t
    reply = r.outbox[-1]
    assert reply.image_path and Path(reply.image_path).stat().st_size > 10000, reply.image_path
    assert "周末爬山" in reply.content and "龙王" in reply.content and "爬山不累" in reply.content
    p = [c for c in llm.calls if "群聊日报" in c["user"]][0]
    assert "[" in p["user"] and "群友小A: 周末去爬山吧" in p["user"] and "wxid_" not in p["user"].split("## 聊天记录")[1]
    print("日报文字版：\n" + reply.content)
    print("日报图：", reply.image_path)

    # 解析容错 + AI 失败降级
    assert parse_json('好的：{"summary":"x","topics":[]} 以上')["summary"] == "x"
    class Bad:
        def chat(self, *a, **k): return "不是json"
    r2 = make(Path(tempfile.mkdtemp()), Bad())
    for i, (s, t) in enumerate(chat):
        r2.dispatch(normalize(raw(s, t, dt=i)))
    rpt = r2.reporter.build(ROOM, r.stats.texts(ROOM, time.strftime('%Y-%m-%d')) and time.strftime('%Y-%m-%d'))
    assert rpt.get("ai_error") and "活跃榜" in report_text(rpt)

    # 异步模式：先回「正在整理」，后台 deliver
    got = []
    r3 = make(Path(tempfile.mkdtemp()), MockLLM(), settings=st)
    feat = next(f for f in r3.features if f.name == "daily_report")
    feat.async_mode, feat.deliver = True, got.append
    for i, (s, t) in enumerate(chat):
        r3.dispatch(normalize(raw(s, t, dt=i)))
    t = last(r3.dispatch(normalize(raw(USER, "@小助手 日报", at=True))))
    assert "正在整理" in t["reply"]
    for _ in range(100):
        if got: break
        time.sleep(0.1)
    assert got and got[0].image_path and got[0].to_wxid == ROOM
    print("日报 ✅")


def test_persona():
    tmp = Path(tempfile.mkdtemp()); llm = MockLLM()
    st = FeatureSettings(None); st.set(ROOM, "chat_companion", True)
    store = PersonaStore(tmp / "personas.json")
    r = make(tmp, llm, personas=store, settings=st)
    d = lambda m: r.dispatch(normalize(m))
    v = last(d(raw(USER, "@小助手 查看人设", at=True)))["reply"]
    assert "默认人设" in v and "小助手" in v
    d(raw(USER, "@小助手 你好", at=True))
    assert llm.calls[-1]["system"].startswith("你是「小助手」")
    # 普通群友、群主都不能改
    assert last(d(raw(USER, "@小助手 设置人设 你是猫娘", at=True)))["reply"] == "人设由主人统一管理"
    assert last(d(raw(OWNER, "@小助手 设置人设 你是猫娘", at=True)))["reply"] == "人设由主人统一管理"
    assert last(d(raw(OWNER, "@小助手 恢复默认人设", at=True)))["reply"] == "人设由主人统一管理"
    assert store.custom(ROOM) is None
    # 主人本人可以改
    out = last(d(raw(YM, "@小助手 设置人设 你是一只爱吐槽的猫娘，\n句尾带「喵」", at=True)))["reply"]
    assert "已更新本群人设" in out, out
    assert store.custom(ROOM) == "你是一只爱吐槽的猫娘，\n句尾带「喵」"
    d(raw(USER, "@小助手 在吗", at=True))
    assert llm.calls[-1]["system"].startswith("你是一只爱吐槽的猫娘"), llm.calls[-1]["system"]
    assert "自定义" in last(d(raw(USER, "@小助手 查看人设", at=True)))["reply"]   # 谁都能看
    saved = json.loads((tmp / "personas.json").read_text())
    assert saved["sessions"][ROOM]["by"] == YM and oct((tmp / "personas.json").stat().st_mode)[-3:] == "600"
    # 群主恢复默认被拒，人设保持
    t = last(d(raw(OWNER, "@小助手 恢复默认人设", at=True)))
    assert t["feature"] == "admin.persona" and t["reply"] == "人设由主人统一管理", t
    assert store.custom(ROOM) is not None
    # 重启后仍生效
    r_b = make(tmp, llm, personas=PersonaStore(tmp / "personas.json"), settings=st)
    r_b.dispatch(normalize(raw(USER, "@小助手 喵？", at=True)))
    assert llm.calls[-1]["system"].startswith("你是一只爱吐槽的猫娘")
    # 主人恢复默认（不能被功能开关指令截走）
    t = last(d(raw(YM, "@小助手 恢复默认人设", at=True)))
    assert t["feature"] == "admin.persona" and "恢复为默认" in t["reply"], t
    assert store.custom(ROOM) is None
    assert "请在「设置人设」后面写上" in last(d(raw(YM, "@小助手 设置人设", at=True)))["reply"]
    assert last(d(raw(YM, "设置人设 不@不算"))) is None
    # 功能开关仍按群主/管理员：群主能开关，普通群友不能；主人是默认超管也能
    assert "只有群主" in (last(d(raw(USER, "@小助手 关闭 签到", at=True)))["reply"] or ""), "普通人不应能关开关"
    assert st.enabled(ROOM, "sign_in") is True
    d(raw(OWNER, "@小助手 关闭 签到", at=True)); assert st.enabled(ROOM, "sign_in") is False
    d(raw(YM, "@小助手 开启 签到", at=True)); assert st.enabled(ROOM, "sign_in") is True
    # BOT_ADMIN_WXIDS 里的超管也不能改人设
    import os
    os.environ["BOT_ADMIN_WXIDS"] = USER
    try:
        assert last(d(raw(USER, "@小助手 设置人设 x", at=True)))["reply"] == "人设由主人统一管理"
        d(raw(USER, "@小助手 关闭 签到", at=True)); assert st.enabled(ROOM, "sign_in") is False   # 但开关可以
    finally:
        os.environ.pop("BOT_ADMIN_WXIDS")
    print("人设 ✅")


if __name__ == "__main__":
    import logging; logging.basicConfig(level=logging.WARNING)
    test_daily_report(); test_persona(); print("全部通过 ✅")
