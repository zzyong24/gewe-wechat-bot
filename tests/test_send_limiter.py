"""全局发送限速：每分钟上限 / 同群间隔 2~5 秒 / 私聊不受群间隔 / 环境变量可配 / 0=关闭 / 超时报错 /
接进 gewe_client.call（只限发送类接口）。假时钟，不联网。"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import send_limiter
from send_limiter import SendLimiter, RateLimitTimeout


class Clock:
    def __init__(self): self.t = 1000.0; self.slept = 0.0
    def now(self): return self.t
    def sleep(self, s): self.t += s; self.slept += s


def mk(**k):
    c = Clock()
    return SendLimiter(clock=c.now, sleep=c.sleep, rand=k.pop("rand", lambda a, b: b), **k), c


# 每分钟最多 40 条：第 41 条要等到第 1 条满 60 秒
L, c = mk(per_min=40, gap_min=0, gap_max=0)
for i in range(40):
    assert L.acquire(f"wxid_{i}") == 0
w = L.acquire("wxid_x")
assert 59.9 <= w <= 60.0 + 1e-6, w
print("每分钟上限 ✅")

# 同一个群：相邻两条间隔 2~5 秒（这里 rand 取上限 5）；不同群互不影响；私聊不受群间隔
L, c = mk(per_min=40, gap_min=2, gap_max=5)
R1, R2 = "20000000001@chatroom", "20000000002@chatroom"
assert L.acquire(R1) == 0
assert L.acquire(R2) == 0
assert L.acquire("wxid_a") == 0
w = L.acquire(R1); assert 4.9 <= w <= 5.0 + 1e-6, w
L2, c2 = mk(per_min=40, gap_min=2, gap_max=5, rand=lambda a, b: a)
L2.acquire(R1); assert 1.9 <= L2.acquire(R1) <= 2.0 + 1e-6
print("群间隔 2~5 秒 / 不同群互不影响 / 私聊不限间隔 ✅")

# 间隔真的是随机取 [min, max]
import random
L3 = SendLimiter(per_min=0, gap_min=2, gap_max=5)
gaps = []
for _ in range(50):
    c3 = Clock(); L3 = SendLimiter(per_min=0, gap_min=2, gap_max=5, clock=c3.now, sleep=c3.sleep)
    L3.acquire(R1); gaps.append(L3.acquire(R1))
assert all(1.9 <= g <= 5.0 + 1e-6 for g in gaps) and max(gaps) - min(gaps) > 0.5
print("随机间隔 ✅")

# 0 = 关闭
L, c = mk(per_min=0, gap_min=0, gap_max=0)
assert all(L.acquire(R1) == 0 for _ in range(200))
print("关闭 ✅")

# 等太久 → RateLimitTimeout
L, c = mk(per_min=1, gap_min=0, gap_max=0, max_wait=10)
L.acquire("wxid_a")
try:
    L.acquire("wxid_b"); raise AssertionError("应超时")
except RateLimitTimeout:
    pass
print("等待超时 ✅")

# 环境变量
os.environ.update(BOT_RATE_PER_MIN="7", BOT_RATE_GROUP_GAP_MIN="1", BOT_RATE_GROUP_GAP_MAX="3", BOT_RATE_MAX_WAIT="9")
send_limiter.reset_default()
d = send_limiter.default()
assert (d.per_min, d.gap_min, d.gap_max, d.max_wait) == (7, 1.0, 3.0, 9.0)
for k in ("BOT_RATE_PER_MIN", "BOT_RATE_GROUP_GAP_MIN", "BOT_RATE_GROUP_GAP_MAX", "BOT_RATE_MAX_WAIT"):
    del os.environ[k]
send_limiter.reset_default()
d = send_limiter.default()
assert (d.per_min, d.gap_min, d.gap_max) == (40, 2.0, 5.0)
print("环境变量 / 默认 40 条·2~5 秒 ✅")

# 接进 gewe_client.call：发送类接口过限速，查询类不过
os.environ["GEWE_TOKEN"] = "fake-token-for-test"
import gewe_client as g
seen = []
class Resp:
    status_code = 200
    def json(self): return {"ret": 200, "data": {}}
class Sess:
    def post(self, url, json=None, headers=None, timeout=None): seen.append(url); return Resp()
g._session = Sess()
g.get_app_id = lambda: "wx_demo_appid"
calls = []
class Spy(SendLimiter):
    def acquire(self, to): calls.append(to); return 0.0
send_limiter._default = Spy(per_min=0, gap_min=0, gap_max=0)
g.post_text("20000000001@chatroom", "hi")
g.get_chatroom_info("20000000001@chatroom")
assert calls == ["20000000001@chatroom"] and len(seen) == 2, (calls, seen)
class Boom(SendLimiter):
    def acquire(self, to): raise RateLimitTimeout("x")
send_limiter._default = Boom(per_min=0)
try:
    g.post_text("wxid_a", "hi"); raise AssertionError("应抛 GeweError")
except g.GeweError:
    pass
assert len(seen) == 2                     # 限速超时的那条没有发出去
send_limiter.reset_default()
print("gewe_client 接入 ✅")
print("test_send_limiter 全部通过")
