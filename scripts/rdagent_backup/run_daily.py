r"""每日编排: 交易日才重算主sleeve(regime_advisor_pro) + 抢跑(export_runup) 拷贝到群晖 + 检测买卖动作 + 到调仓日; 抢跑:当日建仓/平仓非空且变化 -> 微信/邮件推送; 状态存 C:\rdagent\data\.daily_state.json 防重复推送。挂Windows计划任务每交易日盘后跑 -> D:\anaconda3\python.exe C:\rdagent\run_daily.py
"""
import io,sys,os,json,subprocess,time,tempfile
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, io.UnsupportedOperation):
    pass
import tushare as ts
from notify import send_push
PY=r"D:\anaconda3\python.exe"
MAPPED_NAS=r"Z:\claude\qlib\data\csv_tmp"
UNC_NAS=r"\/app/qlib_data\csv_tmp"
RDAGENT_DIR=r"C:\rdagent"
PROJECT_DIR=r"C:\path\to\quantinvest-course"  # TODO: 改为你的项目路径
STATE=r"C:\rdagent\data\.daily_state.json"
TOKEN_FILE=r"C:\rdagent\data\.tushare_token"
tok=os.environ.get("TUSHARE_TOKEN", "").strip()
if not tok and os.path.isfile(TOKEN_FILE):
    tok=open(TOKEN_FILE, encoding="utf-8").read().strip()
TODAY=time.strftime("%Y%m%d")


def resolve_nas_path():
    configured = os.environ.get("QI_SHARED_DIR", "").strip()
    if configured:
        return configured
    unc_path = os.environ.get("QI_SHARED_UNC", "").strip() or UNC_NAS
    return MAPPED_NAS if os.path.isdir(MAPPED_NAS) else unc_path


NAS=resolve_nas_path()
QLIB_CALENDAR=os.environ.get(
    "QI_QLIB_CALENDAR",
    os.path.join(os.path.dirname(NAS), "cn_data", "calendars", "day.txt"),
)


def _record_failure(failures, message):
    failures.append(message)
    print(message, flush=True)


def _local_trade_day(failures, api_error):
    try:
        with open(QLIB_CALENDAR, encoding="utf-8") as f:
            days = sorted({line.strip().replace("-", "") for line in f if line.strip()})
        if not days:
            raise ValueError("empty Qlib calendar")
        if TODAY > days[-1]:
            raise ValueError(f"Qlib calendar ends at {days[-1]}")
        result = TODAY in set(days)
        print(f"[daily] Tushare交易日历不可用, 使用本地Qlib日历: {api_error}", flush=True)
        return result
    except Exception as local_error:
        _record_failure(
            failures,
            f"[daily] 交易日历失败: Tushare={api_error}; Qlib={local_error}",
        )
        return None


def is_trade_day(failures):
    try:
        pro=ts.pro_api(tok); df=pro.trade_cal(exchange='SSE',start_date=TODAY,end_date=TODAY)
        if df is None or len(df) != 1:
            raise ValueError("unexpected empty or duplicate trade calendar response")
        return int(df.iloc[0]['is_open'])==1
    except Exception as e:
        return _local_trade_day(failures, e)


def run_command(args, *, cwd, timeout, failures, label, **kwargs):
    try:
        subprocess.run(args, cwd=cwd, timeout=timeout, check=True, **kwargs)
        return True
    except Exception as e:
        _record_failure(failures, f"[run] {label} 失败 {e}")
        return False


def run(script, failures, timeout=1800, quiet=True):
    kwargs = {}
    if quiet:
        kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return run_command(
        [PY, os.path.join(RDAGENT_DIR, script)],
        cwd=RDAGENT_DIR,
        timeout=timeout,
        failures=failures,
        label=script,
        **kwargs,
    )


def _reject_json_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def publish_json(source, destination, failures):
    temp_path = None
    try:
        with open(source, "rb") as f:
            payload = f.read()
        json.loads(payload.decode("utf-8-sig"), parse_constant=_reject_json_constant)
        destination_dir = os.path.dirname(destination)
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(destination)}.", suffix=".tmp", dir=destination_dir
        )
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, destination)
        return True
    except Exception as e:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        _record_failure(failures, f"[publish] {source} -> {destination} 失败 {e}")
        return False


def cp(f, failures):
    return publish_json(os.path.join(RDAGENT_DIR, f), os.path.join(NAS, f), failures)


def run_and_publish(script, outputs, failures, **kwargs):
    started = time.time()
    if not run(script, failures, **kwargs):
        return False
    published = True
    for output in outputs:
        source = os.path.join(RDAGENT_DIR, output)
        try:
            if os.path.getmtime(source) < started - 2:
                raise RuntimeError("exporter did not refresh output")
        except Exception as e:
            _record_failure(failures, f"[output] {source} 无本次新产出 {e}")
            published = False
            continue
        published = cp(output, failures) and published
    return published


def publish_project_json(source, failures):
    return publish_json(source, os.path.join(NAS, os.path.basename(source)), failures)


def run_project_and_publish(script, outputs, failures, *, timeout=1800, args=()):
    """Run a project exporter and publish only files produced by this invocation."""
    sources = [os.path.join(PROJECT_DIR, "data", output) for output in outputs]
    started = time.time()
    if not run_command(
        [PY, os.path.join(PROJECT_DIR, "scripts", script), *args],
        cwd=PROJECT_DIR,
        timeout=timeout,
        failures=failures,
        label=script,
        env={**os.environ, "TUSHARE_TOKEN": tok},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ):
        return False
    published = True
    for source in sources:
        try:
            # Some legacy exporters return exit 0 without writing anything.  Do not
            # let that silently republish an old JSON as if today's refresh worked.
            if os.path.getmtime(source) < started - 2:
                raise RuntimeError("exporter did not refresh output")
        except Exception as e:
            _record_failure(failures, f"[output] {source} 无本次新产出 {e}")
            published = False
            continue
        published = publish_project_json(source, failures) and published
    return published


def jload(p):
    try: return json.load(open(p,encoding='utf-8-sig'))
    except Exception: return {}

def main():
    failures=[]
    trade_day=is_trade_day(failures)
    if trade_day is None:
        print("[daily] 交易日历不可用, 中止", flush=True)
        return 1
    if not trade_day and os.environ.get("RUNDAILY_FORCE")!="1":
        print(f"[daily] {TODAY} 非交易日, 跳过",flush=True); return 0
    print(f"[daily] {TODAY} 交易日 开始",flush=True)
    state=jload(STATE) if os.path.exists(STATE) else {}
    # A Monday holiday must not suppress the whole week's refresh.  The first
    # trading-day run in each calendar week owns the weekly jobs and retries them
    # on the next trading day if any component fails.
    weekly_slot=time.strftime("%Y-%W")
    weekly_due=state.get("last_weekly_refresh") != weekly_slot
    weekly_ok=True
    msgs=[]
    # 0) 行情数据自检+自动补全: 检测各股bin是否滞后于最新parquet, 滞后则全量重建(防创业板等漏更新导致持仓/回测取不到近期行情)
    run_command(
        [PY, os.path.join(RDAGENT_DIR, "verify_and_backfill_qlib.py")],
        cwd=RDAGENT_DIR,
        timeout=3900,
        failures=failures,
        label="verify_and_backfill_qlib.py",
    )
    # 1) 主sleeve
    adv_ok=run_and_publish(
        "regime_advisor_pro.py", ("regime_advisor_pro.json",), failures
    )
    adv=jload(os.path.join(RDAGENT_DIR, "regime_advisor_pro.json")) if adv_ok else {}
    tr=adv.get("trade",{})
    nb,nsl=tr.get("n_buy",0),tr.get("n_sell",0); nextreb=tr.get("next_rebalance","")
    # 错过调仓日 且有买卖动作且未推送(防重复, 按next_rebalance日期)
    if (nb or nsl) and TODAY>= (nextreb or "").replace("-","") and state.get("last_reb")!=nextreb:
        items=tr.get("items",[])
        buys=[i for i in items if i.get("action")=="buy"][:30]; sells=[i for i in items if i.get("action")=="sell"][:30]
        body="【主sleeve 季度调仓】regime=%s\n买入%d: %s\n卖出%d: %s"%(tr.get("regime"),nb,
            ', '.join(b.get('name',b.get('code','')) for b in buys), nsl,
            ', '.join(s.get('name',s.get('code','')) for s in sells))
        msgs.append(("📊主sleeve调仓",body)); state["last_reb"]=nextreb
    # 2) 抢跑: 先定向刷新未来20天要发报告股票的预测(防公司改披露日/临时发新预告), 再生成清单
    forecast_ok=run("pull_forecast_upcoming.py", failures)
    runup_ok=forecast_ok and run_and_publish(
        "export_runup.py", ("runup.json",), failures
    )
    browse_ok=forecast_ok and run_and_publish(
        "export_forecast_browse.py", ("forecast_browse.json",), failures
    )   # 全市场预告浏览(抢跑页分区, 仅浏览)
    ru=jload(os.path.join(RDAGENT_DIR, "runup.json")) if runup_ok else {}
    rb=ru.get("buy",[]); rs=ru.get("sell",[]); rbp=ru.get("buy_post",[])
    cur_codes=sorted([x.get("code") for x in rb]+["P:"+x.get("code") for x in rbp])
    if (rb or rs or rbp) and state.get("last_runup")!=cur_codes:
        body="【抢跑 今日】\n腿A建仓%d: %s\n腿A平仓%d\n腿B公告后买%d: %s"%(
            len(rb), ', '.join(x.get('name','') for x in rb[:20]), len(rs),
            len(rbp), ', '.join(x.get('name','') for x in rbp[:20]))
        msgs.append(("📈抢跑清单",body)); state["last_runup"]=cur_codes
    # 3) RSRS 指数择时(独立方向信号): 状态翻转(持有<->空仓)才推送
    rsrs_ok=run_and_publish("export_rsrs.py", ("rsrs.json",), failures)
    rj=jload(os.path.join(RDAGENT_DIR, "rsrs.json")) if rsrs_ok else {}
    idx=rj.get("indices",[])
    cur_rsrs={x.get("name"):x.get("state") for x in idx}
    if cur_rsrs and state.get("last_rsrs")!=cur_rsrs:
        flips=[f"{x['name']}→{x['state']}(分{x.get('score')})" for x in idx if x.get("flip")]
        if flips:  # 仅在真翻转时推送
            body="【RSRS指数择时 信号翻转】\n"+"\n".join(flips)+"\n(独立方向信号, 用于指数/ETF/定投仓位, 不并入三腿中性组合)"
            msgs.append(("🧭RSRS择时翻转",body))
        state["last_rsrs"]=cur_rsrs
    # 4) 回购事件腿(第四sleeve): 今日建仓清单变化才推送
    repo_ok=run_and_publish("export_repo.py", ("repo.json",), failures)
    rj2=jload(os.path.join(RDAGENT_DIR, "repo.json")) if repo_ok else {}
    rbt=rj2.get("buy_today",[]); rss=rj2.get("sell_soon",[])
    cur_repo=sorted([x.get("code") for x in rbt])
    if (rbt or rss) and state.get("last_repo")!=cur_repo:
        body="【回购腿 今日】\n建仓%d: %s\n临近平仓%d: %s\n(持有60交易日·指数对冲, 详见/repo页面)"%(
            len(rbt), ', '.join(x.get('name','') for x in rbt[:20]), len(rss),
            ', '.join(x.get('name','') for x in rss[:10]))
        msgs.append(("🔁回购腿建仓",body)); state["last_repo"]=cur_repo
    # 4.6) 指数纳入腿(第五sleeve): 半年调, 每周首个交易日重算。
    if weekly_due:
        weekly_ok = run_command(
            [PY, os.path.join(PROJECT_DIR, "scripts", "build_inclusion_sleeve.py")],
            cwd=PROJECT_DIR,
            timeout=1800,
            failures=failures,
            label="build_inclusion_sleeve.py",
            env={**os.environ, "TUSHARE_TOKEN": tok},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) and weekly_ok
        # 质量成长腿(第六sleeve): 周一重算, 生成 _sleeve_quality.pkl 供 export_combo 读
        weekly_ok = run_command(
            [PY, os.path.join(PROJECT_DIR, "scripts", "build_quality_sleeve.py")],
            cwd=PROJECT_DIR,
            timeout=1800,
            failures=failures,
            label="build_quality_sleeve.py",
            env={**os.environ, "TUSHARE_TOKEN": tok},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) and weekly_ok
        # 数据操作台监控的是历史纳入研究本身，不是上述 sleeve 缓存。
        weekly_ok = run_project_and_publish(
            "export_index_inclusion.py",
            ("index_inclusion.json",),
            failures,
            timeout=1800,
        ) and weekly_ok
    # 4.7) 指数纳入 准成分预测 (每日记录→复现度, 生效后对比真实命中率). 必须每日跑.
    predict_ok=run_command(
        [PY, os.path.join(PROJECT_DIR, "scripts", "predict_inclusion.py")],
        cwd=PROJECT_DIR,
        timeout=600,
        failures=failures,
        label="predict_inclusion.py",
        env={**os.environ, "TUSHARE_TOKEN": tok},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pj = os.path.join(PROJECT_DIR, "data", "inclusion_predict.json")
    if predict_ok:
        publish_project_json(pj, failures)
    # 指数纳入实盘清单每天刷新；它依赖上面的历史研究及可选官方公告。
    run_project_and_publish(
        "export_index_inclusion_pro.py",
        ("index_inclusion_pro.json",),
        failures,
        timeout=600,
    )
    # 4.75) 卖出提醒: 自选股按经典卖出规则给信号, level>=2(趋势转空/止损)推送
    sell_ok=run_command(
        [PY, os.path.join(PROJECT_DIR, "scripts", "export_sell_signals.py")],
        cwd=PROJECT_DIR,
        timeout=300,
        failures=failures,
        label="export_sell_signals.py",
        env={**os.environ, "TUSHARE_TOKEN": tok},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sj = os.path.join(PROJECT_DIR, "data", "sell_alerts.json")
    if sell_ok:
        publish_project_json(sj, failures)
        sd = jload(sj); al = [x for x in sd.get("alerts", []) if x.get("level", 0) >= 2]
        cur = sorted(x["code"] for x in al)
        if al and state.get("last_sell") != cur:
            body = "【卖出提醒】%d只触发(趋势破坏/止损):\n" % len(al) + "\n".join(
                f"{x.get('name')}({x['code']}) {';'.join(x['signals'])}" for x in al[:15])
            msgs.append(("🔔卖出提醒", body)); state["last_sell"] = cur
    # 4.8) 纳入评审日历提醒: 即将发公告/公告窗口/待生效 三段(每段首次触发推一次)
    calendar_ok=run_command(
        [PY, os.path.join(PROJECT_DIR, "scripts", "inclusion_calendar.py")],
        cwd=PROJECT_DIR,
        timeout=120,
        failures=failures,
        label="inclusion_calendar.py",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cj = os.path.join(PROJECT_DIR, "data", "inclusion_calendar.json")
    if calendar_ok:
        publish_project_json(cj, failures)
        cd = jload(cj)
        for a in cd.get("alerts", []):
            key = "incl_cal_" + a["name"] + "_" + a["stage"]
            if not state.get(key):
                msgs.append(("🗓️纳入评审提醒", a["msg"])); state[key] = cd.get("today")
    # 5) 组合配置(六腿book + RSRS overlay): 静态权重, 刷新overlay; 演进历史同步
    combo_ok=run_and_publish(
        "export_combo.py", ("combo.json", "combo_history.json"), failures
    )
    # 5.4) 热榜避雷清单(日频): 当前同花顺热榜=关注度透支(未来20日-3.2%/t-14), 标红留痕不剔除。
    #      须在 export_combo_holdings 前跑+拷NAS, combo 的 rd() 才能从NAS读到并给持仓标记。
    hot_ok=run_command(
        [PY, os.path.join(PROJECT_DIR, "scripts", "export_hot_avoid.py")],
        cwd=PROJECT_DIR,
        timeout=300,
        failures=failures,
        label="export_hot_avoid.py",
        env={**os.environ, "TUSHARE_TOKEN": tok},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if hot_ok:
        for fn in ("hot_avoid.json", "hot_avoid_history.json"):
            publish_project_json(os.path.join(PROJECT_DIR, "data", fn), failures)
    # 5.5) 组合总买入清单(落地): 各腿当前持仓×权重汇总
    holdings_ok=run_command(
        [PY, os.path.join(PROJECT_DIR, "scripts", "export_combo_holdings.py")],
        cwd=PROJECT_DIR,
        timeout=120,
        failures=failures,
        label="export_combo_holdings.py",
        env={**os.environ, "SHARED_DIR": NAS},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if holdings_ok:
        for fn in ("combo_holdings.json", "combo_holdings_history.json"):
            publish_project_json(os.path.join(PROJECT_DIR, "data", fn), failures)
    # 6) 打新: 今日有可申购新股才推送(别漏了)
    ipo_ok=run_and_publish("export_ipo.py", ("ipo.json",), failures)
    ij=jload(os.path.join(RDAGENT_DIR, "ipo.json")) if ipo_ok else {}
    tb=ij.get("today_buy",[])
    if tb and state.get("last_ipo")!=ij.get("today"):
        body="【打新 今日可申购】%d只\n%s\n(顶格打满·中签首日卖·详见/ipo页面)"%(
            len(tb), '\n'.join(f"{x.get('name')} 申购码{x.get('sub_code')} 发行价{x.get('price')}" for x in tb[:15]))
        msgs.append(("🎯今日可打新",body)); state["last_ipo"]=ij.get("today")
    # 7) 海力士映射页面刷新; 当日尾盘信号由监听器14:30定时刷
    korea_ok=run_and_publish("export_korea_semi.py", ("korea_semi.json",), failures)
    # 8) 基本面对账 + 毛利率/造假避雷(财报季频数据, 每周首个交易日刷新)
    if weekly_due:
        fundamentals_ok=run_and_publish(
            "export_fundamentals.py",
            ("fundamentals.json", "margin_avoid.json", "fraud_avoid.json"),
            failures,
        )
        weekly_ok = fundamentals_ok and weekly_ok
        # 行业基本面(quantinvest脚本): 按行业扣非/营收增速/利润弹性排序
        industry_ok=run_command(
            [PY, os.path.join(PROJECT_DIR, "scripts", "export_industry.py")],
            cwd=PROJECT_DIR,
            timeout=300,
            failures=failures,
            label="export_industry.py",
            env={**os.environ, "TUSHARE_TOKEN": tok},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if industry_ok:
            industry_ok = publish_project_json(os.path.join(PROJECT_DIR, "data", "industry.json"), failures)
        weekly_ok = industry_ok and weekly_ok
        # 质量成长选股清单(第6腿实盘池)
        quality_ok=run_command(
            [PY, os.path.join(PROJECT_DIR, "scripts", "export_quality.py")],
            cwd=PROJECT_DIR,
            timeout=300,
            failures=failures,
            label="export_quality.py",
            env={**os.environ, "TUSHARE_TOKEN": tok},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if quality_ok:
            quality_ok = publish_project_json(os.path.join(PROJECT_DIR, "data", "quality.json"), failures)
        weekly_ok = quality_ok and weekly_ok
    # 9) 滚动业绩是操作台的周度资料，但日更成本很低；在当天 runup/Pro
    # 刷新后重建可确保公告口径与顾问篮子一致。
    run_command(
        [
            PY,
            os.path.join(PROJECT_DIR, "scripts", "build_rolling_earnings.py"),
            "--data-dir", os.path.join(PROJECT_DIR, "data"),
            "--shared-dir", NAS,
        ],
        cwd=PROJECT_DIR,
        timeout=300,
        failures=failures,
        label="build_rolling_earnings.py",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if weekly_due and weekly_ok:
        state["last_weekly_refresh"] = weekly_slot
    # 推送
    if msgs:
        for t,b in msgs:
            try:
                send_push(t,b)
            except Exception as e:
                _record_failure(failures, f"[push] {t} 失败 {e}")
    else:
        print("[daily] 今日无买卖动作(主sleeve非调仓日+抢跑无变化), 不推送",flush=True)
    try:
        with open(STATE,"w",encoding="utf-8") as f:
            json.dump(state,f,ensure_ascii=False)
    except Exception as e:
        _record_failure(failures, f"[daily] 状态写入失败 {e}")
    if failures:
        print(f"[daily] 完成但有{len(failures)}项失败",flush=True)
        return 1
    print("[daily] 完成",flush=True)
    return 0

if __name__=="__main__":
    sys.exit(main())
