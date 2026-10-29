"""掉线 / 断流提醒：只通知主人，不自动登录、不自动重连。

- 每 BOT_ONLINE_CHECK_MIN（默认 5）分钟调一次 gewe checkOnline（只读状态查询）。
  连续 2 次「离线」→ 告警；连续 3 次查询本身失败（网络/接口错误）→ 也告警（状态未知）。
- 回调断流：BOT_STALL_MIN（默认 90）分钟没收到任何回调，且在 09:00–23:00（北京时间）之间 → 告警（夜里群安静不算）。
- 告警方式：日志 ERROR 标红（ANSI 红色 + 【掉线告警】），并按 运维 规则往 data/alerts/ 新增一个 md
  （类型 todo，对象 我（主人），P1）。同一事件只投一次；恢复后再投一条「更新」写明原文件名。
- 绝不调用任何登录 / 重连接口（login、reconnection 都不调）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from features.common import TZ

log = logging.getLogger("online_watch")
RED, RESET = "\x1b[31;1m", "\x1b[0m"
INBOX = Path(os.environ.get("BOT_TODO_INBOX", str(Path(__file__).resolve().parent.parent / "data" / "alerts")))


class OnlineWatch:
    def __init__(self, check=None, inbox: Path | None = None, clock=time.time, interval_min: float | None = None,
                 stall_min: float | None = None):
        if check is None:
            import gewe_client
            check = gewe_client.check_online
        self.check, self.clock = check, clock
        self.inbox = Path(inbox) if inbox else INBOX
        self.interval = 60 * (interval_min if interval_min is not None else float(os.environ.get("BOT_ONLINE_CHECK_MIN", "5")))
        self.stall = 60 * (stall_min if stall_min is not None else float(os.environ.get("BOT_STALL_MIN", "90")))
        self.offline_n = 0
        self.err_n = 0
        self.last_cb = clock()
        self.alerts: dict[str, str] = {}          # 事件类型 → 已投递文件名（未恢复）
        self._stop = threading.Event()

    def saw_callback(self):
        self.last_cb = self.clock()
        if "stall" in self.alerts:
            self._recover("stall", "回调已恢复")

    def tick(self):
        """一次检查（线程每 interval 调一次；测试直接调）。"""
        try:
            ok = bool(self.check())
            self.err_n = 0
            if ok:
                self.offline_n = 0
                for k in ("offline", "unknown"):
                    if k in self.alerts:
                        self._recover(k, "checkOnline 已恢复在线")
            else:
                self.offline_n += 1
                log.warning("checkOnline：离线（连续 %d 次）", self.offline_n)
                if self.offline_n >= 2:
                    self._alert("offline", f"小助手微信掉线：checkOnline 连续 {self.offline_n} 次返回离线",
                                "需要主人本人重新登录（机器人不会自动登录或重连）。")
        except Exception as e:
            self.err_n += 1
            log.warning("checkOnline 查询失败（连续 %d 次）: %s", self.err_n, type(e).__name__)
            if self.err_n >= 3:
                self._alert("unknown", f"小助手在线状态查不到：checkOnline 连续 {self.err_n} 次失败（{type(e).__name__}）",
                            "可能是 gewe 服务或网络问题，请主人看一下 gewe 后台。")
        now = self.clock()
        hour = datetime.fromtimestamp(now, TZ).hour
        if now - self.last_cb >= self.stall and 9 <= hour < 23:
            mins = int((now - self.last_cb) / 60)
            self._alert("stall", f"小助手回调断流：已 {mins} 分钟没收到任何微信消息回调",
                        "可能掉线或 relay 回调异常，请主人确认账号是否在线。")

    def _write(self, title: str, typ: str, note: str) -> str:
        ts = datetime.fromtimestamp(self.clock(), TZ)
        upd = typ == "更新"
        name = f"{ts:%Y%m%d-%H%M}-gewe-wechat-bot-{'更新-恢复' if upd else '掉线提醒'}.md"
        body = (f"# 投递：{title}\n\n- 标题：{title}\n- 类型：{'更新' if upd else 'todo'}\n- 对象：我（主人）\n"
                f"- 截止：{ts:%Y-%m-%d}\n- 来源：gewe-wechat-bot poller 自动监控\n- 优先级：P1\n- 备注：{note}\n")
        try:
            self.inbox.mkdir(parents=True, exist_ok=True)
            p = self.inbox / name
            i = 2
            while p.exists():
                p = self.inbox / name.replace(".md", f"-{i}.md"); i += 1
            p.write_text(body, encoding="utf-8")
            return p.name
        except OSError as e:
            log.error("%s写待办 inbox 失败：%s%s", RED, e, RESET)
            return ""

    def _alert(self, kind: str, title: str, todo: str):
        if kind in self.alerts:
            return
        fn = self._write(title, "todo", f"{todo} 检测时间 {datetime.fromtimestamp(self.clock(), TZ):%H:%M}（北京时间）。")
        self.alerts[kind] = fn or "(未写入)"
        log.error("%s【掉线告警】%s → 已投主人待办 %s%s", RED, title, fn or "（失败）", RESET)

    def _recover(self, kind: str, what: str):
        fn = self.alerts.pop(kind, "")
        new = self._write(f"小助手{what}", "更新", f"对应原条目 {fn}：{what}，可标完成。")
        log.warning("【恢复】%s（原告警 %s，已投更新 %s）", what, fn, new)

    def start(self):
        def run():
            while not self._stop.wait(self.interval):
                try:
                    self.tick()
                except Exception:
                    log.exception("在线监控异常")
        threading.Thread(target=run, daemon=True, name="online-watch").start()

    def stop(self):
        self._stop.set()
