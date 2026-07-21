import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
import calendar

import numpy as np
import pandas as pd
import tushare as ts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("export_index_inclusion")

TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN")
if not TUSHARE_TOKEN:
    try:
        with open(Path(__file__).parent.parent / "data" / ".tushare_token", "r") as f:
            TUSHARE_TOKEN = f.read().strip()
    except Exception:
        pass

# Passing the token directly avoids tushare.set_token() mutating ~/tk.csv at import.
pro = ts.pro_api(TUSHARE_TOKEN) if TUSHARE_TOKEN else None

PARQUET_DIR = Path(os.environ.get("PARQUET_DIR", "Z:/claude/qlib/data/csv_tmp/tushare_daily"))
QLIB_DATA_PATH = Path(os.environ.get("QLIB_DATA_PATH", "C:/qlib_data/cn_data"))
OUTPUT_JSON = Path(__file__).parent.parent / "data" / "index_inclusion.json"

# code -> (中文名, 调仓月份). 半年调=6/12月; 科创50为季调=3/6/9/12月.
INDICES = {
    "000016.SH": ("上证50", [6, 12]),
    "000300.SH": ("沪深300", [6, 12]),
    "000905.SH": ("中证500", [6, 12]),
    "000852.SH": ("中证1000", [6, 12]),
    "000688.SH": ("科创50", [3, 6, 9, 12]),
    "399006.SZ": ("创业板指", [6, 12]),
    "000510.SH": ("中证A500", [6, 12]),  # 2024-09上市, 当前被动资金流入最大; 历史短
    "399310.SZ": ("中证A50", [6, 12]),   # 历史短
    # 注: MSCI中国/富时罗素中国 为境外指数商, tushare 无 index_weight 成分数据, 无法自动跟踪(需官方公告源/手工录入)
}
INDEX_NAME = {k: v[0] for k, v in INDICES.items()}

def get_second_friday(year, month):
    """Get the date of the second Friday of the given month."""
    c = calendar.monthcalendar(year, month)
    fridays = [week[4] for week in c if week[4] != 0]
    return f"{year}{month:02d}{fridays[1]:02d}"

def get_effective_date(year, month, trade_cal):
    """Get the effective date (next trading day after the second Friday)."""
    second_friday_str = get_second_friday(year, month)
    future_dates = trade_cal[trade_cal >= second_friday_str]
    if len(future_dates) > 1:
        return future_dates.iloc[1] # The day after the 2nd Friday
    return None

def fetch_inclusion_events(index_code, months, start_year=2019, end_year=None):
    """Fetch constituent changes and determine inclusion dates."""
    if pro is None:
        raise RuntimeError("TUSHARE_TOKEN is not configured")
    if end_year is None:
        end_year = datetime.now().year   # 动态取当前年, 不写死
    events = []

    cal = pro.trade_cal(exchange='SSE', start_date=f'{start_year}0101', end_date=f'{end_year+1}0131', is_open='1')
    trade_cal = cal['cal_date'].sort_values().reset_index(drop=True)

    for year in range(start_year, end_year + 1):
        for month in months:
            prev_month = month - 1
            pyear = year

            end_of_prev = trade_cal[trade_cal.str.startswith(f"{pyear}{prev_month:02d}")].max()
            end_of_curr = trade_cal[trade_cal.str.startswith(f"{year}{month:02d}")].max()
            
            if pd.isna(end_of_curr) or pd.isna(end_of_prev): continue
            
            try:
                w_prev = pro.index_weight(index_code=index_code, trade_date=end_of_prev)
                w_curr = pro.index_weight(index_code=index_code, trade_date=end_of_curr)
            except Exception as e:
                log.warning(f"Failed to fetch weights for {index_code} {year}-{month}: {e}")
                continue
                
            if w_curr.empty or w_prev.empty: continue
            
            set_prev = set(w_prev['con_code'])
            set_curr = set(w_curr['con_code'])
            
            added = set_curr - set_prev
            
            if added:
                effective_date = get_effective_date(year, month, trade_cal)
                if not effective_date: continue
                
                for code in added:
                    events.append({
                        "index_code": index_code,
                        "index_name": INDEX_NAME[index_code],
                        "ts_code": code,
                        "inclusion_date": effective_date,
                        "year_month": f"{year}-{month:02d}"
                    })
                    
    return pd.DataFrame(events)

def calculate_returns(events_df):
    """Calculate returns around the inclusion dates using Qlib."""
    import qlib
    from qlib.data import D
    qlib.init(provider_uri=str(QLIB_DATA_PATH), region="cn")
        
    results = []
    
    # Pre-fetch all instruments close prices
    log.info("Fetching close prices from Qlib...")
    ts_codes = events_df['ts_code'].unique().tolist()
    # Format ts_code to qlib format (e.g. SH600519)
    def to_qlib_code(code):
        if code.endswith(".SH"): return "SH" + code[:6]
        if code.endswith(".SZ"): return "SZ" + code[:6]
        if code.endswith(".BJ"): return "BJ" + code[:6]
        return code
        
    qlib_codes = [to_qlib_code(c) for c in ts_codes]
    end_date = datetime.now().strftime("%Y-%m-%d")
    df_close = D.features(qlib_codes, ["$close"], start_time="2018-01-01", end_time=end_date)
    if df_close.empty:
        log.error("Failed to fetch close prices from Qlib.")
        return pd.DataFrame()
        
    # Pre-fetch calendar to find trading days easily
    cal = D.calendar(start_time="2018-01-01", end_time=end_date)
    cal_dates = pd.Series(cal)
    
    for _, row in events_df.iterrows():
        ts_code = row['ts_code']
        q_code = to_qlib_code(ts_code)
        inc_date = pd.to_datetime(row['inclusion_date'])
        
        # Find exact or next trading day in Qlib calendar
        valid_cals = cal_dates[cal_dates >= inc_date]
        if valid_cals.empty: continue
        actual_t_date = valid_cals.iloc[0]
        
        try:
            stock_df = df_close.loc[(q_code, slice(None)), :]
            stock_df = stock_df.reset_index(level=0, drop=True)
        except KeyError:
            continue
            
        if stock_df.empty: continue
        
        # Get integer index of T
        idxs = stock_df.index.get_indexer([actual_t_date], method='bfill')
        if idxs[0] == -1: continue
        t_idx = idxs[0]
        
        def get_close(shift):
            i = t_idx + shift
            if 0 <= i < len(stock_df):
                return stock_df.iloc[i]['$close']
            return np.nan
            
        c_T_20 = get_close(-20)
        c_T_10 = get_close(-10)
        c_T_5 = get_close(-5)
        c_T_2 = get_close(-2)
        c_T_1 = get_close(-1)
        c_T = get_close(0)
        c_T_plus_5 = get_close(5)
        c_T_plus_10 = get_close(10)
        c_T_plus_20 = get_close(20)
        
        ret = lambda c_end, c_start: float(c_end / c_start - 1) if pd.notna(c_end) and pd.notna(c_start) and c_start > 0 else None
        
        res = {
            "ts_code": ts_code,
            "index_name": row['index_name'],
            "inclusion_date": actual_t_date.strftime("%Y-%m-%d"),
            "period": row['year_month'],
            "ret_T20_T1": ret(c_T_1, c_T_20),
            "ret_T10_T1": ret(c_T_1, c_T_10),
            "ret_T5_T1": ret(c_T_1, c_T_5),
            "ret_T2_T1": ret(c_T_1, c_T_2),    # 提前1日买入 (T-2买，T-1卖)
            "ret_T1_T0": ret(c_T, c_T_1),      # 纳入当日
            "ret_T0_T5": ret(c_T_plus_5, c_T),
            "ret_T0_T10": ret(c_T_plus_10, c_T),
            "ret_T0_T20": ret(c_T_plus_20, c_T)
        }
        results.append(res)
        
    return pd.DataFrame(results)

def fetch_akshare_recent(index_code, after_date):
    """akshare兜底: tushare未公布的最新一期纳入(index_stock_cons带纳入日期)。after_date=已有库最新事件日, 只补之后的。"""
    ak_sym = index_code.split(".")[0]   # 000016.SH -> 000016
    try:
        import akshare as ak
        df = ak.index_stock_cons(symbol=ak_sym)
    except Exception as e:
        log.warning(f"akshare {ak_sym} 兜底失败: {e}")
        return pd.DataFrame()
    if df is None or "纳入日期" not in df.columns:
        return pd.DataFrame()
    df["纳入日期"] = df["纳入日期"].astype(str)
    new = df[df["纳入日期"] > after_date]
    evs = []
    for _, r in new.iterrows():
        raw = str(r["品种代码"])
        ex = ".SH" if raw[0] in "6589" else (".SZ" if raw[0] in "03" else ".BJ")
        evs.append({"index_code": index_code, "index_name": INDEX_NAME.get(index_code, index_code),
                    "ts_code": raw + ex, "inclusion_date": r["纳入日期"], "year_month": r["纳入日期"][:7]})
    if evs:
        log.info(f"akshare兜底 {INDEX_NAME.get(index_code, index_code)}: 补 {len(evs)} 起 ({after_date}之后)")
    return pd.DataFrame(evs)


def main():
    log.info("Starting Index Inclusion Effect Analysis...")
    all_events = []
    for code, (name, months) in INDICES.items():
        log.info(f"Fetching inclusion events for {code} ({name})...")
        df_ev = fetch_inclusion_events(code, months, 2019)   # end_year动态=当前年
        # akshare兜底: tushare未公布的最新期(如2026-06权重未发), 用index_stock_cons纳入日期补
        last_d = df_ev["inclusion_date"].max() if (df_ev is not None and not df_ev.empty) else "2019-01-01"
        df_ak = fetch_akshare_recent(code, last_d)
        if not df_ak.empty:
            df_ev = pd.concat([df_ev, df_ak], ignore_index=True) if (df_ev is not None and not df_ev.empty) else df_ak
        if df_ev is not None and not df_ev.empty:
            all_events.append(df_ev)
            
    if not all_events:
        log.error("No events found.")
        return False
        
    events_df = pd.concat(all_events, ignore_index=True)
    log.info(f"Found {len(events_df)} inclusion events. Calculating returns...")
    
    ret_df = calculate_returns(events_df)
    
    if ret_df.empty:
        log.error("No returns calculated.")
        return False
        
    stats = {}
    metrics = ["ret_T20_T1", "ret_T10_T1", "ret_T5_T1", "ret_T2_T1", "ret_T1_T0", "ret_T0_T5", "ret_T0_T10", "ret_T0_T20"]
    labels = ["提前20天", "提前10天", "提前5天", "提前1天", "纳入当日", "持有5天", "持有10天", "持有20天"]
    
    for code, (name, _months) in INDICES.items():
        sub_df = ret_df[ret_df['index_name'] == name]
        if sub_df.empty: continue
        
        avg_rets = []
        win_rates = []
        for m in metrics:
            series = sub_df[m].dropna()
            avg = series.mean() if not series.empty else 0
            win = (series > 0).mean() if not series.empty else 0
            avg_rets.append(round(avg * 100, 2))
            win_rates.append(round(win * 100, 2))
            
        stats[name] = {
            "labels": labels,
            "avg_returns": avg_rets,
            "win_rates": win_rates,
            "count": len(sub_df)
        }
        
    for m in metrics:
        ret_df[m] = ret_df[m].apply(lambda x: round(x * 100, 2) if pd.notna(x) else None)
        
    ret_df = ret_df.sort_values("inclusion_date", ascending=False).fillna("").to_dict(orient="records")
    
    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stats": stats,
        "details": ret_df
    }
    
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
        
    log.info(f"Analysis complete. Results saved to {OUTPUT_JSON}")
    return True

if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
