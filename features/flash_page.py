"""AI 快讯「按期详情页」：每次整点推送生成一页，只含这一期推送的那几条，编号/标题/顺序与群消息完全一致。

页面模式（标题+导语+编号短条+详情）：热点时间线 · 整点精选 / 小时快讯 · Top N / 北京时间 X 年 X 月 X 日 X 时 / 导语，
然后 01、02… 每条：短标题（与推送一致）、一两句摘要、来源（原文链接）· 多久前 · 时间线（aihot 站 /story/<id>）。
托管：aihot.example.com/flash/<期号>.html（服务器 nginx 静态目录 <静态目录>/）。
上传：HTTPS PUT /_flash_put/<期号>.html，头 X-Flash-Key = sha256("aihot-flash-v1:" + AIHOT_INGEST_TOKEN)[:32]（nginx map 校验，不落盘、不打印）。
期号：YYYYMMDD-HH-<这期事件 id 的短哈希>，两个群推的同一批事件共用一页。
"""
from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import time
from datetime import datetime, timezone

log = logging.getLogger("feature.flash_page")
SITE = os.environ.get("BOT_AIHOT_API", "https://aihot.example.com").rstrip("/")


def issue_id(now: datetime, ids: list[str]) -> str:
    h = hashlib.sha1("|".join(ids).encode()).hexdigest()[:6]
    return f"{now.strftime('%Y%m%d-%H')}-{h}"


def page_url(issue: str) -> str:
    return f"{SITE}/flash/{issue}.html"


def _summary(text: str, n: int = 110) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    out = ""
    for i, s in enumerate(re.findall(r"[^。！？!?]+[。！？!?]?", t)):          # 一两句，≤n 字
        if i >= 2 or (out and len(out) + len(s) > n):
            break
        out += s
        if len(out) >= n * 0.6:
            break
    if len(out) > n:
        out = out[:n - 1].rstrip("，,、；; ") + "…"
    return out


def _parse_t(v) -> float | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _ago(ts: float | None, now_ts: float) -> str:
    if ts is None:
        return ""
    d = max(0, now_ts - ts)
    if d < 3600:
        return f"{max(1, int(d // 60))} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    return f"{int(d // 86400)} 天前"


def _source(ev: dict) -> tuple[str, str, float | None]:
    srcs = ev.get("sources") or []
    s0 = srcs[0] if srcs else {}
    name = (s0.get("name") or "").strip() or "来源"
    url = s0.get("url") or ""
    ts = _parse_t(s0.get("published_at")) or _parse_t(ev.get("time"))
    return name, url, ts


CSS = """
:root{--bg:#f6f3ee;--fg:#1d1d1f;--mut:#6b6b6b;--acc:#c0392b;--line:#e3ded6}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#ececec;--mut:#9a9a9a;--acc:#e0644f;--line:#2c2c2c}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans SC","Microsoft YaHei",sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:18px 18px 40px}
.brand{display:flex;align-items:center;justify-content:space-between;padding-bottom:14px;border-bottom:1px solid var(--line);
 font:600 19px/1 Georgia,"Songti SC","Noto Serif SC",serif;letter-spacing:.08em}
.brand a{color:inherit;text-decoration:none}.brand small{font:13px/1 -apple-system,sans-serif;color:var(--mut);letter-spacing:0}
.kicker{margin:22px 0 6px;color:var(--acc);font-size:13px;font-weight:600;letter-spacing:.12em}
h1{margin:0 0 8px;font:700 26px/1.3 Georgia,"Songti SC","Noto Serif SC",serif}
.when{color:var(--mut);font-size:15px;margin-bottom:10px}
.lead{font-size:17px;margin:0 0 16px}
ol{list-style:none;margin:0;padding:0;border-top:2px solid var(--fg)}
li{display:flex;gap:14px;padding:16px 0;border-bottom:1px solid var(--line)}
.no{flex:0 0 28px;color:var(--mut);font:600 17px/1.5 Georgia,serif}
.bd{min-width:0;flex:1}
h2{margin:0 0 6px;font:700 18px/1.45 Georgia,"Songti SC","Noto Serif SC",serif}
h2 a{color:inherit;text-decoration:none}
.sum{margin:0 0 8px;color:var(--fg);opacity:.85;font-size:15px}
.meta{color:var(--mut);font-size:13px}.meta a{color:var(--mut)}
.meta .tl{color:var(--acc);text-decoration:none}
footer{margin-top:22px;color:var(--mut);font-size:13px;text-align:center}footer a{color:var(--mut)}
"""


def render(now: datetime, intro: str, titles: list[str], events: list[dict], brand: str = "AI 快讯") -> str:
    """titles 与群消息里的 1..n 完全一致（同一份改写结果），events 同序。"""
    e = html.escape
    now_ts = now.timestamp()
    items = []
    for n, (t, ev) in enumerate(zip(titles, events), 1):
        name, url, ts = _source(ev)
        ts = ev.get("_pub") or ts                                   # 「多久前」按原始发布时间
        story = ev.get("url") or (f"{SITE}/story/{ev['id']}" if ev.get("id") else "")
        when = ""
        if ts:
            when = datetime.fromtimestamp(ts, now.tzinfo or timezone.utc).strftime("%m-%d %H:%M")
        meta = [f'<a href="{e(url)}" rel="noopener">{e(name)}</a>' if url else e(name)]
        ago = _ago(ts, now_ts)
        if ago and ev.get("_est"):                                  # 估计时间（转载/入库时间）如实标「约」
            ago, when = "约" + ago, "估计（转载/入库时间）" + when
        if ago:
            meta.append(f'<span title="{e(when)}">{e(ago)}</span>')
        try:
            from features.ai_news import claim_label
            c = claim_label(ev)
        except Exception:
            c = ""
        if c:
            meta.append(f'<span class="claim">{e(c)}</span>')           # 高风险消息标「据称」
        if story:
            meta.append(f'<a class="tl" href="{e(story)}">时间线 ›</a>')
        head = f'<a href="{e(story)}">{e(t)}</a>' if story else e(t)
        sm = ev.get("_sum") or _summary(ev.get("summary") or "")      # 优先用我们自己改写的摘要
        items.append(f'<li><div class="no">{n:02d}</div><div class="bd"><h2>{head}</h2>'
                     + (f'<p class="sum">{e(sm)}</p>' if sm else "")
                     + f'<div class="meta">{" · ".join(meta)}</div></div></li>')
    when = f"北京时间 {now.year} 年 {now.month} 月 {now.day} 日 {now.hour} 时"
    title = f"小时快讯 · {now.month} 月 {now.day} 日 {now.hour} 时 · {brand}"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex">
<title>{e(title)}</title>
<meta property="og:title" content="{e(title)}"><meta property="og:description" content="{e(intro or when)}">
<style>{CSS}</style></head>
<body><div class="wrap">
<div class="brand"><a href="{e(SITE)}/">{e(brand)}</a><small>{e(SITE.split('//')[-1])}</small></div>
<div class="kicker">热点时间线 · 整点精选</div>
<h1>小时快讯 · Top {len(items)}</h1>
<div class="when">{e(when)}</div>
{f'<p class="lead">{e(intro)}</p>' if intro else ''}
<ol>
{chr(10).join(items)}
</ol>
<footer>本期即群里推送的 {len(items)} 条，编号一一对应；来源标注原始出处 · <a href="{e(SITE)}/">热点时间线</a></footer>
</div></body></html>
"""


def _key() -> str:
    from features.ai_news import _secret
    return hashlib.sha256(("aihot-flash-v1:" + _secret("AIHOT_INGEST_TOKEN")).encode()).hexdigest()[:32]


def publish(issue: str, page: str, tries: int = 3) -> str:
    """PUT 到服务器静态目录，返回公开地址；失败抛 RuntimeError（调用方回退）。"""
    import requests
    if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", issue):
        raise RuntimeError("期号不合法")
    key, last = _key(), ""
    for i in range(tries):
        try:
            r = requests.put(f"{SITE}/_flash_put/{issue}.html", data=page.encode("utf-8"),
                             headers={"X-Flash-Key": key, "Content-Type": "text/html; charset=utf-8"}, timeout=(5, 20))
            if r.status_code in (201, 204):
                return page_url(issue)
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 413):
                break
        except requests.RequestException as e:
            last = type(e).__name__
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"详情页上传失败: {last}")
