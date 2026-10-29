"""高德 mock：假 session（tests 共用）。"""
import requests

LOC_XML = ('<msg><location x="39.996171" y="116.470293" scale="15" label="北京市朝阳区望京街" maptype="0" '
           'poiname="望京SOHO" poiid="" /></msg>')


class Resp:
    def __init__(self, d): self.d = d
    def raise_for_status(self): pass
    def json(self): return self.d


class FakeSession:
    def __init__(self): self.calls = []
    def get(self, url, params=None, timeout=None):
        assert timeout == 5 and params["key"] == "FAKEKEY_for_test_123"
        path = url.split("/v3", 1)[1]
        self.calls.append((path, dict(params)))
        ok = {"status": "1", "info": "OK", "infocode": "10000"}
        if path == "/geocode/geo":
            a = params["address"]
            if a == "不存在的地方":
                return Resp({**ok, "geocodes": []})
            city = {"北京": ("110000", "010", "北京市", "省"), "上海": ("310000", "021", "上海市", "省")}.get(a, ("110105", "010", "北京市", "住宅区"))
            return Resp({**ok, "geocodes": [{"formatted_address": a, "location": "116.4,39.9", "adcode": city[0],
                                            "citycode": city[1], "city": city[2], "province": city[2], "level": city[3]}]})
        if path == "/geocode/regeo":
            return Resp({**ok, "regeocode": {"formatted_address": "北京市朝阳区望京街道望京街", "addressComponent": {
                "province": "北京市", "city": [], "district": "朝阳区", "township": "望京街道", "adcode": "110105",
                "citycode": "010", "streetNumber": {"street": "望京街"}}}})
        if path == "/weather/weatherInfo":
            if params["extensions"] == "base":
                return Resp({**ok, "lives": [{"weather": "晴", "temperature": "15", "winddirection": "西南", "windpower": "4", "humidity": "21"}]})
            return Resp({**ok, "forecasts": [{"casts": [
                {"dayweather": "晴", "nightweather": "晴", "daytemp": "16", "nighttemp": "2"},
                {"dayweather": "晴", "nightweather": "多云", "daytemp": "16", "nighttemp": "4"},
                {"dayweather": "多云", "nightweather": "多云", "daytemp": "15", "nighttemp": "7"}]}]})
        if path == "/place/text":
            k = params["keywords"]
            if k == "不存在的地方":
                return Resp({**ok, "pois": []})
            city = {"天津站": "天津市", "上海外滩": "上海市"}.get(k, "北京市")
            return Resp({**ok, "pois": [{"name": k, "address": "某路1号", "location": "117.2,39.1" if city != "北京市" else "116.45,39.91",
                                        "cityname": city, "citycode": [], "adcode": "110105"}]})
        if path == "/place/around":
            return Resp({**ok, "pois": [{"name": f"{params['keywords']}{i}号店", "address": f"望京街{i}号", "distance": str(i * 350),
                                        "location": "116.47,39.99", "cityname": "北京市"} for i in range(1, 8)]})
        if path == "/direction/transit/integrated":
            seg = [{"bus": {"buslines": [{"name": "地铁14号线(善各庄--北京南站)"}]}, "railway": []}]
            if params.get("cityd"):
                seg = [{"bus": {"buslines": []}, "railway": {"name": "C2027(北京南-天津)", "trip": "C2027"}}]
            return Resp({**ok, "route": {"distance": "14000", "transits": [{"duration": "2940", "walking_distance": "1400", "cost": "4", "segments": seg}]}})
        if path == "/direction/driving":
            return Resp({**ok, "route": {"taxi_cost": "52", "paths": [{"distance": "14200", "duration": "2040", "tolls": "0"}]}})
        if path == "/direction/walking":
            return Resp({**ok, "route": {"paths": [{"distance": "1500", "duration": "1200"}]}})
        return Resp({"status": "0", "info": "UNKNOWN", "infocode": "20000"})


