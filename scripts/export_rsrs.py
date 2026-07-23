"""生成 rsrs.json (RSRS指数择时信号页, 独立于三腿中性组合)。
对 沪深300/中证1000: N=18日 高~低 回归斜率β → M=600日标准分 → 修正标准分(×R²) → S=0.7 多/空仓信号。
产出 <OUT_DIR>/rsrs.json (默认 C:\rdagent, 环境变量 QI_EXPORT_DIR 可覆盖; PC端 watcher 拷群晖/NAS)。
跑: python scripts/export_rsrs.py   (token: 环境变量 TUSHARE_TOKEN 或 data/.tushare_token)
"""
import io,sys,os,json,time
from datetime import datetime
# 国内数据直连: 清掉可能继承的代理(index_daily 走 api.waditu.com 会被代理挡→超时→indices空)
for _k in ('http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','all_proxy','ALL_PROXY'): os.environ.pop(_k,None)
os.environ['no_proxy']='*'; os.environ['NO_PROXY']='*'
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8',errors='replace')
import numpy as np, pandas as pd, tushare as ts
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
N=18; M=600; S=0.7   # 回归窗 / 标准化窗 / 阈值
def safe(fn,*a,**k):
    for _ in range(4):
        try: return fn(*a,**k)
        except Exception as e:
            if 'minute' in str(e) or 'limit' in str(e).lower(): time.sleep(8)
            else: return None
    return None
def rsrs(code):
    d=safe(pro.index_daily, ts_code=code, start_date='20210101', end_date=datetime.now().strftime('%Y%m%d'))
    if d is None or not len(d): return None
    d=d.sort_values('trade_date')
    H=pd.to_numeric(d['high']).values; L=pd.to_numeric(d['low']).values; CL=pd.to_numeric(d['close']).values
    beta=np.full(len(d),np.nan); r2=np.full(len(d),np.nan)
    for i in range(N-1,len(d)):
        x=L[i-N+1:i+1]; y=H[i-N+1:i+1]
        if x.std()==0: continue
        c=np.polyfit(x,y,1); beta[i]=c[0]; yh=np.polyval(c,x)
        r2[i]=1-((y-yh)**2).sum()/(((y-y.mean())**2).sum()+1e-9)
    bs=pd.Series(beta); std=(bs-bs.rolling(M,min_periods=120).mean())/bs.rolling(M,min_periods=120).std()
    corr=(std*pd.Series(r2)).values
    dates=[str(x) for x in d['trade_date'].values]
    track=[]
    for i in range(max(0,len(d)-30),len(d)):
        if corr[i]==corr[i]:
            track.append({"date":f"{dates[i][:4]}-{dates[i][4:6]}-{dates[i][6:]}",
                          "score":round(float(corr[i]),3),"state":"持有" if corr[i]>S else "空仓","close":round(float(CL[i]),1)})
    cur=corr[-1] if corr[-1]==corr[-1] else (corr[~np.isnan(corr)][-1] if (~np.isnan(corr)).any() else np.nan)
    prev=corr[-2] if len(corr)>1 and corr[-2]==corr[-2] else np.nan
    state="持有" if cur>S else "空仓"; pstate=("持有" if prev>S else "空仓") if prev==prev else state
    return {"score":round(float(cur),3),"beta":round(float(beta[-1]),3) if beta[-1]==beta[-1] else None,
            "r2":round(float(r2[-1]),3) if r2[-1]==r2[-1] else None,"state":state,"flip":(state!=pstate),
            "as_of":f"{dates[-1][:4]}-{dates[-1][4:6]}-{dates[-1][6:]}","track":track}
out={"updated":datetime.now().strftime("%Y-%m-%d %H:%M"),
     "strategy":"RSRS 阻力支撑相对强度 指数择时(独立信号, 不并入三腿中性组合)",
     "params":{"N":N,"M":M,"threshold":S,"mode":"多/空仓(修正标准分>0.7持有, 否则空仓)"},
     "indices":[],
     "note":"用于方向性仓位(指数/ETF/定投): 历史上把买入持有夏0.4→0.9、回撤−47%→−16%, 近4年(23-26)分年夏普全正未衰减。硬伤: 2018式震荡阴跌会被打回撤(夏−1.8)。这是裸beta方向择时, 与三腿中性组合物理隔离。"}
for code,nm in [('000300.SH','沪深300'),('000852.SH','中证1000')]:
    r=rsrs(code)
    if r: r["code"]=code; r["name"]=nm; out["indices"].append(r)
open(os.path.join(OUT_DIR,"rsrs.json"),"w",encoding="utf-8").write(json.dumps(out,ensure_ascii=False,indent=1))
flips=[x['name'] for x in out['indices'] if x.get('flip')]
print(f"[rsrs] {[(x['name'],x['state'],x['score']) for x in out['indices']]} | 翻转:{flips or '无'} -> rsrs.json")
