"""AI 快讯按期详情页：页面条目 = 群推送条目（编号/标题/顺序一致）、同批共用一页、上传失败回退、夜间不推不变。全 mock。"""
import sys, re, json, tempfile, os
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("BOT_AIHOT_DETAIL_URL", "BOT_AIHOT_BRAND"):
    os.environ.pop(k, None)
os.environ["BOT_AIHOT_LLM_DEDUP"] = "0"     # 语义去重单独测（test_news_fill）
os.environ["BOT_AIHOT_LLM_VALUE"] = "0"     # 价值打分单独测（test_news_fill）
os.environ["BOT_AIHOT_CONSISTENCY"] = "0"   # 一致性核对单独测（test_news_consistency）
from features import ai_news, flash_page
from features.settings import FeatureSettings
TZ = ai_news.TZ
R1, R2 = "20000000004@chatroom", "20000000002@chatroom"

class LLM:
    def __init__(self): self.n = 0
    def chat(self, prompt, system=None, **kw):
        self.n += 1
        items = json.loads(re.search(r"\[\{\"i\".*?\}\](?=\s|$)", prompt, re.S).group(0))
        return json.dumps({"intro": f"导语第{self.n}版。第二句。", "titles": [f"短{it['title']}这条快讯的完整标题v{self.n}" for it in items],
                           "summaries": [f"我们改写的{it['title']}摘要" for it in items]}, ensure_ascii=False)
def evs(ids):
    return [{"rank": i + 1, "id": x, "title": f"T{x}", "summary": f"这是{x}的第一句。第二句补充<细节>。第三句不该出现在摘要里吗也许。",
             "time": "2026-10-22T00:30:00Z", "url": f"https://aihot.example.com/story/{x}",
             "sources": [{"name": "Src&Co" if x == "id0" else f"媒体{x}", "url": f"https://s{x}.example/{x}?a=1&b=2", "published_at": "2026-10-22T00:40:00Z"}]}
            for i, x in enumerate(ids)]
ids = [f"id{i}" for i in range(12)]
def fetch(day): return {"date": day, "events": evs(ids)}
class NR:
    allow_targets, block_targets = {R1, R2}, set()
    def __init__(self): self.sent = []
    def deliver(self, rep): self.sent.append(rep); return True
pubs = {}
def pub(issue, page):
    pubs[issue] = page; return flash_page.page_url(issue)
st = FeatureSettings(None); st.set(R1, "ai_news", True); st.set(R2, "ai_news", True)
clock = [datetime(2026, 10, 22, 9, 1, tzinfo=TZ).timestamp()]
nr = NR(); llm = LLM()
s = ai_news.AiNewsScheduler(nr, settings=st, fetch=fetch, summarize=llm, state_path=Path(tempfile.mkdtemp()) / "s.json",
                            clock=lambda: clock[0], publish=pub)
assert s.tick() == 2
assert len(pubs) == 1 and llm.n == 1                     # 两群同一批 → 同一份改写、同一页
issue, page = next(iter(pubs.items()))
assert re.fullmatch(r"20261022-09-[0-9a-f]{6}", issue)
url = f"https://aihot.example.com/flash/{issue}.html"
for rep in nr.sent:
    msg = rep.content
    assert msg.endswith("详情:" + url), msg[-80:]
    titles = re.findall(r"^(\d+)\. (.+?)(?:（[^）]*）)?$", msg, re.M)   # 群消息短标题后可带（来源 · 时效），对比时剥掉
    page_titles = re.findall(r'<div class="no">(\d+)</div><div class="bd"><h2><a href="[^"]+">([^<]+)</a>', page)
    assert [(int(a), b) for a, b in titles] == [(int(a), b) for a, b in page_titles] and len(titles) == 10
assert "小时快讯 · Top 10" in page and "北京时间 2026 年 10 月 22 日 9 时" in page and "导语第1版。第二句。" in page
assert "Src&amp;Co" in page and "https://sid0.example/id0?a=1&amp;b=2" in page   # 转义
assert 'href="https://aihot.example.com/story/id0"' in page and "时间线 ›" in page
assert "我们改写的Tid0摘要" in page and "第一句" not in page and "21 分钟前" in page and "viewport" in page
assert "HOURLY" not in page and "AGI" not in page and "🧭" not in nr.sent[0].content
print("详情页与推送逐条一致 / 同批共用 / 转义 / 时间线链接 ✅")

# 下一小时：只剩 2 条新事件 → 新一期页面只有 2 条，编号 1..2
clock[0] = datetime(2026, 10, 22, 10, 0, 30, tzinfo=TZ).timestamp()
nr.sent.clear(); pubs.clear()
assert s.tick() == 2 and len(pubs) == 1
page = next(iter(pubs.values()))
assert "Top 2" in page and page.count('<div class="no">') == 2 and "01" in page
assert all("\n1. 短Tid10" in r.content and "\n2. 短Tid11" in r.content for r in nr.sent)

# 上传失败 → 回退首页链接，照常推送
def bad(issue, page): raise RuntimeError("HTTP 503")
ids[:] = [f"n{i}" for i in range(3)]
nr2 = NR(); s2 = ai_news.AiNewsScheduler(nr2, settings=st, fetch=fetch, summarize=False, state_path=Path(tempfile.mkdtemp()) / "s.json",
                                         clock=lambda: clock[0], publish=bad, digest=lambda d: {"share_url": ""})
assert s2.tick() == 2 and all(r.content.endswith("详情:https://aihot.example.com/") for r in nr2.sent)
# 夜间不推
clock[0] = datetime(2026, 10, 22, 23, 5, tzinfo=TZ).timestamp(); pubs.clear()
assert s.tick() == 0 and not pubs
print("新一期只含新事件 / 上传失败回退首页 / 夜间不推 ✅")

# ---- 信源多样：原始出处优先显示、同一信源每期 ≤2、只有 AGI Hunt 转述的 ≤2 且排后 ----
def ev(i, srcs, rank):
    return {"id": f"q{i}", "rank": rank, "title": f"标题{i}", "summary": "s。",
            "sources": [{"name": n, "url": f"https://{n}/{i}", "published_at": "2026-10-22T01:00:00Z"} for n in srcs]}
pool = [ev(i, ["AGI Hunt"], i) for i in range(1, 9)]                                  # 排名靠前但只有 AGI Hunt
pool += [ev(10 + i, ["AGI Hunt", "OpenAI Blog"], 10 + i) for i in range(4)]           # 有原始出处：显示 OpenAI Blog
pool += [ev(20 + i, ["AIHOT（现有）", f"媒体{i}"], 20 + i) for i in range(6)]
sx = ai_news.AiNewsScheduler(NR(), settings=st, fetch=lambda d: {"date": d, "events": pool}, summarize=False,
                             state_path=Path(tempfile.mkdtemp()) / "s.json", clock=lambda: clock[0], publish=pub)
batch = sx.new_events(None, {"events": pool})
names = [b["sources"][0]["name"] for b in batch]
from collections import Counter
cnt = Counter(names)
assert len(batch) == 10 and max(cnt.values()) <= 2, cnt
assert cnt["AGI Hunt 转述"] == 2 and names[-2:] == ["AGI Hunt 转述"] * 2 and "AGI Hunt" not in cnt   # 过渡期：转述 ≤2 条、排最后
assert cnt["OpenAI Blog"] == 2 and all(not n.startswith("AIHOT") for n in names)      # 原始出处优先、去掉「（现有）」
sx.mark_global(batch)
assert not {b["id"] for b in batch} & {b["id"] for b in sx.new_events(None, {"events": pool})}   # 全局去重
# 旧闻不推：原始发布时间超过 24h / 取不到 → 不进；近 6 小时优先
def ev2(i, pub, rank):
    return {"id": f"z{i}", "rank": rank, "title": f"新旧{i}", "summary": "s。",
            "sources": [{"name": f"源{i}", "url": f"https://z/{i}", "published_at": pub}]}
iso = lambda h: datetime.fromtimestamp(clock[0] - h * 3600, TZ).isoformat()
pool2 = [ev2(1, iso(30), 1), ev2(2, None, 2), ev2(3, iso(10), 3), ev2(4, iso(2), 4), ev2(5, iso(0.5), 5), ev2(6, iso(80), 6)]
b2 = sx.new_events(None, {"events": pool2})
assert [b["id"] for b in b2] == ["z5", "z4", "z3", "z1"], [b["id"] for b in b2]   # 15:13 起：越新越前，24–72h 补位，>72h/无时间不推
pg = flash_page.render(datetime.fromtimestamp(clock[0], TZ), "", ["a"], [b2[1]])
assert "2 小时前" in pg
print("信源多样：原始出处优先 / 单信源 ≤2 / 聚合转述不推 / 全局去重 / 旧闻(>24h/无时间)不推 / 近 6h 优先 / 多久前按原始发布时间 ✅")

# ---- 改写按 i 对齐：模型把顺序打乱也不会张冠李戴；超长原标题取第一个分句 ----
class Shuffled:
    def chat(self, prompt, system=None, **kw):
        return json.dumps({"intro": "x。", "items": [{"i": 3, "title": "第三条新闻的完整短标题", "summary": "三"}, {"i": 1, "title": "第一条新闻的完整短标题", "summary": "一"}]},
                          ensure_ascii=False)
es = [{"title": "A"}, {"title": "B"}, {"title": "很长的原标题很长的原标题很长的原标题，后半句"}]
intro, ts, _ = ai_news.rewrite(Shuffled(), es)
assert ts[0] == "第一条新闻的完整短标题" and ts[2] == "第三条新闻的完整短标题" and ts[1] == "B" and es[0]["_sum"] == "一" and es[2]["_sum"] == "三"
_, ts2, _ = ai_news.rewrite(None, es)
print("改写按 i 对齐 ✅")

# ---- 信源黑名单 + 短标题完整性 ----
os.environ.pop("BOT_AIHOT_SRC_BLOCK", None)
assert ai_news.src_block() == []                                                  # 默认不拉黑（主人 12:59）
os.environ["BOT_AIHOT_SRC_BLOCK"] = "人人都是产品经理,woshipm"                     # 配置项仍可用
e = ai_news.primary_first({"id": "b1", "title": "x", "sources": [
    {"name": "人人都是产品经理", "url": "https://www.woshipm.com/ai/1.html", "published_at": "2026-10-22T01:00:00Z"},
    {"name": "某博客", "url": "https://blog.woshipm.com/x", "published_at": "2026-10-22T01:00:00Z"}]})
assert e["sources"] == [] and e["_pub"] is None and e["_agg_only"]                 # 名字或域名命中都剔除 → 不推
os.environ["BOT_AIHOT_SRC_BLOCK"] = "foo"
assert ai_news.primary_first({"id": "b2", "sources": [{"name": "人人都是产品经理", "url": "", "published_at": "2026-10-22T01:00:00Z"}]})["sources"]
os.environ.pop("BOT_AIHOT_SRC_BLOCK")
src = "NVIDIA 推出 Agile ONE SNX 人形机器人，Isaac Lab 3.0"
assert not ai_news.title_ok("NVIDIA推出Agile ONE SN", src)                         # 半个英文词
assert ai_news.title_ok("NVIDIA推出Agile ONE SNX", src) and ai_news.title_ok("Isaac Lab 3.0发布", src)
assert not ai_news.title_ok("OpenAI发布新模型与", "") and not ai_news.title_ok("《三体", "") and not ai_news.title_ok("截断了…", "")
assert ai_news.compress_title("NVIDIA 扩展 Isaac 机器人平台，新增 Agile ONE SN") == "NVIDIA 扩展 Isaac 机器人平台，新增 Agile ONE SN"   # 10-25 起：不太长就整句用，不截
# 13:00 那期复现：原标题/摘要（aihot 原文）
O13 = "NVIDIA 扩展 Isaac 机器人平台，新增 Agile ONE SN、Isaac Lab 3.0 与开源工作流"
S13 = O13 + " NVIDIA 在 GTC Berlin 扩展 Isaac 开放机器人开发平台。Agile ONE SN 预配置 Isaac GR00T 与 Jetson Thor，配备 31 自由度身体。"
T = ai_news.title_check
assert T("NVIDIA推出31自由度Agile ONE SN", S13, O13) == "soft"           # 以孤立大写片段收尾 → 先让模型重写
assert T("NVIDIA推出31自由度Agile ONE S", S13, O13) == "bad"             # 原文里没有完整的「S」→ 截断碎片
assert T("NVIDIA推出31自由度Agile ON", S13, O13) == "bad"                # 半个单词
assert T("NVIDIA 扩展 Isaac 机器人平台，新增 Agile O", S13, O13) == "bad"  # 原标题前缀、断在词中间
assert T("NVIDIA 扩展 Isaac 机器人平台", S13, O13) == "ok"                # 原标题前缀但断在词边界
assert T("NVIDIA推出31自由度人形机器人", S13, O13) == "ok"
assert T("NVIDIA 发布 Isaac Lab 3.0", S13, O13) == "ok" and T("英伟达开源 GR00T", S13, O13) == "ok"
assert ai_news.compress_title("英伟达发布人形机器人参考设计 Agile ONE SN 与更多平台更新内容") == "英伟达发布人形机器人参考设计 Agile ONE SN 与更多平台更新内容"
class Fix13:
    def __init__(self, fixed): self.fixed, self.n = fixed, 0
    def chat(self, prompt, system=None, **kw):
        self.n += 1
        if self.n == 1:
            return json.dumps({"intro": "", "items": [{"i": 1, "title": "NVIDIA推出31自由度Agile ONE SN", "summary": "s"}]}, ensure_ascii=False)
        return json.dumps({"items": [{"i": 1, "title": self.fixed}]}, ensure_ascii=False)
ev13 = [{"id": "n13", "title": O13, "summary": S13[len(O13):]}]
f = Fix13("NVIDIA推出31自由度人形机器人Agile ONE SN")   # 模型重写仍以缩写收尾（soft）→ 不比原来好，但仍可接受
_, t13, _ = ai_news.rewrite(f, [dict(x) for x in ev13]); assert f.n == 2 and t13 == ["NVIDIA推出31自由度Agile ONE SN"], t13
f = Fix13("英伟达推出人形机器人Agile ONE SN参考机")
_, t13, _ = ai_news.rewrite(f, [dict(x) for x in ev13]); assert t13 == ["英伟达推出人形机器人Agile ONE SN参考机"], t13
class Bad13(Fix13):
    def chat(self, prompt, system=None, **kw):
        self.n += 1
        t = "NVIDIA推出31自由度Agile ONE S" if self.n == 1 else "NVIDIA推出Agile O"
        return json.dumps({"intro": "", "items": [{"i": 1, "title": t, "summary": "s"}]}, ensure_ascii=False)
f = Bad13("")
_, t13, _ = ai_news.rewrite(f, [dict(x) for x in ev13]); assert f.n == 2 and t13 == ["NVIDIA 扩展 Isaac 机器人平台"], t13   # 两次都截断 → 原标题按词边界压缩
print("短标题截断（13:00 Agile ONE SN 复现）：识别、重写一次、再不行按词边界压缩 ✅")

assert not ai_news.compress_title("Anthropic releases a very long English headline about Claude models").endswith("Clau")
class Trunc:
    n = 0
    def chat(self, prompt, system=None, **kw):
        Trunc.n += 1
        if Trunc.n == 1:
            return json.dumps({"intro": "", "items": [{"i": 1, "title": "NVIDIA推出Agile ONE SN", "summary": "s"}]}, ensure_ascii=False)
        return json.dumps({"items": [{"i": 1, "title": "NVIDIA推出人形机器人Agile ONE SNX"}]}, ensure_ascii=False)
_, tt, _ = ai_news.rewrite(Trunc(), [{"title": "NVIDIA 推出 Agile ONE SNX 人形机器人", "summary": src}])
assert tt == ["NVIDIA推出人形机器人Agile ONE SNX"] and Trunc.n == 2
print("信源黑名单（名字/域名、可配置）/ 短标题完整性校验 + 重写 + 原标题压缩 ✅")

# ---- 过渡期 AGI Hunt 转述：解析 X 作者、转载时间 24h 内、上限可配 ----
assert ai_news.hunt_author({"summary": "TheZvi 转发 Anthropic 公共利益报告中的一段话"}) == ("TheZvi（X）", "https://x.com/TheZvi")
assert ai_news.hunt_author({"summary": "研究员 @LesterMackey 发帖称…"})[0] == "LesterMackey（X）"
assert ai_news.hunt_author({"summary": "MIT 研究团队开发出一种技术", "sources": [{"name": "AGI Hunt", "url": "https://agihunt.info/p/1"}]}) == ("AGI Hunt 转述", "https://agihunt.info/p/1")
hp = [{"id": f"h{i}", "rank": i, "title": f"转述{i}", "summary": "TheZvi 转发 x。" if i == 1 else "某团队发布 y。",
       "sources": [{"name": "AGI Hunt", "url": f"https://agihunt.info/p/{i}", "published_at": iso(h)}]} for i, h in ((1, 3), (2, 30), (3, 5), (4, 1))]
hb = sx.new_events(None, {"events": hp})
assert [b["id"] for b in hb] == ["h4", "h1"] and hb[1]["sources"][0]["name"] == "TheZvi（X）"   # 越新越前；补位也受单信源上限 2
assert sx.last_diag["单信源上限"] == 2 and sx.last_diag["AGG_ONLY 上限（先让位）"] == 2
sx.mark_global(hb); assert "https://x.com/TheZvi" not in sx.seen_urls()
ai_news.AGG_ONLY_CAP = 0; assert sx.new_events(None, {"events": hp}) == []; ai_news.AGG_ONLY_CAP = 2
print("过渡期 AGI Hunt 转述：作者解析 / 24h / 上限可配 / 排在原始出处后 ✅")
assert ai_news.hunt_author({"summary": "经济学家 Daniel Rock 表示，新一代模型", "sources": [{"name": "AGI Hunt", "url": "https://agihunt.info/p/9"}]}) == ("Daniel Rock（X）", "https://agihunt.info/p/9")
assert ai_news.hunt_author({"summary": "OpenAI 发布新模型"})[0] == "AGI Hunt 转述"
print("作者解析：人名 / 公司发布不当作者 ✅")


# ---- 单信源每期 ≤2：对所有信源生效（人人都是产品经理 / 雷达侦探精选 / AGI Hunt 转述 / AWS 博客）----
os.environ.pop("BOT_AIHOT_SRC_BLOCK", None)
def evx(i, name, url, h=2, summ="某公司发布 x。"):
    return {"id": f"c{i}", "rank": i, "title": f"刷屏{i}", "summary": summ,
            "sources": [{"name": name, "url": url, "published_at": iso(h)}]}
flood = ([evx(i, "人人都是产品经理", f"https://www.woshipm.com/{i}") for i in range(1, 6)]
         + [evx(10 + i, "AWS Machine Learning Blog", f"https://aws.amazon.com/blogs/{i}") for i in range(5)]
         + [evx(20 + i, "雷达侦探精选", f"https://claude.com/r/{i}") for i in range(3)]
         + [evx(30 + i, "雷达侦探精选", "") for i in range(3)]
         + [evx(40 + i, "AGI Hunt", f"https://agihunt.info/p/{i}", summ=f"User{i} 发帖称 y。") for i in range(5)]
         + [evx(50 + i, f"独立媒体{i}", f"https://m{i}.example/x") for i in range(10)])
fb = sx.new_events(None, {"events": flood})
shown = Counter(b["sources"][0]["name"] for b in fb)
via = Counter(b["sources"][0].get("_via") for b in fb if b["sources"][0].get("_via"))
assert len(fb) == 10 and max(shown.values()) <= 2, shown
assert shown["人人都是产品经理"] == 2 and shown["AWS Machine Learning Blog"] == 2, shown        # 不拉黑但限 2
assert shown["Anthropic"] == 2 and via["雷达侦探精选"] == 2 and "雷达侦探精选" not in shown          # 汇总号显示原始出处，本身也计数
fb2 = sx.new_events(None, {"events": [e for e in flood if e["id"] >= "c30" and e["id"] < "c50"]})
n2 = Counter(b["sources"][0]["name"] for b in fb2)
assert n2["雷达侦探精选"] == 2, n2                                                       # 取不到原始出处 → 正常显示、限 2
hunt = [b for b in fb2 if b["_agg_only"]]
assert len(hunt) == 2 and all(b["sources"][0]["name"].endswith("（X）") for b in hunt)    # AGI Hunt 转述合计 ≤2（作者各不相同也算一家）
ai_news.AGG_ONLY_CAP = 5; fb3 = sx.new_events(None, {"events": flood[16:21]}); ai_news.AGG_ONLY_CAP = 2
assert sum(b["_agg_only"] for b in fb3) == 2                                            # 即便过渡上限放宽，单信源上限照样卡 2
print("单信源每期 ≤2：人人都是产品经理 / AWS / 雷达侦探精选（显示原始出处、本身计数）/ AGI Hunt 转述 ✅")
