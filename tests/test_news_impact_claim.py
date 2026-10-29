"""快讯影响分析 / 据称标注 / 近重复与导语对齐（主人 10-26、10-28）。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.ai_news import claim_label, impact_line, near_dup, lead_ok

X = lambda n, u, a=None: dict(name=n, url=u, **({"origin": {"author": a}} if a else {}))

# —— 据称 ——
cases = [
    ({"title": "知情人士称 OpenAI 将发布 GPT-6", "summary": "", "sources": [X("The Information", "https://theinformation.com/a")]},
     "据称·仅单一来源、未见官方"),
    ({"title": "据传 Google 新模型下周上线", "summary": "", "sources": [X("A", "https://a.com/x"), X("B", "https://b.com/y")]},
     "据称·多家转述、未见官方"),
    ({"title": "OpenAI 发布 GPT-6", "summary": "据悉", "sources": [X("OpenAI", "https://x.com/OpenAI/status/1", "OpenAI")]}, ""),
    ({"title": "OpenCode披露约1.4% Console用户密钥泄露", "summary": "密钥泄露", "sources": [X("thione（X）", "https://x.com/thione/1")]}, ""),
    ({"title": "Anthropic 发布 Claude 5", "summary": "", "sources": [X("TC", "https://techcrunch.com/x")], "_rumor": True}, ""),  # 硬媒体正式发布，忽略模型误判
    ({"title": "苹果发布全新iPad mini，配备8.5英寸OLED屏", "summary": "", "sources": [X("Techmeme", "https://www.techmeme.com/x")], "_rumor": True}, ""),
    ({"title": "Anthropic据报筹备11月IPO，估值约9650亿美元", "summary": "", "sources": [X("aftahi_ai（X）", "https://x.com/a/1")], "_rumor": True},
     "据称·仅单一来源、未见官方"),
    ({"title": "某爆料称 Meta 新模型下周发布", "summary": "", "sources": [X("foo（X）", "https://x.com/foo/1")], "_rumor": True},
     "据称·仅单一来源、未见官方"),
]
for e, want in cases:
    got = claim_label(e)
    assert got == want, (e["title"], got, want)

assert impact_line([{"_impact": "开发者可免费试用"}, {}, {"_impact": "门槛降低。"}, {"_impact": "x"}]) == \
    "💡 有什么用：第1条：开发者可免费试用；第3条：门槛降低"
assert impact_line([{}, {}]) == ""

# —— 近重复：英伟达投 Meta 30 亿换角度 ——
assert near_dup("英伟达向Meta投资30亿美元并加购AI芯片",
                "英伟达扩大Muse合作并拟向Meta投资30亿美元")
assert near_dup("DeepSeek据称完成约150亿美元融资",
                "DeepSeek完成约150亿美元融资轮")
assert not near_dup("DeepSeek据称完成约150亿美元融资",
                    "DeepSeek年化营收据称突破10亿美元")  # 融资 ≠ 营收
assert not near_dup("苹果发布全新iPad mini", "苹果发布M6芯片MacBook Pro")

# —— 导语必须对本批条目成立 ——
evs = [{"title": "英伟达向Meta投资30亿美元并加购AI芯片"},
       {"title": "密歇根大学开源儿童场景安全评测基准KIDBench套件"}]
assert lead_ok("英伟达向Meta投资30亿美元，后者还将加购AI芯片。", evs)
assert not lead_ok("OpenAI公布数学、计算机科学与物理三项成果，并引入外部专家验证。", evs)
assert not lead_ok("英伟达向Meta投资30亿美元。OpenAI公布数学、计算机科学与物理三项成果。", evs)

print("快讯影响分析 / 据称 / 近重复 / 导语对齐 ✅")
