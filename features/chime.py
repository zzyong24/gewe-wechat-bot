"""主动插话（开关 chime，默认关）：群里真活跃时，小助手隔一阵用本群人设口吻冒一句跟当前话题相关的话。

安全阀（都可用 env 配置）：
  1. 每群冷却 BOT_CHIME_COOLDOWN_MIN（默认 40 分钟）：任何一次尝试（不论发没发）后都要等满冷却，省额度。
  2. 活跃门槛：最近 BOT_CHIME_WINDOW_MIN（默认 5）分钟内 ≥ BOT_CHIME_MIN_SPEAKERS（默认 3）个不同的人、
     共 ≥ BOT_CHIME_MIN_MSGS（默认 6）条文字消息；满足后每条新消息再按 BOT_CHIME_PROB（默认 0.35）掷骰，避免一达标就秒插。
  3. 每群每天上限 BOT_CHIME_DAILY_MAX（默认 4）次（按 Asia/Shanghai 自然日，状态存 data/chime_state.json，重启不丢）。
  4. 上下文：把窗口内最近 ≤12 条群消息（昵称: 内容）给模型；模型判断不适合插话时输出 SKIP → 不发。
  模型只给模型白名单会话（= 真发白名单）；拿不到模型 / 调不通 / 空回复 / SKIP → 不发（不回退模板）。
  观察群（router.block_targets）直接跳过，连模型都不调。真发仍走 router 发送闸。
插话判断（开关 chime_judge，默认关，只在开了 chime 的群生效；借鉴 MaiBot 的「先判断要不要说」，宁少勿多）：
  过了上面所有安全阀之后、生成之前再筛一道：
  a. 小助手最近 BOT_CHIME_SELF_GAP_MIN（默认 15）分钟内在本群说过话 → 不插；
  b. 最后一条是 @ 别人 / 引用别人的（有人在等特定的人回答）→ 不插；
  c. 轻量模型判断（≤80 token）：话题相关度 0~10、是否有人在等群里回答、插话是否自然 → 只有 should=true 且相关度 ≥ BOT_CHIME_JUDGE_MIN（默认 7；有人在等群里回答时降到 5）才生成。
  判断不通过也算一次尝试（冷却照走）。
注册在最后（passive）：消息被指令/游戏/@陪聊 等主动功能吃掉时不会走到这里，避免抢话。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable

from features.common import TZ
from features.clock import now_line
from features.humanlike import split_messages, with_style

log = logging.getLogger("feature.chime")

PROMPT = ("下面是群里最近的聊天记录（格式：昵称: 内容）：\n{ctx}\n\n"
          "你作为群里的一员，自然地插一句跟当前话题相关的话（接梗、补充、提问或轻松吐槽都行），"
          "不超过 40 字（可以分成 1~2 行短句）；不要 @ 任何人，不要复述别人的原话，不要自称 AI，不要说教。"
          "如果话题敏感、涉及隐私、有人在吵架，或者你没什么好接的，就只输出 SKIP。直接输出这句话本身。")

JUDGE = ("下面是群里最近的聊天（昵称: 内容），你是群里的机器人群友「小助手」：\n{ctx}\n\n"
         "判断现在要不要主动插一句话。宁少勿多：大家聊得正投入、话题跟你无关、私人话题、有人在等某个具体的人回答、吵架、刚有人说了很完整的结论，都不要插。"
         "只输出 JSON：{{\"related\": 0到10的整数（话题和你能接上的相关度/你能提供的价值）, \"waiting\": true/false（有没有一个抛给群里、还没人回答的问题）, "
         "\"should\": true/false}}")


def _env(name, default, cast=float):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


class ChimeFeature:
    name = "chime"
    passive = True
    toggle_key = "chime"

    def __init__(self, llm_for: Callable[[str], object], persona_of: Callable[[str], str],
                 name_of: Callable[[str, str], str] | None = None, blocked: Callable[[str], bool] | None = None,
                 state_path: str | Path | None = None, rng=None, clock: Callable[[], float] = time.time,
                 cooldown_min: float | None = None, daily_max: int | None = None, min_speakers: int | None = None,
                 window_min: float | None = None, min_msgs: int | None = None, prob: float | None = None,
                 judge_on: Callable[[str], bool] | None = None, last_bot_ts: Callable[[str], float] | None = None,
                 learned=None):
        self.llm_for, self.persona_of, self.name_of = llm_for, persona_of, name_of
        self.blocked = blocked or (lambda room: False)
        self.rng, self.clock = rng or random.Random(), clock
        self.cooldown = 60 * (cooldown_min if cooldown_min is not None else _env("BOT_CHIME_COOLDOWN_MIN", 40))
        self.daily_max = daily_max if daily_max is not None else _env("BOT_CHIME_DAILY_MAX", 4, int)
        self.min_speakers = min_speakers if min_speakers is not None else _env("BOT_CHIME_MIN_SPEAKERS", 3, int)
        self.window = 60 * (window_min if window_min is not None else _env("BOT_CHIME_WINDOW_MIN", 5))
        self.min_msgs = min_msgs if min_msgs is not None else _env("BOT_CHIME_MIN_MSGS", 6, int)
        self.prob = prob if prob is not None else _env("BOT_CHIME_PROB", 0.35)
        self.learned = learned
        self.judge_on = judge_on or (lambda room: False)
        self.last_bot_ts = last_bot_ts or (lambda room: 0.0)
        self.self_gap = 60 * _env("BOT_CHIME_SELF_GAP_MIN", 15)
        self.judge_min = _env("BOT_CHIME_JUDGE_MIN", 7, int)
        self.state_path = Path(state_path) if state_path else None
        self._buf: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}       # room → {last: ts, day: 'YYYY-MM-DD', count: n}
        if self.state_path and self.state_path.exists():
            try:
                self._state = json.loads(self.state_path.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                pass

    # ---- 状态 ----
    def _day(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")

    def count_today(self, room: str, now: float | None = None) -> int:
        st = self._state.get(room) or {}
        return st.get("count", 0) if st.get("day") == self._day(now or self.clock()) else 0

    def _save(self):
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_path)

    # ---- feature ----
    def match(self, msg) -> bool:
        return (msg.event == "AddMsg" and msg.is_group and msg.msg_type == 1 and not msg.is_self
                and msg.self_wxid not in msg.at_list and bool((msg.content or "").strip()))

    def why_not(self, room: str, now: float) -> str | None:
        """返回不插话的原因；None = 可以考虑插。"""
        if self.blocked(room):
            return "观察群"
        st = self._state.get(room) or {}
        if now - st.get("last", 0) < self.cooldown:
            return "冷却中"
        if self.count_today(room, now) >= self.daily_max:
            return "达日上限"
        recent = [m for m in self._buf.get(room, ()) if now - m[0] <= self.window]
        if len(recent) < self.min_msgs or len({m[1] for m in recent}) < self.min_speakers:
            return "活跃度不够"
        return None

    def judge(self, room, msg, llm, ctx, now) -> str | None:
        """插话判断：返回不插的原因；None = 可以插。"""
        try:
            if now - float(self.last_bot_ts(room) or 0) < self.self_gap:
                return "刚说过话"
        except Exception:
            pass
        if getattr(msg, "at_list", None) or getattr(msg, "quote", "") or re.search(r"@\S", msg.content or ""):
            return "在等特定的人回答"
        try:
            out = llm.chat(JUDGE.format(ctx=ctx), system="你是冷静的旁观者，只输出 JSON。", max_completion_tokens=80)
            m = re.search(r"\{.*?\}", out or "", re.S)
            d = json.loads(m.group(0)) if m else {}
        except Exception as e:
            return f"判断失败 {str(e)[:60]}"
        rel = int(d.get("related", 0) or 0)
        need = self.judge_min - (2 if d.get("waiting") else 0)      # 有人抛给群里的问题没人答 → 门槛降 2
        if not d.get("should") or rel < need:
            return f"相关度 {rel} / should={d.get('should')} / waiting={d.get('waiting')}"
        return None

    def handle(self, msg):
        room, now = msg.room_id, self.clock()
        if msg.create_time and now - msg.create_time > 120:
            return None                      # 重启后补拉的旧消息不算活跃
        name = (self.name_of(room, msg.from_wxid) if self.name_of else "") or msg.from_wxid
        buf = self._buf.setdefault(room, deque(maxlen=30))
        buf.append((now, msg.from_wxid, name, msg.content.strip()[:200]))
        with self._lock:
            reason = self.why_not(room, now)
            if reason:
                return None
            if self.rng.random() >= self.prob:
                return None
            llm = self.llm_for(room)
            if llm is None:
                return None                  # 非模型白名单 / 无模型 → 不发
            st = self._state.setdefault(room, {})
            st["last"] = now                 # 尝试即进入冷却（失败/SKIP 也算），防止反复烧额度
            self._save()
        ctx = "\n".join(f"{n}: {t}" for ts, _, n, t in list(buf)[-12:] if now - ts <= self.window)
        if self.judge_on(room):
            why = self.judge(room, msg, llm, ctx, now)
            if why:
                log.info("插话 %s：判断不插（%s）", room, why)
                return None
        try:
            sysp = with_style(self.persona_of(room) if self.persona_of else "") + "\n" + now_line()
            if self.learned is not None:
                ln = self.learned.prompt_for(room)
                if ln:
                    sysp += "\n\n" + ln
            text = llm.chat(PROMPT.format(ctx=ctx), system=sysp)
        except Exception as e:
            log.warning("插话调模型失败，不发: %s", str(e)[:200])
            return None
        if "SKIP" in (text or "").upper():
            text = ""
        lines = [re.sub(r"@\S+\s*", "", ln).strip().strip("\"“”「」'").strip()[:80] for ln in (text or "").splitlines()]
        parts = split_messages("\n".join(ln for ln in lines if ln), n_max=2)   # 插话最多 2 条
        text = "\n".join(parts)
        if not text:
            log.info("插话 %s：模型选择不插（SKIP/空）", room)
            return None
        with self._lock:
            st = self._state.setdefault(room, {})
            day = self._day(now)
            st["count"] = (st.get("count", 0) if st.get("day") == day else 0) + 1
            st["day"] = day
            self._save()
        from router import Reply
        return Reply(room, text, parts=parts if len(parts) > 1 else None)
