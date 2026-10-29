"""日报长图渲染（Pillow）。字体：Noto Sans CJK（box 自带）。emoji 字体不保证有，图里不用 emoji。"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path("/usr/share/fonts/opentype/noto")
REG, BOLD = FONT_DIR / "NotoSansCJK-Regular.ttc", FONT_DIR / "NotoSansCJK-Bold.ttc"
W, PAD = 900, 44
C = {"bg": "#f4f6fb", "card": "#ffffff", "title": "#1f2a44", "text": "#33415c", "muted": "#7a869a",
     "accent": "#4f7cff", "accent2": "#ff8a4c", "bar": "#dfe6ff", "quote": "#f6f1ff"}


def _font(size, bold=False):
    return ImageFont.truetype(str(BOLD if bold else REG), size)


def _wrap(draw, text, font, width):
    lines, cur = [], ""
    for ch in str(text):
        if ch == "\n":
            lines.append(cur); cur = ""; continue
        if draw.textlength(cur + ch, font=font) > width:
            lines.append(cur); cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines or [""]


class _Canvas:
    """两遍渲染：先量高度，再画。"""
    def __init__(self, draw: ImageDraw.ImageDraw | None):
        self.d = draw or ImageDraw.Draw(Image.new("RGB", (W, 10)))
        self.paint = draw is not None
        self.y = PAD

    def text(self, s, size=24, bold=False, color="text", x=PAD, width=W - 2 * PAD, gap=8):
        f = _font(size, bold)
        for line in _wrap(self.d, s, f, width - (x - PAD)):
            if self.paint:
                self.d.text((x, self.y), line, font=f, fill=C.get(color, color))
            self.y += int(size * 1.45)
        self.y += gap

    def section(self, title):
        self.y += 14
        if self.paint:
            self.d.rounded_rectangle((PAD, self.y + 6, PAD + 8, self.y + 34), 4, fill=C["accent"])
        self.text(title, 30, True, "title", x=PAD + 20, gap=10)

    def bar(self, label, value, vmax, right=""):
        f = _font(22)
        bw = W - 2 * PAD - 260
        if self.paint:
            self.d.text((PAD, self.y), label, font=f, fill=C["text"])
            self.d.rounded_rectangle((PAD + 170, self.y + 6, PAD + 170 + bw, self.y + 28), 10, fill=C["bar"])
            if vmax and value:
                self.d.rounded_rectangle((PAD + 170, self.y + 6, PAD + 170 + max(20, int(bw * value / vmax)),
                                          self.y + 28), 10, fill=C["accent"])
            self.d.text((PAD + 180 + bw, self.y), right or str(value), font=f, fill=C["muted"])
        self.y += 40

    def box_start(self, color="quote"):
        self._box = (self.y, color); self.y += 14

    def box_end(self):
        y0, color = self._box; self.y += 6
        if self.paint:   # 背景后画会盖住文字，所以 paint 时由调用方预先量好；这里简单画边框条
            self.d.rounded_rectangle((PAD - 12, y0, PAD - 6, self.y), 3, fill=C["accent2"])
        self.y += 10


def _draw(cv: _Canvas, rpt: dict):
    st = rpt["stats"]
    cv.text(rpt.get("room_name") or "本群", 40, True, "title", gap=2)
    cv.text(f"{rpt['day']} 群聊日报 · 小助手整理", 24, False, "muted", gap=6)
    cv.text(f"消息 {st['total']} 条    发言 {st['speakers']} 人    最热闹 "
            + (f"{st['peak_hour']:02d}:00" if st.get("peak_hour") is not None else "-"), 26, True, "accent")
    if rpt.get("summary"):
        cv.section("今日概览")
        cv.text(rpt["summary"], 24)
    if rpt.get("topics"):
        cv.section("热门话题")
        for i, t in enumerate(rpt["topics"], 1):
            cv.text(f"{i}. {t.get('topic', '')}", 26, True, "title", gap=2)
            if t.get("contributors"):
                cv.text("参与：" + "、".join(t["contributors"]), 20, False, "muted", gap=2)
            cv.text(t.get("detail", ""), 22, gap=12)
    cv.section("活跃榜")
    top = st["members"][:8]
    vmax = top[0]["count"] if top else 0
    for m in top:
        cv.bar(m["name"][:7], m["count"], vmax, f"{m['count']} 条")
    cv.section("时段分布")
    smax = max(st["slots"].values() or [0])
    for k, v in st["slots"].items():
        cv.bar(k, v, smax)
    if rpt.get("titles"):
        cv.section("群友称号")
        for t in rpt["titles"]:
            cv.text(f"{t.get('name', '')} · {t.get('title', '')}", 24, True, "accent2", gap=0)
            cv.text(t.get("reason", ""), 21, False, "muted", gap=10)
    if rpt.get("quotes"):
        cv.section("今日金句")
        for q in rpt["quotes"]:
            cv.box_start()
            cv.text(f"“{q.get('content', '')}”", 24, True, "title", x=PAD + 6, gap=2)
            cv.text(f"—— {q.get('sender', '')}    {q.get('reason', '')}", 20, False, "muted", x=PAD + 6, gap=2)
            cv.box_end()
    cv.y += 10
    cv.text("由 小助手 生成 · 仅供娱乐", 18, False, "muted", gap=0)


def render(rpt: dict, out: str | Path) -> Path:
    probe = _Canvas(None)
    _draw(probe, rpt)
    h = probe.y + PAD
    im = Image.new("RGB", (W, h), C["bg"])
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((16, 16, W - 16, h - 16), 24, fill=C["card"])
    _draw(_Canvas(d), rpt)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "PNG", optimize=True)
    return out
