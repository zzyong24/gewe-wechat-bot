"""当前北京时间提示：陪聊 / 插话 / 欢迎调模型时注入 system prompt，让小助手知道今天几号、星期几、几点（附农历、节假日）。"""
from __future__ import annotations

import datetime as _dt

TZ = _dt.timezone(_dt.timedelta(hours=8))
WEEK = "一二三四五六日"
_HOLI = {"New Year's Day": "元旦", "Spring Festival": "春节", "Tomb-sweeping Day": "清明节", "Labour Day": "劳动节",
         "Dragon Boat Festival": "端午节", "National Day": "国庆节", "Mid-autumn Festival": "中秋节",
         "Anti-Fascist 70th Day": "抗战胜利纪念日"}
_LM = "正二三四五六七八九十冬腊"
_LD = ["初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
       "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
       "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十"]


def lunar(d: _dt.date) -> str:
    try:
        import lunardate
        ld = lunardate.LunarDate.fromSolarDate(d.year, d.month, d.day)
        return f"农历{'闰' if ld.isLeapMonth else ''}{_LM[ld.month - 1]}月{_LD[ld.day - 1]}"
    except Exception:
        return ""


def day_kind(d: _dt.date) -> str:
    try:
        import chinese_calendar as cc
        on, name = cc.get_holiday_detail(d)
        cn = _HOLI.get(name or "", "")
        if on:
            return f"{cn}假期" if cn else ("周末" if d.weekday() >= 5 else "休息日")
        return f"调休上班日（{cn}）" if (cn and d.weekday() >= 5) else "工作日"
    except Exception:                      # 库没覆盖的年份：只按周末判断
        return "周末" if d.weekday() >= 5 else "工作日"


def now_line(now: _dt.datetime | None = None) -> str:
    n = (now or _dt.datetime.now(TZ)).astimezone(TZ)
    d = n.date()
    extra = "，".join(x for x in (lunar(d), day_kind(d)) if x)
    return (f"（现在是北京时间 {n.year}年{n.month}月{n.day}日 星期{WEEK[n.weekday()]} {n:%H:%M}"
            f"{'，' + extra if extra else ''}。说到今天/明天/星期几/几点以这个为准。）")
