"""生成 index_inclusion_pro.json (指数纳入效应 实盘每日清单)。
读取拟纳入事件。如果在 T-20 窗口期，计算流通市值，取后 50% 放入“今日建仓”；
如果在 T-1 日，放入“今日平仓”。平时为空仓状态。
跑: python scripts/export_index_inclusion_pro.py
"""
import os, sys, json, time, calendar
from datetime import datetime
import pandas as pd
import tushare as ts

try:
    from .tushare_auth import get_tushare_token
except ImportError:
    from tushare_auth import get_tushare_token

# 国内数据直连(清代理, 否则 index_weight 可能走代理超时)
for _k in ('http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','all_proxy','ALL_PROXY'): os.environ.pop(_k,None)
os.environ['no_proxy']='*'; os.environ['NO_PROXY']='*'

# --- Config ---
TODAY = os.environ.get("INCLUSION_TODAY", datetime.now().strftime("%Y-%m-%d"))
DATA_DIR = os.path.dirname(os.path.abspath(__file__)) + "/../data"
OUTPUT_FILE = os.path.join(DATA_DIR, "index_inclusion_pro.json")
OFFLINE_DB = os.path.join(DATA_DIR, "index_inclusion.json")
UPCOMING_FILE = os.path.join(DATA_DIR, "upcoming_inclusions.json")  # 手工录入官方公告(真抢跑用)
# code -> (名, 调仓月). MSCI/富时无 index_weight 数据, 不在此(需手工录入 upcoming_inclusions.json)
INDICES = {
    "000016.SH": ("上证50", [6, 12]), "000300.SH": ("沪深300", [6, 12]),
    "000905.SH": ("中证500", [6, 12]), "000852.SH": ("中证1000", [6, 12]),
    "000688.SH": ("科创50", [3, 6, 9, 12]), "399006.SZ": ("创业板指", [6, 12]),
    "000510.SH": ("中证A500", [6, 12]), "399310.SZ": ("中证A50", [6, 12]),
}


def build_upcoming_events(pro, tds):
    """①index_weight差分自动检测最近一期新增(生效后才可见, 确认/监控用) + ②手工录入官方公告(真抢跑用)."""
    def safe(fn, *a, **k):
        for _ in range(4):
            try: return fn(*a, **k)
            except Exception: time.sleep(2)
        return None
    def eff_date(y, m):
        fr = [w[4] for w in calendar.monthcalendar(y, m) if w[4]]
        sf = f"{y}{m:02d}{fr[1]:02d}"
        fut = [d for d in tds if d >= sf]
        return f"{fut[1][:4]}-{fut[1][4:6]}-{fut[1][6:]}" if len(fut) > 1 else None
    out = []
    today_ymd = TODAY.replace("-", "")
    start = f"{int(today_ymd[:4]) - 1}{today_ymd[4:]}"
    for code, (nm, months) in INDICES.items():
        w = safe(pro.index_weight, index_code=code, start_date=start, end_date=today_ymd)
        if w is None or w.empty:
            continue
        dts = sorted(w['trade_date'].unique())
        if len(dts) < 2:
            continue
        cur, prev = dts[-1], dts[-2]
        added = set(w[w['trade_date'] == cur]['con_code']) - set(w[w['trade_date'] == prev]['con_code'])
        if not added:
            continue
        eff = None
        for m in months:
            for y in (int(cur[:4]), int(cur[:4]) - 1):
                e = eff_date(y, m)
                if e and prev < e.replace("-", "") <= cur:
                    eff = e
        if not eff:
            continue
        for c in added:
            out.append({"ts_code": c, "index_name": nm, "inclusion_date": eff, "src": "auto"})
    # ②手工官方公告(真抢跑: 生效前买必须靠这个, tushare只有生效后成分)
    if os.path.exists(UPCOMING_FILE):
        try:
            for e in json.load(open(UPCOMING_FILE, encoding="utf-8")):
                c = e.get("ts_code") or e.get("code"); t = e.get("inclusion_date") or e.get("t_date")
                if c and t:
                    out.append({"ts_code": c, "index_name": e.get("index", e.get("index_name", "手工")),
                                "inclusion_date": t, "src": "manual"})
        except Exception as ex:
            print(f"[upcoming] 手工文件解析失败: {ex}")
    return out

def main():
    pro = ts.pro_api(get_tushare_token())

    def safe(fn,*a,**k):
        for _ in range(4):
            try: return fn(*a,**k)
            except Exception as e:
                time.sleep(2)
        return None

    # 获取交易日历
    cal = safe(pro.trade_cal, exchange='SSE', start_date='20180101', end_date='20301231')
    tds = sorted(cal[cal['is_open'] == 1]['cal_date'].tolist()) if cal is not None else []
    
    def tdays_between(d1, d2):
        d1 = d1.replace("-", "")
        d2 = d2.replace("-", "")
        return len([d for d in tds if d1 < d <= d2])
        
    def get_t_minus_n(target_date_str, n):
        # target_date_str format: YYYY-MM-DD
        dt = target_date_str.replace("-", "")
        try:
            idx = tds.index(dt)
            if idx >= n:
                res = tds[idx - n]
                return f"{res[:4]}-{res[4:6]}-{res[6:]}"
        except:
            pass
        return None

    # 加载已知的纳入事件 (实盘中应由用户手工录入最新一期中证公司公告)
    if not os.path.exists(OFFLINE_DB):
        print(f"Offline DB {OFFLINE_DB} not found. Run export_index_inclusion.py first.")
        return
        
    db_data = json.load(open(OFFLINE_DB, encoding='utf-8'))
    events = pd.DataFrame(db_data.get('details', []))
    # 合并: 历史事件 + 自动检测最近一期新增 + 手工官方公告(真抢跑)
    up = build_upcoming_events(pro, tds)
    if up:
        events = pd.concat([events, pd.DataFrame(up)], ignore_index=True)
    if not events.empty:
        events = events.drop_duplicates(subset=['ts_code', 'inclusion_date'], keep='last')
    
    buy_today = []
    sell_today = []
    holdings = []
    watch = []
    
    if not events.empty:
        # 获取基础信息 (名称)
        nb = safe(pro.stock_basic, exchange='', list_status='L', fields='ts_code,name')
        names = dict(zip(nb['ts_code'], nb['name'])) if nb is not None else {}
        
        # 将事件按纳入日期(T日)分组
        for t_date, g in events.groupby('inclusion_date'):
            t20_date = get_t_minus_n(t_date, 20)
            t1_date = get_t_minus_n(t_date, 1)
            
            if t20_date is None or t1_date is None: continue
            
            # 判断今天是否在窗口期 [T-20, T-1]
            if TODAY < t20_date:
                # 还未到建仓期
                # 我们只关心即将到来的事件（例如距离 t20 还有不到30天的）
                if tdays_between(TODAY, t20_date) < 30:
                    for _, r in g.iterrows():
                        watch.append({"code": r['ts_code'], "name": names.get(r['ts_code'], ""), "index": r['index_name'], "t_date": t_date, "buy_date": t20_date})
            elif t20_date <= TODAY <= t1_date:
                # 已经进入窗口期
                
                # 动态获取 T-20 的流通市值进行过滤 (实盘中获取 T-20 那天的数据)
                dt_str = t20_date.replace("-", "")
                df_basic = safe(pro.daily_basic, trade_date=dt_str)
                if df_basic is None or len(df_basic) == 0:
                    df_basic = safe(pro.daily_basic, trade_date=str(int(dt_str)+1)) # fallback
                if df_basic is None or len(df_basic) == 0:
                    df_basic = safe(pro.daily_basic, trade_date=str(int(dt_str)+2)) # fallback
                
                caps = {}
                if df_basic is not None:
                    for _, row in df_basic.iterrows():
                        caps[row['ts_code']] = row['circ_mv']
                        
                # 筛选当前周期的数据
                g = g.copy()
                g['cap'] = g['ts_code'].map(caps)
                g = g.dropna(subset=['cap'])
                
                # 分指数进行过滤，只取市值最小的 50%
                approved_codes = set()
                for idx_name, sg in g.groupby('index_name'):
                    thresh = sg['cap'].median()
                    approved_codes.update(sg[sg['cap'] <= thresh]['ts_code'].tolist())
                    
                for _, r in g.iterrows():
                    c = r['ts_code']
                    if c not in approved_codes:
                        continue # 盘子太大，被资金冲击过滤掉
                        
                    days_held = tdays_between(t20_date, TODAY)
                    days_left = tdays_between(TODAY, t1_date)
                    
                    item = {"code": c, "name": names.get(c, ""), "index": r['index_name'], "t_date": t_date, "held_td": days_held, "to_sell_td": days_left}
                    
                    if TODAY == t20_date:
                        buy_today.append(item)
                    elif TODAY == t1_date:
                        sell_today.append(item)
                    else:
                        holdings.append(item)

    out = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "today": TODAY,
        "strategy": "指数纳入抢跑增强版 (流动性冲击过滤): T-20建仓流通市值后50%准成分股, T-1尾盘绝不贪恋平仓",
        "params": {"buy_window": "T-20", "sell_window": "T-1", "filter": "Bottom 50% Circulating Market Cap"},
        "n_holdings": len(holdings),
        "buy_today": buy_today,
        "sell_today": sell_today,
        "holdings": holdings,
        "watch": watch[:50], # 只展示前50个观察
        "note": "量化回测: 2018-2025年 829起纳入事件中, 经过市值后50%过滤后, 沪深300/中证500 平均超额收益(CAR)从 +3.72% 提升至 +4.16% (t=7.56), 胜率提升至 64.51%。核心法则：绝对不能持有到T日（生效日当天），因为被动基金的买盘只发生在T-1的尾盘集合竞价，必须在T-1尾盘准时清仓跑路。"
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        
    print(f"[index_inclusion_pro] {TODAY} | 需建仓 {len(buy_today)} | 需平仓 {len(sell_today)} | 持有中 {len(holdings)} -> {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
