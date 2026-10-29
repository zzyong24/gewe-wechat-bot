"""高德地图功能（全部 mock：假 session，不联网、不发消息）。"""
import os, sys, tempfile, logging, io
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["AMAP_KEY"] = "FAKEKEY_for_test_123"           # 只在本进程里，验证脱敏
import requests
import amap_client as A
from features.amap import AmapFeature, fmt_dist, fmt_dur
from tests.helpers import Bot, BOT, ROOM, raw, all_on
from models import normalize

from tests.amap_fakes import LOC_XML, Resp, FakeSession


def make(limits=None, room_on=True, allow=(ROOM,)):
    sess = FakeSession()
    client = A.AmapClient(A.Quota(Path(tempfile.mkdtemp()) / "q.json", limits), session=sess)
    st = all_on() if room_on else None
    b = Bot(settings=st, allow=allow)
    # 用 mock 客户端替换 build_router 默认注入的离线客户端
    b.r.amap.c = client
    return b, sess


# ---- 小工具 ----
assert fmt_dist(153) == "153米" and fmt_dist(14200) == "14.2公里"
assert fmt_dur(2040) == "34分钟" and fmt_dur(4380) == "1小时13分钟" and fmt_dur(3600) == "1小时"

# ---- 句式解析 ----
f = AmapFeature(A.AmapClient(A.Quota(None), session=FakeSession()))
P = f.parse
assert P("北京天气") == ("weather", "北京") and P("上海明天天气怎么样") == ("weather", "上海")
assert P("北京的天气") == ("weather", "北京") and P("天气") == ("weather", "") and P("天气 广州") == ("weather", "广州")
assert P("今天天气真好") is None and P("天气好热") is None
assert P("望京SOHO附近的咖啡") == ("nearby", "望京SOHO", "咖啡") and P("附近的奶茶") == ("nearby", "", "奶茶")
assert P("我附近有什么好吃的") == ("nearby", "我", "好吃的") and P("我家附近好吵") is None
assert P("从国贸到望京怎么走") == ("route", "国贸", "望京", None)
assert P("西直门到动物园怎么去") == ("route", "西直门", "动物园", None)
assert P("从国贸到望京开车怎么走") == ("route", "国贸", "望京", "driving")
assert P("从西直门到动物园步行怎么走") == ("route", "西直门", "动物园", "walking")
assert P("从我这到天安门") == ("route", "我这", "天安门", None)
assert P("你好呀") is None and P("我到家了") is None
print("句式解析 ✅")

# ---- 天气 ----
b, s = make()
out = b.say("wxid_a", "北京天气")
assert out.startswith("北京现在晴，15℃，西南风4级，湿度21%") and "明天晴转多云 4~16℃" in out, out
assert [c[0] for c in s.calls] == ["/geocode/geo", "/weather/weatherInfo", "/weather/weatherInfo"]
out = b.say("wxid_a", "望京天气")          # 非行政区 → 搜地点 + 逆地理取 adcode
assert out.startswith("望京现在晴") and "/geocode/regeo" in [c[0] for c in s.calls]
out = b.say("wxid_a", "天气")
assert "想查哪儿的天气" in out
# 前缀触发（不 @）
assert b.say("wxid_a", "小助手 上海天气", at_bot=False).startswith("上海现在晴")
assert b.say("wxid_d", "上海天气", at_bot=False) in (None, "")      # 不 @、不带前缀、也不在追问窗口 → 不触发
assert b.say("wxid_a", "上海天气", at_bot=False).startswith("上海现在晴")   # 刚被小助手回复过 → 追问窗口内不 @ 也接
print("天气 ✅")

# ---- 附近：指定地点 / 用最近位置 ----
out = b.say("wxid_a", "望京SOHO附近的咖啡")
lines = out.split("\n")
assert lines[0] == "望京SOHO附近的咖啡（按距离）：" and len(lines) == 6 and lines[1] == "1. 咖啡1号店 · 350米 · 望京街1号" and "1.4公里" in lines[4], out
assert "先在群里发个位置" in b.say("wxid_b", "附近的奶茶")
# 发位置（location_reply + amap 都开）→ 逆地理回一句，并记住位置
tr = b.r.dispatch(normalize(raw("wxid_b", LOC_XML, mt=48)))
rep = [t["reply"] for t in tr if t.get("reply")]
assert rep and rep[0].startswith("收到～你在北京市朝阳区望京街道望京街一带（望京SOHO）"), rep
out = b.say("wxid_b", "附近的奶茶")
assert out.startswith("你那儿附近的奶茶（按距离）") and s.calls[-1][1]["location"] == "116.470293,39.996171", out
assert "先在群里发个位置" in b.say("wxid_c", "附近的奶茶")            # 别人的位置不串
out = b.say("wxid_b", "我附近有什么好吃的")
assert out.startswith("你那儿附近的美食")                              # 「好吃的」→ 美食
print("附近 ✅")

# ---- 路线 ----
out = b.say("wxid_a", "从国贸到望京怎么走")
assert out.split("\n") == ["从国贸到望京：", "🚇 公交地铁约49分钟，地铁14号线，步行1.4公里，约4元", "🚗 开车约14.2公里，34分钟，打车约52元"], out
out = b.say("wxid_a", "从国贸到望京开车怎么走")
assert out.split("\n") == ["从国贸到望京：", "🚗 开车约14.2公里，34分钟，打车约52元"], out
out = b.say("wxid_a", "从北京南站到天津站怎么走")
assert out.split("\n")[0] == "从北京南站到天津站（跨城）：" and "🚄 火车+公交约49分钟，C2027次列车" in out and "打车" not in out, out
assert any(c[0] == "/direction/transit/integrated" and c[1].get("cityd") == "天津市" for c in s.calls)
out = b.say("wxid_b", "从我这到天安门怎么走")                        # 用 wxid_b 最近的位置当起点
assert out.startswith("从你那儿到天安门：")
assert "没找到「不存在的地方」" in b.say("wxid_a", "从不存在的地方到望京怎么走")
print("路线 ✅")

# ---- 开关 / 白名单 / 观察群 ----
n = len(s.calls)
b.r.settings.set(ROOM, "amap", False)
out = b.say("wxid_a", "北京天气")
assert len(s.calls) == n and (out is None or "现在晴" not in out)       # 关了就不查（交给陪聊等）
b.r.settings.set(ROOM, "amap", True)
OTHER = "99999@chatroom"
b.r.settings.set(OTHER, "amap", True)
b.r.dispatch(normalize(raw("wxid_a", "@小助手\u2005北京天气", at=[BOT], room=OTHER)))
assert len(s.calls) == n                                                # 非白名单群：不调 API
b.r.block_targets.add(ROOM)
b.say("wxid_a", "北京天气")
assert len(s.calls) == n                                                # 观察群：硬屏蔽
b.r.block_targets.discard(ROOM)
print("开关/白名单/观察群 ✅")

# ---- 每日上限 ----
b2, s2 = make(limits={"weather": 2})
assert b2.say("wxid_a", "北京天气").startswith("北京现在晴")             # 用掉 2 次 weather
assert b2.say("wxid_a", "北京天气") == "今天地图查询次数用完啦，明天再来问我～"
print("日上限 ✅")

# ---- key 不泄露 ----
class Boom:
    def get(self, url, params=None, timeout=None):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}?key={params['key']}")
c = A.AmapClient(A.Quota(None), session=Boom())
try:
    c.geocode("北京"); raise SystemExit("应抛 AmapError")
except A.AmapError as e:
    assert "FAKEKEY_for_test_123" not in str(e) and "***" in str(e)
class Bad:
    def get(self, url, params=None, timeout=None):
        return Resp({"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})
try:
    A.AmapClient(A.Quota(None), session=Bad()).geocode("北京"); raise SystemExit("应抛")
except A.AmapError as e:
    assert "10001" in str(e) and "FAKEKEY" not in str(e)
buf = io.StringIO(); h = logging.StreamHandler(buf); h.addFilter(A.RedactFilter())
lg = logging.getLogger("amaptest"); lg.addHandler(h); lg.propagate = False
lg.warning("url=%s", "https://x?key=FAKEKEY_for_test_123")
assert "FAKEKEY" not in buf.getvalue() and "***" in buf.getvalue()
b3 = Bot(settings=all_on())                                             # 默认离线客户端 → 友好提示
assert "出错" in b3.say("wxid_a", "北京天气")
print("key 脱敏 ✅")
print("test_amap 全部通过")

# ---- 地点详情（评分/人均/营业时间/电话）+ 结果太少时去后缀补搜 ----
class DetailSession(FakeSession):
    def get(self, url, params=None, timeout=None):
        path = url.split("/v3", 1)[1]
        if path == "/place/text":
            self.calls.append((path, dict(params)))
            assert params["extensions"] == "all"
            ok = {"status": "1", "info": "OK", "infocode": "10000"}
            if params["keywords"] == "呼噜猫馆":
                return Resp({**ok, "count": "2", "pois": [{"name": "咕噜噜猫舍", "address": "雄楚大道260号", "cityname": "武汉市", "adname": "洪山区",
                    "location": "1,1", "tel": [], "biz_ext": {"rating": "3.6", "cost": [], "open_time": "10:00-21:00"}}]})
            if params["keywords"] == "呼噜猫":
                return Resp({**ok, "pois": [{"name": "咕噜噜猫舍", "address": "x", "cityname": "武汉市", "adname": "洪山区", "location": "1,1"},
                    {"name": "呼噜喵居", "address": "大华滨江天地", "cityname": "武汉市", "adname": "青山区", "location": "1,1",
                     "tel": "027-1234;027-5678", "biz_ext": {"rating": "2.5", "cost": "39.00", "open_time": []}}]})
        return super().get(url, params, timeout)

ds = DetailSession()
fd = AmapFeature(A.AmapClient(A.Quota(None), session=ds))
out = fd.where("r", "w", "呼噜猫馆", "武汉市")
lines = out.split("\n")
assert lines[0] == "武汉没有完全叫「呼噜猫馆」的，名字相近的有：" and len(lines) == 3, out          # 1 条 → 补搜后 2 条，去重
assert lines[1] == "1. 咕噜噜猫舍 · 洪山区 · 雄楚大道260号（高德评分3.6，10:00-21:00）", lines[1]
assert lines[2] == "2. 呼噜喵居 · 青山区 · 大华滨江天地（高德评分2.5，人均¥39，☎027-1234）", lines[2]
assert [c[1]["keywords"] for c in ds.calls] == ["呼噜猫馆", "呼噜猫"]
print("地点详情/补搜 ✅")

# ---- 人设：诚实能力（查不了美团/抖音/点评）----
from features.chat_companion import LOOKUP_HINT
assert "查不了美团、抖音、大众点评" in LOOKUP_HINT and "别让人发截图" in LOOKUP_HINT
print("诚实能力提示 ✅")
