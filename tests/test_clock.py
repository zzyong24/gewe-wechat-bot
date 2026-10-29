"""当前北京时间注入：陪聊 / 插话 / 欢迎的 system prompt 都带日期、星期、时分（+农历/节假日）。"""
import sys, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.clock import now_line, TZ

s = now_line(dt.datetime(2026, 10, 21, 15, 59, tzinfo=TZ))
assert "2026年10月21日 星期三 15:59" in s and "农历九月十二" in s and "工作日" in s, s
assert "国庆节假期" in now_line(dt.datetime(2026, 10, 1, 9, 0, tzinfo=TZ))
assert "星期日" in now_line(dt.datetime(2026, 10, 25, 9, 0, tzinfo=TZ))
assert "08:30" in now_line(dt.datetime(2026, 10, 21, 0, 30, tzinfo=dt.timezone.utc))      # UTC → 北京时间
print("now_line ✅")

from tests.helpers import Bot
class Rec:
    def __init__(self): self.systems = []
    def chat(self, user, system=None, history=None, **kw):
        self.systems.append(system or ""); return "好"
llm = Rec(); b = Bot(llm=llm)
b.say("wxid_a", "今天几号")
today = dt.datetime.now(TZ)
assert f"{today.year}年{today.month}月{today.day}日" in llm.systems[-1] and "北京时间" in llm.systems[-1]
print("陪聊 system 带当前时间 ✅")

# 插话 / 欢迎的注入断言在 tests/test_chime.py、tests/test_welcome_farewell.py
print("test_clock 全部通过")
