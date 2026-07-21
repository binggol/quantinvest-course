import json, subprocess, os, shutil
MC=r"C:\rdagent\model_curves.json"; NAS=r"Z:\claude\qlib\data\csv_tmp\model_curves.json"
VALID={"csi300","csi500","csi1000"}
def cur(): return json.load(open(MC,encoding="utf-8"))
# 删非标准池孤儿(如 alpha158_fund_csi500)
d=cur(); cc=d["curves"]
bad=[k for k in cc if k.startswith("alpha158_") and "::" in k and k.split("::")[0].replace("alpha158_","") not in VALID]
for k in bad: del cc[k]
if bad:
    json.dump(d,open(MC,"w",encoding="utf-8"),ensure_ascii=False)
    try: shutil.copy(MC,NAS)
    except: pass
    print("删非标准池孤儿:",bad,flush=True)
def oldm():
    c=cur()["curves"]; o=[]
    for k,v in c.items():
        if k.startswith("alpha158_") and "::" in k:
            u=k.split("::")[0].replace("alpha158_","")
            if u in VALID and v.get("dates") and v["dates"][0]<"2025-01-01": o.append((u,k.split("::")[1]))
    return o
GPU_LOCK = "/mnt/c/rdagent/.gpu_train.lock"   # 跨进程GPU训练互斥锁: watcher/各rerun脚本用同一把, 深度训练自动排队不抢卡
def wsl(env,scr):
    # ( flock 9; <训练命令含重定向> ) 9>锁文件 —— 拿到锁才跑, 跑完(子shell退出)自动释放; 别的编排器阻塞等待。
    inner = f"( flock 9; {env} {scr} ) 9>{GPU_LOCK}"
    subprocess.run(["wsl","-e","bash","-lc",f"source ~/miniconda3/etc/profile.d/conda.sh && conda activate rdagent && cd /mnt/c/rdagent && {inner}"])
om=oldm()
print(f"重跑{len(om)}个失败alpha158(无缓存): {om}",flush=True)
for i,(u,m) in enumerate(om):
    print(f"[{i+1}/{len(om)}] alpha158修复 {u}/{m}",flush=True)
    wsl(f"RDAGENT_ALPHA158=1 RDAGENT_UNIVERSE={u} RDAGENT_MODEL={m} SEEDS=0","python run_model.py >> /mnt/c/rdagent/_a158_fix.log 2>&1")
    try: shutil.copy(MC,NAS)
    except: pass
print("=== alpha158修复完成, 开始分池预测 ===",flush=True)
for u in ["csi500","csi1000"]:
    for m in ["lgb","xgb","catboost","ols","ridge","lasso","dlinear","timesnet","patchtst","itransformer"]:
        print(f"分池 {u}/{m}",flush=True)
        wsl(f"RDAGENT_ALPHA158=1 RDAGENT_UNIVERSE={u} RDAGENT_MODEL={m} RDAGENT_RETRAIN=1","python predict_next_day.py >> /mnt/c/rdagent/_pool_predict.log 2>&1")
        bf=rf"C:\rdagent\pool_buy_{u}_{m}.json"
        if os.path.exists(bf):
            try: shutil.copy(bf,rf"Z:\claude\qlib\data\csv_tmp\pool_buy_{u}_{m}.json")
            except: pass
print("ALL_DONE",flush=True)
