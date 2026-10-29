"""位置消息解析 + postLocation 客户端（mock，不联网、不发消息）。"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gewe_client as g
from app import build_router
from features.location import parse_location
from features.settings import FeatureSettings
from models import normalize

BOT, ROOM = "wxid_bot_demo", "20000000002@chatroom"
SAMPLE = ('<?xml version="1.0"?>\n<msg>\n\t<location x="27.690519" y="106.937958" scale="15" '
          'label="贵州省遵义市红花岗区合众路16号" maptype="0" poiname="冰岛咖啡(合众路店)" '
          'poiid="qqmap_16018756252331389352" poiCategoryTips="娱乐休闲:咖啡厅" adcode="" cityname="" />\n</msg>\n')

loc = parse_location(SAMPLE)
assert (loc.lat, loc.lng) == (27.690519, 106.937958) and loc.poiname == "冰岛咖啡(合众路店)"
assert loc.amap_lnglat == "106.937958,27.690519" and loc.category == "娱乐休闲:咖啡厅"
assert loc.shown == "冰岛咖啡(合众路店)（贵州省遵义市红花岗区合众路16号）"
assert parse_location("wxid_x:\n" + SAMPLE).lat == 27.690519          # 带发言人前缀也能解析
assert parse_location("<msg><location x='abc' y='1'/></msg>") is None and parse_location("hello") is None
assert parse_location('<msg><location x="99" y="1"/></msg>') is None

def raw(mid, room=ROOM):
    return {"TypeName": "AddMsg", "Appid": "wx_sim", "Wxid": BOT, "Data": {"NewMsgId": mid, "MsgType": 48,
            "FromUserName": {"string": room}, "ToUserName": {"string": BOT}, "Content": {"string": "wxid_x:\n" + SAMPLE},
            "CreateTime": 1792493289, "MsgSource": "<msgsource/>"}}
st = FeatureSettings(None); sent = []
r = build_router(fetch_list=lambda x: {}, fetch_info=lambda x: {}, sender=sent.append, send_enabled=True,
                 allow_targets={ROOM}, db_path=Path(tempfile.mkdtemp()) / "t.db", settings=st)
tr = r.dispatch(normalize(raw(1)))
assert not any(t.get("reply") for t in tr) and sent == []                # 默认关：只解析不回
st.set(ROOM, "location_reply", True)
tr = r.dispatch(normalize(raw(2)))
assert [t["reply"] for t in tr if t.get("reply")] == ["收到定位：冰岛咖啡(合众路店)（贵州省遵义市红花岗区合众路16号）"] and len(sent) == 1
st.set("11111@chatroom", "location_reply", True)
tr = r.dispatch(normalize(raw(3, "11111@chatroom")))
assert [t["sent"] for t in tr if t.get("reply")] == [False] and len(sent) == 1    # 非白名单不真发

calls = []
orig = g.call
g.call = lambda path, body=None, timeout=0: calls.append((path, body)) or {"newMsgId": 9}
try:
    g.post_location(ROOM, 27.690519, 106.937958, label='合众路16号 "A&B"', poiname="冰岛咖啡")
finally:
    g.call = orig
path, body = calls[-1]
assert path == "/gewe/v2/api/message/postLocation" and body["toWxid"] == ROOM
back = parse_location(body["content"])
assert (back.lat, back.lng, back.label, back.poiname) == (27.690519, 106.937958, '合众路16号 "A&B"', "冰岛咖啡")
try:
    g.build_location_xml(200, 1); raise SystemExit("应拒绝越界坐标")
except ValueError:
    pass
print("位置解析 + postLocation(mock) 单测通过 ✅")
