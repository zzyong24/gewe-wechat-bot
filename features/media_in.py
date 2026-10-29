"""入站语音 / 图片：只在明显是对小助手说的时候才花额度处理。

语音（MsgType 34，开关 asr）：私聊、或发言人在追问窗口内（群里语音没法 @，也不能引用后再说话）→
  回调 ImgBuf.buffer 里有 silk 就直接用，否则 gewe downloadVoice(xml, MsgId) 拿 silk → pysilk 解码 → wav →
  MiniMax asr-1.0 → 把这条消息改成该用户的文字消息（from_voice=True），后面 @ 判断 / 陪聊 / 追问窗口照常。
  每日上限：每群 BOT_ASR_DAILY_ROOM=60 条、总计 BOT_ASR_DAILY=200 条。其它语音不转写。
图片（MsgType 3，开关 vision）：
  - 发言人在追问窗口内发图 → 当作给小助手看的图；
  - @小助手 + 引用了一张图（refermsg type 3）→ 看被引用的图；
  - @小助手 且 3 分钟内本人刚发过图、这句话像在问图（这是啥/看看/截图…）→ 看那张图。
  只记下图片 XML（msg.image_xml），真正下载在陪聊回复时做（gewe downloadImage → 压到 ≤1024px → 多模态 chat）。
  每日上限：每群 BOT_VISION_DAILY_ROOM=30 张、总计 BOT_VISION_DAILY=100 张。
"""
from __future__ import annotations

import base64
import html
import io
import logging
import os
import re
import threading
import time
import wave

log = logging.getLogger("feature.media_in")

ASK_IMAGE = re.compile(r"图|照片|截图|这是|这个|这张|啥|什么|看看|看下|看一下|帮我看|识别|认识|哪里|哪儿|是谁|写的|翻译|多少|几")
_REF = re.compile(r"<refermsg>(.*?)</refermsg>", re.S)


def quote_image_xml(raw_content: str) -> str:
    """引用消息里被引用的是图片 → 返回该图片的 XML（refermsg/content，已反转义）；否则 ""。"""
    m = _REF.search(raw_content or "")
    if not m:
        return ""
    body = m.group(1)
    t = re.search(r"<type>\s*(\d+)\s*</type>", body)
    c = re.search(r"<content>(.*?)</content>", body, re.S)
    if not (t and t.group(1) == "3" and c):
        return ""
    x = html.unescape(c.group(1)).strip()
    i = x.find("<")
    if i < 0:
        return ""
    x = x[i:]
    if ":\n" in x[:80] and not x.startswith("<"):
        x = x.split(":\n", 1)[1]
    return x if "<img" in x else ""


def silk_to_wav(silk: bytes, rate: int = 16000) -> bytes:
    import pysilk
    data = silk
    if data[:1] == b"\x02" and data[1:10] != b"#!SILK_V3":
        pass
    pcm = io.BytesIO()
    pysilk.decode(io.BytesIO(data), pcm, rate)
    w = io.BytesIO()
    wf = wave.open(w, "wb"); wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
    wf.writeframes(pcm.getvalue()); wf.close()
    return w.getvalue()


def to_data_url(img: bytes, max_side: int = 1024) -> str:
    from PIL import Image
    im = Image.open(io.BytesIO(img))
    im = im.convert("RGB")
    im.thumbnail((max_side, max_side))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()


def _clean(e) -> str:
    """日志里去掉下载链接的签名参数（AWSAccessKeyId/Signature 等），只留错误类型。"""
    return re.sub(r"\?[^\s'\")]*", "?…", str(e))[:200]


class MediaIn:
    def __init__(self, enabled, allowed, counter=None, followup=None, download_voice=None, download_image=None,
                 asr_fn=None, fetch=None, clock=time.time, configured=None):
        """enabled(sess, key)；allowed(sess) → 白名单且非观察群。download_*/asr_fn/fetch 可注入（测试）。"""
        self.enabled, self.allowed, self.counter, self.fu = enabled, allowed, counter, followup
        self.clock = clock
        self.configured = configured or (lambda: True)
        if download_voice is None or download_image is None or asr_fn is None:
            import gewe_client as g, minimax_media as mm
            download_voice = download_voice or g.download_voice
            download_image = download_image or g.download_image
            asr_fn = asr_fn or (lambda wav: mm.asr(wav))
        self.download_voice, self.download_image, self.asr_fn = download_voice, download_image, asr_fn
        self.fetch = fetch or self._http_get
        self.asr_daily = int(os.environ.get("BOT_ASR_DAILY", "200"))
        self.asr_daily_room = int(os.environ.get("BOT_ASR_DAILY_ROOM", "60"))
        self.vis_daily = int(os.environ.get("BOT_VISION_DAILY", "100"))
        self.vis_daily_room = int(os.environ.get("BOT_VISION_DAILY_ROOM", "30"))
        self.recent_sec = int(os.environ.get("BOT_IMAGE_RECENT_SEC", "180"))
        self._last_img: dict[tuple, tuple[float, str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _http_get(url: str) -> bytes:
        import requests
        if url.startswith("http://"):                  # gewe 给的天翼云 OOS 链接：轮询端出网 80 端口不通，https 可用（签名与协议无关）
            url = "https://" + url[len("http://"):]
        r = requests.get(url, timeout=(8, 30))
        r.raise_for_status()
        if len(r.content) > 20 * 1024 * 1024:
            raise ValueError("文件太大")
        return r.content

    def _on(self, sess, key) -> bool:
        return bool(sess) and self.configured() and self.enabled(sess, key) and self.allowed(sess)

    def _left(self, kind: str, sess: str) -> bool:
        if self.counter is None:
            return True
        lim = {"asr": (self.asr_daily, self.asr_daily_room), "vis": (self.vis_daily, self.vis_daily_room)}[kind]
        return self.counter.get(f"{kind}:all") < lim[0] and self.counter.get(f"{kind}:{sess}") < lim[1]

    def _take(self, kind: str, sess: str) -> bool:
        if self.counter is None:
            return True
        lim = {"asr": (self.asr_daily, self.asr_daily_room), "vis": (self.vis_daily, self.vis_daily_room)}[kind]
        return self.counter.take({f"{kind}:all": lim[0], f"{kind}:{sess}": lim[1]})

    def _window(self, msg) -> bool:
        if not msg.is_group:
            return True
        fu = self.fu
        if fu is not None and (msg.room_id in (getattr(fu, "owner_only_rooms", None) or ())
                               or msg.room_id in (getattr(fu, "off_rooms", None) or ())):
            return False                               # 示例大群 等：语音/图片只认 @/引用，不走追问窗口
        return self.fu is not None and self.fu.active(msg.room_id, msg.from_wxid, at=self.clock())

    # ---- router 调用：followup.prepare 之前 ----
    def pre(self, msg):
        if msg.event != "AddMsg" or msg.is_self:
            return
        sess = msg.room_id or msg.from_wxid
        if msg.msg_type == 3:
            with self._lock:
                self._last_img[(sess, msg.from_wxid)] = (self.clock(), msg.content)
            if self._on(sess, "vision") and self._window(msg) and self._left("vis", sess):
                msg._media = ("image", msg.msg_type, msg.content)
                msg.msg_type, msg.content = 1, "（发了一张图片给你看）"
        elif msg.msg_type == 34:
            if self._on(sess, "asr") and self._window(msg) and self._left("asr", sess):
                msg._media = ("voice", msg.msg_type, msg.content)
                msg.msg_type, msg.content = 1, "[语音]"

    # ---- router 调用：followup.prepare 之后 ----
    def post(self, msg):
        if msg.event != "AddMsg" or msg.is_self:
            return
        sess = msg.room_id or msg.from_wxid
        media = getattr(msg, "_media", None)
        addressed = (not msg.is_group) or (msg.self_wxid in msg.at_list)
        if media:
            kind, otype, xml = media
            if not addressed:                         # 窗口没接（上限/游戏中等）→ 还原，不花额度
                msg.msg_type, msg.content = otype, xml
                msg._media = None
                return
            if kind == "image":
                msg.image_xml = xml
                return
            text = self._transcribe(msg, sess, xml)
            if text:
                msg.content, msg.from_voice = text, True
                log.info("语音转写 %s %s：%s", sess, msg.from_wxid, text[:80])
            else:
                msg.msg_type, msg.content = otype, xml      # 转写失败：当普通语音，不回
                msg.at_list = [w for w in msg.at_list if w != msg.self_wxid]
            return
        if msg.msg_type != 1 or not addressed or not self._on(sess, "vision"):
            return
        raw_content = ""
        try:
            raw_content = ((msg.raw.get("Data") or {}).get("Content") or {}).get("string", "")
        except Exception:
            pass
        x = quote_image_xml(raw_content)
        if x:
            msg.image_xml = x
            return
        with self._lock:
            hit = self._last_img.get((sess, msg.from_wxid))
        txt = (msg.content or "").strip()
        if hit and self.clock() - hit[0] <= self.recent_sec and (ASK_IMAGE.search(txt) or txt in ("", "?", "？", "??", "？？")):
            msg.image_xml = hit[1]

    def _transcribe(self, msg, sess, xml) -> str:
        if not self._take("asr", sess):
            return ""
        t0 = time.time()
        try:
            silk = b""
            buf = ((msg.raw.get("Data") or {}).get("ImgBuf") or {}).get("buffer") if isinstance(msg.raw, dict) else None
            if buf:
                try:
                    silk = base64.b64decode(buf)
                except Exception:
                    silk = b""
            if not silk:
                mid = (msg.raw.get("Data") or {}).get("MsgId") if isinstance(msg.raw, dict) else None
                url = self.download_voice(xml, int(mid or 0))
                silk = self.fetch(url)
            text = self.asr_fn(silk_to_wav(silk))
            log.info("ASR %.1fs %d 字节 silk", time.time() - t0, len(silk))
            return text.strip()
        except Exception as e:
            if self.counter is not None:
                self.counter.refund([f"asr:all", f"asr:{sess}"])
            log.warning("语音转写失败 %s: %s", sess, _clean(e))
            return ""

    # ---- 陪聊调用：真正下载要看的图 ----
    def image_data_url(self, sess: str, xml: str) -> str | None:
        if not self._take("vis", sess):
            log.info("识图今日额度用完 %s", sess)
            return None
        t0 = time.time()
        last = None
        for t in (2, 1, 3):                            # 常规 → 高清 → 缩略
            try:
                url = self.download_image(xml, t)
                du = to_data_url(self.fetch(url))
                log.info("识图下载 type=%d %.1fs", t, time.time() - t0)
                return du
            except Exception as e:
                last = e
        if self.counter is not None:
            self.counter.refund(["vis:all", f"vis:{sess}"])
        log.warning("下载图片失败 %s: %s", sess, _clean(last))
        return None
