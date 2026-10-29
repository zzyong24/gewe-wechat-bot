#!/usr/bin/env python3
"""手动登录第 1 步：向 gewe 取登录二维码，存成 data/login_qr.png，用要当机器人的那个微信扫码。
  python scripts/login_qr.py [regionId]        # regionId 默认 110000，按 gewe 文档选离你近的地区
- 第一次登录 appId 传空串，gewe 会分配新 appId；以后重新登录要带上原 appId（否则等于换新设备，更容易触发风控）。
- 只打印 ret/msg/appId/uuid，不打印 token。程序里没有任何自动重登：掉线后由人来跑这两个脚本。
"""
import base64, io, json, os, sys, time
from pathlib import Path
import requests

BASE = Path(__file__).resolve().parent.parent
APPID_FILE = Path(os.environ.get("GEWE_APPID_FILE", BASE / "data" / "gewe_appid.txt"))
STATE = BASE / "data" / "gewe_login_state.json"
API = os.environ.get("GEWE_BASE_URL", "https://api.geweapi.com").rstrip("/") + "/gewe/v2/api"


def main():
    tok = os.environ.get("GEWE_TOKEN", "").strip()
    if not tok:
        sys.exit("缺少环境变量 GEWE_TOKEN")
    appid = APPID_FILE.read_text().strip() if APPID_FILE.exists() else ""
    body = {"appId": appid, "type": "ipad", "regionId": sys.argv[1] if len(sys.argv) > 1 else "110000", "proxyIp": ""}
    r = requests.post(API + "/login/getLoginQrCode", json=body,
                      headers={"X-GEWE-TOKEN": tok, "Content-Type": "application/json"}, timeout=30).json()
    d = r.get("data") or {}
    print("ret", r.get("ret"), "msg", r.get("msg"), "appId", d.get("appId"), "uuid", d.get("uuid"))
    if r.get("ret") != 200 or not d.get("uuid"):
        sys.exit("取二维码失败，看上面的 msg")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    out = BASE / "data" / "login_qr.png"
    b = d.get("qrImgBase64") or ""
    if not b:
        sys.exit("接口没返回 qrImgBase64，请按 gewe 文档用 qrData 自行生成二维码")
    from PIL import Image
    Image.open(io.BytesIO(base64.b64decode(b.split(",", 1)[-1]))).save(out)
    STATE.write_text(json.dumps({"appId": d.get("appId") or appid, "uuid": d["uuid"],
                                 "fetched_at": time.strftime("%F %T"), "regionId": body["regionId"]}, ensure_ascii=False))
    print("二维码已保存：", out, "——二维码约 150 秒内有效，尽快扫码，然后运行 scripts/login_check.py")


if __name__ == "__main__":
    main()
