"""豆包语音合成 2.0（seed-tts-2.0）→ PCM（s16le 24kHz 单声道），给群语音回复用。
接口/鉴权同 豆包 TTS 官方文档。Key 只从 env DOUBAO_TTS_API_KEY 读，不打印、不落盘、不进日志/异常。
音色：Vivi 2.0 zh_female_vv_uranus_bigtts；env BOT_VOICE_SPEAKER 可改。
用量：每次调用追加到 data/tts_usage.jsonl（便于看免费额度）。
遇到余额/开通/额度类错误：当天停用（不重试），由调用方回退文字。
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import uuid

import requests

ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
RESOURCE_ID = "seed-tts-2.0"
SPEAKER = os.environ.get("BOT_VOICE_SPEAKER", "zh_female_vv_uranus_bigtts")
USAGE_LOG = os.environ.get("BOT_TTS_USAGE_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tts_usage.jsonl"))
BILLING_HINTS = ("balance", "quota", "insufficient", "not activated", "activate", "arrear",
                 "余额", "额度", "欠费", "开通", "resource not granted")
SAMPLE_RATE = 24000


class TTSError(Exception):
    def __init__(self, msg: str, billing: bool = False):
        super().__init__(msg)
        self.billing = billing


_session = requests.Session()
_blocked_day = {"d": ""}           # 计费类错误 → 当天停用


def available() -> bool:
    return bool(os.environ.get("DOUBAO_TTS_API_KEY")) and _blocked_day["d"] != datetime.date.today().isoformat()


def _log(chars, ok, extra):
    rec = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "bot": "gewe-wechat-bot",
           "service": "doubao-tts", "endpoint": ENDPOINT, "resource_id": RESOURCE_ID, "chars": chars,
           "voice": SPEAKER, "ok": ok, "cost": "免费试用"}
    rec.update(extra)
    try:
        os.makedirs(os.path.dirname(USAGE_LOG), exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def synth_pcm(text: str, timeout: float = 15.0, speech_rate: int = 0) -> bytes:
    key = os.environ.get("DOUBAO_TTS_API_KEY")
    if not key:
        raise TTSError("DOUBAO_TTS_API_KEY 未设置")
    if not available():
        raise TTSError("TTS 今日已停用（计费类错误）", billing=True)
    body = {"req_params": {"text": text, "speaker": SPEAKER,
                           "audio_params": {"format": "pcm", "sample_rate": SAMPLE_RATE, "speech_rate": speech_rate}}}
    rid = str(uuid.uuid4())
    headers = {"X-Api-Key": key, "X-Api-Resource-Id": RESOURCE_ID, "X-Api-Request-Id": rid,
               "X-Control-Require-Usage-Tokens-Return": "*", "Content-Type": "application/json"}
    chunks, err, billed, logid = [], None, None, None
    try:
        with _session.post(ENDPOINT, json=body, headers=headers, timeout=timeout, stream=True) as r:
            logid = r.headers.get("X-Tt-Logid")
            if r.status_code != 200:
                txt = r.text[:300]
                try:
                    j = json.loads(txt); err = (j.get("code", r.status_code), j.get("message", txt))
                except ValueError:
                    err = (r.status_code, txt)
            else:
                for raw in r.iter_lines():
                    line = (raw or b"").decode("utf-8", "ignore").strip()
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line:
                        continue
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    if m.get("data"):
                        chunks.append(base64.b64decode(m["data"]))
                    if isinstance(m.get("usage"), dict):
                        billed = m["usage"].get("text_words", billed)
                    if m.get("code", 0) not in (0, 20000000):
                        err = (m.get("code"), m.get("message"))
                        break
    except requests.RequestException as e:
        err = ("network", type(e).__name__)
    extra = {"request_id": rid, "logid": logid, "billed_chars": billed}
    if err or not chunks:
        err = err or ("empty", "未收到音频")
        extra.update(error_code=err[0], error_msg=str(err[1])[:200])
        _log(len(text), False, extra)
        billing = any(h in str(err[1]).lower() for h in BILLING_HINTS)
        if billing:
            _blocked_day["d"] = datetime.date.today().isoformat()
        raise TTSError(f"TTS 失败 code={err[0]} msg={str(err[1])[:120]} logid={logid}", billing=billing)
    _log(len(text), True, extra)
    return b"".join(chunks)
