"""结婚 / 师徒 / 同意拒绝。开关：marriage（默认关）、mentor（默认关）。

结婚（参考 KarisAya/nonebot_plugin_groupmate_waifu，MIT 的「每日娶群友」玩法；正式结婚流程自己设计）：
  @小助手 娶群友            每日随机抽一位群友当「今日CP」（当日有效，每天一次）
  @小助手 娶 @某人          指定：40% 成功 / 20% 被拒 / 40% 随机换一个人（同原插件概率思路）
  @小助手 求婚 @某人        正式求婚（需双方都单身），对方 5 分钟内 @小助手 同意 / 拒绝；成婚扣求婚方 52 分彩礼
  @小助手 现在对象 / 我的对象   查看正式伴侣 + 今日CP
  @小助手 离婚              解除正式婚姻（冷静费 20 分）
师徒：
  @小助手 拜师 @某人 / 收徒 @某人   对方 @小助手 同意 后成立；一人只有一个师父、一个师父最多 5 个徒弟、不能成环
  @小助手 师门 / 师承       查看自己的师父和徒弟
  @小助手 出师             徒弟主动出师；@小助手 逐出师门 @某人  师父解除
  徒弟每次签到，师父额外得到 1/5 的孝敬（不扣徒弟的）。
"""
from __future__ import annotations

from datetime import datetime

from features.common import TZ, at_targets
from features.economy import InsufficientCoins, Pending
from features.play import CommandFeature

BRIDE_PRICE, DIVORCE_FEE, MAX_APPRENTICES = 52, 20, 5


def _day(msg):
    return datetime.fromtimestamp(msg.create_time, TZ).strftime("%Y-%m-%d")


class MarriageFeature(CommandFeature):
    name = "play.marriage"
    toggle_key = "marriage"
    COMMANDS = {"娶群友": "waifu", "抽老婆": "waifu", "娶": "waifu_target", "求婚": "propose",
                "现在对象": "show", "我的对象": "show", "离婚": "divorce"}

    def __init__(self, ctx, pending: Pending):
        super().__init__(ctx)
        self.pending = pending

    def _today_cp(self, room, wxid, day):
        v = self.ctx.eco.get(room, wxid, "cp")
        if v and v.startswith(day + "|"):
            return v.split("|", 1)[1]
        return None

    def _set_cp(self, room, a, b, day):
        self.ctx.eco.put(room, a, "cp", f"{day}|{b}")
        self.ctx.eco.put(room, b, "cp", f"{day}|{a}")

    def _pool(self, msg, day):
        room, me = msg.room_id, msg.from_wxid
        return [w for w in self.ctx.members_of(room)
                if w not in (me, msg.self_wxid) and not self._today_cp(room, w, day)]

    def waifu(self, msg, arg):
        room, me, day = msg.room_id, msg.from_wxid, _day(msg)
        n = self.ctx.name
        cp = self._today_cp(room, me, day)
        if cp:
            return f"{n(room, me)} 今天已经有 CP 啦：{n(room, cp)}，明天再来吧～"
        pool = self._pool(msg, day)
        if not pool:
            return "群里的小伙伴今天都被抽走啦，明天早点来～"
        pick = self.ctx.rng.choice(pool)
        self._set_cp(room, me, pick, day)
        return f"💘 {n(room, me)} 今天的 CP 是：{n(room, pick)}！\n（今日有效，明天重新抽）"

    def waifu_target(self, msg, arg):
        tg = at_targets(msg)
        if not tg:
            return None if arg.startswith("群友") else "要娶谁呀？用法：@小助手 娶 @某人（或 @小助手 娶群友）"
        room, me, day, n = msg.room_id, msg.from_wxid, _day(msg), self.ctx.name
        if self._today_cp(room, me, day):
            return f"{n(room, me)} 今天已经有 CP 啦：{n(room, self._today_cp(room, me, day))}"
        t = tg[0]
        if t == msg.self_wxid:
            return "小助手只陪大家聊天，不参与抽 CP 哦～"
        if self._today_cp(room, t, day):
            return f"{n(room, t)} 今天已经被抽走啦（CP：{n(room, self._today_cp(room, t, day))}）"
        r = self.ctx.rng.randint(1, 100)
        if r <= 40:
            self._set_cp(room, me, t, day)
            return f"💞 成功！{n(room, me)} 和 {n(room, t)} 成为今日 CP～"
        if r <= 60:
            self.ctx.eco.put(room, me, "cp", f"{day}|")      # 今天机会用掉了
            return f"💔 {n(room, t)} 婉拒了你，今天就先单着吧～"
        pool = [w for w in self._pool(msg, day) if w != t]
        if not pool:
            return f"💔 没抽到 {n(room, t)}，群里也没别人了…"
        pick = self.ctx.rng.choice(pool)
        self._set_cp(room, me, pick, day)
        return f"🎲 月老手滑了！{n(room, me)} 的今日 CP 变成了：{n(room, pick)}"

    def propose(self, msg, arg):
        tg = at_targets(msg)
        room, me, n, eco = msg.room_id, msg.from_wxid, self.ctx.name, self.ctx.eco
        if not tg or tg[0] == msg.self_wxid:
            return "用法：@小助手 求婚 @某人"
        t = tg[0]
        if eco.spouse(room, me):
            return f"你已经和 {n(room, eco.spouse(room, me))} 结婚了，要专一哦～"
        if eco.spouse(room, t):
            return f"{n(room, t)} 已经名花有主啦～"
        if eco.account(room, me).coins < BRIDE_PRICE:
            return f"求婚需要准备 {BRIDE_PRICE} 分彩礼，你现在只有 {eco.account(room, me).coins} 分，先去签到攒攒吧～"
        self.pending.put(room, t, "marriage", me)
        return (f"💍 {n(room, me)} 向 {n(room, t)} 求婚啦！\n"
                f"{n(room, t)} 请在 5 分钟内回复「@小助手 同意」或「@小助手 拒绝」")

    def accept(self, room, src, me):
        eco, n = self.ctx.eco, self.ctx.name
        if eco.spouse(room, src) or eco.spouse(room, me):
            return "有一方已经结婚了，求婚作废～"
        try:
            eco.add(room, src, -BRIDE_PRICE, f"彩礼 → {me}")
        except InsufficientCoins:
            return f"{n(room, src)} 的彩礼不够了，求婚作废～"
        eco.relate(room, "spouse", src, me)
        return f"🎉 恭喜 {n(room, src)} ❤ {n(room, me)} 喜结连理！（彩礼 {BRIDE_PRICE} 分已扣）"

    def show(self, msg, arg):
        room, me, n, eco = msg.room_id, msg.from_wxid, self.ctx.name, self.ctx.eco
        sp, cp = eco.spouse(room, me), self._today_cp(room, me, _day(msg))
        lines = [f"💑 {n(room, me)}"]
        lines.append(f"正式伴侣：{n(room, sp)}" if sp else "正式伴侣：暂无（@小助手 求婚 @某人）")
        lines.append(f"今日CP：{n(room, cp)}" if cp else "今日CP：暂无（@小助手 娶群友）")
        return "\n".join(lines)

    def divorce(self, msg, arg):
        room, me, n, eco = msg.room_id, msg.from_wxid, self.ctx.name, self.ctx.eco
        sp = eco.spouse(room, me)
        if not sp:
            return "你还没结婚呢～"
        try:
            eco.add(room, me, -DIVORCE_FEE, "离婚冷静费")
            fee = f"（冷静费 {DIVORCE_FEE} 分）"
        except InsufficientCoins:
            fee = "（积分不够付冷静费，这次就免了）"
        eco.unrelate(room, "spouse", me, sp)
        return f"📄 {n(room, me)} 和 {n(room, sp)} 和平分手了{fee}，祝各自安好～"


class MentorFeature(CommandFeature):
    name = "play.mentor"
    toggle_key = "mentor"
    COMMANDS = {"拜师": "ask", "收徒": "recruit", "师门": "show", "师承": "show", "出师": "graduate", "逐出师门": "expel"}

    def __init__(self, ctx, pending: Pending):
        super().__init__(ctx)
        self.pending = pending

    def _master(self, room, w):
        r = self.ctx.eco.related(room, "mentor", b=w)
        return r[0][0] if r else None

    def _check(self, room, master, apprentice):
        eco, n = self.ctx.eco, self.ctx.name
        if master == apprentice:
            return "不能拜自己为师哦～"
        if self._master(room, apprentice):
            return f"{n(room, apprentice)} 已经有师父了（{n(room, self._master(room, apprentice))}）"
        if len(eco.related(room, "mentor", a=master)) >= MAX_APPRENTICES:
            return f"{n(room, master)} 的徒弟已满 {MAX_APPRENTICES} 人"
        w = master                                   # 防成环：师父的师父链里不能有徒弟
        for _ in range(50):
            w = self._master(room, w)
            if not w:
                break
            if w == apprentice:
                return "辈分乱了！不能拜自己的徒子徒孙为师～"
        return None

    def ask(self, msg, arg):
        tg = at_targets(msg)
        if not tg or tg[0] == msg.self_wxid:
            return "用法：@小助手 拜师 @某人"
        room, me, t, n = msg.room_id, msg.from_wxid, tg[0], self.ctx.name
        err = self._check(room, t, me)
        if err:
            return err
        self.pending.put(room, t, "mentor", me, {"master": t, "apprentice": me})
        return f"🙇 {n(room, me)} 想拜 {n(room, t)} 为师！\n{n(room, t)} 请 5 分钟内回复「@小助手 同意」或「@小助手 拒绝」"

    def recruit(self, msg, arg):
        tg = at_targets(msg)
        if not tg or tg[0] == msg.self_wxid:
            return "用法：@小助手 收徒 @某人"
        room, me, t, n = msg.room_id, msg.from_wxid, tg[0], self.ctx.name
        err = self._check(room, me, t)
        if err:
            return err
        self.pending.put(room, t, "mentor", me, {"master": me, "apprentice": t})
        return f"📜 {n(room, me)} 想收 {n(room, t)} 为徒！\n{n(room, t)} 请 5 分钟内回复「@小助手 同意」或「@小助手 拒绝」"

    def accept(self, room, src, me, extra):
        err = self._check(room, extra["master"], extra["apprentice"])
        if err:
            return err
        self.ctx.eco.relate(room, "mentor", extra["master"], extra["apprentice"])
        n = self.ctx.name
        return f"🎊 拜师礼成！{n(room, extra['master'])} 收 {n(room, extra['apprentice'])} 为徒～徒弟签到时师父有孝敬分"

    def show(self, msg, arg):
        room, me, n, eco = msg.room_id, msg.from_wxid, self.ctx.name, self.ctx.eco
        m = self._master(room, me)
        aps = [b for _, b, _ in eco.related(room, "mentor", a=me)]
        gm = self._master(room, m) if m else None
        lines = [f"🏯 {n(room, me)} 的师门"]
        lines.append(f"师父：{n(room, m)}" + (f"（师祖：{n(room, gm)}）" if gm else "") if m else "师父：暂无")
        lines.append("徒弟：" + ("、".join(n(room, b) for b in aps) if aps else "暂无"))
        return "\n".join(lines)

    def graduate(self, msg, arg):
        room, me, n = msg.room_id, msg.from_wxid, self.ctx.name
        m = self._master(room, me)
        if not m:
            return "你还没有师父～"
        self.ctx.eco.unrelate(room, "mentor", m, me)
        return f"🎓 {n(room, me)} 学成出师，感谢 {n(room, m)} 的教导！"

    def expel(self, msg, arg):
        tg = at_targets(msg)
        room, me, n = msg.room_id, msg.from_wxid, self.ctx.name
        if not tg:
            return "用法：@小助手 逐出师门 @某人"
        if not self.ctx.eco.unrelate(room, "mentor", me, tg[0]):
            return f"{n(room, tg[0])} 不是你的徒弟～"
        return f"🚪 {n(room, me)} 将 {n(room, tg[0])} 逐出师门"


class RespondFeature(CommandFeature):
    """@小助手 同意 / 拒绝：处理发给自己的待确认请求（求婚、拜师、收徒）。对应玩法开关关闭时不处理。"""
    name = "play.respond"
    toggle_key = None
    COMMANDS = {"同意": "yes", "接受": "yes", "我愿意": "yes", "拒绝": "no"}

    def __init__(self, ctx, pending: Pending, handlers: dict):
        super().__init__(ctx)
        self.pending, self.handlers = pending, handlers      # kind → feature（有 accept 方法）

    def match(self, msg) -> bool:
        if not super().match(msg):
            return False
        p = self.pending.peek(msg.room_id, msg.from_wxid)
        return bool(p) and self.ctx.enabled(msg.room_id, p["kind"])

    def yes(self, msg, arg):
        p = self.pending.pop(msg.room_id, msg.from_wxid)
        f = self.handlers[p["kind"]]
        return f.accept(msg.room_id, p["src"], msg.from_wxid, p["extra"]) if p["extra"] else \
            f.accept(msg.room_id, p["src"], msg.from_wxid)

    def no(self, msg, arg):
        p = self.pending.pop(msg.room_id, msg.from_wxid)
        what = {"marriage": "求婚", "mentor": "师徒请求"}.get(p["kind"], "请求")
        return f"{self.ctx.name(msg.room_id, msg.from_wxid)} 婉拒了 {self.ctx.name(msg.room_id, p['src'])} 的{what}～"
