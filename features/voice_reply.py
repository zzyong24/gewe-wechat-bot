"""陪聊偶尔用语音回。按群开关 voice_reply。

触发：
  (a) 有人明确要语音（「用语音说/发语音/说给我听/语音回我」）→ 这条用语音回（仍受每日字数上限、同群 60 秒冷却保护）
  (b) 自发：回复短（≤BOT_VOICE_MAX_CHARS=40 字，非列表/地图/链接/数字串/游戏）时按 BOT_VOICE_PROB=12% 改语音；
      每群每天自发 ≤ BOT_VOICE_DAILY_AUTO=8 条；同群语音间隔 ≥ BOT_VOICE_COOLDOWN=600 秒。
全局每天合成字数 ≤ BOT_VOICE_DAILY_CHARS=1500（保护豆包免费额度：试用 2 万字，用完自动后付费）。
链路（异步，在 router 发送线程里）：MiniMax speech-2.8-hd（Token Plan 内，音色 female-shaonv）→ 失败用豆包 seed-tts 备用 → pysilk → relay 图床（48h 过期）→ gewe postVoice；任何一步失败回退发文字。
一条回复要么全语音要么全文字。只在白名单群、非观察群生效（router 发送闸照常拦）。
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from pathlib import Path


def _env_f(k, d):
    try:
        return float(os.environ.get(k, d))
    except ValueError:
        return float(d)


ASK = re.compile(r"(用|发|来|回|讲|整)(个|条|段|一段|一条)?语音|语音(说|讲|回|念|读|聊|发)|说给我听|念给我听|读给我听|讲给我听|用声音|听听你的声音|听你说话")
NOT_AUTO = re.compile(r"https?://|www\.|\d{3,}|[0-9]+[.、)）]|^[-•·*]|【|】|口令|@小助手|\n.*\n.*\n|km|公里|米\b|路线|地铁|公交")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u2B00-\u2BFF]")
_SYM = re.compile(r"[#*_`~^|<>\[\]{}\\/=+$%&@]+")


def speakable(text: str) -> str:
    """语音稿：去 emoji/符号、拼成一句口语（换行→逗号），收尾加句号。"""
    t = _EMOJI.sub("", text or "")
    t = re.sub(r"\[[^\]]{1,6}\]", "", t)              # 微信表情码 [捂脸]
    t = _SYM.sub("", t).replace("～", "，").replace("…", "，")
    t = re.sub(r"\s*\n+\s*", "，", t.strip())
    t = re.sub(r"[，,]{2,}", "，", t).strip("，, ")
    if t and t[-1] not in "。！？!?":
        t += "。"
    return t


class VoicePolicy:
    def __init__(self, enabled, allowed, state_path: Path | None = None, rng=None, clock=time.time, tts_ok=None):
        """enabled(sess) → 本群 voice_reply 开关；allowed(sess) → 在白名单且非观察群；tts_ok() → TTS 可用（有 key、未因计费停用）。"""
        self.enabled, self.allowed = enabled, allowed
        self.rng, self.clock = rng or random.Random(), clock
        self.path = Path(state_path) if state_path else None
        self.tts_ok = tts_ok or (lambda: True)
        self.prob = _env_f("BOT_VOICE_PROB", 0.12)
        self.max_chars = int(_env_f("BOT_VOICE_MAX_CHARS", 40))
        self.req_max = int(_env_f("BOT_VOICE_REQ_MAX_CHARS", 120))
        self.daily_auto = int(_env_f("BOT_VOICE_DAILY_AUTO", 8))
        self.cooldown = _env_f("BOT_VOICE_COOLDOWN", 600)
        self.req_cooldown = _env_f("BOT_VOICE_REQ_COOLDOWN", 60)
        self.daily_chars = int(_env_f("BOT_VOICE_DAILY_CHARS", 1500))
        self._lock = threading.Lock()
        self._st = self._load()

    # ---- 状态 ----
    def _load(self):
        if self.path and self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except (OSError, ValueError):
                pass
        return {}

    def _today(self):
        st = self._st
        day = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        if st.get("day") != day:
            last = st.get("last", {})
            st.clear(); st.update(day=day, auto={}, chars=0, last=last)
        return st

    def _save(self):
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._st)); os.replace(tmp, self.path)

    # ---- 决策 ----
    def plan(self, sess: str, user_text: str) -> str | None:
        """回复前：'req'（对方要语音）/'auto'（这次抽中自发语音，最终还要看回复是否够短）/None。"""
        if not (self.enabled(sess) and self.allowed(sess) and self.tts_ok()):
            return None
        with self._lock:
            st = self._today()
            if st["chars"] >= self.daily_chars:
                return None
            gap = self.clock() - st["last"].get(sess, 0)
            if ASK.search(user_text or ""):
                return "req" if gap >= self.req_cooldown else None
            if gap < self.cooldown or st["auto"].get(sess, 0) >= self.daily_auto:
                return None
        return "auto" if self.rng.random() < self.prob else None

    def accept(self, sess: str, reply_text: str, kind: str) -> str | None:
        """回复出来后：合格就记账并返回语音稿，否则 None（发文字）。"""
        if not kind:
            return None
        if kind == "auto" and (len(reply_text) > self.max_chars or NOT_AUTO.search(reply_text)):
            return None
        script = speakable(reply_text)
        if not script or len(script) > (self.req_max if kind == "req" else self.max_chars + 5):
            return None
        with self._lock:
            st = self._today()
            if st["chars"] + len(script) > self.daily_chars:
                return None
            st["chars"] += len(script)
            st["last"][sess] = self.clock()
            if kind == "auto":
                st["auto"][sess] = st["auto"].get(sess, 0) + 1
            self._save()
        return script


VOICE_HINT = ("（对方想听你的语音：这条回复会被转成语音发出去。只写要说的话，口语、自然、像在跟朋友说话，"
              "80 字以内，不要 emoji、符号、列表、链接，也别说「我用语音说」这种话。）")


def synth_pcm(script: str) -> tuple[bytes, str]:
    """先 MiniMax speech-2.8（套餐内），失败再豆包 seed-tts-2.0。返回 (pcm, 引擎名)。都失败抛最后一个错误。"""
    import logging
    err = None
    try:
        import minimax_media
        if minimax_media.configured():
            return minimax_media.tts_pcm(script), "minimax"
    except Exception as e:
        err = e
        logging.getLogger("voice").warning("MiniMax TTS 失败，改用豆包: %s", str(e)[:160])
    import tts_doubao
    try:
        return tts_doubao.synth_pcm(script), "doubao"
    except Exception as e:
        raise e if err is None else RuntimeError(f"MiniMax: {str(err)[:100]} / 豆包: {str(e)[:100]}") from None


def tts_available() -> bool:
    import minimax_media, tts_doubao
    return minimax_media.configured() or tts_doubao.available()


def make_voice_sender(tmp_dir: Path):
    """→ voice_sender(to_wxid, script) -> bool：TTS → silk → 上传 → postVoice；失败返回 False（调用方回退文字）。"""
    import logging
    log = logging.getLogger("voice")

    def send(to_wxid: str, script: str) -> bool:
        import uuid
        import voice, imagehost, gewe_client as g
        tmp_dir.mkdir(parents=True, exist_ok=True)
        pcm = tmp_dir / f"v-{uuid.uuid4().hex}.pcm"
        silk = pcm.with_suffix(".silk")
        stage = "tts"
        try:
            t0 = time.time()
            data, engine = synth_pcm(script)
            pcm.write_bytes(data)
            stage = "silk"
            silk, ms = voice.pcm_to_silk(pcm, silk)
            stage = "upload"
            url = imagehost.upload_silk(silk)
            stage = "post"
            g.post_voice(to_wxid, url, ms)
            log.info("语音已发 -> %s %d 字 %.1fs 音频 %dms 引擎=%s", to_wxid, len(script), time.time() - t0, ms, engine)
            return True
        except Exception as e:
            # postVoice 网络超时：可能其实已发出 → 不回退文字（宁缺勿重复）；其余失败都回退文字
            maybe_sent = stage == "post" and "network" in str(e)
            log.warning("语音发送失败(%s) %s -> %s: %s", stage, "不回退（可能已发出）" if maybe_sent else "回退文字",
                        to_wxid, str(e)[:200])
            return maybe_sent
        finally:
            pcm.unlink(missing_ok=True)
            Path(silk).unlink(missing_ok=True)
    return send
