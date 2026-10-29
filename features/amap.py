"""高德地图群指令（开关 amap，默认关；不调大模型，规则直出，省延迟）。

触发：群文本 @小助手，或以「小助手」开头（如「小助手 北京天气」）。支持句式：
  · 天气：「北京天气」「望京明天天气怎么样」「天气 上海」「天气」（不带地名 → 用你最近发的位置）
  · 附近：「望京SOHO附近的咖啡」「附近的奶茶」「我附近有什么好吃的」（不带地点 → 用你最近 24 小时在本群发的位置），前 5 个，含距离/地址
  · 路线：「从国贸到望京怎么走」「西直门到北京南站怎么去」「从我这到天安门怎么走」；
          默认给 公交/地铁 + 驾车 两条摘要（2 公里内顺带步行）；句中带「开车/驾车/打车」「公交/地铁」「步行/走路」只给对应方式；
          跨城：驾车 + 跨城公共交通（高德 cityd），查不到就提示坐高铁/飞机
位置消息：由 features/location.py 收到后调用 remember()/describe()；location_reply 开时回一句逆地理地址。
保护：只在发送白名单会话里调 API（非白名单/观察群一律不查、不回）；每日上限见 amap_client.Quota。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

from amap_client import AmapClient, AmapError, QuotaExceeded
from features.common import at_me, is_group_text

log = logging.getLogger("feature.amap")

PREFIXES = ("小助手",)
ADMIN_LEVELS = {"国家", "省", "市", "区县", "乡镇", "开发区"}
LOC_TTL = 24 * 3600
SELF_WORDS = {"我", "我这", "我这里", "我这儿", "这", "这里", "这儿", "我的位置", "我现在的位置", "当前位置", "我所在的位置", "这边", "我这边"}
_TAIL = re.compile(r"[\s?？!！。.~～呀呢啊吗嘛哈]+$")
_ROUTE_MODES = {"driving": ("开车", "驾车", "自驾", "打车"), "transit": ("坐公交", "坐地铁", "公交", "地铁", "公共交通"),
                "walking": ("步行", "走路", "走过去")}
_ROUTE = re.compile(r"^(?:请问)?(?:从)?(?P<a>.+?)(?:到|去)(?P<b>.+?)(?:要)?(?:怎么走|怎么去|怎么过去|咋走|咋去|怎么坐车|怎么坐|路线|多远|多久)$")
_ROUTE_FROM = re.compile(r"^(?:请问)?从(?P<a>.+?)(?:到|去)(?P<b>.+)$")
_WEATHER = re.compile(r"^(?P<p>.*?)(?:今天|明天|后天|现在|这几天|最近|未来几天|今日|明日)?的?天气(?:怎么样|如何|咋样|怎样|预报)?$")
_WEATHER2 = re.compile(r"^天气(?:预报)?[\s:：\u2005]+(?P<p>\S{1,12})$")
_NEARBY = re.compile(r"^(?:帮我)?(?:找找|找|搜搜|搜|查查|查)?(?P<p>.{0,20}?)(?:附近|周边|周围)(?:的|有什么|有啥|有没有|有哪些)(?:好的)?(?P<k>.{1,12}?)(?:推荐)?$")
_WHERE = re.compile(r"^(?P<k>.{1,24}?)(?:到底|具体)?(?:在哪里|在哪儿|在哪|在什么地方|在什么位置|的地址|地址是什么|地址是啥|地址|的位置|位置)$")
_GOTO = re.compile(r"^(?:去|到)?(?P<k>.{1,24}?)(?:要)?(?:怎么去|怎么走|咋去|咋走|怎么过去)$")
_WHERE2 = re.compile(r"^(?:哪里有|哪儿有|哪有)(?P<k>.{1,24})$")
_CITY_KW = re.compile(r"^(?P<c>[\u4e00-\u9fa5]{2,7}?)(?:市)?(?:的|那边的|那里的)(?P<k>.{1,20})$")
_CITY_ONLY = re.compile(r"^(?:在|是)?(?P<c>[\u4e00-\u9fa5]{2,7}?)(?:市)?(?:的|那家|那个|的那家|那边|那边的)?$")
_NOT_CITY = {"好的", "是的", "对的", "谢谢", "哈哈", "可以", "不是", "没有", "知道", "行吧", "算了", "好吧", "你好", "嗯嗯", "不用"}
PENDING_SEC = 600
_KW_MAP = {"好吃的": "美食", "吃的": "美食", "吃饭的": "美食", "饭店": "餐厅", "喝的": "饮品", "好玩的": "休闲娱乐", "玩的": "休闲娱乐",
           "厕所": "公共厕所", "卫生间": "公共厕所", "住的": "酒店"}


_SHORTEN = re.compile(r"(馆|店|屋|坊|舍|吧|厅|中心|会所)$")


def _extra(p: dict) -> str:
    """高德 POI 的评分 / 人均 / 营业时间 / 电话（有才显示）。"""
    bits = []
    if p.get("rating"):
        bits.append(f"高德评分{p['rating']}")
    if p.get("cost"):
        try:
            bits.append(f"人均¥{float(p['cost']):.0f}")
        except ValueError:
            pass
    if p.get("opentime"):
        bits.append(p["opentime"])
    if p.get("tel"):
        bits.append(f"☎{p['tel']}")
    return f"（{'，'.join(bits)}）" if bits else ""


def _city(x: dict) -> str:
    return (x.get("city") or "").removesuffix("市")


def fmt_dist(m: int) -> str:
    return f"{m}米" if m < 1000 else f"{m / 1000:.1f}公里"


def fmt_dur(s: int) -> str:
    mins = max(1, round(s / 60))
    if mins < 60:
        return f"{mins}分钟"
    h, m = divmod(mins, 60)
    return f"{h}小时{m}分钟" if m else f"{h}小时"


class AmapFeature:
    name = "amap"
    passive = False
    toggle_key = "amap"

    def __init__(self, client: AmapClient, allow: Callable[[str], bool] = lambda s: True,
                 blocked: Callable[[str], bool] = lambda s: False, state_path: Path | None = None, now=time.time):
        self.c, self.allow, self.blocked, self.now = client, allow, blocked, now
        self.state_path, self.lock = state_path, threading.Lock()
        self.locs: dict = {}
        self.pending: dict[tuple, dict] = {}          # (群, 人) → 上次没给城市的地点搜索 {kw, ts}，供「武汉的」接上
        self._geo_cache: dict[str, dict | None] = {}
        if state_path and Path(state_path).exists():
            try:
                self.locs = json.loads(Path(state_path).read_text())
            except Exception:
                self.locs = {}

    # ---------- 位置记忆 ----------
    def remember(self, sess: str, wxid: str, loc) -> None:
        if self.blocked(sess):
            return
        with self.lock:
            self.locs.setdefault(sess, {})[wxid] = {"lnglat": loc.amap_lnglat, "shown": loc.shown, "ts": self.now()}
            self._save()

    def _save(self):
        if not self.state_path:
            return
        tmp = Path(self.state_path).with_suffix(".tmp")
        tmp.write_text(json.dumps(self.locs, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_path)

    def last_loc(self, sess: str, wxid: str) -> dict | None:
        x = (self.locs.get(sess) or {}).get(wxid)
        if x and self.now() - x.get("ts", 0) <= LOC_TTL:
            return x
        return None

    def _loc_city(self, x: dict) -> dict:
        """给记住的位置补上城市（逆地理一次后缓存）。"""
        if not x.get("city"):
            r = self.c.regeo(x["lnglat"])
            x.update(city=r["city"], citycode=r["citycode"], adcode=r["adcode"], district=r["district"])
            with self.lock:
                self._save()
        return x

    def describe(self, sess: str, loc) -> str | None:
        """location_reply 用：逆地理一句话。不允许/出错返回 None（由调用方回退）。"""
        if not self.c.configured or not self.allow(sess) or self.blocked(sess):
            return None
        try:
            r = self.c.regeo(loc.amap_lnglat)
        except AmapError as e:
            log.warning("逆地理失败 %s", e)
            return None
        where = r["district"] + r["township"] + (r["street"] or "") or r["address"]
        poi = f"（{loc.poiname}）" if loc.poiname else ""
        return f"收到～你在{r['city'] if r['city'] not in where else ''}{where}一带{poi}。想找附近的店可以@我「附近的奶茶」这样问"

    # ---------- 触发 ----------
    def _text(self, msg) -> str | None:
        if not is_group_text(msg):
            return None
        t = msg.command_text if at_me(msg) else (msg.content or "").strip()
        p = next((p for p in PREFIXES if t.startswith(p)), None)
        if p and not at_me(msg) and not self.prefix_ok(msg.room_id or msg.from_wxid):
            return None
        if p:
            t = t[len(p):].lstrip(" ,，:：\u2005")
        elif not at_me(msg):
            return None
        return _TAIL.sub("", t).strip()

    def parse(self, t: str):
        if not t or len(t) > 60:
            return None
        if "天气" in t:
            m = (_WEATHER2.match(t) if t.startswith("天气") else None) or _WEATHER.match(t)
            if m:
                return ("weather", m.group("p").strip())
        mode = None
        core = t
        for k, words in _ROUTE_MODES.items():
            for w in words:
                if w in core and ("到" in core or "去" in core):
                    mode, core = k, core.replace(w, "")
                    break
            if mode:
                break
        m = _ROUTE.match(core) or (_ROUTE_FROM.match(core) if core.startswith(("从", "请问从")) else None)
        if m and m.group("a").strip() and m.group("b").strip():
            return ("route", m.group("a").strip(), m.group("b").strip(), mode)
        m = _NEARBY.match(t)
        if m and m.group("k").strip():
            return ("nearby", m.group("p").strip(), m.group("k").strip())
        m = _GOTO.match(t)
        if m and m.group("k").strip() not in SELF_WORDS:
            return ("goto", m.group("k").strip())
        m = _WHERE.match(t) or _WHERE2.match(t)
        if m:
            k = m.group("k").strip().removesuffix("的")
            if k and k not in SELF_WORDS and k not in {"你", "他", "她", "它", "你们", "我们", "这个", "那个", "这家", "那家"}:
                return ("where", k)
        return None

    def match(self, msg) -> bool:
        t = self._text(msg)
        if not t:
            return False
        if self.parse(t) is not None:
            return True
        return self._city_followup(msg, t) is not None

    def _is_city(self, name: str) -> dict | None:
        if name in self._geo_cache:
            return self._geo_cache[name]
        try:
            g = self.c.geocode(name)
        except AmapError:
            return None
        g = g if g and g.get("level") in ADMIN_LEVELS else None
        self._geo_cache[name] = g
        return g

    def _city_followup(self, msg, t: str):
        """上一句搜地点没给城市，这句只说了城市（「武汉的」「在武汉」）→ 返回 (kw, 城市名)。"""
        sess = msg.room_id or msg.from_wxid
        p = self.pending.get((sess, msg.from_wxid))
        if not p or self.now() - p["ts"] > PENDING_SEC or self.blocked(sess) or not self.allow(sess) or not self.c.configured:
            return None
        m = _CITY_ONLY.match(t)
        if not m or m.group("c") in _NOT_CITY:
            return None
        g = self._is_city(m.group("c"))
        return (p["kw"], g.get("city") or m.group("c")) if g else None

    prefix_ok = staticmethod(lambda sess: True)   # 某些群（allow_send 观察群）只认 @，不认「小助手」前缀
    on_reply = None          # on_reply(msg, text)：规则层回了话后回调（app 里绑到陪聊上下文）

    def handle(self, msg):
        out = self._handle(msg)
        if out and self.on_reply:
            try:
                self.on_reply(msg, out)
            except Exception:
                log.exception("amap on_reply 失败")
        return out

    def _handle(self, msg):
        sess = msg.room_id or msg.from_wxid
        if self.blocked(sess) or not self.allow(sess):
            return None
        if not self.c.configured:
            return "地图功能还没配置好～"
        t = self._text(msg)
        q = self.parse(t)
        try:
            if q is None:
                fu = self._city_followup(msg, t)
                if not fu:
                    return None
                return self.where(sess, msg.from_wxid, fu[0], fu[1])
            if q[0] == "where":
                return self.where(sess, msg.from_wxid, q[1])
            if q[0] == "goto":                       # 没说起点：有最近位置就从那儿规划，没有就先告诉在哪
                if self.last_loc(sess, msg.from_wxid):
                    return self.route(sess, msg.from_wxid, "我这", q[1])
                return self.where(sess, msg.from_wxid, q[1]) + "\n（发个位置给我，就能帮你规划从你那儿怎么过去）"
            if q[0] == "weather":
                return self.weather(sess, msg.from_wxid, q[1])
            if q[0] == "nearby":
                return self.nearby(sess, msg.from_wxid, q[1], q[2])
            return self.route(sess, msg.from_wxid, q[1], q[2], q[3])
        except QuotaExceeded:
            return "今天地图查询次数用完啦，明天再来问我～"
        except AmapError as e:
            log.warning("高德查询失败 %s", e)
            return "地图那边没查到/出错了，换个说法或者稍后再试～"

    # ---------- 地点解析 ----------
    def _resolve(self, sess, wxid, name, city_hint=None) -> dict | None:
        if name in SELF_WORDS:
            x = self.last_loc(sess, wxid)
            if not x:
                return None
            x = self._loc_city(x)
            return {"name": "你那儿", "location": x["lnglat"], "city": x.get("city"), "citycode": x.get("citycode"),
                    "adcode": x.get("adcode")}
        # POI/地标优先用关键字搜索（高德地理编码对「国贸」这类简称常给出外地同名村庄），没有再退回地理编码
        pois = self.c.place_text(name, city_hint, limit=1)        # city_hint 只是优先，不限死（「天津站」不会被锁在北京）
        if pois and pois[0]["location"]:
            p = pois[0]
            return {"name": p["name"], "location": p["location"], "city": p["city"], "citycode": p["citycode"], "adcode": p["adcode"]}
        try:
            g = self.c.geocode(name, city_hint)
        except QuotaExceeded:
            raise
        except AmapError:
            g = None
        return g if g and g.get("location") else None

    def _hint(self, sess, wxid):
        x = self.last_loc(sess, wxid)
        if not x:
            return None
        try:
            return self._loc_city(x).get("city") or None
        except AmapError:
            return None

    # ---------- 三个指令 ----------
    def weather(self, sess, wxid, place):
        if not place or place in SELF_WORDS:
            x = self.last_loc(sess, wxid)
            if not x:
                return "想查哪儿的天气？比如「北京天气」，或者先发个位置给我～"
            x = self._loc_city(x)
            adcode, label = x.get("adcode"), x.get("district") or x.get("city") or "你那儿"
        else:
            g = self.c.geocode(place)
            if not g or g.get("level") not in ADMIN_LEVELS:      # 不是行政区名（如「国贸」）→ 搜地点再逆地理取 adcode
                r = self._resolve(sess, wxid, place)
                g = {"adcode": self.c.regeo(r["location"])["adcode"]} if r else g
            if not g or not g.get("adcode"):
                return f"没找到「{place}」这个地方，换个说法试试～"
            adcode, label = g["adcode"], place
        w = self.c.weather(adcode)
        lv = w["live"] or {}
        if not lv.get("weather"):
            return f"{label}的天气暂时没查到～"
        s = f"{label}现在{lv['weather']}，{lv.get('temperature')}℃，{lv.get('winddirection')}风{lv.get('windpower')}级，湿度{lv.get('humidity')}%"
        names = ["今天", "明天", "后天"]
        days = [f"{names[i]}{c['dayweather']}{'' if c['dayweather'] == c['nightweather'] else '转' + c['nightweather']} {c['nighttemp']}~{c['daytemp']}℃"
                for i, c in enumerate(w["casts"][:3])]
        return s + ("。\n" + "；".join(days) if days else "")

    def nearby(self, sess, wxid, place, kw):
        kw = _KW_MAP.get(kw, kw)
        if not place or place in SELF_WORDS:
            x = self.last_loc(sess, wxid)
            if not x:
                return "你在哪儿呀？先在群里发个位置给我，或者说「望京附近的咖啡」这样～"
            center, label = x["lnglat"], "你那儿"
        else:
            g = self._resolve(sess, wxid, place, self._hint(sess, wxid))
            if not g:
                return f"没找到「{place}」这个地方～"
            center, label = g["location"], place
        pois = self.c.place_around(center, kw, limit=5)
        if not pois:
            return f"{label}附近 3 公里内没找到{kw}～"
        lines = [f"{label}附近的{kw}（按距离）："]
        for i, p in enumerate(pois, 1):
            addr = f" · {p['address']}" if p["address"] else ""
            lines.append(f"{i}. {p['name']} · {fmt_dist(p['distance'])}{addr}")
        return "\n".join(lines)

    def where(self, sess, wxid, kw, city=None, limit=5):
        """地点在哪：先查再问。没城市 → 全国搜，多个结果列出来让对方挑（并记下，等对方补城市）；有城市 → 只在该城市搜。"""
        if not city:
            m = _CITY_KW.match(kw)
            if m and self._is_city(m.group("c")):
                city, kw = (self._is_city(m.group("c")).get("city") or m.group("c")), m.group("k")
        hint = None if city else self._hint(sess, wxid)     # 没说城市但发过位置 → 优先那个城市（不限死）
        pois = self.c.place_text(kw, city or hint, limit=limit, strict=bool(city), detail=True)
        short = _SHORTEN.sub("", kw)
        if city and len(pois) < 3 and short != kw and len(short) >= 2 and not any(kw in p["name"] for p in pois):
            # 高德对「呼噜猫馆」这类词常只回 1 条（count=2 也只给 1 条）→ 去掉「馆/店」后缀再搜一次补充
            seen = {p["name"] for p in pois}
            pois += [p for p in self.c.place_text(short, city, limit=limit, strict=True, detail=True) if p["name"] not in seen]
            pois = pois[:limit]
        cn = (city or "").removesuffix("市")
        if not pois:
            if city:
                return f"{cn}没搜到「{kw}」😅 名字对吗？可能换了名或者高德没收录，换个关键词我再搜"
            self.pending[(sess, wxid)] = {"kw": kw, "ts": self.now()}
            return f"全国都没搜到叫「{kw}」的😅 名字对吗？或者告诉我大概在哪个城市，我再搜搜"
        exact = [p for p in pois if kw in p["name"]]
        head = (f"{cn}的「{kw}」：" if exact else f"{cn}没有完全叫「{kw}」的，名字相近的有：") if city else \
               (f"搜到这几个「{kw}」：" if exact else f"没说城市我先大范围搜了下，没有完全叫「{kw}」的，名字相近的有：")
        lines = [head]
        for i, p in enumerate(pois, 1):
            where = (p["city"] if not city else "") + p.get("district", "")
            lines.append(f"{i}. {p['name']} · {where}{(' · ' + p['address']) if p['address'] else ''}{_extra(p)}")
        if not city:
            cities = {p["city"] for p in pois}
            self.pending[(sess, wxid)] = {"kw": kw, "ts": self.now()}
            lines.append("你说的是哪个城市的？告诉我城市我再精确搜～" if len(cities) > 1 or not exact else "是这家吗？不是的话告诉我城市～")
        return "\n".join(lines)

    def route(self, sess, wxid, a, b, mode=None):
        hint = self._hint(sess, wxid)
        A = self._resolve(sess, wxid, a, hint)
        if not A:
            return "你在哪儿出发呀？先发个位置给我～" if a in SELF_WORDS else f"没找到「{a}」这个地方～"
        B = self._resolve(sess, wxid, b, A.get("city") or hint)
        if not B:
            return "目的地是你那儿？先发个位置给我～" if b in SELF_WORDS else f"没找到「{b}」这个地方～"
        an, bn = ("你那儿" if a in SELF_WORDS else a), ("你那儿" if b in SELF_WORDS else b)
        ca, cb = _city(A), _city(B)
        cross = bool(ca and cb and ca != cb)
        out = [f"从{an}到{bn}{'（跨城）' if cross else ''}："]
        if mode in (None, "transit"):
            t = self.c.transit(A["location"], B["location"], A.get("citycode") or A.get("city") or "",
                               (B.get("citycode") or B.get("city")) if cross else None) if (A.get("citycode") or A.get("city")) else None
            if t:
                via = "→".join(t["lines"][:4])
                cost = f"，约{t['cost']:g}元" if t["cost"] else ""
                walk = f"，步行{fmt_dist(t['walking'])}" if t["walking"] else ""
                head = "🚄 火车+公交" if t.get("rail") else "🚇 公交地铁"
                out.append(f"{head}约{fmt_dur(t['duration'])}{'，' + via if via else ''}{walk}{cost}")
            elif cross:
                out.append("🚄 跨城的公共交通没查到，建议查高铁/飞机～")
            elif mode == "transit":
                out.append("🚇 没查到合适的公交/地铁，可能离得太近，走过去吧～")
        drive = None
        if mode in (None, "driving"):
            drive = self.c.driving(A["location"], B["location"])
            if drive:
                taxi = f"，打车约{drive['taxi_cost']:.0f}元" if drive.get("taxi_cost") and not cross else ""
                toll = f"，过路费约{drive['tolls']:.0f}元" if drive.get("tolls") else ""
                out.append(f"🚗 开车约{fmt_dist(drive['distance'])}，{fmt_dur(drive['duration'])}{taxi}{toll}")
        if mode == "walking" or (mode is None and drive and drive["distance"] <= 2000):
            w = self.c.walking(A["location"], B["location"])
            if w:
                out.append(f"🚶 走路约{fmt_dist(w['distance'])}，{fmt_dur(w['duration'])}")
            elif mode == "walking":
                out.append("🚶 太远了，走不过去的～")
        if len(out) == 1:
            return f"从{an}到{bn}没查到路线～"
        return "\n".join(out)


class AmapTools:
    """给陪聊模型用的工具（OpenAI function calling 格式）。结果是给模型看的短文本，模型再用口语转述。"""
    names = {"search_place", "weather", "nearby", "route"}
    ack = True

    @staticmethod
    def wants(text: str, last_user: str = "") -> bool:
        from features.chat_companion import MAP_HINT
        if MAP_HINT.search(text or ""):
            return True
        return len(text or "") <= 15 and bool(MAP_HINT.search(last_user or ""))
    specs = [
        {"type": "function", "function": {"name": "search_place", "description": "按名字/关键字搜地点、店铺(POI)在哪。没说城市就全国搜，结果多个时把几个都告诉用户让他挑",
         "parameters": {"type": "object", "properties": {"keywords": {"type": "string", "description": "店名或关键字"},
                                                         "city": {"type": "string", "description": "城市名，如 武汉；不知道就留空"}},
                        "required": ["keywords"]}}},
        {"type": "function", "function": {"name": "weather", "description": "查某地实时天气和 3 天预报",
         "parameters": {"type": "object", "properties": {"place": {"type": "string", "description": "城市/区县/地标；留空=用户最近发的位置"}},
                        "required": []}}},
        {"type": "function", "function": {"name": "nearby", "description": "搜某个地点附近 3 公里内的某类地点（按距离）",
         "parameters": {"type": "object", "properties": {"place": {"type": "string", "description": "中心地点；留空=用户最近发的位置"},
                                                         "keywords": {"type": "string", "description": "要找什么，如 咖啡、奶茶、地铁站"}},
                        "required": ["keywords"]}}},
        {"type": "function", "function": {"name": "route", "description": "查从 A 到 B 怎么走（公交地铁/驾车/步行摘要，跨城也行）",
         "parameters": {"type": "object", "properties": {"origin": {"type": "string", "description": "起点；「我这」=用户最近发的位置"},
                                                         "destination": {"type": "string"},
                                                         "mode": {"type": "string", "enum": ["", "transit", "driving", "walking"]}},
                        "required": ["origin", "destination"]}}},
    ]

    def __init__(self, feature: AmapFeature, enabled=lambda sess: True):
        self.f, self.enabled = feature, enabled

    def available(self, sess: str) -> bool:
        f = self.f
        return f.c.configured and self.enabled(sess) and f.allow(sess) and not f.blocked(sess)

    def run(self, name: str, args: dict, sess: str, wxid: str) -> str:
        f = self.f
        if not self.available(sess):
            return "地图工具在这里不可用"
        a = {k: (v or "").strip() if isinstance(v, str) else v for k, v in (args or {}).items()}
        try:
            if name == "search_place" and a.get("keywords"):
                return f.where(sess, wxid, a["keywords"], a.get("city") or None)
            if name == "weather":
                return f.weather(sess, wxid, a.get("place", ""))
            if name == "nearby" and a.get("keywords"):
                return f.nearby(sess, wxid, a.get("place", ""), a["keywords"])
            if name == "route" and a.get("origin") and a.get("destination"):
                return f.route(sess, wxid, a["origin"], a["destination"], a.get("mode") or None)
            return "参数不对，没查"
        except QuotaExceeded:
            return "今天地图查询次数用完了"
        except AmapError as e:
            log.warning("地图工具失败 %s", e)
            return "地图查询出错了"
