"""买卖群友（雇佣玩法）。开关 slave（默认关）。没有成熟开源实现，基于 economy 的 owner 关系自己设计。
为了轻松不冒犯，对外叫「雇佣 / 打工人 / 老板」，且任何人可随时 @小助手 不想被雇佣 退出（不能被买）。

  @小助手 雇佣 @某人 / 买下 @某人   按对方身价付分：有老板 → 钱给原老板（换老板）；无老板 → 钱给对方本人（签约费）
                                   成交后身价 +20%；刚被雇佣 1 小时内受保护不能被转手；不能雇佣自己的老板
  @小助手 赎身                     按当前身价付给老板，恢复自由身（身价不变）
  @小助手 解雇 @某人                老板主动放人（不退钱）
  @小助手 身价 [@某人]              查看身价 / 老板 / 名下打工人
  @小助手 我的打工人                 查看名下打工人
  @小助手 不想被雇佣 / 可以被雇佣     退出 / 重新加入玩法（退出时自动恢复自由身）
  打工人每次签到，老板额外得 1/10 分成（不扣打工人的）。
"""
from __future__ import annotations

import time

from features.common import at_targets
from features.economy import InsufficientCoins
from features.play import CommandFeature

BASE_PRICE, RAISE, PROTECT_SEC, MAX_STAFF = 100, 1.2, 3600, 10


class HireFeature(CommandFeature):
    name = "play.hire"
    toggle_key = "slave"
    COMMANDS = {"雇佣": "hire", "买下": "hire", "买群友": "hire", "赎身": "redeem", "解雇": "fire", "放生": "fire",
                "身价": "price", "我的打工人": "staff", "不想被雇佣": "opt_out", "可以被雇佣": "opt_in"}

    def _price(self, room, w) -> int:
        return int(self.ctx.eco.get(room, w, "price", BASE_PRICE))

    def _boss(self, room, w):
        r = self.ctx.eco.related(room, "owner", b=w)
        return (r[0][0], r[0][2]) if r else (None, 0)

    def hire(self, msg, arg):
        tg = at_targets(msg)
        room, me, eco, n = msg.room_id, msg.from_wxid, self.ctx.eco, self.ctx.name
        if not tg or tg[0] == msg.self_wxid:
            return "用法：@小助手 雇佣 @某人"
        t = tg[0]
        if eco.get(room, t, "no_hire"):
            return f"{n(room, t)} 设置了不参与雇佣玩法～"
        boss, since = self._boss(room, t)
        if boss == me:
            return f"{n(room, t)} 已经是你的打工人啦～"
        if self._boss(room, me)[0] == t:
            return f"{n(room, t)} 是你的老板，不能反向雇佣哦～（可以先 @小助手 赎身）"
        if boss and time.time() - since < PROTECT_SEC:
            return f"{n(room, t)} 刚换了老板，保护期还剩 {int((PROTECT_SEC - (time.time() - since)) // 60) + 1} 分钟"
        if len(eco.related(room, "owner", a=me)) >= MAX_STAFF:
            return f"你名下已经有 {MAX_STAFF} 个打工人了，忙不过来啦～"
        price = self._price(room, t)
        payee = boss or t
        with eco.lock:
            try:
                eco.transfer(room, me, payee, price, f"雇佣 {t}")
            except InsufficientCoins as e:
                return f"{n(room, t)} 身价 {price} 分，你只有 {e.have} 分～"
            if boss:
                eco.unrelate(room, "owner", boss, t)
            eco.relate(room, "owner", me, t)
            eco.put(room, t, "price", int(price * RAISE))
        who = f"原老板 {n(room, boss)} 收到 {price} 分" if boss else f"{n(room, t)} 拿到签约费 {price} 分"
        return f"🤝 {n(room, me)} 以 {price} 分雇佣了 {n(room, t)}！{who}\n{n(room, t)} 新身价 {int(price * RAISE)} 分"

    def redeem(self, msg, arg):
        room, me, eco, n = msg.room_id, msg.from_wxid, self.ctx.eco, self.ctx.name
        boss, _ = self._boss(room, me)
        if not boss:
            return "你本来就是自由身～"
        price = self._price(room, me)
        with eco.lock:
            try:
                eco.transfer(room, me, boss, price, "赎身")
            except InsufficientCoins as e:
                return f"赎身需要 {price} 分，你只有 {e.have} 分，再攒攒～"
            eco.unrelate(room, "owner", boss, me)
        return f"🕊 {n(room, me)} 支付 {price} 分给 {n(room, boss)}，恢复自由身！"

    def fire(self, msg, arg):
        tg = at_targets(msg)
        room, me, n = msg.room_id, msg.from_wxid, self.ctx.name
        if not tg:
            return "用法：@小助手 解雇 @某人"
        if not self.ctx.eco.unrelate(room, "owner", me, tg[0]):
            return f"{n(room, tg[0])} 不是你的打工人～"
        return f"👋 {n(room, me)} 让 {n(room, tg[0])} 恢复了自由身"

    def price(self, msg, arg):
        tg = at_targets(msg)
        room, n = msg.room_id, self.ctx.name
        t = tg[0] if tg else msg.from_wxid
        boss, _ = self._boss(room, t)
        staff = [b for _, b, _ in self.ctx.eco.related(room, "owner", a=t)]
        s = [f"🏷 {n(room, t)} 身价 {self._price(room, t)} 分", f"老板：{n(room, boss)}" if boss else "自由身"]
        if staff:
            s.append("名下打工人：" + "、".join(n(room, b) for b in staff))
        if self.ctx.eco.get(room, t, "no_hire"):
            s.append("（不参与雇佣玩法）")
        return "\n".join(s)

    def staff(self, msg, arg):
        room, n = msg.room_id, self.ctx.name
        staff = [b for _, b, _ in self.ctx.eco.related(room, "owner", a=msg.from_wxid)]
        return ("👥 名下打工人：" + "、".join(f"{n(room, b)}(身价{self._price(room, b)})" for b in staff)) if staff \
            else "你名下还没有打工人～ @小助手 雇佣 @某人"

    def opt_out(self, msg, arg):
        room, me, eco = msg.room_id, msg.from_wxid, self.ctx.eco
        eco.put(room, me, "no_hire", "1")
        boss, _ = self._boss(room, me)
        if boss:
            eco.unrelate(room, "owner", boss, me)
        return "好的，你已退出雇佣玩法，别人不能再雇佣你" + ("（已恢复自由身）" if boss else "") + "～"

    def opt_in(self, msg, arg):
        self.ctx.eco.put(msg.room_id, msg.from_wxid, "no_hire", None)
        return "欢迎回到雇佣玩法～"
