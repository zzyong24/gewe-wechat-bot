"""把本地图片托管成公网 URL，供 gewe postImage 拉取。

gewe 没有「上传图片」接口，postImage 只收 imgUrl。方案：国内服务器 relay（<your-server-ip>:8787）加了临时图床：
  POST /upload/<secret>  (body=图片/语音字节，仅 PNG/JPEG/腾讯 silk，≤5MB) → {"path": "/img/<随机32位名>.png|.silk"}
  GET  /img/<name>       公开读（名字不可猜），48 小时后自动清理、目录总量封顶 200MB
secret / server_url 复用 .relay_config.json（与轮询器同一份），绝不打印。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("RELAY_CONFIG", BASE / ".relay_config.json"))


class ImageHostError(RuntimeError):
    pass


def upload(path: str | Path, timeout: int = 30) -> str:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    base = cfg["server_url"].rstrip("/")
    data = Path(path).read_bytes()
    import time
    for attempt in range(3):                    # 跨境丢包：连接 5s 超时，连接失败重试（重复上传只是多一个临时文件，无副作用）
        try:
            r = requests.post(f"{base}/upload/{cfg['secret']}", data=data, timeout=(5, timeout),
                              headers={"Content-Type": "application/octet-stream"})
            break
        except requests.RequestException as e:      # URL 含 secret，只报类型
            if attempt < 2 and isinstance(e, (requests.exceptions.ConnectTimeout, requests.ConnectionError)) \
                    and not isinstance(e, requests.exceptions.ReadTimeout):
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise ImageHostError(f"上传网络错误: {type(e).__name__}") from None
    if r.status_code != 200:
        raise ImageHostError(f"上传失败 http {r.status_code}")
    # 上传走 server_url（可以是 https）；给 gewe 拉取的公开地址用 public_img_base（gewe 能否拉 https 未验证前保持 http）
    pub = (os.environ.get("BOT_IMG_PUBLIC_BASE") or cfg.get("public_img_base") or base).rstrip("/")
    return pub + r.json()["path"]


SILK_MAGIC = b"\x02#!SILK_V3"


def upload_silk(path: str | Path, timeout: int = 30) -> str:
    """把本地 silk 语音（腾讯 silk 头）传到 relay，返回公网 URL（供 gewe postVoice 的 voiceUrl）。"""
    head = Path(path).read_bytes()[:len(SILK_MAGIC)]
    if head != SILK_MAGIC:
        raise ImageHostError("不是腾讯 silk 文件（缺 \\x02#!SILK_V3 头）")
    url = upload(path, timeout)
    if not url.endswith(".silk"):
        raise ImageHostError("relay 未按 silk 存储（服务器 relay 版本过旧？）")
    return url


def send_image(to_wxid: str, path: str | Path) -> dict:
    import gewe_client as g
    return g.post_image(to_wxid, upload(path))
