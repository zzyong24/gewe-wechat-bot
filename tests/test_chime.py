"""主动插话测试（不发微信、不打 gewe；模型用 Mock；时钟可控）。"""
import random, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import build_router
from features.persona import PersonaStore
from features.settings import FeatureSettings
from models import normalize

BOT, ROOM, OTHER, OBS = "wxid_bot_demo", "20000000002@chatroom", "11111111111@chatroom", "20000000003@chatroom"
T0 = 1792504800.0          # 2026-10-20 22:00 (UTC+8)
_mid = 0


class Clock:
    def __init__(self): self.t = T0
    def __call__(self): return self.t


class MockLLM:
    def __init__(self, reply="哈哈火锅配冰粉才是灵魂，谁懂！"): self.calls, self.reply = [], reply
    def chat(self, user, system=None, history=None, **k):
        self.calls.append({"user": user, "system": system}); return self.reply


def raw(sender, text, clock, room=ROOM, at=None):
    global _mid; _mid += 1
    src = f"<msgsource><atuserlist><![CDATA[{','.join(at)}]]></atuserlist></msgsource>" if at else "<msgsource/>"
    return {"TypeName": "AddMsg", "Appid": "wx_sim", "Wxid": BOT, "Data": {
        "NewMsgId": 90000 + _mid, "MsgType": 1, "FromUserName": {"string": room}, "ToUserName": {"string": BOT},
        "Content": {"string": f"{sender}:\n{text}"}, "CreateTime": int(clock()), "MsgSource": src}}


def setup(llm=None, allow=(ROOM, OBS), llm_allow=None, on=(ROOM, OTHER, OBS), **opts):
    tmp = Path(tempfile.mkdtemp()); st = FeatureSettings(tmp / "settings.json")
    for room in on:
        st.set(room, "chime", True)
    clock, sent = Clock(), []
    o = dict(rng=random.Random(0), clock=clock, prob=1.0, cooldown_min=40, daily_max=3, min_speakers=3,
             window_min=5, min_msgs=6); o.update(opts)
    r = build_router(fetch_list=lambda room: {"memberList": []}, fetch_info=lambda room: {"nickName": "测试群"},
                     db_path=tmp / "s.db", llm=llm or MockLLM(), settings=st, allow_targets=set(allow),
                     send_enabled=True, sender=sent.append, personas=PersonaStore(tmp / "p.json", default="你是「小助手」"),
                     block_targets={OBS}, llm_allow=llm_allow, chime_opts=o)
    return r, st, clock, sent, tmp


def burst(r, clock, room=ROOM, speakers=("wxid_a", "wxid_b", "wxid_c"), n=6, step=20):
    """模拟一波聊天，返回 chime 的轨迹项列表。"""
    out = []
    for i in range(n):
        clock.t += step
        tr = r.dispatch(normalize(raw(speakers[i % len(speakers)], f"今晚吃火锅还是烧烤 {i}", clock, room)))
        out += [t for t in tr if t["feature"] == "chime" and t.get("reply")]
    return out


def test_trigger_and_context():
    llm = MockLLM()
    r, st, clock, sent, tmp = setup(llm)
    hits = burst(r, clock)
    assert len(hits) == 1 and hits[0]["sent"] is True and hits[0]["reply"] == "哈哈火锅配冰粉才是灵魂，谁懂！", hits
    assert sent[-1].to_wxid == ROOM and len(llm.calls) == 1
    assert "今晚吃火锅还是烧烤 5" in llm.calls[0]["user"] and llm.calls[0]["system"].startswith("你是「小助手」")
    assert "现在是北京时间" in llm.calls[0]["system"]                       # 当前时间注入
    assert r.chime.count_today(ROOM) == 1 and (tmp / "chime_state.json").exists()
    assert oct((tmp / "chime_state.json").stat().st_mode)[-3:] == "600"
    # 真人感：插话最多拆 2 条连发
    r, st, clock, sent, tmp = setup(MockLLM("哈哈火锅必须的\n冰粉安排上\n再来个第三条"))
    r.multi_async, r.sleep = False, lambda s: None
    hits = burst(r, clock)
    assert len(hits) == 1 and [s.content for s in sent] == ["哈哈火锅必须的", "冰粉安排上\n再来个第三条"], [s.content for s in sent]
    print("活跃触发 + 话题上下文 + 拆条 ✅")


def test_not_active_enough():
    llm = MockLLM()
    r, st, clock, sent, _ = setup(llm)
    assert burst(r, clock, speakers=("wxid_a", "wxid_b"), n=10) == [] and llm.calls == []     # 只有 2 人
    r, st, clock, sent, _ = setup(llm)
    assert burst(r, clock, n=5) == [] and llm.calls == []                                     # 3 人但只有 5 条
    r, st, clock, sent, _ = setup(llm)
    assert burst(r, clock, n=6, step=100) == [] and llm.calls == []                           # 太稀疏，窗口 5 分钟内凑不齐
    llm = MockLLM(); r, st, clock, sent, _ = setup(llm)
    clock.t += 1
    for i, w in enumerate(["wxid_a", "wxid_b", "wxid_c"] * 2):                               # @小助手 的不算（交给陪聊）
        r.dispatch(normalize(raw(w, f"@小助手 你好 {i}", clock, at=[BOT])))
    assert not any("最近的聊天记录" in c["user"] for c in llm.calls)
    print("活跃度不够不插 ✅")


def test_cooldown():
    llm = MockLLM()
    r, st, clock, sent, _ = setup(llm)
    assert len(burst(r, clock)) == 1
    assert burst(r, clock, n=12) == [] and len(llm.calls) == 1          # 冷却 40 分钟内不再插
    last_t = r.chime._state[ROOM]["last"]
    clock.t = last_t + 38 * 60                                         # 38~40 分钟之间这一波仍在冷却
    assert burst(r, clock, n=5) == []
    clock.t = last_t + 40 * 60
    assert len(burst(r, clock)) == 1 and len(llm.calls) == 2           # 过了冷却又可以
    # SKIP 也进入冷却、不发
    r, st, clock, sent, _ = setup(MockLLM("SKIP"))
    assert burst(r, clock) == [] and sent == [] and r.chime.why_not(ROOM, clock()) == "冷却中"
    print("冷却生效 ✅")


def test_daily_cap():
    llm = MockLLM()
    r, st, clock, sent, tmp = setup(llm, daily_max=3, cooldown_min=1)
    n = 0
    for _ in range(6):
        n += len(burst(r, clock)); clock.t += 120
    assert n == 3 and r.chime.count_today(ROOM) == 3 and r.chime.why_not(ROOM, clock()) == "达日上限"
    # 重启后上限仍在（状态持久化）
    r2, *_ = setup(llm, daily_max=3, cooldown_min=1)
    from features.chime import ChimeFeature
    c = ChimeFeature(lambda room: llm, lambda room: "", state_path=tmp / "chime_state.json", clock=clock, daily_max=3)
    assert c.count_today(ROOM) == 3
    clock.t += 24 * 3600                                               # 第二天清零
    assert c.count_today(ROOM) == 0
    print("日上限 ✅")


def test_gate_and_toggle():
    llm = MockLLM()
    # 默认关
    assert FeatureSettings(None).enabled(ROOM, "chime") is False
    r, st, clock, sent, _ = setup(llm, on=())
    assert burst(r, clock) == [] and llm.calls == []
    # 非白名单群：默认模型白名单 = 发送白名单 → 不调模型、不发
    r, st, clock, sent, _ = setup(llm)
    assert burst(r, clock, room=OTHER) == [] and llm.calls == [] and sent == []
    # 即使放开模型白名单，非发送白名单群也只模拟
    r, st, clock, sent, _ = setup(llm, llm_allow=lambda s: True)
    hits = burst(r, clock, room=OTHER)
    assert len(hits) == 1 and hits[0]["sent"] is False and sent == [] and r.outbox[-1].to_wxid == OTHER
    # 观察群：即使在发送白名单里，也直接跳过（连模型都不调）
    llm2 = MockLLM()
    r, st, clock, sent, _ = setup(llm2, llm_allow=lambda s: True)
    assert burst(r, clock, room=OBS) == [] and llm2.calls == [] and sent == []
    print("开关 + 发送闸 ✅")


if __name__ == "__main__":
    test_trigger_and_context(); test_not_active_enough(); test_cooldown(); test_daily_cap(); test_gate_and_toggle()
    print("全部通过 ✅")
