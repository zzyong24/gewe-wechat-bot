"""装备。开关 equipment（默认关）。参考修仙类插件（nonebot_plugin_xiuxian, MIT）的「部位 + 品阶 + 属性」装备体系，简化为：
  部位：武器 / 防具 / 饰品，各只能穿一件；属性：战力（attack/defense）+ 签到加成%（饰品）。
指令：@小助手 商店 · 购买 <物品名> · 背包 · 装备 <物品名> · 卸下 <物品名> · 战力
购买后若该部位为空自动穿上。战力 = 已穿装备战力之和（+ 基础 10）。
"""
from __future__ import annotations

from dataclasses import dataclass

from features.economy import InsufficientCoins
from features.play import CommandFeature


@dataclass(frozen=True)
class Item:
    name: str
    slot: str          # 武器 / 防具 / 饰品
    tier: str          # 凡品 / 灵品 / 仙品
    price: int
    power: int = 0
    sign_pct: int = 0  # 签到加成 %
    desc: str = ""


SHOP = {i.name: i for i in [
    Item("木剑", "武器", "凡品", 50, power=5, desc="新手村标配"),
    Item("铁剑", "武器", "灵品", 150, power=15, desc="削铁如泥（大概）"),
    Item("青锋剑", "武器", "仙品", 400, power=40, desc="剑气纵横三万里"),
    Item("布衣", "防具", "凡品", 40, power=3, desc="朴素但保暖"),
    Item("锁子甲", "防具", "灵品", 160, power=14, desc="叮叮当当"),
    Item("云纹道袍", "防具", "仙品", 380, power=35, desc="仙气飘飘"),
    Item("幸运草", "饰品", "凡品", 80, power=1, sign_pct=5, desc="签到 +5%"),
    Item("招财玉佩", "饰品", "灵品", 220, power=5, sign_pct=12, desc="签到 +12%"),
    Item("锦鲤护符", "饰品", "仙品", 500, power=10, sign_pct=25, desc="签到 +25%"),
]}
BASE_POWER = 10


class EquipmentFeature(CommandFeature):
    name = "play.equipment"
    toggle_key = "equipment"
    COMMANDS = {"商店": "shop", "购买": "buy", "买装备": "buy", "背包": "bag", "装备": "equip",
                "卸下": "unequip", "战力": "show_power"}

    def _worn(self, room, w) -> list[Item]:
        return [SHOP[i] for i, q, e in self.ctx.eco.items(room, w) if e and i in SHOP]

    def power(self, room, w) -> int:
        return BASE_POWER + sum(i.power for i in self._worn(room, w))

    def sign_bonus_pct(self, room, w) -> int:
        return sum(i.sign_pct for i in self._worn(room, w))

    def shop(self, msg, arg):
        lines = ["🛒 小助手装备铺（@小助手 购买 物品名）"]
        for slot in ("武器", "防具", "饰品"):
            lines.append(f"【{slot}】")
            for i in SHOP.values():
                if i.slot == slot:
                    attr = f"战力+{i.power}" + (f" 签到+{i.sign_pct}%" if i.sign_pct else "")
                    lines.append(f"· {i.name}（{i.tier}）{i.price} 分｜{attr}｜{i.desc}")
        return "\n".join(lines)

    def buy(self, msg, arg):
        item = SHOP.get(arg.strip())
        if not item:
            return f"没有「{arg.strip()}」这件装备，发 @小助手 商店 看看有什么～" if arg.strip() else "用法：@小助手 购买 木剑"
        room, me, eco = msg.room_id, msg.from_wxid, self.ctx.eco
        if any(i == item.name for i, q, e in eco.items(room, me)):
            return f"你已经有「{item.name}」了～"
        with eco.lock:
            try:
                bal = eco.add(room, me, -item.price, f"购买 {item.name}")
            except InsufficientCoins as e:
                return f"积分不够：{item.name} 要 {item.price} 分，你有 {e.have} 分"
            eco.give_item(room, me, item.name)
            auto = not any(w.slot == item.slot for w in self._worn(room, me))
            if auto:
                eco.set_equipped(room, me, item.name, True)
        return f"🎁 购买「{item.name}」成功，余额 {bal} 分" + ("，已自动装备 ✅" if auto else f"（{item.slot}位已有装备，可 @小助手 装备 {item.name} 替换）")

    def bag(self, msg, arg):
        room, me = msg.room_id, msg.from_wxid
        its = self.ctx.eco.items(room, me)
        if not its:
            return "背包空空如也，去 @小助手 商店 逛逛吧～"
        lines = [f"🎒 {self.ctx.name(room, me)} 的背包（战力 {self.power(room, me)}）"]
        for i, q, e in its:
            it = SHOP.get(i)
            lines.append(f"· {i}" + (f"（{it.slot}·{it.tier}）" if it else "") + (" [已装备]" if e else ""))
        return "\n".join(lines)

    def equip(self, msg, arg):
        room, me, eco = msg.room_id, msg.from_wxid, self.ctx.eco
        name = arg.strip()
        if name not in [i for i, q, e in eco.items(room, me)]:
            return f"背包里没有「{name}」～" if name else "用法：@小助手 装备 铁剑"
        item = SHOP[name]
        for w in self._worn(room, me):
            if w.slot == item.slot:
                eco.set_equipped(room, me, w.name, False)
        eco.set_equipped(room, me, name, True)
        return f"⚔️ 已装备「{name}」，当前战力 {self.power(room, me)}"

    def unequip(self, msg, arg):
        room, me = msg.room_id, msg.from_wxid
        if arg.strip() not in [i.name for i in self._worn(room, me)]:
            return "你没穿这件装备～"
        self.ctx.eco.set_equipped(room, me, arg.strip(), False)
        return f"已卸下「{arg.strip()}」，当前战力 {self.power(room, me)}"

    def show_power(self, msg, arg):
        room, me = msg.room_id, msg.from_wxid
        worn = self._worn(room, me)
        return (f"💪 {self.ctx.name(room, me)} 战力 {self.power(room, me)}"
                + (f"\n装备：{'、'.join(i.name for i in worn)}" if worn else "\n还没有装备～")
                + (f"\n签到加成 +{self.sign_bonus_pct(room, me)}%" if self.sign_bonus_pct(room, me) else ""))
