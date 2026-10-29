"""功能 7 签到养成单测：签到/连签/断签、积分榜、结婚(娶群友/求婚/离婚)、师徒、装备、雇佣、开关。"""
from datetime import datetime, timedelta
from helpers import Bot, ROOM, BOT, FeatureSettings, raw, normalize
from features.common import TZ


def ts(days_ago):
    return int((datetime.now(TZ) - timedelta(days=days_ago)).replace(hour=10).timestamp())


def test_signin():
    b = Bot()
    r = b.say("wxid_a", "签到", ts=ts(2))
    assert "连签 1 天：+10" in r, r
    assert "今天已经签过" in b.say("wxid_a", "签到", ts=ts(2))
    assert "连签 2 天：+15" in b.say("wxid_a", "签到", ts=ts(1))
    assert "连签 3 天：+20" in b.say("wxid_a", "签到", ts=ts(0))
    acc = b.r.economy.account(ROOM, "wxid_a")
    assert acc.streak == 3 and 45 <= acc.coins <= 60, acc
    b2 = Bot()
    b2.say("wxid_a", "签到", ts=ts(3)); b2.say("wxid_a", "签到", ts=ts(2))
    assert "连签 1 天" in b2.say("wxid_a", "签到", ts=ts(0)), "断签应重置"
    b.say("wxid_b", "签到", ts=ts(0))
    board = b.say("wxid_c", "积分榜")
    assert board.index("阿离") < board.index("Bob"), board
    assert "第 1 名" in b.say("wxid_a", "我的积分")
    print("签到 ✅", r.replace("\n", " | "))


def test_marriage_mentor():
    b = Bot()
    r = b.say("wxid_a", "娶群友")
    assert "今天的 CP 是" in r and "小助手" not in r.split("：")[-1], r
    assert "今天已经有 CP" in b.say("wxid_a", "娶群友")
    # 求婚：积分不够
    assert "彩礼" in b.say("wxid_b", "求婚 @小C", also_at=["wxid_c"])
    b.r.economy.add(ROOM, "wxid_b", 100, "test")
    assert "请在 5 分钟内" in b.say("wxid_b", "求婚 @小C", also_at=["wxid_c"])
    assert b.say("wxid_d", "同意") is None or "喜结连理" not in (b.say("wxid_d", "同意") or "")   # 别人不能代答
    assert "喜结连理" in b.say("wxid_c", "同意")
    assert b.r.economy.spouse(ROOM, "wxid_b") == "wxid_c" and b.r.economy.account(ROOM, "wxid_b").coins == 48
    assert "正式伴侣：小C" in b.say("wxid_b", "现在对象")
    assert "名花有主" in b.say("wxid_d", "求婚 @Bob", also_at=["wxid_b"]) or "已经和" in b.say("wxid_d", "求婚 @Bob", also_at=["wxid_b"])
    assert "和平分手" in b.say("wxid_c", "离婚") and b.r.economy.spouse(ROOM, "wxid_b") is None
    # 拒绝
    b.r.economy.add(ROOM, "wxid_d", 100, "test")
    b.say("wxid_d", "求婚 @小鹿", also_at=["wxid_e"])
    assert "婉拒" in b.say("wxid_e", "拒绝")
    # 师徒
    assert "想拜" in b.say("wxid_a", "拜师 @主人", also_at=["wxid_owner"])
    assert "拜师礼成" in b.say("wxid_owner", "同意")
    assert "想收" in b.say("wxid_a", "收徒 @Bob", also_at=["wxid_b"]) and "拜师礼成" in b.say("wxid_b", "同意")
    assert "辈分乱了" in b.say("wxid_b", "收徒 @主人", also_at=["wxid_owner"])
    s = b.say("wxid_b", "师门")
    assert "师父：阿离（师祖：主人）" in s, s
    before = b.r.economy.account(ROOM, "wxid_a").coins
    r = b.say("wxid_b", "签到")
    assert "师父 阿离 收到孝敬" in r and b.r.economy.account(ROOM, "wxid_a").coins > before
    assert "学成出师" in b.say("wxid_b", "出师")
    print("结婚/师徒 ✅")


def test_equipment_hire():
    b = Bot()
    assert "木剑" in b.say("wxid_a", "商店")
    assert "积分不够" in b.say("wxid_a", "购买 木剑")
    b.r.economy.add(ROOM, "wxid_a", 300, "test")
    assert "已自动装备" in b.say("wxid_a", "购买 木剑")
    assert "已经有" in b.say("wxid_a", "购买 木剑")
    assert "替换" in b.say("wxid_a", "购买 铁剑")
    assert "战力 25" in b.say("wxid_a", "装备 铁剑")
    b.say("wxid_a", "购买 幸运草")
    assert "签到加成 +5%" in b.say("wxid_a", "战力")
    assert "[已装备]" in b.say("wxid_a", "背包")
    # 雇佣
    b.r.economy.add(ROOM, "wxid_b", 500, "test")
    r = b.say("wxid_b", "雇佣 @小C", also_at=["wxid_c"])
    assert "以 100 分雇佣了 小C" in r and "签约费" in r, r
    assert b.r.economy.account(ROOM, "wxid_c").coins == 100
    assert "保护期" in b.say("wxid_a", "雇佣 @小C", also_at=["wxid_c"])
    assert "你的老板" in b.say("wxid_c", "雇佣 @Bob", also_at=["wxid_b"])
    assert "身价 120" in b.say("wxid_d", "身价 @小C", also_at=["wxid_c"])
    assert "赎身需要 120" in b.say("wxid_c", "赎身")
    b.r.economy.add(ROOM, "wxid_c", 50, "test")
    assert "恢复自由身" in b.say("wxid_c", "赎身")
    assert "已退出雇佣玩法" in b.say("wxid_e", "不想被雇佣")
    assert "不参与雇佣" in b.say("wxid_b", "雇佣 @小鹿", also_at=["wxid_e"])
    print("装备/雇佣 ✅")


def test_toggles_default():
    s = FeatureSettings(None)            # 默认值：签到开，其它养成关
    b = Bot(settings=s)
    assert "签到成功" in b.say("wxid_a", "签到")
    for cmd in ("娶群友", "拜师 @主人", "商店", "雇佣 @Bob"):
        assert b.say("wxid_a", cmd, also_at=["wxid_owner"]) in (None, "陪聊"), cmd   # 关闭 → 落到陪聊（默认也关）→ None
    assert "已开启本群「结婚」" in b.say("wxid_owner", "开启 结婚")
    assert "CP" in b.say("wxid_a", "娶群友")
    assert "只有群主或管理员" in b.say("wxid_a", "开启 买卖群友")
    print("开关 ✅")


if __name__ == "__main__":
    test_signin(); test_marriage_mentor(); test_equipment_hire(); test_toggles_default()
    print("全部通过 ✅")
