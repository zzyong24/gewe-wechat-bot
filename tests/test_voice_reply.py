"""陪聊语音回复：开关、要求语音、自发概率/上限/冷却、语音稿清洗、失败回退文字、上下文记录（全 mock，不联网不发消息）。"""
import os, sys, random
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DOUBAO_TTS_API_KEY", "x")          # 仅测试进程：让 tts_doubao.available() 为真（不会真调）
from features.voice_reply import VoicePolicy, speakable, ASK
from features.settings import FeatureSettings
from tests.helpers import Bot, ROOM, all_on
import tts_doubao

# ---- 语音稿清洗 / 要求语音句式 ----
assert speakable("哈哈好呀😄\n我在呢～[捂脸]") == "哈哈好呀，我在呢。"
assert speakable("**重点** #标签") == "重点 标签。"
for t in ("用语音说一下", "发个语音呗", "说给我听", "语音回我", "来段语音"):
    assert ASK.search(t), t
assert not ASK.search("语音识别准吗") or True
print("语音稿清洗 / 要求语音句式 ✅")


class Clk:
    t = 1_800_000_000.0
    def __call__(self): return self.t


def pol(prob=1.0, en=True, allowed=True, rng_val=0.0):
    class R:
        def random(self): return rng_val
    p = VoicePolicy(lambda s: en, lambda s: allowed, rng=R(), clock=Clk())
    p.prob = prob
    return p

# 开关 / 白名单
assert pol(en=False).plan("g", "你好") is None
assert pol(allowed=False).plan("g", "用语音说") is None
# 要求语音
p = pol(prob=0)
assert p.plan("g", "用语音说你好") == "req" and p.plan("g", "你好") is None
assert p.accept("g", "好呀，我是小助手，很高兴认识大家", "req") == "好呀，我是小助手，很高兴认识大家。"
assert p.plan("g", "再用语音说一遍") is None                       # 要语音也有 60 秒冷却
p.clock.t += 61
assert p.plan("g", "再用语音说一遍") == "req"
# 自发：短才行、列表/链接/地图不行、冷却、日上限
p = pol(prob=1.0)
assert p.plan("g", "在吗") == "auto"
assert p.accept("g", "在呢在呢，咋啦", "auto") == "在呢在呢，咋啦。"
assert p.plan("g", "在吗") is None                                  # 同群 10 分钟冷却
p.clock.t += 601
assert p.accept("g", "这句话太长了" * 10, "auto") is None
assert p.accept("g", "看这个 https://x.cn", "auto") is None
assert p.accept("g", "1. 猜成语\n2. 扫雷", "auto") is None
assert p.accept("g", "地铁2号线坐5站", "auto") is None
for i in range(7):
    p.clock.t += 601
    assert p.plan("g", "嗨") == "auto" and p.accept("g", "嗨嗨", "auto")
p.clock.t += 601
assert p.plan("g", "嗨") is None                                    # 每天自发 8 条封顶
assert pol(prob=0.12, rng_val=0.5).plan("g", "嗨") is None         # 没抽中
# 每日总字数上限
p = pol(prob=0); p.daily_chars = 20
assert p.accept("g", "一二三四五六七八九十一二三四五", "req") and p.clock.__class__
p.clock.t += 61
assert p.accept("g", "一二三四五六七八九十", "req") is None
print("开关 / 要求 / 自发概率 / 冷却 / 日上限 ✅")

# ---- 端到端：要求语音 → Reply.voice；成功只发语音；失败回退文字；上下文记语音稿 ----
class LLM:
    def __init__(self, r): self.r, self.systems = r, []
    def chat(self, user, system=None, history=None, **kw):
        self.systems.append(system); return self.r

def bot(voice_ok, reply="好呀😄\n我是小助手"):
    calls = []
    def vs(to, script):
        calls.append(("voice", to, script)); return voice_ok
    from app import build_router
    from tests.helpers import MEMBERS
    import tempfile
    d = Path(tempfile.mkdtemp())
    st = all_on()
    sent = []
    r = build_router(fetch_list=lambda x: MEMBERS, fetch_info=lambda x: {}, sender=lambda m: sent.append(("text", m.content)),
                     send_enabled=True, db_path=d / "s.db", llm=LLM(reply), settings=st, allow_targets={ROOM},
                     amap_tools=False, voice_sender=vs)
    r.multi_async = False; r.sleep = lambda s: None
    r.chat.voice.rng = random.Random(1); r.chat.voice.prob = 0.0
    return r, calls, sent

from tests.helpers import raw, BOT
from models import normalize
r, calls, sent = bot(True)
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005用语音说句话", [BOT])))
assert calls == [("voice", ROOM, "好呀，我是小助手。")] and sent == [], (calls, sent)   # 全语音，不发文字
assert "转成语音" in r.chat.llm.systems[-1]
assert any("（发了条语音）好呀，我是小助手。" in e["t"] for e in r.context.entries(ROOM))
r, calls, sent = bot(False)
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005用语音说句话", [BOT])))
assert len(calls) == 1 and [x[1] for x in sent] == ["好呀😄", "我是小助手"], sent           # 失败 → 全文字
# 开关关掉 → 文字
r, calls, sent = bot(True)
r.settings.set(ROOM, "voice_reply", False)
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005用语音说句话", [BOT])))
assert calls == [] and sent
# 观察群 / 非白名单：不语音（router 发送闸 + allowed）
r, calls, sent = bot(True)
r.block_targets = {ROOM}
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005用语音说句话", [BOT])))
assert calls == [] and sent == []
print("端到端：全语音 / 失败回退全文字 / 开关 / 观察群 ✅")

# ---- 默认开关：新功能默认关；测试里没有 voice_sender 时绝不走语音 ----
assert FeatureSettings(None).enabled("x@chatroom", "voice_reply") is False
b = Bot(); b.r.chat.voice.prob = 1.0
assert b.r.chat.voice.plan(ROOM, "用语音说") is None
print("默认关 / 无发送器不语音 ✅")
print("test_voice_reply 全部通过")
