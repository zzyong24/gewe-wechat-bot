"""扫雷。移植自 noneplugin/nonebot-plugin-minesweeper（MIT, Copyright (c) 2022 MeetWq），保留判定/绘图逻辑与皮肤资源。
开关 game_minesweeper（默认开），不调模型。

  @小助手 扫雷 [初级|中级|高级]    开局（初级 8x8/10 雷；中级 16x16/40；高级 16x30/99）
  挖开 A1 [B2 …]                  挖开（可多个）
  标记 C3 [D4 …]                  插旗/取消
  @小助手 结束游戏
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL.Image import Resampling

from features.common import at_command
from features.games import Game

RES = Path(__file__).resolve().parent / "resources" / "minesweeper"
LEVELS = {"初级": (8, 8, 10), "中级": (16, 16, 40), "高级": (16, 30, 99)}
_POS = re.compile(r"^([A-Za-z])(\d{1,2})$")


class St(Enum):
    PREPARE, GAMING, WIN, FAIL = range(4)


class OpenR(Enum):
    OUT, DUP, WIN, FAIL = range(4)


class MarkR(Enum):
    OUT, OPENED, WIN = range(3)


@dataclass
class Tile:
    is_mine: bool = False
    is_open: bool = False
    marked: bool = False
    boom: bool = False
    count: int = 0


@dataclass
class Skin:
    numbers: list
    icons: list
    digits: list
    faces: list
    background: Image.Image


def load_skin(row, col, name="winxp") -> Skin:
    im = Image.open(RES / "skins" / f"{name}.bmp").convert("RGBA")
    cut = im.crop
    numbers = [cut((i * 16, 0, i * 16 + 16, 16)) for i in range(9)]
    icons = [cut((i * 16, 16, i * 16 + 16, 32)) for i in range(8)]
    digits = [cut((i * 12, 33, i * 12 + 11, 54)) for i in range(11)]
    faces = [cut((i * 27, 55, i * 27 + 26, 81)) for i in range(5)]
    w, h = col, row
    bg = Image.new("RGBA", (w * 16 + 24, h * 16 + 66), "silver")
    for s, t in [((0, 82, 12, 93), (0, 0, 12, 11)), ((13, 82, 14, 93), (12, 0, 12 + w * 16, 11)),
                 ((15, 82, 27, 93), (12 + w * 16, 0, 24 + w * 16, 11)), ((0, 94, 12, 95), (0, 11, 12, 44)),
                 ((15, 94, 27, 95), (12 + w * 16, 11, 24 + w * 16, 44)), ((0, 96, 12, 107), (0, 44, 12, 55)),
                 ((13, 96, 14, 107), (12, 44, 12 + w * 16, 55)), ((15, 96, 27, 107), (12 + w * 16, 44, 24 + w * 16, 55)),
                 ((0, 108, 12, 109), (0, 55, 12, 55 + h * 16)),
                 ((15, 108, 27, 109), (12 + w * 16, 55, 24 + w * 16, 55 + h * 16)),
                 ((0, 110, 12, 121), (0, 55 + h * 16, 12, 66 + h * 16)),
                 ((13, 110, 14, 121), (12, 55 + h * 16, 12 + w * 16, 66 + h * 16)),
                 ((15, 110, 27, 121), (12 + w * 16, 55 + h * 16, 24 + w * 16, 66 + h * 16)),
                 ((28, 82, 69, 107), (16, 15, 57, 40)), ((28, 82, 69, 107), (w * 16 - 33, 15, 8 + w * 16, 40))]:
        bg.paste(im.crop(s).resize((t[2] - t[0], t[3] - t[1])), t)
    return Skin(numbers, icons, digits, faces, bg)


class MineSweeper:
    NB = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))

    def __init__(self, row, column, mine_num, skin="winxp", rng=None):
        self.row, self.column, self.mine_num = row, column, mine_num
        self.start_time, self.state = time.time(), St.PREPARE
        self.tiles = [[Tile() for _ in range(column)] for _ in range(row)]
        self.skin_name, self.scale, self.rng = skin, 4, rng or random

    def all_tiles(self):
        for r in self.tiles:
            yield from r

    def valid(self, x, y):
        return 0 <= x < self.row and 0 <= y < self.column

    def around(self, x, y):
        return sum(1 for dx, dy in self.NB if self.valid(x + dx, y + dy) and self.tiles[x + dx][y + dy].is_mine)

    def set_mines(self):        # 首次挖开之后再布雷，保证第一下不踩雷
        n = 0
        while n < self.mine_num:
            i, j = self.rng.randint(0, self.row - 1), self.rng.randint(0, self.column - 1)
            t = self.tiles[i][j]
            if t.is_mine or t.is_open:
                continue
            t.is_mine, n = True, n + 1
        for i in range(self.row):
            for j in range(self.column):
                self.tiles[i][j].count = self.around(i, j)
        self.state = St.GAMING

    def open(self, x, y):
        if not self.valid(x, y):
            return OpenR.OUT
        t = self.tiles[x][y]
        if t.is_open:
            return OpenR.DUP
        t.is_open = True
        if self.state == St.PREPARE:
            self.set_mines()
        if t.is_mine:
            self.state, t.boom = St.FAIL, True
            self.show_mines()
            return OpenR.FAIL
        if t.count == 0:
            for dx, dy in self.NB:
                self.spread(x + dx, y + dy)
        if sum(1 for t in self.all_tiles() if t.is_open) + self.mine_num >= self.row * self.column:
            self.state = St.WIN
            self.show_mines()
            return OpenR.WIN
        return None

    def mark(self, x, y):
        if not self.valid(x, y):
            return MarkR.OUT
        t = self.tiles[x][y]
        if t.is_open:
            return MarkR.OPENED
        t.marked = not t.marked
        mk = [t for t in self.all_tiles() if t.marked]
        if len(mk) == self.mine_num and all(t.is_mine for t in mk):
            self.state = St.WIN
            self.show_mines()
            return MarkR.WIN
        return None

    def show_mines(self):
        for t in self.all_tiles():
            if (t.is_mine and not t.marked) or (not t.is_mine and t.marked):
                t.is_open = True

    def spread(self, x, y):
        stack = [(x, y)]
        while stack:
            x, y = stack.pop()
            if not self.valid(x, y):
                continue
            t = self.tiles[x][y]
            if t.is_open or t.is_mine:
                continue
            t.is_open, t.marked = True, False
            if t.count == 0:
                stack.extend((x + dx, y + dy) for dx, dy in self.NB)

    # ---- 绘图 ----
    def draw(self) -> Image.Image:
        sk = load_skin(self.row, self.column, self.skin_name)
        bg = sk.background
        face = sk.faces[3 if self.state == St.WIN else 2 if self.state == St.FAIL else 0]
        bg.paste(face, (int((bg.width - face.width) / 2), 15))
        left = self.mine_num - sum(1 for t in self.all_tiles() if t.marked)
        for i, s in enumerate(f"{left:03d}"[:3]):
            d = sk.digits[10 if s == "-" else int(s)]
            bg.paste(d, (18 + i * (d.width + 2), 17))
        for i, s in enumerate(reversed(f"{int(time.time() - self.start_time):03d}"[-3:])):
            d = sk.digits[int(s)]
            bg.paste(d, (bg.width - 16 - (i + 1) * (d.width + 2), 17))
        for i in range(self.row):
            for j in range(self.column):
                t = self.tiles[i][j]
                if t.is_open:
                    img = sk.icons[5 if t.boom else 2] if t.is_mine else (sk.icons[4] if t.marked else sk.numbers[t.count])
                else:
                    img = sk.icons[3 if t.marked else 0]
                bg.paste(img, (12 + img.width * j, 55 + img.height * i))
        bg = bg.resize((bg.width * self.scale, bg.height * self.scale), Resampling.NEAREST)
        font = ImageFont.truetype(str(RES / "fonts" / "00TT.TTF"), 7 * self.scale)
        tw, th = sk.numbers[0].width * self.scale, sk.numbers[0].height * self.scale
        dx, dy = 12.5 * self.scale, 54.5 * self.scale
        d = ImageDraw.Draw(bg)
        for i in range(self.row):
            for j in range(self.column):
                t = self.tiles[i][j]
                if t.is_open or t.marked:
                    continue
                text = chr(i + 65) + str(j + 1)
                _, _, w, h = font.getbbox(text)
                d.text((dx + tw * j + (tw - w) / 2, dy + th * i + (th - h) / 2), text, font=font, fill="black")
        return bg


def parse_pos(s: str):
    m = _POS.match(s.strip())
    if not m:
        return None
    return ord(m.group(1).upper()) - 65, int(m.group(2)) - 1


class MinesweeperGame(Game):
    kind = "game_minesweeper"
    title = "扫雷"
    idle_timeout = 600

    def __init__(self, room, out_dir, level="初级", rng=None):
        super().__init__(room)
        self.level = level if level in LEVELS else "初级"
        self.ms = MineSweeper(*LEVELS[self.level], rng=rng)
        self.out_dir, self.n = Path(out_dir), 0

    def _img(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.n += 1
        p = self.out_dir / f"mine-{self.room.split('@')[0]}-{self.n}.png"
        self.ms.draw().save(p, "PNG")
        return str(p)

    def intro(self):
        from router import Reply
        r, c, n = LEVELS[self.level]
        return Reply(self.room, f"💣 扫雷开始（{self.level} {r}x{c}，{n} 个雷）！\n发「挖开 A1」挖开、「标记 B2」插旗，可一次写多个位置。"
                                "10 分钟无操作自动结束。", image_path=self._img())

    def on_message(self, msg):
        from router import Reply
        t = msg.command_text.strip()
        m = re.match(r"^(挖开|挖|open|标记|mark|插旗)\s+(.+)$", t, re.I)
        if not m:
            return None
        op = "open" if m.group(1).lower() in ("挖开", "挖", "open") else "mark"
        notes, end = [], None
        for p in m.group(2).split():
            pos = parse_pos(p)
            if not pos:
                notes.append(f"「{p}」不是合法位置")
                continue
            r = self.ms.open(*pos) if op == "open" else self.ms.mark(*pos)
            if r in (OpenR.OUT, MarkR.OUT):
                notes.append(f"{p} 超出范围")
            elif r == OpenR.DUP:
                notes.append(f"{p} 已经挖开了")
            elif r == MarkR.OPENED:
                notes.append(f"{p} 已挖开，不能标记")
            elif r in (OpenR.WIN, MarkR.WIN):
                end = "🎉 恭喜，扫雷成功！"
                break
            elif r == OpenR.FAIL:
                end = f"💥 {p} 是雷！游戏结束～"
                break
        if end:
            self.over = True
        return Reply(self.room, end or ("；".join(notes) or "继续加油～"), image_path=self._img())

    def on_timeout(self):
        return "⏰ 扫雷 10 分钟没人操作，自动结束啦～"


class MinesweeperStartFeature:
    name = "games.minesweeper"
    passive = False
    toggle_key = "game_minesweeper"

    def __init__(self, manager, rng=None):
        self.m, self.rng = manager, rng

    def match(self, msg):
        return at_command(msg, ("扫雷",)) is not None

    def handle(self, msg):
        g = MinesweeperGame(msg.room_id, self.m.out_dir, at_command(msg, ("扫雷",)) or "初级", self.rng)
        return self.m.start(g) or g.intro()
