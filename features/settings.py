"""功能 9：功能设置开关（按群 / 全局启停各功能）。

生效顺序：群级设置 > 全局设置 > DEFAULTS。持久化到 data/settings.json（权限 600，原子写入）。
会话 key：群用 xxx@chatroom，私聊用对方 wxid（私聊同样走这套开关）。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

# 功能 key → (中文名, 默认开关)
FEATURES: dict[str, tuple[str, bool]] = {
    "chat_companion": ("智能陪聊", False),   # 要调模型、花额度、会主动说话 → 新群默认关，管理员手动开
    "group_members": ("成员列表", True),      # 只在有人发触发词时回复，安全
    "speech_stats": ("发言统计", True),       # 被动计数 + 触发词日报
    "member_change": ("进退群提醒", True),    # 进群 / 退群通知文案（welcome/farewell 开时被它们取代）
    "welcome": ("进群欢迎", False),           # 小助手亲自欢迎（@新人；白名单群用人设调模型，失败回退模板）
    "welcome_voice": ("进群语音", False),     # 欢迎文字后再补一条固定模板语音（features/welcome_voice.py）
    "farewell": ("退群送别", False),          # 退群 / 被移出送别（纯模板，不调模型）
    "chime": ("主动插话", False),
    "location_reply": ("位置回复", False),     # 收到位置消息回一句地址（amap 可用时用高德逆地理，否则只用 XML 里的地址）
    "amap": ("地图查询", False),               # 高德：天气 / 附近 / 路线（调高德 Web 服务，有日上限；不调模型）
    "web_search": ("联网搜索", False),         # 陪聊模型可联网搜（MiniMax 搜索接口；新闻/价格/最新等才带工具；有日上限）
    "image_gen": ("画图", False),             # 「画一张XX」/模型工具 → MiniMax image-01 生图（每群每天 10 张）
    "asr": ("语音转文字", False),             # 对小助手说的语音（窗口内/私聊）→ MiniMax asr-1.0 转写后当文字处理（有日上限）
    "vision": ("识图", False),                # @/引用图片/窗口内发图 → gewe 下载图片 → 多模态模型看图回答（每群每天 30 张）
    "voice_reply": ("语音回复", False),
    "memory": ("长期记忆", False),            # 按群/按人记少量稳定事实注入陪聊；「@小助手 忘掉我」清除（features/memory.py）
    "chime_judge": ("插话判断", False),       # 主动插话前再做一步轻量判断（相关度/有人在等回答/刚插过），只在开了 chime 的群生效
    "ai_news": ("AI快讯", False),             # 每天 08:30 推 AI 快讯（features/ai_news.py）        # 陪聊偶尔/被要求时用语音回（豆包 TTS，有日上限；features/voice_reply.py）
    "daily_report": ("AI日报", False),
    "gzh_promo": ("公众号新文推群", False),     # 「示例公众号」群发新文 → 总结 + 链接卡片推到开了的群（features/gzh_promo.py）        # 要调模型（一次几千 token）→ 默认关
    # 功能 7 签到养成（纯规则，不调模型）
    "sign_in": ("签到", True),
    "marriage": ("结婚", False),
    "mentor": ("师徒", False),
    "equipment": ("装备", False),
    "slave": ("买卖群友", False),
    # 功能 8 文字游戏
    "game_handle": ("猜成语", True),
    "game_minesweeper": ("扫雷", True),
    "game_idiom_chain": ("成语接龙", True),
    "game_undercover": ("谁是卧底", False),  # 白名单群调模型出词
    "game_guess_song": ("猜歌", False),      # 白名单群调模型写提示
    "game_gomoku": ("五子棋", False),        # 双人五子棋（纯规则）
}
ALIASES = {
    "陪聊": "chat_companion", "智能陪聊": "chat_companion", "聊天": "chat_companion",
    "成员列表": "group_members", "群成员": "group_members",
    "发言统计": "speech_stats", "统计": "speech_stats", "发言": "speech_stats",
    "日报": "daily_report", "AI日报": "daily_report", "ai日报": "daily_report", "群聊总结": "daily_report", "总结": "daily_report",
    "进退群提醒": "member_change", "进退群": "member_change", "群员动态": "member_change",
    "进群提醒": "member_change", "退群提醒": "member_change",
    "欢迎": "welcome", "欢迎语": "welcome", "迎新": "welcome", "进群语音": "welcome_voice", "欢迎语音": "welcome_voice", "送别": "farewell", "退群欢送": "farewell",
    "位置": "location_reply", "定位": "location_reply", "插话": "chime", "主动插话": "chime", "proactive": "chime", "冒泡": "chime",
    "语音": "voice_reply", "语音回复": "voice_reply",
    "搜索": "web_search", "联网": "web_search", "联网搜索": "web_search", "上网": "web_search",
    "语音转文字": "asr", "听语音": "asr", "转写": "asr", "识图": "vision", "看图": "vision", "图片理解": "vision",
    "记忆": "memory", "长期记忆": "memory", "插话判断": "chime_judge", "AI快讯": "ai_news", "快讯": "ai_news", "AI日报": "ai_news",
    "画图": "image_gen", "生图": "image_gen", "画画": "image_gen", "生成图片": "image_gen",
    "地图": "amap", "高德": "amap", "天气": "amap", "导航": "amap", "路线": "amap", "附近": "amap", "地图查询": "amap",
    "积分": "sign_in", "打卡": "sign_in", "签到养成": "sign_in",
    "娶群友": "marriage", "求婚": "marriage", "拜师": "mentor", "师门": "mentor",
    "商店": "equipment", "背包": "equipment", "雇佣": "slave", "买群友": "slave", "奴隶": "slave",
    "汉字wordle": "game_handle", "接龙": "game_idiom_chain", "卧底": "game_undercover", "猜歌名": "game_guess_song", "下棋": "game_gomoku", "下五子棋": "game_gomoku",
}


def resolve(name: str) -> str | None:
    name = (name or "").strip()
    if name in FEATURES:
        return name
    if name in ALIASES:
        return ALIASES[name]
    return next((k for k, (cn, _) in FEATURES.items() if cn == name), None)   # 中文名本身也可用


class FeatureSettings:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: dict = {"global": {}, "rooms": {}}
        if self.path and self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = {"global": dict(d.get("global") or {}), "rooms": dict(d.get("rooms") or {})}
            except (ValueError, OSError):
                pass   # 配置损坏时回落默认值（不覆盖原文件，等下次写入）

    def enabled(self, session: str | None, feature: str) -> bool:
        if feature not in FEATURES:
            return True
        room = self._data["rooms"].get(session or "", {})
        if feature in room:
            return bool(room[feature])
        if feature in self._data["global"]:
            return bool(self._data["global"][feature])
        return FEATURES[feature][1]

    def set(self, session: str | None, feature: str, on: bool | None) -> None:
        """session=None 表示全局；on=None 表示清除该层设置（回落上一层）。"""
        if feature not in FEATURES:
            raise KeyError(feature)
        with self._lock:
            layer = self._data["global"] if session is None else self._data["rooms"].setdefault(session, {})
            if on is None:
                layer.pop(feature, None)
            else:
                layer[feature] = bool(on)
            self._save()

    def room_get(self, session: str | None, key: str, default=None):
        """读群级任意字段（如 welcome_tpl 字符串）；不在 FEATURES 里的 key 也能读。"""
        return self._data["rooms"].get(session or "", {}).get(key, default)

    def room_set(self, session: str, key: str, value) -> None:
        """写群级任意字段；value=None 表示删除。不经 FEATURES 校验（给 welcome_tpl 等用）。"""
        if not session:
            raise ValueError("room_set 需要 session")
        with self._lock:
            layer = self._data["rooms"].setdefault(session, {})
            if value is None:
                layer.pop(key, None)
            else:
                layer[key] = value
            self._save()

    def status(self, session: str | None) -> list[tuple[str, str, bool, str]]:
        """[(key, 中文名, 是否开启, 来源 群设置/全局/默认)]"""
        room = self._data["rooms"].get(session or "", {})
        out = []
        for k, (cn, _) in FEATURES.items():
            src = "本群设置" if k in room else "全局设置" if k in self._data["global"] else "默认"
            out.append((k, cn, self.enabled(session, k), src))
        return out

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
