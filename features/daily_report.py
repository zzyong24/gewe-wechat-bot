"""功能 6：聊天总结日报（AI 版）。

数据：data/stats.db 的发言统计（messages）+ 当天消息正文（texts）。
模型：一次调用 MiniMax 输出 JSON {summary, topics, titles, quotes}（省额度，不分多次）。
提示词与报告结构参考 SXP-Simon/astrbot_plugin_qq_group_daily_analysis（MIT）：话题提取 / 用户称号 / 金句。
输出：优先长图（render/report_card.py → imagehost → gewe postImage），图失败自动降级为结构化文字。

触发：
  群内 @小助手 日报 / 群聊总结 / 今日总结 / AI日报（「昨日日报」类前缀「昨日」→ 昨天）
  定时：python3 jobs/daily_report.py [--room ID] [--date YYYY-MM-DD]（见该文件）
受开关 daily_report 控制（默认关）；只对模型白名单（=真发白名单）会话调模型。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("feature.daily_report")
TZ = ZoneInfo("Asia/Shanghai")
TRIGGERS = {"日报": 0, "群聊总结": 0, "今日总结": 0, "AI日报": 0, "ai日报": 0, "昨日总结": -1, "昨日AI日报": -1}
MAX_CHARS = 30000           # 喂给模型的聊天记录上限（字符），超出保留最近的
MIN_TEXT_MSGS = 5

SYSTEM = "你是「小助手」，一个友好、幽默的微信群助手，负责给群聊写每日总结。只输出 JSON。"
PROMPT = """请分析下面这个微信群「{room}」{day} 的聊天记录，生成群聊日报。

## 要求
1. summary：用 2~3 句话概括今天群里聊了什么、整体氛围（口吻友好轻松）。
2. topics：提取 1~{max_topics} 个主要话题。每个话题：topic（10 字以内）、contributors（主要参与者名字，最多 5 人，按参与度排序）、
   detail（讲清楚谁说了什么、前因后果和结论，1~3 句）。忽略纯灌水/表情；消息太少可返回 []。
3. titles：给最活跃的最多 {max_titles} 位群友各分配一个称号（每人一个、不重复），可选：龙王（发言最多）、技术专家、夜猫子（深夜发言）、
   表情包军火库、沉默终结者（开启话题）、评论家（发言长）、互动达人、气氛担当……可自行拓展。每项：name、title、reason（一句话）。
4. quotes：挑 0~{max_quotes} 句有趣、有梗或令人印象深刻的原话（不要低俗、不要攻击性内容）。每项：content（原文）、sender（名字）、reason（一句话）。
5. 名字一律使用记录里的群昵称，不要输出 wxid。

## 群友数据（count=消息数，avg_len=平均字数，night=0-6 点消息数，emoji=表情数，image=图片数）
{users}

## 聊天记录（格式：[HH:MM] 名字: 内容）
{messages}

## 只返回如下 JSON，不要 markdown 代码块，不要多余文字：
{{"summary": "...", "topics": [{{"topic": "...", "contributors": ["..."], "detail": "..."}}],
 "titles": [{{"name": "...", "title": "...", "reason": "..."}}],
 "quotes": [{{"content": "...", "sender": "...", "reason": "..."}}]}}"""


def day_of(offset: int = 0, base: datetime | None = None) -> str:
    return ((base or datetime.now(TZ)) + timedelta(days=offset)).strftime("%Y-%m-%d")


def format_messages(texts: list[dict], limit: int = MAX_CHARS) -> str:
    lines = [f"[{datetime.fromtimestamp(t['ts'], TZ):%H:%M}] {t['name']}: {t['content'].replace(chr(10), ' ')}"
             for t in texts]
    out, size = [], 0
    for line in reversed(lines):          # 超长时保留最近的
        size += len(line) + 1
        if size > limit:
            break
        out.append(line)
    return "\n".join(reversed(out))


def parse_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1:
        raise ValueError("模型输出里没有 JSON")
    d = json.loads(t[i:j + 1])
    return {"summary": str(d.get("summary") or ""),
            "topics": [x for x in d.get("topics") or [] if isinstance(x, dict)][:5],
            "titles": [x for x in d.get("titles") or [] if isinstance(x, dict)][:6],
            "quotes": [x for x in d.get("quotes") or [] if isinstance(x, dict)][:3]}


def report_text(rpt: dict) -> str:
    st = rpt["stats"]
    L = [f"📰【{rpt.get('room_name') or '本群'}】{rpt['day']} 群聊日报",
         f"消息 {st['total']} 条 · {st['speakers']} 人发言"
         + (f" · 最热闹 {st['peak_hour']:02d}:00" if st.get("peak_hour") is not None else "")]
    if rpt.get("summary"):
        L += ["", rpt["summary"]]
    if rpt.get("topics"):
        L += ["", "💬 热门话题"]
        for i, t in enumerate(rpt["topics"], 1):
            who = f"（{'、'.join(t.get('contributors') or [])}）" if t.get("contributors") else ""
            L.append(f"{i}. {t.get('topic', '')}{who}：{t.get('detail', '')}")
    L += ["", "🏆 活跃榜"] + [f"{i}. {m['name']} {m['count']} 条" for i, m in enumerate(st["members"][:5], 1)]
    if rpt.get("titles"):
        L += ["", "🎖 群友称号"] + [f"· {t.get('name', '')}【{t.get('title', '')}】{t.get('reason', '')}" for t in rpt["titles"]]
    if rpt.get("quotes"):
        L += ["", "✨ 今日金句"] + [f"「{q.get('content', '')}」—— {q.get('sender', '')}" for q in rpt["quotes"]]
    return "\n".join(L)


class DailyReporter:
    """与 router 解耦的日报生成器：feature 和定时任务共用。"""

    def __init__(self, stats, llm_factory, room_name_of: Callable[[str], str] | None = None,
                 out_dir: str | Path | None = None, render_image: bool = True,
                 max_topics: int = 5, max_titles: int = 5, max_quotes: int = 3):
        self.stats, self._llm, self.room_name_of = stats, llm_factory, room_name_of or (lambda r: "")
        self.out_dir = Path(out_dir) if out_dir else Path(__file__).resolve().parent.parent / "data" / "reports"
        self.render_image = render_image
        self.limits = dict(max_topics=max_topics, max_titles=max_titles, max_quotes=max_quotes)

    @property
    def llm(self):
        if isinstance(self._llm, type) or not hasattr(self._llm, "chat"):
            self._llm = self._llm()
        return self._llm

    def build(self, room_id: str, day: str) -> dict:
        """返回 rpt dict：stats + AI 字段（AI 失败时只有统计，ai_error 记录原因）。"""
        stats = self.stats.daily(room_id, day)
        rpt = {"room_id": room_id, "room_name": self.room_name_of(room_id) or "", "day": day, "stats": stats,
               "summary": "", "topics": [], "titles": [], "quotes": []}
        texts = [t for t in self.stats.texts(room_id, day) if t["msg_type"] == 1]
        if len(texts) < MIN_TEXT_MSGS:
            rpt["summary"] = "今天群里比较安静，没聊出什么话题～" if stats["total"] else ""
            return rpt
        users = "\n".join(json.dumps({k: u[k] for k in ("name", "count", "avg_len", "night", "emoji", "image")},
                                     ensure_ascii=False) for u in self.stats.user_profiles(room_id, day)[:15])
        prompt = PROMPT.format(room=rpt["room_name"] or "本群", day=day, users=users,
                               messages=format_messages(self.stats.texts(room_id, day)), **self.limits)
        try:
            raw = self.llm.chat(prompt, system=SYSTEM, max_completion_tokens=4096)
            rpt.update(parse_json(raw))
        except Exception as e:
            log.warning("日报 AI 生成失败 room=%s day=%s: %s", room_id, day, e)
            rpt["ai_error"] = str(e)[:200]
        return rpt

    def make_reply(self, room_id: str, day: str):
        from router import Reply
        rpt = self.build(room_id, day)
        text = report_text(rpt)
        img = None
        if self.render_image:
            try:
                from render.report_card import render
                img = str(render(rpt, self.out_dir / f"{room_id.split('@')[0]}-{day}.png"))
            except Exception as e:
                log.warning("日报出图失败，降级文字: %s", e)
        return Reply(room_id, text, image_path=img)


class DailyReportFeature:
    name = "daily_report"
    passive = False
    toggle_key = "daily_report"

    def __init__(self, reporter: DailyReporter, allow: Callable[[str], bool] | None = None,
                 deliver: Callable | None = None, async_mode: bool = False):
        """async_mode=True：先回一句「正在整理」，后台线程生成后用 deliver(reply) 发出（不阻塞轮询）。"""
        self.reporter, self.allow, self.deliver, self.async_mode = reporter, allow, deliver, async_mode

    def match(self, msg) -> bool:
        if not (msg.is_group and msg.msg_type == 1 and not msg.is_self and msg.self_wxid in msg.at_list):
            return False
        if msg.command_text not in TRIGGERS:
            return False
        if self.allow is not None and not self.allow(msg.room_id):
            log.info("daily_report 跳过 %s：不在模型白名单", msg.room_id)
            return False
        return True

    def handle(self, msg):
        day = day_of(TRIGGERS[msg.command_text], datetime.fromtimestamp(msg.create_time, TZ))
        if self.async_mode and self.deliver:
            def work():
                try:
                    self.deliver(self.reporter.make_reply(msg.room_id, day))
                except Exception:
                    log.exception("日报后台生成失败")
            threading.Thread(target=work, daemon=True, name="daily-report").start()
            return "📝 小助手正在整理群聊日报，稍等一下～"
        return self.reporter.make_reply(msg.room_id, day)
