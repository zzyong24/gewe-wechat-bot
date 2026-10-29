"""测试公用：假群、构造消息、建 router（全部 simulate，不打 gewe、不调真模型）。"""
import random, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import build_router
from features.settings import FeatureSettings, FEATURES
from features.persona import PersonaStore
from models import normalize

BOT, ROOM = "wxid_bot_demo", "20000000002@chatroom"
NAMES = {"wxid_owner": "主人", "wxid_a": "阿离", "wxid_b": "Bob", "wxid_c": "小C", "wxid_d": "大熊", "wxid_e": "小鹿",
         BOT: "小助手"}
MEMBERS = {"chatroomOwner": "wxid_owner", "adminWxid": [],
           "memberList": [{"wxid": w, "nickName": n} for w, n in NAMES.items()]}
_mid = [0]


def raw(sender, text, at=(), room=ROOM, mt=1, ts=None):
    _mid[0] += 1
    ats = list(at)
    src = f"<msgsource><atuserlist><![CDATA[{','.join(ats)}]]></atuserlist></msgsource>" if ats else "<msgsource/>"
    return {"TypeName": "AddMsg", "Appid": "wx_sim", "Wxid": BOT, "Data": {
        "NewMsgId": 100000 + _mid[0], "MsgType": mt, "FromUserName": {"string": room},
        "ToUserName": {"string": BOT}, "Content": {"string": f"{sender}:\n{text}"},
        "CreateTime": int(ts or time.time()), "MsgSource": src}}


def private(sender, text):
    _mid[0] += 1
    return {"TypeName": "AddMsg", "Appid": "wx_sim", "Wxid": BOT, "Data": {
        "NewMsgId": 100000 + _mid[0], "MsgType": 1, "FromUserName": {"string": sender},
        "ToUserName": {"string": BOT}, "Content": {"string": text}, "CreateTime": int(time.time())}}


class _NoNet:
    def get(self, *a, **k):
        import requests
        raise requests.ConnectionError("tests: 高德已断网")


def offline_amap():
    """测试默认不联网：高德客户端的 session 一调就报网络错误（功能会回友好提示）。"""
    import amap_client
    return amap_client.AmapClient(amap_client.Quota(None), session=_NoNet())


def all_on():
    s = FeatureSettings(None)
    for k in FEATURES:
        s.set(ROOM, k, True)
    return s


class Bot:
    def __init__(self, llm=None, settings=None, seed=1, allow=(ROOM,), word_link=lambda w, t: f"https://img.example/{t}"):
        self.tmp = Path(tempfile.mkdtemp())
        self.r = build_router(fetch_list=lambda room: MEMBERS, fetch_info=lambda room: {"nickName": "测试群"},
                              db_path=self.tmp / "s.db", llm=llm or Echo(), settings=settings or all_on(),
                              personas=PersonaStore(None), allow_targets=set(allow), send_enabled=False,
                              rng=random.Random(seed), word_link=word_link, amap=offline_amap())

    def say(self, sender, text, at_bot=True, also_at=(), **kw):
        body = (f"@小助手\u2005{text}" if at_bot else text)
        trace = self.r.dispatch(normalize(raw(sender, body, ([BOT] if at_bot else []) + list(also_at), **kw)))
        rs = [t for t in trace if t.get("reply")]
        return rs[-1]["reply"] if rs else None

    def last_reply(self):
        return self.r.outbox[-1]


class Echo:
    def __init__(self): self.calls = []
    def chat(self, user, system=None, history=None, **kw):
        self.calls.append(user); return "陪聊"
