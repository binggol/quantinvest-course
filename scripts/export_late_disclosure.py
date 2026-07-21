# -*- coding: utf-8 -*-
"""生成 data/late_disclosure.json: 年报季 晚披露的小/中盘股(靓女先嫁的反面=A股真信号)。
验证 gate_disclosure_bytype.py: 年报晚披露-早披露 T+10超额+1.69%/IC0.073/2022-25四年全正; 小盘+2.1%中盘+2.1%(大盘无效, 非size马甲)。
机制: 年报早披露=好消息1月已预告→见光死; 晚披露(小/中盘)未被抢跑→披露释放信息→正漂移。
口径: 当前年报季(end_date=上年1231)中, 实际披露日处于全市场后40%(晚)、且流通市值非大盘(后2/3)的股票。持有~10交易日。
季节性: 仅年报季(每年3-4月)有标的; 非年报季输出空窗提示。
跑: D:/anaconda3/python.exe scripts/export_late_disclosure.py
"""
import os, json, datetime
import pandas as pd
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(PROJ, "data")
OUT = os.path.join(DATA, "late_disclosure.json")
NAS = os.environ.get("QI_EXPORT_NAS_DIR", r"Z:\claude\qlib\data\csv_tmp")


def main():
    for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY']:
        os.environ.pop(k, None)
    os.environ['no_proxy'] = '*'
    import tushare as ts
    tok = os.environ.get("TUSHARE_TOKEN") or open(os.path.join(DATA, ".tushare_token")).read().strip()
    pro = ts.pro_api(tok)
    today = datetime.date.today()
    # 年报季: 上一年12-31报告期, 披露窗口次年1月~4月底
    rpt_year = today.year - 1 if today.month <= 5 else today.year - 1   # 当前/最近年报期
    end_date = f"{rpt_year}1231"
    # 年报季活跃窗口: 报告期次年的 1/1 ~ 4/30
    season_start = datetime.date(rpt_year + 1, 1, 1)
    season_end = datetime.date(rpt_year + 1, 4, 30)
    in_season = season_start <= today <= season_end + datetime.timedelta(days=15)  # 季末后15天仍在持有窗口

    d = pro.disclosure_date(end_date=end_date)
    out = {"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "rpt_period": end_date,
           "season": "年报季(每年1-4月)", "in_season": bool(in_season),
           "note": "年报晚披露的小/中盘=验证信号(四年全正/IC0.073/T+10超额+1.69%)。机制:早披露见光死,晚披露未被抢跑→正漂移。持有~10交易日。仅年报季有标的。",
           "items": []}
    if d is None or not len(d):
        out["msg"] = f"{end_date} 暂无披露数据"
        _write(out); return
    d = d.dropna(subset=['actual_date']).copy()
    d['actual_date'] = d['actual_date'].astype(str)
    d = d[d['actual_date'].str.len() == 8]
    # 已披露的按实际披露日排名; 晚=后40%
    d['rank'] = d['actual_date'].rank(pct=True)
    late = d[d['rank'] >= 0.60].copy()
    if not in_season or not len(late):
        out["msg"] = "非年报季(或暂无晚披露标的)。年报季为每年1-4月, 那时本页列出晚披露小/中盘股。当前空窗。"
        _write(out); return
    # 取最近5个交易日内刚披露的(新鲜)
    recent_cut = (today - datetime.timedelta(days=10)).strftime("%Y%m%d")
    fresh = late[late['actual_date'] >= recent_cut]
    pool = fresh if len(fresh) >= 5 else late.sort_values('actual_date', ascending=False).head(60)
    codes = pool['ts_code'].tolist()
    # 市值过滤: 非大盘(流通市值后2/3)
    last_td = pro.trade_cal(exchange='SSE', start_date=(today - datetime.timedelta(days=12)).strftime('%Y%m%d'),
                            end_date=today.strftime('%Y%m%d'), is_open='1')['cal_date'].max()
    db = pro.daily_basic(trade_date=last_td, fields='ts_code,circ_mv,close')
    cmv = dict(zip(db['ts_code'], db['circ_mv']))
    sb = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry')
    nm = dict(zip(sb['ts_code'], sb['name'])); ind = dict(zip(sb['ts_code'], sb['industry']))
    # 大盘门槛: 全市场流通市值66分位
    allmv = pd.Series(cmv).dropna()
    big_thr = allmv.quantile(0.66)
    adate = dict(zip(pool['ts_code'], pool['actual_date']))
    items = []
    for c in codes:
        mv = cmv.get(c)
        if mv is None or mv >= big_thr:   # 剔大盘
            continue
        items.append({"code": c, "name": nm.get(c, c), "industry": ind.get(c, ""),
                      "circ_mv_yi": round(mv / 10000.0, 1),    # 流通市值(亿)
                      "discl_date": adate.get(c, ""),
                      "size": "小盘" if mv < allmv.quantile(0.33) else "中盘"})
    items.sort(key=lambda x: x["discl_date"], reverse=True)
    items = items[:60]
    # CFO/审计变更标记(标记不剔除): 晚披露若因换CFO/审计=程序性延误非业绩超预期, ⚠️提醒
    try:
        ca_flags = check_cfo_audit(pro, [it["code"] for it in items], rpt_year)
        for it in items:
            if it["code"] in ca_flags:
                it["cfo_audit_flag"] = True
                it["cfo_audit_reason"] = ca_flags[it["code"]]
        out["n_cfo_audit_flag"] = len(ca_flags)
    except Exception as e:
        print(f"[late_disclosure] CFO/审计检测失败: {e}")
    out["items"] = items
    out["n"] = len(out["items"])
    out["as_of"] = last_td[:4] + "-" + last_td[4:6] + "-" + last_td[6:8]
    _write(out)


def check_cfo_audit(pro, codes, rpt_year):
    """检测 近90天CFO/财务总监/董秘离职 或 会计师事务所/签字会计师较上年变更 → 标记(不剔除)。
    hermes#5: 晚披露可能是程序性延误(换CFO/审计)非业绩超预期, 标记⚠️提醒人工判断。"""
    import datetime as _dt
    cutoff = (_dt.date.today() - _dt.timedelta(days=90)).strftime("%Y%m%d")
    FIN_TITLES = ("财务", "CFO", "总会计", "董事会秘书", "董秘")
    flags = {}
    for c in codes:
        reasons = []
        # 1) 财务高管近90天离职
        try:
            mg = pro.stk_managers(ts_code=c)
            if mg is not None and len(mg):
                for r in mg.itertuples(index=False):
                    ttl = str(getattr(r, "title", "") or "")
                    ed = str(getattr(r, "end_date", "") or "")
                    if any(t in ttl for t in FIN_TITLES) and ed and ed >= cutoff and ed <= _dt.date.today().strftime("%Y%m%d"):
                        reasons.append(f"近期{ttl}离任"); break
        except Exception:
            pass
        # 2) 会计师事务所/签字会计师较上年变更
        try:
            cur = pro.fina_audit(ts_code=c, period=f"{rpt_year}1231")
            prev = pro.fina_audit(ts_code=c, period=f"{rpt_year-1}1231")
            if cur is not None and len(cur) and prev is not None and len(prev):
                ag_c, ag_p = str(cur.iloc[0].get("audit_agency", "")), str(prev.iloc[0].get("audit_agency", ""))
                sg_c, sg_p = str(cur.iloc[0].get("audit_sign", "")), str(prev.iloc[0].get("audit_sign", ""))
                if ag_c and ag_p and ag_c != ag_p:
                    reasons.append("换会计师事务所")
                elif sg_c and sg_p and sg_c != sg_p:
                    reasons.append("换签字会计师")
        except Exception:
            pass
        if reasons:
            flags[c] = "/".join(reasons)
    return flags


def _write(out):
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    try:
        import shutil
        os.makedirs(NAS, exist_ok=True)
        shutil.copy(OUT, os.path.join(NAS, "late_disclosure.json"))
    except Exception as e:
        print(f"[late_disclosure] 拷NAS失败: {e}")
    print(f"[late_disclosure] in_season={out.get('in_season')} n={out.get('n', 0)} -> {OUT}")


if __name__ == "__main__":
    main()
