"""语音转文字 / 识图 / TTS 主备切换。全 mock：不连 gewe、不调真模型、不发消息。"""
import sys, tempfile, time, io, math, struct
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import build_router
from features.settings import FeatureSettings, FEATURES
from features.media_in import quote_image_xml, silk_to_wav, MediaIn, to_data_url
from features.daily_quota import DailyCounter
from features.capabilities import describe
from tests.helpers import MEMBERS, ROOM, BOT, raw
from models import normalize

IMG_XML = '<?xml version="1.0"?><msg><img aeskey="k" cdnmidimgurl="u" length="10"/></msg>'
VOICE_XML = '<msg><voicemsg endflag="1" length="2000" voicelength="1500" aeskey="k"/></msg>'

# ---- 引用图片 XML 提取 ----
ref = ("<msg><appmsg><title>这是啥</title><type>57</type><refermsg><type>3</type><svrid>1</svrid>"
       "<content>wxid_a:\n&lt;?xml version=\"1.0\"?&gt;&lt;msg&gt;&lt;img aeskey=\"k\" cdnmidimgurl=\"u\"/&gt;&lt;/msg&gt;</content>"
       "</refermsg></appmsg></msg>")
x = quote_image_xml(ref)
assert x.startswith("<?xml") and "<img" in x, x
assert quote_image_xml(ref.replace("<type>3</type>", "<type>1</type>")) == "" and quote_image_xml("hi") == ""
print("引用图片 XML 提取 ✅")

# ---- silk 解码：pcm → silk（腾讯头）→ wav ----
import voice
d = Path(tempfile.mkdtemp())
pcm = b"".join(struct.pack("<h", int(8000 * math.sin(i / 10))) for i in range(24000))
(d / "a.pcm").write_bytes(pcm)
silk, ms = voice.pcm_to_silk(d / "a.pcm", d / "a.silk")
assert silk.read_bytes()[:10] == b"\x02#!SILK_V3"
wav = silk_to_wav(silk.read_bytes())
assert wav[:4] == b"RIFF" and len(wav) > 16000, len(wav)
SILK = silk.read_bytes()
print("silk → wav 解码 ✅")

# ---- 图片压缩成 data URL ----
from PIL import Image
b = io.BytesIO(); Image.new("RGB", (3000, 2000), (200, 100, 50)).save(b, "PNG"); PNG = b.getvalue()
du = to_data_url(PNG)
assert du.startswith("data:image/jpeg;base64,")
im = Image.open(io.BytesIO(__import__("base64").b64decode(du.split(",", 1)[1])))
assert max(im.size) == 1024
print("图片 ≤1024px data URL ✅")


# ---- 端到端：router + 陪聊 ----
class LLM:
    def __init__(self):
        self.seen = []
    def chat(self, user, system=None, history=None, **kw):
        self.seen.append({"user": user, "system": system})
        return "看到啦" if isinstance(user, list) else "好的呀"


def mk(st=None, asr_fail=False, img_fail=False, owner_only=False):
    d = Path(tempfile.mkdtemp()); sent = []; io_calls = {"voice": [], "image": [], "asr": []}
    def dv(xml, mid):
        io_calls["voice"].append((xml, mid)); return "http://oos.example/v.silk"
    def di(xml, t):
        io_calls["image"].append((xml, t))
        if img_fail:
            raise RuntimeError("boom")
        return "http://oos.example/i.png"
    def asr(w):
        io_calls["asr"].append(len(w))
        if asr_fail:
            raise RuntimeError("asr down")
        return "今天晚上吃什么"
    def fetch(url):
        return SILK if url.endswith(".silk") else PNG
    if st is None:
        st = FeatureSettings(None)
        for k in ("chat_companion", "asr", "vision"):
            st.set(ROOM, k, True)
    llm = LLM()
    r = build_router(fetch_list=lambda x: MEMBERS, fetch_info=lambda x: {}, sender=lambda m: sent.append(m), send_enabled=True,
                     db_path=d / "s.db", llm=llm, settings=st, allow_targets={ROOM}, amap_tools=False,
                     media_io={"download_voice": dv, "download_image": di, "asr": asr, "fetch": fetch})
    r.multi_async = False; r.sleep = lambda s: None
    if owner_only:
        r.followup.owner_only_rooms.add(ROOM)
    return r, sent, llm, io_calls


def media(sender, mt, content, at=(), msgid=None):
    m = raw(sender, content, at, mt=mt)
    if msgid:
        m["Data"]["MsgId"] = msgid
    return normalize(m)


# 1) 语音：先 @ 聊一句开窗口 → 窗口内发语音 → 下载+转写 → 当文字走陪聊
r, sent, llm, io_ = mk()
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005在吗", [BOT])))
n = len(llm.seen)
r.dispatch(media("wxid_a", 34, VOICE_XML, msgid=4242))
assert io_["voice"] == [(VOICE_XML, 4242)] and len(io_["asr"]) == 1, io_
assert len(llm.seen) == n + 1 and "今天晚上吃什么" in llm.seen[-1]["user"] and "语音" in llm.seen[-1]["system"]
ctx = r.context.recent(ROOM) if hasattr(r.context, "recent") else None
print("窗口内语音 → downloadVoice(MsgId) → silk 解码 → ASR → 陪聊 ✅")

# 2) 不是对小助手说的语音（别人、没窗口）→ 不下载不转写
r.dispatch(media("wxid_b", 34, VOICE_XML, msgid=1))
assert len(io_["voice"]) == 1 and len(io_["asr"]) == 1
print("非对小助手的语音不转写（省额度）✅")

# 3) 回调自带 ImgBuf.buffer → 不再调 downloadVoice
import base64
m = raw("wxid_a", VOICE_XML, mt=34); m["Data"]["ImgBuf"] = {"iLen": len(SILK), "buffer": base64.b64encode(SILK).decode()}
r.dispatch(normalize(m))
assert len(io_["voice"]) == 1 and len(io_["asr"]) == 2
print("回调自带 silk 直接转写 ✅")

# 4) asr 开关关 → 不转写；转写失败 → 不回、退额度
st = FeatureSettings(None); st.set(ROOM, "chat_companion", True)
r, sent, llm, io_ = mk(st)
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005在吗", [BOT]))); n = len(llm.seen)
r.dispatch(media("wxid_a", 34, VOICE_XML, msgid=5))
assert io_["asr"] == [] and len(llm.seen) == n
r, sent, llm, io_ = mk(asr_fail=True)
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005在吗", [BOT]))); n = len(llm.seen)
r.dispatch(media("wxid_a", 34, VOICE_XML, msgid=5))
assert len(io_["asr"]) == 1 and len(llm.seen) == n
assert r.media_in.counter.get(f"asr:{ROOM}") == 0
print("asr 关闭不转写 / 转写失败不回且退额度 ✅")

# 5) 大群（追问窗口仅群主 / 示例大群）：语音图片不走窗口
r, sent, llm, io_ = mk(owner_only=True)
r.dispatch(normalize(raw("wxid_owner", "@小助手\u2005在吗", [BOT])))
r.dispatch(media("wxid_owner", 34, VOICE_XML, msgid=7))
r.dispatch(media("wxid_owner", 3, IMG_XML))
assert io_["asr"] == [] and io_["image"] == []
print("SI 类大群：语音/图片只认 @/引用 ✅")

# 6) 识图：@ + 引用图片 → 下载 → 多模态 content
r, sent, llm, io_ = mk()
QREF = ref.replace("<title>这是啥</title>", "<title>@小助手\u2005这是啥</title>")
q = raw("wxid_a", QREF, [BOT], mt=49)
r.dispatch(normalize(q))
assert io_["image"] and io_["image"][0][1] == 2, io_
u = llm.seen[-1]["user"]
assert isinstance(u, list) and any(p.get("type") == "image_url" for p in u), u
assert sent and sent[-1].content == "看到啦"
print("@+引用图片 → downloadImage → 多模态回答 ✅")

# 7) 刚发图后 @ 问「这是啥」→ 看那张图；@ 闲聊不带图
r, sent, llm, io_ = mk()
r.dispatch(media("wxid_b", 3, IMG_XML))
assert io_["image"] == []                                     # 没对小助手说 → 不下载
r.dispatch(normalize(raw("wxid_b", "@小助手\u2005你好呀", [BOT])))
assert io_["image"] == [] and isinstance(llm.seen[-1]["user"], str)
r.dispatch(normalize(raw("wxid_b", "@小助手\u2005这张图是什么地方", [BOT])))
assert len(io_["image"]) == 1 and isinstance(llm.seen[-1]["user"], list)
# 窗口内直接发图
r.dispatch(media("wxid_b", 3, IMG_XML))
assert len(io_["image"]) == 2 and isinstance(llm.seen[-1]["user"], list)
print("近期本人发图 + @问图 / 窗口内发图 → 识图；闲聊不带图 ✅")

# 8) 上限 & 下载失败：如实说没看到，退额度
r, sent, llm, io_ = mk(img_fail=True)
r.dispatch(normalize(q))
assert isinstance(llm.seen[-1]["user"], str) and "没能看到" in llm.seen[-1]["user"]
assert r.media_in.counter.get(f"vis:{ROOM}") == 0 and len(io_["image"]) == 3      # type 2→1→3 都试过
r, sent, llm, io_ = mk(); r.media_in.vis_daily_room = 1
r.dispatch(normalize(q)); r.dispatch(normalize(raw("wxid_c", QREF, [BOT], mt=49)))
assert len(io_["image"]) == 1 and "没能看到" in llm.seen[-1]["user"]
print("识图失败如实说 / 每群日上限 ✅")

# 9) 能力清单按开关
st = FeatureSettings(None)
for k in ("chat_companion", "vision", "asr"):
    st.set("g", k, True)
txt = describe(st.enabled, "g")
assert "看图" in txt and "听语音" in txt
st.set("g", "vision", False); st.set("g", "asr", False)
txt = describe(st.enabled, "g"); assert "看图：" not in txt and "听语音：" not in txt and "本群没开" in txt   # 10-22：关着的只列在「本群没开」里
print("能力清单随开关 ✅")

# 10) TTS：MiniMax 优先，失败走豆包；都失败抛错（上层回退文字）
import minimax_media, tts_doubao
from features import voice_reply as vr
orig = (minimax_media.configured, minimax_media.tts_pcm, tts_doubao.synth_pcm)
try:
    minimax_media.configured = lambda: True
    minimax_media.tts_pcm = lambda s: b"MM"
    tts_doubao.synth_pcm = lambda s: b"DB"
    assert vr.synth_pcm("你好") == (b"MM", "minimax")
    def bad(s): raise RuntimeError("quota")
    minimax_media.tts_pcm = bad
    assert vr.synth_pcm("你好") == (b"DB", "doubao")
    tts_doubao.synth_pcm = bad
    try:
        vr.synth_pcm("你好"); assert False
    except RuntimeError as e:
        assert "MiniMax" in str(e) and "豆包" in str(e)
finally:
    minimax_media.configured, minimax_media.tts_pcm, tts_doubao.synth_pcm = orig
print("TTS：MiniMax speech-2.8 → 豆包备用 → 失败回退文字 ✅")
print("test_media_in 全部通过")
