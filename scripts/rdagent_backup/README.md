# RD-Agent PC 端脚本备份 (C:\rdagent, 非主仓库, 此处留存版本)

本会话(2026-06-21)关键改动:
- **predict_next_day.py / live_topk_dropout.py**: ptnn(TimesNet等)真正只吃批次精选因子；实盘清单从同批次×池×模型的最近有效持仓按回测 TopkDropout(topk=50,n_drop=5)演进，历史同时保存保留/卖出/补入/目标与原始信号Top50，避免每日全量换仓。
- **prediction_preflight.py**: 实盘预测 fail-closed 门禁；绑定最新行情日，核对 csi300/500/1000 的 PIT 成分唯一数与模型出分覆盖率，并为清单提供原子写入。
- **run_model.py**: save_artifacts缓存(pred/model/label存_cache, 未来秒级重算免重训); liquidity_validate(流动性过滤+冲击成本复验, RDAGENT_LIQ_VALIDATE=1触发)。
- **fast_backtest.py**(新): 从缓存pred秒级重测(换topk/过滤/成本免重训)。
- **_fix_rerun.py / _rerun_orphans.py**: 加flock GPU锁(/mnt/c/rdagent/.gpu_train.lock)串行不抢卡。

注: 这些在 PC 的 C:\rdagent 跑(WSL rdagent env), NAS 用不到。仅备份留档。

2026-07-13 因子挖矿修复:
- `build_csi300.py`: 按完整300只历史快照开关成员区间, 保留退出后重新纳入的空窗, 拒绝截断快照。
- `build_universe.py`: 通用 CSI300/500/1000 PIT 成分构建器；按 Tushare 月度
  `index_weight` 快照分页，接近 5000 行或快照不完整时递归拆分，严格校验每个
  Qlib 交易日的成分数，最后原子发布。本机成功后 NAS 写入失败只告警，不回滚
  本机文件。它用于替换 `C:\rdagent\build_universe.py` 旧的 `min/max` 桥接实现。
- `build_all_mine_history.py`: 将数据路径、模型接口和无指标错误与真正的“无赢家”分开记账。
- `factor_rdagent_screen.py`: 按股票池选择评估器；沪深300不再误用中证1000评估器。
- `factor_analysis.py`: 拒绝没有真实回测配置的工作区，且不再把兜底Top-N冒充有效因子。
- `refresh_rdagent_daily_pv.py`: 仅在Qlib日历前进后原子刷新量价H5，并同步未来标签安全截止日。
- `..\repair_rdagent_factor_mining.ps1`: 把备份脚本部署到 `C:\rdagent`, 将 `C:\qlib_data` 显式只读挂载到 Docker `/root/qlib_data`, 并修复 provider 路径。

单独重建中证500/1000历史成分：

```powershell
D:\anaconda3\python.exe -u C:\rdagent\build_universe.py csi500 csi1000
```

## Research SOTA and production champion separation

- `resolve_sota_ws.py --accepted-manifest <path> <trace>` resolves every accepted
  experiment in `trace.hist`, hashes its evaluated artifacts, and marks the
  non-dominated research Pareto candidates. The legacy no-option invocation still
  returns the single RD-Agent continuation workspace.
- `promote_production_champion.py` is dry-run by default. It accepts one or more
  `--candidate-batch` values and requires an exact-screen/FDR batch manifest plus
  like-for-like, cost-bearing OOS results with at least three seed rows. It jointly
  gates net excess return, IR, drawdown, worst-seed return and seed dispersion.
- Only `--commit` updates `sota_workspace.txt`, `final/effective_factors.json` and
  the auditable `final/production_champion.json`. A failed or incomplete gate does
  not change production pointers.
- `..\evaluate_rdagent_pareto_queue.ps1` consumes the accepted manifest for CSI300
  OHLCV runs. It evaluates up to four non-dominated workspaces by default, in net
  return/IR order; each candidate gets an exact screen, FDR intersection, durable
  workspace, and same-window three-seed backtest. Stable candidate-derived batch
  IDs deduplicate across traces, failures are isolated in a resumable queue JSON,
  and all completed batches enter one production tournament.
- Automatic pointer commits remain off by default. Set
  `RDAGENT_AUTO_SOTA_PROMOTION=1` only after reviewing shadow promotion manifests;
  `RDAGENT_PARETO_MAX_CANDIDATES` bounds per-run evaluation from 1 to 8.

Deployment remains centralized in the idempotent repair script:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\repair_rdagent_factor_mining.ps1 -RdagentRoot C:\rdagent
```

Before enabling `--commit`, rerun both the current incumbent and candidate with the
maintained `run_model.py` and `SEEDS=0,1,2`; older result rows intentionally lack
the strict seed/OOS contracts and will fail closed.

首次替换旧缓存时可加 `--full-refresh`；该模式必须能访问 Tushare，且完全不借用
旧快照补历史，CSI500/1000 还会校验最早快照、月度覆盖率和最大快照间隔。
验证期间可加 `--no-nas`，确认本机文件后再执行正常发布命令。Token 仅从
`TUSHARE_TOKEN`、`TUSHARE_TOKEN_FILE` 或 `C:\rdagent\data\.tushare_token`
读取，脚本不内嵌密钥。不传指数参数时默认只构建 `csi500 csi1000`。
