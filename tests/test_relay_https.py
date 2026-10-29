"""中转 https：上传走 server_url、公开图片地址走 public_img_base；拉取读超时≥35s、首拉小批。全 mock。"""
import sys, json, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import imagehost, poller

d = Path(tempfile.mkdtemp()); cfgp = d / "c.json"
cfgp.write_text(json.dumps({"server_url": "https://relay.example", "secret": "s", "public_img_base": "http://1.2.3.4:8787"}))
imagehost.CONFIG = cfgp
img = d / "a.png"; img.write_bytes(b"\x89PNG\r\n\x1a\nxx")
seen = {}
class R:
    status_code = 200
    def json(self): return {"path": "/img/abc.png"}
def fake_post(url, **kw): seen["url"] = url; return R()
imagehost.requests.post = fake_post
assert imagehost.upload(img) == "http://1.2.3.4:8787/img/abc.png"
assert seen["url"].startswith("https://relay.example/upload/")
cfgp.write_text(json.dumps({"server_url": "https://relay.example", "secret": "s"}))
assert imagehost.upload(img) == "https://relay.example/img/abc.png"

class PR:
    status_code = 200
    def json(self): return []
class S:
    def get(self, url, params=None, timeout=None): seen.update(p=params, t=timeout, u=url); return PR()
poller._session = S()
poller.pull({"server_url": "https://relay.example/", "secret": "s"}, 5, poller.FIRST_LIMIT)
assert seen["u"] == "https://relay.example/pull" and seen["p"]["limit"] == poller.FIRST_LIMIT <= 50
assert seen["p"]["wait"] == 25 and seen["t"][1] >= 35 and 4 <= seen["t"][0] <= 5
poller.pull({"server_url": "https://relay.example", "secret": "s"}, 5)
assert seen["p"]["limit"] == 200
print("中转 https：上传/公开图片地址分离、wait=25 读超时≥35s、首拉小批 ✅")
