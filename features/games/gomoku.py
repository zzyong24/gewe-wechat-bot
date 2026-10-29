"""双人五子棋（两个群友一起下，小助手只当裁判、摆棋盘）。
开关 game_gomoku（默认关，只在示例AI交流群开），纯规则，不调模型。

  五子棋 / 下五子棋 @某人        开局（可带 @小助手）；点名=邀请，对方回「加入/接受」；不点名=第一个发「加入」的人
  55 / 5 5 / 5,5                  轮到的人落子：第 5 行第 5 列
  认输 / 结束五子棋               玩家认输 / 结束（主人本人也能强制结束）；@小助手 结束游戏 也行

一群一局；开局人执黑⚫先下，对手执白🟡。等人 3 分钟没人加入自动取消，10 分钟没人落子自动结束。
棋局状态写 data/games/gomoku.json，poller 重启后接着下。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from features.games import Game

log = logging.getLogger("games.gomoku")

SIZE = 9
EMPTY, BLACK, WHITE = "⬜", "⚫", "🟡"
LABELS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
CORNER, COL_MARK, ROW_MARK = "🔢", "⬇️", "➡️"     # 刚下那一子：所在列号换成 ⬇️、行号换成 ➡️
WAIT_TIMEOUT = 180
RULES = ("\n规则：9×9 棋盘，开局人执黑⚫先下；轮到谁谁发两位数落子，先行后列，「55」= 第5行第5列；"
         "先连成五子获胜；「认输」「结束五子棋」随时结束。\n")
MOVE_TIMEOUT = 600

# 开局（10-28 修）：消息里去掉所有 @ 后，「五子棋」「下一局五子棋」「你们两下一局五子棋」「我们来下盘五子棋吧」都算；
# 问句/否定（会下五子棋吗、不下五子棋、五子棋怎么玩）不算
_START_EXACT = re.compile(r"^(?:下|来|玩|开)?(?:一|个|一局|一把|局|把|盘|一盘)?\s*五子棋(?:吧|呀|啊|呗)?[!！。~～]*$")
_START = re.compile(r"(?:下|来|玩|开|整|搞|开始)\s*(?:一|个|一个|一局|一把|一盘|局|把|盘)?\s*五子棋")
_START_NO = re.compile(r"吗|么|[?？]|怎么|规则|不|别|没|结束|取消|会下")


def is_start(t: str) -> bool:
    t = (t or "").strip()
    if not t or len(t) > 30:
        return False
    if _START_EXACT.match(t):
        return True
    return bool(_START.search(t)) and not _START_NO.search(t)
# 加入（10-28 修）：只看开头，「加入！行吧 五子棋 我没输过」也算（机器人乙就是这么回的）
_JOIN = re.compile(r"^(?:我)?(?:加入|接受|应战|参加|来下|我来|来吧|接招)")
_REFUSE = re.compile(r"^(?:拒绝|不下|不玩|不来)[!！。~～]*$")
_CANCEL = re.compile(r"^(?:取消|取消五子棋|结束五子棋|不下了|结束棋局)[!！。~～]*$")
_END = re.compile(r"^(?:结束五子棋|结束棋局|不下了)[!！。~～]*$")
_RESIGN = re.compile(r"^(?:认输|投降|我认输)[!！。~～]*$")
_MOVE = re.compile(r"^(\d{1,2})\s*[,，、.。\s]\s*(\d{1,2})$|^(\d)(\d)$")
_STRIP_AT = re.compile(r"@[^\s\u2005]+[\u2005\s]?")


def _text(msg) -> str:
    return _STRIP_AT.sub("", msg.content or "").strip()


def parse_move(t: str):
    """「55」「5 5」「5,5」→ (5, 5)（1 起）；不像落子 → None。越界也返回数字，交给调用方提示。"""
    m = _MOVE.match((t or "").strip())
    if not m:
        return None
    a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    return int(a), int(b)


def render(board, last=None) -> str:
    """文字棋盘：首行列号，每行前行号；刚下那一子所在列/行的号码换成 ⬇️/➡️。"""
    lr, lc = last or (None, None)
    head = CORNER + "".join(COL_MARK if c == lc else LABELS[c] for c in range(SIZE))
    rows = [head]
    for r in range(SIZE):
        cells = "".join({0: EMPTY, 1: BLACK, 2: WHITE}[board[r][c]] for c in range(SIZE))
        rows.append((ROW_MARK if r == lr else LABELS[r]) + cells)
    return "\n".join(rows)


def five(board, r, c) -> bool:
    who = board[r][c]
    for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
        n = 1
        for s in (1, -1):
            rr, cc = r + dr * s, c + dc * s
            while 0 <= rr < SIZE and 0 <= cc < SIZE and board[rr][cc] == who:
                n += 1
                rr, cc = rr + dr * s, cc + dc * s
        if n >= 5:
            return True
    return False


class GomokuStore:
    """棋局落盘（原子写）。path=None 时不落盘（测试默认）。"""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()

    def load(self) -> dict:
        if not self.path or not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (ValueError, OSError):
            log.warning("五子棋存档读不了，忽略")
            return {}

    def _write(self, d: dict):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        try:                                   # poller 以 root 跑：跟 data 目录保持同一属主
            st = self.path.parent.stat()
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def save(self, game: "GomokuGame"):
        with self._lock:
            d = self.load()
            d[game.room] = game.to_state()
            self._write(d)

    def drop(self, room: str):
        with self._lock:
            d = self.load()
            if room in d:
                d.pop(room)
                self._write(d)


class GomokuGame(Game):
    kind = "game_gomoku"
    title = "五子棋"

    def __init__(self, room, host, host_name="", invitee=None, invitee_name="", store=None,
                 clock=time.time, owners=None, quiet=None, name_of=None):
        super().__init__(room)
        self.clock, self.store = clock, store
        self.name_of = name_of or (lambda r, w: "")
        self.owners = owners or (lambda: set())
        self.quiet = quiet or (lambda room: False)
        self.host, self.invitee = host, invitee
        self.names = {host: host_name or "黑方"}
        if invitee:
            self.names[invitee] = invitee_name or "对手"
        self.black, self.white = host, None
        self.board = [[0] * SIZE for _ in range(SIZE)]
        self.turn, self.last, self.moves = 1, None, 0
        self.phase = "wait"
        self.started = self.last_active = self.clock()

    # ---- 持久化 ----
    def to_state(self) -> dict:
        return {k: getattr(self, k) for k in ("host", "invitee", "names", "black", "white", "board", "turn",
                                               "last", "moves", "phase", "started", "last_active")}

    @classmethod
    def from_state(cls, room, d, **kw):
        g = cls(room, d["host"], store=kw.pop("store", None), **kw)
        for k, v in d.items():
            setattr(g, k, v)
        g.last = tuple(g.last) if g.last else None
        return g

    def _save(self):
        if self.store:
            try:
                self.store.save(self)
            except OSError:
                log.exception("五子棋存档失败")

    def _drop(self):
        self.over = True
        if self.store:
            try:
                self.store.drop(self.room)
            except OSError:
                log.exception("五子棋删存档失败")

    def touch(self):            # 只有开局/落子算「有动作」，旁人发消息不续命（GameInputFeature 会调）
        pass

    # ---- 文案 ----
    def nm(self, w):
        return self.names.get(w) or ("黑方" if w == self.black else "白方")

    def _reply(self, text, ats=()):
        from router import Reply
        ats = [w for w in ats if w]
        return Reply(self.room, text, ats=",".join(dict.fromkeys(ats)) or None)

    def intro(self):
        if self.invitee:
            return self._reply(f"♟️ {self.nm(self.host)} 邀请 @{self.nm(self.invitee)} 下五子棋！回「加入」或「接受」就开始，"
                               f"回「拒绝」就算了。3 分钟没回自动取消。" + RULES + render(self.board), [self.invitee])
        return self._reply(f"♟️ {self.nm(self.host)} 想下五子棋，谁来？第一个发「加入」的人当对手。3 分钟没人加入自动取消。"
                           + RULES + render(self.board))

    # ---- 消息 ----
    def on_message(self, msg):
        w, t = msg.from_wxid, _text(msg)
        is_owner = w in self.owners()
        if self.phase == "wait":
            return self._on_wait(msg, w, t, is_owner)
        players = (self.black, self.white)
        if _END.match(t) and (w in players or is_owner):
            self._drop()
            who = "主人" if is_owner and w not in players else self.nm(w)
            return self._reply(f"🛑 {who} 结束了这局五子棋（共 {self.moves} 手，不计胜负）。\n" + render(self.board, self.last))
        if _RESIGN.match(t) and w in players:
            other = self.white if w == self.black else self.black
            self._drop()
            return self._reply(f"🏳️ {self.nm(w)} 认输，@{self.nm(other)} {self._stone(other)} 获胜！共 {self.moves} 手。", [other])
        mv = parse_move(t)
        if mv is None:
            return None                                   # 不是棋局消息：照常走陪聊等
        cur = self.black if self.turn == 1 else self.white
        if w != cur:
            return ""                                     # 不是轮到的人：一律不理（也不让陪聊接「55」）
        r, c = mv
        if not (1 <= r <= SIZE and 1 <= c <= SIZE):
            return f"超出棋盘啦，行列都是 1~{SIZE}，比如「55」= 第5行第5列。"
        if self.board[r - 1][c - 1]:
            return f"{r}{c} 已经有子了，换个位置～"
        self.board[r - 1][c - 1] = self.turn
        self.last, self.moves = (r - 1, c - 1), self.moves + 1
        self.last_active = self.clock()
        board = render(self.board, self.last)
        if five(self.board, r - 1, c - 1):
            self._drop()
            return self._reply(f"{board}\n🎉 五子连珠！@{self.nm(w)} {self._stone(w)} 获胜！共 {self.moves} 手。", [w])
        if self.moves >= SIZE * SIZE:
            self._drop()
            return self._reply(f"{board}\n🤝 棋盘下满了，和棋！")
        self.turn = 2 if self.turn == 1 else 1
        nxt = self.black if self.turn == 1 else self.white
        self._save()
        return self._reply(f"{board}\n{self._stone(w)} {self.nm(w)} 下在 {r}{c}，轮到 @{self.nm(nxt)} {self._stone(nxt)} 下子。", [nxt])

    def _stone(self, w):
        return BLACK if w == self.black else WHITE

    def begin(self, white, white_name=""):
        """点名的两个人直接开下（「@A @B 你们两下一局」）：不等加入，当场发棋盘。"""
        self.white, self.phase = white, "play"
        self.names[white] = white_name or self.names.get(white) or "白方"
        self.last_active = self.clock()
        b, wn = self.nm(self.black), self.nm(white)
        return self._reply(f"♟️ 五子棋开局！{b} 执黑子{BLACK}，{wn} 执白子{WHITE}，轮到 @{b} 先下。" + RULES
                           + render(self.board), [self.black, white])

    def _on_wait(self, msg, w, t, is_owner):
        if _CANCEL.match(t) and (w == self.host or is_owner):
            self._drop()
            return "五子棋已取消～"
        if self.invitee and w == self.invitee and _REFUSE.match(t):
            self._drop()
            return self._reply(f"{self.nm(w)} 这次不下，五子棋取消啦～")
        if not _JOIN.match(t) or w == self.host:
            return None
        if self.invitee and w != self.invitee:
            return None                                   # 点名邀请：只等被邀请的人
        name = self.names.get(w) or self.name_of(self.room, w) or "白方"
        self.names[w] = name
        self.white, self.phase = w, "play"
        self.last_active = self.clock()
        self._save()
        b, wn = self.nm(self.black), name
        return self._reply(f"@{wn} 报名成功，游戏开始！{b} 执黑子{BLACK}，{wn} 执白子{WHITE}，轮到 @{b} 下子。\n"
                           f"直接发两位数落子：先行后列，「55」= 第5行第5列（「5 5」「5,5」也行）。\n"
                           + render(self.board), [w, self.black])

    # ---- 超时 / 强制结束 ----
    def tick(self, now: float):
        limit = WAIT_TIMEOUT if self.phase == "wait" else MOVE_TIMEOUT
        if (now or self.clock()) - self.last_active <= limit:
            return None
        self._drop()
        if self.quiet(self.room):                         # 主人静音中：悄悄结束，不说话
            return None
        if self.phase == "wait":
            return "⏰ 3 分钟没人加入，五子棋取消啦～"
        return f"⏰ 五子棋 {MOVE_TIMEOUT // 60} 分钟没人落子，自动结束（共 {self.moves} 手，不计胜负）。"

    def stop(self, by: str):
        self._drop()
        return f"🛑 五子棋已结束（共 {self.moves} 手，不计胜负）。"


class GomokuStartFeature:
    name = "games.gomoku"
    passive = False
    toggle_key = "game_gomoku"

    def __init__(self, manager, name_of=None, store_path: Path | None = None, clock=time.time, owners=None, quiet=None):
        self.m, self.name_of, self.clock = manager, name_of or (lambda r, w: ""), clock
        self.store = GomokuStore(store_path)
        self.owners = owners
        self.quiet = quiet
        self.restore()

    def _kw(self):
        return dict(store=self.store, clock=self.clock, owners=self.owners, quiet=self.quiet, name_of=self.name_of)

    def restore(self):
        """poller 重启：把没下完的棋局放回 GameManager（早已超时很久的直接丢掉）。"""
        for room, d in self.store.load().items():
            try:
                g = GomokuGame.from_state(room, d, **self._kw())
            except (KeyError, TypeError, ValueError):
                log.warning("五子棋存档 %s 损坏，丢弃", room)
                self.store.drop(room)
                continue
            limit = WAIT_TIMEOUT if g.phase == "wait" else MOVE_TIMEOUT
            if self.clock() - g.last_active > limit + 1800:
                self.store.drop(room)
                continue
            with self.m.lock:
                if not self.m.get(room):
                    self.m.games[room] = g
                    log.info("五子棋恢复 %s：%s 手", room, g.moves)

    def match(self, msg):
        if not (msg.event == "AddMsg" and msg.is_group and msg.msg_type == 1 and not msg.is_self):
            return False
        return is_start(_text(msg))

    def handle(self, msg):
        """开局。小助手只当裁判：被 @ 的人里去掉小助手自己。
        点名 2 人及以上（发起人不在其中）→ 这两人直接对下；点名 1 人 → 发起人对他（等他回「加入」）；没点名 → 谁先回「加入」谁下。"""
        from features.common import at_targets
        room, host = msg.room_id, msg.from_wxid
        tg = [w for w in dict.fromkeys(at_targets(msg)) if w and w != getattr(msg, "self_wxid", None)]
        names = _at_names(msg.content)
        nm = lambda w, i: self.name_of(room, w) or (names[i] if i < len(names) else "")
        if len(tg) >= 2:
            g = GomokuGame(room, tg[0], nm(tg[0], 0), **self._kw())
            busy = self.m.start(g)
            if busy:
                return busy
            out = g.begin(tg[1], nm(tg[1], 1))
            g._save()
            log.info("五子棋开局 %s：点名对下 %s vs %s（发起 %s）", room, tg[0], tg[1], host)
            return out
        invitee = tg[0] if tg else None
        g = GomokuGame(room, host, self.name_of(room, host),
                       invitee, nm(invitee, 0) if invitee else "", **self._kw())
        busy = self.m.start(g)
        if busy:
            return busy
        g._save()
        log.info("五子棋开局 %s：%s 等%s加入", room, host, invitee or "任何人")
        return g.intro()


def _at_names(content: str) -> list:
    """名字缓存里没有时，从正文「@张三 」里取被 @ 的昵称（跳过 @小助手）。"""
    ns = re.findall(r"@([^\s\u2005@]+)", content or "")
    return [n for n in ns if n != "小助手"]


def _at_name(content: str) -> str:
    ns = _at_names(content)
    return ns[0] if ns else ""
