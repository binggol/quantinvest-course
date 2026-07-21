"""
SK海力士(000660.KS)当日分时 -> hynix_intraday.json 推NAS。NAS在中国连不上Yahoo(403/墙),故PC取数(走代理)推过去。
韩股盘中 KST9:00-15:30 = 北京8:00-14:30。距昨收%, 阈值+2%=A股尾盘买半导体信号。
由 intraday_t_loop 在韩股时段每~60秒调用; 也可手动: D:/anaconda3/python.exe scripts/export_hynix_intraday.py
"""
import os, io, sys, json, urllib.request, datetime as dt
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DATA = os.path.dirname(os.path.abspath(__file__)) + "/../data"
OUT = os.path.join(DATA, "hynix_intraday.json")
NAS = os.environ.get("SHARED_DIR", r"Z:\claude\qlib\data\csv_tmp")
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
PROXY = os.environ.get("YF_PROXY", "http://127.0.0.1:7897")   # 境外数据走本地代理(墙)


def fetch():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/000660.KS?interval=5m&range=5d"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last = None
    for handlers in ([urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})],
                     [urllib.request.ProxyHandler({})]):   # 先代理, 再直连兜底
        try:
            return json.load(urllib.request.build_opener(*handlers).open(req, timeout=15))
        except Exception as e:
            last = e
    raise last


def _legacy_main():
    try:
        r = fetch()
        res = r["chart"]["result"][0]; meta = res["meta"]
        ts = res["timestamp"]; q = res["indicators"]["quote"][0]["close"]
        byday = {}
        for t, c in zip(ts, q):
            if c is None:
                continue
            kst = dt.datetime.utcfromtimestamp(t) + dt.timedelta(hours=9)
            byday.setdefault(kst.strftime("%Y-%m-%d"), []).append((kst.strftime("%H:%M"), float(c)))
        days = sorted(byday)
        if not days:
            out = {"ok": False, "message": "海力士暂无分时(休市)"}
        else:
            today = days[-1]; prev = [d for d in days if d < today]
            pre = byday[prev[-1]][-1][1] if prev else (meta.get("chartPreviousClose") or byday[today][0][1])
            bars = byday[today]; cp = bars[-1][1]
            out = {"ok": True, "date": today, "pre_close": pre, "cur_price": cp,
                   "cur_pct": round((cp / pre - 1) * 100, 2) if pre else None,
                   "market_state": meta.get("marketState"),
                   "points": [{"t": tm, "pct": round((c / pre - 1) * 100, 2)} for tm, c in bars],
                   "ts": dt.datetime.now().strftime("%H:%M:%S")}
    except Exception as e:
        out = {"ok": False, "message": f"海力士分时取数失败(PC): {e}"}

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    try:
        import shutil
        shutil.copy(OUT, os.path.join(NAS, "hynix_intraday.json"))
    except Exception as e:
        print(f"[hynix] 推NAS失败 {e}")
    print(f"[hynix] {out.get('date','-')} cur={out.get('cur_pct')}% pts={len(out.get('points',[]))} ok={out.get('ok')}")


def _atomic_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + f".{os.getpid()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def main():
    try:
        result = fetch()["chart"]["result"][0]
        meta = result["meta"]
        byday = {}
        for timestamp, close in zip(
            result.get("timestamp") or [],
            ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [],
        ):
            if close is None:
                continue
            kst = dt.datetime.utcfromtimestamp(timestamp) + dt.timedelta(hours=9)
            byday.setdefault(kst.strftime("%Y-%m-%d"), []).append(
                (kst.strftime("%H:%M"), float(close))
            )
        days = sorted(byday)
        if not days:
            raise RuntimeError("Yahoo returned no SK Hynix intraday bars")
        market_day = days[-1]
        if market_day != dt.date.today().isoformat():
            raise RuntimeError(f"latest SK Hynix session is stale: {market_day}")
        previous = [day for day in days if day < market_day]
        bars = byday[market_day]
        if len(bars) < 3:
            raise RuntimeError(f"SK Hynix intraday coverage is too short: {len(bars)} bars")
        pre_close = (
            byday[previous[-1]][-1][1]
            if previous
            else (meta.get("chartPreviousClose") or bars[0][1])
        )
        current_price = bars[-1][1]
        if not pre_close or current_price <= 0:
            raise RuntimeError("SK Hynix quote has no valid price")
        payload = {
            "ok": True,
            "date": market_day,
            "pre_close": pre_close,
            "cur_price": current_price,
            "cur_pct": round((current_price / pre_close - 1) * 100, 2),
            "market_state": meta.get("marketState"),
            "points": [
                {"t": bar_time, "pct": round((close / pre_close - 1) * 100, 2)}
                for bar_time, close in bars
            ],
            "ts": dt.datetime.now().strftime("%H:%M:%S"),
        }
        _atomic_json(OUT, payload)
        _atomic_json(os.path.join(NAS, "hynix_intraday.json"), payload)
        print(
            f"[hynix] {market_day} cur={payload['cur_pct']}% pts={len(payload['points'])} ok=True"
        )
        return 0
    except Exception as exc:
        print(f"[hynix] refresh failed, previous snapshot preserved: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
