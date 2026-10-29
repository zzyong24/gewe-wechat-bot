"""经济 + 关系系统（功能 7 的底座）。每人每群一个账户；关系（结婚/师徒/雇佣）也按群隔离。
存储：data/economy.db（SQLite，权限 600）。设计参考 HenryXiaoYang/XYBotV2 的积分签到思路（仅参考逻辑）。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(room TEXT, wxid TEXT, coins INTEGER DEFAULT 0, streak INTEGER DEFAULT 0,
    last_day TEXT DEFAULT '', total_days INTEGER DEFAULT 0, PRIMARY KEY(room, wxid));
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT, wxid TEXT, delta INTEGER,
    reason TEXT, ts INTEGER);
-- kind: spouse(a<b 无向) / mentor(a=师父,b=徒弟) / owner(a=老板,b=打工人)
CREATE TABLE IF NOT EXISTS relations(room TEXT, kind TEXT, a TEXT, b TEXT, since INTEGER, PRIMARY KEY(room, kind, a, b));
CREATE TABLE IF NOT EXISTS items(room TEXT, wxid TEXT, item TEXT, qty INTEGER DEFAULT 0, equipped INTEGER DEFAULT 0,
    PRIMARY KEY(room, wxid, item));
CREATE TABLE IF NOT EXISTS kv(room TEXT, wxid TEXT, k TEXT, v TEXT, PRIMARY KEY(room, wxid, k));
"""


class InsufficientCoins(Exception):
    def __init__(self, have: int, need: int):
        super().__init__(f"积分不足：需要 {need}，你只有 {have}")
        self.have, self.need = have, need


@dataclass
class Account:
    room: str
    wxid: str
    coins: int = 0
    streak: int = 0
    last_day: str = ""
    total_days: int = 0


class Economy:
    def __init__(self, path: str | Path | None = None):
        p = str(path) if path else ":memory:"
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
        self.db.executescript(SCHEMA)
        if path:
            os.chmod(path, 0o600)
        self.lock = threading.RLock()

    # ---- 账户 ----
    def account(self, room: str, wxid: str) -> Account:
        r = self.db.execute("SELECT coins, streak, last_day, total_days FROM accounts WHERE room=? AND wxid=?",
                            (room, wxid)).fetchone()
        return Account(room, wxid, *r) if r else Account(room, wxid)

    def _ensure(self, room, wxid):
        self.db.execute("INSERT OR IGNORE INTO accounts(room, wxid) VALUES(?,?)", (room, wxid))

    def add(self, room: str, wxid: str, delta: int, reason: str) -> int:
        """加/扣积分（扣成负数会抛 InsufficientCoins）。返回新余额。"""
        with self.lock:
            self._ensure(room, wxid)
            have = self.account(room, wxid).coins
            if have + delta < 0:
                raise InsufficientCoins(have, -delta)
            self.db.execute("UPDATE accounts SET coins=coins+? WHERE room=? AND wxid=?", (delta, room, wxid))
            self.db.execute("INSERT INTO ledger(room, wxid, delta, reason, ts) VALUES(?,?,?,?,?)",
                            (room, wxid, delta, reason, int(time.time())))
            return have + delta

    def transfer(self, room: str, src: str, dst: str, amount: int, reason: str):
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.add(room, src, -amount, reason)
                self.add(room, dst, amount, reason)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def set_sign(self, room, wxid, streak, day):
        with self.lock:
            self._ensure(room, wxid)
            self.db.execute("UPDATE accounts SET streak=?, last_day=?, total_days=total_days+1 WHERE room=? AND wxid=?",
                            (streak, day, room, wxid))

    def top(self, room: str, n: int = 10) -> list[tuple[str, int]]:
        return self.db.execute("SELECT wxid, coins FROM accounts WHERE room=? ORDER BY coins DESC, wxid LIMIT ?",
                               (room, n)).fetchall()

    def rank(self, room: str, wxid: str) -> int:
        c = self.account(room, wxid).coins
        return self.db.execute("SELECT COUNT(*) FROM accounts WHERE room=? AND coins>?", (room, c)).fetchone()[0] + 1

    # ---- 关系 ----
    def relate(self, room, kind, a, b):
        if kind == "spouse":
            a, b = sorted((a, b))
        self.db.execute("INSERT OR REPLACE INTO relations VALUES(?,?,?,?,?)", (room, kind, a, b, int(time.time())))

    def unrelate(self, room, kind, a, b):
        if kind == "spouse":
            a, b = sorted((a, b))
        return self.db.execute("DELETE FROM relations WHERE room=? AND kind=? AND a=? AND b=?",
                               (room, kind, a, b)).rowcount

    def related(self, room, kind, a=None, b=None) -> list[tuple[str, str, int]]:
        q, args = "SELECT a, b, since FROM relations WHERE room=? AND kind=?", [room, kind]
        if a is not None:
            q += " AND a=?"; args.append(a)
        if b is not None:
            q += " AND b=?"; args.append(b)
        return self.db.execute(q + " ORDER BY since", args).fetchall()

    def spouse(self, room, wxid) -> str | None:
        r = self.db.execute("SELECT a, b FROM relations WHERE room=? AND kind='spouse' AND (a=? OR b=?)",
                            (room, wxid, wxid)).fetchone()
        return (r[1] if r[0] == wxid else r[0]) if r else None

    # ---- 物品 ----
    def items(self, room, wxid) -> list[tuple[str, int, int]]:
        return self.db.execute("SELECT item, qty, equipped FROM items WHERE room=? AND wxid=? AND qty>0 ORDER BY item",
                               (room, wxid)).fetchall()

    def give_item(self, room, wxid, item, qty=1):
        self.db.execute("INSERT INTO items(room, wxid, item, qty) VALUES(?,?,?,?) "
                        "ON CONFLICT(room, wxid, item) DO UPDATE SET qty=qty+excluded.qty", (room, wxid, item, qty))

    def set_equipped(self, room, wxid, item, on: bool):
        self.db.execute("UPDATE items SET equipped=? WHERE room=? AND wxid=? AND item=?", (int(on), room, wxid, item))

    # ---- 杂项键值（每日娶群友、身价、免打扰等）----
    def get(self, room, wxid, k, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE room=? AND wxid=? AND k=?", (room, wxid, k)).fetchone()
        return r[0] if r else default

    def put(self, room, wxid, k, v):
        if v is None:
            self.db.execute("DELETE FROM kv WHERE room=? AND wxid=? AND k=?", (room, wxid, k))
        else:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?,?,?)", (room, wxid, k, str(v)))

    def scan(self, room, k) -> list[tuple[str, str]]:
        return self.db.execute("SELECT wxid, v FROM kv WHERE room=? AND k=?", (room, k)).fetchall()


class Pending:
    """待确认请求（求婚 / 拜师 / 收徒）：key=(room, 被请求人)；同一人同时只有一个待处理请求，过期自动作废。"""

    def __init__(self, ttl: int = 300):
        self.ttl, self._d, self._lock = ttl, {}, threading.Lock()

    def put(self, room, target, kind, src, extra=None):
        with self._lock:
            self._d[(room, target)] = {"kind": kind, "src": src, "extra": extra, "ts": time.time()}

    def pop(self, room, target):
        with self._lock:
            v = self._d.pop((room, target), None)
        return v if v and time.time() - v["ts"] <= self.ttl else None

    def peek(self, room, target):
        v = self._d.get((room, target))
        return v if v and time.time() - v["ts"] <= self.ttl else None
