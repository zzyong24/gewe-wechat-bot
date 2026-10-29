#!/usr/bin/env python3
"""定时日报入口（给 routine / cron 调用）。

  cd /opt/gewe-wechat-bot && .venv/bin/python jobs/daily_report.py                    # 今天，所有「已开启日报 且 在真发白名单」的群
  .venv/bin/python jobs/daily_report.py --room 20000000002@chatroom --date 2026-10-20  # 指定群/日期
  .venv/bin/python jobs/daily_report.py --yesterday                                   # 过了零点跑昨天
  .venv/bin/python jobs/daily_report.py --dry-run                                     # 只生成不发（图存 data/reports/）

发送仍走 router：需要环境里 BOT_SEND_ENABLED=1 且群在 BOT_SEND_ALLOW 内才真发，否则只进 outbox（dry-run）。
退出码：0 成功（含无可发群）；1 有群生成/发送失败。输出 JSON 摘要（不含任何 key）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import build_router                     # noqa: E402
from features.daily_report import day_of         # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", action="append", help="群 id，可多次；缺省=开启日报且在白名单的群")
    ap.add_argument("--date", help="YYYY-MM-DD，缺省今天（Asia/Shanghai）")
    ap.add_argument("--yesterday", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="强制不发送")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    send = os.environ.get("BOT_SEND_ENABLED") == "1" and not a.dry_run
    router = build_router(send_enabled=send)
    day = a.date or day_of(-1 if a.yesterday else 0)
    rooms = a.room or sorted(r for r in router.allow_targets
                             if r.endswith("@chatroom") and router.settings.enabled(r, "daily_report"))
    result, failed = [], False
    for room in rooms:
        if room not in router.allow_targets:     # 只对模型白名单调模型
            result.append({"room": room, "skipped": "不在白名单"}); continue
        if not router.settings.enabled(room, "daily_report"):
            result.append({"room": room, "skipped": "日报开关关闭"}); continue
        try:
            reply = router.reporter.make_reply(room, day)
            sent = router.deliver(reply)
            result.append({"room": room, "day": day, "sent": sent, "image": reply.image_path,
                           "chars": len(reply.content)})
        except Exception as e:
            failed = True
            result.append({"room": room, "error": str(e)[:200]})
    print(json.dumps({"day": day, "send_enabled": send, "results": result}, ensure_ascii=False, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
