import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import numpy as np


def main():
    import qlib
    from qlib.data import D
    qlib.init(provider_uri="Z:/claude/qlib/data/cn_data", region="cn")  # __main__守护避免worker重跑
    df = D.features(D.instruments("all"), ["$open", "$close"], start_time="2021-01-01", end_time="2025-12-31")
    op = df["$open"].unstack(level=0).sort_index(); cl = df["$close"].unstack(level=0).reindex(op.index)
    opv, clv = op.values, cl.values
    overnight = opv[1:] / clv[:-1] - 1
    intraday = clv / opv - 1
    f = lambda x: float(np.nanmean(x[np.isfinite(x)]) * 100)
    print(f"全市场(2021-25): 隔夜(收→次开) {f(overnight):+.3f}% | 日内(开→收) {f(intraday):+.3f}%")
    ret1 = clv[1:] / clv[:-1] - 1
    nxt_on = opv[2:] / clv[1:-1] - 1
    nxt_id = clv[2:] / opv[2:] - 1
    nxt_oc = clv[2:] / clv[1:-1] - 1
    on_s, id_s, oc_s = [], [], []
    for i in range(len(ret1) - 1):
        r = ret1[i]; v = np.isfinite(r) & np.isfinite(clv[i + 1])
        if v.sum() < 50:
            continue
        th = np.nanpercentile(r[v], 90); sel = v & (r >= th)
        on_s.append(np.nanmean(nxt_on[i][sel])); id_s.append(np.nanmean(nxt_id[i][sel])); oc_s.append(np.nanmean(nxt_oc[i][sel]))
    print(f"昨日涨幅前10%强势股 次日: 开盘卖(持夜) {np.nanmean(on_s)*100:+.3f}% | 日内(开→收) {np.nanmean(id_s)*100:+.3f}% | 持到收盘 {np.nanmean(oc_s)*100:+.3f}%")


if __name__ == "__main__":
    main()
