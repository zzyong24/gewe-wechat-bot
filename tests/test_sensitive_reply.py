"""MiniMax 敏感拒答 → 固定短回（simulate，不发微信）。python3 tests/test_sensitive_reply.py"""
from __future__ import annotations
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import build_router                       # noqa: E402
from models import normalize                       # noqa: E402
from simulate import BOT, ROOM, fake_info, fake_list, group_msg, sim_settings   # noqa: E402
import features.chat_companion as cc               # noqa: E402

SENS = RuntimeError("MiniMax 错误 http=422 code=0: input new_sensitive (1026)")


class FailLLM:
    def __init__(self, err): self.err = err
    def chat(self, *a, **kw): raise self.err


def _router(llm):
    return build_router(fetch_list=fake_list, fetch_info=fake_info, send_enabled=False,
                        db_path=Path(tempfile.mkdtemp()) / "t.db", llm=llm,
                        allow_targets={ROOM}, settings=sim_settings())


def _replies(trace):
    return [t["reply"] for t in trace if t.get("feature") == "chat_companion" and t.get("reply")]


def test_detect():
    assert cc.is_sensitive_error(SENS)
    assert cc.is_sensitive_error(Exception("output sensitive"))
    assert cc.is_sensitive_error(Exception("base_resp status_code: 1026"))
    for e in [TimeoutError("read timed out"), Exception("http=502 bad gateway"),
              Exception("http=422 invalid params"), Exception("port 10260 refused")]:
        assert not cc.is_sensitive_error(e), e


def test_reply_and_cooldown():
    cc._SENSITIVE_LAST.clear()
    r = _router(FailLLM(SENS))
    t1 = r.dispatch(normalize(group_msg("wxid_carol", "@小助手\u2005敏感话", 10, 0, [BOT])))
    assert _replies(t1) == [cc.SENSITIVE_REPLY], t1
    t2 = r.dispatch(normalize(group_msg("wxid_carol", "@小助手\u2005再问", 10, 1, [BOT])))
    assert _replies(t2) == [], t2                        # 60s 内只回一次
    cc._SENSITIVE_LAST[next(iter(cc._SENSITIVE_LAST))] -= 61
    t3 = r.dispatch(normalize(group_msg("wxid_carol", "@小助手\u2005又问", 10, 2, [BOT])))
    assert _replies(t3) == [cc.SENSITIVE_REPLY]


def test_timeout_unchanged():
    cc._SENSITIVE_LAST.clear()
    r = _router(FailLLM(TimeoutError("timed out")))
    t = r.dispatch(normalize(group_msg("wxid_carol", "@小助手\u2005你好", 10, 0, [BOT])))
    assert _replies(t) == []


if __name__ == "__main__":
    test_detect(); test_reply_and_cooldown(); test_timeout_unchanged(); print("敏感拒答测试通过 ✅")
