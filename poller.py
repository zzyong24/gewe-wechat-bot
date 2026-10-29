#!/usr/bin/env python3
"""box 端轮询器：从服务器薄回调中转端 /pull 拉取 gewe 回调 → models.normalize → router.dispatch。

- 配置：.relay_config.json（server_url / secret / last_pulled_id），处理完一批即持久化 last_pulled_id。
- 默认 dry-run：BOT_SEND_ENABLED=1 才真发（且仍受 router 白名单限制）。
- secret / GEWE_TOKEN 绝不出现在日志里。
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent


def _load_bot_env(path: Path) -> None:
    """bot.env：非密钥配置（KEY=VALUE），已在环境变量里的不覆盖。"""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_load_bot_env(BASE / "bot.env")
sys.path.insert(0, str(BASE))

from app import DATA_DIR, build_router   # noqa: E402
from models import normalize               # noqa: E402

CONFIG = Path(os.environ.get("RELAY_CONFIG", BASE / ".relay_config.json"))
INTERVAL = float(os.environ.get("POLL_INTERVAL", "0.3"))   # 两次拉取之间的间隔（长轮询时只是兜底的小间隔）
WAIT = float(os.environ.get("POLL_WAIT", "25"))            # 长轮询：/pull 无新消息时中转端挂住最多 WAIT 秒（0=普通轮询）
ERROR_BACKOFF = 3.0
CONNECT_TIMEOUT = float(os.environ.get("BOT_CONNECT_TIMEOUT", "4"))   # 跨境间歇丢包：建连超时短一点，快速重试
BACKOFF_MAX = float(os.environ.get("BOT_PULL_BACKOFF_MAX", "10"))
MAX_MSG_AGE = float(os.environ.get("BOT_MAX_MSG_AGE", "300"))   # 超过这么久的旧消息（如掉线后积压）只计数、不回复
_session = requests.Session()                              # keep-alive，省每次 TCP/握手
LIMIT = 200
FIRST_LIMIT = int(os.environ.get("BOT_FIRST_PULL_LIMIT", "20"))   # 重启后第一拉只要一小批，别一次灌大积压
READ_TIMEOUT_MIN = 35.0
RAW_DIR = DATA_DIR / "inbox"


class _Redact(logging.Filter):
    """把 secret / token 从任何日志行里抹掉。"""
    def __init__(self, secrets):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record):
        msg = record.getMessage()
        for s in self.secrets:
            msg = msg.replace(s, "***")
        msg = re.sub(r"secret=[^&\s]+", "secret=***", msg)
        record.msg, record.args = msg, ()
        return True


def load_cfg() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def save_last_id(last_id: int) -> None:
    cfg = load_cfg()
    cfg["last_pulled_id"] = int(last_id)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)


def reset_session() -> None:
    global _session
    try:
        _session.close()
    except Exception:
        pass
    _session = requests.Session()


def pull(cfg: dict, after: int, limit: int | None = None) -> list[dict]:
    try:
        params = {"secret": cfg["secret"], "after": after, "limit": limit or LIMIT}
        if WAIT > 0:
            params["wait"] = WAIT
        r = _session.get(cfg["server_url"].rstrip("/") + "/pull", params=params,
                         timeout=(CONNECT_TIMEOUT, max(READ_TIMEOUT_MIN, WAIT + 15)))
    except requests.RequestException as e:   # 异常信息里可能带 URL(含 secret)，只记类型
        raise RuntimeError(f"pull network error: {type(e).__name__}") from None
    if r.status_code != 200:
        raise RuntimeError(f"pull http {r.status_code}")
    return r.json()


PULL_DEADLINE = float(os.environ.get("BOT_PULL_DEADLINE", "0")) or (CONNECT_TIMEOUT + max(READ_TIMEOUT_MIN, WAIT + 15) + 15)
JUMP_SEC = 20.0      # 墙钟比单调钟多走这么多 → 机器被挂起/快照恢复过，旧连接多半已死


class PullStuck(RuntimeError):
    pass


def pull_guarded(cfg: dict, after: int, limit: int | None = None, deadline: float | None = None,
                 _pull=None, log=None) -> list[dict]:
    """给 /pull 加总时限看门狗（requests 的 read timeout 只管「两次收到数据的间隔」，不管总时长；
    虚拟机挂起/快照恢复后旧 TCP 连接可能半死不活）。在独立线程里拉，超时或检测到挂起就放弃这次请求、重建连接池。
    放弃的线程是 daemon，卡着也不影响主循环。"""
    deadline = deadline or PULL_DEADLINE
    box = {}

    def run():
        try:
            box["rows"] = (_pull or pull)(cfg, after, limit)
        except BaseException as e:      # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=run, daemon=True, name="pull")
    w0, m0 = time.time(), time.monotonic()
    t.start()
    while t.is_alive():
        t.join(1.0)
        if not t.is_alive():
            break
        jump = (time.time() - w0) - (time.monotonic() - m0)
        if jump > JUMP_SEC:
            reset_session()
            raise PullStuck(f"pull 期间机器挂起约 {jump:.0f}s（虚拟机快照/暂停），放弃旧连接")
        if time.monotonic() - m0 > deadline:
            reset_session()
            raise PullStuck(f"pull 超过总时限 {deadline:.0f}s 未返回，放弃并重建连接")
    if "err" in box:
        raise box["err"]
    return box.get("rows") or []


def handle_row(router, row: dict, log, queues=None, debouncer=None) -> None:
    """解析 + 存档在拉取线程里做（快）；路由（可能调模型/工具，慢）交给该会话的队列线程（同群保序、不同群并行）。"""
    rid = row.get("id")
    raw = row.get("raw_json") or ""
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        log.warning("id=%s 非 JSON 回调，跳过 (len=%d)", rid, len(raw))
        return
    # 原始报文存档（研究真实格式用）。gewe 验证包带 token 字段，必须先抹掉
    try:
        if isinstance(payload, dict):
            for k in list(payload):
                if k.lower() in ("token", "secret", "x-gewe-token"):
                    payload[k] = "***"
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        with open(RAW_DIR / time.strftime("raw-%Y%m%d.jsonl"), "a", encoding="utf-8") as f:
            line = json.dumps({"id": rid, "recv_time": row.get("recv_time"), "payload": payload},
                              ensure_ascii=False)
            tok = os.environ.get("GEWE_TOKEN")
            if tok:
                line = line.replace(tok, "***")
            f.write(line + "\n")
    except Exception:
        log.exception("id=%s 存档失败", rid)

    if isinstance(payload, dict) and "testMsg" in payload:
        log.info("id=%s gewe 回调地址验证包(testMsg)，跳过", rid)
        return
    msg = normalize(payload)
    if msg is None:
        keys = list(payload.keys())[:8] if isinstance(payload, dict) else type(payload).__name__
        log.info("id=%s 无法识别的回调，跳过 keys=%s", rid, keys)
        return
    if msg.event not in ("AddMsg", "ModContacts"):   # ModContacts：群成员变更 → 退群比对
        log.info("id=%s 事件 %s (非新消息)，跳过路由", rid, msg.event)
        return
    room_name = ""
    if msg.room_id:
        try:
            room_name = router.cache.room_name(msg.room_id)
        except Exception:
            pass
    log.info("id=%s 收到 %s | 群=%s(%s) | 发送人=%s | 类型=%s | @我=%s | self=%s | 内容=%r",
             rid, "群消息" if msg.is_group else "私聊", msg.room_id or "-", room_name or "?",
             msg.from_wxid, msg.type_name, msg.mentions_me(), msg.is_self, msg.content[:200])
    t_recv = time.time()
    stale = bool(msg.create_time) and MAX_MSG_AGE > 0 and t_recv - msg.create_time > MAX_MSG_AGE
    if stale:
        log.info("id=%s 旧消息（%.0fs 前），只计数不回复", rid, t_recv - msg.create_time)
    lag = f"回调入库→拉到 {t_recv - float(row['recv_time']):.2f}s | " if row.get("recv_time") else ""
    msg._rid, msg._lag = rid, lag
    if debouncer is not None and not stale and debouncer.offer(msg):
        return                                    # 防抖：等这人 2.5s 内的后续消息一起处理（features/debounce.py）
    submit(router, msg, log, queues, stale)


def submit(router, msg, log, queues=None, stale: bool = False) -> None:
    rid, lag = getattr(msg, "_rid", "?"), getattr(msg, "_lag", "")

    def work(queued: float = 0.0):
        t0 = time.time()
        trace = router.dispatch(msg, passive_only=stale)
        merged = getattr(msg, "merged_ids", None)
        log.info("id=%s 路由轨迹 %s排队 %.2fs | 耗时 %.2fs%s %s", rid, lag, queued, time.time() - t0,
                 f" | 合并 {len(merged) + 1} 条" if merged else "", json.dumps(trace, ensure_ascii=False)[:500])

    if queues is None:
        work()
    else:
        n = queues.submit(msg.room_id or msg.from_wxid, work)
        if n > 1:
            log.info("id=%s 进入会话队列 %s（前面还有 %d 条）", rid, msg.room_id or msg.from_wxid, n - 1)


REPLAY_FILE = DATA_DIR / "replay_once.json"


def replay_once(router, log, queues, path: Path = REPLAY_FILE, raw_dir: Path | None = None) -> int:
    """一次性补处理：data/replay_once.json = {"ids": [771], "followup": true}（运维手工写，读完即删）。
    从 raw 归档找到这些 id，按「追问窗口内」重新走路由（不受旧消息只计数的限制）。只处理今天/昨天归档里的群消息。"""
    if not path.exists():
        return 0
    try:
        req = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
    except (OSError, ValueError):
        return 0
    ids = {int(i) for i in req.get("ids") or []}
    n = 0
    raw_dir = raw_dir or RAW_DIR
    for day in (time.strftime("%Y%m%d"), time.strftime("%Y%m%d", time.localtime(time.time() - 86400))):
        p = raw_dir / f"raw-{day}.jsonl"
        if not p.exists() or not ids:
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("id") not in ids:
                continue
            ids.discard(row["id"])
            msg = normalize(row.get("payload") or {})
            if not msg.is_group or msg.is_self:
                continue
            if req.get("followup") and msg.self_wxid not in msg.at_list:
                msg.at_list = list(msg.at_list) + [msg.self_wxid]
                msg.addressed = "followup"
            msg._rid, msg._lag = f"{row['id']}(补处理)", ""
            log.info("id=%s 补处理（replay_once）群=%s 内容=%r", row["id"], msg.room_id, (msg.content or "")[:60])
            submit(router, msg, log, queues)
            n += 1
    return n


def make_debouncer(router, log, queues):
    from features.debounce import Debouncer

    def cand(m) -> bool:
        sess = m.room_id or m.from_wxid
        if m.from_wxid in router.ignore_senders or sess not in router.allow_targets or sess in router.block_targets:
            return False
        if not m.is_group or m.self_wxid in m.at_list:
            return True
        fu = getattr(router, "followup", None)
        return bool(fu is not None and fu.active(m.room_id, m.from_wxid))

    return Debouncer(lambda m: submit(router, m, log, queues), cand)


def main() -> None:
    cfg = load_cfg()
    log_path = DATA_DIR / "poller.log"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.addFilter(_Redact([cfg.get("secret"), os.environ.get("GEWE_TOKEN"), os.environ.get("AMAP_KEY"),
                               os.environ.get("MINIMAX_API_KEY"), os.environ.get("DOUBAO_TTS_API_KEY")]))
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [handler]
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    log = logging.getLogger("poller")

    send_enabled = os.environ.get("BOT_SEND_ENABLED") == "1"
    router = build_router(send_enabled=send_enabled, report_async=True,
                          context_path=DATA_DIR / "chat_context.json",
                          memory_path=DATA_DIR / "memory.json",
                          learned_path=DATA_DIR / "learned_notes.json",
                          followup_path=DATA_DIR / "followup.json")   # 陪聊共享上下文落盘，重启不丢   # 日报后台生成，不阻塞轮询
    router.games.start_ticker()                                           # 游戏超时推进
    router.cache.prewarm(sorted(router.allow_targets))                    # 白名单群成员表后台预拉
    log.info("poller 启动 pid=%s server=%s last_pulled_id=%s 发送=%s 白名单=%s 间隔=%ss 长轮询=%ss 陪聊模型key=%s",
             os.getpid(), cfg.get("server_url"), cfg.get("last_pulled_id"),
             "开启" if send_enabled else "关闭(simulate)", sorted(router.allow_targets) or "空(一律不发)",
             INTERVAL, WAIT, "已配置" if os.environ.get("MINIMAX_API_KEY") else "未配置")

    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__("v", True))
    signal.signal(signal.SIGINT, lambda *a: stop.__setitem__("v", True))

    from session_queue import SessionQueues
    queues = SessionQueues()                                               # 每群一条队列：同群保序、不同群并行
    debouncer = make_debouncer(router, log, queues)                        # 同一人 2.5s 内连发 → 合并
    from features.online_watch import OnlineWatch
    watch = OnlineWatch()                                                  # 掉线/断流 → 只通知主人（待办 inbox + 日志标红）
    watch.start()
    from features.ai_news import AiNewsScheduler
    news = AiNewsScheduler(router)                                         # 每天 08:30 AI 快讯（开关 ai_news）
    news.start()
    router.gzh.start()                                                     # 公众号新文推群（开关 gzh_promo；23–8 点排队）
    replay_once(router, log, queues)                                       # 运维一次性补处理（data/replay_once.json）
    last_id = int(cfg.get("last_pulled_id") or 0)
    idle, last_beat = 0, time.time()
    out = {"since": None, "n": 0}                                          # 中转断连计时
    first_pull = True
    while not stop["v"]:
        try:
            cfg = load_cfg()
            rows = pull_guarded(cfg, last_id, FIRST_LIMIT if first_pull else None)
            ok_at = time.time()
            first_pull = False
            for row in rows:
                try:
                    handle_row(router, row, log, queues, debouncer)
                    watch.saw_callback()
                except Exception:
                    log.exception("id=%s 处理失败（跳过，不阻塞后续）", row.get("id"))
                last_id = max(last_id, int(row["id"]))
                save_last_id(last_id)
            idle = 0 if rows else idle + 1
            if time.time() - last_beat >= 60:   # 每分钟一条心跳，证明在跑
                if time.time() - last_beat > 180:
                    log.warning("心跳间隔 %.0fs（正常 ≈60s）：轮询曾被卡住或机器挂起过", time.time() - last_beat)
                log.info("heartbeat last_pulled_id=%s", last_id)
                last_beat = time.time()
        except Exception as e:
            if out["since"] is None:
                out["since"], out["n"] = time.time(), 0
            out["n"] += 1
            (log.error if out["n"] == 1 else log.info)("轮询异常（第 %d 次）: %s", out["n"], str(e)[:300])
            reset_session()                                    # 重建连接池，别卡在坏连接上
            delay = min(BACKOFF_MAX, 0.5 * (2 ** (out["n"] - 1)))
            time.sleep(delay)
            continue
        prev_ok = out.get("last_ok")
        out["last_ok"] = ok_at
        if out["since"] is not None:
            out["ok_at"] = prev_ok or out["since"]
            log.warning("中转恢复：断连 %.1fs（自上次成功拉取 %.0fs，失败 %d 次）", time.time() - out["since"],
                        time.time() - out.get("ok_at", out["since"]), out["n"])
            out["since"] = None
        time.sleep(INTERVAL)
    debouncer.flush_all()
    ok = queues.drain(20)
    log.info("poller 退出 last_pulled_id=%s 队列%s", last_id, "已清空" if ok else f"未清空（剩 {queues.pending()} 条）")


if __name__ == "__main__":
    main()
