"""生图（MiniMax image-01）。按群开关 image_gen；每群每天 BOT_IMAGE_DAILY=10 张；同一群同时只画一张。

触发：
  口令（@小助手 或追问窗口内）：「画一张XX」「画个XX」「帮我画XX」「生成图片XX」「生成一张XX的图」
  陪聊模型工具 generate_image(prompt)：模型判断对方想要图时调用。
流程：先回「在画了，稍等～」，后台线程生成（~20 秒）→ 存 data/images → router.deliver(图) → 中转图床 + gewe postImage。
      不阻塞会话队列；失败回一句口语说明（额度退回）。
婉拒：真实人物肖像（含群友昵称、明星/政要）、色情、暴力血腥、政治敏感——口语化婉拒，不调接口。
"""
from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from pathlib import Path

from features.common import at_me

log = logging.getLogger("feature.image")

CMD = re.compile(r"^(?:请|麻烦)?(?:你)?(?:帮我|给我|帮忙|替我)?(?:画|生成|做)(?:一张|一幅|一个|个|张|幅|一下|下)?(?:图片|图|画|插画)?"
                 r"[：:，, ]*(?P<p>.{2,300})$", re.S)
CMD2 = re.compile(r"^(?:来|整)(?:一张|张|一幅|幅)(?P<p>.{2,300}?)(?:的)?(?:图|图片|画)$", re.S)

_SEX = re.compile(r"裸|色情|情色|性感|涩图|瑟图|色图|内衣|比基尼|泳装美女|胸|乳|屁股|臀|做爱|啪啪|成人|18禁|r18|nsfw|porn|nude|sexy|hentai", re.I)
_VIOLENCE = re.compile(r"血腥|杀人|尸体|爆头|斩首|砍头|虐杀|虐待|自杀|自残|割腕|枪杀|屠杀|恐怖袭击|炸弹|断肢|肢解|内脏|gore", re.I)
_POLITICS = re.compile(r"习近平|毛泽东|邓小平|江泽民|胡锦涛|李强|特朗普|川普|拜登|普京|泽连斯基|金正恩|共产党|国民党|台独|港独|藏独|疆独|"
                       r"法轮|六四|天安门|领导人|国家主席|总统|总理|国旗|党旗|游行|示威|政变|政府")
# 真人：明确是真实人物才婉拒（2026-10-23「猪猪侠的照片」误判修正：卡通/动漫/游戏角色、动物、物品都能画；「照片」「真实感」本身不算真人）
_PERSON_HARD = re.compile(r"证件照|自拍|(?:我|他|她|你|主人|小助手)(?:自己)?的?(?:样子|长相|照片|肖像|头像|脸|真人)|"
                          r"周杰伦|刘德华|成龙|杨幂|赵丽颖|迪丽热巴|蔡徐坤|王一博|肖战|易烊千玺|马斯克|雷军|马云|黄仁勋")
_PERSON_SOFT = re.compile(r"明星|艺人|演员|歌手|网红|主播|爱豆|偶像|真人|真实人物|肖像|写真|长得像")   # 判不了时才按这些婉拒
_PERSONISH = re.compile(r"明星|艺人|演员|歌手|网红|主播|爱豆|偶像|真人|真实|肖像|写真|长得像|照片|写实|逼真|人|男|女|孩|帅哥|美女|"
                        r"老板|同事|朋友|老婆|老公|爸|妈|他|她|谁|[A-Za-z]{3,}")     # 可能涉及人物 → 交给小模型判
PERSON_JUDGE_PROMPT = ("群友让 AI 画一张图，要求是：「{p}」\n"
                       "画面主体是不是一个真实存在的具体人物（比如群友、明星、政要、网红、某个现实中的人）？\n"
                       "虚构 / 卡通 / 动漫 / 游戏角色（如猪猪侠、孙悟空、皮卡丘）、泛指的无名人物（如「一个女孩」）、动物、物品、风景都不算。\n"
                       "「照片」「真实感」「写实」只是风格，不代表真人。只输出：是 / 否 / 不确定")


def make_person_judge(llm_for):
    """→ judge(sess, prompt) → True（真实人物）/ False（不是）/ None（判不了）。一次短调用。"""
    def judge(sess, prompt):
        llm = llm_for(sess)
        if llm is None:
            return None
        out = llm.chat(PERSON_JUDGE_PROMPT.format(p=prompt[:300]), system="你只输出「是」「否」或「不确定」。",
                       max_completion_tokens=1200)
        out = re.sub(r"<think>.*?</think>", "", str(out or ""), flags=re.S).strip()
        return True if out.startswith("是") else False if out.startswith("否") else None
    return judge


REFUSE = {
    "sex": ["这种我画不了哈，换个题材吧～", "这个不行哈，咱画点别的？"],
    "violence": ["太血腥的画不了，换个温和点的吧～", "这个有点吓人，我不画哈，换一个？"],
    "politics": ["政治相关的我就不画了哈，换个别的吧～"],
    "person": ["真人的样子我不画哈（怕侵犯肖像），画个卡通角色或者小动物怎么样？",
               "画真人不太合适，要不我画个卡通版的形象？"],
}


def refuse_reason(prompt: str, member_names=(), judge=None) -> str | None:
    """judge(prompt) → True/False/None：真人与否拿不准时问小模型；没有 judge 或判不了 → 只按明确的真人词婉拒。"""
    t = prompt or ""
    if _SEX.search(t):
        return "sex"
    if _VIOLENCE.search(t):
        return "violence"
    if _POLITICS.search(t):
        return "politics"
    if _PERSON_HARD.search(t):
        return "person"
    for n in member_names:
        n = (n or "").strip()
        if len(n) >= 2 and n not in ("小助手",) and n in t:
            return "person"
    if _PERSONISH.search(t):
        verdict = None
        if judge is not None:
            try:
                verdict = judge(t)
            except Exception as e:
                log.warning("真人判断小模型失败，按规则: %s", e)
        if verdict is True:
            return "person"
        if verdict is None and _PERSON_SOFT.search(t):
            return "person"
    return None


def parse_command(text: str) -> str | None:
    t = (text or "").strip()
    m = CMD.match(t) or CMD2.match(t)
    if not m:
        return None
    p = m.group("p").strip(" ，,。.！!~～")
    if len(p) < 2 or re.fullmatch(r"(吗|呢|啊|好|得好|画|图|过|完|错|的)+", p):
        return None
    return p


def failure_text(e: Exception) -> str:
    """生图最终失败时如实说原因（重试已在 minimax_media.generate_image 里做过一次）。"""
    if getattr(e, "sensitive", False):
        return "这个题材画不出来，换个说法试试？"
    if getattr(e, "timeout", False):
        return "这次没画出来，网络超时了，稍后再让我画一次"
    if getattr(e, "network", False):
        return "这次没画出来，网络断了一下，稍后再让我画一次"
    return "这张没画出来，等会儿再试试吧"


class ImageGen:
    def __init__(self, enabled, allow, blocked, deliver, counter=None, out_dir: Path | None = None,
                 gen_fn=None, configured=None, members_of=None, rng=None, async_mode: bool = True,
                 person_judge=None, vision=None, describe_fn=None):
        self.enabled, self.allow, self.blocked, self.deliver = enabled, allow, blocked, deliver
        self.counter = counter
        self.out_dir = Path(out_dir) if out_dir else Path("data/images")
        if gen_fn is None:
            import minimax_media
            gen_fn, configured = minimax_media.generate_image, configured or minimax_media.configured
        self.gen_fn = gen_fn
        self.configured = configured or (lambda: True)
        self.members_of = members_of or (lambda room: [])
        self.rng = rng or random.Random()
        self.async_mode = async_mode
        self.daily = int(os.environ.get("BOT_IMAGE_DAILY", "10"))
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        self.person_judge = person_judge  # person_judge(sess, prompt) → True/False/None（make_person_judge）
        self.vision = vision              # features/media_in.MediaIn：改图时下载原图（image_data_url）
        self.describe_fn = describe_fn    # describe_fn(sess, data_url, instruction) → dict（make_describe）：看原图写重画描述
        self._pending: dict[tuple, tuple[float, str]] = {}   # (群, 人) → (时间, 改图要求)：先说要求、后发图

    def available(self, sess: str) -> bool:
        return bool(self.configured() and self.enabled(sess) and self.allow(sess) and not self.blocked(sess))

    def start(self, sess: str, wxid: str, prompt: str, ack_now: bool = False) -> str:
        """返回给群里的一句话；需要画时后台开画。ack_now=True：「在画了」先由这里直接发出（保证先于图片），返回 ""。"""
        if not self.available(sess):
            return "这个群还没开画图功能哈"
        try:
            names = list(self.members_of(sess) or [])
        except Exception:
            names = []
        judge = (lambda p: self.person_judge(sess, p)) if self.person_judge else None
        why = refuse_reason(prompt, names, judge)
        if why:
            log.info("生图婉拒 %s %s: %s", sess, why, prompt[:60])
            return self.rng.choice(REFUSE[why])
        with self._lock:
            if sess in self._busy:
                return "上一张还在画呢，等它出来再画下一张～"
            if self.counter is not None and not self.counter.take({f"img:{sess}": self.daily}):
                return f"今天这个群画了 {self.daily} 张了，明天再画吧～"
            self._busy.add(sess)
        said = self.rng.choice(["在画了，稍等～", "好，在画了，二十来秒～", "收到，画着呢，稍等一下～"])
        if ack_now:
            from router import Reply
            self.deliver(Reply(sess, said))
        if self.async_mode:
            threading.Thread(target=self._run, args=(sess, prompt), daemon=True, name="image-gen").start()
        else:
            self._run(sess, prompt)
        return "" if ack_now else said

    def _run(self, sess: str, prompt: str, ref: str | None = None, note_prefix: str = "发了一张自己画的图"):
        from router import Reply
        t0 = time.time()
        try:
            self._cleanup()
            path = self.gen_fn(prompt, self.out_dir, subject_ref=ref) if ref else self.gen_fn(prompt, self.out_dir)
            log.info("生图完成 %s %.1fs %s", sess, time.time() - t0, path)
            self.deliver(Reply(sess, "图发不出来了，等会儿再让我画一次吧", image_path=str(path),
                               note=f"[{note_prefix}：{prompt[:40]}]"))
        except Exception as e:
            if self.counter is not None:
                self.counter.refund([f"img:{sess}"])
            log.warning("生图失败 %s %.1fs: %s（额度已退）", sess, time.time() - t0, str(e)[:200])
            self.deliver(Reply(sess, failure_text(e)))
        finally:
            with self._lock:
                self._busy.discard(sess)

    # ---- 改图：「把这张图做成红色的」+ 同一人前后发的图 / 引用的图 ----
    def remember_request(self, sess: str, wxid: str, instruction: str) -> None:
        with self._lock:
            self._pending[(sess, wxid)] = (time.time(), instruction)

    def take_request(self, sess: str, wxid: str) -> str | None:
        with self._lock:
            hit = self._pending.pop((sess, wxid), None)
        if hit and time.time() - hit[0] <= EDIT_WAIT_SEC:
            return hit[1]
        return None

    def has_request(self, sess: str, wxid: str) -> bool:
        with self._lock:
            hit = self._pending.get((sess, wxid))
        return bool(hit and time.time() - hit[0] <= EDIT_WAIT_SEC)

    def start_edit(self, sess: str, wxid: str, xml: str, instruction: str) -> str:
        """按原图重画（MiniMax 没有通用的「在原图上改」接口）：看图 → 原图主体/风格/构图 + 修改要求写成描述 → 重新生成；
        原图是卡通角色时同时作为 subject_reference 传给 image-01。如实说明是重画。真人照片婉拒。额度同普通生图。"""
        if not self.available(sess):
            return "这个群还没开画图功能哈"
        if self.vision is None or self.describe_fn is None:
            return "这边看不了原图，没法照着画；你描述一下想要的画面，我直接画一张？"
        names = []
        try:
            names = list(self.members_of(sess) or [])
        except Exception:
            pass
        why = refuse_reason(instruction, names)          # 修改要求本身（色情/暴力/政治/点名真人）
        if why:
            return self.rng.choice(REFUSE[why])
        with self._lock:
            if sess in self._busy:
                return "上一张还在画呢，等它出来再画下一张～"
            if self.counter is not None and not self.counter.take({f"img:{sess}": self.daily}):
                return f"今天这个群画了 {self.daily} 张了，明天再画吧～"
            self._busy.add(sess)
        from router import Reply
        self.deliver(Reply(sess, "好，我照着这张图重新画一张（不是在原图上直接改，细节会有出入），二十来秒～"))
        if self.async_mode:
            threading.Thread(target=self._run_edit, args=(sess, xml, instruction), daemon=True, name="image-edit").start()
        else:
            self._run_edit(sess, xml, instruction)
        return ""

    def _run_edit(self, sess: str, xml: str, instruction: str):
        from router import Reply
        handed = False
        try:
            du = self.vision.image_data_url(sess, xml)
            if not du:
                raise RuntimeError("原图下载失败")
            d = self.describe_fn(sess, du, instruction) or {}
            if d.get("real_person"):
                log.info("改图婉拒（原图是真人）%s", sess)
                self._refund(sess)
                self.deliver(Reply(sess, "这张是真人照片，真人我不改也不画哈（怕侵犯肖像）；换成卡通形象或者别的图可以～"))
                return
            prompt = str(d.get("prompt") or "").strip()
            if len(prompt) < 6:
                raise RuntimeError("没写出重画描述")
            ref = du if d.get("character") else None
            log.info("改图描述 %s ref=%s：%s", sess, bool(ref), prompt[:80])
            handed = True
            self._busy_run(sess, prompt, ref)
        except Exception as e:
            log.warning("改图失败 %s: %s", sess, str(e)[:200])
            if not handed:
                self._refund(sess)
                self.deliver(Reply(sess, "这张没能照着画出来，等会儿再试试吧"))
        finally:
            if not handed:
                with self._lock:
                    self._busy.discard(sess)

    def _refund(self, sess):
        if self.counter is not None:
            self.counter.refund([f"img:{sess}"])

    def _busy_run(self, sess, prompt, ref):
        self._run(sess, prompt, ref=ref, note_prefix="照着群友发的图重画了一张")

    def _cleanup(self, keep_days: int = 7):
        try:
            cut = time.time() - keep_days * 86400
            for p in self.out_dir.glob("gen-*"):
                if p.stat().st_mtime < cut:
                    p.unlink(missing_ok=True)
        except OSError:
            pass


class ImageGenFeature:
    name = "image_gen"
    passive = False
    toggle_key = "image_gen"

    def __init__(self, gen: ImageGen):
        self.g = gen

    def match(self, msg) -> bool:
        if msg.event != "AddMsg" or msg.msg_type != 1 or msg.is_self or not msg.is_group or not at_me(msg):
            return False
        return parse_command(msg.command_text) is not None and self.g.available(msg.room_id)

    def handle(self, msg):
        msg.early_replied = True              # 已回话（开追问窗口、计配额）
        return self.g.start(msg.room_id, msg.from_wxid, parse_command(msg.command_text), ack_now=True) or None


EDIT_WAIT_SEC = int(os.environ.get("BOT_IMAGE_EDIT_WAIT_SEC", "90"))   # 先说改图要求、后发图：等同一个人的图多久
# 改图 / 照着图画：「把这个图做成红色的」「这张改成赛博朋克风」「按这张图画…」「P 一下」
EDIT_RE = re.compile(r"(?:这|那|这个|那个|这张|那张|上面|刚才|刚发|原|我的|我发的|你的)(?:的)?(?:图|图片|照片|头像|logo|LOGO|表情).{0,12}"
                     r"(?:做成|改成|变成|换成|弄成|调成|画成|p成|P成|修成|加上|加个|加点|去掉|改一下|改改|美化|重画|重新画|处理)"
                     r"|(?:把|将|给)(?:这|那|它|这个|那个|这张|那张|图|图片|照片).{0,10}(?:做成|改成|变成|换成|弄成|调成|画成|p成|P成|加上|加个|去掉|改)"
                     r"|(?:按|照|参考|仿照|照着|根据)(?:着)?(?:这|那|这个|这张|那张|原|我发的|上面的)(?:张|个)?(?:图|图片|照片)?.{0,6}(?:画|做|生成|改|来)"
                     r"|改图|修图|[pP]图|[pP]一下|做成.{0,10}(?:风格|的那种|那样)", re.I)


def edit_instruction(text: str) -> str | None:
    t = (text or "").strip()
    return t if len(t) >= 3 and EDIT_RE.search(t) else None


def _media_xml(msg) -> str:
    """这条消息本身是图片 → 图片 XML（media_in 可能已把它换成文本占位，原 XML 在 _media 里）。"""
    if msg.msg_type == 3:
        return msg.content or ""
    m = getattr(msg, "_media", None)
    return m[2] if m and m[0] == "image" else ""


class ImageEditFeature:
    """改图请求 → 生图（不再走陪聊：10-23 00:23 陪聊把生图提示词当文字发、还说「已经加上了」）。
    文字（@/引用/追问窗口内）像改图要求：
      - 带了图（引用图 / media_in 认出的图 / 同一人 EDIT_WAIT_SEC 秒内刚发的图）→ 开画；
      - 没图 → 记下要求，回一句「把图发我」；同一人 EDIT_WAIT_SEC 秒内发来的下一张图 → 开画。"""
    name = "image_gen"
    passive = False
    toggle_key = "image_gen"

    def __init__(self, gen: ImageGen, recent_image=None):
        self.g = gen
        self.recent_image = recent_image or (lambda sess, wxid, sec: "")   # → 同一人 sec 秒内发的图 XML

    @staticmethod
    def _addressed(msg) -> bool:
        return at_me(msg) or bool(getattr(msg, "addressed", ""))

    def match(self, msg) -> bool:
        if msg.event != "AddMsg" or msg.is_self or not msg.is_group or not self.g.available(msg.room_id):
            return False
        if _media_xml(msg) and self.g.has_request(msg.room_id, msg.from_wxid):
            return True
        return msg.msg_type == 1 and self._addressed(msg) and edit_instruction(msg.command_text) is not None

    def handle(self, msg):
        msg.early_replied = True
        sess, who = msg.room_id, msg.from_wxid
        xml = _media_xml(msg)
        if xml:
            ins = self.g.take_request(sess, who)
            if ins:
                return self.g.start_edit(sess, who, xml, ins) or None
        ins = edit_instruction(msg.command_text)
        from features.media_in import quote_image_xml
        raw_content = ""
        try:
            raw_content = ((msg.raw.get("Data") or {}).get("Content") or {}).get("string", "")
        except Exception:
            pass
        xml = quote_image_xml(raw_content) or getattr(msg, "image_xml", "") or self.recent_image(sess, who, EDIT_WAIT_SEC)
        if xml:
            return self.g.start_edit(sess, who, xml, ins) or None
        self.g.remember_request(sess, who, ins)
        return "行，把要改的那张图发上来（或者引用那张图 @我）～"


DESCRIBE_PROMPT = ("群友发了这张图，要求：「{ins}」。你要用 AI 生图模型照着这张图重新画一张满足要求的新图（模型看不到原图，只看你的描述）。\n"
                   "只输出一行 JSON：{{\"real_person\": 原图主体是不是现实中的真人照片(true/false), "
                   "\"character\": 原图主体是不是卡通/动漫/游戏角色或吉祥物(true/false), "
                   "\"prompt\": \"中文生图描述：先把原图的主体、外形、配色、材质、背景、构图写具体，再按要求改，60~200 字，不要出现「原图」「参考图」字样\"}}")


def make_describe(llm_for):
    """→ describe(sess, data_url, instruction) → {real_person, character, prompt}；看图用陪聊同款多模态模型。"""
    def describe(sess, data_url, instruction):
        import json
        from minimax_media import vision_chat_content
        llm = llm_for(sess)
        if llm is None:
            raise RuntimeError("这个会话不能调模型")
        out = llm.chat(vision_chat_content(DESCRIBE_PROMPT.format(ins=instruction[:200]), data_url),
                       system="你是生图提示词助手，只输出 JSON。", max_completion_tokens=2000)
        out = re.sub(r"<think>.*?</think>", "", str(out or ""), flags=re.S)
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            raise RuntimeError("看图描述不是 JSON")
        d = json.loads(m.group(0))
        return {"real_person": bool(d.get("real_person")), "character": bool(d.get("character")),
                "prompt": str(d.get("prompt") or "")[:600]}
    return describe


IMAGE_HINT = re.compile(r"画|生成.{0,4}图|做.{0,3}图|来.{0,3}张|配图|插画|头像|壁纸|表情包")
SPEC = {"type": "function", "function": {
    "name": "generate_image",
    "description": "画一张图（AI 生图，约 20 秒后自动发到群里）。只在对方明确想要你画图/生成图片时用；不画真人、色情、暴力、政治。",
    "parameters": {"type": "object", "properties": {"prompt": {"type": "string", "description": "画面描述（中文即可，具体些：主体、风格、场景）"}},
                   "required": ["prompt"]}}}


class ImageTools:
    names = {"generate_image"}
    ack = False                        # 不垫「我查一下」；模型自己的回复就是「在画了」

    def __init__(self, gen: ImageGen):
        self.g = gen
        self.specs = [SPEC]

    def available(self, sess: str) -> bool:
        return self.g.available(sess)

    @staticmethod
    def wants(text: str, last_user: str = "") -> bool:
        return bool(IMAGE_HINT.search(text or "") or EDIT_RE.search(text or ""))

    def run(self, name: str, args: dict, sess: str, wxid: str) -> str:
        p = str((args or {}).get("prompt") or "").strip()
        if not p:
            return "没有画面描述，没画"
        said = self.g.start(sess, wxid, p)
        return f"（系统已处理，你要跟对方说的意思是：{said}——用你自己的口吻简短说一句，别重复描述画面）"
