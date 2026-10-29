"""默认人设的三条规则：写进默认人设；语音行按本群开关。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.chat_companion import ChatCompanionFeature, DEFAULT_PERSONA
for k in ("呆逼", "核实年份", "不确定", "语音", "来源和大概时效"):
    assert k in DEFAULT_PERSONA, k
class V:
    def __init__(self, on): self.on = on
    enabled = lambda self, s: self.on
    allowed = lambda self, s: True
    tts_ok = lambda self: True
c = ChatCompanionFeature(lambda: None)
assert "只能打字" in c._voice_line("r@chatroom", True)
c.voice = V(True); assert "可以发语音" in c._voice_line("r@chatroom", True)
c.voice = V(False); assert "只能打字" in c._voice_line("r@chatroom", True)
print("人设三条 + 按群语音开关提示 ✅")
