"""发送失败（gewe 抛异常）不记进群聊上下文；发成功照常记。"""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ROOM = "20000000001@chatroom"
os.environ["BOT_SEND_ALLOW"] = ROOM
from app import build_router
from router import Reply

calls = {"fail": True}
def sender(reply):
    if calls["fail"]:
        raise RuntimeError("gewe /gewe/v2/api/message/postText failed: ret=500 msg=java.lang.NullPointerException")

r = build_router(fetch_list=lambda x: {}, fetch_info=lambda x: {}, sender=sender, send_enabled=True,
                 db_path=Path(tempfile.mkdtemp()) / "t.db", llm=None)
r.context.path = Path(tempfile.mkdtemp()) / "ctx.json" if hasattr(r.context, "path") else None
before = len(r.context.entries(ROOM))
try:
    r.deliver(Reply(ROOM, "📡 AI 快讯 | 失败的一期"))
    raise SystemExit("应抛异常")
except RuntimeError:
    pass
assert len(r.context.entries(ROOM)) == before, r.context.entries(ROOM)
calls["fail"] = False
assert r.deliver(Reply(ROOM, "📡 AI 快讯 | 成功的一期")) is True
ent = r.context.entries(ROOM)
assert len(ent) == before + 1 and "成功的一期" in ent[-1]["t"]
print("发送失败不记上下文 ✅")
