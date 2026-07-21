"""
纳入评审日历 + 三段提醒。各指数评审月份固定, 据此推算 公告日/生效日, 在三个时点提醒去抓名单:
  ①即将发公告(announce-5d ~ announce-1d)  ②公告窗口(announce ~ announce+3d, 去抓名单)  ③已公告待生效(~effective前)
日期为规则推算(近似, 提醒用), 节假日可能差1-2天。

评审日历:
  MSCI 季度评审(QIR): 2月/8月, 公告~月中第二个周四, 生效=月末
  MSCI 半年评审(SAIR): 5月/11月, 同上(规模更大)
  富时罗素 China A: 3月/9月, 生效=第三个周五, 公告~生效前约2周
  中证(沪深300/500/1000/A500等): 6月/12月, 生效=第二个周五次一交易日, 公告~前一月第二个周五

输出 data/inclusion_calendar.json (下次各评审 + 今日提醒). 跑: python scripts/inclusion_calendar.py
"""
import os, json, calendar
from datetime import date, datetime, timedelta

DATA_DIR = os.path.dirname(os.path.abspath(__file__)) + "/../data"
OUT = os.path.join(DATA_DIR, "inclusion_calendar.json")
TODAY = datetime.strptime(os.environ.get("INCLUSION_TODAY", datetime.now().strftime("%Y-%m-%d")), "%Y-%m-%d").date()


def nth_weekday(y, m, weekday, n):
    """第n个星期weekday(0=周一..4=周五)的日期."""
    cnt = 0
    for d in range(1, calendar.monthrange(y, m)[1] + 1):
        if date(y, m, d).weekday() == weekday:
            cnt += 1
            if cnt == n:
                return date(y, m, d)
    return None


def last_bday(y, m):
    d = calendar.monthrange(y, m)[1]
    dt = date(y, m, d)
    while dt.weekday() >= 5:
        dt -= timedelta(days=1)
    return dt


def next_td(dt):
    dt += timedelta(days=1)
    while dt.weekday() >= 5:
        dt += timedelta(days=1)
    return dt


# 各 provider: months=评审月; announce/effective 推算函数
def reviews_for_year(y):
    out = []
    for m in (2, 8):   # MSCI 季度
        a = nth_weekday(y, m, 3, 2)  # 第二个周四(近似公告日)
        out.append(("MSCI中国(季度)", a, last_bday(y, m)))
    for m in (5, 11):  # MSCI 半年
        a = nth_weekday(y, m, 3, 2)
        out.append(("MSCI中国(半年)", a, last_bday(y, m)))
    for m in (3, 9):   # 富时罗素
        eff = nth_weekday(y, m, 4, 3)  # 第三个周五生效
        out.append(("富时罗素中国", eff - timedelta(days=14), eff))
    for m in (6, 12):  # 中证(境内宽基)
        eff = next_td(nth_weekday(y, m, 4, 2))  # 第二个周五次一交易日
        am = m - 1
        out.append(("中证宽基(沪深300/500/1000/A500)", nth_weekday(y, am, 4, 2), eff))
    return out


def main():
    allrev = reviews_for_year(TODAY.year) + reviews_for_year(TODAY.year + 1)
    # 每个 provider 取"下一次生效日在今天及以后"的最近一期
    nextrev = {}
    for name, ann, eff in allrev:
        if eff and eff >= TODAY and (name not in nextrev or eff < nextrev[name][1]):
            nextrev[name] = (ann, eff)

    alerts = []
    cal_out = []
    for name, (ann, eff) in sorted(nextrev.items(), key=lambda x: x[1][1]):
        stage = None
        if ann:
            if ann - timedelta(days=5) <= TODAY < ann:
                stage = ("soon", f"⏰ {name} 评审公告即将发布(预计 {ann})。准备好去抓官方调入名单。")
            elif ann <= TODAY <= ann + timedelta(days=3):
                stage = ("announce", f"📢 {name} 评审公告窗口({ann})!立即去官网抓调入名单 → 填 fetch_foreign_inclusion 源/upcoming_inclusions, 生效日 {eff}。")
            elif ann + timedelta(days=3) < TODAY < eff:
                stage = ("pending", f"🎯 {name} 已公告待生效(生效 {eff})。确认抢跑清单, T-1尾盘前布好仓。")
        cal_out.append({"name": name, "announce": ann.isoformat() if ann else None,
                        "effective": eff.isoformat(), "days_to_effective": (eff - TODAY).days,
                        "stage": stage[0] if stage else None})
        if stage:
            alerts.append({"name": name, "stage": stage[0], "msg": stage[1]})

    out = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"), "today": TODAY.isoformat(),
           "reviews": cal_out, "alerts": alerts}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for r in cal_out:
        print(f"[cal] {r['name']:32} 公告~{r['announce']} 生效{r['effective']} (还{r['days_to_effective']}天){' ⚠️'+r['stage'] if r['stage'] else ''}")
    for a in alerts:
        print("  ALERT:", a["msg"])
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
