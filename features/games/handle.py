"""猜成语（汉字 Wordle）。移植自 noneplugin/nonebot-plugin-handle（MIT, Copyright (c) 2022 MeetWq），
保留其判定与绘图逻辑（字/声母/韵母/声调四维着色），词库 answers.json / idioms.txt 来自原仓库。
开关 game_handle（默认开），不调模型。

  @小助手 猜成语     开局（10 次机会）
  直接发四字成语     猜（必须是词库里的成语）
  提示              出提示图（已猜出的字/拼音）
  @小助手 结束游戏   提前结束
"""
from __future__ import annotations

import json
import random
import re
from enum import Enum
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypinyin import Style, pinyin

from features.common import at_command
from features.games import Game

RES = Path(__file__).resolve().parent / "resources" / "handle"
CJK_SERIF = "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"
_HAN4 = re.compile(r"^[\u4e00-\u9fff]{4}$")

INITIALS = ["zh", "z", "y", "x", "w", "t", "sh", "s", "r", "q", "p", "n", "m", "l", "k", "j", "h", "g", "f", "d",
            "ch", "c", "b"]
FINALS = ["ün", "üe", "üan", "ü", "uo", "un", "ui", "ue", "uang", "uan", "uai", "ua", "ou", "iu", "iong", "ong", "io",
          "ing", "in", "ie", "iao", "iang", "ian", "ia", "er", "eng", "en", "ei", "ao", "ang", "an", "ai", "u", "o",
          "i", "e", "a"]


@lru_cache(1)
def idioms() -> frozenset:
    return frozenset(x.strip() for x in (RES / "idioms.txt").read_text(encoding="utf-8").splitlines() if x.strip())


@lru_cache(1)
def answers() -> list:
    return json.loads((RES / "answers.json").read_text(encoding="utf-8"))


def get_pinyin(idiom: str) -> list[tuple[str, str, str]]:
    out = []
    for p in pinyin(idiom, style=Style.TONE3, v_to_u=True):
        py = p[0]
        tone = py[-1] if py[-1].isdigit() else ""
        py = py[:-1] if tone else py
        ini = next((i for i in INITIALS if py.startswith(i)), "")
        fin = next((f for f in FINALS if py.endswith(f)), "")
        out.append((ini, fin, tone))
    return out


class S(Enum):
    CORRECT, EXIST, WRONG = 0, 1, 2


def get_states(guessed: list, answer: list) -> list[S]:
    states, incorrect = [], [a if g != a else "_" for g, a in zip(guessed, answer)]
    for g, a in zip(guessed, answer):
        if g == a:
            states.append(S.CORRECT)
        elif g in incorrect:
            states.append(S.EXIST)
            incorrect[incorrect.index(g)] = "_"
        else:
            states.append(S.WRONG)
    return states


BG, BLOCK, CORRECT, EXIST, WRONG_PY, WRONG_CH = "#ffffff", "#f7f8f9", "#1d9c9c", "#de7525", "#b4b8be", "#5d6673"


class HandleGame(Game):
    kind = "game_handle"
    title = "猜成语"
    idle_timeout = 300

    def __init__(self, room, out_dir: Path, rng=None, answer: dict | None = None):
        super().__init__(room)
        a = answer or (rng or random).choice(answers())
        self.idiom, self.explanation = a["word"], a["explanation"]
        self.pinyin = get_pinyin(self.idiom)
        self.times, self.guessed, self.guessed_py = 10, [], []
        self.out_dir = Path(out_dir)
        self.bs, self.bp, self.pad = (160, 160), (20, 20), (40, 40)
        self.f_char = ImageFont.truetype(CJK_SERIF, 60)
        self.f_py = ImageFont.truetype(str(RES / "NotoSansMono-Regular.ttf"), 30)
        self.f_tone = ImageFont.truetype(str(RES / "NotoSansMono-Regular.ttf"), 22)
        self.result = f"【成语】{self.idiom}\n【释义】{self.explanation}"

    # ---- 消息 ----
    def on_message(self, msg):
        from router import Reply
        t = msg.command_text.strip()
        if t in ("提示", "猜成语提示"):
            return Reply(self.room, "提示：" + "".join(c if c in "".join(self.guessed) else "？" for c in self.idiom),
                         image_path=str(self._save(self.draw_hint(), "hint")))
        if not _HAN4.match(t):
            return None
        if t not in idioms():
            return f"「{t}」不在成语词库里哦～"
        if t in self.guessed:
            return f"「{t}」已经猜过啦"
        self.guessed.append(t)
        self.guessed_py.append(get_pinyin(t))
        img = str(self._save(self.draw(), f"g{len(self.guessed)}"))
        if t == self.idiom:
            self.over = True
            return Reply(self.room, f"🎉 恭喜猜中！\n{self.result}", image_path=img)
        if len(self.guessed) >= self.times:
            self.over = True
            return Reply(self.room, f"😢 10 次机会用完了\n{self.result}", image_path=img)
        return Reply(self.room, f"第 {len(self.guessed)}/{self.times} 次：{t}（可发「提示」）", image_path=img)

    def on_timeout(self):
        return f"⏰ 猜成语超时结束。\n{self.result}"

    def stop(self, by):
        self.over = True
        return f"🛑 猜成语已结束。\n{self.result}"

    def intro(self):
        from router import Reply
        return Reply(self.room, f"🀄 猜成语开始！你们有 {self.times} 次机会猜一个四字成语，直接在群里发成语即可。\n"
                                "青色=位置正确，橙色=存在但位置不对；拼音/声调同理。发「提示」看提示，5 分钟无人猜自动结束。",
                     image_path=str(self._save(self.draw(), "start")))

    # ---- 绘图（移植自原插件）----
    def _save(self, im, tag) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        p = self.out_dir / f"handle-{self.room.split('@')[0]}-{tag}.jpg"
        im.convert("RGB").save(p, "JPEG", quality=90)
        return p

    def block(self, color, char="", cc="", ini="", ic="", fin="", fc="", tone="", tc="", underline=False, uc=""):
        b = Image.new("RGB", self.bs, color)
        if not char:
            return b
        d = ImageDraw.Draw(b)
        cw, ch = self.f_char.getbbox(char)[2:]
        d.text(((self.bs[0] - cw) / 2, (self.bs[1] - ch) / 5 * 3), char, font=self.f_char, fill=cc)
        space = 5
        need = bool(ini and fin)
        L = self.f_py.getlength(ini + fin) + (space if need else 0)
        x0 = x = (self.bs[0] - L) / 2
        y = self.bs[0] / 8
        d.text((x, y), ini, font=self.f_py, fill=ic)
        x += self.f_py.getlength(ini) + (space if need else 0)
        d.text((x, y), fin, font=self.f_py, fill=fc)
        tw, th = self.f_tone.getbbox(tone)[2:] if tone else (0, 0)
        d.text(((self.bs[0] + L) / 2 + tw / 3, y - th / 3), tone, font=self.f_tone, fill=tc)
        if underline:
            yy = self.bs[0] / 8 + self.f_py.getbbox(ini + fin)[3] + 2
            d.line((x0, yy, x0 + L, yy), fill=uc, width=1)
            d.line((x0, yy + 3, x0 + L, yy + 3), fill=uc, width=1)
        return b

    def _pos(self, r, c):
        return (self.pad[0] + (self.bs[0] + self.bp[0]) * c, self.pad[1] + (self.bs[1] + self.bp[1]) * r)

    def _board(self, rows):
        w = 4 * self.bs[0] + 3 * self.bp[0] + 2 * self.pad[0]
        h = rows * self.bs[1] + (rows - 1) * self.bp[1] + 2 * self.pad[1]
        return Image.new("RGB", (w, h), BG)

    def draw(self):
        rows = min(len(self.guessed) + 1, self.times)
        board = self._board(rows)
        pyc = lambda s: CORRECT if s == S.CORRECT else EXIST if s == S.EXIST else WRONG_PY
        chc = lambda s: CORRECT if s == S.CORRECT else EXIST if s == S.EXIST else WRONG_CH
        for i, (idiom, py) in enumerate(zip(self.guessed, self.guessed_py)):
            cs = get_states(list(idiom), list(self.idiom))
            ins = get_states([p[0] for p in py], [p[0] for p in self.pinyin])
            fs = get_states([p[1] for p in py], [p[1] for p in self.pinyin])
            ts = get_states([p[2] for p in py], [p[2] for p in self.pinyin])
            us = get_states([p[0] + p[1] for p in py], [p[0] + p[1] for p in self.pinyin])
            for j in range(4):
                i2, f2, t2 = py[j]
                if idiom[j] == self.idiom[j]:
                    blk = self.block(CORRECT, idiom[j], BG, i2, BG, f2, BG, t2, BG)
                else:
                    uc = pyc(us[j])
                    blk = self.block(BLOCK, idiom[j], chc(cs[j]), i2, pyc(ins[j]), f2, pyc(fs[j]), t2, pyc(ts[j]),
                                     uc in (CORRECT, EXIST), uc)
                board.paste(blk, self._pos(i, j))
        for i in range(len(self.guessed), rows):
            for j in range(4):
                board.paste(self.block(BLOCK), self._pos(i, j))
        return board

    def draw_hint(self):
        gc = set("".join(self.guessed))
        gi = {p[0] for py in self.guessed_py for p in py}
        gf = {p[1] for py in self.guessed_py for p in py}
        gt = {p[2] for py in self.guessed_py for p in py}
        board = self._board(1)
        for i in range(4):
            ch, (hi, hf, ht) = self.idiom[i], self.pinyin[i]
            color = cc = ic = fc = tc = CORRECT
            if ch not in gc:
                ch, color, cc = "?", BLOCK, WRONG_CH
            else:
                cc = ic = fc = tc = BG
            if hi not in gi:
                hi, ic = "?", WRONG_PY
            if hf not in gf:
                hf, fc = "?", WRONG_PY
            if ht not in gt:
                ht, tc = "?", WRONG_PY
            board.paste(self.block(color, ch, cc, hi, ic, hf, fc, ht, tc), self._pos(0, i))
        return board


class HandleStartFeature:
    name = "games.handle"
    passive = False
    toggle_key = "game_handle"

    def __init__(self, manager, rng=None):
        self.m, self.rng = manager, rng

    def match(self, msg):
        return at_command(msg, ("猜成语", "汉字wordle", "handle")) is not None

    def handle(self, msg):
        g = HandleGame(msg.room_id, self.m.out_dir, self.rng)
        err = self.m.start(g)
        return err or g.intro()
