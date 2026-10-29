"""功能 8 文字游戏单测：猜成语、扫雷、成语接龙、谁是卧底、猜歌；一群一局、超时、开关、模型白名单。"""
import time
from pathlib import Path
from helpers import Bot, ROOM, BOT, FeatureSettings, private, normalize
from features.games.handle import HandleGame, idioms
from features.games.idiom_chain import candidates, py
from features.games.minesweeper import MinesweeperGame, OpenR
from features.games.undercover import UndercoverGame, gen_words
from features.games.guess_song import GuessSongGame, safe_hint, initials
from PIL import Image

OTHER = "20000000005@chatroom"


def game(b):
    return b.r.games.get(ROOM)


def test_handle():
    b = Bot()
    b.say("wxid_a", "猜成语")
    out = b.last_reply()
    assert "猜成语开始" in out.content and Path(out.image_path).exists()
    g = game(b)
    assert isinstance(g, HandleGame)
    assert "一次只能玩一局" in b.say("wxid_b", "扫雷"), "一群一局"
    assert "不在成语词库" in b.say("wxid_b", "随便四字", at_bot=False)
    wrong = next(w for w in ("一心一意", "画蛇添足", "守株待兔") if w != g.idiom)
    r = b.say("wxid_b", wrong, at_bot=False)
    assert "第 1/10 次" in r and Path(b.last_reply().image_path).exists()
    im = Image.open(b.last_reply().image_path); assert im.size[1] > 300
    assert "已经猜过" in b.say("wxid_c", wrong, at_bot=False)
    assert b.say("wxid_c", "今天天气不错", at_bot=False) != "提示" and True
    b.say("wxid_c", "提示", at_bot=False); assert b.last_reply().image_path
    assert "恭喜猜中" in b.say("wxid_c", g.idiom, at_bot=False) and game(b) is None
    print("猜成语 ✅ 示例图", out.image_path)


def test_minesweeper():
    b = Bot()
    b.say("wxid_a", "扫雷")
    g = game(b); assert isinstance(g, MinesweeperGame)
    r = b.say("wxid_a", "挖开 A1", at_bot=False)
    assert g.ms.tiles[0][0].is_open and not g.ms.tiles[0][0].is_mine     # 首挖必不是雷
    assert "超出范围" in b.say("wxid_a", "挖开 Z9", at_bot=False)
    # 把所有非雷格挖开 → 胜利
    for i in range(8):
        for j in range(8):
            if not g.ms.tiles[i][j].is_mine and not g.ms.tiles[i][j].is_open and not g.over:
                b.say("wxid_b", f"挖开 {chr(65 + i)}{j + 1}", at_bot=False)
    assert g.over and "扫雷成功" in b.last_reply().content and game(b) is None
    b.say("wxid_a", "扫雷 中级"); g = game(b)
    assert g.ms.row == 16 and g.ms.mine_num == 40
    b.say("wxid_a", "挖开 A1", at_bot=False)
    mine = next((i, j) for i in range(16) for j in range(16) if g.ms.tiles[i][j].is_mine)
    assert "是雷" in b.say("wxid_a", f"挖开 {chr(65 + mine[0])}{mine[1] + 1}", at_bot=False)
    print("扫雷 ✅ 示例图", b.last_reply().image_path)


def test_idiom_chain():
    b = Bot()
    b.say("wxid_a", "成语接龙")
    g = game(b)
    cur = g.cur
    nxt = candidates(cur, g.used)[0]
    assert b.say("wxid_c", "我们去吃饭", at_bot=False) is None        # 普通聊天不打扰
    r = b.say("wxid_b", nxt, at_bot=False)
    assert "Bob 接" in r and g.cur == nxt, r
    assert b.say("wxid_c", nxt, at_bot=False) is None                 # 首字对不上（已轮到下一个字），不打扰
    fake = nxt[-1] + "乱七八"
    assert fake not in idioms() and "不在成语词库" in b.say("wxid_c", fake, at_bot=False)
    assert "可以试试" in b.say("wxid_c", "提示", at_bot=False)
    g.last_active -= 200
    outs = b.r.games.tick()
    assert outs and "接龙结束" in outs[0].content and "Bob 1 个（+5 分）" in outs[0].content, outs[0].content
    assert b.r.economy.account(ROOM, "wxid_b").coins == 5 and game(b) is None
    print("成语接龙 ✅")


def test_undercover():
    llm_calls = []

    class WordLLM:
        def chat(self, user, system=None, history=None, **kw):
            llm_calls.append(user)
            return '{"civilian": "苹果", "undercover": "梨"}' if "谁是卧底" in user else "陪聊"

    b = Bot(llm=WordLLM())
    assert "招募中" in b.say("wxid_a", "谁是卧底")
    for w in ("wxid_b", "wxid_c"):
        b.say(w, "加入", at_bot=False)
    assert "至少 4 人" in b.say("wxid_a", "开始", at_bot=False)
    b.say("wxid_d", "加入", at_bot=False)
    start = b.say("wxid_a", "开始", at_bot=False)
    g = game(b)
    assert "出词：MiniMax" in start and "https://img.example/1" in start and g.phase == "speak", start
    assert len(g.spies) == 1 and set(g.words.values()) == {"苹果", "梨"}
    # 私聊查词（被动回复）：生成回复，但私聊不在白名单 → 只进 outbox
    tr = b.r.dispatch(normalize(private(g.alive[0], "查词")))
    assert tr[-1]["sent"] is False and g.words[g.alive[0]] in tr[-1]["reply"]
    # 一轮发言
    for p in list(g.order):
        assert g.order[g.turn] == p
        b.say(p, "这个东西很好吃", at_bot=False)
    assert g.phase == "vote"
    spy = next(iter(g.spies))
    civ = [p for p in g.alive if p != spy]
    for p in civ:
        b.say(p, f"投 {g.alive.index(spy) + 1}", at_bot=False)
    r = b.say(spy, f"投 {g.alive.index(civ[0]) + 1}", at_bot=False)
    assert "平民胜利" in r and "卧底词：梨" in r and game(b) is None, r
    # 非白名单群：不调模型，用本地词库
    n = len(llm_calls)
    b2 = Bot(llm=WordLLM(), allow=())
    b2.say("wxid_a", "谁是卧底")
    for w in ("wxid_b", "wxid_c", "wxid_d"):
        b2.say(w, "加入", at_bot=False)
    s2 = b2.say("wxid_a", "开始", at_bot=False)
    assert "本地词库" in s2 and len(llm_calls) == n
    # 超时推进：发言超时跳过、投票超时结算
    g2 = b2.r.games.get(ROOM)
    g2.deadline = 0
    out = b2.r.games.tick()
    assert "超时，跳过" in out[0].content
    print("谁是卧底 ✅")


def test_undercover_foul_and_recruit_timeout():
    b = Bot(allow=())
    b.say("wxid_a", "谁是卧底")
    g = game(b); g.deadline = 0
    out = b.r.games.tick()
    assert "取消" in out[0].content and game(b) is None
    b.say("wxid_a", "谁是卧底")
    for w in ("wxid_b", "wxid_c", "wxid_d", "wxid_e"):
        b.say(w, "加入", at_bot=False)
    b.say("wxid_a", "开始", at_bot=False)
    g = game(b)
    p = g.order[0]
    r = b.say(p, f"我的词是{g.words[p]}", at_bot=False)
    assert "犯规出局" in r and p not in g.alive
    print("卧底犯规/招募超时 ✅")


def test_guess_song():
    class HintLLM:
        def __init__(self, text): self.text = text
        def chat(self, *a, **k): return self.text
    song = ("晴天", "周杰伦", 2003)
    g = GuessSongGame(ROOM, llm=HintLLM("一首关于青春里错过的雨天和暗恋的歌"), song=song)
    assert g.hint_src == "MiniMax"
    g2 = GuessSongGame(ROOM, llm=HintLLM("周杰伦的晴天"), song=song)
    assert g2.hint_src == "本地" and "00 年代" in g2.hint1
    assert initials("晴天") == "QT" and not safe_hint("晴天的雨", "晴天", "周杰伦")
    b = Bot(llm=HintLLM("一首关于青春里错过的雨天和暗恋的歌"))
    b.say("wxid_a", "猜歌")
    g = game(b)
    t0 = g.t0
    assert "提示 2" in b.r.games.tick(t0 + 41)[0].content
    assert "提示 3" in b.r.games.tick(t0 + 81)[0].content
    assert b.say("wxid_b", "不知道", at_bot=False) is None
    r = b.say("wxid_c", g.song[0], at_bot=False)
    assert "猜中了" in r and b.r.economy.account(ROOM, "wxid_c").coins == 20
    b.say("wxid_a", "猜歌"); g = game(b)
    assert "答案是" in b.r.games.tick(g.t0 + 121)[0].content and game(b) is None
    print("猜歌 ✅")


def test_toggles_and_stop():
    s = FeatureSettings(None)
    b = Bot(settings=s)
    assert "猜成语开始" in (b.say("wxid_a", "猜成语") and b.last_reply().content)   # 默认开
    assert "已结束" in b.say("wxid_a", "结束游戏") and game(b) is None
    assert b.say("wxid_a", "谁是卧底") is None and b.say("wxid_a", "猜歌") is None   # 默认关
    s.set(ROOM, "game_handle", False)
    assert b.say("wxid_a", "猜成语") is None
    print("游戏开关 ✅")


if __name__ == "__main__":
    test_handle(); test_minesweeper(); test_idiom_chain(); test_undercover(); test_undercover_foul_and_recruit_timeout()
    test_guess_song(); test_toggles_and_stop()
    print("全部通过 ✅")
