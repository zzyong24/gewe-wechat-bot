"""功能 8：文字游戏。GameManager 保证一群同时只有一局，并负责超时推进/结束（后台 ticker 线程）。

每个游戏：一个「开局」CommandFeature（toggle_key=game_xxx）+ 统一的 GameInputFeature（把群消息喂给本群当前那局）。
游戏对象接口（Game）：
  on_message(msg) -> str | Reply | list | None   处理本局相关消息；None=与本局无关（继续走后面的功能）
  tick(now)       -> str | Reply | list | None   定时推进（阶段超时）；结束时把 self.over 置 True
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("games")
GAME_TIMEOUT = 600          # 任何一局最长无动作时间（秒）
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "games"


class Game:
    kind = "game"
    title = "游戏"
    idle_timeout = GAME_TIMEOUT

    def __init__(self, room: str):
        self.room, self.over = room, False
        self.started = self.last_active = time.time()

    def touch(self):
        self.last_active = time.time()

    def on_message(self, msg):
        return None

    def tick(self, now: float):
        if now - self.last_active > self.idle_timeout:
            self.over = True
            return self.on_timeout()
        return None

    def on_timeout(self):
        return f"⏰ {self.title}太久没人动，自动结束啦～"

    def stop(self, by: str):
        self.over = True
        return f"🛑 {self.title}已结束。"


class GameManager:
    def __init__(self, deliver=None, out_dir: Path | None = None):
        self.games: dict[str, Game] = {}
        self.lock = threading.RLock()
        self.deliver = deliver
        self.out_dir = Path(out_dir or OUT_DIR)
        self._ticker = None

    def get(self, room) -> Game | None:
        g = self.games.get(room)
        return g if g and not g.over else None

    def start(self, game: Game):
        with self.lock:
            cur = self.get(game.room)
            if cur:
                return f"本群正在进行「{cur.title}」，一次只能玩一局哦～（@小助手 结束游戏 可提前结束）"
            self.games[game.room] = game
            self._cleanup()
            return None

    def _cleanup(self, max_age: float = 86400):
        """删 1 天前的游戏图（图已上传到 relay，本地不需要长期保留）。"""
        try:
            for p in self.out_dir.glob("*.*"):
                if time.time() - p.stat().st_mtime > max_age:
                    p.unlink(missing_ok=True)
        except OSError:
            pass

    def finish(self, room):
        with self.lock:
            self.games.pop(room, None)

    def tick(self, now: float | None = None) -> list:
        """推进所有对局，返回需要发出的 Reply 列表（ticker 线程会自动 deliver）。"""
        from router import Reply
        now = now or time.time()
        out = []
        with self.lock:
            for room, g in list(self.games.items()):
                try:
                    r = g.tick(now)
                except Exception:
                    log.exception("game tick failed %s", room)
                    g.over, r = True, f"😵 {g.title}出错了，已结束"
                for x in (r if isinstance(r, list) else [r] if r else []):
                    out.append(x if isinstance(x, Reply) else Reply(room, str(x)))
                if g.over:
                    self.games.pop(room, None)
        return out

    def start_ticker(self, interval: float = 5.0):
        if self._ticker or not self.deliver:
            return

        def loop():
            while True:
                time.sleep(interval)
                for r in self.tick():
                    try:
                        self.deliver(r)
                    except Exception:
                        log.exception("deliver tick reply failed")
        self._ticker = threading.Thread(target=loop, daemon=True, name="game-ticker")
        self._ticker.start()


class GameInputFeature:
    """把本群消息交给当前对局。放在开局指令之后、陪聊之前。对局所属游戏的开关被关掉时不再接收输入。"""
    name = "games.input"
    passive = False
    toggle_key = None

    def __init__(self, manager: GameManager, settings=None):
        self.m, self.settings = manager, settings
        self._out = {}

    def match(self, msg) -> bool:
        if not (msg.event == "AddMsg" and msg.is_group and msg.msg_type == 1 and not msg.is_self):
            return False
        g = self.m.get(msg.room_id)
        if not g or (self.settings and not self.settings.enabled(msg.room_id, g.kind)):
            return False
        with self.m.lock:
            out = g.on_message(msg)
        if out is None:
            return False
        g.touch()
        if g.over:
            self.m.finish(msg.room_id)
        self._out[id(msg)] = out
        return True

    def handle(self, msg):
        out = self._out.pop(id(msg), None)
        if isinstance(out, list):       # 多条：第一条作为回复，其余直接投递
            for extra in out[1:]:
                if self.m.deliver:
                    from router import Reply
                    self.m.deliver(extra if isinstance(extra, Reply) else Reply(msg.room_id, str(extra)))
            out = out[0] if out else None
        return out


class StopGameFeature:
    name = "games.stop"
    passive = False
    toggle_key = None

    def __init__(self, manager: GameManager):
        self.m = manager

    def match(self, msg):
        from features.common import at_command
        return at_command(msg, ("结束游戏", "结束")) is not None and self.m.get(msg.room_id) is not None

    def handle(self, msg):
        g = self.m.get(msg.room_id)
        out = g.stop(msg.from_wxid)
        self.m.finish(msg.room_id)
        return out
