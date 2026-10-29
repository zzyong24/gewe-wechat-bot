#!/usr/bin/env python3
"""手动登录第 2 步：扫码后轮询 checkLogin（最多 5 分钟）。登录成功后：
  1) appId 有变化就写回 GEWE_APPID_FILE（gewe_client 每次调用都实时读这个文件，不用重启）；
  2) 可选 --set-callback：把 gewe 回调地址设成 .relay_config.json 的 server_url + /callback/<secret>。
  python scripts/login_check.py [--set-callback]
BOT_WXID（可选）：登录上来的 wxid 不是它就报警退出，不改任何配置（防扫错号）。
日志写 data/login_check.log；token / secret 不打印。
"""
import json, os, shutil, sys, time
from pathlib import Path
import requests

BASE = Path(__file__).resolve().parent.parent
APPID_FILE = Path(os.environ.get("GEWE_APPID_FILE", BASE / "data" / "gewe_appid.txt"))
STATE = BASE / "data" / "gewe_login_state.json"
RELAY_CONFIG = Path(os.environ.get("RELAY_CONFIG", BASE / ".relay_config.json"))
LOG = BASE / "data" / "login_check.log"
API = os.environ.get("GEWE_BASE_URL", "https://api.geweapi.com").rstrip("/") + "/gewe/v2/api"
EXPECT = os.environ.get("BOT_WXID", "").strip()


def log(s):
    line = time.strftime("%F %T") + " " + s
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    tok = os.environ.get("GEWE_TOKEN", "").strip()
    if not tok:
        sys.exit("缺少环境变量 GEWE_TOKEN")
    H = {"X-GEWE-TOKEN": tok, "Content-Type": "application/json"}

    def post(p, b):
        return requests.post(API + p, json=b, headers=H, timeout=30).json()

    st = json.loads(STATE.read_text())
    app, uuid = st["appId"], st["uuid"]
    end, li = time.time() + 300, {}
    while time.time() < end:
        try:
            r = post("/login/checkLogin", {"appId": app, "uuid": uuid, "proxyIp": "", "captchCode": ""})
            d = r.get("data") or {}
            li = d.get("loginInfo") or {}
            log(f"ret={r.get('ret')} msg={r.get('msg')} status={d.get('status')} wxid={li.get('wxid')}")
            if li.get("wxid"):
                break
            if r.get("ret") != 200 and "验证" in str(r.get("msg")):
                log("需要验证：看手机上是验证码还是新设备确认")
        except Exception as e:
            log(f"error {type(e).__name__}")
        time.sleep(5)
    wx = li.get("wxid")
    if not wx:
        log("5 分钟内没登录成功"); sys.exit(1)
    if EXPECT and wx != EXPECT:
        log(f"ALERT 登录的 wxid={wx} 不是 BOT_WXID={EXPECT}，不改 appId / 回调"); sys.exit(1)
    log(f"登录成功 wxid={wx}")
    old = APPID_FILE.read_text().strip() if APPID_FILE.exists() else ""
    if old != app:                       # 重新登录后 appId 可能会变：最常见的坑（不改就会 checkOnline=False、发消息 500）
        if old:
            shutil.copy(APPID_FILE, f"{APPID_FILE}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        APPID_FILE.parent.mkdir(parents=True, exist_ok=True)
        APPID_FILE.write_text(app + "\n")
        log(f"appId 变更 {old or '(空)'} -> {app}，已写回 {APPID_FILE}")
    if "--set-callback" in sys.argv:
        cfg = json.loads(RELAY_CONFIG.read_text())
        url = cfg["server_url"].rstrip("/") + "/callback/" + cfg["secret"]
        r = post("/tools/setCallback", {"token": tok, "callbackUrl": url})
        log(f"setCallback -> {cfg['server_url']}/callback/*** ret={r.get('ret')} msg={r.get('msg')}")
    r = post("/login/checkOnline", {"appId": app})
    log(f"checkOnline ret={r.get('ret')} data={r.get('data')}")


if __name__ == "__main__":
    main()
