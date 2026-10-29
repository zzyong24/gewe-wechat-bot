"""功能：发言统计 / 日报（非 AI 版）。
- passive 统计：每条群消息都计数（SQLite，data/stats.db）。
- 触发词（群内）：「今日发言」「发言统计」→ 回复当日日报；「昨日日报」→ 昨天。
日期按 Asia/Shanghai 划分。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "stats.db"
TEXT_KEEP_DAYS = 7
TYPE_PLACEHOLDER = {3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[表情]", 48: "[位置]", 49: "[链接/文件]", 42: "[名片]"}
SLOTS = [("凌晨", 0, 6), ("上午", 6, 12), ("下午", 12, 18), ("晚上", 18, 24)]


class SpeechStats:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        import threading
        self.lock = threading.RLock()                 # 按群并发后多线程共用一个连接：写入串行化
        self.db.execute("""CREATE TABLE IF NOT EXISTS messages(
            app_id TEXT, msg_id TEXT, room_id TEXT, sender TEXT, sender_name TEXT,
            msg_type INTEGER, day TEXT, hour INTEGER, ts INTEGER, length INTEGER,
            PRIMARY KEY(app_id, msg_id))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_room_day ON messages(room_id, day)")
        # 消息正文（供 AI 日报总结），只保留 TEXT_KEEP_DAYS 天
        self.db.execute("""CREATE TABLE IF NOT EXISTS texts(
            app_id TEXT, msg_id TEXT, room_id TEXT, sender TEXT, sender_name TEXT,
            msg_type INTEGER, day TEXT, ts INTEGER, content TEXT, PRIMARY KEY(app_id, msg_id))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_texts_room_day ON texts(room_id, day)")
        self.db.commit()

    def record(self, msg, sender_name: str = "") -> bool:
        """记录一条群消息；重复 msg_id 忽略。返回是否新写入。"""
        if not msg.is_group:
            return False
        with self.lock:
            return self._record(msg, sender_name)

    def _record(self, msg, sender_name: str = "") -> bool:
        dt = datetime.fromtimestamp(msg.create_time, TZ)
        cur = self.db.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (msg.app_id, msg.msg_id or f"{msg.create_time}-{msg.from_wxid}", msg.room_id,
                               msg.from_wxid, sender_name, msg.msg_type, dt.strftime("%Y-%m-%d"), dt.hour,
                               msg.create_time, len(msg.content or "")))
        if cur.rowcount == 1:
            body = (msg.content or "")[:500] if msg.msg_type == 1 else TYPE_PLACEHOLDER.get(msg.msg_type, "[其它消息]")
            self.db.execute("INSERT OR IGNORE INTO texts VALUES(?,?,?,?,?,?,?,?,?)",
                            (msg.app_id, msg.msg_id or f"{msg.create_time}-{msg.from_wxid}", msg.room_id,
                             msg.from_wxid, sender_name, msg.msg_type, dt.strftime("%Y-%m-%d"), msg.create_time, body))
            if dt.minute == 0:   # 低频清理
                cutoff = (dt - timedelta(days=TEXT_KEEP_DAYS)).strftime("%Y-%m-%d")
                self.db.execute("DELETE FROM texts WHERE day < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount == 1

    def texts(self, room_id: str, day: str) -> list[dict]:
        rows = self.db.execute("""SELECT ts, sender, sender_name, msg_type, content FROM texts
                                  WHERE room_id=? AND day=? ORDER BY ts, rowid""", (room_id, day)).fetchall()
        return [{"ts": t, "sender": s, "name": n or s, "msg_type": mt, "content": c} for t, s, n, mt, c in rows]

    def user_profiles(self, room_id: str, day: str) -> list[dict]:
        """每人：消息数、平均长度、深夜(0-6点)条数、表情/图片条数——给 AI 分配称号用。"""
        rows = self.db.execute("""SELECT sender, MAX(sender_name), COUNT(*), AVG(length),
                   SUM(hour < 6), SUM(msg_type = 47), SUM(msg_type = 3)
                   FROM messages WHERE room_id=? AND day=? GROUP BY sender ORDER BY COUNT(*) DESC""",
                               (room_id, day)).fetchall()
        return [{"wxid": w, "name": n or w, "count": c, "avg_len": round(a or 0, 1), "night": nt or 0,
                 "emoji": e or 0, "image": im or 0} for w, n, c, a, nt, e, im in rows]

    def daily(self, room_id: str, day: str) -> dict:
        q = self.db.execute
        total = q("SELECT COUNT(*) FROM messages WHERE room_id=? AND day=?", (room_id, day)).fetchone()[0]
        rows = q("""SELECT sender, MAX(sender_name), COUNT(*) c FROM messages WHERE room_id=? AND day=?
                    GROUP BY sender ORDER BY c DESC, sender""", (room_id, day)).fetchall()
        hours = dict(q("SELECT hour, COUNT(*) FROM messages WHERE room_id=? AND day=? GROUP BY hour",
                       (room_id, day)).fetchall())
        slots = {n: sum(hours.get(h, 0) for h in range(a, b)) for n, a, b in SLOTS}
        peak = max(hours.items(), key=lambda kv: kv[1])[0] if hours else None
        return {"room_id": room_id, "day": day, "total": total, "speakers": len(rows),
                "members": [{"wxid": w, "name": n or w, "count": c} for w, n, c in rows],
                "hours": hours, "slots": slots, "peak_hour": peak}

    def report_text(self, room_id: str, day: str, room_name: str = "", top: int = 5,
                    name_of=None) -> str:
        d = self.daily(room_id, day)
        title = f"📊【{room_name or '本群'}】{day} 发言日报"
        if d["total"] == 0:
            return f"{title}\n今天还没有人说话～"
        medals = ["🥇", "🥈", "🥉"]
        lines = [title, f"总消息 {d['total']} 条，{d['speakers']} 人发言", "", f"🏆 活跃榜 Top{top}"]
        for i, m in enumerate(d["members"][:top]):
            name = (name_of(m["wxid"]) if name_of else None) or m["name"]
            pct = m["count"] * 100 / d["total"]
            lines.append(f"{medals[i] if i < 3 else f'{i+1}.'} {name}  {m['count']} 条（{pct:.0f}%）")
        lines += ["", "⏰ 时段分布"]
        for n, a, b in SLOTS:
            c = d["slots"][n]
            lines.append(f"{n}({a:02d}-{b:02d}) {'▇' * min(c, 20)} {c}")
        if d["peak_hour"] is not None:
            lines.append(f"最热闹：{d['peak_hour']:02d}:00-{d['peak_hour']:02d}:59")
        return "\n".join(lines)


def today(offset_days: int = 0) -> str:
    return (datetime.now(TZ) + timedelta(days=offset_days)).strftime("%Y-%m-%d")


class SpeechStatsRecorder:
    """被动功能：所有群消息计数（不回复）。应放在 router 最前面。"""
    name = "speech_stats.recorder"
    passive = True
    toggle_key = "speech_stats"

    def __init__(self, stats: SpeechStats, name_resolver=None):
        self.stats, self.name_resolver = stats, name_resolver

    def match(self, msg) -> bool:
        return msg.event == "AddMsg" and msg.is_group and msg.msg_type not in (10000, 10002)

    def handle(self, msg):
        name = self.name_resolver(msg.room_id, msg.from_wxid) if self.name_resolver else ""
        self.stats.record(msg, name or "")
        return None


class SpeechReportFeature:
    name = "speech_stats.report"
    passive = False
    toggle_key = "speech_stats"
    TRIGGERS = {"今日发言": 0, "发言统计": 0, "今日日报": 0, "昨日日报": -1}

    def __init__(self, stats: SpeechStats, room_name_of=None):
        self.stats, self.room_name_of = stats, room_name_of

    def match(self, msg) -> bool:
        return msg.is_group and msg.msg_type == 1 and msg.command_text in self.TRIGGERS

    def handle(self, msg):
        off = self.TRIGGERS[msg.command_text]
        day = (datetime.fromtimestamp(msg.create_time, TZ) + timedelta(days=off)).strftime("%Y-%m-%d")
        name = self.room_name_of(msg.room_id) if self.room_name_of else ""
        return self.stats.report_text(msg.room_id, day, name or "")
