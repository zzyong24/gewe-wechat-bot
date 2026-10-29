"""组装 Router + 功能模块。callback_server 与其它入口共用。"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

from router import Router, Reply
from features.group_members import GroupMembersFeature, parse_member_list
from features.speech_stats import SpeechStats, SpeechStatsRecorder, SpeechReportFeature
from features.chat_companion import ChatCompanionFeature, DEFAULT_PERSONA
from features.settings import FeatureSettings
from features.admin import SettingsAdminFeature
from features.member_change import MemberChangeFeature
from features.persona import PersonaStore, PersonaAdminFeature
from features.daily_report import DailyReporter, DailyReportFeature
from features.economy import Economy, Pending
from features.play import PlayContext
from features.play.signin import SignInFeature
from features.play.relations import MarriageFeature, MentorFeature, RespondFeature
from features.play.equipment import EquipmentFeature
from features.play.hire import HireFeature
from features.games import GameManager, GameInputFeature, StopGameFeature
from features.games.handle import HandleStartFeature
from features.games.minesweeper import MinesweeperStartFeature
from features.games.idiom_chain import IdiomChainStartFeature
from features.games.undercover import UndercoverStartFeature, UndercoverPrivateWordFeature
from features.games.guess_song import GuessSongStartFeature
from features.games.gomoku import GomokuStartFeature

DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", Path(__file__).resolve().parent / "data"))


class MemberCache:
    """群成员 / 群名缓存，用于把 wxid 翻成群昵称（TTL 默认 30 分钟）。"""

    def __init__(self, fetch_list, fetch_info=None, ttl: int = 1800):
        self.fetch_list, self.fetch_info, self.ttl = fetch_list, fetch_info, ttl
        self._members: dict[str, tuple[float, dict]] = {}
        self._names: dict[str, str] = {}
        self._refreshing: set[str] = set()
        self.background = True

    def _fetch(self, room_id: str) -> dict:
        try:
            ms = {m.wxid: m.shown for m in parse_member_list(self.fetch_list(room_id) or {})}
        except Exception:
            hit = self._members.get(room_id)
            ms = hit[1] if hit else {}
        self._members[room_id] = (time.time(), ms)
        return ms

    def _load(self, room_id: str) -> dict:
        """过期时先返回旧数据、后台刷新（不让一次 2~3 秒的 gewe 调用卡住消息处理）；从没拉过才同步拉。"""
        hit = self._members.get(room_id)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        if hit and self.background:
            if room_id not in self._refreshing:
                self._refreshing.add(room_id)

                def run():
                    try:
                        self._fetch(room_id)
                    finally:
                        self._refreshing.discard(room_id)
                threading.Thread(target=run, daemon=True, name="member-refresh").start()
            return hit[1]
        return self._fetch(room_id)

    def prewarm(self, rooms) -> None:
        """启动时后台预拉（白名单群），首条消息不用等成员列表。"""
        def run():
            for r in rooms:
                if r.endswith("@chatroom"):
                    self._fetch(r)
                    self.room_name(r)
        threading.Thread(target=run, daemon=True, name="member-prewarm").start()

    def name(self, room_id: str, wxid: str) -> str:
        return self._load(room_id).get(wxid, "")

    def room_name(self, room_id: str) -> str:
        if room_id not in self._names and self.fetch_info:
            try:
                self._names[room_id] = (self.fetch_info(room_id) or {}).get("nickName", "")
            except Exception:
                return ""
        return self._names.get(room_id, "")


def _observe(path: Path | None = None) -> dict:
    import json
    p = Path(path) if path else DATA_DIR / "observe.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("rooms") or {}
    except (OSError, ValueError):
        return {}


def observe_rooms(path: Path | None = None) -> set[str]:
    """data/observe.json 里的观察群：只读收集，发送闸硬拦（即使误加进白名单也不发）。
    例外：条目里 "allow_send": true 的群（主人明确同意）不硬拦，但仍要在 BOT_SEND_ALLOW 里，且受 send_policy 约束。"""
    return {r for r, v in _observe(path).items() if not (isinstance(v, dict) and v.get("allow_send") is True)}


def observe_policies(path: Path | None = None) -> dict[str, dict]:
    """allow_send 观察群的发送策略：{room: {daily_reply_cap, user_cooldown_sec, followup}}。"""
    out = {}
    for r, v in _observe(path).items():
        if isinstance(v, dict) and v.get("allow_send") is True:
            pol = dict(v.get("send_policy") or {})
            pol.setdefault("daily_reply_cap", 60)
            pol.setdefault("user_cooldown_sec", 60)
            pol.setdefault("followup", "owner_only")
            out[r] = pol
    return out


def ignore_senders() -> set[str]:
    """全局忽略的发言人（别的机器人，防互相触发）：只做被动统计，不回复，即使 @ 小助手。env BOT_IGNORE_WXIDS 覆盖。"""
    import os
    v = os.environ.get("BOT_IGNORE_WXIDS", "")      # 逗号分隔，例：别的机器人的 wxid
    return {w.strip() for w in v.split(",") if w.strip()}


def default_sender(r: Reply):
    """真实发送：有图先发图（失败则降级发文字），否则发文字。"""
    import gewe_client as g
    if r.image_path:
        try:
            import imagehost
            return imagehost.send_image(r.to_wxid, r.image_path)
        except Exception as e:
            import logging
            logging.getLogger("app").warning("发图失败，降级文字: %s", str(e)[:200])
    return g.post_text(r.to_wxid, r.content, r.ats)


def _word_card_link(out_dir: Path):
    """谁是卧底「词卡链接」：把词画成小图传到 relay 图床，返回不可猜 URL（不主动私信的替代方案）。"""
    def make(word: str, tag: str):
        import secrets
        from PIL import Image, ImageDraw, ImageFont
        import imagehost
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"word-{secrets.token_hex(8)}.png"
        im = Image.new("RGB", (480, 220), "#fff8e7")
        d = ImageDraw.Draw(im)
        f = ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 64)
        d.text((240, 95), word, font=f, fill="#333", anchor="mm")
        d.text((240, 180), "谁是卧底 · 只给自己看", font=ImageFont.truetype(
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 22), fill="#999", anchor="mm")
        im.save(p)
        try:
            return imagehost.upload(p)
        finally:
            p.unlink(missing_ok=True)
    return make


def send_allowlist() -> set[str]:
    """真实发送白名单：只认环境变量 BOT_SEND_ALLOW（逗号分隔 wxid / xxx@chatroom）。
    未设置 = 空集合 = 任何对象都不真发（比旧默认 filehelper 更保守）。"""
    return {w.strip() for w in os.environ.get("BOT_SEND_ALLOW", "").split(",") if w.strip()}


def build_router(fetch_list=None, fetch_info=None, sender=None, send_enabled: bool = False,
                 db_path: Path | None = None, llm=None, persona: str | None = None,
                 allow_targets: set[str] | None = None, settings: FeatureSettings | None = None,
                 llm_allow: Callable[[str], bool] | None = None, personas: PersonaStore | None = None,
                 report_async: bool = False, report_image: bool = True, economy: Economy | None = None,
                 rng=None, games_dir: Path | None = None, word_link=None,
                 block_targets: set[str] | None = None, chime_opts: dict | None = None, amap=None,
                 amap_tools: bool = True, room_policy: dict | None = None,
                 context_path: Path | None = None, voice_sender=None,
                 search_fn=None, image_fn=None, image_async: bool = True, media_io: dict | None = None,
                 memory_path=None, memory_async: bool = True, learned_path=None,
                 followup_path=None, article_fetch=None, gzh_opts: dict | None = None) -> Router:
    """llm：实现 chat(user, system, history) 的对象；None 时懒加载 MiniMaxClient（key 从 env 读）。
    allow_targets：真发白名单，默认读 env BOT_SEND_ALLOW（空=一律不发）。
    llm_allow：哪些会话允许调模型，默认 = 真发白名单（省额度：测试阶段只有白名单会话花钱）。
    settings：功能开关，默认 data/settings.json。"""
    real_gewe = fetch_list is None
    if fetch_list is None:   # 默认走真实 gewe
        import gewe_client as g
        fetch_list, fetch_info = g.get_chatroom_member_list, g.get_chatroom_info
        if sender is None:
            sender = default_sender
    cache = MemberCache(fetch_list, fetch_info)
    stats = SpeechStats(db_path or DATA_DIR / "stats.db")
    settings = settings if settings is not None else FeatureSettings(DATA_DIR / "settings.json")
    router = Router(send_enabled=send_enabled, sender=sender,
                    allow_targets=allow_targets if allow_targets is not None else send_allowlist(),
                    block_targets=block_targets if block_targets is not None else observe_rooms(),
                    ignore_senders=ignore_senders(),
                    room_policy=room_policy if room_policy is not None else (observe_policies() if block_targets is None else {}),
                    policy_state_path=(Path(db_path).parent if db_path else DATA_DIR) / "room_quota.json",
                    gate=settings.enabled)
    from features.chat_context import ChatContext
    router.context = ChatContext(context_path)        # 陪聊共享上下文；只有 poller 传路径才落盘（测试不写真实文件）
    router.name_of = cache.name
    if llm_allow is None:
        llm_allow = lambda session: session in router.allow_targets
    # 顺序：被动监听 → 管理指令 → 触发词功能 → 陪聊兜底（「@小助手 成员列表」走成员列表）
    router.register(SpeechStatsRecorder(stats, cache.name))          # 被动：发言计数
    from features.location import LocationFeature
    from features.amap import AmapFeature
    import amap_client
    if amap is None:
        amap = amap_client.AmapClient(amap_client.Quota(DATA_DIR / "amap_quota.json"))
    amap_feat = AmapFeature(amap, allow=lambda s: s in router.allow_targets, blocked=lambda s: s in router.block_targets,
                            state_path=(Path(db_path).parent if db_path else DATA_DIR) / "amap_lastloc.json")
    router.register(LocationFeature(settings, amap_feat))             # 被动：位置消息解析 + 记位置（回复默认关）
    member_feat = MemberChangeFeature(                               # 被动：进退群提醒 / 欢迎 / 送别
        fetch_list, settings=settings, llm_for=lambda room: llm_for(room),
        persona_of=lambda room: personas.get(room), room_name=cache.room_name)
    router.register(member_feat)
    admin = SettingsAdminFeature(settings, fetch_list)
    router.register(admin)                                           # 开关指令（不受开关控制）
    personas = personas if personas is not None else PersonaStore(DATA_DIR / "personas.json")
    personas.default = persona or DEFAULT_PERSONA
    router.register(PersonaAdminFeature(personas))                   # 人设指令（仅主人本人可改）
    router.register(GroupMembersFeature(fetch_list, fetch_info))
    router.register(SpeechReportFeature(stats, cache.room_name))
    router.register(amap_feat)                                       # 高德：天气 / 附近 / 路线（开关 amap）
    router.amap = amap_feat
    from features.followup import Followup
    router.followup = Followup(owner_only_rooms={r for r, p in router.room_policy.items() if p.get("followup") == "owner_only"},
                               off_rooms={r for r, p in router.room_policy.items() if p.get("followup") == "off"},
                               path=followup_path)                                     # 追问窗口 / 引用视同 @（router.dispatch 开头处理）
    if llm is None:
        from llm import MiniMaxClient
        llm = MiniMaxClient          # 类本身作为工厂，首次触发时才实例化

    def llm_for(room):               # 游戏用：只有模型白名单会话才给模型
        if not llm_allow(room):
            return None
        return llm() if isinstance(llm, type) else llm

    from features.followup import make_judge
    if followup_path is not None and os.environ.get("BOT_FOLLOWUP_JUDGE", "1") not in ("0", "false", "no"):   # 只在 poller（传了落盘路径）启用
        router.followup.judge = make_judge(llm_for, cache.name)      # 窗口外的追问：MiniMax 判一下是不是接着跟小助手聊

    # 主人静音：主人本人让它休息 → 该群静音，只有主人能解除（router.dispatch 里处理）
    from features.owner_mute import OwnerMute, make_judge as make_mute_judge
    router.mute = OwnerMute(Path(followup_path).parent / "owner_mute.json" if followup_path is not None else None,
                            judge=make_mute_judge(llm_for))
    # ---- 功能 7 签到养成（规则计算，不调模型）----
    base = Path(db_path).parent if db_path else DATA_DIR      # 测试传了 db_path 时，经济库/游戏图跟着走，不污染 data/
    eco = economy if economy is not None else Economy(base / "economy.db")
    ctx = PlayContext(eco, cache.name, lambda room: list(cache._load(room).keys()), rng=rng, settings=settings)
    pending = Pending()
    equip = EquipmentFeature(ctx)
    marriage, mentor = MarriageFeature(ctx, pending), MentorFeature(ctx, pending)
    for f in (SignInFeature(ctx, equip), marriage, mentor, RespondFeature(ctx, pending, {"marriage": marriage, "mentor": mentor}),
              equip, HireFeature(ctx)):
        router.register(f)
    # ---- 功能 8 文字游戏（一群一局，后台 ticker 推进超时）----
    games = GameManager(deliver=router.deliver, out_dir=games_dir or base / "games")
    if word_link is None:
        word_link = _word_card_link(games.out_dir)
    router.register(StopGameFeature(games))
    router.register(GameInputFeature(games, settings))               # 进行中的对局优先吃消息
    router.register(HandleStartFeature(games, rng))
    router.register(MinesweeperStartFeature(games, rng))
    router.register(IdiomChainStartFeature(games, eco, cache.name, rng))
    router.register(UndercoverStartFeature(games, cache.name, llm_for, rng, word_link))
    router.register(UndercoverPrivateWordFeature(games))
    router.register(GuessSongStartFeature(games, llm_for, eco, cache.name, rng))
    router.register(GomokuStartFeature(games, cache.name, base / "gomoku.json" if (db_path or followup_path is not None) else None,
                                       owners=lambda: router.mute.owners(), quiet=lambda room: router.mute.is_muted(room)))   # 双人五子棋（存档 data/gomoku.json）
    router.economy, router.games = eco, games
    router.reporter = DailyReporter(stats, llm, cache.room_name, out_dir=DATA_DIR / "reports", render_image=report_image)
    router.register(DailyReportFeature(router.reporter, allow=llm_allow, deliver=router.deliver, async_mode=report_async))
    from features.amap import AmapTools
    from features import capabilities as caps
    _running = lambda room: (getattr(games.get(room), "title", None) if room else None)
    router.followup.busy = lambda room: games.get(room) is not None if room else False
    from features.play import CommandFeature
    play_feats = [f for f in router.features if isinstance(f, CommandFeature)]
    starters = {f.toggle_key: f for f in router.features
                if getattr(f, "toggle_key", None) in caps.GAMES and type(f).__name__.endswith("StartFeature")}
    from features.voice_reply import VoicePolicy, make_voice_sender, tts_available as voice_tts_available
    import tts_doubao
    voice_policy = VoicePolicy(lambda s: settings.enabled(s, "voice_reply"),
                               lambda s: s in router.allow_targets and s not in router.block_targets,
                               state_path=base / "voice_state.json",
                               tts_ok=lambda: (real_gewe or voice_sender is not None) and voice_tts_available())
    router.voice_sender = voice_sender or (make_voice_sender(DATA_DIR / "voice_tmp") if real_gewe else None)
    # 进群欢迎语音（10-26 主人）：欢迎文字后补一条固定模板语音；开关 welcome_voice，失败不回退文字
    from features.welcome_voice import WelcomeVoice
    member_feat.welcome_voice = WelcomeVoice(
        settings, router.deliver,
        allowed=lambda s: s in router.allow_targets and s not in router.block_targets and router.voice_sender is not None,
        is_muted=lambda room: router.mute.is_muted(room), name_of=cache.name,
        state_path=(base / "welcome_voice_state.json") if (followup_path is not None or db_path) else None)
    router.welcome_voice = member_feat.welcome_voice
    # ---- 第一批：统一工具层 + 联网搜索 + 生图 ----
    from features.daily_quota import DailyCounter
    import minimax_media
    from features.toolbox import Toolbox
    from features.web_search import SearchTools
    from features.image_gen import ImageGen, ImageGenFeature, ImageTools
    _ok = lambda s: s in router.allow_targets
    _blk = lambda s: s in router.block_targets
    counter = DailyCounter(base / "media_quota.json")
    search_tools = SearchTools(lambda s: settings.enabled(s, "web_search"), _ok, _blk, counter,
                               search_fn=search_fn,
                               configured=(lambda: True) if search_fn else (lambda: real_gewe and minimax_media.configured()))
    router.imagegen = ImageGen(lambda s: settings.enabled(s, "image_gen"), _ok, _blk, router.deliver, counter,
                               out_dir=base / "images", gen_fn=image_fn,
                               configured=(lambda: True) if image_fn else (lambda: real_gewe and minimax_media.configured()),
                               members_of=lambda room: list(cache._load(room).values()), async_mode=image_async)
    from features.image_gen import ImageEditFeature, make_person_judge, make_describe
    router.imagegen.person_judge = make_person_judge(llm_for)     # 真人与否拿不准时小模型判（10-23 猪猪侠误判）

    def _recent_image(sess, wxid, sec):
        mi_ = getattr(router, "media_in", None)
        hit = mi_._last_img.get((sess, wxid)) if mi_ is not None else None
        return hit[1] if hit and mi_.clock() - hit[0] <= sec else ""
    router.register(ImageEditFeature(router.imagegen, _recent_image))   # 改图（照着群友的图重画）：在普通生图 / 陪聊之前
    router.register(ImageGenFeature(router.imagegen))
    toolbox = Toolbox([AmapTools(amap_feat, lambda s: settings.enabled(s, "amap")) if amap_tools else None,
                       search_tools, ImageTools(router.imagegen)])
    from features.media_in import MediaIn
    _mio = media_io or {}
    router.media_in = MediaIn(settings.enabled, lambda s: _ok(s) and not _blk(s), counter, router.followup,
                              download_voice=_mio.get("download_voice"), download_image=_mio.get("download_image"),
                              asr_fn=_mio.get("asr"), fetch=_mio.get("fetch"),
                              configured=(lambda: True) if media_io else (lambda: real_gewe and minimax_media.configured()))
    router.imagegen.vision = router.media_in                        # 改图：下载原图
    router.imagegen.describe_fn = make_describe(llm_for)             # 改图：看原图写重画描述
    from features.memory import Memory, MemoryStore, ForgetMeFeature
    router.memory = Memory(MemoryStore(memory_path),               # 只有传了落盘路径（poller / 专门的测试）才启用，其它测试不受影响
                           lambda s: memory_path is not None and settings.enabled(s, "memory") and _ok(s) and not _blk(s),
                           llm_for, async_mode=memory_async)
    from features.learned import LearnedNotes
    router.learned = LearnedNotes(learned_path)                      # 每日学习笔记（只有 poller 传路径才启用）
    from features.article import ArticleReader
    router.articles = ArticleReader(allow=lambda s: settings.enabled(s, "chat_companion") and _ok(s) and not _blk(s)
                                    and (llm_allow is None or llm_allow(s)),
                                    fetch_fn=article_fetch)                  # 读公众号文章（只在 @/引用卡片时）
    from features.news_recall import NewsRecall
    _recall = NewsRecall(Path(context_path).parent / "ai_news_last.json") if context_path else None   # 最近一期快讯（只 poller 落盘时）
    router.chat = ChatCompanionFeature(llm, persona=persona or DEFAULT_PERSONA, name_of=cache.name,
                                       allow=llm_allow, persona_of=personas.custom, early_send=router.deliver,
                                       context=router.context,
                                       capabilities=lambda s: caps.describe(settings.enabled, s, play_feats, running=_running),
                                       game_starter=caps.GameStarter(settings.enabled, starters, running=_running),
                                       voice=voice_policy, vision=router.media_in, memory=router.memory, learned=router.learned,
                                       tools=toolbox, articles=router.articles, news_recall=_recall)
    router.features.insert(next(i for i, f in enumerate(router.features) if type(f).__name__ == "PersonaAdminFeature") + 1,
                           ForgetMeFeature(router.memory))          # 「@小助手 忘掉我」：在游戏/陪聊之前
    router.register(router.chat)
    amap_feat.prefix_ok = lambda sess: sess not in router.room_policy      # 大群只认 @ / 引用，不认前缀
    amap_feat.on_reply = router.chat.remember_turn                   # 地图规则层的问答也进陪聊上下文
    # 主动插话：放最后（passive），被指令/游戏/@陪聊吃掉的消息不会走到这里
    from features.chime import ChimeFeature
    router.chime = ChimeFeature(llm_for, personas.get, name_of=cache.name,
                                blocked=lambda room: room in router.block_targets,
                                state_path=base / "chime_state.json",
                                judge_on=lambda room: settings.enabled(room, "chime_judge"), learned=router.learned,
                                last_bot_ts=lambda room: max((e.get("ts", 0) for e in router.context.entries(room) if e.get("r") == "a"),
                                                             default=0),
                                **(chime_opts or {}))
    router.register(router.chime)
    # 公众号新文推群（「示例公众号」群发 → 总结 + 链接卡片；开关 gzh_promo；poller 里 router.gzh.start() 起调度）
    from features.gzh_promo import GzhPromo, GzhPromoFeature
    _go = dict(gzh_opts or {})
    if "post_link" not in _go and real_gewe:
        import gewe_client as _g
        _go["post_link"] = _g.post_link
    router.gzh = GzhPromo(router, settings, llm_for, persona_for=personas.get, state_path=base / "gzh_promo.json", **_go)
    router.features.insert(0, GzhPromoFeature(router.gzh))             # 被动，放最前（公众号私聊推送，不回复）
    router.stats, router.cache, router.settings, router.personas = stats, cache, settings, personas   # 调试访问
    return router
