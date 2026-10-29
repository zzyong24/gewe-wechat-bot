#!/usr/bin/env python3
"""gewe 薄回调中转端：只入库、供 box 轮询拉取，不做业务解析。"""
import hmac, json, logging, os, re, secrets, sqlite3, threading, time
from flask import Flask, request, jsonify, abort, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "queue.db")
DB_PATH = os.environ.get("RELAY_DB", DB_PATH)
SECRET = os.environ.get("RELAY_SECRET", "").strip()      # 优先读环境变量；否则读同目录 .secret（一行随机串，chmod 600）
if not SECRET:
    with open(os.path.join(BASE, ".secret")) as f:
        SECRET = f.read().strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("relay")
# 屏蔽 werkzeug 访问日志（URL 里有 secret）
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)
_lock = threading.Lock()
_new = threading.Condition()     # 长轮询：有新回调入库就唤醒挂起的 /pull
_latest = {"id": 0}
PULL_WAIT_MAX = 25               # /pull?wait=N 最多挂 N 秒（box 端 requests 超时须大于它）

def db():
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.execute("PRAGMA journal_mode=WAL")
    return c

with db() as c:
    c.execute("CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY AUTOINCREMENT, recv_time REAL NOT NULL, raw_json TEXT NOT NULL)")
    _latest["id"] = c.execute("SELECT COALESCE(MAX(id), 0) FROM queue").fetchone()[0]

def ok(s):
    return bool(s) and hmac.compare_digest(str(s), SECRET)

def mask(s):
    return (s[:4] + "***") if s else "<none>"

_SENSITIVE_KEYS = {"token", "secret", "x-gewe-token"}
_RAW_RE = re.compile(r'("(?:token|secret|x-gewe-token)"\s*:\s*)"[^"]*"', re.I)

def _scrub(obj):
    """递归把键名为 token/secret/X-GEWE-TOKEN（大小写不敏感）的值替换成 ***。"""
    if isinstance(obj, dict):
        return {k: ("***" if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS else _scrub(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj

def redact_raw(raw):
    try:
        return json.dumps(_scrub(json.loads(raw)), ensure_ascii=False)
    except (ValueError, TypeError):
        return _RAW_RE.sub(r'\1"***"', raw)   # 非 JSON 兜底

@app.post("/callback/<secret>")
def callback(secret):
    if not ok(secret):
        log.warning("callback bad secret %s from %s", mask(secret), request.remote_addr)
        abort(404)
    raw = redact_raw(request.get_data(as_text=True) or "")
    with _lock, db() as c:
        cur = c.execute("INSERT INTO queue (recv_time, raw_json) VALUES (?, ?)", (time.time(), raw))
        mid = cur.lastrowid
    with _new:
        _latest["id"] = max(_latest["id"], mid)
        _new.notify_all()
    log.info("callback stored id=%s len=%d from %s", mid, len(raw), request.remote_addr)
    return jsonify(ret=200, msg="ok")

@app.get("/pull")
def pull():
    if not ok(request.args.get("secret")):
        abort(401)
    after = int(request.args.get("after", 0) or 0)
    limit = max(1, min(int(request.args.get("limit", 200) or 200), 1000))
    wait = max(0.0, min(float(request.args.get("wait", 0) or 0), PULL_WAIT_MAX))

    def fetch():
        with db() as c:
            return c.execute("SELECT id, recv_time, raw_json FROM queue WHERE id > ? ORDER BY id ASC LIMIT ?", (after, limit)).fetchall()
    rows = fetch()
    if not rows and wait > 0:      # 长轮询：没新消息就挂住，入库即唤醒，最多 wait 秒
        deadline = time.time() + wait
        with _new:
            while _latest["id"] <= after and time.time() < deadline:
                _new.wait(timeout=max(0.0, deadline - time.time()))
        rows = fetch()
    return jsonify([{"id": r[0], "recv_time": r[1], "raw_json": r[2]} for r in rows])

# ---- 临时图床：给 gewe postImage 提供公网 imgUrl；也托管 postVoice 的 silk 语音（腾讯 silk 头 \x02#!SILK_V3）----
# 上传需 secret；下载靠服务端生成的不可猜文件名（gewe 拉图不能带鉴权）。文件 IMG_TTL 秒后清理，总量封顶。
IMG_DIR = os.environ.get("RELAY_IMG_DIR", os.path.join(BASE, "img"))
IMG_TTL = int(os.environ.get("IMG_TTL", 48 * 3600))
IMG_MAX_BYTES = 5 * 1024 * 1024
IMG_DIR_CAP = 200 * 1024 * 1024
_IMG_NAME = re.compile(r"^[A-Za-z0-9_-]{22,64}\.(png|jpg|silk)$")
_MAGIC = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg", b"\x02#!SILK_V3": "silk"}
os.makedirs(IMG_DIR, mode=0o700, exist_ok=True)

def _img_cleanup():
    now, files = time.time(), []
    for n in os.listdir(IMG_DIR):
        fp = os.path.join(IMG_DIR, n)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if now - st.st_mtime > IMG_TTL:
            os.remove(fp)
        else:
            files.append((st.st_mtime, st.st_size, fp))
    total = sum(f[1] for f in files)
    for _, size, fp in sorted(files):          # 超总量从最旧删起
        if total <= IMG_DIR_CAP:
            break
        os.remove(fp); total -= size

@app.post("/upload/<secret>")
def upload(secret):
    if not ok(secret):
        abort(404)
    data = request.get_data(cache=False)
    if not data or len(data) > IMG_MAX_BYTES:
        abort(413)
    ext = next((e for m, e in _MAGIC.items() if data.startswith(m)), None)
    if not ext:
        abort(415)
    _img_cleanup()
    name = f"{secrets.token_urlsafe(24)}.{ext}"
    with open(os.path.join(IMG_DIR, name), "wb") as f:
        f.write(data)
    log.info("img stored %s bytes=%d", name[:6] + "***", len(data))
    return jsonify(ret=200, path=f"/img/{name}", ttl=IMG_TTL)

@app.get("/img/<name>")
def img(name):
    if not _IMG_NAME.match(name):
        abort(404)
    if name.endswith(".silk"):
        return send_from_directory(IMG_DIR, name, max_age=3600, mimetype="audio/silk")
    return send_from_directory(IMG_DIR, name, max_age=3600)

@app.get("/health")
def health():
    if not ok(request.args.get("secret")):
        abort(401)
    with db() as c:
        cnt, mx = c.execute("SELECT COUNT(*), MAX(id) FROM queue").fetchone()
    return jsonify(count=cnt, max_id=mx or 0, now=time.strftime("%Y-%m-%d %H:%M:%S %z"))

@app.get("/")
def root():
    return "ok"

if __name__ == "__main__":
    from waitress import serve
    host, port = os.environ.get("RELAY_HOST", "0.0.0.0"), int(os.environ.get("RELAY_PORT", "8787"))
    log.info("gewe-relay listening on %s:%s", host, port)
    serve(app, host=host, port=port, threads=8)
