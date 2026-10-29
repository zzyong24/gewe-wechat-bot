"""语音消息链路：豆包 TTS 的 PCM（s16le 单声道）→ pysilk 编腾讯 silk → relay 托管拿公网 URL → gewe postVoice。

仅提供能力，不接入自动回复。真发与否由调用方负责，务必只发白名单。
时长：voiceDuration(ms) = PCM 字节数 ÷ (采样率×2) × 1000（24kHz 时即 ÷48000×1000）。
"""
from __future__ import annotations

import io
from pathlib import Path


def pcm_duration_ms(pcm: bytes, sample_rate: int = 24000) -> int:
    return len(pcm) * 1000 // (sample_rate * 2)


def pcm_to_silk(pcm_path: str | Path, silk_path: str | Path | None = None, sample_rate: int = 24000) -> tuple[Path, int]:
    """返回 (silk 路径, 时长毫秒)。"""
    import pysilk
    pcm = Path(pcm_path).read_bytes()
    if not pcm:
        raise ValueError("空 PCM")
    out = io.BytesIO()
    pysilk.encode(io.BytesIO(pcm), out, sample_rate, sample_rate, tencent=True)
    silk_path = Path(silk_path) if silk_path else Path(pcm_path).with_suffix(".silk")
    silk_path.write_bytes(out.getvalue())
    return silk_path, pcm_duration_ms(pcm, sample_rate)


def send_voice_pcm(to_wxid: str, pcm_path: str | Path, sample_rate: int = 24000) -> dict:
    """PCM → silk → 上传 → postVoice。返回 gewe data（含 msgId/newMsgId）。"""
    import gewe_client as g
    import imagehost
    silk, ms = pcm_to_silk(pcm_path, sample_rate=sample_rate)
    return g.post_voice(to_wxid, imagehost.upload_silk(silk), ms)
