"""位置消息（MsgType=48）解析。只解析本地 XML，不调任何外部地图 API。

gewe 回调结构说明 v1（doc.geweapi.com/doc-3599831）只写了判断条件 $.Data.MsgType = 48；XML 字段以 gewe 发送定位接口
（postLocation，doc.geweapi.com/api-452601074）的 content 示例为准（微信原生格式）：
  <msg><location x="纬度" y="经度" scale="15" label="详细地址" maptype="0" poiname="地点名" poiid="qqmap_…"
       poiCategoryTips="娱乐休闲:咖啡厅" poiBusinessHour="…" poiPhone="…" adcode="" cityname="" /></msg>
注意：x 是纬度、y 是经度（与高德 "经度,纬度" 顺序相反）；坐标系为 GCJ-02（腾讯地图），与高德一致，不用转换。
⚠️ 真实回调样本尚未收到（inbox 里 0 条 MsgType=48），收到后需核对。

LocationFeature：开关 location_reply（默认关）。开了回一句地址：本群 amap 也开着时用高德逆地理（features/amap.py），否则「收到定位：xx」。
本群 amap 开着时还会记住每人最近一次位置（data/amap_lastloc.json，24 小时有效），供「附近的YY」使用。
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

log = logging.getLogger("feature.location")


@dataclass
class Location:
    lat: float
    lng: float
    label: str = ""          # 详细地址
    poiname: str = ""        # 地点名
    poiid: str = ""
    category: str = ""
    scale: str = ""

    @property
    def amap_lnglat(self) -> str:
        """高德 Web 服务 API 要的 "经度,纬度"（小数点后 ≤6 位）。"""
        return f"{self.lng:.6f},{self.lat:.6f}"

    @property
    def shown(self) -> str:
        if self.poiname and self.label and self.poiname not in self.label:
            return f"{self.poiname}（{self.label}）"
        return self.poiname or self.label or f"{self.lat:.5f},{self.lng:.5f}"


def parse_location(content: str) -> Location | None:
    text = (content or "").strip()
    i = text.find("<")
    if i < 0:
        return None
    text = text[i:]
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    node = root if root.tag == "location" else root.find(".//location")
    if node is None:
        return None
    try:
        lat, lng = float(node.get("x", "")), float(node.get("y", ""))
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return Location(lat, lng, node.get("label", "") or "", node.get("poiname", "") or "", node.get("poiid", "") or "",
                    node.get("poiCategoryTips", "") or "", node.get("scale", "") or "")


class LocationFeature:
    name = "location"
    passive = True
    toggle_key = None            # 解析/记日志总是做；回复受 location_reply 开关控制（handle 里判断）

    def __init__(self, settings=None, amap=None):
        self.settings, self.amap = settings, amap     # amap：features.amap.AmapFeature（记位置 + 逆地理回复），可为 None

    def match(self, msg) -> bool:
        return msg.event == "AddMsg" and msg.msg_type == 48 and not msg.is_self

    def handle(self, msg):
        loc = parse_location(msg.content)
        sess = msg.room_id or msg.from_wxid
        if not loc:
            log.info("位置消息解析失败 session=%s", sess)
            return None
        log.info("位置消息 session=%s from=%s lat=%.6f lng=%.6f poi=%s label=%s",
                 sess, msg.from_wxid, loc.lat, loc.lng, loc.poiname, loc.label)
        amap_on = self.amap is not None and (self.settings is None or self.settings.enabled(sess, "amap"))
        if amap_on:
            self.amap.remember(sess, msg.from_wxid, loc)      # 之后「附近的YY」用这个人最近的位置
        if self.settings is not None and self.settings.enabled(sess, "location_reply"):
            return (self.amap.describe(sess, loc) if amap_on else None) or f"收到定位：{loc.shown}"
        return None
