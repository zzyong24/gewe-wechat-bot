"""中转端长轮询单测（本地 Flask test_client，临时目录，不连服务器）。"""
import json, os, shutil, sys, tempfile, threading, time
from pathlib import Path
src = Path(__file__).resolve().parent.parent / "relay" / "relay.py"
tmp = Path(tempfile.mkdtemp()); shutil.copy(src, tmp / "relay.py"); (tmp / ".secret").write_text("s3cret")
sys.path.insert(0, str(tmp)); os.chdir(tmp)
import relay
c = relay.app.test_client()
t = time.time(); assert c.get("/pull?secret=s3cret&after=0&wait=0.5").json == [] and 0.45 < time.time() - t < 2
def later():
    time.sleep(0.3); relay.app.test_client().post("/callback/s3cret", data=json.dumps({"a": 1}))
threading.Thread(target=later).start()
t = time.time(); rows = c.get("/pull?secret=s3cret&after=0&wait=10").json
assert [r["id"] for r in rows] == [1] and time.time() - t < 1.5, time.time() - t     # 入库即返回，不等满 10 秒
t = time.time(); assert len(c.get("/pull?secret=s3cret&after=0&wait=10").json) == 1 and time.time() - t < 0.2
assert c.get("/pull?secret=s3cret&after=1").json == []                             # 不带 wait = 旧行为
assert c.get("/pull?secret=bad&after=0&wait=5").status_code == 401
assert relay.PULL_WAIT_MAX == 25
print("长轮询单测通过 ✅")
