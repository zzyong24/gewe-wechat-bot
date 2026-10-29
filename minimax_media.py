"""MiniMax Token Plan 的联网搜索 / 生图接口（与陪聊同一个 MINIMAX_API_KEY；key 不打印、不进日志和异常）。
  搜索：POST https://api.minimaxi.com/v1/coding_plan/search  {"q": ...} → organic[{title, link, snippet, date}]
        （官方 MiniMax-Coding-Plan-MCP 的 web_search 就是调它；docs/probe/probe_minimax.py 实测 200 / 2.1s）
  生图：POST https://api.minimaxi.com/v1/image_generation  model=image-01 → data.image_urls（OSS 临时链接）
        （实测 200 / 17.5s，1024×1024 JPG ~400KB）
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

import requests

BASE = os.environ.get("MINIMAX_MEDIA_BASE", "https://api.minimaxi.com")
_session = requests.Session()


class MediaError(RuntimeError):
    """network：网络层失败（连接失败/超时，没拿到 HTTP 响应）；timeout：其中的超时类（ReadTimeout/ConnectTimeout）。"""
    def __init__(self, msg: str, code=None, sensitive: bool = False, network: bool = False, timeout: bool = False):
        super().__init__(msg)
        self.code, self.sensitive, self.network, self.timeout = code, sensitive, network, timeout


def _key() -> str:
    k = os.environ.get("MINIMAX_API_KEY", "").strip()
    i = k.find("sk-")
    if i > 0:
        k = k[i:].strip()
    if not k:
        raise MediaError("MINIMAX_API_KEY 未设置")
    return k


def configured() -> bool:
    return bool(os.environ.get("MINIMAX_API_KEY", "").strip())


def _post(path: str, body: dict, timeout) -> dict:
    try:
        r = _session.post(BASE + path, json=body, timeout=timeout,
                          headers={"Authorization": "Bearer " + _key(), "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise MediaError(f"网络错误 {type(e).__name__}", network=True,
                         timeout=isinstance(e, requests.Timeout)) from None
    try:
        j = r.json()
    except ValueError:
        raise MediaError(f"非 JSON 响应 http {r.status_code}") from None
    br = j.get("base_resp") or {}
    code = br.get("status_code", 0)
    if r.status_code != 200 or code not in (0, None):
        msg = str(br.get("status_msg") or j.get("message") or "")[:200]
        sens = code in (1026, 1027) or bool(re.search(r"sensitive|敏感|违规|moderation", msg, re.I))
        raise MediaError(f"MiniMax {path} http {r.status_code} code={code} {msg}", code, sens)
    return j


_DOMAIN = re.compile(r"https?://(?:www\.)?([^/]+)")


def search(q: str, limit: int = 5, timeout: float = 10.0) -> list[dict]:
    j = _post("/v1/coding_plan/search", {"q": q[:200]}, timeout)
    out = []
    for o in (j.get("organic") or [])[:limit]:
        link = str(o.get("link") or "")
        m = _DOMAIN.match(link)
        out.append({"title": str(o.get("title") or "")[:80], "snippet": str(o.get("snippet") or "")[:220],
                    "date": str(o.get("date") or "")[:20], "link": link[:200], "site": m.group(1) if m else ""})
    return out


IMAGE_READ_TIMEOUT = float(os.environ.get("BOT_IMAGE_READ_TIMEOUT", "150"))   # 单次生图读超时（10-23 90s 时 ReadTimeout）
IMAGE_TOTAL_SEC = float(os.environ.get("BOT_IMAGE_TOTAL_SEC", "190"))         # 含重试的总时长上限（约 3 分钟）
IMAGE_RETRY_MIN_SEC = 25.0                                                    # 剩余不足这么多就不重试了
log = logging.getLogger("minimax_media")


def generate_image(prompt: str, out_dir: Path, aspect_ratio: str = "1:1", timeout: float | None = None,
                   subject_ref: str | None = None, total: float | None = None, _clock=time.monotonic) -> Path:
    """生成一张图并下载到 out_dir，返回本地路径（jpg）。
    网络错误（连接失败/超时）自动重试一次，总时长不超过 total（默认 BOT_IMAGE_TOTAL_SEC=190s）；单次读超时默认 150s。
    业务错误（敏感词/额度/没返回图）不重试。最终失败抛 MediaError（network/timeout 标记供上层说实话）。
    subject_ref：参考图（公网 URL 或 data:image/jpeg;base64,…）→ image-01 的 subject_reference（官方文档：type 目前只支持
    character 人像主体，用于保持角色一致；不是通用的「在原图上改」）。2026-10-23 本地实测非人像参考图也返回 200，但效果主要取决于 prompt。"""
    read_to = float(timeout if timeout is not None else IMAGE_READ_TIMEOUT)
    total = float(total if total is not None else IMAGE_TOTAL_SEC)
    deadline = _clock() + total
    body = {"model": "image-01", "prompt": prompt[:1200], "aspect_ratio": aspect_ratio,
            "response_format": "url", "n": 1, "prompt_optimizer": True}
    if subject_ref:
        body["subject_reference"] = [{"type": "character", "image_file": subject_ref}]

    def left() -> float:
        return deadline - _clock()

    def with_retry(what: str, fn):
        for attempt in (1, 2):
            budget = min(read_to, left() - 5)
            try:
                return fn(max(budget, 5.0))
            except MediaError as e:
                if not e.network or attempt == 2 or left() < IMAGE_RETRY_MIN_SEC:
                    raise
                log.warning("%s 网络失败（%s），剩余 %.0fs，重试一次", what, e, left())

    j = with_retry("生图请求", lambda t: _post("/v1/image_generation", body, (10, t)))
    urls = ((j.get("data") or {}).get("image_urls") or [])
    if not urls:
        meta = j.get("metadata") or {}
        raise MediaError(f"没有返回图片 metadata={str(meta)[:120]}", sensitive=bool(meta.get("failed_count")))

    def download(t):
        try:
            r = _session.get(urls[0], timeout=(10, min(t, 60)))
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            net = not isinstance(e, requests.HTTPError)
            raise MediaError(f"下载图片失败 {type(e).__name__}", network=net,
                             timeout=isinstance(e, requests.Timeout)) from None
    r = with_retry("下载图片", download)
    if len(r.content) > 5 * 1024 * 1024 or not r.content[:3] in (b"\xff\xd8\xff", b"\x89PN"):
        raise MediaError(f"图片格式/大小不对 ({len(r.content)} 字节)")
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = "png" if r.content[:3] == b"\x89PN" else "jpg"
    p = out_dir / f"gen-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid() % 1000}{int(time.time() * 1000) % 1000:03d}.{ext}"
    p.write_bytes(r.content)
    return p


def asr(wav: bytes, timeout: float = 30.0) -> str:
    """语音识别 asr-1.0（multipart；不收裸 PCM，要 wav）。docs/probe/probe_mm_asr.py 实测 2.2s。"""
    try:
        r = _session.post(BASE + "/v1/speech_to_text", headers={"Authorization": "Bearer " + _key()},
                          data={"model": "asr-1.0", "response_format": "json"},
                          files={"file": ("voice.wav", wav, "audio/wav")}, timeout=timeout)
    except requests.RequestException as e:
        raise MediaError(f"网络错误 {type(e).__name__}") from None
    try:
        j = r.json()
    except ValueError:
        raise MediaError(f"非 JSON 响应 http {r.status_code}") from None
    br = j.get("base_resp") or {}
    if r.status_code != 200 or br.get("status_code", 0) not in (0, None):
        raise MediaError(f"asr http {r.status_code} code={br.get('status_code')} {str(br.get('status_msg'))[:120]}")
    return str(j.get("text") or j.get("result") or "").strip()


TTS_VOICE = os.environ.get("BOT_VOICE_MM_ID", "female-shaonv")     # 少女音（MiniMax 系统音色，年轻自然女声）
TTS_MODEL = os.environ.get("BOT_VOICE_MM_MODEL", "speech-2.8-hd")


def tts_pcm(text: str, timeout: float = 20.0, voice_id: str | None = None, speed: float = 1.0) -> bytes:
    """MiniMax 语音合成 → PCM s16le 24kHz 单声道（与豆包同格式，可直接 voice.pcm_to_silk）。"""
    j = _post("/v1/t2a_v2", {"model": TTS_MODEL, "text": text[:500], "stream": False,
                             "voice_setting": {"voice_id": voice_id or TTS_VOICE, "speed": speed, "vol": 1, "pitch": 0},
                             "audio_setting": {"sample_rate": 24000, "format": "pcm", "channel": 1}}, timeout)
    a = (j.get("data") or {}).get("audio")
    if not a:
        raise MediaError("TTS 没有返回音频")
    return bytes.fromhex(a)


def vision_chat_content(text: str, data_url: str) -> list[dict]:
    """多模态 user content（MiniMax-M3.1-Flash 的 OpenAI 兼容 chat 实测能看图：2026-10-21 4.8s）。"""
    return [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": data_url}}]
