"""追问窗口 / 不抢别人的话 / 引用视同 @ / 地点问句主动搜 / 陪聊工具调用（全 mock，不联网、不发消息）。"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import os
os.environ["AMAP_KEY"] = "FAKEKEY_for_test_123"
import amap_client as A
from features.settings import FeatureSettings
from features.followup import Followup, parse_quote
from features.chat_companion import ChatCompanionFeature
from tests.helpers import Bot, BOT, ROOM, raw
from tests.amap_fakes import FakeSession, Resp
from models import normalize


def settings(*keys):
    s = FeatureSettings(None)
    for k in keys:
        s.set(ROOM, k, True)
    return s


class Clock:
    t = 1000.0
    def __call__(self): return self.t


class LLM:
    """记录每次收到的 user，回一句反问。"""
    def __init__(self): self.users = []
    def chat(self, user, system=None, history=None, **kw):
        self.users.append(user); return "你说的是哪个城市呀？"


def make(keys=("chat_companion",), llm=None, sec=180, max_n=None):
    b = Bot(llm=llm or LLM(), settings=settings(*keys))
    clk = Clock()
    b.r.followup = Followup(sec=sec, max_n=max_n, now=clk)
    return b, clk


def quote_raw(sender, title, quoted_from, quoted_text="你说的是哪个城市呀？", rtype=1, at=(), room=ROOM):
    xml = (f'<?xml version="1.0"?>\n<msg><appmsg><title>{title}</title><type>57</type><refermsg><type>{rtype}</type>'
           f'<svrid>1</svrid><fromusr>{room}</fromusr><chatusr>{quoted_from}</chatusr><displayname>x</displayname>'
           f'<content>{quoted_text}</content></refermsg></appmsg><fromusername>{sender}</fromusername></msg>')
    return raw(sender, xml, at=at, mt=49, room=room)


# ---- 引用解析 ----
q = parse_quote('<?xml version="1.0"?><msg><appmsg><title>武汉的</title><type>57</type><refermsg><type>1</type>'
                f'<chatusr>{BOT}</chatusr><content>哪个城市？</content></refermsg></appmsg></msg>')
assert q[:3] == ("武汉的", "哪个城市？", BOT)
assert parse_quote("hello") is None and parse_quote("<msg><appmsg><type>5</type></appmsg></msg>") is None
print("引用解析 ✅")

# ---- 追问窗口：同一个人、窗口内、不 @ 也接，带前文 ----
llm = LLM()
b, clk = make(llm=llm, max_n=2)
assert b.say("wxid_owner", "咕噜猫馆好玩吗") == "你说的是哪个城市呀？"
clk.t += 30
assert b.say("wxid_owner", "武汉的", at_bot=False) == "你说的是哪个城市呀？"            # 没 @ 也接
assert llm.users[-1] == "[主人]：武汉的"
hist = b.r.chat._history(ROOM)
assert any("咕噜猫馆" in m["content"] for m in hist)                                    # 前文在上下文里
# 别人说话不接
assert b.say("wxid_a", "我也想去", at_bot=False) is None
# 窗口上限：再接 1 条（max_n=2），第 3 条不接
assert b.say("wxid_owner", "洪山区那家", at_bot=False) is not None
assert b.say("wxid_owner", "还有别的吗", at_bot=False) is None
# 重新 @ → 计数重置
assert b.say("wxid_owner", "那汉口呢") is not None
assert b.say("wxid_owner", "好的谢谢", at_bot=False) is not None
# 窗口过期
clk.t += 181
assert b.say("wxid_owner", "在吗", at_bot=False) is None
print("追问窗口 ✅")

# ---- 不抢话：@ 了别人 / 引用别人 → 不接，并关窗口 ----
b, clk = make()
b.say("wxid_owner", "推荐个电影")
assert b.say("wxid_owner", "你看过吗", at_bot=False, also_at=["wxid_a"]) is None          # @别人
assert b.say("wxid_owner", "那就这个", at_bot=False) is None                               # 窗口已关
b.say("wxid_owner", "再推荐一个")
tr = b.r.dispatch(normalize(quote_raw("wxid_owner", "哈哈确实", "wxid_a", "我昨天看了沙丘")))
assert not any(t.get("reply") for t in tr)                                               # 引用别人 + 不像对小助手说 → 不接
assert b.say("wxid_owner", "嗯", at_bot=False) is None
# 被动/旧消息不消耗窗口
b.say("wxid_owner", "第三次")
b.r.dispatch(normalize(raw("wxid_owner", "旧的", [])), passive_only=True)
assert b.r.followup.win[(ROOM, "wxid_owner")]["used"] == 0
print("不抢别人的话 ✅")

# ---- 窗口内引用别人 + 对小助手下指令 → 接（15:40 「去这里查」场景）----
YM = "wxid_owner_demo"          # 主人本人（bot_owners）
llm = LLM()
b, clk = make(llm=llm)
assert b.r.followup.max_n == 20                                                         # 默认上限 20（安全阀）
b.r.followup.max_n = 4
b.say(YM, "呼噜猫馆是不是黑店")
tr = b.r.dispatch(normalize(quote_raw(YM, "去这里查", "wxid_a", "美团抖音都有")))
assert [t["feature"] for t in tr if t.get("reply")] == ["chat_companion"]
assert llm.users[-1].endswith("（引用了x的「美团抖音都有」）去这里查"), llm.users[-1]
# 主人引用别人、不是祈使句也接（窗口内）
tr = b.r.dispatch(normalize(quote_raw(YM, "嗯这个说得对", "wxid_a", "看评价")))
assert any(t.get("reply") for t in tr)
# 非主人：祈使句接，闲聊不接
b.say("wxid_c", "呼噜猫馆在哪")
tr = b.r.dispatch(normalize(quote_raw("wxid_c", "你看看这个", "wxid_a", "大众点评有")))
assert any(t.get("reply") for t in tr)
tr = b.r.dispatch(normalize(quote_raw("wxid_c", "哈哈哈笑死", "wxid_a", "黑店吧")))
assert not any(t.get("reply") for t in tr)
# 不在窗口内：主人引用别人也不接
tr = b.r.dispatch(normalize(quote_raw("wxid_b", "去这里查", "wxid_a", "美团有")))
assert not any(t.get("reply") for t in tr)
# 引用别人但 @ 了别人 → 不接
b.say(YM, "再问一下")
tr = b.r.dispatch(normalize(quote_raw(YM, "你看看", "wxid_a", "x", at=["wxid_a"])))
assert not any(t.get("reply") for t in tr)
print("窗口内引用别人+指令 ✅")

# ---- 引用小助手 = @ ----
llm = LLM()
b, clk = make(llm=llm)
tr = b.r.dispatch(normalize(quote_raw("wxid_c", "武汉的", BOT)))
assert [t["feature"] for t in tr if t.get("reply")] == ["chat_companion"]
assert llm.users[-1] == "[小C]：（回复你刚才说的「你说的是哪个城市呀？」）武汉的", llm.users[-1]
# 引用小助手的图片
tr = b.r.dispatch(normalize(quote_raw("wxid_c", "这是啥", BOT, rtype=3)))
assert "（回复你刚才说的「[图片]」）这是啥" in llm.users[-1]
# 没开陪聊的群：引用也只是转成文本，不会乱回
b2, _ = make(keys=())
tr = b2.r.dispatch(normalize(quote_raw("wxid_c", "武汉的", BOT)))
assert not any(t.get("reply") for t in tr)
print("引用视同 @ ✅")


# ---- 地点问句：先查再问（规则层，不调模型），「武汉的」不 @ 接上 ----
class PlaceSession(FakeSession):
    def get(self, url, params=None, timeout=None):
        path = url.split("/v3", 1)[1]
        if path == "/place/text" and "咕噜" in params["keywords"]:
            self.calls.append((path, dict(params)))
            ok = {"status": "1", "info": "OK", "infocode": "10000"}
            if params.get("city") in ("武汉市", "武汉"):
                assert params["citylimit"] == "true"
                return Resp({**ok, "pois": [{"name": "咕噜噜猫舍·幼猫专售(和成中心店)", "address": "雄楚大道260号", "cityname": "武汉市", "adname": "洪山区", "location": "114.3,30.5"},
                                            {"name": "Gulu咕噜噜喵猫咪寄养", "address": "白沙三路48号", "cityname": "武汉市", "adname": "洪山区", "location": "114.3,30.5"}]})
            return Resp({**ok, "pois": [{"name": "咕噜猫馆(三里屯店)", "address": "工体北路8号", "cityname": "北京市", "adname": "朝阳区", "location": "116.4,39.9"},
                                        {"name": "咕噜猫馆", "address": "解放大道1号", "cityname": "武汉市", "adname": "江岸区", "location": "114.3,30.6"}]})
        if path == "/geocode/geo" and params["address"] in ("武汉", "好的"):
            self.calls.append((path, dict(params)))
            ok = {"status": "1", "info": "OK", "infocode": "10000"}
            if params["address"] == "好的":
                return Resp({**ok, "geocodes": []})
            return Resp({**ok, "geocodes": [{"formatted_address": "湖北省武汉市", "location": "114.3,30.6", "adcode": "420100",
                                            "citycode": "027", "city": "武汉市", "province": "湖北省", "level": "市"}]})
        return super().get(url, params, timeout)

llm = LLM()
b, clk = make(keys=("chat_companion", "amap"), llm=llm)
sess = PlaceSession()
b.r.amap.c = A.AmapClient(A.Quota(None), session=sess)
out = b.say("wxid_owner", "咕噜猫馆在哪里")
lines = out.split("\n")
assert lines[0] == "搜到这几个「咕噜猫馆」：" and lines[1].startswith("1. 咕噜猫馆(三里屯店) · 北京市朝阳区") and "武汉市江岸区" in lines[2], out
assert "哪个城市" in lines[-1] and llm.users == []                     # 先查再问；没调模型
out = b.say("wxid_owner", "武汉的", at_bot=False)                         # 不 @ 也接上，直接在武汉搜
assert out.split("\n")[0] == "武汉没有完全叫「咕噜猫馆」的，名字相近的有：" and "洪山区" in out, out
assert sess.calls[-1][1]["city"] == "武汉市" and llm.users == []
# 一句话里带城市
out = b.say("wxid_a", "武汉的咕噜猫馆在哪")
assert out.startswith("武汉没有完全叫「咕噜猫馆」的")
# 「好的」不当城市
b.say("wxid_b", "咕噜猫馆在哪里")
assert b.say("wxid_b", "好的", at_bot=False) == "你说的是哪个城市呀？"   # 交给陪聊，不去搜「好的」
# XX怎么去：没位置 → 先告诉在哪 + 提示发位置
out = b.say("wxid_c", "咕噜猫馆怎么去")
assert out.startswith("搜到这几个「咕噜猫馆」") and "发个位置给我" in out
print("地点问句主动搜索 ✅")


# ---- 陪聊工具调用（MiniMax function calling，mock）----
class ToolLLM:
    def __init__(self): self.rounds = []
    def chat(self, user, system=None, history=None, **kw): return "普通回复"
    def complete_raw(self, messages, **kw):
        self.rounds.append((messages, kw))
        if len(self.rounds) == 1:
            assert "能查的先查" in messages[0]["content"] and kw.get("tools")
            return {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "search_place", "arguments": json.dumps({"keywords": "咕噜猫馆", "city": "武汉"})}}]}
        tool_msg = messages[-1]
        assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1" and "洪山区" in tool_msg["content"]
        return {"content": "武汉没有完全同名的，最像的是洪山区的咕噜噜猫舍～"}

tl = ToolLLM()
b, clk = make(keys=("chat_companion", "amap"), llm=tl)
b.r.amap.c = A.AmapClient(A.Quota(None), session=PlaceSession())
out = b.say("wxid_owner", "武汉有没有咕噜猫馆这家店，靠谱吗")          # 规则层不吃（不是「在哪」句式）→ 模型调工具
assert out == "武汉没有完全同名的，最像的是洪山区的咕噜噜猫舍～" and len(tl.rounds) == 2, out
# 闲聊不带工具（不多花一轮）
tl2 = ToolLLM(); b2, _ = make(keys=("chat_companion", "amap"), llm=tl2)
assert b2.say("wxid_owner", "讲个笑话") == "普通回复" and tl2.rounds == []
# 本群没开 amap → 不给工具
tl3 = ToolLLM(); b3, _ = make(keys=("chat_companion",), llm=tl3)
assert b3.say("wxid_owner", "武汉有没有咕噜猫馆这家店") == "普通回复" and tl3.rounds == []
assert ChatCompanionFeature._needs_tools("武汉的", [{"role": "user", "content": "[主人]：咕噜猫馆好玩吗"}])
assert not ChatCompanionFeature._needs_tools("哈哈哈", [{"role": "user", "content": "[主人]：讲个笑话"}])
print("陪聊工具调用 ✅")
print("test_followup 全部通过")
