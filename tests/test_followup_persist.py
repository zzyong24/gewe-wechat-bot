"""追问窗口落盘：重启后还在；过期的不恢复。replay_once：按 id 从归档补处理、读完即删。"""
import sys, json, os, tempfile, time, logging
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.followup import Followup
import poller

d = Path(tempfile.mkdtemp()); fp = d / "followup.json"
R, U, B = "1@chatroom", "wxid_u", "wxid_bot"
def m(text, at=()):
    return NS(event="AddMsg", is_self=False, is_group=True, msg_type=1, content=text, room_id=R, from_wxid=U,
              self_wxid=B, at_list=list(at), quote_from="", addressed="", create_time=time.time())
f = Followup(sec=180, max_n=4, path=str(fp), owners=lambda: set())
f.note_reply(m("问", [B]), "chat_companion")
assert oct(fp.stat().st_mode & 0o777) == "0o600"
f2 = Followup(sec=180, max_n=4, path=str(fp), owners=lambda: set())       # 模拟重启
x = m("那国家有啥政策扶持"); f2.prepare(x)
assert x.addressed == "followup" and B in x.at_list
assert json.loads(fp.read_text())[f"{R}|{U}"]["used"] == 1
fp.write_text(json.dumps({f"{R}|{U}": {"until": time.time() - 5, "used": 0}}))
f3 = Followup(sec=180, path=str(fp), owners=lambda: set()); y = m("再问"); f3.prepare(y)
assert y.addressed != "followup"
assert Followup(sec=180).path is None                                     # 不传路径不落盘

# replay_once
raw = d / "inbox"; raw.mkdir()
payload = {"TypeName": "AddMsg", "Appid": "a", "Wxid": B, "Data": {"NewMsgId": 9, "MsgType": 1,
           "FromUserName": {"string": R}, "ToUserName": {"string": B}, "Content": {"string": f"{U}:\n国家有啥政策扶持"},
           "CreateTime": int(time.time()) - 600, "MsgSource": "<msgsource/>"}}
(raw / time.strftime("raw-%Y%m%d.jsonl")).write_text(json.dumps({"id": 771, "payload": payload}) + "\n" +
                                                     json.dumps({"id": 772, "payload": payload}) + "\n")
got = []
poller.submit = lambda router, msg, log, queues=None, stale=False: got.append(msg)
rf = d / "replay_once.json"; rf.write_text(json.dumps({"ids": [771], "followup": True}))
assert poller.replay_once(None, logging.getLogger("t"), None, path=rf, raw_dir=raw) == 1
assert not rf.exists() and len(got) == 1 and B in got[0].at_list and got[0].addressed == "followup"
assert poller.replay_once(None, logging.getLogger("t"), None, path=rf, raw_dir=raw) == 0
print("追问窗口落盘/重启恢复/过期不恢复 + replay_once 补处理 ✅")
