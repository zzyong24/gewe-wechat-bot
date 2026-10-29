#!/usr/bin/env python3
"""端到端冒烟测试（全本地、模拟消息，不连 gewe、不调模型、不发任何微信消息）：
  1) 起中转 relay（随机端口、临时目录）：gewe 地址验证包 + 一条模拟群消息 → /callback/<secret>，错 secret 被拒，/pull 拉得到；
  2) 起 poller（dry-run，BOT_SEND_ENABLED=0）从本地 relay 拉取 → normalize → router，日志里看到「收到」「路由轨迹」；
  3) 起单机版回调服务 callback_server（BOT_OFFLINE=1）：带 secret 的 POST 200 并落盘 inbox，错 secret 404。
  python scripts/smoke_test.py
"""
import json, os, secrets, signal, socket, subprocess, sys, tempfile, time
from pathlib import Path
import requests

BASE = Path(__file__).resolve().parent.parent
PY = sys.executable
BOT, ROOM = "wxid_bot_demo", "20000000002@chatroom"


def port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait_http(url, t=15):
    end = time.time() + t
    while time.time() < end:
        try:
            requests.get(url, timeout=1); return
        except requests.RequestException:
            time.sleep(0.2)
    raise SystemExit(f"服务没起来：{url}")


def msg(i, text):
    return {"TypeName": "AddMsg", "Appid": "wx_demo_appid", "Wxid": BOT,
            "Data": {"NewMsgId": 900000 + i, "MsgId": i, "MsgType": 1, "FromUserName": {"string": ROOM},
                     "ToUserName": {"string": BOT}, "Content": {"string": f"wxid_alice:\n{text}"},
                     "CreateTime": int(time.time()), "MsgSource": f"<msgsource><atuserlist>{BOT}</atuserlist></msgsource>"}}


def env(tmp, **k):
    e = {x: v for x, v in os.environ.items() if x not in ("GEWE_TOKEN", "MINIMAX_API_KEY", "AMAP_KEY", "DOUBAO_TTS_API_KEY",
                                                          "BWS_ACCESS_TOKEN", "BOT_SEND_ENABLED")}
    e.update(BOT_DATA_DIR=str(tmp / "data"), BOT_SEND_ALLOW=ROOM, BOT_TODO_INBOX=str(tmp / "alerts"),
             GEWE_APPID_FILE=str(tmp / "appid.txt"), BOT_RATE_GROUP_GAP_MIN="0", BOT_RATE_GROUP_GAP_MAX="0", **k)
    return e


def main():
    tmp = Path(tempfile.mkdtemp(prefix="wxbot-smoke-")); (tmp / "data").mkdir(); (tmp / "appid.txt").write_text("wx_demo_appid\n")
    procs, ok = [], True
    try:
        # ---- 1) relay ----
        sec, rp = secrets.token_urlsafe(16), port()
        procs.append(subprocess.Popen([PY, str(BASE / "relay" / "relay.py")], cwd=tmp, stdout=open(tmp / "relay.log", "w"),
                                      stderr=subprocess.STDOUT, env=env(tmp, RELAY_SECRET=sec, RELAY_PORT=str(rp),
                                      RELAY_HOST="127.0.0.1", RELAY_DB=str(tmp / "queue.db"), RELAY_IMG_DIR=str(tmp / "img"))))
        R = f"http://127.0.0.1:{rp}"
        wait_http(R + "/")
        assert requests.post(f"{R}/callback/wrong", json={"x": 1}, timeout=5).status_code in (401, 403, 404)
        assert requests.post(f"{R}/callback/{sec}", json={"testMsg": "回调地址链接成功"}, timeout=5).status_code == 200
        assert requests.post(f"{R}/callback/{sec}", json=msg(1, "@小助手 功能状态"), timeout=5).status_code == 200
        rows = requests.get(f"{R}/pull", params={"secret": sec, "after": 0}, timeout=10).json()
        rows = rows.get("rows", rows) if isinstance(rows, dict) else rows
        assert len(rows) == 2, rows
        print("① relay：错 secret 被拒 / 验证包 200 / 模拟消息入库 / /pull 拉到 2 条 ✅")

        # ---- 2) poller（dry-run）----
        cfg = tmp / "relay_config.json"
        cfg.write_text(json.dumps({"server_url": R, "secret": sec, "last_pulled_id": 0}))
        pp = subprocess.Popen([PY, "poller.py"], cwd=BASE, stdout=open(tmp / "poller.stdout", "w"), stderr=subprocess.STDOUT,
                              env=env(tmp, RELAY_CONFIG=str(cfg), POLL_WAIT="2", BOT_SEND_ENABLED="0"))
        procs.append(pp)
        log = tmp / "data" / "poller.log"
        end = time.time() + 40
        while time.time() < end and not (log.exists() and "id=2 路由轨迹" in log.read_text(encoding="utf-8")):
            time.sleep(0.5)
        t = log.read_text(encoding="utf-8") if log.exists() else ""
        assert "发送=关闭(simulate)" in t, t[-2000:]
        assert "id=1 gewe 回调地址验证包" in t and "id=2 收到" in t and "id=2 路由轨迹" in t, t[-2000:]
        assert "postText" not in t
        pp.send_signal(signal.SIGTERM); pp.wait(timeout=30)
        assert json.loads(cfg.read_text())["last_pulled_id"] == 2
        print("② poller：dry-run 启动 / 跳过验证包 / 模拟群消息进路由 / 没有真发 / 优雅退出并记住 last_pulled_id ✅")

        # ---- 3) 单机回调服务 ----
        cs, cp = secrets.token_urlsafe(16), port()
        procs.append(subprocess.Popen([PY, "callback_server.py"], cwd=BASE, stdout=open(tmp / "callback.log", "w"),
                                      stderr=subprocess.STDOUT, env=env(tmp, CALLBACK_SECRET=cs, PORT=str(cp), BOT_OFFLINE="1")))
        C = f"http://127.0.0.1:{cp}"
        wait_http(C + "/healthz")
        assert requests.post(f"{C}/callback/wrong", json=msg(3, "hi"), timeout=5).status_code in (401, 403, 404)
        t0 = time.time()
        assert requests.post(f"{C}/callback/{cs}", json=msg(4, "@小助手 今日发言"), timeout=5).status_code == 200
        assert time.time() - t0 < 3, "回调要在 3 秒内返回"
        time.sleep(1.5)
        inbox = "".join(f.read_text(encoding="utf-8") for f in (tmp / "data" / "inbox").glob("*.jsonl"))
        assert "今日发言" in inbox, "inbox 没落盘"
        cl = (tmp / "callback.log").read_text(encoding="utf-8")
        assert "routed 900004" in cl and "'sent': True" not in cl, cl[-1500:]
        print("③ callback_server：错 secret 被拒 / 3 秒内 200 / 原始回调落盘 inbox / 路由出回复但 sent=False ✅")
    except AssertionError as e:
        ok = False; print("❌ 冒烟失败：", e)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=10)
                except subprocess.TimeoutExpired: p.kill()
    print("冒烟测试", "全部通过 ✅" if ok else f"失败，日志在 {tmp}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
