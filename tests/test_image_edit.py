"""10-23 两个生图 bug：
1) 「帮我生成一张猪猪侠的照片」被当真人婉拒 → 卡通/虚构角色、动物、物品可画；「照片」本身不算真人；拿不准问小模型。
2) 「你把这个图给我做成红色的，带火焰的那种」+ 图 → 走改图（看原图写描述 → 重新生成，卡通角色带 subject_reference），如实说是重画；
   陪聊发出前拦「图已发/已经加上了/成品」和生图提示词。每群每日限额照旧。全 mock，不联网、不发消息。"""
import io, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image
from app import build_router
from features.image_gen import refuse_reason, edit_instruction, make_person_judge, make_describe
from features.chat_companion import fix_image_claims, looks_like_image_prompt, IMG_HONEST
from tests.helpers import MEMBERS, ROOM, BOT, raw, all_on
from models import normalize

# ---- 1) 真人判断 ----
assert refuse_reason("猪猪侠的照片") is None                          # 没小模型：照片本身不算真人
assert refuse_reason("猪猪侠的照片", judge=lambda p: False) is None
assert refuse_reason("一只柯基的真实感照片", judge=lambda p: False) is None
assert refuse_reason("孙悟空大战奥特曼") is None
assert refuse_reason("周杰伦弹钢琴") == "person"                       # 明确的真人
assert refuse_reason("阿离在跳舞", ["阿离"]) == "person"               # 群友
assert refuse_reason("我的样子画成漫画") == "person"
assert refuse_reason("刘亦菲的写真", judge=lambda p: True) == "person"  # 名单外的明星 → 小模型判是真人
assert refuse_reason("某个明星的照片") == "person"                      # 小模型判不了 + 明确真人词 → 婉拒
assert refuse_reason("某个明星的照片", judge=lambda p: None) == "person"
assert refuse_reason("动漫里的偶像角色", judge=lambda p: False) is None  # 小模型说不是真人 → 画
class J:
    def __init__(self, out): self.out, self.n = out, 0
    def chat(self, user, system=None, **kw): self.n += 1; self.user = user; return self.out
j = J("否"); assert make_person_judge(lambda s: j)("r", "猪猪侠的照片") is False and "猪猪侠" in j.user
assert make_person_judge(lambda s: J("是"))("r", "x") is True
assert make_person_judge(lambda s: J("不确定"))("r", "x") is None
assert make_person_judge(lambda s: None)("r", "x") is None
print("真人判断：卡通/照片不误伤，真人照拒，拿不准问小模型 ✅")

# ---- 2b) 陪聊发出前拦截 ----
P = "画一张参考图同款 crescent 机械月牙，保持透明蓝白科技材质、深色背景，加入赤红火焰与橙色火星，高细节，中心留空，方形构图，无文字"
assert looks_like_image_prompt(P)
assert fix_image_claims(P, False) == IMG_HONEST and fix_image_claims(P, True) == ""
assert fix_image_claims("行，我按原图做成红色火焰风格", False) == IMG_HONEST
assert fix_image_claims("红色火焰已经加上了。刚才发的就是成品", False) == IMG_HONEST
assert fix_image_claims("这个模型挺好用的，你可以试试", False) == "这个模型挺好用的，你可以试试"
assert fix_image_claims("把图发我一下", False) == "把图发我一下"
assert not looks_like_image_prompt("Claude 和 GPT 都能写代码，各有优势，看你习惯")
print("陪聊拦截：假称已发图 / 生图提示词 ✅")

assert edit_instruction("你把这个图给我做成红色的，带火焰的那种")
assert edit_instruction("按这张图画一个赛博朋克版") and not edit_instruction("今天天气不错")

# ---- 2a) 改图端到端 ----
buf = io.BytesIO(); Image.new("RGB", (64, 64), (100, 160, 255)).save(buf, "PNG"); PNG = buf.getvalue()
IMG_XML = '<?xml version="1.0"?>\n<msg><img aeskey="k" cdnthumburl="x" length="10"/></msg>'


class LLM:
    """陪聊回复固定；看图描述（多模态 list）返回 JSON。"""
    def __init__(self, real=False, character=False):
        self.real, self.character, self.desc = real, character, []
    def chat(self, user, system=None, history=None, **kw):
        if isinstance(user, list):
            self.desc.append(user)
            return ('{"real_person": %s, "character": %s, "prompt": "深色背景上一枚透明蓝白科技感机械月牙，改成赤红色并燃起火焰，方形构图"}'
                    % ("true" if self.real else "false", "true" if self.character else "false"))
        if "是不是一个真实存在的具体人物" in str(user):
            return "否"
        return "陪聊回答"


def mk(llm, daily=10):
    d = Path(tempfile.mkdtemp()); sent, gens = [], []
    def img_fn(p, out, subject_ref=None):
        gens.append((p, subject_ref)); out.mkdir(parents=True, exist_ok=True)
        f = out / "gen-edit.jpg"; f.write_bytes(b"\xff\xd8\xff" + b"0" * 10); return f
    r = build_router(fetch_list=lambda x: MEMBERS, fetch_info=lambda x: {}, sender=lambda m: sent.append(m), send_enabled=True,
                     db_path=d / "s.db", llm=llm, settings=all_on(), allow_targets={ROOM}, amap_tools=False,
                     image_fn=img_fn, image_async=False,
                     media_io={"download_voice": lambda x, m: "", "download_image": lambda x, t: "http://oos.example/i.png",
                               "asr": lambda w: "", "fetch": lambda u: PNG})
    r.multi_async = False; r.sleep = lambda s: None
    r.imagegen.daily = daily
    return r, sent, gens

# 复现 00:23：先说要求（@），1 秒后发图 → 开画
llm = LLM(); r, sent, gens = mk(llm)
r.dispatch(normalize(raw("wxid_a", "@小助手\u2005你把这个图给我做成红色的，带火焰的那种", [BOT])))
assert "图发上来" in sent[-1].content and not gens
r.dispatch(normalize(raw("wxid_a", IMG_XML, mt=3)))
assert any("重新画" in m.content and "不是在原图上直接改" in m.content for m in sent), [m.content for m in sent]
assert sent[-1].image_path and sent[-1].image_path.endswith("gen-edit.jpg")
assert gens and "赤红" in gens[0][0] and gens[0][1] is None              # 非角色：不带参考图
assert llm.desc and "做成红色" in llm.desc[0][0]["text"]
assert any("照着群友发的图重画" in e["t"] for e in r.context.entries(ROOM))
assert r.imagegen.counter.get(f"img:{ROOM}") == 1
# 别人发的图不会被当成这个人的改图
r.dispatch(normalize(raw("wxid_b", "@小助手\u2005把这张图改成赛博朋克风", [BOT])))
r.dispatch(normalize(raw("wxid_a", IMG_XML, mt=3)))
assert len(gens) == 1
# 先发图、再说要求（同一人 90 秒内）→ 直接开画；卡通角色带 subject_reference
llm2 = LLM(character=True); r2, sent2, gens2 = mk(llm2)
r2.dispatch(normalize(raw("wxid_a", IMG_XML, mt=3)))
r2.dispatch(normalize(raw("wxid_a", "@小助手\u2005按这张图画成赛博朋克风", [BOT])))
assert gens2 and gens2[0][1] and gens2[0][1].startswith("data:image/jpeg;base64,")
assert sent2[-1].image_path
# 原图是真人照片 → 婉拒、退额度、不生成
llm3 = LLM(real=True); r3, sent3, gens3 = mk(llm3)
r3.dispatch(normalize(raw("wxid_a", IMG_XML, mt=3)))
r3.dispatch(normalize(raw("wxid_a", "@小助手\u2005把这张照片改成动漫风", [BOT])))
assert not gens3 and "真人" in sent3[-1].content and r3.imagegen.counter.get(f"img:{ROOM}") == 0
# 每群每日限额照旧
r4, sent4, gens4 = mk(LLM(), daily=1)
r4.dispatch(normalize(raw("wxid_a", IMG_XML, mt=3)))
r4.dispatch(normalize(raw("wxid_a", "@小助手\u2005把这张图改成红色", [BOT])))
r4.dispatch(normalize(raw("wxid_a", "@小助手\u2005把这张图改成蓝色", [BOT])))
assert len(gens4) == 1 and "明天再画" in sent4[-1].content
# 猪猪侠端到端：不再婉拒
r5, sent5, gens5 = mk(LLM())
r5.dispatch(normalize(raw("wxid_a", "@小助手\u2005帮我生成一张猪猪侠的照片", [BOT])))
assert gens5 and "猪猪侠" in gens5[0][0] and sent5[-1].image_path, [m.content for m in sent5]
print("改图：先说后发图 / 先发图后说 / 只认同一人 / 卡通带参考图 / 真人照婉拒 / 限额 / 猪猪侠可画 ✅")

# describe 解析
class V:
    def chat(self, user, system=None, **kw): return '<think>x</think>好的 {"real_person": false, "character": true, "prompt": "一只粉色卡通小猪超人"}'
d = make_describe(lambda s: V())("r", "data:image/jpeg;base64,AAA", "改成红色")
assert d == {"real_person": False, "character": True, "prompt": "一只粉色卡通小猪超人"}
print("test_image_edit 全部通过")

# 陪聊端到端：模型吐生图提示词 / 假称已发图 → 发出去的是如实一句
class Liar:
    def chat(self, user, system=None, history=None, **kw):
        return "红色火焰已经加上了\n" + P
for st_off in (True,):
    s = all_on(); s.set(ROOM, "image_gen", False)
    d = Path(tempfile.mkdtemp()); sent6 = []
    r6 = build_router(fetch_list=lambda x: MEMBERS, fetch_info=lambda x: {}, sender=lambda m: sent6.append(m), send_enabled=True,
                      db_path=d / "s.db", llm=Liar(), settings=s, allow_targets={ROOM}, amap_tools=False)
    r6.multi_async = False; r6.sleep = lambda s: None
    r6.dispatch(normalize(raw("wxid_a", "@小助手\u2005图呢", [BOT])))
    out = " ".join(m.content for m in sent6)
    assert "已经加上" not in out and "构图" not in out and "没真的出图" in out, out
print("陪聊端到端拦截 ✅")
