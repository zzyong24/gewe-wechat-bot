"""双人五子棋：开局、加入、轮次、非玩家干扰、非法落子、横竖斜五连、认输、超时、静音、重启恢复（全部 simulate）。"""
import random, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from helpers import Bot, BOT, ROOM, MEMBERS, all_on, offline_amap, Echo
from app import build_router
from features.persona import PersonaStore
from features.games.gomoku import parse_move, render, five, SIZE, BLACK, WHITE, LABELS
from features.settings import FeatureSettings
from models import normalize
from helpers import raw

A, B, C, OWNER = "wxid_a", "wxid_b", "wxid_c", "wxid_owner_demo"

# ---- 纯函数 ----
assert parse_move("55") == (5, 5) and parse_move("5 5") == (5, 5) and parse_move("5,5") == (5, 5)
assert parse_move("5，6") == (5, 6) and parse_move("12") == (1, 2) and parse_move("5,10") == (5, 10)
assert parse_move("555") is None and parse_move("你好") is None and parse_move("5") is None
b = [[0] * SIZE for _ in range(SIZE)]
for i in range(5): b[2][i] = 1                       # 横
assert five(b, 2, 4)
b = [[0] * SIZE for _ in range(SIZE)]
for i in range(5): b[i][7] = 2                       # 竖
assert five(b, 4, 7)
b = [[0] * SIZE for _ in range(SIZE)]
for i in range(5): b[i + 1][i + 2] = 1               # 斜 ↘
assert five(b, 3, 4)
b = [[0] * SIZE for _ in range(SIZE)]
for i in range(5): b[i][8 - i] = 2                   # 斜 ↙
assert five(b, 2, 6)
b = [[0] * SIZE for _ in range(SIZE)]
for i in range(4): b[0][i] = 1
assert not five(b, 0, 3)
txt = render(b, (0, 3))
assert len(txt.splitlines()) == SIZE + 1 and "⬇️" in txt.splitlines()[0] and txt.splitlines()[1].startswith("➡️")


def mk(tmp=None, settings=None):
    tmp = tmp or Path(tempfile.mkdtemp())
    s = settings or all_on()
    r = build_router(fetch_list=lambda room: MEMBERS, fetch_info=lambda room: {"nickName": "测试群"},
                     db_path=tmp / "s.db", llm=Echo(), settings=s, personas=PersonaStore(None),
                     allow_targets={ROOM}, send_enabled=False, rng=random.Random(1),
                     word_link=lambda w, t: "x", amap=offline_amap())
    return r, tmp


def say(r, who, text, at=()):
    tr = r.dispatch(normalize(raw(who, text, list(at))))
    rs = [t for t in tr if t.get("reply")]
    return (rs[-1]["reply"] if rs else None), tr


def fed(tr):
    return [t["feature"] for t in tr if "skipped" not in t]


r, tmp = mk()
# 开关默认关：没开的群不理
s_off = FeatureSettings(None)
r0, _ = mk(settings=s_off)
assert say(r0, A, "五子棋")[0] is None or "五子棋" not in (say(r0, A, "五子棋")[0] or "")

# ---- 点名邀请 ----
rep, _ = say(r, A, "下五子棋 @Bob\u2005", at=[B])
assert "邀请" in rep and "Bob" in rep, rep
assert r.outbox[-1].ats == B
rep, tr = say(r, C, "加入")                       # 不是被邀请的人：不算
assert r.games.get(ROOM).phase == "wait"
rep, _ = say(r, B, "接受")
assert "报名成功" in rep and "阿离 执黑子⚫" in rep and "Bob 执白子🟡" in rep and "轮到 @阿离" in rep, rep
assert set(r.outbox[-1].ats.split(",")) == {A, B}
g = r.games.get(ROOM)
assert g.black == A and g.white == B and g.phase == "play"

# ---- 轮次 / 干扰 / 非法 ----
rep, tr = say(r, B, "55")                          # 没轮到白
assert rep is None and "games.input" in fed(tr) and g.moves == 0
rep, tr = say(r, C, "33")                          # 旁观者发数字：不理，也不走陪聊/插话
assert rep is None and fed(tr)[-1] == "games.input" and "chat_companion" not in str(tr), tr
rep, _ = say(r, A, "5 5")
assert "下在 55" in rep and "轮到 @Bob 🟡" in rep and r.outbox[-1].ats == B, rep
assert "➡️" in rep and "⬇️" in rep
rep, _ = say(r, B, "5,5")
assert rep == "55 已经有子了，换个位置～", rep
rep, _ = say(r, B, "50")
assert "超出棋盘" in rep, rep
rep, _ = say(r, B, "5,10")
assert "超出棋盘" in rep
assert g.turn == 2 and g.moves == 1
rep, tr = say(r, C, "这局谁会赢啊")                 # 非棋局消息：照常往后走（不被游戏吃掉）
assert "games.input" not in fed(tr)

# ---- 重启恢复：同一 data 目录重建 router ----
say(r, B, "44")
r2, _ = mk(tmp)
g2 = r2.games.get(ROOM)
assert g2 and g2.moves == 2 and g2.turn == 1 and g2.board[4][4] == 1 and g2.board[3][3] == 2, g2 and g2.to_state()
r = r2
# 黑横向连五：51..55 → 黑已有 55；下 51 52 53 54
for bm, wm in (("51", "11"), ("52", "12"), ("53", "13")):
    assert "轮到" in say(r, A, bm)[0]
    assert "轮到" in say(r, B, wm)[0]
rep, _ = say(r, A, "54")
assert "五子连珠" in rep and "阿离 ⚫ 获胜" in rep, rep
assert r.games.get(ROOM) is None
r3, _ = mk(tmp)
assert r3.games.get(ROOM) is None                  # 结束后存档清掉

# ---- 不点名：第一个发「加入」的人；竖向白胜 ----
rep, _ = say(r, A, "@小助手\u2005五子棋", at=[BOT])
assert "谁来" in rep
assert say(r, A, "加入")[0] is None                # 开局人自己不能加入
rep, _ = say(r, C, "加入")
assert "小C 执白子" in rep
for bm, wm in (("11", "29"), ("12", "39"), ("13", "49"), ("15", "59")):
    say(r, A, bm); say(r, C, wm)
rep, _ = say(r, A, "88")
rep, _ = say(r, C, "69")
assert "五子连珠" in rep and "小C 🟡 获胜" in rep, rep

# ---- 斜向 + 认输 ----
say(r, A, "五子棋"); say(r, B, "加入")
rep, _ = say(r, B, "认输") if False else (None, None)
for bm, wm in (("11", "19"), ("22", "29")):
    say(r, A, bm); say(r, B, wm)
rep, _ = say(r, C, "认输")                        # 旁观者认输：不算
assert r.games.get(ROOM) is not None
rep, _ = say(r, B, "认输")
assert "Bob 认输" in rep and "阿离 ⚫ 获胜" in rep, rep
say(r, A, "五子棋"); say(r, B, "加入")
for bm, wm in (("11", "19"), ("22", "29"), ("33", "39"), ("44", "49")):
    say(r, A, bm); say(r, B, wm)
rep, _ = say(r, A, "55")
assert "五子连珠" in rep

# ---- 一群一局 ----
say(r, A, "五子棋")
rep, _ = say(r, C, "五子棋")
assert "正在进行「五子棋」" in rep, rep

# ---- 等人超时 ----
out = r.games.tick(time.time() + 181)
assert out and "没人加入" in out[0].content and r.games.get(ROOM) is None

# ---- 落子超时 ----
say(r, A, "五子棋"); say(r, B, "加入"); say(r, A, "55")
assert not r.games.tick(time.time() + 300)
out = r.games.tick(time.time() + 601)
assert out and "没人落子" in out[0].content and r.games.get(ROOM) is None

# ---- 结束五子棋：玩家 / 主人本人强制；旁观者不行 ----
say(r, A, "五子棋"); say(r, B, "加入")
assert say(r, C, "结束五子棋")[0] is None and r.games.get(ROOM)
rep, _ = say(r, OWNER, "结束五子棋")
assert "主人 结束了" in rep and r.games.get(ROOM) is None, rep
say(r, A, "五子棋"); say(r, B, "加入")
rep, _ = say(r, B, "结束五子棋")
assert "Bob 结束了" in rep
say(r, A, "下五子棋 @Bob\u2005", at=[B])
rep, _ = say(r, B, "拒绝")
assert "取消" in rep and r.games.get(ROOM) is None

# ---- 主人静音：静音中棋局不说话，超时也悄悄结束 ----
say(r, A, "五子棋"); say(r, B, "加入")
r.mute.mute(ROOM, OWNER, 3600, "测试静音")
rep, tr = say(r, A, "55")
assert rep is None and r.games.get(ROOM).moves == 0
assert r.games.tick(time.time() + 601) == [] and r.games.get(ROOM) is None
r.mute.unmute(ROOM, OWNER, "解除")

# ---- 能力清单 / 帮助 ----
from features import capabilities as caps
from features.admin import HELP
assert "五子棋" in caps.describe(r.settings.enabled if hasattr(r, "settings") else all_on().enabled, ROOM)
assert "五子棋" in HELP
print("五子棋测试通过 ✅")

# ==== 10-28 线上事故回归：带 @ 前缀开局 / 模型钩子开局 / 加入 / 落子 / 不许编「已开」 ====
from features.games.gomoku import is_start
for t in ("五子棋", "下一局五子棋", "你们两下一局五子棋", "你俩来一局五子棋吧", "我们来下盘五子棋", "开一把五子棋"):
    assert is_start(t), t
for t in ("五子棋怎么玩", "你会下五子棋吗", "不下五子棋了", "结束五子棋", "今天下了一局五子棋输麻了，好气啊，不想再下了啊啊"):
    assert not is_start(t), t
CAT = "wxid_d"                                       # 群里另一个 bot（机器人乙）；helpers 里叫「大熊」

def fresh():
    rr, _ = mk()
    return rr

def outs(rr, n0):
    return [o.content for o in rr.outbox[n0:]]

# 1) 主人 @小助手 @机器人乙 「你们两下一局五子棋」：小助手是裁判要排除，只剩一个对手 → 发起人对机器人乙，当场发棋盘+规则
r = fresh()
rep, tr = say(r, OWNER, "@小助手\u2005\u2005@大熊\u2005你们两下一局五子棋", at=[BOT, CAT])
assert "games.gomoku" in fed(tr) and "chat_companion" not in str(tr), tr
g = r.games.get(ROOM)
assert g and g.host == OWNER and g.invitee == CAT and BOT not in (g.host, g.invitee), g.to_state()
assert "规则" in rep and LABELS[0] in rep and "邀请" in rep, rep
assert r.outbox[-1].ats == CAT
# 机器人乙那种带后缀的「加入！行吧…」也要算加入
rep, tr = say(r, CAT, "加入！行吧 五子棋 我没输过 主要是没人跟我下")
assert "games.input" in fed(tr) and "报名成功" in rep and g.phase == "play" and g.white == CAT, rep
rep, tr = say(r, OWNER, "55")
assert "games.input" in fed(tr) and "下在 55" in rep and g.board[4][4] == 1, rep
rep, tr = say(r, CAT, "55")
assert rep == "55 已经有子了，换个位置～", rep
rep, tr = say(r, CAT, "44")
assert "下在 44" in rep and g.moves == 2

# 2) 点名两个真人（发起人不在其中）→ 这两人直接对下，不用等加入
r = fresh()
rep, tr = say(r, OWNER, "@小助手\u2005@阿离\u2005@Bob\u2005你们两下一局五子棋", at=[BOT, A, B])
g = r.games.get(ROOM)
assert g and g.phase == "play" and (g.black, g.white) == (A, B), g and g.to_state()
assert "开局" in rep and "规则" in rep and LABELS[8] in rep, rep
assert set(r.outbox[-1].ats.split(",")) == {A, B}
rep, _ = say(r, A, "55")
assert "下在 55" in rep

# 3) 只 @小助手、不点别人 → 谁先回「加入」谁下
r = fresh()
rep, _ = say(r, OWNER, "@小助手\u2005下一局五子棋", at=[BOT])
assert "谁来" in rep and "规则" in rep and r.games.get(ROOM).invitee is None, rep
rep, _ = say(r, C, "加入")
assert "报名成功" in rep and r.games.get(ROOM).white == C

# 4) 模型钩子开局：话术不像口令，模型回【开局:五子棋】→ 真建棋局，棋盘真的发出去（普通 + 流式两条路径）
class TagLLM:
    def __init__(self, text): self.text, self.calls = text, 0
    def chat(self, user, system=None, history=None, **kw):
        self.calls += 1; return self.text

class StreamTagLLM(TagLLM):
    def chat_stream(self, user, system=None, history=None, **kw):
        self.calls += 1
        for i in range(0, len(self.text), 3):
            yield self.text[i:i + 3]

for L in (TagLLM, StreamTagLLM):
    for text in ("收到～\n你们俩回复「加入」即可开始\n【开局:五子棋】", "好，五子棋开一局～\n【开局:五子棋】"):
        tmp = Path(tempfile.mkdtemp())
        llm = L(text)
        r = build_router(fetch_list=lambda room: MEMBERS, fetch_info=lambda room: {"nickName": "测试群"},
                         db_path=tmp / "s.db", llm=llm, settings=all_on(), personas=PersonaStore(None),
                         allow_targets={ROOM}, send_enabled=False, rng=random.Random(1),
                         word_link=lambda w, t: "x", amap=offline_amap())
        n0 = len(r.outbox)
        rep, tr = say(r, OWNER, "@小助手\u2005跟 @Bob\u2005比比五子棋", at=[BOT, B])
        g = r.games.get(ROOM)
        sent = "\n".join(outs(r, n0))
        assert llm.calls == 1 and g and g.host == OWNER and g.invitee == B, (L, text, tr)
        assert "邀请" in sent and "规则" in sent and LABELS[0] in sent, (L, sent)
        assert "开局:" not in sent
        rep, tr = say(r, B, "接受")
        assert "games.input" in fed(tr) and "报名成功" in rep
        rep, tr = say(r, OWNER, "55")
        assert "games.input" in fed(tr) and "下在 55" in rep

# 5) 不许编：模型没写开局标记却说「正式重开」/ 写了标记但开不成 → 如实说没开成
for L in (TagLLM, StreamTagLLM):
    for text, settings in (("刚才我漏了开局标记，棋盘没生成，前面两个 55 不算\n现在正式重开", None),
                           ("好，五子棋开一局～\n【开局:五子棋】", "off")):
        s2 = all_on()
        if settings == "off":
            s2.set(ROOM, "game_gomoku", False)
        tmp = Path(tempfile.mkdtemp())
        r = build_router(fetch_list=lambda room: MEMBERS, fetch_info=lambda room: {"nickName": "测试群"},
                         db_path=tmp / "s.db", llm=L(text), settings=s2, personas=PersonaStore(None),
                         allow_targets={ROOM}, send_enabled=False, rng=random.Random(1),
                         word_link=lambda w, t: "x", amap=offline_amap())
        n0 = len(r.outbox)
        say(r, OWNER, "@小助手\u2005棋盘呢。。。", at=[BOT])
        sent = "\n".join(outs(r, n0))
        assert r.games.get(ROOM) is None
        assert "正式重开" not in sent and "开一局" not in sent and "没开成" in sent, (L, text, sent)
print("五子棋 10-28 回归测试通过 ✅")

