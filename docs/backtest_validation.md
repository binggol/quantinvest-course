# Independent backtest validation

QuantInvest uses three separate responsibilities:

1. Qlib is the primary research and portfolio replay engine.
2. The vn.py validator independently replays a frozen target and quote bundle.
3. A third comparison process reads both completed outputs and applies hard gates.

The vn.py process never imports Qlib and never reads Qlib orders, trades,
positions, daily NAV, or metrics.  Its bundle contains only dated target
weights, raw quote fields, execution parameters, provenance, and SHA256 hashes.

## Environment boundary

Do not add vn.py to the website `requirements.txt` or Docker image.  Qlib in
this repository uses NumPy 1.26, while vn.py 4.4 uses NumPy 2.  Create a
separate Python 3.11 environment:

```powershell
py -3.11 -m venv .venv-vnpy-validator
.\.venv-vnpy-validator\Scripts\python -m pip install -r requirements-vnpy-validator.txt
```

The bundle exporter runs in the existing Qlib environment.  The validator runs
in the isolated vn.py environment.

## Reproduce the published control

```powershell
python scripts\export_advisor_pro_validation_bundle.py `
  --audit data\advisor_pro_execution_audit_published_fixed.json `
  --out data\backtest_bundles\advisor_pro_published_fixed `
  --qlib-data C:\qlib_data\cn_data

.\.venv-vnpy-validator\Scripts\python scripts\validate_advisor_pro_vnpy.py `
  --bundle data\backtest_bundles\advisor_pro_published_fixed `
  --out data\advisor_pro_vnpy_validation_published_fixed.json

python scripts\compare_advisor_pro_engines.py `
  --qlib data\advisor_pro_execution_audit_published_fixed.json `
  --vnpy data\advisor_pro_vnpy_validation_published_fixed.json `
  --bundle data\backtest_bundles\advisor_pro_published_fixed `
  --out data\advisor_pro_engine_comparison_published_fixed.json
```

Use `data/advisor_pro_execution_audit.json`, bundle name
`advisor_pro_corrected`, and result suffix `corrected` for the corrected path.

## Current verified results

| Path | Periods | Attempts | Trades | Qlib final account | vn.py final account | Reproduced |
|---|---:|---:|---:|---:|---:|---|
| Published control | 33 | 1,452 | 1,405 | 1,009,613,770.48 | 1,009,613,770.53 | Yes |
| Corrected path | 28 | 1,198 | 1,163 | 404,049,614.43 | 404,049,614.44 | Yes |

The corrected exposure-matched hedged result is reproduced at 9.6701%
annualized return, 0.9990 Sharpe, and -15.8151% maximum drawdown.

Execution reproduction passes for both paths.  Publication remains blocked
until point-in-time ST and IPO/no-price-limit fields replace board fallbacks on
every attempted order.  The input schema already accepts `is_st`,
`has_price_limit`, `limit_pct`, `suspended`, and `rule_source` for that upgrade.

## Factor-selection contract

`scripts/rdagent_backup/run_model.py` now applies each batch manifest's
`effective_factors` and `all_features` to the Qlib handler before model fitting.
This prevents a screening result from being displayed while Qlib silently
trains on all evaluated factors.
