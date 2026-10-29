"""网络容错（10-23 01:05 生图 ReadTimeout / 01:01 postText ReadTimeout）：
生图网络错误重试一次且总时长有上限、不重复说「在画了」、失败如实说网络超时并退额度；
gewe 发送类只在确定没发出（建连失败）时重试，ReadTimeout/中途断开不重试。全 mock，不联网不发消息。"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests
import urllib3.exceptions as U
import minimax_media as M
import gewe_client as G
from features.image_gen import ImageGen, failure_text
from features.daily_quota import DailyCounter

JPG = b"\xff\xd8\xff" + b"0" * 100


class Resp:
    def __init__(self, j=None, content=b""):
        self._j, self.content, self.status_code = j, content, 200
    def json(self): return self._j
    def raise_for_status(self): pass


class FakeMM:
    """posts / gets：依次返回的结果（异常就抛）；clock 前进 cost 秒模拟耗时。"""
    def __init__(self, posts, gets, clock, cost=0.0):
        self.posts, self.gets, self.clock, self.cost = list(posts), list(gets), clock, cost
        self.post_timeouts, self.get_n = [], 0
    def post(self, url, json=None, timeout=None, headers=None):
        self.post_timeouts.append(timeout)
        self.clock[0] += self.cost
        x = self.posts.pop(0)
        if isinstance(x, Exception):
            raise x
        return Resp(x)
    def get(self, url, timeout=None):
        self.get_n += 1
        x = self.gets.pop(0)
        if isinstance(x, Exception):
            raise x
        return Resp(content=x)


OK = {"base_resp": {"status_code": 0}, "data": {"image_urls": ["https://oss/x.jpg"]}}
clock = [0.0]
tick = lambda: clock[0]
_orig = (M._session, M._key)
M._key = lambda: "k"
try:
    out = Path(tempfile.mkdtemp())
    # 1) 第一次 ReadTimeout → 自动重试一次成功；读超时默认 150s
    clock[0] = 0
    M._session = s = FakeMM([requests.exceptions.ReadTimeout(), OK], [JPG], clock)
    p = M.generate_image("机械月牙", out, _clock=tick)
    assert p.exists() and len(s.post_timeouts) == 2, s.post_timeouts
    assert s.post_timeouts[0][1] == 150, s.post_timeouts
    # 2) 两次都超时 → 最多 2 次，抛 network+timeout
    clock[0] = 0
    M._session = s = FakeMM([requests.exceptions.ReadTimeout(), requests.exceptions.ReadTimeout(), OK], [JPG], clock)
    try:
        M.generate_image("x", out, _clock=tick); assert False
    except M.MediaError as e:
        assert e.network and e.timeout and len(s.post_timeouts) == 2
    # 3) 总时长上限：第一次耗掉 150s+ → 第二次读超时被压到剩余预算内；剩余不足就不再重试
    clock[0] = 0
    M._session = s = FakeMM([requests.exceptions.ReadTimeout(), OK], [JPG], clock, cost=150)
    M.generate_image("x", out, _clock=tick, total=190)
    assert s.post_timeouts[1][1] <= 190 - 150, s.post_timeouts
    clock[0] = 0
    M._session = s = FakeMM([requests.exceptions.ReadTimeout(), OK], [JPG], clock, cost=175)
    try:
        M.generate_image("x", out, _clock=tick, total=190); assert False
    except M.MediaError as e:
        assert e.timeout and len(s.post_timeouts) == 1                  # 只剩 15s → 不重试
    # 4) 业务错误（敏感）不重试
    clock[0] = 0
    M._session = s = FakeMM([{"base_resp": {"status_code": 1026, "status_msg": "sensitive"}}, OK], [JPG], clock)
    try:
        M.generate_image("x", out, _clock=tick); assert False
    except M.MediaError as e:
        assert e.sensitive and not e.network and len(s.post_timeouts) == 1
    # 5) 下载图片网络失败也重试一次
    clock[0] = 0
    M._session = s = FakeMM([OK], [requests.ConnectionError(), JPG], clock)
    assert M.generate_image("x", out, _clock=tick).exists() and s.get_n == 2
finally:
    M._session, M._key = _orig
print("生图：网络错误重试一次 / 读超时 150s / 总时长封顶 / 业务错误不重试 / 下载重试 ✅")

# ---- ImageGen：失败话术如实、退额度、不重复说「在画了」 ----
assert failure_text(M.MediaError("网络错误 ReadTimeout", network=True, timeout=True)) == "这次没画出来，网络超时了，稍后再让我画一次"
assert "网络" in failure_text(M.MediaError("网络错误 ConnectionError", network=True))
assert failure_text(M.MediaError("x", sensitive=True)).startswith("这个题材")
assert failure_text(RuntimeError("x")) == "这张没画出来，等会儿再试试吧"

sent = []
calls = []
def gen_fail(prompt, out_dir):
    calls.append(prompt)
    raise M.MediaError("网络错误 ReadTimeout", network=True, timeout=True)
cnt = DailyCounter(Path(tempfile.mkdtemp()) / "q.json")
g = ImageGen(lambda s: True, lambda s: True, lambda s: False, sent.append, counter=cnt, out_dir=Path(tempfile.mkdtemp()),
             gen_fn=gen_fail, async_mode=False)
g.daily = 1
said = g.start("r@chatroom", "u", "机械月牙", ack_now=True)
texts = [r.content for r in sent]
assert said == "" and len(calls) == 1
assert sum(1 for t in texts if "在画" in t or "画着" in t) == 1, texts         # 「在画了」只说一次
assert texts[-1] == "这次没画出来，网络超时了，稍后再让我画一次", texts
assert not any(r.image_path for r in sent)
# 额度已退：daily=1 时还能再画
sent.clear()
g.gen_fn = lambda prompt, out_dir: (_ for _ in ()).throw(RuntimeError("boom"))
g.start("r@chatroom", "u", "机械月牙", ack_now=True)
assert not any("画了 1 张" in r.content for r in sent), [r.content for r in sent]
print("ImageGen：失败如实说网络超时 / 退额度 / 「在画了」不重复 ✅")

# ---- gewe 发送类：建连失败（请求没发出）重试；ReadTimeout / 中途断开不重试 ----
class FakeSess:
    def __init__(self, errs): self.errs, self.n = list(errs), 0
    def post(self, *a, **k):
        self.n += 1
        if self.errs:
            raise self.errs.pop(0)
        class R:
            status_code = 200
            def json(self): return {"ret": 200, "data": {"ok": 1}}
        return R()

def refused():
    nc = U.NewConnectionError(None, "Failed to establish a new connection: [Errno 111] Connection refused")
    return requests.ConnectionError(U.MaxRetryError(None, "/x", reason=nc))

def reset():
    return requests.ConnectionError(U.ProtocolError("Connection aborted.", ConnectionResetError(104, "reset")))

assert G._never_sent(refused()) and G._never_sent(requests.exceptions.ConnectTimeout())
assert not G._never_sent(requests.exceptions.ReadTimeout()) and not G._never_sent(reset())
_orig = (G._session, G.get_app_id, G._token, G.requests.Session, G.time.sleep)
try:
    G.get_app_id = lambda: "app"; G._token = lambda: "t"; G.time.sleep = lambda s: None
    holder = {}
    G.requests.Session = lambda: holder["s"]
    holder["s"] = G._session = FakeSess([refused()])
    assert G.call("/gewe/v2/api/message/postText", {}) == {"ok": 1} and holder["s"].n == 2   # 拒绝连接 → 重试
    for err in (requests.exceptions.ReadTimeout(), reset()):
        holder["s"] = G._session = FakeSess([err])
        try:
            G.call("/gewe/v2/api/message/postText", {}); assert False
        except G.GeweError as e:
            assert holder["s"].n == 1 and "network error" in str(e)                          # 可能已发出 → 不重试
    holder["s"] = G._session = FakeSess([reset()])
    assert G.call("/gewe/v2/api/login/checkOnline", {}) == {"ok": 1}                          # 查询类照旧重试
finally:
    G._session, G.get_app_id, G._token, G.requests.Session, G.time.sleep = _orig
print("gewe 发送：建连失败重试 / ReadTimeout、中途断开不重试防重复 ✅")
