"""本地模拟测试台：不连真实消息、不发送任何消息（dry-run）。
构造 gewe v1 格式的假群消息 → normalize → router，验证全链路并打印日报。"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app import build_router
from models import normalize

TZ = ZoneInfo("Asia/Shanghai")
BOT = "wxid_bot_sim"            # 模拟用，避免和真实账号混淆
ROOM = "10000000001@chatroom"   # 假群
FAKE_MEMBERS = {
    "memberList": [
        {"wxid": "wxid_alice", "nickName": "Alice", "displayName": "阿离", "inviterUserName": "wxid_owner", "memberFlag": 0},
        {"wxid": "wxid_bob", "nickName": "Bob", "displayName": None, "inviterUserName": "wxid_owner", "memberFlag": 0},
        {"wxid": "wxid_carol", "nickName": "Carol", "displayName": "小C", "inviterUserName": "wxid_alice", "memberFlag": 0},
        {"wxid": "wxid_owner", "nickName": "群主大人", "displayName": "", "inviterUserName": None, "memberFlag": 0},
        {"wxid": BOT, "nickName": "小助手", "displayName": "", "inviterUserName": "wxid_owner", "memberFlag": 0},
    ],
    "chatroomOwner": "wxid_owner", "adminWxid": ["wxid_alice"],
}
fake_list = lambda room: FAKE_MEMBERS
fake_info = lambda room: {"chatroomId": room, "nickName": "主人测试群"}

_seq = 0


def sim_settings():
    """模拟用内存开关：假群开陪聊（默认关）。不读写 data/settings.json。"""
    from features.settings import FeatureSettings
    s = FeatureSettings(None)
    s.set(ROOM, "chat_companion", True)
    return s


class EchoLLM:
    """离线假模型：不联网，回显用户消息，用于验证陪聊链路。"""
    def chat(self, user, system=None, history=None, **kw):
        return f"(mock) 小助手收到：{user}"


def group_msg(sender, text, hh, mm, at=None, msg_type=1):
    global _seq
    _seq += 1
    ts = int(datetime.now(TZ).replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp())
    src = f"<msgsource><atuserlist><![CDATA[{','.join(at)}]]></atuserlist></msgsource>" if at else "<msgsource/>"
    return {"TypeName": "AddMsg", "Appid": "wx_sim", "Wxid": BOT,
            "Data": {"MsgId": _seq, "NewMsgId": 900000 + _seq, "MsgType": msg_type,
                     "FromUserName": {"string": ROOM}, "ToUserName": {"string": BOT},
                     "Content": {"string": f"{sender}:\n{text}"}, "CreateTime": ts, "MsgSource": src}}


def main():
    db = Path(tempfile.mkdtemp()) / "sim_stats.db"
    router = build_router(fetch_list=fake_list, fetch_info=fake_info, sender=None, send_enabled=False, db_path=db,
                          llm=EchoLLM(), allow_targets={ROOM}, settings=sim_settings())
    script = [
        ("wxid_alice", "早上好～", 7, 5, None),
        ("wxid_bob", "早", 7, 10, None),
        ("wxid_alice", "今天开会吗", 9, 30, None),
        ("wxid_carol", "@小助手 你好呀", 10, 2, [BOT]),
        ("wxid_alice", "中午吃啥", 12, 15, None),
        ("wxid_bob", "[图片]", 12, 16, None, ),
        ("wxid_alice", "哈哈哈", 15, 40, None),
        ("wxid_owner", "@所有人 今晚8点开会", 19, 0, ["notify@all"]),
        ("wxid_carol", "收到", 19, 1, None),
        ("wxid_alice", "收到+1", 19, 2, None),
        ("wxid_bob", "成员列表", 21, 0, None),
        ("wxid_carol", "今日发言", 23, 30, None),
    ]
    print("=== 逐条路由 ===")
    for s, t, h, m, at in script:
        raw = group_msg(s, t, h, m, at)
        msg = normalize(raw)
        trace = router.dispatch(msg)
        flag = " [@我]" if msg.mentions_me() else ""
        print(f"{h:02d}:{m:02d} {msg.from_wxid:<11} {msg.content!r:<16}{flag} -> "
              + ", ".join(f"{x['feature']}{'(回复)' if x.get('reply') else ''}" for x in trace))
    # 去重验证
    dup = normalize(group_msg("wxid_bob", "x", 22, 0))
    dup.msg_id = "900001"
    print("重复消息 ->", router.dispatch(dup))

    print("\n=== dry-run 待发回复（未发送）===")
    for r in router.outbox:
        print(f"--- to {r.to_wxid} ---\n{r.content}\n")

    d = router.stats.daily(ROOM, datetime.now(TZ).strftime("%Y-%m-%d"))
    print("=== 统计原始数据 ===")
    print(json.dumps({k: d[k] for k in ("total", "speakers", "members", "slots", "peak_hour")}, ensure_ascii=False))
    assert d["total"] == 12 and d["members"][0]["wxid"] == "wxid_alice" and d["members"][0]["count"] == 5
    assert len(router.outbox) == 3   # 成员列表 + 日报 + @小助手 陪聊
    assert any(r.content.startswith('(mock) 小助手收到：[小C]：你好呀') for r in router.outbox)
    print("\n全部断言通过 ✅")


if __name__ == "__main__":
    main()
