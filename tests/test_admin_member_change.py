"""功能开关管理指令 + 进退群提醒测试（simulate，不发微信、不调模型、不打 gewe）。"""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import build_router
from features.member_change import parse_sysmsg_template
from features.settings import FeatureSettings
from models import normalize

BOT, ROOM = "wxid_bot_demo", "20000000002@chatroom"
OWNER, ADMIN, USER = "wxid_owner_demo", "wxid_admin", "wxid_user"
MEMBERS = {"chatroomOwner": OWNER, "adminWxid": [ADMIN], "memberList": [
    {"wxid": OWNER, "nickName": "示例公众号"}, {"wxid": ADMIN, "nickName": "管理"},
    {"wxid": USER, "nickName": "路人"}, {"wxid": BOT, "nickName": "小助手"}]}
_mid = 0


def raw(sender, text, at=None, msg_type=1, room=ROOM):
    global _mid; _mid += 1
    src = f"<msgsource><atuserlist><![CDATA[{','.join(at)}]]></atuserlist></msgsource>" if at else "<msgsource/>"
    return {"TypeName": "AddMsg", "Appid": "wx_sim", "Wxid": BOT, "Data": {
        "NewMsgId": 7000 + _mid, "MsgType": msg_type, "FromUserName": {"string": room},
        "ToUserName": {"string": BOT}, "Content": {"string": f"{sender}:\n{text}"}, "CreateTime": 1792493289,
        "MsgSource": src}}


def sysmsg(template, links):
    """links: {name: [(wxid, nick), ...]}"""
    ls = "".join(f'<link name="{n}" type="link_profile"><memberlist>' + "".join(
        f"<member><username><![CDATA[{w}]]></username><nickname><![CDATA[{k}]]></nickname></member>" for w, k in ms)
        + "</memberlist><separator><![CDATA[、]]></separator></link>" for n, ms in links.items())
    xml = (f'<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template type="tmpl_type_profile">'
           f"<plain><![CDATA[]]></plain><template><![CDATA[{template}]]></template><link_list>{ls}</link_list>"
           "</content_template></sysmsgtemplate></sysmsg>")
    return raw(ROOM, xml, msg_type=10002)


class Mock:
    def __init__(self): self.calls = 0
    def chat(self, *a, **k): self.calls += 1; return "陪聊回复"


def last(trace):
    return next((t for t in reversed(trace) if t.get("reply")), None)


def main():
    tmp = Path(tempfile.mkdtemp())
    members = {"v": MEMBERS}
    settings = FeatureSettings(tmp / "settings.json")
    llm = Mock()
    r = build_router(fetch_list=lambda room: members["v"], fetch_info=lambda room: {"nickName": "示例测试群"},
                     db_path=tmp / "s.db", llm=llm, settings=settings, allow_targets={ROOM}, send_enabled=False)
    r.followup = None            # 本测试验证开关/权限，关掉追问窗口（否则被陪聊回复过的人下一句会被接走，见 test_followup）
    d = lambda m: r.dispatch(normalize(m))

    # ---- 默认值 ----
    st = last(d(raw(USER, "@小助手\u2005功能状态", [BOT])))
    print(st["reply"]); assert "⛔ 智能陪聊（默认）" in st["reply"] and "✅ 发言统计（默认）" in st["reply"]
    assert last(d(raw(USER, "@小助手 你好", [BOT]))) is None and llm.calls == 0      # 陪聊默认关，不调模型

    # ---- 权限 ----
    assert "只有群主或管理员" in last(d(raw(USER, "@小助手 开启 陪聊", [BOT])))["reply"]
    assert not settings.enabled(ROOM, "chat_companion")
    assert "已开启本群「智能陪聊」" in last(d(raw(OWNER, "@小助手 开启 陪聊", [BOT])))["reply"]
    assert last(d(raw(USER, "@小助手 你好", [BOT])))["reply"] == "陪聊回复" and llm.calls == 1
    assert "已关闭本群「发言统计」" in last(d(raw(ADMIN, "@小助手 关闭 发言统计", [BOT])))["reply"]
    assert last(d(raw(USER, "今日发言"))) is None                                   # 关掉后触发词无效
    n_before = r.stats.daily(ROOM, "2026-10-20")["total"]; d(raw(USER, "随便说一句"))
    assert r.stats.daily(ROOM, "2026-10-20")["total"] == n_before                    # 也不再计数
    assert "没有叫「打怪」的功能" in last(d(raw(OWNER, "@小助手 开启 打怪", [BOT])))["reply"]
    assert "恢复" in last(d(raw(OWNER, "@小助手 恢复默认 发言统计", [BOT])))["reply"]
    assert settings.enabled(ROOM, "speech_stats")
    assert last(d(raw(USER, "开启 陪聊"))) is None                                   # 不 @ 不当指令
    saved = json.loads((tmp / "settings.json").read_text())
    assert saved["rooms"][ROOM] == {"chat_companion": True} and oct((tmp / "settings.json").stat().st_mode)[-3:] == "600"
    print("功能开关 ✅", saved)

    # ---- 进群：真实样本（小助手被拉进测试群）----
    real = json.loads((Path(__file__).resolve().parent / "fixtures/join_invite_sample.json").read_text(encoding="utf-8"))   # 真实报文脱敏
    t = last(d(real)); print("真实样本 →", t)
    assert t["feature"] == "member_change" and t["reply"] == "欢迎 群友小A 加入本群～（群主 邀请）", t

    # ---- 进群：别人邀请多人 / 扫码 ----
    t = last(d(sysmsg('"$username$"邀请"$names$"加入了群聊', {"username": [(OWNER, "示例公众号")],
                                                            "names": [("wxid_a", "阿离"), ("wxid_b", "Bob")]})))
    assert t["reply"] == "欢迎 阿离、Bob 加入本群～（示例公众号 邀请）", t
    t = last(d(sysmsg('"$adder$"通过扫描"$from$"分享的二维码加入群聊', {"adder": [("wxid_c", "小C")], "from": [(ADMIN, "管理")]})))
    assert t["reply"] == "欢迎 小C 加入本群～（扫 管理 的二维码）", t
    # 只拉了机器人自己 → 不欢迎自己
    assert last(d(sysmsg('"$username$"邀请你加入了群聊', {"username": [(OWNER, "示例公众号")]}))) is None

    # ---- 移出 ----
    t = last(d(sysmsg('你将"$kickoutname$"移出了群聊', {"kickoutname": [("wxid_b", "Bob")]})))
    assert t["reply"] == "Bob 被移出了本群", t
    # 10000 纯文本兜底
    t = last(d(raw(ROOM, '"示例公众号"邀请"大熊"加入了群聊', msg_type=10000)))
    assert t["reply"] == "欢迎 大熊 加入本群～（示例公众号 邀请）", t

    # ---- 主动退群：ModContacts → 成员快照比对 ----
    mod = lambda: {"TypeName": "ModContacts", "Appid": "wx_sim", "Wxid": BOT, "Data": {"UserName": {"string": ROOM}}}
    assert last(d(mod())) is None                                      # 首次建基线
    members["v"] = {**MEMBERS, "memberList": [m for m in MEMBERS["memberList"] if m["wxid"] != USER]}
    t = last(d(mod())); print("退群 →", t)
    assert t["reply"] == "路人 退出了本群"
    assert last(d(mod())) is None                                      # 不重复报

    # ---- 开关关掉 → 不提醒 ----
    settings.set(ROOM, "member_change", False)
    assert last(d(sysmsg('"$username$"邀请"$names$"加入了群聊', {"username": [(OWNER, "主人")], "names": [("wxid_z", "Z")]}))) is None
    assert all(o.to_wxid == ROOM for o in r.outbox)                    # send_enabled=False：全部只在 outbox
    print("进退群提醒 ✅  outbox 条数", len(r.outbox))


if __name__ == "__main__":
    main()
    print("全部通过 ✅")
