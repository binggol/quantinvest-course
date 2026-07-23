"""生成 ipo.json (打新日历/今日可申购 提醒)。tushare new_share: 今日可申购(申购日=今天) + 即将(未来7日) + 近期上市(提醒卖)。
产出 <OUT_DIR>/ipo.json (默认 C:\rdagent, 环境变量 QI_EXPORT_DIR 可覆盖; PC端 watcher 拷群晖/NAS)。
跑: python scripts/export_ipo.py   (token: 环境变量 TUSHARE_TOKEN 或 data/.tushare_token)
"""
import io,sys,os,json,time
from datetime import datetime,timedelta
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8',errors='replace')
import pandas as pd, tushare as ts
OUT_DIR=os.environ.get("QI_EXPORT_DIR", r"C:\rdagent")
def _load_token():
    t=os.environ.get("TUSHARE_TOKEN")
    if t: return t.strip()
    for p in (os.path.join(OUT_DIR,"data",".tushare_token"),
              os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),"data",".tushare_token")):
        if os.path.exists(p): return open(p,encoding="utf-8").read().strip()
    raise SystemExit("缺tushare token: 设环境变量 TUSHARE_TOKEN 或放 data/.tushare_token")
tok=_load_token()
pro=ts.pro_api(tok)
TODAY=os.environ.get("IPO_TODAY", datetime.now().strftime("%Y%m%d"))
def safe(fn,*a,**k):
    for _ in range(4):
        try: return fn(*a,**k)
        except Exception as e:
            if 'minute' in str(e) or 'limit' in str(e).lower(): time.sleep(8)
            else: return None
    return None
lo=(datetime.strptime(TODAY,"%Y%m%d")-timedelta(days=20)).strftime("%Y%m%d")
hi=(datetime.strptime(TODAY,"%Y%m%d")+timedelta(days=30)).strftime("%Y%m%d")
ns=safe(pro.new_share, start_date=lo, end_date=hi)
def board(c):
    if c.endswith(".BJ"): return "北交所"
    if c.startswith("688"): return "科创板"
    if c.startswith("3"): return "创业板"
    return "主板"
def row(r):
    c=r["ts_code"]
    return {"code":c,"sub_code":str(r.get("sub_code","")),"name":r["name"],"board":board(c),
            "price":r.get("price"),"pe":r.get("pe"),"ballot":r.get("ballot"),
            "ipo_date":str(int(r["ipo_date"])) if r.get("ipo_date")==r.get("ipo_date") else "",
            "issue_date":str(int(r["issue_date"])) if r.get("issue_date")==r.get("issue_date") else "",
            "limit_amount":r.get("limit_amount")}
today_buy=[]; soon_buy=[]; just_listed=[]
if ns is not None and len(ns):
    for _,r in ns.iterrows():
        ip=str(int(r["ipo_date"])) if r.get("ipo_date")==r.get("ipo_date") else ""
        iss=str(int(r["issue_date"])) if r.get("issue_date")==r.get("issue_date") else ""
        it=row(r)
        if ip==TODAY: today_buy.append(it)
        elif ip and TODAY<ip<= (datetime.strptime(TODAY,"%Y%m%d")+timedelta(days=7)).strftime("%Y%m%d"): soon_buy.append(it)
        if iss and (datetime.strptime(TODAY,"%Y%m%d")-timedelta(days=5)).strftime("%Y%m%d")<=iss<=TODAY: just_listed.append(it)
today_buy.sort(key=lambda x:x["code"]); soon_buy.sort(key=lambda x:x["ipo_date"]); just_listed.sort(key=lambda x:x["issue_date"],reverse=True)
out={"updated":datetime.now().strftime("%Y-%m-%d %H:%M"),"today":TODAY,
     "strategy":"打新提醒: 逢新必打(运气活·正收益·与组合正交), 中签首日卖。市值底仓需沪深各备(科创板/创业板需开通权限)",
     "today_buy":today_buy,"soon_buy":soon_buy[:20],"just_listed":just_listed[:20],
     "note":"打新是账户级白捡增厚(近年破发率~0-2%、新股少但单签收益高, 历史散户年化~2-8%)。今日有可申购的→用申购代码顶格打满。中签后通常首日卖出。需提前备好沪/深市值底仓。"}
open(os.path.join(OUT_DIR,"ipo.json"),"w",encoding="utf-8").write(json.dumps(out,ensure_ascii=False,indent=1).replace("NaN","null"))
print(f"[ipo] 今日{TODAY} | 今日可申购{len(today_buy)} 即将{len(soon_buy)} 近期上市{len(just_listed)} -> ipo.json")
if today_buy: print("今日可申购:", [(x['name'],x['sub_code'],x['price']) for x in today_buy])
