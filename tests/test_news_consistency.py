"""快讯推送前一致性核对（主人 10-22 23:36）：短标题实体/数字要有原文依据、显示来源要来自原始信源；
对不上先按原标题重写一次、仍不行丢弃并按分层补位；模型判断一期一次，失败降级只跑规则；BOT_AIHOT_CONSISTENCY=0 可关。全 mock。"""
import sys, os, re, json, tempfile
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["BOT_AIHOT_LLM_DEDUP"] = "0"
os.environ["BOT_AIHOT_LLM_VALUE"] = "0"
os.environ.pop("BOT_AIHOT_CONSISTENCY", None)
from features import ai_news, flash_page
from features.settings import FeatureSettings
TZ = ai_news.TZ
NOW = datetime(2026, 10, 22, 16, 0, 20, tzinfo=TZ).timestamp()
iso = lambda h: datetime.fromtimestamp(NOW - h * 3600, TZ).isoformat()

# ---- 规则：标题依据 ----
e = {"title": "OpenAI 发布 GPT-6 预览版", "summary": "OpenAI 宣布 GPT-6 预览，上下文 200 万 token，融资 150 亿美元。"}
G = ai_news.title_grounded
assert G("OpenAI发布GPT-6预览", e) == ""
assert "Google" in G("Google发布GPT-6预览", e)                     # 公司张冠李戴
assert "GPT-7" in G("OpenAI发布GPT-7", e)                           # 产品名对不上
assert "300" in G("OpenAI融资300亿美元", e)                          # 数字对不上
assert G("OpenAI融资150亿美元", e) == "" and G("AI 圈大事：OpenAI 新模型", e) == ""   # AI 这类通用词不算实体
assert G("英伟达发布新芯片", {"title": "NVIDIA unveils new chip", "summary": ""}) == ""   # 中英写法互通
assert "英伟达" in G("英伟达发布新芯片", {"title": "AMD unveils new chip", "summary": ""})
assert G("Nokia利润1,200万", {"title": "Nokia profit 1200", "summary": ""}) == ""         # 千分位
assert G("Nirva融资3300万美元推出AI吊坠", {"title": "Nirva 融资 $33M 推出 AI 吊坠", "summary": ""}) == ""   # 单位换算（有效数字一致）
assert "34" in G("Nirva融资3400万美元", {"title": "Nirva 融资 $33M", "summary": ""})
FLY = "Fly.io 推出 Sprites Block Device（SBD）文件系统"
assert ai_news.title_check("Fly.io 推出", FLY, FLY) == "bad"                  # 原标题截一半停在动词上（10-22 23:4x dry-run 实例）
assert ai_news.title_ok("Isaac Lab 3.0发布", "", "")                         # 主语+动词本身完整的不误判（soft：先请模型重写一次，重写不好也能用）
# 10-25 22 时「Anthropic 发布免费」（主人：像谜语人）：悬空收尾/太短 → bad；回退不硬截，整句用原标题
ANT = "Anthropic 发布免费的 OSS Scanner AI 漏洞扫描器"
assert ai_news.title_check("Anthropic 发布免费", ANT, ANT) == "bad"
assert ai_news.title_check("Anthropic 免费开放 AI 漏洞扫描器 OSS Scanner", ANT, ANT) == "ok"
assert ai_news.title_check("OpenAI 将", "", "") == "bad" and ai_news.title_check("谷歌", "", "") == "bad"
assert ai_news.title_check(ANT, ANT, ANT) == "ok"                             # 原标题原样照用 = 完整
assert ai_news.compress_title(ANT) == ANT                                     # 以前会硬截成「Anthropic 发布免费」
LONG = "OpenAI 宣布：将在明年推出全新的 GPT-6 模型，并同步开放给所有企业客户使用，价格下调一半以上"
c = ai_news.compress_title(LONG); assert ai_news.title_ok(c, LONG, LONG) and not c.endswith(("免费", "的", "宣布")), c
print("规则：短标题自洽（不悬空、不硬截半句）✅")
print("规则：标题实体/数字依据 ✅")

# ---- 规则：来源依据 ----
pf = ai_news.primary_first
ok = pf({"id": "s1", "title": "t", "sources": [{"name": "36氪", "url": "https://36kr.com/a", "published_at": iso(1)}]})
assert ai_news.source_grounded(ok) == ""
bad = dict(ok, sources=[{"name": "Bloomberg", "url": "https://bloomberg.com/x"}] + ok["sources"])
assert "Bloomberg" in ai_news.source_grounded(bad)
bad2 = dict(ok, sources=[{"name": "36氪", "url": "https://evil.example/x"}])
assert "链接" in ai_news.source_grounded(bad2)
o = pf({"id": "s2", "title": "t", "sources": [{"name": "AGI Hunt（现有）", "url": "https://agihunt.info/p/1", "published_at": iso(2),
        "origin": {"url": "https://x.com/Pytlicek/status/1", "author": "@Pytlicek", "platform": "x", "publishedAt": iso(3)}}]})
assert o["sources"][0]["name"] == "Pytlicek（X）" and ai_news.source_grounded(o) == ""     # 原帖作者
r = pf({"id": "s3", "title": "t", "sources": [{"name": "雷达侦探精选", "url": "https://claude.com/a", "published_at": iso(1)}]})
assert r["sources"][0]["name"] == "Anthropic" and ai_news.source_grounded(r) == ""          # 二次汇总号显示原文域名
lab = {"title": "x", "summary": "TheZvi 转发了一段话", "sources": [{"name": "TheZvi（X）", "url": "https://x.com/TheZvi", "_label": True}]}
assert ai_news.source_grounded(lab) == "" and ai_news.source_grounded(dict(lab, summary="某人转发")) != ""
print("规则：来源依据（原始信源/原帖作者/汇总号域名/转述作者）✅")

# ---- 端到端：重写 / 丢弃 / 补位 / 一期一次模型判断 ----
NAMES = ["OpenAI", "Anthropic", "Mistral", "Cohere", "Perplexity", "Runway", "Midjourney", "Stability", "Groq", "Cerebras", "SambaNova", "Lambda"]
def pool():
    return [{"id": f"p{i}", "rank": i, "title": f"{n} 发布 M{i} 新版本", "summary": f"{n} 宣布 M{i}，性能提升 {10 + i}%。",
             "url": f"https://aihot.example.com/story/p{i}",
             "sources": [{"name": f"媒体{i}", "url": f"https://m{i}.example/a", "published_at": iso(0.5 + i * 0.3)}]}
            for i, n in enumerate(NAMES)]

class LLM:
    def __init__(self, consist='{"bad": [{"i": 7, "kind": "source", "why": "来源不对"}]}', refix=None):
        self.calls = {"rewrite": 0, "consist": 0, "refix": 0}
        self.consist, self.refix = consist, refix
    def chat(self, prompt, system=None, **kw):
        if "核对下面每条" in prompt:
            self.calls["consist"] += 1
            if self.consist is None:
                raise RuntimeError("minimax 502")
            return self.consist
        if "和原文对不上" in prompt:
            self.calls["refix"] += 1
            items = json.loads(re.findall(r"^\[.*\]$", prompt, re.M)[-1])
            fixed = self.refix or {}
            return json.dumps({"items": [{"i": it["i"], "title": fixed.get(it["原标题"], it["原标题"].replace(" 新版本", ""))} for it in items]}, ensure_ascii=False)
        self.calls["rewrite"] += 1
        items = json.loads(re.search(r"\[\{\"i\".*?\}\](?=\s|$)", prompt, re.S).group(0))
        out = []
        for it in items:
            t = it["title"].replace(" 新版本", "")
            if "OpenAI" in t:
                t = t.replace("OpenAI 发布", "Google 发布")             # 张冠李戴 → 重写后修好
            if "Mistral" in t:
                t = "Mistral 新模型提速 99%"                                # 数字编造 → 重写仍错 → 丢
            out.append({"i": it["i"], "title": t, "summary": "摘要"})
        return json.dumps({"intro": "导语。第二句。", "items": out}, ensure_ascii=False)

class NR:
    allow_targets, block_targets = {"r1@chatroom"}, set()
    def __init__(self): self.sent = []
    def deliver(self, rep): self.sent.append(rep); return True

def run(llm, env=None):
    if env is None:
        os.environ.pop("BOT_AIHOT_CONSISTENCY", None)
    else:
        os.environ["BOT_AIHOT_CONSISTENCY"] = env
    st = FeatureSettings(None); st.set("r1@chatroom", "ai_news", True)
    pubs = {}
    sx = ai_news.AiNewsScheduler(NR(), settings=st, fetch=lambda d: {"date": d, "events": pool()}, summarize=llm,
                                 state_path=Path(tempfile.mkdtemp()) / "s.json", clock=lambda: NOW,
                                 publish=lambda issue, page: pubs.setdefault(issue, page) and flash_page.page_url(issue))
    assert sx.tick() == 1
    return sx, pubs

refix = {"Mistral 发布 M2 新版本": "Mistral 发布 M2 提速 50%"}   # 重写仍编数字 → 丢
llm = LLM(refix=refix)
sx, pubs = run(llm)
text = sx.router.sent[0].content
fin = sx._final_evs
ids = [e["id"] for e in fin]
assert llm.calls["consist"] == 1                                        # 一期只调一次模型判断
assert len(fin) == 10 and "p2" not in ids and "p6" not in ids, ids    # Mistral（数字）和第 6 条（模型判来源不符）丢弃
assert "p10" in ids and "p11" in ids                                    # 按分层顺序用备选补位
assert "Google" not in text and "OpenAI 发布 M0" in text  # p0 张冠李戴已按原标题重写
assert sx.last_consistency == {"rewritten": 1, "dropped": 2, "final": 10}, sx.last_consistency
page = next(iter(pubs.values()))
assert page.count('<div class="no">') == 10 and "Google" not in page and "Mistral 发布 M2" not in page
seen = sx.seen_ids()
assert "p2" not in seen and "p6" not in seen and "p11" in seen          # 丢掉的不算推过，下期还能再核
assert [e["rank"] for e in fin] == list(range(1, 11))
print("端到端：张冠李戴重写 / 数字编造重写仍错丢弃 / 模型判来源不符丢弃 / 补位到 10 条 / 丢的不算推过 ✅")

# ---- 模型判断失败 → 只跑规则，不阻塞推送 ----
llm = LLM(consist=None, refix=refix)
sx, pubs = run(llm)
ids = [e["id"] for e in sx._final_evs]
assert llm.calls["consist"] == 1 and len(ids) == 10 and "p2" not in ids and "p6" in ids   # 规则仍抓到 Mistral；p6 没有模型判断就保留
assert sx.last_consistency["dropped"] == 1 and sx.last_consistency["rewritten"] == 1
llm = LLM(consist="不是 JSON", refix=refix)
sx, _ = run(llm); assert len(sx._final_evs) == 10 and "p6" in [e["id"] for e in sx._final_evs]
print("模型判断失败/格式不对 → 降级只跑规则，照常推送 ✅")

# ---- 重写也挂了 → 原标题压缩（原标题天然有依据）----
class NoFix(LLM):
    def chat(self, prompt, system=None, **kw):
        if "和原文对不上" in prompt:
            raise RuntimeError("down")
        return super().chat(prompt, system, **kw)
sx, _ = run(NoFix(consist='{"bad": []}'))
fin = sx._final_evs
assert len(fin) == 10 and "p2" in [e["id"] for e in fin]               # Mistral 改用原标题压缩 → 通过
print("重写失败 → 原标题压缩 ✅")

# ---- 开关关掉：不核对、不多调模型 ----
llm = LLM(refix=refix)
sx, _ = run(llm, env="0")
assert llm.calls["consist"] == 0 and llm.calls["refix"] == 0 and len(sx._final_evs) == 10 and "p2" in [e["id"] for e in sx._final_evs]
os.environ.pop("BOT_AIHOT_CONSISTENCY", None)
print("BOT_AIHOT_CONSISTENCY=0 关闭 ✅")
