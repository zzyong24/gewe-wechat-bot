"""gewe 回调接收端（薄）。

POST /callback/<CALLBACK_SECRET>     ← gewe 控制台填这个（gewe 推送无法自定义 header，用 secret path）
POST /callback  + Header X-Callback-Secret: <CALLBACK_SECRET>   ← 本地调试用
GET  /healthz

收到即：原始 JSON 追加落盘 data/inbox/YYYY-MM-DD.jsonl → 立即返回 200（gewe 要求 3 秒内）
→ 后台线程 normalize + router.dispatch。

环境变量：CALLBACK_SECRET(必填)、PORT(默认 8787)、BOT_DATA_DIR、
BOT_SEND_ENABLED=1 才真正发送（且仅限 BOT_SEND_ALLOW 白名单）、BOT_OFFLINE=1 本地离线调试。
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, request

from app import DATA_DIR, build_router
from models import normalize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("callback")
TZ = ZoneInfo("Asia/Shanghai")
SECRET = os.environ.get("CALLBACK_SECRET", "")
INBOX = DATA_DIR / "inbox"
INBOX.mkdir(parents=True, exist_ok=True)



class _RedactSecret(logging.Filter):
    """访问日志里把 secret path 打码，避免 secret 落进日志。"""
    def filter(self, record):
        if SECRET:
            msg = record.getMessage()
            if SECRET in msg:
                record.msg, record.args = msg.replace(SECRET, "<secret>"), ()
        return True


logging.getLogger("werkzeug").addFilter(_RedactSecret())

app = Flask(__name__)
router = None
jobs: "queue.Queue[dict]" = queue.Queue()
_lock = threading.Lock()


def _ok_secret(given: str | None) -> bool:
    return bool(SECRET) and given is not None and hmac.compare_digest(given, SECRET)


def _persist(payload: dict) -> None:
    rec = {"recv_at": datetime.now(TZ).isoformat(timespec="seconds"), "payload": payload}
    path = INBOX / f"{datetime.now(TZ):%Y-%m-%d}.jsonl"
    with _lock, path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _worker():
    while True:
        payload = jobs.get()
        try:
            msg = normalize(payload)
            if msg is None:
                log.info("非消息回调（可能是地址验证包），已落盘不处理")
                continue
            trace = router.dispatch(msg)
            log.info("routed %s room=%s from=%s type=%s -> %s", msg.msg_id, msg.room_id, msg.from_wxid,
                     msg.type_name, trace)
        except Exception:
            log.exception("dispatch failed")


def _receive():
    payload = request.get_json(silent=True, force=True)
    if payload is None:
        payload = {"_raw_text": request.get_data(as_text=True)[:10000]}
    _persist(payload)
    jobs.put(payload)
    return jsonify({"ret": 200})


@app.post("/callback/<secret>")
def callback_path(secret):
    if not _ok_secret(secret):
        abort(404)
    return _receive()


@app.post("/callback")
def callback_header():
    if not _ok_secret(request.headers.get("X-Callback-Secret")):
        abort(401)
    return _receive()


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "queued": jobs.qsize()})


def main():
    global router
    if not SECRET:
        raise SystemExit("请设置环境变量 CALLBACK_SECRET")
    if os.environ.get("BOT_OFFLINE") == "1":   # 本地调试：群成员用模拟数据，不调 gewe
        from simulate import fake_list, fake_info
        router = build_router(fetch_list=fake_list, fetch_info=fake_info, send_enabled=False)
    else:
        router = build_router(send_enabled=os.environ.get("BOT_SEND_ENABLED") == "1")
    threading.Thread(target=_worker, daemon=True).start()
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8787")))


if __name__ == "__main__":
    main()
