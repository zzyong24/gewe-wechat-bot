"""主人静音。

只有主人本人（admin.bot_owners()）在群里 @小助手 / 引用小助手、意思是让它休息 / 闭嘴 / 别说了 / 安静 / 停 → 该群静音：
  陪聊、主动插话、追问接话、主动语音、生图等一切交互都不响应（只做被动统计、记上下文），
  回一句很短的确认（「好，我先歇了」）后不再说话。
只有主人本人说「出来吧 / 可以说话了 / 恢复」之类才解除；其他任何人的解除或反向要求（「先别休息接着说」）一律无视、不回复。
主人说了时长（「休息半小时」「歇 20 分钟」）就按时长自动解除，否则一直静音到他解除。
定时任务（AI 快讯、公众号新文推广、日报）走 router.deliver，不受静音影响。
状态持久化到 data/owner_mute.json（原子写，重启不丢）。

判断：规则先行（强匹配的短指令直接定；含否定/反问/较长的句子交给小模型判「静音 / 解除 / 无」），
小模型失败时只采纳规则的强结论，拿不准就当作普通消息（不静音也不解除）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("feature.owner_mute")

MUTE_ACK = os.environ.get("BOT_MUTE_ACK", "好，我先歇了")
UNMUTE_ACK = os.environ.get("BOT_UNMUTE_ACK", "好，我回来了")

# 让它停的意思（关键词只用来「起疑」，最终结论看规则强匹配或小模型）
MUTE_HINT = re.compile(r"休息|歇|闭嘴|别\s*b\s*b|别逼逼|别哔哔|别说了|别说话|不要说话|少说|别吵|安静|消停|停一下|停下|先停|别回|不要回|"
                       r"别接话|别插话|闭麦|静音|禁言|睡(?:觉|吧|去)|下线|退下|一边去|别出声|shut\s*up|stop", re.I)
UNMUTE_HINT = re.compile(r"出来吧|出来了|出来说|可以说话|能说话了|说话吧|恢复|解除|解禁|回来吧|回来了|上线吧|醒醒|起床|别休息|不用休息|"
                         r"继续说|接着说|接着聊|继续聊|开口|可以(?:回|聊|接话)了|取消静音|unmute", re.I)
# 明确的反向/否定（「别休息」「不用安静」）→ 不是静音
NEG_MUTE = re.compile(r"(?:别|不要|不用|甭|没让你)(?:去)?(?:休息|歇|闭嘴|安静|停)")
# 短句强匹配（去掉 @ 和标点后整句就是这类指令）
STRONG_MUTE = re.compile(r"^(?:你|小助手|那你|好了|行了|好|行|ok|先|赶紧|快|去|[，, ])*"
                         r"(?:休息(?:一下|会儿?|下|吧|去)?|歇(?:会儿?|一会儿?|着|吧)?|闭嘴|安静(?:点|一下|会儿?)?|别\s*b\s*b|别说了|别说话|"
                         r"停(?:一下|下)?|闭麦|静音|睡(?:觉|吧|觉去)?|退下|消停(?:点|会儿?)?|别吵了?)"
                         r"(?:[，, ]|别\s*b\s*b|别说了|别说话|闭嘴|吧|了|啊|哈|去)*$", re.I)
STRONG_UNMUTE = re.compile(r"^(?:小助手|你|好了|好|行|ok)*[，, ]*(?:出来吧|出来|可以说话了|能说话了|说话吧|恢复(?:吧|一下|说话)?|解除(?:静音)?|"
                           r"回来吧|回来|取消静音|醒醒|起床了?|上线吧)(?:吧|了|啦|哈)*$", re.I)
_PUNCT = re.compile(r"[\s。.!！?？~～、…]+")
_DUR = re.compile(r"(半|\d+|[一二两三四五六七八九十]+)\s*(?:个)?\s*(分钟|分|小时|钟头|h|min)", re.I)
_CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

JUDGE_PROMPT = ("你在判断群主主人对群里的机器人「小助手」说的这句话是什么意思。\n"
                "A = 让小助手停下来：休息 / 闭嘴 / 别说了 / 安静 / 别回话\n"
                "B = 让小助手恢复说话：出来吧 / 可以说话了 / 继续说\n"
                "C = 都不是（普通聊天、问问题、开玩笑地提到这些词、在说别人）\n"
                "{state}\n主人说：「{text}」\n只输出一个字母 A、B 或 C。")


def _cn_num(s: str) -> float:
    if s == "半":
        return 0.5
    if s.isdigit():
        return float(s)
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        return (_CN.get(a, 1)) * 10 + _CN.get(b, 0)
    return float(_CN.get(s, 0))


def parse_duration(text: str) -> float | None:
    """「休息半小时」→ 1800；「歇 20 分钟」→ 1200；没说时长 → None（一直静音到主人解除）。"""
    m = _DUR.search(text or "")
    if not m:
        return None
    n, unit = _cn_num(m.group(1)), m.group(2).lower()
    if n <= 0:
        return None
    sec = n * (3600 if unit in ("小时", "钟头", "h") else 60)
    return sec if 60 <= sec <= 7 * 86400 else None


def _strip(text: str, bot_names=("小助手",)) -> str:
    t = re.sub(r"@[^\s\u2005]+[\u2005\s]*", "", text or "")
    for n in bot_names:
        t = t.replace(n, "")
    return _PUNCT.sub("", t).strip()


def rule_intent(text: str) -> tuple[str | None, bool]:
    """→ (intent, decisive)。intent ∈ {"mute","unmute",None}；decisive=False 表示拿不准，要问小模型。"""
    t = _strip(text)
    if not t:
        return None, True
    t_nodur = _DUR.sub("", t)
    short = len(t) <= 24
    if short and STRONG_UNMUTE.match(t):
        return "unmute", True
    if short and STRONG_MUTE.match(t_nodur) and not NEG_MUTE.search(t):
        return "mute", True
    um, mu = bool(UNMUTE_HINT.search(t)), bool(MUTE_HINT.search(t))
    if NEG_MUTE.search(t):
        um, mu = True, False
    if not (um or mu):
        return None, True            # 跟静音无关的普通消息：不花模型
    return ("unmute" if um and not mu else "mute" if mu and not um else None), False


class OwnerMute:
    def __init__(self, path: str | Path | None = None, owners: Callable[[], set] | None = None,
                 judge: Callable[[str, str, bool], str | None] | None = None, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path else None
        self._owners = owners
        self.judge = judge            # judge(room, text, muted) -> "mute"/"unmute"/None（小模型）；None=只用规则
        self.clock = clock
        self._lock = threading.RLock()
        self._rooms: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                self._rooms = dict(json.loads(self.path.read_text(encoding="utf-8")).get("rooms") or {})
            except (OSError, ValueError):
                log.exception("读静音状态失败（当作都没静音，不覆盖原文件）")

    def owners(self) -> set:
        if self._owners is not None:
            return set(self._owners())
        from features.admin import bot_owners
        return bot_owners()

    # ---- 状态 ----
    def is_muted(self, room: str | None) -> bool:
        if not room:
            return False
        with self._lock:
            st = self._rooms.get(room)
            if not st or not st.get("muted"):
                return False
            until = st.get("until")
            if until and self.clock() >= float(until):
                st["muted"] = False
                st["ended"] = self.clock()
                st["end_reason"] = "到期"
                self._save()
                log.info("静音到期自动解除 %s", room)
                return False
            return True

    def mute(self, room: str, by: str, seconds: float | None = None, text: str = "") -> None:
        with self._lock:
            now = self.clock()
            self._rooms[room] = {"muted": True, "since": now, "until": (now + seconds) if seconds else None,
                                 "by": by, "text": (text or "")[:80]}
            self._save()
        log.info("主人静音 %s by=%s until=%s", room, by, "无" if not seconds else int(seconds))

    def unmute(self, room: str, by: str, text: str = "") -> None:
        with self._lock:
            st = self._rooms.setdefault(room, {})
            st.update(muted=False, ended=self.clock(), end_reason=f"{by}：{(text or '')[:40]}")
            self._save()
        log.info("主人解除静音 %s by=%s", room, by)

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"rooms": self._rooms}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # ---- 判断 ----
    @staticmethod
    def addressed(msg) -> bool:
        return (msg.self_wxid in (msg.at_list or []) or getattr(msg, "addressed", "") == "quote"
                or (getattr(msg, "quote_from", "") == msg.self_wxid and bool(getattr(msg, "quote", ""))))

    def intent(self, msg) -> str | None:
        """主人本人 @/引用小助手的群消息 → "mute"/"unmute"/None；其他人一律 None（不认）。"""
        if not msg.is_group or msg.is_self or msg.msg_type != 1 or msg.from_wxid not in self.owners():
            return None
        if not self.addressed(msg):
            return None
        text = msg.content or ""
        it, decisive = rule_intent(text)
        if decisive or self.judge is None:
            return it if decisive else None
        try:
            out = self.judge(msg.room_id, _strip(text)[:200], self.is_muted(msg.room_id))
        except Exception as e:
            log.warning("静音判断小模型失败，按规则不确定处理: %s", e)
            return None
        return out if out in ("mute", "unmute") else None


def make_judge(llm_for):
    """小模型判断：只在规则拿不准时调一次；出错/输出不认识 → None。"""
    def judge(room, text, muted):
        llm = llm_for(room)
        if llm is None:
            return None
        state = "（小助手现在正在静音中。）" if muted else "（小助手现在正常说话中。）"
        out = llm.chat(JUDGE_PROMPT.format(state=state, text=text), system="你只输出 A、B 或 C。", max_completion_tokens=1200)
        out = re.sub(r"<think>.*?</think>", "", str(out or ""), flags=re.S).strip().upper()
        return {"A": "mute", "B": "unmute"}.get(out[:1])
    return judge
