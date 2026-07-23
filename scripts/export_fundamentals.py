"""生成 fundamentals.json(每股最近3年报+最新一期 扣非增速/营收增速/毛利率, 带行业, 供下单页对比同行) 与
margin_avoid.json(毛利率同比恶化"避雷"清单, 流动性过滤) + fraud_avoid.json(Beneish-DSRI造假避雷)。
用 _dedt_q/_rev_q/_gpm_q + _name_map + _industry_map。
⚠️输入依赖: <R> 目录下 RD-Agent 日更管线落盘的 _name_map.pkl / _sw_lvl_map.pkl / _industry_map.pkl / _dedt_q.pkl / _rev_q.pkl / _gpm_q.pkl
产出 <R>/fundamentals.json + <R>/margin_avoid.json + <R>/fraud_avoid.json (PC端 watcher 拷群晖到 predictions.json 同目录)。
R 默认 C:\rdagent, 环境变量 QI_RDAGENT_DIR 可覆盖。
跑: python scripts/export_fundamentals.py   (token: 环境变量 TUSHARE_TOKEN 或 data/.tushare_token)
"""
import os,io,sys,json,pickle,time
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8',errors='replace')
import numpy as np,pandas as pd,tushare as ts
R=os.environ.get("QI_RDAGENT_DIR", r"C:\rdagent")
def _load_token():
    t=os.environ.get("TUSHARE_TOKEN")
    if t: return t.strip()
    for p in (os.path.join(R,"data",".tushare_token"),
              os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),"data",".tushare_token")):
        if os.path.exists(p): return open(p,encoding="utf-8").read().strip()
    raise SystemExit("缺tushare token: 设环境变量 TUSHARE_TOKEN 或放 data/.tushare_token")
TOK=_load_token()
def load(f): return pickle.load(open(os.path.join(R,f),"rb"))
nm=load("_name_map.pkl")                       # ts_code -> name
lvl=load("_sw_lvl_map.pkl")                     # ts_code -> {l1,l2,l3} 申万三级
imap=load("_industry_map.pkl")                 # qlib(sz000001) -> 行业(旧粗分类, 兜底)
def q(c): n,mk=c.split("."); return ("sh" if mk=="SH" else "sz")+n
def L(c,k,fb): v=(lvl.get(c) or {}).get(k); return v if v else fb
ind_ts={c:L(c,"l3",imap.get(q(c),"其他")) for c in nm}    # ts_code -> 申万三级(主营), 兜底旧分类
de=load("_dedt_q.pkl"); rv=load("_rev_q.pkl"); gp=load("_gpm_q.pkl")
def prep(df,col):
    df=df.dropna(subset=["end_date",col]).copy()
    df["v"]=pd.to_numeric(df[col],errors="coerce"); df["end"]=df["end_date"].astype(str); df["ann"]=df["ann_date"].astype(str)
    df=df.dropna(subset=["v"]).sort_values(["ts_code","end"])
    return df
de=prep(de,"profit_dedt"); rv=prep(rv,"revenue"); gp=prep(gp,"grossprofit_margin")
# 先按(ts_code,end)去重(留最新ann, 防重述报告期打乱shift(4)对齐), 再算同比
de=de.drop_duplicates(["ts_code","end"],keep="last"); rv=rv.drop_duplicates(["ts_code","end"],keep="last"); gp=gp.drop_duplicates(["ts_code","end"],keep="last")
gp["v"]=gp["v"].clip(-50,100)   # 毛利率clip(和回测一致), 去*ST极端脏值
# 累计同比(vs去年同期同季): shift(4)对齐同季
def cumyoy(df):
    df=df.copy(); df["v4"]=df.groupby("ts_code")["v"].shift(4)
    df["yoy"]=np.where((df["v4"].abs()>1e-6)&(df["v4"]>0),(df["v"]-df["v4"])/df["v4"]*100,np.nan)  # 仅基数>0给增速
    return df
dey=cumyoy(de); rvy=cumyoy(rv)
gp=gp.copy(); gp["gm4"]=gp.groupby("ts_code")["v"].shift(4); gp["dgm"]=gp["v"]-gp["gm4"]
# 取每股 最近3个年报(1231) + 最新一期
def latest_periods(allp):
    ann=[p for p in allp if p.endswith("1231")][-3:]
    latest=allp[-1]
    pl=ann+([latest] if latest not in ann else [])
    return pl
dey=dey.drop_duplicates(["ts_code","end"],keep="last"); rvy=rvy.drop_duplicates(["ts_code","end"],keep="last"); gp=gp.drop_duplicates(["ts_code","end"],keep="last")
dge=dey.set_index(["ts_code","end"]); rge=rvy.set_index(["ts_code","end"]); gge=gp.set_index(["ts_code","end"])
codes=sorted(set(de["ts_code"]))
fund={}
for c in codes:
    allp=sorted(de[de["ts_code"]==c]["end"].unique())
    if not allp: continue
    pl=latest_periods(allp)
    rows=[]
    for p in pl:
        def g(idx,c,p,col):
            try: return idx.loc[(c,p),col]
            except Exception: return None
        dy=g(dge,c,p,"yoy"); ry=g(rge,c,p,"yoy"); gm=g(gge,c,p,"v"); dg=g(gge,c,p,"dgm")
        rows.append({"period":p[:4]+("年报" if p.endswith("1231") else "Q"+str((int(p[4:6])+2)//3)),
                     "end":p,
                     "dedt_yoy":None if dy is None or pd.isna(dy) else round(float(dy),1),
                     "rev_yoy":None if ry is None or pd.isna(ry) else round(float(ry),1),
                     "gm":None if gm is None or pd.isna(gm) else round(float(gm),1),
                     "dgm":None if dg is None or pd.isna(dg) else round(float(dg),1)})
    fund[c]={"name":nm.get(c,c),"l1":L(c,"l1","其他"),"l2":L(c,"l2","其他"),"l3":L(c,"l3",ind_ts.get(c,"其他")),
             "ind":ind_ts.get(c,"其他"),"rows":rows}
print(f"fundamentals: {len(fund)}只",flush=True)
# 行业分组: 申万三级(精确主营) + 申万二级(广义板块)
by_l3={}; by_l2={}
for c,v in fund.items():
    by_l3.setdefault(v["l3"],[]).append(c)
    by_l2.setdefault(v["l2"],[]).append(c)
out_fund={"as_of":time.strftime("%Y-%m-%d"),"stocks":fund,"by_l3":by_l3,"by_l2":by_l2}
open(os.path.join(R,"fundamentals.json"),"w",encoding="utf-8").write(json.dumps(out_fund,ensure_ascii=False).replace("NaN","null"))
# ---- 避雷清单: 最新一期 dgm 最差(毛利率同比恶化), 流动性过滤(circ_mv>20亿) ----
pro=ts.pro_api(TOK)
cm={}
try:
    cal=pro.trade_cal(exchange="SSE",start_date=time.strftime("%Y%m%d",time.localtime(time.time()-12*86400)),end_date=time.strftime("%Y%m%d"))
    opendays=sorted(cal[cal["is_open"]==1]["cal_date"])
except Exception: opendays=[]
for td in reversed(opendays):   # 盘中当日数据未出(0行)→往前找最近有数据的交易日
    try:
        db=pro.daily_basic(trade_date=td,fields="ts_code,circ_mv")
        if db is not None and len(db): cm={r["ts_code"]:r["circ_mv"] for _,r in db.iterrows()}; print(f"circ_mv {td} {len(cm)}",flush=True); break
    except Exception: time.sleep(5)
latest_dgm=[]
for c in codes:
    sub=gp[gp["ts_code"]==c]
    if not len(sub): continue
    r=sub.iloc[-1]
    if pd.isna(r["dgm"]): continue
    mv=cm.get(c,np.nan)
    if not (mv==mv) or mv<200000:  # circ_mv单位万元, 20亿=200000万
        continue
    latest_dgm.append({"code":c,"name":nm.get(c,c),"ind":ind_ts.get(c,"其他"),
                       "gm":round(float(r["v"]),1),"gm_prev":round(float(r["gm4"]),1) if pd.notna(r["gm4"]) else None,
                       "dgm":round(float(r["dgm"]),1),"period":str(r["end"])})
latest_dgm.sort(key=lambda x:x["dgm"])
avoid=latest_dgm[:150]
out_av={"as_of":time.strftime("%Y-%m-%d"),"n_universe":len(latest_dgm),"period":avoid[0]["period"] if avoid else "",
        "items":avoid}
open(os.path.join(R,"margin_avoid.json"),"w",encoding="utf-8").write(json.dumps(out_av,ensure_ascii=False).replace("NaN","null"))
print(f"margin_avoid: 流动性池{len(latest_dgm)}只, 取最恶化{len(avoid)}只 -> margin_avoid.json")

# ---- 财务造假避雷(Beneish-DSRI): 应收账款透支指数高=虚增收入嫌疑. 验证(gate_beneish_skew, 2625样本): DSRI最高组前向半年跑输1.05%/t=6.8 ----
# DSRI = (本期应收/本期营收)/(上期应收/上期营收). 取最近两年报. 流动性过滤(circ_mv>20亿)同毛利率避雷.
def _latest_annual():
    import datetime as _dt; y=_dt.datetime.now().year; m=_dt.datetime.now().month
    ay=(y-1) if m>=5 else (y-2)   # 5月起最新年报是去年
    return f"{ay}1231", f"{ay-1}1231"
try:
    cur_p, prev_p = _latest_annual()
    inc_c=pro.income_vip(period=cur_p,fields="ts_code,revenue"); inc_p=pro.income_vip(period=prev_p,fields="ts_code,revenue")
    bs_c=pro.balancesheet_vip(period=cur_p,fields="ts_code,accounts_receiv"); bs_p=pro.balancesheet_vip(period=prev_p,fields="ts_code,accounts_receiv")
    rc=dict(zip(inc_c["ts_code"],pd.to_numeric(inc_c["revenue"],errors="coerce"))); rp=dict(zip(inc_p["ts_code"],pd.to_numeric(inc_p["revenue"],errors="coerce")))
    ac=dict(zip(bs_c.drop_duplicates("ts_code")["ts_code"],pd.to_numeric(bs_c.drop_duplicates("ts_code")["accounts_receiv"],errors="coerce")))
    ap=dict(zip(bs_p.drop_duplicates("ts_code")["ts_code"],pd.to_numeric(bs_p.drop_duplicates("ts_code")["accounts_receiv"],errors="coerce")))
    fraud=[]
    for c in rc:
        if c not in rp or c not in ac or c not in ap: continue
        rev_c,rev_p,ar_c,ar_p=rc[c],rp.get(c),ac.get(c),ap.get(c)
        if not (rev_c and rev_p and rev_c>0 and rev_p>0 and ar_p and ar_p>0 and ar_c==ar_c): continue
        arr_c=ar_c/rev_c; arr_p=ar_p/rev_p
        if arr_p<0.02 or arr_c<0.02: continue   # 应收/营收基数<2%=分母噪声(基数效应), 剔除防DSRI虚高
        dsri=arr_c/arr_p
        if not (dsri==dsri) or dsri<=0 or dsri>10: continue   # DSRI>10多为数据异常/重组, 非正常造假区间
        mv=cm.get(c,np.nan)
        if not (mv==mv) or mv<200000: continue   # 流动性过滤(circ_mv>20亿)
        fraud.append({"code":c,"name":nm.get(c,c),"ind":ind_ts.get(c,"其他"),
                      "dsri":round(float(dsri),2),"ar_ratio":round(ar_c/rev_c*100,1),"ar_ratio_prev":round(ar_p/rev_p*100,1),
                      "period":cur_p})
    fraud.sort(key=lambda x:-x["dsri"])   # DSRI最高=造假嫌疑最大
    out_fr={"as_of":time.strftime("%Y-%m-%d"),"period":cur_p,"n_universe":len(fraud),
            "items":fraud[:120],
            "note":"Beneish-DSRI应收账款透支指数=本期(应收/营收)/上期(应收/营收), >1.5重点警惕。应收涨得比营收快=可能虚增收入造假。验证(2625样本): DSRI最高20%组前向半年跑输1.05%/t=6.8。负面剔除信号: 从持仓/买入候选剔除, 非做空。流动性池(流通市值≥20亿)。"}
    open(os.path.join(R,"fraud_avoid.json"),"w",encoding="utf-8").write(json.dumps(out_fr,ensure_ascii=False).replace("NaN","null"))
    print(f"fraud_avoid(Beneish-DSRI): 池{len(fraud)}只, 取DSRI最高{len(out_fr['items'])}只 -> fraud_avoid.json")
except Exception as _e:
    print(f"fraud_avoid 跳过: {_e}")
