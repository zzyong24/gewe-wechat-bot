"""按群并发队列：同群保序、不同群并行、排队耗时、异常不影响后续、drain；以及 router 在多线程下的去重/配额。"""
import sys, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_queue import SessionQueues

q = SessionQueues(max_sessions=4, idle_exit=0.5)
out, lock = [], threading.Lock()
def job(key, i, dur):
    def f(queued):
        time.sleep(dur)
        with lock:
            out.append((key, i, queued))
    return f

t0 = time.time()
for i in range(5):
    q.submit("A", job("A", i, 0.1))       # A 群 5 条，每条 0.1s → 串行 0.5s
q.submit("B", job("B", 0, 0.3))           # B 群 1 条 0.3s → 与 A 并行
assert q.drain(5)
el = time.time() - t0
assert [i for k, i, _ in out if k == "A"] == [0, 1, 2, 3, 4]               # 同群保序
assert el < 0.75, el                                                         # 不同群并行（串行要 0.8s）
assert next(qd for k, i, qd in out if k == "A" and i == 4) >= 0.35            # 第 5 条排队约 0.4s
b_done = [n for n, (k, i, _) in enumerate(out) if k == "B"][0]
assert b_done < 4                                                            # B 不用等 A 全部做完
print("同群保序 / 不同群并行 / 排队耗时 ✅")

out.clear()
q.submit("C", lambda qd: 1 / 0)
q.submit("C", job("C", 1, 0))
assert q.drain(2) and out == [("C", 1, out[0][2])]
print("单条异常不影响后续 ✅")

time.sleep(1.2)
assert not q._workers, q._workers                                            # 空闲线程退出
q.submit("D", job("D", 0, 0)); assert q.drain(2) and out[-1][0] == "D"     # 退出后再来消息自动重建
print("空闲退出 / 自动重建 ✅")

# router：多线程同时 dispatch 同一条消息只处理一次；房间配额计数不丢
from tests.helpers import Bot, raw, BOT, ROOM
from models import normalize
b = Bot()
m = raw("wxid_a", "@小助手\u2005在吗", [BOT])
res = []
ths = [threading.Thread(target=lambda: res.append(b.r.dispatch(normalize(m)))) for _ in range(8)]
[t.start() for t in ths]; [t.join() for t in ths]
assert sum(1 for r in res if r and r[0].get("feature") != "<dedup>") == 1
b.r.room_policy = {ROOM: {"daily_reply_cap": 10_000, "user_cooldown_sec": 0}}
msgs = [normalize(raw(f"wxid_{i % 5}", "@小助手\u2005hi", [BOT])) for i in range(40)]
ths = [threading.Thread(target=b.r.dispatch, args=(x,)) for x in msgs]
[t.start() for t in ths]; [t.join() for t in ths]
assert b.r._pstate["count"][ROOM] == 40, b.r._pstate["count"]
print("router 多线程去重 / 配额计数 ✅")

# speech_stats 多线程写入不报错
from features.speech_stats import SpeechStats
import tempfile
ss = SpeechStats(Path(tempfile.mkdtemp()) / "s.db")
errs = []
def w(i):
    try:
        ss.record(normalize(raw("wxid_a", f"x{i}")))
    except Exception as e:
        errs.append(e)
ths = [threading.Thread(target=w, args=(i,)) for i in range(50)]
[t.start() for t in ths]; [t.join() for t in ths]
assert not errs and ss.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 50
print("发言统计多线程写入 ✅")
print("test_session_queue 全部通过")
