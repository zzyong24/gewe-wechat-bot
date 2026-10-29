"""真人感：多条拆分 + 逐条连发 + 发送闸 + 人设权限不变；朋友圈函数 mock。不联网、不发消息。"""
import os, random, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import build_router
from features import humanlike as H
from features.persona import PersonaStore
from features.settings import FeatureSettings
from models import normalize

BOT, ROOM, OTHER = "wxid_bot_demo", "20000000002@chatroom", "11111111111@chatroom"
YM, OWNER, USER = "wxid_owner_demo", "wxid_owner", "wxid_user"
_mid = 0


def at(sender, text, room=ROOM):
    global _mid; _mid += 1
    return {"TypeName": "AddMsg", "Appid": "wx_sim", "Wxid": BOT, "Data": {
        "NewMsgId": 50000 + _mid, "MsgType": 1, "FromUserName": {"string": room}, "ToUserName": {"string": BOT},
        "Content": {"string": f"{sender}:\n@小助手\u2005{text}"}, "CreateTime": 1792493289,
        "MsgSource": f"<msgsource><atuserlist><![CDATA[{BOT}]]></atuserlist></msgsource>"}}


class Mock:
    def __init__(self, reply): self.reply, self.calls = reply, []
    def chat(self, user, system=None, history=None, **k):
        self.calls.append({"user": user, "system": system}); return self.reply


def make(llm, allow=(ROOM,)):
    tmp = Path(tempfile.mkdtemp()); st = FeatureSettings(tmp / "s.json")
    for r in (ROOM, OTHER):
        st.set(r, "chat_companion", True)
    sent, sleeps = [], []
    r = build_router(fetch_list=lambda x: {"chatroomOwner": OWNER, "memberList": []}, fetch_info=lambda x: {},
                     sender=sent.append, send_enabled=True, db_path=tmp / "t.db", llm=llm, settings=st,
                     allow_targets=set(allow), llm_allow=lambda s: True,
                     personas=PersonaStore(tmp / "p.json"), block_targets=set())
    r.multi_async, r.sleep = False, sleeps.append
    return r, sent, sleeps, (lambda m: r.dispatch(normalize(m)))


def test_split():
    assert H.split_messages("哈哈确实\n先把余额用完\n\n再说。") == ["哈哈确实", "先把余额用完", "再说"]
    assert H.split_messages("一\n二\n三\n四\n五\n六", n_max=4) == ["一", "二", "三", "四\n五\n六"]   # 10-22：尾巴换行合并，不粘连
    assert H.split_messages("一\n二\n三\n四") == ["一", "二", "三\n四"]                 # 默认最多 3 条
    assert H.gap_range() == (0.8, 2.0)
    long = "这个我试过，效果还行。不过价格有点贵！你可以先薅免费额度？"
    assert H.split_messages(long, prob=1.0) == ["这个我试过，效果还行", "不过价格有点贵！", "你可以先薅免费额度？"]
    assert H.split_messages(long, prob=0.0) == [long.rstrip("。")]
    assert H.split_messages("1. 先注册\n2. 发帖") == ["先注册", "发帖"]
    assert H.split_messages("好的") == ["好的"] and H.split_messages("  ") == []
    os.environ["BOT_HUMANLIKE"] = "0"
    try:
        assert H.split_messages("a\nb") == ["a\nb"] and H.style_prompt() == ""
    finally:
        os.environ.pop("BOT_HUMANLIKE")
    os.environ["BOT_MULTI_MAX"] = "2"
    try:
        assert H.split_messages("一\n二\n三") == ["一", "二\n三"] and "1~2 条" in H.style_prompt()
    finally:
        os.environ.pop("BOT_MULTI_MAX")
    print("拆分 ✅")


def test_multi_send_and_style():
    llm = Mock("哈哈这个我熟\n先把 Cursor 余额用完\n再决定要不要换")
    r, sent, sleeps, d = make(llm)
    t = [x for x in d(at(USER, "要不要换工具")) if x.get("reply")][-1]
    assert t["sent"] is True and [s.content for s in sent] == ["哈哈这个我熟", "先把 Cursor 余额用完", "再决定要不要换"]
    assert all(s.to_wxid == ROOM for s in sent) and len(sleeps) == 2 and all(0.8 <= g <= 2 for g in sleeps), sleeps   # 默认 0.8–2 秒
    sys_prompt = llm.calls[-1]["system"]
    assert "底层说话风格" in sys_prompt and "是不是机器人" in sys_prompt and sys_prompt.startswith("你是「小助手」")
    # 间隔可配
    os.environ["BOT_MULTI_GAP_MIN"], os.environ["BOT_MULTI_GAP_MAX"] = "7", "8"
    try:
        sleeps.clear(); d(at(USER, "再说一次"))
        assert len(sleeps) == 2 and all(7 <= g <= 8 for g in sleeps), sleeps
    finally:
        os.environ.pop("BOT_MULTI_GAP_MIN"); os.environ.pop("BOT_MULTI_GAP_MAX")
    # 单条不拆、不 sleep
    llm.reply = "嗯嗯"; sent.clear(); sleeps.clear()
    d(at(USER, "在吗"))
    assert [s.content for s in sent] == ["嗯嗯"] and sleeps == []
    # 异步模式：后台线程发完
    import threading, time
    r.multi_async, r.sleep = True, lambda s: None
    llm.reply = "一\n二"; sent.clear(); d(at(USER, "异步"))
    for _ in range(50):
        if len(sent) == 2: break
        time.sleep(0.02)
    assert [s.content for s in sent] == ["一", "二"]
    print("逐条连发 + 风格提示 ✅")


class StreamMock(Mock):
    """模拟流式：首行先到，剩余稍后到；记录每块产出时 sent 的状态，验证首句是提前发的。"""
    def __init__(self, chunks, sent):
        super().__init__("".join(chunks)); self.chunks, self.sent, self.seen = chunks, sent, []
    def chat_stream(self, user, system=None, history=None, **k):
        self.calls.append({"user": user, "system": system, "stream": True})
        for c in self.chunks:
            self.seen.append(len(self.sent)); yield c


def test_stream_first_line_early():
    sent = []
    llm = StreamMock(["哈哈这个", "我熟\n", "先把余额", "用完\n再说"], sent)
    r, sent_list, sleeps, d = make(llm)
    llm.sent = sent_list
    t = [x for x in d(at(USER, "要不要换工具")) if x.get("reply")][-1]
    assert [s.content for s in sent_list] == ["哈哈这个我熟", "先把余额用完", "再说"], [s.content for s in sent_list]
    assert llm.seen == [0, 0, 1, 1]                      # 首行一到（第 3 块之前）就已发出第一条
    assert t["reply"] == "先把余额用完\n再说" and t["sent"] is True
    assert len(sleeps) == 2 and all(0 <= g <= 2 for g in sleeps), sleeps       # 第一段剩余前补足间隔 + 条间
    assert "stream" in llm.calls[-1]
    # 只有一行：不提前发、整条正常返回
    llm2 = StreamMock(["好的", "呀"], sent_list); r.chat._llm = llm2; sent_list.clear()
    d(at(USER, "在吗")); assert [s.content for s in sent_list] == ["好的呀"]
    # 非白名单群：首句也只进 outbox，不真发
    llm3 = StreamMock(["第一句\n", "第二句"], sent_list); r.chat._llm = llm3; sent_list.clear()
    d(at(USER, "你好", room=OTHER))
    assert sent_list == [] and [o.content for o in r.outbox[-2:]] == ["第一句", "第二句"]
    # 流式报错（首句前）→ 回退普通调用
    class Bad(Mock):
        def chat_stream(self, *a, **k):
            raise RuntimeError("stream down"); yield ""
    r.chat._llm = Bad("回退成功"); sent_list.clear(); d(at(USER, "再试"))
    assert [s.content for s in sent_list] == ["回退成功"]
    print("流式首句提前发 ✅")


def test_history_trim():
    from features.chat_companion import ChatCompanionFeature
    c = ChatCompanionFeature(Mock("x"))
    for i in range(10):
        c._remember("s", "问" * 300, "答" * 300)
    h = c._history("s")
    assert sum(len(m["content"]) for m in h) <= 1800 and h[-1]["content"].startswith("答")   # 字数上限，保留最新
    print("上下文裁剪 ✅")


def test_member_cache_swr():
    import time as _t
    from app import MemberCache
    calls = []
    def fetch(room):
        calls.append(room); _t.sleep(0.3)
        return {"memberList": [{"wxid": "w1", "nickName": f"n{len(calls)}"}]}
    c = MemberCache(fetch, ttl=0)
    assert c.name("r@chatroom", "w1") == "n1" and len(calls) == 1           # 第一次只能同步拉
    t0 = _t.time(); assert c.name("r@chatroom", "w1") == "n1"                # 过期：立即返回旧值
    assert _t.time() - t0 < 0.1
    for _ in range(50):
        if len(calls) == 2 and c._members["r@chatroom"][1].get("w1") == "n2": break
        _t.sleep(0.02)
    assert c._members["r@chatroom"][1]["w1"] == "n2"                         # 后台已刷新
    print("成员缓存不阻塞 ✅")


def test_gate():
    llm = Mock("第一条\n第二条")
    r, sent, sleeps, d = make(llm)
    t = [x for x in d(at(USER, "你好", room=OTHER)) if x.get("reply")][-1]   # 非白名单群（模型放开）
    assert t["sent"] is False and sent == [] and [o.content for o in r.outbox[-2:]] == ["第一条", "第二条"]
    r.block_targets = {ROOM}                                                   # 观察群：在白名单也不发
    t = [x for x in d(at(USER, "你好")) if x.get("reply")][-1]
    assert t["sent"] is False and sent == []
    print("发送闸 ✅")


def test_persona_permission_unchanged():
    llm = Mock("好")
    r, sent, sleeps, d = make(llm)
    reply = lambda m: [x for x in d(m) if x.get("reply")][-1]["reply"]
    assert reply(at(OWNER, "设置人设 你是猫娘")) == "人设由主人统一管理"
    assert reply(at(USER, "恢复默认人设")) == "人设由主人统一管理"
    assert "已更新本群人设" in reply(at(YM, "设置人设 你是猫娘，说话带喵"))
    d(at(USER, "喵"))
    s = llm.calls[-1]["system"]
    assert s.startswith("你是猫娘，说话带喵") and "底层说话风格" in s and "以人设为准" in s
    print("人设权限不变 + 自定义人设叠加底层风格 ✅")


def test_sns_mock():
    import gewe_client as g
    calls = []
    orig = g.call
    g.call = lambda path, body=None, timeout=0: calls.append((path, body)) or {"id": 1, "userName": BOT}
    try:
        out = g.post_text_sns("今天天气不错", at_wxids=[YM])
        assert calls[-1][0] == "/gewe/v2/api/sns/sendTextSns" and calls[-1][1]["content"] == "今天天气不错"
        assert calls[-1][1]["atWxIds"] == [YM] and calls[-1][1]["privacy"] is False and out["id"] == 1
        try:
            g.post_text_sns("  "); raise SystemExit("空内容应报错")
        except ValueError:
            pass
        g.contacts_sns_list("wxid_x")
        assert calls[-1] == ("/gewe/v2/api/sns/contactsSnsList", {"wxid": "wxid_x", "maxId": 0, "firstPageMd5": "", "decrypt": True})
    finally:
        g.call = orig
    print("朋友圈函数（mock）✅")


if __name__ == "__main__":
    random.seed(1)
    test_split(); test_multi_send_and_style(); test_stream_first_line_early(); test_history_trim(); test_member_cache_swr()
    test_gate(); test_persona_permission_unchanged(); test_sns_mock()
    print("全部通过 ✅")
