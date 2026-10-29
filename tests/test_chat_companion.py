"""智能陪聊测试（simulate：不发微信）。
  python3 tests/test_chat_companion.py           # mock 模型；若 env 有 MINIMAX_API_KEY 再做一次真实调用
  python3 tests/test_chat_companion.py --mock    # 只跑 mock
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import build_router                       # noqa: E402
from llm import MiniMaxClient                      # noqa: E402
from models import normalize                       # noqa: E402
from simulate import BOT, ROOM, fake_info, fake_list, group_msg, sim_settings   # noqa: E402


class RecLLM:
    def __init__(self):
        self.calls = []

    def chat(self, user, system=None, history=None, **kw):
        self.calls.append((user, system, list(history or [])))
        return f"mock回复#{len(self.calls)}"


def _settings():
    st = sim_settings()
    st.set("wxid_alice", "chat_companion", True)   # 私聊也要开开关
    return st


def private_msg(sender, text, mid):
    return {"TypeName": "AddMsg", "Appid": "wx_sim", "Wxid": BOT,
            "Data": {"NewMsgId": mid, "MsgType": 1, "FromUserName": {"string": sender},
                     "ToUserName": {"string": BOT}, "Content": {"string": text}, "CreateTime": 1792493289}}


def run(llm):
    db = Path(tempfile.mkdtemp()) / "t.db"
    r = build_router(fetch_list=fake_list, fetch_info=fake_info, send_enabled=False, db_path=db, llm=llm,
                     allow_targets={ROOM, "wxid_alice"}, settings=_settings())
    traces = {}
    cases = {
        "at_me": group_msg("wxid_carol", "@小助手\u2005你好", 10, 0, [BOT]),
        "at_me_2": group_msg("wxid_carol", "@小助手 你还记得我刚才说了什么吗", 10, 1, [BOT]),
        "no_at": group_msg("wxid_alice", "大家好", 10, 2),
        "at_all": group_msg("wxid_owner", "@所有人 开会", 10, 3, ["notify@all"]),
        "at_members": group_msg("wxid_bob", "@小助手 成员列表", 10, 4, [BOT]),
        "private": private_msg("wxid_alice", "在吗", 5001),
        "weixin": private_msg("weixin", "系统通知", 5002),
    }
    for k, raw in cases.items():
        traces[k] = r.dispatch(normalize(raw))
    return r, traces


def feats(trace):
    return [t["feature"] for t in trace if t.get("reply")]


def test_mock():
    llm = RecLLM()
    r, t = run(llm)
    assert feats(t["at_me"]) == ["chat_companion"], t["at_me"]
    assert t["at_me"][-1]["sent"] is False                    # simulate：不发
    assert llm.calls[0][0] == "[小C]：你好"                    # 去掉了 @小助手，带群昵称
    assert len(llm.calls[1][2]) == 2                           # 第二轮带上一轮上下文
    assert feats(t["no_at"]) == [] and feats(t["at_all"]) == [] and feats(t["weixin"]) == []
    assert feats(t["at_members"]) == ["group_members"]         # 触发词功能优先
    assert feats(t["private"]) == ["chat_companion"] and llm.calls[-1][0] == "在吗"
    assert all(o.to_wxid in (ROOM, "wxid_alice") for o in r.outbox)
    print("mock 测试通过 ✅  拟回复：", [(o.to_wxid, o.content) for o in r.outbox if o.content.startswith("mock")])


def test_real():
    if not MiniMaxClient.available():
        print("跳过真实调用：MINIMAX_API_KEY 未设置")
        return
    client = MiniMaxClient()
    db = Path(tempfile.mkdtemp()) / "t.db"
    r = build_router(fetch_list=fake_list, fetch_info=fake_info, send_enabled=False, db_path=db, llm=client,
                     allow_targets={ROOM}, settings=sim_settings())
    tr = r.dispatch(normalize(group_msg("wxid_carol", "@小助手\u2005你好", 10, 0, [BOT])))
    reply = tr[-1].get("reply")
    print("trace:", tr)
    assert tr[-1]["feature"] == "chat_companion" and reply, "真实调用未得到回复（看日志 WARNING）"
    print(f"真实调用通过 ✅ model={client.model} base={client.last_base_url} usage={client.last_usage}")
    print("拟回复（未发送）：", reply)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    test_mock()
    if "--mock" not in sys.argv:
        test_real()
