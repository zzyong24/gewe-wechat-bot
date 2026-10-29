"""对话接话：让「不 @ 的追问」和「引用小助手」也能被接住（router.dispatch 开头调用 prepare）。

1) 引用回复（appmsg type=57，refermsg）：转成普通文本消息（msg_type=1，content=新说的话，quote=被引用内容）；
   被引用的是小助手自己 → 视同 @小助手（把机器人 wxid 加进 at_list，addressed="quote"）。
2) 追问窗口：小助手在群里回复了某人（陪聊 / 地图）之后 BOT_FOLLOWUP_SEC 秒内（默认 180），
   同一个人的下一条文本即使不 @ 也当作对小助手说的（addressed="followup"）。防抢话：
   · 只对被回复的那个人生效，别人说话不算；
   · 一个窗口里最多接 BOT_FOLLOWUP_MAX 条（默认 4），用户重新 @ 小助手才重置；
   · 消息里 @ 了别人（含 @所有人）→ 不接，并关闭窗口；
   · 引用别人的消息：窗口内且（发言人是主人 bot_owners，或内容像对小助手的指令，如「去这里查」「你看看这个」）→ 接，
     被引用内容带进上下文；否则当作在回那个人 → 不接，关窗口；
   · 太长（>120 字）的不接（多半在跟别人长聊）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
import xml.etree.ElementTree as ET

FOLLOWUP_FEATURES = {"chat_companion", "amap", "image_gen"}
SEMANTIC_SEC = 1800          # 窗口过期后，30 分钟内聊过 → 交给模型判断是不是接着聊
# 明显是在追问上一个话题
FOLLOW_RE = re.compile(r"啥意思|什么意思|是啥|是什么|怎么(?:进|弄|搞|办|做|申请|报名|用|说)|^那.{0,12}呢|详细(?:说|讲|介绍)|展开(?:说|讲)|具体(?:说|讲|点)|"
                       r"然后呢|还有呢|为啥|为什么|靠谱吗|真的吗|多少钱|在哪(?:里)?(?:申请|报|看)|举个例子|比如呢")
# 像是对小助手说的祈使句 / 指令（用于「窗口内引用别人的消息」时判断要不要接）
DIRECTIVE = re.compile(r"^(?:那)?(?:你)?(?:去|帮我?|给我|麻烦|请|看看|看下|看一下|瞧瞧|查|搜|找|问问|试试|按这个|照这个|用这个|就这个|就这家|这个|这家|那家)"
                       r"|小助手|你(?:帮|去|看|查|搜|找|问|试|说|觉得)")


def _owners():
    from features.admin import bot_owners
    return bot_owners()


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def parse_quote(content: str):
    """返回 (新说的话, 被引用内容, 被引用发送人 wxid, 被引用人昵称)；不是引用消息返回 None。"""
    text = (content or "").strip()
    i = text.find("<")
    if i < 0 or "<refermsg>" not in text:
        return None
    try:
        root = ET.fromstring(text[i:])
    except ET.ParseError:
        return None
    app = root.find("appmsg") if root.tag == "msg" else root
    if app is None or (app.findtext("type") or "").strip() != "57":
        return None
    ref = app.find("refermsg")
    if ref is None:
        return None
    rtype = (ref.findtext("type") or "").strip()
    rc = (ref.findtext("content") or "").strip()
    if rtype == "49":
        from features.article import parse_card
        card = parse_card(rc)
        if card:
            rc = f"[公众号文章《{card['title']}》]"
    if rtype not in ("1", "49") or (rtype == "49" and not rc.startswith("[公众号文章")):
        rc = {"3": "[图片]", "34": "[语音]", "43": "[视频]", "47": "[表情]", "48": "[位置]"}.get(rtype, "[消息]")
    return (app.findtext("title") or "").strip(), rc[:200], (ref.findtext("chatusr") or "").strip(), \
        (ref.findtext("displayname") or "").strip()


class Followup:
    def __init__(self, sec: int | None = None, max_n: int | None = None, now=None, owners=None,
                 owner_only_rooms: set | None = None, off_rooms: set | None = None, path=None):
        """now：测试注入的时钟；不注入时用消息自己的 CreateTime（回放/积压时也按消息时间算窗口）。"""
        self.sec = sec if sec is not None else _env_int("BOT_FOLLOWUP_SEC", 600)
        self.max_n = max_n if max_n is not None else _env_int("BOT_FOLLOWUP_MAX", 20)   # 安全阀：连续聊每回一次就续期
        self.max_len = _env_int("BOT_FOLLOWUP_MAX_LEN", 400)
        self.judge = None        # judge(room, wxid, text, last_bot_text, recent_lines) → bool：语义判断（app 绑定 MiniMax）
        self.recent: dict[str, deque] = {}                     # room → 最近发言 (wxid, t, text)，判断中间有没有人插话
        self.room_last: dict[str, dict] = {}                   # room → 小助手最近一次回复 {"to", "t", "text"}
        self.owners = owners if owners is not None else _owners
        self.owner_only_rooms = set(owner_only_rooms or ())   # 这些群：追问窗口只对主人本人生效（大群防误接）
        self.off_rooms = set(off_rooms or ())
        self.busy = None                                       # busy(room) → True 时窗口暂停（app 绑定：本群有进行中的游戏）                 # 这些群：追问窗口全关（引用小助手仍算 @）
        self.now, self.lock = now, threading.Lock()
        self.win: dict[tuple[str, str], dict] = {}
        self.path = path                                       # 落盘（data/followup.json）：重启不丢追问窗口；None=只在内存
        self._load()

    def _load(self) -> None:
        if not self.path:
            return
        try:
            data = json.loads(open(self.path, encoding="utf-8").read())
        except (OSError, ValueError):
            return
        now = time.time()
        for k, w in (data or {}).items():
            room, _, wxid = k.partition("|")
            try:
                if room and wxid and max(float(w["until"]), float(w.get("last", 0)) + SEMANTIC_SEC) >= now:
                    self.win[(room, wxid)] = {"until": float(w["until"]), "used": int(w.get("used", 0)),
                                              "last": float(w.get("last", 0)), "text": str(w.get("text", ""))[:200]}
                    lr = self.room_last.get(room)
                    if w.get("last") and (not lr or lr["t"] < float(w["last"])):
                        self.room_last[room] = {"to": wxid, "t": float(w["last"]), "text": str(w.get("text", ""))[:200]}
            except (KeyError, TypeError, ValueError):
                continue

    def _save(self) -> None:
        """调用方持锁。只存没过期的窗口，原子写 + 600。"""
        if not self.path:
            return
        now = time.time()
        data = {f"{r}|{w}": v for (r, w), v in self.win.items()
                if max(v["until"], v.get("last", 0) + SEMANTIC_SEC) >= now - 60}
        try:
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _clock(self) -> float:
        return self.now() if self.now is not None else time.time()

    def _t(self, msg) -> float:
        if self.now is not None:
            return self.now()
        return float(getattr(msg, "create_time", 0) or time.time())

    def prepare(self, msg, allow_window: bool = True) -> None:
        if msg.event != "AddMsg" or msg.is_self or not msg.is_group:
            return
        if msg.msg_type == 49:
            q = parse_quote(msg.content)
            if q:
                msg.msg_type, msg.content, msg.quote, msg.quote_from, msg.quote_name = 1, q[0], q[1], q[2], q[3]
                if q[2] and q[2] == msg.self_wxid and msg.self_wxid not in msg.at_list:
                    msg.at_list = list(msg.at_list) + [msg.self_wxid]
                    msg.addressed = "quote"
        if msg.msg_type != 1:
            return
        key = (msg.room_id, msg.from_wxid)
        if self.busy is not None and self.busy(msg.room_id):
            return                               # 本群正在玩游戏：不 @ 的话都是游戏输入/闲聊，不当追问抢话
        if msg.room_id in self.off_rooms or (msg.room_id in self.owner_only_rooms and msg.from_wxid not in self.owners()):
            return                               # 窗口不适用（引用小助手已在上面视同 @）
        others = [w for w in msg.at_list if w != msg.self_wxid]
        with self.lock:
            w = self.win.get(key)
            if others:
                if self.win.pop(key, None) is not None:   # @了别人 → 明显在跟别人说话，不接，关窗口
                    self._save()
                return
            if msg.quote_from and msg.quote_from != msg.self_wxid:
                # 引用别人的消息：窗口内、且是主人或像在对小助手下指令（「去这里查」「你看看这个」）→ 接，被引用内容当上下文；
                # 否则当作在跟被引用的人说话 → 不接，关窗口
                if not (w and (msg.from_wxid in self.owners() or DIRECTIVE.search(msg.content or ""))):
                    if self.win.pop(key, None) is not None:
                        self._save()
                    return
            if msg.self_wxid in msg.at_list or not allow_window:
                return
            t = self._t(msg)
            if w and t <= w["until"] and w["used"] < self.max_n and len(msg.content or "") <= self.max_len:
                w["used"] += 1
                self._save()
                hit = True
            else:
                hit = False
                cand = self._semantic_candidate(msg, w, t)
        if not hit:
            if not cand:
                return
            try:
                ok = bool(self.judge(msg.room_id, msg.from_wxid, msg.content or "", cand["text"], cand["lines"]))
            except Exception:
                ok = False
            if not ok:
                return
            msg.addressed = "followup_judged"
            msg.at_list = list(msg.at_list) + [msg.self_wxid]
            return
        msg.at_list = list(msg.at_list) + [msg.self_wxid]
        msg.addressed = "followup"

    def _semantic_candidate(self, msg, w, t):
        """窗口外：30 分钟内跟小助手聊过 / 明显在追问上一个话题，且中间没别人插话 → 交给模型判断（调用方持锁）。"""
        if self.judge is None or msg.room_id in self.owner_only_rooms or msg.quote_from:
            return None
        text = msg.content or ""
        if not text.strip() or len(text) > self.max_len:
            return None
        base = None
        if w and w.get("last") and t - w["last"] <= SEMANTIC_SEC:
            base = {"t": w["last"], "text": w.get("text", ""), "to": msg.from_wxid}
        else:
            lr = self.room_last.get(msg.room_id)
            if lr and t - lr["t"] <= SEMANTIC_SEC and FOLLOW_RE.search(text):
                base = lr
        if not base:
            return None
        rec = [r for r in self.recent.get(msg.room_id, ()) if r[1] > base["t"]]
        if any(r[0] not in (msg.from_wxid, base["to"]) for r in rec):
            return None                                  # 中间有别人插话 → 不接
        return {"text": base["text"], "lines": [r[2] for r in rec][-6:]}

    def observe(self, msg) -> None:
        """记录群里每条发言（router 在 prepare 之后调用），用来判断有没有人插话。"""
        if not getattr(msg, "is_group", False) or getattr(msg, "is_self", False) or not msg.room_id:
            return
        with self.lock:
            q = self.recent.setdefault(msg.room_id, deque(maxlen=30))
            q.append((msg.from_wxid, self._t(msg), (msg.content or "")[:80] if msg.msg_type == 1 else "[非文本]"))

    def note_reply(self, msg, feature: str, reply_text: str = "") -> None:
        """某功能回复了 msg 之后调用：给这个人开 / 续追问窗口。从「小助手实际发出回复」的时刻起算（当前时钟，补处理也一样）。"""
        if feature not in FOLLOWUP_FEATURES or not msg.is_group or self.sec <= 0:
            return
        key = (msg.room_id, msg.from_wxid)
        now = self._clock()
        with self.lock:
            w = self.win.get(key)
            used = w["used"] if (w and msg.addressed == "followup") else 0   # 真 @ / 引用 / 语义判断接上 → 重置计数
            text = (reply_text or (w or {}).get("text", ""))[:200]
            self.win[key] = {"until": now + self.sec, "used": used, "last": now, "text": text}
            self.room_last[msg.room_id] = {"to": msg.from_wxid, "t": now, "text": text}
            self._save()

    def active(self, room: str, wxid: str, at: float | None = None) -> bool:
        w = self.win.get((room, wxid))
        t = at if at is not None else (self.now() if self.now else time.time())
        return bool(w) and t <= w["until"]


JUDGE_PROMPT = """微信群里，AI 小伙伴「小助手」之前回复过一位群友。判断这位群友的新消息是不是在接着跟小助手聊（追问/接话/回应小助手说的内容）。

小助手上一次说的话：
{bot}

那之后群里的发言（按时间）：
{lines}

这位群友（{who}）的新消息：
{text}

如果新消息是在问小助手、追问小助手刚说的内容、或者回应小助手，回答「是」；如果更像在跟别人说话、自言自语、换了无关话题，回答「否」。只输出「是」或「否」。"""


def make_judge(llm_for, name_of=None):
    """→ judge(room, wxid, text, bot_text, lines) → bool。便宜快速：一次短调用，出错一律不接。"""
    def judge(room, wxid, text, bot_text, lines):
        llm = llm_for(room)
        if llm is None:
            return False
        who = (name_of(room, wxid) if name_of else "") or "群友"
        prompt = JUDGE_PROMPT.format(bot=(bot_text or "（不记得了）")[:200], lines="\n".join(lines) or "（无）",
                                     who=who, text=text[:300])
        out = llm.chat(prompt, system="你只输出「是」或「否」。", max_completion_tokens=1500)
        out = re.sub(r"<think>.*?</think>", "", str(out or ""), flags=re.S).strip()
        return out.startswith("是")
    return judge
