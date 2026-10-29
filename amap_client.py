"""高德 Web 服务 API 客户端（restapi.amap.com v3）。

- Key 只从环境变量 AMAP_KEY 读；任何异常信息/日志都会把 key 抹成 ***，绝不回显。
- 复用 requests.Session；超时 5 秒。
- 每日调用上限（按类别计数，持久化到 data/amap_quota.json，按北京时间日期重置）：
    weather / search / route 默认 150 次/天，geo（地理编码+逆地理）默认 300 次/天；
    可用环境变量 AMAP_DAILY_WEATHER / AMAP_DAILY_SEARCH / AMAP_DAILY_ROUTE / AMAP_DAILY_GEO 覆盖。
  一次「天气」= 1 次 geo + 1~2 次 weather；一次「附近」= 0~1 次 geo + 1 次 search；
  一次「路线」= 2 次 geo + 1~2 次 route。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("amap")
BASE = os.environ.get("AMAP_BASE_URL", "https://restapi.amap.com/v3")
TIMEOUT = 5
TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_LIMITS = {"weather": 150, "search": 150, "route": 150, "geo": 300}


class AmapError(RuntimeError):
    pass


class QuotaExceeded(AmapError):
    pass


def _key() -> str:
    return os.environ.get("AMAP_KEY", "").strip()


def redact(text: str) -> str:
    k = _key()
    s = str(text)
    return s.replace(k, "***") if k else s


class RedactFilter(logging.Filter):
    """挂到 handler 上：把日志里出现的 AMAP_KEY 抹掉（兜底，正常路径本来就不打 key）。"""
    def filter(self, record):
        k = _key()
        if k:
            msg = record.getMessage()
            if k in msg:
                record.msg, record.args = msg.replace(k, "***"), ()
        return True


class Quota:
    def __init__(self, path: Path | None, limits: dict | None = None):
        self.path, self.lock = path, threading.Lock()
        self.limits = dict(DEFAULT_LIMITS)
        for cat in self.limits:
            v = os.environ.get(f"AMAP_DAILY_{cat.upper()}")
            if v and v.isdigit():
                self.limits[cat] = int(v)
        self.limits.update(limits or {})
        self.state = {"date": "", "used": {}}
        if path and Path(path).exists():
            try:
                self.state = json.loads(Path(path).read_text())
            except Exception:
                pass

    def _today(self):
        return datetime.now(TZ).strftime("%Y-%m-%d")

    def take(self, cat: str):
        with self.lock:
            if self.state.get("date") != self._today():
                self.state = {"date": self._today(), "used": {}}
            used = self.state["used"].get(cat, 0)
            if used >= self.limits.get(cat, 150):
                raise QuotaExceeded(cat)
            self.state["used"][cat] = used + 1
            if self.path:
                tmp = Path(self.path).with_suffix(".tmp")
                tmp.write_text(json.dumps(self.state))
                os.replace(tmp, self.path)

    def used(self, cat: str) -> int:
        return self.state.get("used", {}).get(cat, 0) if self.state.get("date") == self._today() else 0


class AmapClient:
    def __init__(self, quota: Quota | None = None, session: requests.Session | None = None):
        self.quota = quota or Quota(None)
        self.s = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(_key())

    def _get(self, path: str, params: dict, cat: str) -> dict:
        key = _key()
        if not key:
            raise AmapError("AMAP_KEY 未设置")
        self.quota.take(cat)
        try:
            r = self.s.get(BASE + path, params={**params, "key": key, "output": "JSON"}, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            # requests 的异常串里可能带完整 URL（含 key）→ 只留类型 + 抹掉 key
            raise AmapError(f"网络错误 {type(e).__name__}: {redact(e)[:200]}") from None
        except ValueError:
            raise AmapError("返回不是 JSON") from None
        if str(data.get("status")) != "1":
            raise AmapError(f"高德返回错误 {data.get('infocode')} {data.get('info')}")
        return data

    # ---------- 地理编码 ----------
    def geocode(self, address: str, city: str | None = None) -> dict | None:
        p = {"address": address}
        if city:
            p["city"] = city
        g = (self._get("/geocode/geo", p, "geo").get("geocodes") or [])
        if not g:
            return None
        x = g[0]
        return {"name": address, "address": x.get("formatted_address") or address, "location": x.get("location"),
                "adcode": x.get("adcode"), "citycode": _s(x.get("citycode")), "city": _s(x.get("city")) or _s(x.get("province")),
                "province": _s(x.get("province")), "level": x.get("level")}

    def regeo(self, lnglat: str) -> dict:
        r = self._get("/geocode/regeo", {"location": lnglat, "extensions": "base"}, "geo").get("regeocode") or {}
        ac = r.get("addressComponent") or {}
        street = ac.get("streetNumber") or {}
        return {"address": _s(r.get("formatted_address")), "province": _s(ac.get("province")),
                "city": _s(ac.get("city")) or _s(ac.get("province")), "district": _s(ac.get("district")),
                "township": _s(ac.get("township")), "street": _s(street.get("street")),
                "adcode": _s(ac.get("adcode")), "citycode": _s(ac.get("citycode"))}

    # ---------- 天气 ----------
    def weather(self, adcode: str, forecast: bool = True) -> dict:
        live = (self._get("/weather/weatherInfo", {"city": adcode, "extensions": "base"}, "weather").get("lives") or [{}])[0]
        casts = []
        if forecast:
            f = (self._get("/weather/weatherInfo", {"city": adcode, "extensions": "all"}, "weather").get("forecasts") or [{}])[0]
            casts = f.get("casts") or []
        return {"live": live, "casts": casts}

    # ---------- 搜索 ----------
    def place_text(self, keywords: str, city: str | None = None, limit: int = 5, strict: bool = False,
                   detail: bool = False) -> list[dict]:
        """city 只作「优先」提示（citylimit=false），strict=True 才限死在该城市。
        detail=True 用 extensions=all，多拿评分/人均/电话/营业时间（biz_ext、tel）。"""
        p = {"keywords": keywords, "offset": limit, "page": 1, "extensions": "all" if detail else "base"}
        if city:
            p.update(city=city, citylimit="true" if strict else "false")
        return [_poi(x) for x in (self._get("/place/text", p, "search").get("pois") or [])[:limit]]

    def place_around(self, lnglat: str, keywords: str, radius: int = 3000, limit: int = 5) -> list[dict]:
        p = {"location": lnglat, "keywords": keywords, "radius": radius, "sortrule": "distance",
             "offset": limit, "page": 1, "extensions": "base"}
        return [_poi(x) for x in (self._get("/place/around", p, "search").get("pois") or [])[:limit]]

    # ---------- 路线 ----------
    def walking(self, o: str, d: str) -> dict | None:
        paths = (self._get("/direction/walking", {"origin": o, "destination": d}, "route").get("route") or {}).get("paths") or []
        return {"distance": _i(paths[0].get("distance")), "duration": _i(paths[0].get("duration"))} if paths else None

    def driving(self, o: str, d: str) -> dict | None:
        route = self._get("/direction/driving", {"origin": o, "destination": d, "extensions": "base"}, "route").get("route") or {}
        paths = route.get("paths") or []
        if not paths:
            return None
        p = paths[0]
        return {"distance": _i(p.get("distance")), "duration": _i(p.get("duration")),
                "taxi_cost": _f(route.get("taxi_cost")), "tolls": _f(p.get("tolls"))}

    def transit(self, o: str, d: str, city: str, cityd: str | None = None) -> dict | None:
        p = {"origin": o, "destination": d, "city": city, "strategy": 0, "nightflag": 0}
        if cityd:
            p["cityd"] = cityd
        route = self._get("/direction/transit/integrated", p, "route").get("route") or {}
        ts = route.get("transits") or []
        if not ts:
            return None
        t = ts[0]
        lines, has_rail = [], False
        for seg in t.get("segments") or []:
            for b in ((seg.get("bus") or {}).get("buslines") or [])[:1]:
                lines.append(_s(b.get("name")).split("(")[0])
            rw = seg.get("railway") or {}
            if isinstance(rw, dict) and rw.get("name"):
                lines.append((_s(rw.get("trip")) or _s(rw.get("name"))) + "次列车")
                has_rail = True
        return {"duration": _i(t.get("duration")), "walking": _i(t.get("walking_distance")),
                "cost": _f(t.get("cost")), "lines": lines, "distance": _i(route.get("distance")), "rail": has_rail}


def _s(v) -> str:
    return "" if v is None or isinstance(v, list) else str(v)   # 高德空字段常是 []


def _i(v) -> int:
    try:
        return int(float(_s(v) or 0))
    except ValueError:
        return 0


def _f(v) -> float | None:
    try:
        x = _s(v)
        return float(x) if x else None
    except ValueError:
        return None


def _poi(x: dict) -> dict:
    return {"name": _s(x.get("name")), "address": _s(x.get("address")), "distance": _i(x.get("distance")),
            "location": _s(x.get("location")), "city": _s(x.get("cityname")), "citycode": _s(x.get("citycode")),
            "district": _s(x.get("adname")),
            "rating": _s((x.get("biz_ext") or {}).get("rating")) if isinstance(x.get("biz_ext"), dict) else "",
            "cost": _s((x.get("biz_ext") or {}).get("cost")) if isinstance(x.get("biz_ext"), dict) else "",
            "opentime": _s((x.get("biz_ext") or {}).get("open_time")) if isinstance(x.get("biz_ext"), dict) else "",
            "tel": _s(x.get("tel")).split(";")[0],
            "adcode": _s(x.get("adcode"))}
