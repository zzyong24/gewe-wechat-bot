#!/usr/bin/env python3
"""把 data/inbox/raw-*.jsonl 里的历史群消息正文补进 stats.db 的 texts 表（AI 日报用）。幂等，可重复跑。
  .venv/bin/python jobs/backfill_texts.py
"""
import glob, json, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import DATA_DIR                                   # noqa: E402
from features.speech_stats import SpeechStats, TZ, TYPE_PLACEHOLDER   # noqa: E402
from models import normalize                               # noqa: E402

st = SpeechStats(DATA_DIR / "stats.db")
names = dict(((r, s), n) for r, s, n in st.db.execute("SELECT room_id, sender, MAX(sender_name) FROM messages GROUP BY 1, 2"))
n = 0
for f in sorted(glob.glob(str(DATA_DIR / "inbox" / "raw-*.jsonl"))):
    for line in open(f, encoding="utf-8"):
        try:
            m = normalize(json.loads(line).get("payload"))
        except ValueError:
            continue
        if not m or m.event != "AddMsg" or not m.is_group or m.msg_type in (10000, 10002):
            continue
        body = m.content[:500] if m.msg_type == 1 else TYPE_PLACEHOLDER.get(m.msg_type, "[其它消息]")
        cur = st.db.execute("INSERT OR IGNORE INTO texts VALUES(?,?,?,?,?,?,?,?,?)",
                            (m.app_id, m.msg_id or f"{m.create_time}-{m.from_wxid}", m.room_id, m.from_wxid,
                             names.get((m.room_id, m.from_wxid), ""), m.msg_type,
                             datetime.fromtimestamp(m.create_time, TZ).strftime("%Y-%m-%d"), m.create_time, body))
        n += cur.rowcount
st.db.commit()
print(f"补入 {n} 条")
