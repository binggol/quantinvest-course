"""生成 repo.json (回购事件腿 每日清单, 第四sleeve)。近60交易日内有回购公告的中证1000成分=持仓; 今日新公告=建仓; 临近60日=平仓。
持有60交易日, 指数对冲, 等权。产出 <OUT_DIR>/repo.json (默认 C:\rdagent, 环境变量 QI_EXPORT_DIR 可覆盖; PC端 watcher 拷群晖/NAS)。
跑: python scripts/export_repo.py   (token: 环境变量 TUSHARE_TOKEN 或 data/.tushare_token)
"""
import io,sys,os,json,time
from datetime import datetime
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8',errors='replace')
for _k in ['http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','all_proxy','ALL_PROXY']: os.environ.pop(_k,None)
os.environ['no_proxy']='*'   # tushare=中国直连; 不清代理时stock_basic走境外代理超时->names空->名字显示成代码
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
pro=ts.pro_api(tok); HOLD=60; TODAY=os.environ.get("REPO_TODAY", datetime.now().strftime("%Y%m%d"))
def safe(fn,*a,**k):
    for _ in range(4):
        try: return fn(*a,**k)
        except Exception as e:
            if 'minute' in str(e) or 'limit' in str(e).lower(): time.sleep(8)
            else: return None
    return None
# 交易日历
cal=safe(pro.trade_cal, exchange='SSE', start_date='20250101', end_date=TODAY)
tds=sorted(cal[cal['is_open']==1]['cal_date'].tolist()) if cal is not None else []
def n_td_ago(n): return tds[-n-1] if len(tds)>n else (tds[0] if tds else TODAY)
win_start=n_td_ago(HOLD)   # 60交易日前
# 中证1000当前成分
iw=safe(pro.index_weight, index_code='000852.SH', start_date='20260101', end_date=TODAY)
mem=set(iw[iw['trade_date']==iw['trade_date'].max()]['con_code']) if iw is not None and len(iw) else set()
nb=safe(pro.stock_basic, exchange='', list_status='L', fields='ts_code,name'); names=dict(zip(nb['ts_code'],nb['name'])) if nb is not None else {}
# 近期回购公告(覆盖60交易日窗+余量)
rows=[]
for back in range(0,5):
    ym=(pd.to_datetime(TODAY)-pd.DateOffset(months=back)).strftime("%Y%m")
    d=safe(pro.repurchase, start_date=ym+"01", end_date=ym+"31")
    if d is not None and len(d): rows.append(d)
rp=pd.concat(rows,ignore_index=True) if rows else pd.DataFrame()
holdings=[]; buy_today=[]; sell_soon=[]
if len(rp):
    rp=rp[rp["ts_code"].isin(mem)].dropna(subset=["ann_date"])
    rp["ann"]=rp["ann_date"].astype(str)
    # 每股取窗口内最近一次公告
    last={}
    for _,r in rp.iterrows():
        a=r["ann"]
        if a>=win_start and a<=TODAY:
            c=r["ts_code"]
            if c not in last or a>last[c]["ann"]: last[c]={"ann":a,"proc":r.get("proc",""),"amount":r.get("amount")}
    for c,v in last.items():
        held=len([d for d in tds if v["ann"]<d<=TODAY])  # 已持有交易日
        item={"code":c,"name":names.get(c,c),"ann_date":v["ann"],"proc":v["proc"],"held_td":held,"to_close":HOLD-held}
        holdings.append(item)
        if held<=1: buy_today.append(item)
        if HOLD-held<=3: sell_soon.append(item)
holdings.sort(key=lambda x:x["held_td"]); buy_today.sort(key=lambda x:x["code"]); sell_soon.sort(key=lambda x:x["to_close"])
out={"updated":datetime.now().strftime("%Y-%m-%d %H:%M"),"today":TODAY,
     "strategy":"回购事件腿(第四sleeve): 中证1000成分 回购公告后建仓、持有60交易日、指数对冲、等权",
     "params":{"hold_td":HOLD,"universe":"中证1000","hedge":"中证1000股指期货(IM)"},
     "n_holdings":len(holdings),"buy_today":buy_today[:50],"sell_soon":sell_soon[:50],"holdings":holdings[:200],
     "note":"回测: 回购公告后CAR[+1,+60]+2.07%/t=11.4; 持60日对冲全夏0.95、2024/25-26牛OOS全正(不衰减); 与三腿相关0.02/−0.01/0.10近零, 配20%抬组合夏普2.06→2.34(+0.28)。慢漂移、低换手、容量大。下单前自查停牌/涨停。"}
open(os.path.join(OUT_DIR,"repo.json"),"w",encoding="utf-8").write(json.dumps(out,ensure_ascii=False,indent=1))
print(f"[repo] 今日{TODAY} 1000成分回购持仓{len(holdings)} | 今日建仓{len(buy_today)} 临近平仓{len(sell_soon)} -> repo.json")
if buy_today: print("今日建仓样例:", [(b['name'],b['proc']) for b in buy_today[:6]])
