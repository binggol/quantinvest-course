"""
盘中实时循环: 交易时段每 ~60秒 重算 超短线/做T 信号(export_intraday_t), 拷到 NAS csv_tmp 供网页实时刷新。
交易时段外只等待。开机自启或盘前手动起: python scripts/intraday_t_loop.py
(Windows计划任务: 每交易日 07:58 启动, 15:05 自停亦可)
"""
import os, sys, time, subprocess, json, tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
PY = sys.executable
MAPPED_SHARED = r"Z:\claude\qlib\data\csv_tmp"
UNC_SHARED = r"\/app/qlib_data\csv_tmp"
NAS = os.environ.get("SHARED_DIR") or (
    MAPPED_SHARED if os.path.isdir(MAPPED_SHARED) else UNC_SHARED
)
INTERVAL = int(os.environ.get("INTRADAY_T_INTERVAL", "60"))


def in_session(hm):
    return ("09:25" <= hm <= "11:32") or ("12:58" <= hm <= "15:02")


def kr_session(hm):
    return "08:00" <= hm <= "14:35"   # 韩股 KST9:00-15:30 = 北京8:00-14:30(无午休)


def publish_intraday_snapshot(source, destination):
    with open(source, "r", encoding="utf-8-sig") as stream:
        payload = json.load(stream)
    today = datetime.now().date().isoformat()
    if not isinstance(payload, dict) or not str(payload.get("updated") or "").startswith(today):
        raise RuntimeError("intraday-T output is not today's snapshot")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows or int(payload.get("n", -1)) != len(rows):
        raise RuntimeError("intraday-T output failed row-count validation")
    fd, temp_name = tempfile.mkstemp(
        prefix=".intraday_t.json.", suffix=".tmp", dir=os.path.dirname(destination)
    )
    try:
        with os.fdopen(fd, "wb") as target, open(source, "rb") as origin:
            target.write(origin.read())
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_name, destination)
        temp_name = ""
    finally:
        if temp_name and os.path.exists(temp_name):
            os.remove(temp_name)


def main():
    if not os.path.isdir(NAS):
        raise FileNotFoundError(f"shared data directory unavailable: {NAS}")
    print(f"[intraday_t_loop] 启动, 间隔{INTERVAL}s, 交易时段刷新", flush=True)
    while True:
        hm = datetime.now().strftime("%H:%M")
        wd = datetime.now().weekday()
        if wd < 5 and kr_session(hm):   # 海力士分时(PC取Yahoo推NAS, NAS在华连不上) — 整个韩股时段
            try:
                result = subprocess.run([PY, os.path.join(HERE, "export_hynix_intraday.py")], cwd=PROJ, timeout=60,
                                        env={**os.environ, "SHARED_DIR": NAS}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode != 0:
                    print(f"[intraday_t_loop] {hm} Hynix refresh failed exit={result.returncode}", flush=True)
            except Exception as exc:
                print(f"[intraday_t_loop] {hm} Hynix refresh failed: {exc}", flush=True)
        if wd < 5 and in_session(hm):
            try:
                src = os.path.join(PROJ, "data", "intraday_t.json")
                previous = open(src, "rb").read() if os.path.exists(src) else None
                result = subprocess.run([PY, os.path.join(HERE, "export_intraday_t.py")], cwd=PROJ, timeout=110,
                                        env={**os.environ, "SHARED_DIR": NAS}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode != 0:
                    raise RuntimeError(f"export_intraday_t.py exit={result.returncode}")
                try:
                    publish_intraday_snapshot(src, os.path.join(NAS, "intraday_t.json"))
                except Exception:
                    if previous is None:
                        if os.path.exists(src):
                            os.remove(src)
                    else:
                        with open(src, "wb") as stream:
                            stream.write(previous)
                    raise
                print(f"[intraday_t_loop] {hm} 刷新完成", flush=True)
            except Exception as e:
                print(f"[intraday_t_loop] {hm} err {e}", flush=True)
            time.sleep(INTERVAL)
        elif hm > "15:05":
            print(f"[intraday_t_loop] {hm} 收盘, 退出(下一交易日07:58计划任务再起)", flush=True)
            break
        else:
            time.sleep(60)


if __name__ == "__main__":
    main()
