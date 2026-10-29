"""进群欢迎语音测试（假发送：sender / voice_sender 都是假函数，不打 gewe、不调 TTS）。"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import build_router
from features.persona import PersonaStore
from features.settings import FeatureSettings
from features.owner_mute import OwnerMute
from features.welcome_voice import clean_name, compose_names, DEFAULT_TPL
from models import normalize
from test_admin_member_change import BOT, ROOM, OWNER, MEMBERS, sysmsg, last

OTHER = "11111111111@chatroom"
TPL = "{ats}欢迎{names}加入！在这里，我们一起学习、交流、用好 AI。"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t
    def __call__(self):
        return self.t


def setup(voice_ok=True, other_voice=False):
    tmp = Path(tempfile.mkdtemp())
    st = FeatureSettings(tmp / "settings.json")
    for room in (ROOM, OTHER):
        st.set(room, "welcome", True)
        st.room_set(room, "welcome_tpl", TPL)
    st.set(ROOM, "welcome_voice", True)
    if other_voice:
        st.set(OTHER, "welcome_voice", True)
    sent, voices = [], []

    def vsend(to, script):
        voices.append((to, script))
        return voice_ok
    r = build_router(fetch_list=lambda room: MEMBERS, fetch_info=lambda room: {"nickName": "示例AI交流群"},
                     db_path=tmp / "s.db", llm=None, settings=st, allow_targets={ROOM}, send_enabled=True,
                     sender=sent.append, personas=PersonaStore(tmp / "p.json", default="你是「小助手」"),
                     block_targets=set(), voice_sender=vsend)
    r.multi_async = False
    r.mute = OwnerMute(tmp / "owner_mute.json", owners=lambda: {OWNER})
    wv = r.welcome_voice
    clock = Clock(); wv.clock = clock
    timers = []
    wv.start_timer = lambda delay, fn: timers.append((delay, fn)) or object()
    fire = lambda: [fn() for _, fn in [timers.pop(0) for _ in range(len(timers))]]
    d = lambda m: r.dispatch(normalize(m))
    return r, st, sent, voices, timers, fire, clock, d


def join(*people, room=ROOM):
    m = sysmsg('"$username$"邀请"$names$"加入了群聊', {"username": [(OWNER, "示例公众号")], "names": list(people)})
    if room != ROOM:
        m["Data"]["FromUserName"]["string"] = room
        m["Data"]["Content"]["string"] = m["Data"]["Content"]["string"].replace(ROOM, room)
    return m


def ctx_texts(r):
    return [e.get("t", "") for e in r.context.entries(ROOM)]


def test_names():
    assert clean_name("🌙主人✨") == "主人"
    assert clean_name("Alice 🚀") == "Alice"
    assert clean_name("😀😀") == "新朋友"
    assert clean_name("一二三四五六七八九十一二三") == "新朋友"        # 13 字
    assert clean_name("【AI】张三~") == "AI张三"
    assert compose_names(["张三"]) == "张三"
    assert compose_names(["张三", "李四", "王五"]) == "张三、李四、王五"
    assert compose_names(["A", "B", "C", "D"]) == "A、B、C等几位新朋友"
    assert compose_names(["新朋友", "新朋友"]) == "几位新朋友"
    assert compose_names(["张三", "新朋友"]) == "张三等几位新朋友"
    print("名字清洗/合并 ✅")


def test_single():
    r, st, sent, voices, timers, fire, clock, d = setup()
    t = last(d(join(("wxid_new", "群友小A🔥"))))
    assert t["sent"] is True and t["reply"].startswith("@群友小A🔥 欢迎加入！在这里"), t   # 文字照旧，不改
    assert voices == [] and len(timers) == 1 and timers[0][0] == 10          # 文字先发，语音 10s 后
    fire()
    assert len(sent) == 1 and len(voices) == 1                               # 没有额外文字
    to, script = voices[0]
    assert to == ROOM and script == DEFAULT_TPL.replace("{name}", "群友小A"), script
    assert any(x.startswith("（发了条语音）欢迎群友小A加入示例AI交流群") for x in ctx_texts(r))
    print("单人进群 ✅", script)


def test_multi_and_gap():
    r, st, sent, voices, timers, fire, clock, d = setup()
    d(join(("wxid_a", "阿离"), ("wxid_b", "Bob😎")))
    d(join(("wxid_c", "🎉🎉")))                                               # 窗口内又进一个 → 不新开定时器
    assert len(timers) == 1 and len(sent) == 2
    fire()
    assert len(voices) == 1 and voices[0][1].startswith("欢迎阿离、Bob等几位新朋友加入"), voices
    clock.t += 20                                                             # 20s 后又来 → 等到上一条 +60s
    d(join(("wxid_d", "D"), ("wxid_e", "E"), ("wxid_f", "F"), ("wxid_g", "G")))
    assert abs(timers[0][0] - 40) < 1e-6, timers
    fire()
    assert voices[-1][1].startswith("欢迎D、E、F等几位新朋友加入"), voices[-1]
    print("多人合并 + 间隔 ✅", voices[0][1][:20], "|", voices[-1][1][:24])


def test_tts_fail():
    r, st, sent, voices, timers, fire, clock, d = setup(voice_ok=False)
    d(join(("wxid_new", "小王")))
    fire()
    assert len(voices) == 1 and len(sent) == 1                               # 失败：不回退文字、不重发欢迎文字
    assert not any("发了条语音" in x for x in ctx_texts(r))                    # 没发成功不记上下文
    print("TTS 失败只保留文字 ✅")


def test_other_room():
    r, st, sent, voices, timers, fire, clock, d = setup(other_voice=True)
    t = last(d(join(("wxid_new", "小王"), room=OTHER)))
    assert t["sent"] is False and timers == [] and voices == [] and sent == []   # 非白名单群：都不发
    st.set(ROOM, "welcome_voice", False)
    d(join(("wxid_x", "小李")))
    assert timers == [] and len(sent) == 1                                    # 本群开关关 → 只有文字
    print("其他群/开关关 不发语音 ✅")


def test_muted():
    r, st, sent, voices, timers, fire, clock, d = setup()
    r.mute.mute(ROOM, OWNER)
    trace = d(join(("wxid_new", "小王")))
    assert sent == [] and timers == [] and voices == []                       # 静音：文字、语音都不发（与现状一致）
    r.mute.unmute(ROOM, OWNER)
    d(join(("wxid_new2", "小张")))
    assert len(sent) == 1 and len(timers) == 1
    r.mute.mute(ROOM, OWNER)                                                  # 排队期间被静音 → 取消
    fire()
    assert voices == []
    print("静音 ✅")


def test_daily_cap():
    r, st, sent, voices, timers, fire, clock, d = setup()
    r.welcome_voice.daily_cap = 2
    for i in range(3):
        d(join((f"wxid_{i}", f"人{i}")))
        fire()
        clock.t += 61
    assert len(voices) == 2 and len(sent) == 3
    print("每日上限 ✅")


def test_custom_tpl():
    r, st, sent, voices, timers, fire, clock, d = setup()
    st.room_set(ROOM, "welcome_voice_tpl", "嗨{name}，欢迎！{不是占位}")
    d(join(("wxid_new", "小王"))); fire()
    assert voices[-1][1] == "嗨小王，欢迎！{不是占位}"
    print("群级模板 ✅")


if __name__ == "__main__":
    test_names(); test_single(); test_multi_and_gap(); test_tts_fail(); test_other_room(); test_muted()
    test_daily_cap(); test_custom_tpl()
    print("全部通过 ✅")
