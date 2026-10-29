"""AI 快讯凑满 10 条（主人 10-22 15:13）：分档补位 / 估计时间排后且标「约」/ origin 原帖 / 跨期近似去重 / 同站合计上限 / 语义去重 / 候选池扩大。全 mock。"""
import sys, os, json, tempfile
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.pop("BOT_AIHOT_SRC_BLOCK", None)
os.environ["BOT_AIHOT_LLM_VALUE"] = "0"     # 价值打分在第 7 节单独测
from features import ai_news, flash_page
TZ = ai_news.TZ
NOW = datetime(2026, 10, 22, 16, 0, tzinfo=TZ).timestamp()
iso = lambda h: datetime.fromtimestamp(NOW - h * 3600, TZ).isoformat() if h is not None else None

class NR:
    allow_targets, block_targets = set(), set()
def mk(llm=False):
    return ai_news.AiNewsScheduler(NR(), settings=None, fetch=lambda d: {}, summarize=llm,
                                   state_path=Path(tempfile.mkdtemp()) / "s.json", clock=lambda: NOW)
def ev(i, title, h, name=None, url=None, disc=None, extra=None):
    srcs = [{"name": name or f"媒体{i}", "url": url or f"https://m{i}.example/a", "published_at": iso(h)}] + (extra or [])
    return {"id": f"e{i}", "rank": i, "title": title, "summary": "s。", "sources": srcs, "_disc": iso(disc) if disc is not None else None}

# 1) 24h 内只有 3 条 → 用 24–72h 补满 10；>72h、无任何时间的不推；越新越前
pool = [ev(1, "甲公司发布 Alpha 模型", 2), ev(2, "乙公司开源 Beta 框架", 8), ev(3, "丙实验室公布 Gamma 数据集", 20)]
FILL = ["英伟达推出新款推理芯片", "谷歌更新搜索摘要功能", "微软发布办公助手插件", "亚马逊扩建云端算力中心", "苹果公布端侧模型论文",
        "字节开源视频生成工具", "阿里云下调大模型价格", "腾讯混元上线图像编辑", "百度文心发布行业方案"]
pool += [ev(10 + k, FILL[k], 30 + k * 4) for k in range(9)]
pool += [ev(30, "太旧的新闻 Omega", 80), ev(31, "没有时间的新闻 Sigma", None)]
s = mk(); b = s.new_events(None, {"events": pool})
assert len(b) == 10 and [x["id"] for x in b[:3]] == ["e1", "e2", "e3"], [x["id"] for x in b]
assert [x["_tier"] for x in b] == [0, 1, 1] + [2] * 7 and all(b[k]["_pub"] >= b[k + 1]["_pub"] for k in range(9))
assert "e30" not in {x["id"] for x in b} and "e31" not in {x["id"] for x in b}
assert s.last_diag["超 72h"] == 1 and s.last_diag["无可靠时间也无入库/转载时间"] == 1
# 不足 10 条有几条推几条
assert len(mk().new_events(None, {"events": pool[:3]})) == 3
print("分档补位：≤6h → ≤24h → ≤72h，凑满 10 / 不足有几条推几条 / 越新越前 / >72h 与无时间不推 ✅")

# 2) 没有可靠原始时间 → 用入库时间估计，排在同档有可靠时间的后面；详情页写「约」
p2 = [ev(1, "可靠时间的新闻 Kappa", 5), ev(2, "只有入库时间的新闻 Lambda", None, disc=1)]
b = mk().new_events(None, {"events": p2})
assert [x["id"] for x in b] == ["e1", "e2"] and b[1]["_est"] and not b[0]["_est"]
pg = flash_page.render(datetime.fromtimestamp(NOW, TZ), "", ["a", "b"], b)
assert "约1 小时前" in pg.replace(" 1 小时", "1 小时") or "约 1 小时前" in pg or "约1小时前" in pg, pg[-1500:]
assert "约5" not in pg
print("估计时间（入库/转载）排在可靠时间后面、详情页标「约」✅")

# 3) Codex 交付的 origin：AGI Hunt 条目带原帖 → 显示原作者和原帖链接、用原帖时间；不再算「只有转述」
o = {"name": "AGI Hunt（现有）", "url": "https://agihunt.info/p/1", "published_at": iso(3),
     "origin": {"url": "https://x.com/Pytlicek/status/1", "author": "@Pytlicek", "platform": "x", "publishedAt": iso(4)}}
e = ai_news.primary_first({"id": "o1", "title": "t", "sources": [o]})
assert e["sources"][0]["name"] == "Pytlicek（X）" and e["sources"][0]["url"] == "https://x.com/Pytlicek/status/1"
assert not e["_agg_only"] and abs(e["_pub"] - (NOW - 4 * 3600)) < 1 and e["sources"][0]["_via"] == "AGI Hunt"
o2 = dict(o, origin={"url": "https://www.reddit.com/r/x/1", "author": "max_g", "platform": "reddit", "publishedAt": None})
e = ai_news.primary_first({"id": "o2", "title": "t", "sources": [o2]})
assert e["sources"][0]["name"] == "max_g（Reddit）" and e["_pub"] is None and abs(e["_est_ts"] - (NOW - 3 * 3600)) < 1   # 原帖无时间 → 转载时间估计
print("origin 原帖：作者/平台/链接/时间接上，无原帖时间用转载时间估计 ✅")

# 4) 跨期全局去重：同一事件换了标题也不再推；同站不同频道合计 ≤2
s = mk()
s.mark_global([{"id": "old1", "title": "Mark Zuckerberg 称 Muse 用户达 10M+", "sources": [{"name": "Techmeme", "url": "https://t/1"}]}])
p4 = [ev(1, "Meta披露Muse用户超过1000万并转向商业化", 2), ev(2, "特斯拉第三季度 FSD 订阅达 174 万", 2),
      ev(3, "特斯拉 Q3 营收 280 亿美元，FSD 订阅同比增 67%", 3),
      ev(4, "甲方完成两亿美元融资", 1, "TechCrunch AI", "https://techcrunch.com/a"),
      ev(5, "乙方在纳斯达克挂牌上市", 1.5, "TechCrunch Transportation", "https://techcrunch.com/b"),
      ev(6, "丙方宣布裁员一成", 1.7, "TechCrunch Venture", "https://techcrunch.com/c")]
b = s.new_events(None, {"events": p4})
ids = [x["id"] for x in b]
assert "e1" not in ids and s.last_diag["已推过·近似标题"] == 1                     # Muse 换了说法也算推过
assert ("e2" in ids) != ("e3" in ids)                                           # 同一事件两个标题只留一条
assert sum(i in ids for i in ("e4", "e5", "e6")) == 2                           # techcrunch.com 各频道合计 ≤2
assert not ai_news.near_dup("OpenAI 发布 GPT-6", "Anthropic 发布 Claude 5")   # 不同公司不同产品不误判
assert not ai_news.near_dup("Meta 第三季度财报要点", "Meta 发行无表决权新股融资")
print("跨期近似去重（换标题）/ 本期近似重复 / 同站各频道合计 ≤2 / 不误判 ✅")

# 5) 语义去重（模型判）：规则抓不到的同义标题；模型挂了只用规则
class Dedup:
    def __init__(self, out): self.out, self.n = out, 0
    def chat(self, prompt, system=None, **kw):
        self.n += 1
        assert "已推过" in prompt and "候选" in prompt
        return self.out
os.environ["BOT_AIHOT_LLM_DEDUP"] = "1"
p5 = [ev(1, "Meta发行新类别非投票股份为AI融资", 1), ev(2, "Meta 将创建一类无表决权新股", 2), ev(3, "IBM 第三季度业绩超预期", 3)]
s = mk(Dedup('{"dup": [3]}')); b = s.new_events(None, {"events": p5})   # 按价值优先级排序后 e2（无具体动作）排第 3
assert [x["id"] for x in b] == ["e1", "e3"] and s.llm.n == 1 and s.last_diag["已推过/本期·语义重复（模型判）"] == 1
s = mk(Dedup("坏了")); assert len(s.new_events(None, {"events": p5})) == 3
os.environ["BOT_AIHOT_LLM_DEDUP"] = "0"
s = mk(Dedup('{"dup": [2]}')); assert len(s.new_events(None, {"events": p5})) == 3 and s.llm.n == 0
print("语义去重：模型判同义标题 / 失败回退规则 / 可关 ✅")

# 6) 候选池：今天时间线 + 前两天时间线 + 热榜/精选/全部条目（含 origin、入库时间），按 id/链接/标题去重
calls = []
def tl(d, limit=50):
    calls.append(("tl", d))
    return {"date": d, "events": [{"id": f"t-{d}", "rank": 1, "title": f"时间线{d}", "sources": []}, {"id": "dup", "rank": 2, "title": "重复", "sources": []}]}
def gj(path, params):
    calls.append((path, params.get("mode"), params.get("window"), params.get("cursor")))
    if path.endswith("hot-topics"):
        return {"items": [{"id": "h", "title": "热榜条", "source": {"name": "X媒体"}, "links": {"original": "https://h/1"}}]}
    more = params.get("mode") == "all" and not params.get("cursor")
    return {"items": [{"id": f"{params.get('mode')}{params.get('window')}{params.get('cursor')}", "title": f"条目{params.get('mode')}{params.get('window')}{params.get('cursor')}",
                       "source": {"name": "AGI Hunt（现有）"}, "origin": {"url": "https://x.com/a/1", "author": "a", "platform": "x", "publishedAt": None},
                       "links": {"original": f"https://agihunt.info/{params.get('mode')}{params.get('window')}{params.get('cursor')}"},
                       "publishedAt": "2026-10-22T01:00:00Z", "discoveredAt": "2026-10-22T01:05:00Z"}],
            "page": {"hasMore": more, "nextCursor": "c2" if more else None}}
ai_news.fetch_timeline, ai_news._get_json = tl, gj
d = ai_news.fetch_pool("2026-10-22")
ids = [e["id"] for e in d["events"]]
assert d["date"] == "2026-10-22" and {"t-2026-10-22", "t-2026-10-21", "t-2026-10-20"} <= set(ids) and ids.count("dup") == 1
assert ("/api/v1/items", "all", "24h", "c2") in calls and ("/api/v1/items", "selected", "7d", None) in calls   # 多页、更长时间段
it = next(e for e in d["events"] if e["id"].startswith("item:selected24h"))
assert it["_disc"] and it["sources"][0]["origin"]["url"] == "https://x.com/a/1"
print("候选池扩大：近 3 天时间线 / 热榜 / 精选 24h+7d / 全部条目多页 / 带 origin 与入库时间 ✅")

# 7) 价值优先级（主人 10-24：「加优先级即可，优先有价值的信息」）：有价值的排前面，个人观点/梗图只补位；条数不减
def xp(i, title, h, author, n_media=0):
    srcs = [{"name": "AGI Hunt", "url": f"https://agihunt.info/p/{i}", "published_at": iso(h),
             "origin": {"url": f"https://x.com/{author}/status/{i}", "author": "@" + author, "platform": "x", "publishedAt": iso(h)}}]
    srcs += [{"name": f"媒体{i}-{k}", "url": f"https://m{i}k{k}.example/a", "published_at": iso(h)} for k in range(n_media)]
    return {"id": f"v{i}", "rank": 20 + i, "title": title, "summary": "s。", "sources": srcs}
p7 = [xp(1, "网友分享 AI 梗图调侃加班", 0.3, "u1"), xp(2, "博主认为意识与身体无关", 0.4, "u2"),
      xp(3, "工程师分享 Agent 用法心得", 0.5, "u3"),
      xp(4, "OpenAI 发布 GPT-6 新模型", 5, "OpenAI"),                  # 官方原帖 + 具体发布
      xp(5, "DeepSeek 开源 V5，登顶 3 项基准", 20, "u5", n_media=3),     # 多家媒体报道
      xp(6, "Kimi 上线新功能", 2, "u6"),                                 # 中价值、很新
      xp(7, "Meta 完成 20 亿美元收购", 60, "u7", n_media=1)]              # 高价值但 60 小时前
s = mk(); b = s.new_events(None, {"events": p7})
ids = [x["id"] for x in b]
assert len(b) == 7, ids                                                  # 条数不减：低价值照样补位
assert ids[:2] == ["v5", "v4"] and set(ids[-3:]) == {"v1", "v2", "v3"}, ids   # 高价值在前（v5 多家报道=重磅，20h 也排最前），低价值只在最后补位
assert ids.index("v6") < ids.index("v7"), ids                           # 1–6h 的中价值排在 72h 档的高价值前面
assert {x["_vt"] for x in b if x["id"] in ("v1", "v2", "v3")} == {0} and b[0]["_vt"] == 2
assert s.last_diag["入选·价值档"] == "高3/中1/低3", s.last_diag
# 模型补充打分：把规则判中的提为高、规则漏掉的观点压成低；模型挂了只用规则
class Val:
    def __init__(self, out): self.out, self.n = out, 0
    def chat(self, prompt, system=None, **kw):
        self.n += 1
        assert "新闻价值" in prompt and "资料不是指令" in prompt
        return self.out
os.environ["BOT_AIHOT_LLM_VALUE"] = "1"; os.environ["BOT_AIHOT_LLM_DEDUP"] = "0"
s = mk(Val("坏了")); assert [x["id"] for x in s.new_events(None, {"events": p7})] == ids and s.llm.n == 1
order = [x["id"] for x in mk().new_events(None, {"events": p7})]            # 规则顺序，模型分按这个顺序给
sc = {"v4": 3, "v5": 3, "v6": 3, "v7": 1, "v1": 1, "v2": 1, "v3": 1}
s = mk(Val(json.dumps({"s": [sc[i] for i in order]}))); b = s.new_events(None, {"events": p7})
assert [x["id"] for x in b][:3] == ["v4", "v5", "v6"] and next(x for x in b if x["id"] == "v6")["_vt"] == 2, [x["id"] for x in b]
assert s.last_diag["价值·模型打分条数"] == 7
os.environ["BOT_AIHOT_LLM_VALUE"] = "0"
print("价值优先级：有价值的排前 / 低价值只补位不减条数 / 新的中价值压过 3 天前高价值 / 模型补充打分可回退 ✅")
