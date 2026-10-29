"""语音链路单测：本地编码 + mock 上传/postVoice（不联网、不发消息）。"""
import math, struct, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gewe_client, imagehost, voice

tmp = Path(tempfile.mkdtemp())
pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 24000))) for i in range(48000))  # 2s
(tmp / "a.pcm").write_bytes(pcm)
silk, ms = voice.pcm_to_silk(tmp / "a.pcm")
assert ms == 2000 and silk.read_bytes().startswith(imagehost.SILK_MAGIC), (ms, silk.read_bytes()[:10])
calls = {}
imagehost.upload = lambda p, timeout=30: calls.setdefault("up", str(p)) and "http://x/img/abc.silk"
gewe_client.call = lambda path, body, timeout=0: calls.setdefault("call", (path, body)) and {"msgId": 1, "newMsgId": 2}
out = voice.send_voice_pcm("20000000002@chatroom", tmp / "a.pcm")
assert calls["call"] == ("/gewe/v2/api/message/postVoice", {"toWxid": "20000000002@chatroom",
                         "voiceUrl": "http://x/img/abc.silk", "voiceDuration": 2000}) and out["newMsgId"] == 2
(tmp / "x.bin").write_bytes(b"#!AMR\n"); 
try:
    imagehost.upload_silk(tmp / "x.bin"); raise SystemExit("应拒绝非 silk")
except imagehost.ImageHostError:
    pass
print("语音链路单测通过 ✅")
