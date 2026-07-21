# -*- coding: utf-8 -*-
"""qlib 行情数据覆盖自检 + 自动补全。
问题背景: build_qlib_bin 是全量重建, 但没规律跑 -> 部分股票(尤其创业板/中小板)bin滞后于最新parquet,
导致持仓对比/回测取不到近期行情(如长川只到6-18, 主板到6-22)。
本脚本: 比对"最新parquet交易日" vs "各股bin最后日", 有滞后就跑 build_qlib_bin 补全, 写 qlib_coverage.json 供网页看数据新鲜度。

跑法:  python verify_and_backfill_qlib.py [--no-rebuild]
挂 run_daily.py 每交易日自动跑; 网页"🔄补全数据"按钮也可触发。
"""
import os, sys, json, glob, subprocess
import numpy as np
from datetime import datetime

QBASE = r"Z:\claude\qlib\data\cn_data"
PARQUET_DIR = r"Z:\claude\qlib\data\csv_tmp\tushare_daily"
BUILD = r"Z:\claude\qlib\scripts\build_qlib_bin.py"
SHARED = r"\/app/qlib_data\csv_tmp"
PY = sys.executable
NO_REBUILD = "--no-rebuild" in sys.argv


def latest_parquet_date():
    fs = sorted(glob.glob(os.path.join(PARQUET_DIR, "*.parquet")))
    return os.path.basename(fs[-1])[:8] if fs else None   # YYYYMMDD


def read_calendar():
    p = os.path.join(QBASE, "calendars", "day.txt")
    return open(p, encoding="utf-8").read().split() if os.path.exists(p) else []


def bin_last_date(code, cal):
    """读某股 close.day.bin 的最后交易日(qlib bin: 第0个float=start_index)。"""
    p = os.path.join(QBASE, "features", code, "close.day.bin")
    if not os.path.exists(p):
        return None
    try:
        a = np.fromfile(p, dtype="<f4")
        end = int(a[0]) + len(a[1:]) - 1
        return cal[end] if 0 <= end < len(cal) else None
    except Exception:
        return None


def _qlib_code(ts_code):
    """300604.SZ -> sz300604 (与 build_qlib_bin.code_dir_name 一致)."""
    c, suf = str(ts_code).split(".")
    return {"SZ": "sz", "SH": "sh", "BJ": "bj"}.get(suf, suf.lower()) + c


def latest_traded_codes():
    """最新parquet里"当日有成交"的股票(qlib码) —— 只有这些没更到才算真滞后(排除停牌/退市)。"""
    import pandas as pd
    fs = sorted(glob.glob(os.path.join(PARQUET_DIR, "*.parquet")))
    if not fs:
        return set()
    df = pd.read_parquet(fs[-1])
    col = "ts_code" if "ts_code" in df.columns else df.columns[0]
    return set(_qlib_code(c) for c in df[col].astype(str))


def scan():
    cal = read_calendar()
    cal_last = cal[-1] if cal else None
    pq_last = latest_parquet_date()
    target = (pq_last or (cal_last or "").replace("-", "")).replace("-", "")
    traded = latest_traded_codes()   # 当日有成交的股票, 只查这些
    lagging = []
    for code in traded:
        ld = bin_last_date(code, cal)
        # 真滞后 = 当日交易了, 但bin取不到/没更到当日(排除停牌退市: 它们不在traded里)
        if ld is None or ld.replace("-", "") < target:
            lagging.append({"code": code, "last": ld or "无数据"})
    return cal_last, pq_last, len(traded), lagging


def main():
    cal_last, pq_last, n_total, lagging = scan()
    n_lag = len(lagging)
    print(f"[coverage] 最新parquet={pq_last} 日历末={cal_last} 股票数={n_total} 滞后={n_lag}", flush=True)
    rebuilt = False
    if n_lag > 0 and not NO_REBUILD:
        print(f"[coverage] {n_lag}只滞后 -> 跑 build_qlib_bin 全量补全...", flush=True)
        r = subprocess.run([PY, BUILD], cwd=os.path.dirname(BUILD), capture_output=True, text=True, timeout=3600)
        rebuilt = (r.returncode == 0)
        print(f"[coverage] 重建 {'成功' if rebuilt else '失败 '+r.stderr[-300:]}", flush=True)
        if rebuilt:
            cal_last, pq_last, n_total, lagging = scan(); n_lag = len(lagging)   # 重扫确认
            print(f"[coverage] 重建后仍滞后={n_lag}", flush=True)
    out = {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latest_parquet": pq_last, "calendar_last": cal_last,
        "n_total": n_total, "n_lagging": n_lag,
        "lagging_sample": lagging[:40],
        "rebuilt": rebuilt,
        "ok": (n_lag == 0),
    }
    for path in (r"C:\rdagent\qlib_coverage.json", os.path.join(SHARED, "qlib_coverage.json")):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[coverage] 写 {path} 失败: {e}", flush=True)
    print(f"[coverage] {'[OK] 全覆盖' if out['ok'] else '[WARN] 仍有'+str(n_lag)+'只滞后'} -> qlib_coverage.json", flush=True)


if __name__ == "__main__":
    main()
