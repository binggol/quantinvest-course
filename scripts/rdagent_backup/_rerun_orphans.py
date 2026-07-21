#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""顺序重跑被删的4条孤儿曲线(csi1000/csi500深度模型), 一次一个独占GPU(无并发抢卡)。
run_model 会写回 model_curves.json+universe_arena(同期数据) + 同步NAS + 存_cache缓存。
在 Windows D:\anaconda3 python 跑, 每个 wsl 调用阻塞=串行。"""
import subprocess, datetime

JOBS = [("csi1000", "patchtst"), ("csi500", "patchtst"), ("csi500", "timesnet"), ("csi500", "itransformer")]
LOG = "/mnt/c/rdagent/_rerun_orphans.log"

for i, (u, m) in enumerate(JOBS, 1):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{i}/{len(JOBS)}] 重跑 {u}/{m} (独占GPU)...", flush=True)
    env = f"RDAGENT_ALPHA158=1 RDAGENT_UNIVERSE={u} RDAGENT_MODEL={m} SEEDS=0"
    cmd = (f"source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent "
           f"&& cd /mnt/c/rdagent && {env} python run_model.py >> {LOG} 2>&1")
    r = subprocess.run(["wsl", "-e", "bash", "-lc", cmd])
    print(f"[{datetime.datetime.now():%H:%M:%S}] [{i}/{len(JOBS)}] {u}/{m} 完成 (exit={r.returncode})", flush=True)

print("ALL_DONE", flush=True)
