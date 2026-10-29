"""「真人感」底层风格（学观察群里的机器人甲）：口语、短句、拆成 1–N 条连发。

- HUMANLIKE_STYLE：拼在人设（默认或主人自定义的群人设）后面的底层风格提示。BOT_HUMANLIKE=0 可整体关闭。
- split_messages()：把模型输出拆成 1–N 条短消息。模型按要求用换行分条；若只给了一整段，按概率在句末标点处拆。
- 发送：router 对 Reply.parts 逐条 postText，条间随机 sleep（BOT_MULTI_GAP_MIN~MAX 秒），后台线程发，不阻塞轮询。

环境变量：BOT_HUMANLIKE(1) BOT_MULTI_MAX(3) BOT_MULTI_PROB(0.7) BOT_MULTI_GAP_MIN(0.8) BOT_MULTI_GAP_MAX(2)
（2026-10-21 降延迟：第一条立刻发，条间 0.8–2 秒，最多 3 条；陪聊可流式先发第一行，见 chat_companion。）
"""
from __future__ import annotations

import os
import random
import re

HUMANLIKE_STYLE = (
    "【底层说话风格】像群里一个普通群友那样聊天：口语、自然、随意，句子短，别写成文章，不用 Markdown/列表/小标题。\n"
    "想说的话拆成 1~{max_parts} 条短消息，每条单独一行（用换行分开），每条一般不超过 30 字；简单回应一条就够，别硬凑。\n"
    "标点可以随意点，句尾不一定要句号，可以用「哈哈」「确实」「嗯」「笑死」这类口头语，但别故意写错字，要让人看得懂。\n"
    "别一上来就自我介绍，也别主动说自己是 AI、机器人或模型；不说「作为一个AI」「我无法」这类话，不说教、不端着。\n"
    "如果有人直接问你是不是机器人/AI：轻松含糊地带过或打个岔（比如「你猜」「这很重要吗哈哈」），然后接着聊原来的话题；"
    "不要较真、不要长篇解释，也不要信誓旦旦地声称自己是真人。若对方认真、反复追问，或涉及钱、隐私、安全等需要对方知情的事，就如实说你是主人的 AI 小伙伴。\n"
    "以上只是底层风格；如果上面的人设对说话方式或身份另有要求，以人设为准。"
)


def _env(name, default, cast=float):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


def enabled() -> bool:
    return os.environ.get("BOT_HUMANLIKE", "1") not in ("0", "false", "no", "")


def max_parts() -> int:
    return max(1, _env("BOT_MULTI_MAX", 3, int))


def gap_range() -> tuple[float, float]:
    lo, hi = _env("BOT_MULTI_GAP_MIN", 0.8), _env("BOT_MULTI_GAP_MAX", 2)
    return (min(lo, hi), max(lo, hi))


def style_prompt() -> str:
    return HUMANLIKE_STYLE.format(max_parts=max_parts()) if enabled() else ""


def with_style(persona: str) -> str:
    s = style_prompt()
    return f"{persona}\n\n{s}" if s else persona


_SENT = re.compile(r"(?<=[。！？!?；;…~～])")


def _clean_line(p: str) -> str:
    p = re.sub(r"^[-*•·\d]+[.、)）]?\s*", "", p) if re.match(r"^([-*•·]|\d+[.、)）])\s*", p) else p   # 去列表符号
    return p.rstrip("。")                                                     # 句号收尾太工整


def split_messages(text: str, n_max: int | None = None, prob: float | None = None, rng=None) -> list[str]:
    """→ 1..n_max 条非空短消息。关闭真人感时原样返回一条。"""
    text = (text or "").strip()
    if not text:
        return []
    if not enabled():
        return [text]
    n_max = n_max or max_parts()
    prob = _env("BOT_MULTI_PROB", 0.7) if prob is None else prob
    rng = rng or random
    parts = [p.strip() for p in text.splitlines() if p.strip()]
    if len(parts) == 1 and len(parts[0]) > 18 and rng.random() < prob:        # 一整段 → 句末拆
        parts = [p.strip() for p in _SENT.split(parts[0]) if p.strip()]
    if len(parts) > n_max:                                                     # 多了就把尾巴并进最后一条
        # 多了就把尾巴并进最后一条：用换行并（2026-10-22 修：之前直接拼接，列表项粘成「Codex：…Harness：…」）
        parts = parts[:n_max - 1] + [parts[n_max - 1:]] if n_max > 1 else [parts]
    out = []
    for p in parts:
        lines = p if isinstance(p, list) else [p]
        lines = [_clean_line(x) for x in lines]
        p = "\n".join(x for x in lines if x)
        if p:
            out.append(p)
    return out or [text]
