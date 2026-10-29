"""签到 / 积分榜 / 我的积分。开关 sign_in（默认开）。
规则：每日（Asia/Shanghai）一次；第 n 天连签得 10+5*(n-1)，第 7 天起封顶 40；另有 0~5 随机幸运分；
断签（昨天没签）连签重置为 1。装备「签到加成」与师徒孝敬、老板分成在这里结算。"""
from __future__ import annotations

from datetime import datetime, timedelta

from features.common import TZ
from features.play import CommandFeature

BASE, STEP, CAP_DAYS = 10, 5, 7
MEDALS = ["🥇", "🥈", "🥉"]


def streak_reward(streak: int) -> int:
    return BASE + STEP * (min(streak, CAP_DAYS) - 1)


class SignInFeature(CommandFeature):
    name = "play.sign_in"
    toggle_key = "sign_in"
    COMMANDS = {"签到": "sign", "打卡": "sign", "积分榜": "board", "排行榜": "board", "我的积分": "mine", "积分": "mine"}

    def __init__(self, ctx, equipment=None):
        super().__init__(ctx)
        self.equipment = equipment     # EquipmentFeature：提供 sign_bonus_pct / power

    def sign(self, msg, arg, today: str | None = None):
        eco, room, me = self.ctx.eco, msg.room_id, msg.from_wxid
        day = today or datetime.fromtimestamp(msg.create_time, TZ).strftime("%Y-%m-%d")
        with eco.lock:
            acc = eco.account(room, me)
            name = self.ctx.name(room, me)
            if acc.last_day == day:
                return f"{name} 今天已经签过啦～连签 {acc.streak} 天，余额 {acc.coins} 分"
            yday = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
            streak = acc.streak + 1 if acc.last_day == yday else 1
            base = streak_reward(streak)
            luck = self.ctx.rng.randint(0, 5)
            pct = self.equipment.sign_bonus_pct(room, me) if self.equipment and self.ctx.enabled(room, "equipment") else 0
            bonus = round((base + luck) * pct / 100)
            gain = base + luck + bonus
            eco.set_sign(room, me, streak, day)
            bal = eco.add(room, me, gain, f"签到 第{streak}天")
            extra = []
            if self.ctx.enabled(room, "mentor"):
                for master, _, _ in eco.related(room, "mentor", b=me):
                    tip = max(1, gain // 5)
                    eco.add(room, master, tip, f"徒弟 {me} 签到孝敬")
                    extra.append(f"师父 {self.ctx.name(room, master)} 收到孝敬 {tip} 分")
            if self.ctx.enabled(room, "slave"):
                for boss, _, _ in eco.related(room, "owner", b=me):
                    cut = max(1, gain // 10)
                    eco.add(room, boss, cut, f"打工人 {me} 签到分成")
                    extra.append(f"老板 {self.ctx.name(room, boss)} 分成 {cut} 分（不扣你的）")
        lines = [f"✅ {name} 签到成功！",
                 f"连签 {streak} 天：+{base}" + (f"，幸运 +{luck}" if luck else "") + (f"，装备加成 +{bonus}" if bonus else ""),
                 f"本次共 +{gain}，余额 {bal} 分"]
        if streak < CAP_DAYS:
            lines.append(f"明天继续签到可得 {streak_reward(streak + 1)}+ 分")
        return "\n".join(lines + extra)

    def board(self, msg, arg):
        rows = self.ctx.eco.top(msg.room_id, 10)
        if not rows:
            return "还没有人签到过，快来 @小助手 签到 吧～"
        lines = ["🏆 本群积分榜"]
        for i, (w, c) in enumerate(rows):
            lines.append(f"{MEDALS[i] if i < 3 else f'{i + 1}.'} {self.ctx.name(msg.room_id, w)}  {c} 分")
        return "\n".join(lines)

    def mine(self, msg, arg):
        eco, room, me = self.ctx.eco, msg.room_id, msg.from_wxid
        a = eco.account(room, me)
        s = [f"💰 {self.ctx.name(room, me)}：{a.coins} 分（第 {eco.rank(room, me)} 名）",
             f"连签 {a.streak} 天 · 累计签到 {a.total_days} 天"]
        if self.equipment and self.ctx.enabled(room, "equipment"):
            s.append(f"战力 {self.equipment.power(room, me)}")
        return "\n".join(s)
