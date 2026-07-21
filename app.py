"""
quantinvest Phase 1: K-line viewer with pinyin-initial search.

Endpoints:
  GET /                        -> index page
  GET /api/health              -> health check
  GET /api/search?q=xxx        -> stock search (code OR pinyin initials OR name substring)
  GET /api/kline?code=xxx&days=N -> OHLCV for ECharts candlestick
"""

import os
import json
import re
import subprocess
import sys
import time
import sqlite3
import logging
import secrets
import threading
import uuid
from collections import defaultdict, deque
from contextlib import closing
from pathlib import Path
from datetime import datetime, timedelta, date

import numpy as np
import pandas as pd
import tushare as ts
from flask import Flask, g, has_request_context, jsonify, redirect, render_template, request, send_file, session, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from werkzeug.middleware.proxy_fix import ProxyFix

from scripts.access_policy import PUBLIC_PATHS, csrf_required, navigation_for, required_feature
from scripts.member_data import MemberDataStore
from scripts.membership_auth import (
    PLAN_FEATURES,
    ROLE_FEATURES,
    MembershipStore,
)

APP_ROOT = Path(__file__).resolve().parent
_in_app_container = os.name != "nt" and Path("/app").is_dir()
_default_data_dir = Path("/app/data") if _in_app_container else APP_ROOT / "data"
_default_qlib_dir = Path("/app/qlib_data") if _in_app_container else APP_ROOT / "qlib_data"

PORT = int(os.environ.get("PORT", "5055"))
HOST = os.environ.get("HOST", "0.0.0.0")
QLIB_DATA_PATH = Path(os.environ.get("QLIB_DATA_PATH", str(_default_qlib_dir / "cn_data")))
PARQUET_DIR = Path(
    os.environ.get("PARQUET_DIR", str(_default_qlib_dir / "csv_tmp" / "tushare_daily"))
)
STOCK_META_DB = os.environ.get("STOCK_META_DB", str(_default_data_dir / "stock_meta.db"))
FINANCIALS_DB = os.environ.get("FINANCIALS_DB", str(_default_data_dir / "financials.db"))
MEMBERS_DB = Path(os.environ.get("QI_MEMBERS_DB", str(_default_data_dir / "members.db")))
# qlib 预测结果路径 (默认 ./data; 方案B 下指向 PC↔NAS 共享目录, 见 docker-compose)
PREDICT_JSON = Path(
    os.environ.get("PREDICT_JSON", str(Path(STOCK_META_DB).parent / "predictions.json"))
)
# 是否在本机(容器)做预测计算. 默认 0 = 计算在 PC 上跑、NAS 只读展示
PREDICT_COMPUTE_HERE = (os.environ.get("PREDICT_COMPUTE_HERE", "0") == "1")
# 方案B: 网页按钮写请求文件到共享目录, PC 监听脚本执行并回写状态
PREDICT_REQUEST = PREDICT_JSON.parent / "predict_request.json"
PREDICT_STATUS = PREDICT_JSON.parent / "predict_status.json"
# RD-Agent 产出 (有效因子 + 买入清单), 由 PC 的 export_rdagent.py 写到共享目录
RDAGENT_JSON = PREDICT_JSON.parent / "rdagent.json"
RDAGENT_REQUEST = PREDICT_JSON.parent / "rdagent_request.json"   # 网页按钮触发
RDAGENT_STATUS = PREDICT_JSON.parent / "rdagent_status.json"     # PC 监听回写
RDAGENT_BATCHES = PREDICT_JSON.parent / "rdagent_batches.json"   # 可选的有效因子批次索引 (PC 端导出)
FUND_FEAT_VALIDATE = PREDICT_JSON.parent / "fund_feat_validate.json"  # 28基本面特征对base正交增量榜 (validate_new_features.py)
FUND_RESID_SCREEN = PREDICT_JSON.parent / "fund_resid_screen.json"    # 基本面路挖完的因子resid增量榜 (watcher post-mine)
FUND_MINE_HISTORY = PREDICT_JSON.parent / "fund_mine_history.json"    # 每次基本面挖掘留痕(时间/因子/通过/结果) build_fund_mine_history.py
FUND_COMPARE_LATEST = PREDICT_JSON.parent / "fund_compare_latest.json"   # 🧬批次vs基线 最近一次对比结果
FUND_COMPARE_HISTORY = PREDICT_JSON.parent / "fund_compare_history.json" # 对比留痕(最近60次)
FUND_COMPARE_REQUEST = PREDICT_JSON.parent / "fund_compare_request.json" # 网页写 {batch,baseline,model} -> PC
FUND_COMPARE_STATUS = PREDICT_JSON.parent / "fund_compare_status.json"   # 对比进度 (PC回写)
RDAGENT_MODEL_RESULTS = PREDICT_JSON.parent / "model_results.json"  # 模型实验室回测结果 (PC 端写)
RDAGENT_MODEL_CURVES = PREDICT_JSON.parent / "model_curves.json"    # 回测净值曲线 (PC 端写, 回测对比页用)
TA_REQUEST = PREDICT_JSON.parent / "ta_request.json"      # TradingAgents 分析请求
TA_STATUS = PREDICT_JSON.parent / "ta_status.json"        # TradingAgents 进度
TA_RESULTS = PREDICT_JSON.parent / "ta_results.json"      # TradingAgents 分析报告
FACTOR_REQUEST = PREDICT_JSON.parent / "factor_request.json"  # 因子抽取请求 (Phase 4)
FACTOR_VALUES = PREDICT_JSON.parent / "factor_values.json"    # 因子值结果 (PC 回写)
FACTOR_STATUS = PREDICT_JSON.parent / "factor_status.json"    # 因子抽取进度
STRATEGY_RESULT = PREDICT_JSON.parent / "strategy_result.json"  # 单票·周频策略回测结果 (PC 写)
REGIME_ADVISOR = PREDICT_JSON.parent / "regime_advisor.json"    # 策略顾问: regime择时 当前推荐+战绩 (PC 写)
REGIME_ADVISOR_PRO = PREDICT_JSON.parent / "regime_advisor_pro.json"  # 策略顾问Pro: 增强版(regime+正交选股) (PC 写)
ADVISOR_PRO_BACKTEST_FILES = {
    "standard": APP_ROOT / "data" / "advisor_pro_n8_f15_d2_total100m_staggered_summary.json",
    "stress": APP_ROOT / "data" / "advisor_pro_n8_f15_d2_total100m_stress_staggered_summary.json",
    "top20": APP_ROOT / "data" / "advisor_pro_n20_f15_d2_staggered_summary.json",
}
ADVISOR_PRO_BACKTEST_CHART_DIR = APP_ROOT / "data" / "charts"
RESEARCH_LOG = PREDICT_JSON.parent / "research_log.json"  # 研究台: 因子/策略调查结论 (PC export_research.py 写)
RUNUP_JSON = PREDICT_JSON.parent / "runup.json"  # 抢跑第二sleeve: 每日预增抢跑买/卖/观察清单 (PC export_runup.py 写)
LATE_DISCLOSURE_JSON = PREDICT_JSON.parent / "late_disclosure.json"  # 年报晚披露(小/中盘)季节性信号: T+10超额+1.69%/四年全正 (PC export_late_disclosure.py 写)
REPO_JSON = PREDICT_JSON.parent / "repo.json"  # 回购第四sleeve: 回购公告后持有60日 每日清单 (PC export_repo.py 写)
COMBO_JSON = PREDICT_JSON.parent / "combo.json"  # 组合配置: 四腿中性book权重+各腿夏普+相关+合并 (PC export_combo.py 写)
IPO_JSON = Path(os.environ.get("IPO_JSON", str(PREDICT_JSON.parent / "ipo.json")))  # 打新提醒: 今日可申购/即将/近期上市
KOREA_SEMI_JSON = PREDICT_JSON.parent / "korea_semi.json"  # 海力士映射: 海力士涨>2%→A股半导体持1天 (PC export_korea_semi.py 写)
RSRS_JSON = PREDICT_JSON.parent / "rsrs.json"  # RSRS指数择时(独立方向信号, 不并入三腿中性组合) (PC export_rsrs.py 写)
ALPHAGEN_RESULT = PREDICT_JSON.parent / "alphagen_result.json"  # AlphaGen RL挖掘的alpha池 + 对base筛结果 (PC alphagen_listener.py 写)
ALPHAGEN_REQUEST = PREDICT_JSON.parent / "alphagen_request.json"  # 网页按钮触发: PC GPU上跑挖掘+评估
RDQUANT_RESULT = PREDICT_JSON.parent / "rdquant_result.json"  # RD-Agent-Quant(fin_quant)因子+模型联合优化 逐轮回测指标 (PC pc_listener.py 写)
RDQUANT_REQUEST = PREDICT_JSON.parent / "rdquant_request.json"  # 网页按钮触发: PC 上 rdagent fin_quant
RDAGENT_SCREEN = PREDICT_JSON.parent / "rdagent_screen.json"  # RD-Agent因子 对饱和base的正交性筛 (PC factor_rdagent_screen.py 写)
FUNDAMENTALS_JSON = PREDICT_JSON.parent / "fundamentals.json"  # 每股近3年报+最新一期 扣非增速/营收增速/毛利率 +行业 (PC export_fundamentals.py 写)
MARGIN_AVOID_JSON = PREDICT_JSON.parent / "margin_avoid.json"  # 避雷: 毛利率同比恶化最严重(流动性池) (PC export_fundamentals.py 写)
LEVERAGE_AVOID_JSON = PREDICT_JSON.parent / "leverage_avoid.json"  # 弱避雷: 融资净买入暴增=杠杆透支(反向IC-0.0155, 标记不剔除) (PC export_leverage_avoid.py 写)
FRAUD_AVOID_JSON = PREDICT_JSON.parent / "fraud_avoid.json"  # 避雷: Beneish-DSRI应收透支=财务造假嫌疑(前向半年-1.05%/t6.8) (PC export_fundamentals.py 写)
HOT_AVOID_JSON = PREDICT_JSON.parent / "hot_avoid.json"  # 避雷: 当前同花顺热榜股=关注度透支(20日-3.2%/t-14) (PC export_hot_avoid.py 写)
SNOWBALL_AVOID_JSON = PREDICT_JSON.parent / "snowball_avoid.json"  # 避雷: 场外雪球距敲入/敲出旗标(踩踏/抛压预警, 非alpha) (PC export_snowball.py 读W盘Excel写)
HYNIX_INTRADAY_JSON = PREDICT_JSON.parent / "hynix_intraday.json"  # 海力士当日分时(PC取Yahoo推, NAS在华连不上Yahoo) (PC export_hynix_intraday.py 写)
CROSS_MARKET_STORAGE_JSON = PREDICT_JSON.parent / "cross_market_storage.json"
CROSS_MARKET_STORAGE_BACKTEST = PREDICT_JSON.parent / "cross_market_storage_backtest.json"
REPORT_REQUEST = PREDICT_JSON.parent / "report_request.json"  # 个股研报按需生成请求 (网页写, PC监听器跑 gen_report.py)
REPORT_STATUS = PREDICT_JSON.parent / "report_status.json"   # 生成进度 (PC监听器回写)
REPORT_EDIT_DIR = Path(STOCK_META_DB).parent                 # 研报定性编辑覆盖层 (持久卷, 网页保存)
FORECAST_REQUEST = PREDICT_JSON.parent / "forecast_request.json"  # 财务预测三表 拉数据请求 (网页写, PC fetch_statements.py)
FORECAST_STATUS = PREDICT_JSON.parent / "forecast_status.json"    # 拉取进度 (PC回写)
FORECAST_EDIT_DIR = Path(STOCK_META_DB).parent               # 财务预测 假设/产品线 编辑覆盖层 (持久卷)
WATCHLIST_JSON = Path(STOCK_META_DB).parent / "watchlist.json"  # 自选股 (持久卷 /app/data)
POSITIONS_JSON = Path(STOCK_META_DB).parent / "positions.json"  # 我的持仓(代码+成本+日期, 供卖出提醒-8%止损/+25%止盈) (持久卷)
SELLS_HISTORY_JSON = Path(STOCK_META_DB).parent / "sells_history.json"  # 卖出历史台账(代码/成本/卖价/盈亏/持有天数) (持久卷)
INCLUSION_REQUEST = PREDICT_JSON.parent / "inclusion_request.json"  # 指数纳入重算请求 (网页写, PC跑 export_index_inclusion(_pro).py 并拷回 csv_tmp)
INCLUSION_STATUS = PREDICT_JSON.parent / "inclusion_status.json"    # 纳入重算进度 (PC回写)
REFRESH_REQUEST = PREDICT_JSON.parent / "refresh_request.json"  # 通用页面刷新请求 (网页写kind, PC watcher跑对应export脚本拷回csv_tmp)
REFRESH_STATUS = PREDICT_JSON.parent / "refresh_status.json"    # 通用刷新进度 (PC回写)
REFRESH_KINDS = {"rsrs", "ipo", "repo", "runup", "transfer_events", "earnings_times", "industry", "quality", "sell", "intraday_t", "backfill", "avoid", "hotavoid", "snowball", "chipmap", "cross_market", "top_risk", "money_outflow"}  # industry/quality/sell/intraday_t/hotavoid/snowball/transfer_events/earnings_times/top_risk/money_outflow走quantinvest脚本; backfill/avoid/chipmap=C:\rdagent
MONEY_OUTFLOW_JSON = PREDICT_JSON.parent / "money_outflow_signal.json"
HUJIN_ETF_FLOW_JSON = PREDICT_JSON.parent / "huijin_etf_flow.json"
HUJIN_ETF_SERIES_JSON = PREDICT_JSON.parent / "huijin_etf_share_series.json"
BATCH_GEN_REQUEST = PREDICT_JSON.parent / "batch_gen_request.json"  # 一键批量生成研报/预测请求 (网页写, PC alphagen_listener 串行跑)
BATCH_GEN_STATUS = PREDICT_JSON.parent / "batch_gen_status.json"    # 批量生成进度 (PC回写)
THESIS_REQUEST = PREDICT_JSON.parent / "thesis_request.json"  # 瓶颈链分析请求 (网页写theme, PC watcher跑 export_thesis.py 拷回csv_tmp)
THESIS_STATUS = PREDICT_JSON.parent / "thesis_status.json"    # 瓶颈链分析进度 (PC回写)
MINE_HISTORY = PREDICT_JSON.parent / "mine_history.json"                  # 挖矿进展台账: 各批次eff/all因子数+新增 (PC build_mine_history.py 写)
FUND_FEATURES_META = PREDICT_JSON.parent / "fund_features_meta.json"      # 基本面特征元信息: 覆盖率/范围/分布 (PC dump_fundamental_features.py 写)
PREDICT_A158_REQUEST = PREDICT_JSON.parent / "predict_a158_request.json"  # Alpha158预测请求 (网页写model, PC跑 predict_next_day.py RDAGENT_ALPHA158=1)
PREDICT_A158_STATUS = PREDICT_JSON.parent / "predict_a158_status.json"    # Alpha158预测进度 (PC回写)
PREDICT_A158_RESULT = PREDICT_JSON.parent / "predictions_a158.json"       # Alpha158预测买入清单 (PC predict_next_day.py 写, watcher拷回)
POOL_PREDICT_REQUEST = PREDICT_JSON.parent / "pool_predict_request.json"  # 分池买入清单一键全跑请求 (网页写universe)
POOL_PREDICT_STATUS = PREDICT_JSON.parent / "pool_predict_status.json"    # 分池预测进度 (PC回写)
BATCH_PREDICT_REQUEST = PREDICT_JSON.parent / "batch_predict_request.json"  # 用某OHLCV批次因子全模型预测csi300次日清单 (网页写batch)
BATCH_PREDICT_STATUS = PREDICT_JSON.parent / "batch_predict_status.json"    # 批次预测进度 (PC回写)
BUYLIST_HISTORY = PREDICT_JSON.parent / "buylist_history.json"            # 每次生成的次日清单留痕(时间/批次/池/模型/hits), PC predict_next_day 追加
ALPHA158_ARENA_RESULT = PREDICT_JSON.parent / "alpha158_arena.json"       # Alpha158模型擂台: 各模型在全Alpha158上的IR/超额/回撤 (PC run_model.py RDAGENT_ALPHA158=1 写)
ALPHA158_ARENA_REQUEST = PREDICT_JSON.parent / "alpha158_arena_request.json"  # 擂台跑模型请求 (网页写model)
ALPHA158_ARENA_STATUS = PREDICT_JSON.parent / "alpha158_arena_status.json"    # 擂台进度 (PC回写)
UNIVERSE_ARENA_RESULT = PREDICT_JSON.parent / "universe_arena.json"           # 股票池擂台: universe×model 的IR/超额/回撤
UNIVERSE_ARENA_REQUEST = PREDICT_JSON.parent / "universe_arena_request.json"  # 股票池回测请求 (网页写 universe+model)
UNIVERSE_ARENA_STATUS = PREDICT_JSON.parent / "universe_arena_status.json"    # 股票池回测进度
BATCH_ARENA_RESULT = PREDICT_JSON.parent / "batch_arena.json"                 # 批次擂台: batch×universe×model 的IR/超额/回撤
BATCH_ARENA_REQUEST = PREDICT_JSON.parent / "batch_arena_request.json"        # 批次擂台回测请求 (网页写 batch+universe+model)
BATCH_ARENA_STATUS = PREDICT_JSON.parent / "batch_arena_status.json"          # 批次擂台回测进度
THESIS_PRESET = ["AI算力", "电网 特高压", "人形机器人", "低空经济", "固态电池",
                 "先进封装 Chiplet", "可控核聚变", "卫星互联网", "半导体设备", "光通信 CPO"]
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
DAILY_HOUR = int(os.environ.get("DAILY_UPDATE_HOUR", "21"))
DAILY_MINUTE = int(os.environ.get("DAILY_UPDATE_MINUTE", "0"))
DAILY_UPDATE_STATUS_PATH = Path(
    os.environ.get("QI_DAILY_UPDATE_STATUS", str(_default_data_dir / "daily_update_status.json"))
)
WEEKLY_FINANCIALS_STATUS_PATH = Path(
    os.environ.get(
        "QI_WEEKLY_FINANCIALS_STATUS",
        str(_default_data_dir / "weekly_financials_status.json"),
    )
)
DAILY_UPDATE_MAX_RETRIES = max(0, min(5, int(os.environ.get("QI_DAILY_UPDATE_MAX_RETRIES", "2"))))
DAILY_UPDATE_RETRY_BASE_MINUTES = max(
    1, min(180, int(os.environ.get("QI_DAILY_UPDATE_RETRY_BASE_MINUTES", "15")))
)
DAILY_UPDATE_RETRY_CUTOFF_HOUR = max(
    0, min(23, int(os.environ.get("QI_DAILY_UPDATE_RETRY_CUTOFF_HOUR", "23")))
)
DAILY_UPDATE_STALE_HOURS = max(1, int(os.environ.get("QI_DAILY_UPDATE_STALE_HOURS", "36")))

# 全局锁: 同一时刻只允许一个 backfill 任务跑, 避免重复 tushare 请求 + bin 写竞态
_backfill_lock = threading.Lock()
_trade_history_lock = threading.Lock()
_daily_update_status_lock = threading.Lock()
_weekly_financials_status_lock = threading.Lock()
_trade_cal_cache: dict[str, bool] = {}  # YYYY-MM-DD -> is_trading_day

# bin 字段列表 (跟 update_daily.py 保持一致)
BIN_FIELDS = ["open", "close", "high", "low", "volume", "change", "factor", "adj"]
BENCHMARK_INDEX_TS_CODES = ("000300.SH", "000905.SH", "000852.SH")
BENCHMARK_INDEX_CODES = frozenset(
    f"{ts_code[-2:].lower()}{ts_code[:6]}" for ts_code in BENCHMARK_INDEX_TS_CODES
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("app")

app = Flask(__name__)
if os.environ.get("QI_TRUST_PROXY", "0").strip().lower() in {"1", "true", "yes", "on"}:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
_configured_secret = (os.environ.get("SECRET_KEY") or os.environ.get("QI_SECRET_KEY") or "").strip()
_secret_placeholders = {
    "quantinvest-dev-secret-change-me",
    "replace_with_a_long_random_secret",
    "change-me",
}
_secret_is_strong = (
    len(_configured_secret) >= 32
    and _configured_secret not in _secret_placeholders
    and len(set(_configured_secret)) >= 8
)
app.config["SECRET_KEY"] = _configured_secret if _secret_is_strong else secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("QI_COOKIE_SECURE", "0").strip().lower() in {
    "1", "true", "yes", "on"
}
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    is_public_cacheable = request.path in PUBLIC_PATHS and request.path not in {
        "/login",
        "/logout",
        "/membership-expired",
    }
    if not is_public_cacheable and not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# ---------- membership auth and access policy ----------

TERMS_VERSION = os.environ.get("QI_TERMS_VERSION", "2026-07-11")
PLAN_LABELS = {
    "basic": "基础数据版",
    "data_pro": "数据专业版",
    "enterprise": "企业数据版",
}
FEATURE_LABELS = {
    "market_data": "行情与基础资料",
    "advanced_data": "高级数据工具",
    "data_export": "数据导出",
    "api_access": "系统接口",
    "member_workspace": "个人数据工作区",
    "internal_operations": "内部运营工具",
    "manage_members": "会员管理",
}
_membership_init_lock = threading.Lock()
_membership_init_signature: tuple[str, str, str] | None = None
_login_attempts: dict[str, deque[float]] = {}
_login_attempts_lock = threading.Lock()
_LOGIN_ATTEMPT_WINDOW_SECONDS = 15 * 60
_LOGIN_ATTEMPT_LIMIT = 5
_LOGIN_IP_ATTEMPT_LIMIT = 50
_LOGIN_ATTEMPT_KEY_LIMIT = 10_000
_safe_job_label_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_safe_factor_name_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
_allowed_models = {
    "", "lgb", "xgb", "catboost", "ols", "ridge", "lasso",
    "dlinear", "timesnet", "patchtst", "itransformer", "all",
}


def _membership_store() -> MembershipStore:
    return MembershipStore(MEMBERS_DB)


def _member_data_store() -> MemberDataStore:
    return MemberDataStore(MEMBERS_DB)


def _member_scope_id() -> int:
    if has_request_context():
        member = getattr(g, "current_member", None)
        if member and member.get("id") is not None:
            return int(member["id"])
    return 0


def _has_internal_access() -> bool:
    member = getattr(g, "current_member", None) if has_request_context() else None
    return _membership_store().has_feature(member, "internal_operations")


def _member_document(namespace: str, default, *, item_key: str = "default", legacy_path: Path | None = None):
    store = _member_data_store()
    marker = object()
    value = store.get(_member_scope_id(), namespace, item_key=item_key, default=marker)
    if value is not marker:
        return value
    member = getattr(g, "current_member", None) if has_request_context() else None
    if legacy_path and legacy_path.exists() and (not member or member.get("role") == "admin"):
        try:
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
            store.put(_member_scope_id(), namespace, legacy, item_key=item_key)
            log.info("migrated legacy %s data for member %s", namespace, _member_scope_id())
            return legacy
        except Exception as exc:
            log.warning("legacy %s migration skipped: %s", namespace, exc)
    return default


def _parse_bool(value: str | None, *, default: bool, fail_closed: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    log.error("invalid boolean configuration value: %r", value)
    return True if fail_closed else default


def _valid_job_label(value: str) -> bool:
    text = str(value or "").strip()
    return not text or (".." not in text and bool(_safe_job_label_re.fullmatch(text)))


def _valid_factor_name(value: str) -> bool:
    return bool(_safe_factor_name_re.fullmatch(str(value or "").strip()))


def _auth_enabled() -> bool:
    flag = os.environ.get("QI_AUTH_ENABLED")
    if app.config.get("TESTING") and flag is None:
        return False
    return _parse_bool(flag, default=True, fail_closed=True)


def _init_membership() -> None:
    global _membership_init_signature
    signature = (
        str(MEMBERS_DB.resolve()),
        os.environ.get("QI_ADMIN_EMAIL", ""),
        os.environ.get("QI_ADMIN_PASSWORD", ""),
    )
    if _membership_init_signature == signature:
        return
    with _membership_init_lock:
        if _membership_init_signature == signature:
            return
        store = _membership_store()
        store.init_db()
        _member_data_store().init_db()
        store.ensure_admin(signature[1], signature[2])
        _membership_init_signature = signature


def _is_public_auth_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/static/")


def _is_api_path(path: str) -> bool:
    return path.startswith("/api/")


def _safe_next_url(next_url: str | None) -> str:
    if (
        next_url
        and next_url.startswith("/")
        and not next_url.startswith("//")
        and not any(char in next_url for char in ("\\", "\r", "\n"))
    ):
        return next_url
    return url_for("index")


def _auth_response(status: int, message: str, login: bool = False, expired: bool = False):
    if _is_api_path(request.path):
        return jsonify({"ok": False, "error": message}), status
    if login:
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    if expired:
        return redirect(url_for("membership_expired"))
    titles = {
        400: "请求验证失败",
        403: "无权访问",
        503: "服务暂不可用",
    }
    return render_template(
        "access_denied.html",
        message=message,
        error_title=titles.get(status, "无法访问"),
    ), status


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _csrf_is_valid() -> bool:
    expected = session.get("csrf_token")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token")
    return bool(expected and supplied and secrets.compare_digest(str(expected), str(supplied)))


def _operator_info() -> dict[str, str]:
    return {
        "name": os.environ.get("QI_OPERATOR_NAME", "待配置"),
        "license_no": os.environ.get("QI_OPERATOR_LICENSE_NO", "待配置"),
        "email": os.environ.get("QI_CONTACT_EMAIL", "待配置"),
        "phone": os.environ.get("QI_CONTACT_PHONE", "待配置"),
        "icp_no": os.environ.get("QI_ICP_NO", "待配置"),
    }


def _login_key(email: str) -> str:
    # ProxyFix normalizes remote_addr when QI_TRUST_PROXY is enabled. Reading the
    # header directly would let an internet client rotate spoofed addresses.
    ip = request.remote_addr or "unknown"
    return f"{ip}|{MembershipStore.normalize_email(email)}"


def _login_ip_key() -> str:
    return f"{request.remote_addr or 'unknown'}|*"


def _login_is_limited(
    key: str,
    now: float | None = None,
    *,
    limit: int | None = None,
) -> bool:
    cutoff = (now or time.time()) - _LOGIN_ATTEMPT_WINDOW_SECONDS
    with _login_attempts_lock:
        attempts = _login_attempts.get(key)
        if attempts is None:
            return False
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        if not attempts:
            _login_attempts.pop(key, None)
            return False
        effective_limit = _LOGIN_ATTEMPT_LIMIT if limit is None else int(limit)
        return len(attempts) >= effective_limit


def _record_login_failure(key: str) -> None:
    now = time.time()
    with _login_attempts_lock:
        attempts = _login_attempts.get(key)
        if attempts is None:
            if len(_login_attempts) >= _LOGIN_ATTEMPT_KEY_LIMIT:
                cutoff = now - _LOGIN_ATTEMPT_WINDOW_SECONDS
                expired_keys = [
                    cache_key
                    for cache_key, cached in _login_attempts.items()
                    if not cached or cached[-1] < cutoff
                ]
                for cache_key in expired_keys:
                    _login_attempts.pop(cache_key, None)
            if len(_login_attempts) >= _LOGIN_ATTEMPT_KEY_LIMIT:
                oldest_key = min(_login_attempts, key=lambda item: _login_attempts[item][-1])
                _login_attempts.pop(oldest_key, None)
            attempts = deque()
            _login_attempts[key] = attempts
        attempts.append(now)


def _clear_login_failures(key: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(key, None)


@app.before_request
def require_membership():
    if (
        csrf_required(request.path, request.method)
        and not (app.config.get("TESTING") and not app.config.get("CSRF_TESTING"))
        and not _csrf_is_valid()
    ):
        return _auth_response(400, "invalid CSRF token")

    if (
        request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
        and request.is_json
        and not isinstance(request.get_json(silent=True), dict)
    ):
        if _is_api_path(request.path):
            return jsonify({"error": "JSON object required"}), 400
        return _auth_response(400, "JSON object required")

    if _is_public_auth_path(request.path):
        return None
    if not _auth_enabled():
        dev_role = os.environ.get("QI_DEV_ROLE", "admin").strip().lower()
        if dev_role not in {"member", "operator", "admin"}:
            dev_role = "admin"
        dev_plan = os.environ.get("QI_DEV_PLAN", "enterprise").strip().lower()
        if dev_plan not in PLAN_FEATURES:
            dev_plan = "enterprise"
        g.current_member = {
            "id": 0,
            "email": "development@local",
            "role": dev_role,
            "plan": dev_plan,
            "status": "active",
            "membership_until": "2099-12-31" if dev_role == "member" else None,
        }
        feature = required_feature(request.path, request.method)
        if feature and not _membership_store().has_feature(g.current_member, feature):
            message = "该页面不在当前数据套餐中。" if feature != "internal_operations" else "该功能仅限内部授权人员。"
            return _auth_response(403, message)
        return None
    if not _secret_is_strong and not app.config.get("TESTING"):
        return _auth_response(503, "server authentication secret is not configured")
    _init_membership()

    store = _membership_store()
    member = store.get_member_by_id(session.get("member_id"))
    if (
        not member
        or member.get("status") != "active"
        or not store.is_session_valid(member.get("id"), session.get("session_version"))
    ):
        session.clear()
        return _auth_response(401, "login required", login=True)

    member = dict(member)
    member.pop("password_hash", None)
    g.current_member = member
    if not store.has_active_membership(member):
        return _auth_response(403, "membership expired", expired=True)
    feature = required_feature(request.path, request.method)
    if feature and not store.has_feature(member, feature):
        message = "该页面不在当前数据套餐中。" if feature != "internal_operations" else "该功能仅限内部授权人员。"
        return _auth_response(403, message)
    return None


@app.context_processor
def inject_current_member():
    member = getattr(g, "current_member", None)
    store = _membership_store()
    features = sorted(
        set(PLAN_FEATURES.get(str((member or {}).get("plan") or "basic"), frozenset()))
        | set(ROLE_FEATURES.get(str((member or {}).get("role") or "member"), frozenset()))
    ) if member else []
    return {
        "current_member": member,
        "navigation_groups": navigation_for(member, store.has_feature),
        "member_features": features,
        "member_feature_labels": [FEATURE_LABELS.get(feature, feature) for feature in features],
        "plan_label": PLAN_LABELS.get(str((member or {}).get("plan") or "basic"), "基础数据版"),
        "csrf_token": _csrf_token(),
        "operator": _operator_info(),
        "terms_version": TERMS_VERSION,
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if _auth_enabled() and not _secret_is_strong and not app.config.get("TESTING"):
        return render_template(
            "access_denied.html",
            error_title="服务暂不可用",
            message="服务安全配置尚未完成。",
        ), 503
    if _auth_enabled():
        _init_membership()
    store = _membership_store()
    error = ""
    next_url = _safe_next_url(request.args.get("next"))
    if request.method == "POST":
        email = request.form.get("email", "")[:255]
        password = request.form.get("password", "")[:257]
        key = _login_key(email)
        ip_key = _login_ip_key()
        if _login_is_limited(key) or _login_is_limited(ip_key, limit=_LOGIN_IP_ATTEMPT_LIMIT):
            return render_template("login.html", error="尝试次数过多，请稍后再试。", next_url=next_url), 429
        accepted = request.form.get("accept_terms") == "1"
        member = store.verify_login(email, password)
        if member:
            needs_acceptance = member.get("terms_version") != TERMS_VERSION
            if needs_acceptance and not accepted and not app.config.get("TESTING"):
                return render_template(
                    "login.html",
                    error="请先阅读并同意服务协议、隐私政策和风险揭示。",
                    next_url=next_url,
                ), 400
            _clear_login_failures(key)
            if accepted and needs_acceptance:
                member = store.accept_terms(member["id"], TERMS_VERSION) or member
            session.clear()
            session.permanent = True
            session["member_id"] = member["id"]
            session["member_email"] = member["email"]
            session["session_version"] = int(member.get("session_version") or 1)
            session["csrf_token"] = secrets.token_urlsafe(32)
            if not store.has_active_membership(member):
                return redirect(url_for("membership_expired"))
            if next_url == url_for("index"):
                next_url = url_for("admin_dashboard") if store.has_feature(member, "internal_operations") else url_for("member_dashboard")
            return redirect(next_url)
        _record_login_failure(key)
        _record_login_failure(ip_key)
        error = "邮箱或密码错误，或账号已被禁用。"
    return render_template("login.html", error=error, next_url=next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/membership-expired")
def membership_expired():
    store = _membership_store()
    member = store.get_member_by_id(session.get("member_id"))
    if (
        not member
        or member.get("status") != "active"
        or not store.is_session_valid(member.get("id"), session.get("session_version"))
    ):
        session.clear()
        return redirect(url_for("login"))
    member = dict(member)
    member.pop("password_hash", None)
    return render_template("membership_expired.html", member=member)


@app.route("/member")
def member_dashboard():
    return render_template("member_dashboard.html")


@app.route("/account")
def account_page():
    return render_template("account.html")


@app.route("/admin")
def admin_dashboard():
    return render_template("admin_dashboard.html")


@app.route("/service-info")
def service_info():
    return render_template("service_info.html")


@app.route("/terms")
def terms_page():
    return render_template("terms.html")


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html")


@app.route("/risk-disclosure")
def risk_disclosure_page():
    return render_template("risk_disclosure.html")


def _require_admin():
    member = getattr(g, "current_member", None)
    if not _membership_store().has_feature(member, "manage_members"):
        if _is_api_path(request.path):
            return jsonify({"ok": False, "error": "admin required"}), 403
        return "Forbidden", 403
    return None


def _member_audit_state(member: dict | None) -> dict | None:
    if not member:
        return None
    return {
        "role": member.get("role"),
        "status": member.get("status"),
        "plan": member.get("plan"),
        "membership_until": member.get("membership_until"),
        "session_version": member.get("session_version"),
    }


@app.route("/admin/members", methods=["GET", "POST"])
def admin_members():
    denied = _require_admin()
    if denied:
        return denied

    store = _membership_store()
    message = request.args.get("message", "")
    error = ""
    if request.method == "POST":
        action = str(request.form.get("action", "save") or "save").strip()
        email = request.form.get("email", "")
        actor = getattr(g, "current_member", None) or {}
        try:
            if action not in {"save", "extend", "plan", "status", "revoke_sessions"}:
                raise ValueError("unsupported member action")
            before = store.get_member_by_email(email)
            if action == "extend":
                days = int(request.form.get("days", "0"))
                updated = store.extend_membership(email, days)
                if not updated:
                    raise ValueError("member not found")
                store.record_audit(
                    actor_member_id=actor.get("id"),
                    target_email=email,
                    action=action,
                    details={
                        "days": days,
                        "before": _member_audit_state(before),
                        "after": _member_audit_state(updated),
                    },
                    remote_addr=request.remote_addr,
                )
                return redirect(url_for("admin_members", message="会员期限已延长"))
            if action == "plan":
                plan = request.form.get("plan", "basic")
                updated = store.set_plan(email, plan)
                if not updated:
                    raise ValueError("member not found")
                store.record_audit(
                    actor_member_id=actor.get("id"),
                    target_email=email,
                    action=action,
                    details={
                        "plan": plan,
                        "before": _member_audit_state(before),
                        "after": _member_audit_state(updated),
                    },
                    remote_addr=request.remote_addr,
                )
                return redirect(url_for("admin_members", message="会员套餐已更新"))
            if action == "status":
                status = request.form.get("status", "active")
                updated = store.set_status(email, status)
                if not updated:
                    raise ValueError("member not found")
                store.record_audit(
                    actor_member_id=actor.get("id"),
                    target_email=email,
                    action=action,
                    details={
                        "status": status,
                        "before": _member_audit_state(before),
                        "after": _member_audit_state(updated),
                    },
                    remote_addr=request.remote_addr,
                )
                return redirect(url_for("admin_members", message="账号状态已更新"))
            if action == "revoke_sessions":
                updated = store.bump_session_version(email)
                if not updated:
                    raise ValueError("member not found")
                store.record_audit(
                    actor_member_id=actor.get("id"),
                    target_email=email,
                    action=action,
                    details={
                        "before": _member_audit_state(before),
                        "after": _member_audit_state(updated),
                    },
                    remote_addr=request.remote_addr,
                )
                return redirect(url_for("admin_members", message="该账号的现有会话已撤销"))
            password = request.form.get("password") or None
            role = request.form.get("role", "member")
            status = request.form.get("status", "active")
            plan = request.form.get("plan", "basic")
            updated = store.upsert_member(
                email=email,
                password=password,
                role=role,
                status=status,
                membership_until=request.form.get("membership_until") or None,
                plan=plan,
            )
            store.record_audit(
                actor_member_id=actor.get("id"),
                target_email=email,
                action="save",
                details={
                    "before": _member_audit_state(before),
                    "after": _member_audit_state(updated),
                    "password_changed": bool(password),
                },
                remote_addr=request.remote_addr,
            )
            return redirect(url_for("admin_members", message="会员信息已保存"))
        except Exception as exc:
            error = str(exc)
            try:
                store.record_audit(
                    actor_member_id=actor.get("id"),
                    target_email=email,
                    action=f"{action or 'unknown'}_failed",
                    details={"error": error[:300]},
                    remote_addr=request.remote_addr,
                )
            except Exception:
                log.exception("failed to record membership admin audit")

    return render_template(
        "admin_members.html",
        members=store.list_members(),
        audit_records=store.list_audit(50),
        today=date.today().isoformat(),
        message=message,
        error=error,
    )


# ---------- qlib bin reader (no qlib package needed) ----------

def _read_calendar() -> list[str]:
    p = QLIB_DATA_PATH / "calendars" / "day.txt"
    if not p.exists():
        return []
    return [line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _open_sqlite_readonly(path: str | Path) -> sqlite3.Connection:
    """Open an existing SQLite database without creating or mutating it."""
    db_path = Path(path)
    if not db_path.is_file():
        raise FileNotFoundError(str(db_path))
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)


def _read_bin(code: str, field: str) -> tuple[int, np.ndarray]:
    """Return (start_idx_in_calendar, values_float32)."""
    p = QLIB_DATA_PATH / "features" / code / f"{field}.day.bin"
    if not p.exists():
        return -1, np.array([], dtype=np.float32)
    arr = np.fromfile(p, dtype="<f4")
    if arr.size == 0:
        return -1, arr
    return int(arr[0]), arr[1:]


def _qlib_feature_readiness(calendar: list[str] | None = None) -> dict:
    """Check that the mounted Qlib feature store can serve a benchmark K-line."""
    calendar = calendar if calendar is not None else _read_calendar()
    features_dir = QLIB_DATA_PATH / "features"
    features_ready = features_dir.is_dir()
    readable_code = None
    if features_ready and calendar:
        for code in sorted(BENCHMARK_INDEX_CODES):
            try:
                start_idx, values = _read_bin(code, "close")
            except (OSError, ValueError, OverflowError):
                continue
            if (
                start_idx >= 0
                and start_idx < len(calendar)
                and values.size > 0
                and np.isfinite(values).any()
                and start_idx + values.size <= len(calendar)
            ):
                readable_code = code
                break
    return {
        "features": features_ready,
        "benchmark_close": readable_code is not None,
        "benchmark_code": readable_code,
    }


def _write_bin(code: str, field: str, start_idx: int, values: np.ndarray):
    """Atomically write a bin file: header float32(start_idx) + values."""
    stock_dir = QLIB_DATA_PATH / "features" / code
    stock_dir.mkdir(parents=True, exist_ok=True)
    p = stock_dir / f"{field}.day.bin"
    arr = np.hstack([np.float32(start_idx), values.astype("<f4")]).astype("<f4")
    tmp = p.with_suffix(".bin.tmp")
    arr.tofile(tmp)
    tmp.replace(p)


def _adj_normalization(adj_values: np.ndarray, n: int) -> tuple[np.ndarray, float, float] | None:
    """Return filled factors and the bin's stored/current adjustment bases.

    Price bins use ``raw * adj / max(adj)`` as a stable internal
    normalization. Standard qfq instead uses the latest factor as its base.
    """
    if n <= 0 or adj_values.size < n:
        return None
    adj = np.asarray(adj_values[:n], dtype=np.float64)
    adj[~np.isfinite(adj) | (adj <= 0)] = np.nan
    if np.isnan(adj).all():
        return None
    adj = pd.Series(adj).ffill().bfill().to_numpy(dtype=np.float64)
    stored_base = float(np.max(adj))
    latest_base = float(adj[-1])
    if not (np.isfinite(stored_base) and stored_base > 0 and
            np.isfinite(latest_base) and latest_base > 0):
        return None
    return adj, stored_base, latest_base


def _repair_ohlc_envelope(
    open_values: np.ndarray,
    high_values: np.ndarray,
    low_values: np.ndarray,
    close_values: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Expand only invalid high/low bounds and report repaired bar count."""
    open_arr = np.asarray(open_values, dtype=np.float64)
    high_arr = np.asarray(high_values, dtype=np.float64)
    low_arr = np.asarray(low_values, dtype=np.float64)
    close_arr = np.asarray(close_values, dtype=np.float64)
    repaired_high = np.maximum.reduce((high_arr, open_arr, close_arr))
    repaired_low = np.minimum.reduce((low_arr, open_arr, close_arr))
    repaired = (repaired_high != high_arr) | (repaired_low != low_arr)
    return repaired_high, repaired_low, int(np.count_nonzero(repaired))


def _qlib_code_to_ts(code: str) -> str:
    """sh600519 -> 600519.SH ; sz000001 -> 000001.SZ ; bj832317 -> 832317.BJ"""
    return f"{code[2:]}.{code[:2].upper()}"


# ============================================================
#  TUSHARE / BACKFILL
# ============================================================

def _tushare_api():
    if not TUSHARE_TOKEN:
        raise RuntimeError("TUSHARE_TOKEN not set")
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


def _is_trading_day(date_str: str) -> bool:
    """date_str: YYYY-MM-DD. Cached per process."""
    if date_str in _trade_cal_cache:
        return _trade_cal_cache[date_str]
    try:
        pro = _tushare_api()
        ymd = date_str.replace("-", "")
        df = pro.trade_cal(exchange="SSE", start_date=ymd, end_date=ymd)
        is_open = bool(len(df) > 0 and int(df.iloc[0]["is_open"]) == 1)
        _trade_cal_cache[date_str] = is_open
        return is_open
    except Exception as e:
        log.warning(f"trade_cal check failed for {date_str}: {e}")
        return False


def _fetch_one_day_parquet(date_str: str, force: bool = False) -> bool:
    """Try fetch one day's full-market daily+adj_factor parquet from tushare.
    Returns True if parquet now exists (either fetched or already there).

    ``force`` replaces an existing cache only after a complete fresh response.
    This is needed on IPO day when an early full-market snapshot can omit a
    stock that Tushare publishes later.
    """
    ymd = date_str.replace("-", "")
    p = PARQUET_DIR / f"{ymd}.parquet"
    if p.exists() and not force:
        return True
    try:
        pro = _tushare_api()
        daily = pro.daily(trade_date=ymd)
        if daily is None or daily.empty:
            log.info(f"tushare daily empty for {ymd}, data not yet published")
            return False
        adj = pro.adj_factor(trade_date=ymd)
        if adj is None or adj.empty:
            log.info(f"tushare adj_factor empty for {ymd}, refusing incomplete parquet")
            return False
        required = {"ts_code", "trade_date", "adj_factor"}
        if not required.issubset(adj.columns):
            log.warning(f"tushare adj_factor missing columns for {ymd}: {required - set(adj.columns)}")
            return False
        daily = daily.copy()
        adj = adj.copy()
        daily["trade_date"] = daily["trade_date"].astype(str)
        adj["trade_date"] = adj["trade_date"].astype(str)
        adj = adj.drop_duplicates(["ts_code", "trade_date"], keep="last")
        merged = daily.merge(
            adj[["ts_code", "trade_date", "adj_factor"]],
            on=["ts_code", "trade_date"], how="left",
        )
        merged["adj_factor"] = pd.to_numeric(merged["adj_factor"], errors="coerce")
        if merged["adj_factor"].isna().any() or merged["adj_factor"].le(0).any():
            log.warning(f"tushare adj_factor incomplete for {ymd}, refusing incomplete parquet")
            return False
        PARQUET_DIR.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        try:
            merged.to_parquet(tmp, index=False)
            os.replace(tmp, p)
        finally:
            tmp.unlink(missing_ok=True)
        log.info(f"fetched {ymd}.parquet from tushare ({len(merged)} rows)")
        return True
    except Exception as e:
        log.warning(f"tushare fetch failed for {ymd}: {e}")
        return False


def _append_dates_to_stock_bin(code: str, new_dates_ymd: list[str]) -> int:
    """Append given trading days' rows (from parquets) to one stock's bin files.
    Returns count of dates successfully appended. Handles qfq adjustment incl.
    rescaling old values if a new adj_factor exceeds the historical max."""
    ts_code = _qlib_code_to_ts(code)

    # collect new rows
    rows = []
    for ymd in new_dates_ymd:
        p = PARQUET_DIR / f"{ymd}.parquet"
        if not p.exists():
            continue
        df = pd.read_parquet(p)
        r = df[df["ts_code"] == ts_code]
        if r.empty:
            continue
        rows.append(r.iloc[0].to_dict())
    if not rows:
        return 0

    cal = _read_calendar()
    cal_set = set(cal)
    eligible_rows = []
    for row in rows:
        d_ymd = str(row["trade_date"])
        d_iso = f"{d_ymd[:4]}-{d_ymd[4:6]}-{d_ymd[6:8]}"
        if d_iso in cal_set:
            eligible_rows.append(row)
        else:
            log.info(f"[{code}] {d_iso} 尚未由全量任务发布到全局日历，网页不扩展日历")
    rows = eligible_rows
    if not rows:
        return 0

    # get current adj history for this stock
    start_idx, adj_v = _read_bin(code, "adj")
    if adj_v.size == 0:
        if code not in BENCHMARK_INDEX_CODES:
            # legacy stock with no adj.day.bin — caller should do full rebuild
            return -1
        # These price-only benchmark indices do not have corporate actions.
        start_idx, close_v = _read_bin(code, "close")
        if close_v.size == 0:
            return -1
        adj_v = np.ones(close_v.size, dtype=np.float32)

    bin_last_idx = start_idx + adj_v.size - 1
    cal_idx_map = {d: i for i, d in enumerate(cal)}

    # 各新行按日历位置归位, 只保留比 bin 末尾更新的交易日
    row_by_idx: dict[int, dict] = {}
    for row in rows:
        d_ymd = str(row["trade_date"])
        d_iso = f"{d_ymd[:4]}-{d_ymd[4:6]}-{d_ymd[6:8]}"
        ci = cal_idx_map.get(d_iso)
        if ci is not None and ci > bin_last_idx:
            row_by_idx[ci] = row
    if not row_by_idx:
        return 0
    newer = sorted(row_by_idx)
    max_new_idx = newer[-1]

    # 若 [bin_last_idx+1, max_new_idx] 间有缺口 (该股停牌、日历却有交易日),
    # 简单尾部 append 会让数据与日期错位 -> 返回 -1, 交调用方全量重建 (已正确填充停牌).
    if len(newer) != (max_new_idx - bin_last_idx):
        log.info(f"[{code}] append 检测到停牌缺口, 改走全量重建以保证日期对齐")
        return -1

    old_max = float(adj_v.max())
    overall_max = max(old_max, max(float(r.get("adj_factor") or 1.0) for r in row_by_idx.values()))

    # 新 adj 超过历史最大值时，重缩放历史内部归一化价格，保持存储基准一致。
    if overall_max > old_max + 1e-9:
        scale = old_max / overall_max
        log.info(f"[{code}] rescaling historical qfq by {scale:.6f} (new adj_factor > old max)")
        for field in ("open", "close", "high", "low"):
            si, vals = _read_bin(code, field)
            if vals.size > 0:
                _write_bin(code, field, si, vals * scale)

    # 连续 append 新交易日 (已确认无缺口)
    field_tails: dict[str, list[float]] = {f: [] for f in
        ("open", "close", "high", "low", "volume", "change", "factor", "adj")}
    for ci in newer:
        row = row_by_idx[ci]
        adj_now = float(row.get("adj_factor") or 1.0)
        ratio = adj_now / overall_max
        field_tails["open"].append(float(row["open"]) * ratio)
        field_tails["close"].append(float(row["close"]) * ratio)
        field_tails["high"].append(float(row["high"]) * ratio)
        field_tails["low"].append(float(row["low"]) * ratio)
        field_tails["volume"].append(float(row.get("vol", row.get("volume", 0))))
        field_tails["change"].append(float(row.get("pct_chg", 0)) / 100.0)
        field_tails["factor"].append(1.0)
        field_tails["adj"].append(adj_now)

    for field, new_vals in field_tails.items():
        si, vals = _read_bin(code, field)
        if vals.size == 0:
            continue
        merged = np.concatenate([vals, np.array(new_vals, dtype="<f4")])
        _write_bin(code, field, si, merged)

    return len(newer)


def _full_rebuild_one_stock(code: str) -> dict:
    """从所有 parquet 重建单只股票的所有 8 个 bin 文件 (慢但保证正确).

    用于以下场景:
      - 该股 bin 缺 adj 字段 (旧数据)
      - 该股 bin 数据落后 parquet 很多天
      - 数据有损坏
    """
    ts_code = _qlib_code_to_ts(code)
    parquet_files = sorted(PARQUET_DIR.glob("*.parquet"))
    if not parquet_files:
        return {"ok": False, "message": "无 parquet 文件"}

    log.info(f"[{code}] full rebuild from {len(parquet_files)} parquets ...")
    rows = []
    for p in parquet_files:
        try:
            df = pd.read_parquet(p, columns=None)
        except Exception as e:
            log.warning(f"  skip {p.name}: {e}")
            continue
        r = df[df["ts_code"] == ts_code]
        if not r.empty:
            rows.append(r.iloc[0].to_dict())
    if not rows:
        return {"ok": False, "message": "parquet 里没有该股票 (可能未上市或代码错误)"}

    sdf = pd.DataFrame(rows)
    # Keep the one-stock repair path on the same fail-closed data contract as
    # the full-market scheduler.  In particular, only a provenance-backed
    # first observation may have an undefined pct_chg.
    from scripts.update_daily import _validate_equity_history

    sdf = _validate_equity_history(
        sdf,
        source=f"historical parquet for {code}",
        repair_legacy_envelope=True,
        allow_legacy_initial_pct_chg=True,
    )
    sdf["trade_date"] = pd.to_datetime(sdf["trade_date"], format="%Y%m%d")
    sdf = sdf.sort_values("trade_date").reset_index(drop=True)

    # The global calendar belongs to the full-market scheduler. A page request
    # may rebuild one stock only for dates that scheduler has already published.
    cal = _read_calendar()
    if not cal:
        return {"ok": False, "message": "qlib 日历为空, 请先运行 update_daily.py"}
    cal_set = set(cal)
    date_iso = sdf["trade_date"].dt.strftime("%Y-%m-%d")
    ignored = int((~date_iso.isin(cal_set)).sum())
    if ignored:
        log.info(f"[{code}] ignored {ignored} rows newer than the scheduler-published calendar")
    sdf = sdf[date_iso.isin(cal_set)].copy()
    if sdf.empty:
        return {"ok": False, "message": "该股票数据尚未进入全局交易日历, 等待每日全量更新"}

    cal_idx = {d: i for i, d in enumerate(cal)}
    sdf["cal_idx"] = sdf["trade_date"].dt.strftime("%Y-%m-%d").map(cal_idx)

    # qfq 计算
    sdf["adj_factor"] = sdf["adj_factor"].astype("float32")
    max_adj = float(sdf["adj_factor"].max())
    ratio = sdf["adj_factor"] / max_adj
    n_trade = len(sdf)

    start_idx = int(sdf["cal_idx"].iloc[0])
    last_idx = int(sdf["cal_idx"].iloc[-1])

    vol_src = sdf.get("vol", sdf.get("volume", pd.Series([0] * n_trade)))
    chg_src = sdf.get("pct_chg", pd.Series([0] * n_trade))
    g = pd.DataFrame({
        "cal_idx": sdf["cal_idx"].astype(int).values,
        "open":   (sdf["open"] * ratio).astype("float32").values,
        "close":  (sdf["close"] * ratio).astype("float32").values,
        "high":   (sdf["high"] * ratio).astype("float32").values,
        "low":    (sdf["low"] * ratio).astype("float32").values,
        "volume": vol_src.astype("float32").values,
        "change": (chg_src.astype("float32") / 100.0).values,
        "adj":    sdf["adj_factor"].astype("float32").values,
    })
    # !! 关键: qlib bin 假设从 start_idx 起逐日连续. reindex 到 [start_idx, last_idx]
    # 连续区间, 停牌日前向填充, 否则停牌后所有值与日期错位 (历史复权价全错).
    g = (g.drop_duplicates("cal_idx", keep="last")
           .set_index("cal_idx")
           .reindex(range(start_idx, last_idx + 1)))
    susp = g["close"].isna()
    g["close"] = g["close"].ffill()
    for c in ("open", "high", "low"):       # 停牌日 O=H=L=前一日收盘
        g[c] = g[c].where(~susp, g["close"])
    g["adj"] = g["adj"].ffill()
    g["volume"] = g["volume"].fillna(0.0)
    g["change"] = g["change"].where(~susp, 0.0)
    g["factor"] = np.float32(1.0)

    for field in ("open", "close", "high", "low", "volume", "change", "factor", "adj"):
        _write_bin(code, field, start_idx, g[field].to_numpy(dtype="<f4"))

    n = len(g)
    first_iso, last_iso = cal[start_idx], cal[last_idx]
    log.info(f"[{code}] full rebuild done: {n} 天 ({n_trade} 交易 + {n - n_trade} 停牌填充), "
             f"{first_iso} ~ {last_iso}")
    return {"ok": True, "n_days": n, "first": first_iso, "last": last_iso}


def _get_today_iso() -> str:
    """today in Asia/Shanghai timezone (the container uses TZ=Asia/Shanghai)."""
    return datetime.now().strftime("%Y-%m-%d")


def ensure_freshness_for_stock(code: str) -> dict:
    """Try to ensure this stock's bin contains data up to the latest available day.
    Returns a status dict the caller can include in the response.

    Logic:
      - today's market_open ∈ [9:30, 15:00) → 用昨天数据为最新, 显示"今日交易中"
      - today's after_close (>= 15:00) + 今日是交易日 → 尝试拉今天 parquet, 成功就 append
      - 周末/节假日 → 用最近交易日数据
    """
    from scripts.update_daily import UpdateAlreadyRunning, qlib_update_lock

    with _backfill_lock:
        try:
            with qlib_update_lock(QLIB_DATA_PATH):
                return _ensure_freshness_inner(code)
        except UpdateAlreadyRunning:
            return {
                "status": "update_in_progress",
                "message": "全市场行情正在更新，当前显示上一版完整数据",
            }


def _ensure_freshness_inner(code: str) -> dict:
    now = datetime.now()
    today_iso = now.strftime("%Y-%m-%d")
    cal = _read_calendar()
    if not cal:
        return {"status": "no_calendar", "message": "qlib 日历为空, 请先运行 update_daily.py"}

    # find stock's bin last date
    start_idx, close_v = _read_bin(code, "close")
    if close_v.size == 0:
        # 首日新股在历史库中没有目录：收盘后先拉当日全市场数据，再尝试创建单股 bin。
        is_today_trade = _is_trading_day(today_iso)
        can_refresh_today = is_today_trade and now.hour >= 15
        if can_refresh_today:
            _fetch_one_day_parquet(today_iso)
        rebuilt = _full_rebuild_one_stock(code)
        # 当日 parquet 可能在新股行情发布前已缓存。强制替换一次后再重建，
        # 但不删除旧文件，刷新失败时仍保留原始缓存。
        if not rebuilt.get("ok") and can_refresh_today:
            if _fetch_one_day_parquet(today_iso, force=True):
                rebuilt = _full_rebuild_one_stock(code)
        if rebuilt.get("ok"):
            return {
                "status": "rebuilt",
                "message": "新上市股票已创建首份K线数据",
                "n_days": rebuilt["n_days"],
                "bin_first_date": rebuilt["first"],
                "bin_last_date": rebuilt["last"],
            }
        return {"status": "stock_not_in_db",
                "message": "该股票尚无已发布的日线数据；上市首日通常需收盘后等待Tushare发布"}
    bin_last_idx = start_idx + close_v.size - 1
    bin_last_date = cal[bin_last_idx] if bin_last_idx < len(cal) else cal[-1]

    # ---- step 1: 判断时段 + 若可拉今日 parquet 就先下 ----
    is_today_trade = _is_trading_day(today_iso)
    after_close = now.hour >= 15  # CST, container TZ should be set to Asia/Shanghai
    today_status = ""
    if is_today_trade and after_close:
        if _fetch_one_day_parquet(today_iso):
            today_status = "今日数据已发布"
        else:
            today_status = "今日交易日, 但 tushare 暂未发布数据 (一般 16:00 后才有)"
    elif is_today_trade and not after_close:
        today_status = "今日交易时段中, 行情未结算, 显示截至昨日数据"
    else:
        today_status = "今日非交易日"

    # ---- step 2: 永远检查 bin 是否落后于现有 parquet (不依赖今日是否拉得到) ----
    bin_last_ymd = bin_last_date.replace("-", "")
    existing_ymds = sorted(p.stem for p in PARQUET_DIR.glob("*.parquet"))
    missing_to_fill = [ymd for ymd in existing_ymds if ymd > bin_last_ymd]

    if not missing_to_fill:
        return {"status": "up_to_date", "message": today_status,
                "bin_last_date": bin_last_date}

    n_added = _append_dates_to_stock_bin(code, missing_to_fill)
    if n_added < 0:
        if code in BENCHMARK_INDEX_CODES:
            return {
                "status": "benchmark_refresh_required",
                "message": "基准指数需由独立 index_daily 更新流程刷新",
                "bin_last_date": bin_last_date,
            }
        # 增量 append 失败 (无 adj 字段) → 自动 fallback 到全量单股重建
        log.info(f"[{code}] append failed, falling back to full single-stock rebuild")
        rebuilt = _full_rebuild_one_stock(code)
        if rebuilt.get("ok"):
            return {
                "status": "rebuilt",
                "message": f"{today_status} (已自动重建该股全部历史)",
                "n_days": rebuilt["n_days"],
                "bin_last_date": rebuilt["last"],
                "bin_first_date": rebuilt["first"],
            }
        else:
            return {"status": "rebuild_failed", "message":
                    f"重建失败: {rebuilt.get('message', '未知错误')}"}

    new_bin_last_idx = bin_last_idx + n_added
    new_bin_last = _read_calendar()[new_bin_last_idx] if n_added > 0 else bin_last_date
    return {
        "status": "appended" if n_added > 0 else "no_change",
        "message": today_status,
        "appended_dates": n_added,
        "bin_last_date": new_bin_last,
    }


def load_ohlcv(code: str, last_n_days: int | None = None, adjust: str = "qfq") -> dict:
    """读取 OHLCV.

    bin 文件存的是内部归一化价格 ``raw * adj / max(adj)``，adj.day.bin
    存真实 adj_factor。读取时再转换为标准 Tushare 复权口径。
    支持三种 adjust:
      - 'qfq' (default): 前复权 = raw * adj / latest(adj)
      - 'hfq':           后复权 = raw * adj
      - 'none' / 'raw':  不复权 = raw
    """
    cal = _read_calendar()
    if not cal:
        return {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "adjust": adjust}

    start_idx, open_v = _read_bin(code, "open")
    close_idx, close_v = _read_bin(code, "close")
    high_idx,  high_v = _read_bin(code, "high")
    low_idx,   low_v = _read_bin(code, "low")
    vol_idx,   vol_v = _read_bin(code, "volume")
    adj_idx,   adj_v = _read_bin(code, "adj")  # 可能为空 (旧数据未重建)
    if open_v.size == 0:
        return {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "adjust": adjust}
    if len({start_idx, close_idx, high_idx, low_idx, vol_idx}) != 1:
        log.error("misaligned OHLCV bin headers for %s", code)
        return {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "adjust": adjust}

    n = min(open_v.size, close_v.size, high_v.size, low_v.size, vol_v.size)
    end_idx = start_idx + n
    if start_idx < 0 or n <= 0 or end_idx > len(cal):
        log.error(
            "OHLCV bin range is outside calendar for %s: start=%s count=%s calendar=%s",
            code, start_idx, n, len(cal),
        )
        return {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": [], "adjust": adjust}
    dates = cal[start_idx:end_idx]

    o = open_v[:n].astype(np.float64)
    c = close_v[:n].astype(np.float64)
    h = high_v[:n].astype(np.float64)
    l = low_v[:n].astype(np.float64)
    v = vol_v[:n].astype(np.float64)

    # 复权变换。三个价格指数没有公司行动，所有口径均等价于原始价格。
    actual_adjust = adjust
    adjustment_applicable = code not in BENCHMARK_INDEX_CODES
    norm = _adj_normalization(adj_v, n) if adj_idx == start_idx else None
    if not adjustment_applicable:
        norm = None
    elif norm is not None:
        adj_arr, stored_base, latest_base = norm
        if adjust == "qfq":
            ratio = stored_base / latest_base
        elif adjust == "hfq":
            ratio = stored_base
        else:  # none / raw
            ratio = stored_base / adj_arr
        o, c, h, l = o * ratio, c * ratio, h * ratio, l * ratio
    elif adjust != "qfq":
        actual_adjust = "qfq"

    # 截取最近 N 天 (last_n_days <= 0 或 None 表示全部历史)
    if last_n_days and last_n_days > 0 and len(dates) > last_n_days:
        offset = len(dates) - last_n_days
        dates = dates[offset:]
        o, c, h, l, v = o[offset:], c[offset:], h[offset:], l[offset:], v[offset:]

    # 防御: 停牌日在某些 bin 构建脚本里是 NaN; 前向填充, 避免 K线断裂 + jsonify 吐非法 NaN
    o = pd.Series(o).ffill().bfill().fillna(0).to_numpy()
    c = pd.Series(c).ffill().bfill().fillna(0).to_numpy()
    h = pd.Series(h).ffill().bfill().fillna(0).to_numpy()
    l = pd.Series(l).ffill().bfill().fillna(0).to_numpy()
    v = np.nan_to_num(np.asarray(v, dtype=float), nan=0.0)
    h, l, envelope_repairs = _repair_ohlc_envelope(o, h, l, c)

    result = {
        "dates": dates,
        "open": [round(float(x), 4) for x in o],
        "high": [round(float(x), 4) for x in h],
        "low": [round(float(x), 4) for x in l],
        "close": [round(float(x), 4) for x in c],
        "volume": [int(x) for x in v],
        "adjust": actual_adjust,
        "adjust_requested": adjust,
        "quality": {"ohlc_envelope_repairs": envelope_repairs},
    }
    if not adjustment_applicable:
        result["adjustment_applicable"] = False
    return result


def _eastmoney_daily_ohlcv(
    code: str,
    last_n_days: int | None = None,
    adjust: str = "qfq",
) -> dict:
    """Fetch response-only daily OHLCV for a stock missing from local Qlib data."""
    import requests

    raw = str(code or "").strip().lower()
    requested_adjust = str(adjust or "qfq").strip().lower()
    actual_adjust = "none" if requested_adjust in ("none", "raw") else requested_adjust
    fqt = {"none": "0", "qfq": "1", "hfq": "2"}.get(actual_adjust)
    if fqt is None:
        return {"dates": [], "open": [], "high": [], "low": [], "close": [],
                "volume": [], "adjust": requested_adjust,
                "adjust_requested": requested_adjust, "source": "eastmoney"}
    if len(raw) != 8 or raw[:2] not in ("sh", "sz", "bj") or not raw[2:].isdigit():
        return {"dates": [], "open": [], "high": [], "low": [], "close": [],
                "volume": [], "adjust": actual_adjust,
                "adjust_requested": requested_adjust, "source": "eastmoney"}
    market = "1" if raw.startswith("sh") else "0"
    limit = max(1, int(last_n_days)) if last_n_days else 10000
    try:
        response = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": f"{market}.{raw[2:]}",
                "klt": "101", "fqt": fqt, "lmt": str(limit),
                "end": "20500101",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56",
            },
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        body = payload.get("data") or {}
        rows = []
        for line in body.get("klines") or []:
            parts = str(line).split(",")
            if len(parts) < 6:
                continue
            try:
                rows.append((
                    parts[0], float(parts[1]), float(parts[2]),
                    float(parts[3]), float(parts[4]), int(float(parts[5])),
                ))
            except (TypeError, ValueError):
                continue
        rows.sort(key=lambda row: row[0])
        if last_n_days:
            rows = rows[-last_n_days:]
        return {
            "dates": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "close": [r[2] for r in rows],
            "high": [r[3] for r in rows],
            "low": [r[4] for r in rows],
            "volume": [r[5] for r in rows],
            "adjust": actual_adjust,
            "adjust_requested": requested_adjust,
            "source": "eastmoney",
            "name": str(body.get("name") or ""),
        }
    except Exception as e:
        log.warning(f"eastmoney daily fallback failed for {raw}: {e}")
        return {"dates": [], "open": [], "high": [], "low": [], "close": [],
                "volume": [], "adjust": actual_adjust,
                "adjust_requested": requested_adjust,
                "source": "eastmoney"}


def _demo_daily_ohlcv(code: str, last_n_days: int | None = None) -> dict:
    """Deterministic local-only data used solely by the isolated preview server."""
    calendar = _read_calendar()
    requested_count = last_n_days if last_n_days and last_n_days > 0 else 500
    if calendar:
        count = min(len(calendar), requested_count)
        dates = calendar[-count:]
    elif reference_date < datetime(2023, 2, 17):
        regime = "2020再融资规则调整"
        expected_months = [6, 18]
        rule_status = "pending_evidence"
        applicable_rules.append({
            "scope": "base_issuance",
            "name": regime,
            "status": "historical_transition_review",
            "effective_from": "2020-02-14",
            "baseline_months": expected_months,
            "url": "https://www.csrc.gov.cn/csrc/c100028/c1000837/content.shtml",
        })
    else:
        count = min(requested_count, 5000)
        end = pd.offsets.BDay().rollback(pd.Timestamp(date.today()))
        dates = [value.strftime("%Y-%m-%d") for value in pd.bdate_range(end=end, periods=count)]
    seed = sum((index + 1) * ord(char) for index, char in enumerate(str(code or ""))) % (2 ** 32)
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0002, 0.013, count)
    close = 50.0 * np.exp(np.cumsum(returns))
    open_values = close * (1 + rng.normal(0, 0.0035, count))
    high = np.maximum(open_values, close) * (1 + rng.uniform(0.001, 0.012, count))
    low = np.minimum(open_values, close) * (1 - rng.uniform(0.001, 0.012, count))
    volume = rng.integers(500_000, 4_000_000, count)
    return {
        "dates": dates,
        "open": [round(float(value), 4) for value in open_values],
        "close": [round(float(value), 4) for value in close],
        "high": [round(float(value), 4) for value in high],
        "low": [round(float(value), 4) for value in low],
        "volume": [int(value) for value in volume],
        "adjust": "qfq",
        "adjust_requested": "qfq",
        "source": "demo",
    }


_DEMO_STOCKS = (
    {"code": "sh600519", "ts_code": "600519.SH", "name": "贵州茅台", "industry": "白酒", "list_status": "L"},
    {"code": "sz000001", "ts_code": "000001.SZ", "name": "平安银行", "industry": "银行", "list_status": "L"},
    {"code": "sz300750", "ts_code": "300750.SZ", "name": "宁德时代", "industry": "电池", "list_status": "L"},
)


def _demo_search_hits(query: str) -> list[dict]:
    needle = str(query or "").strip().lower()
    if not needle:
        return []
    aliases = {
        "sh600519": "gzmt",
        "sz000001": "payh",
        "sz300750": "ndsd",
    }
    return [
        dict(item)
        for item in _DEMO_STOCKS
        if any(
            needle in candidate
            for candidate in (
                item["code"].lower(),
                item["ts_code"].lower(),
                item["name"].lower(),
                aliases[item["code"]],
            )
        )
    ]


def _normalize_market_code(value: str | None) -> str | None:
    raw = str(value or "").strip().lower()
    qlib_match = re.fullmatch(r"(sh|sz|bj)(\d{6})", raw)
    if qlib_match:
        return raw
    ts_match = re.fullmatch(r"(\d{6})\.(sh|sz|bj)", raw)
    if ts_match:
        return f"{ts_match.group(2)}{ts_match.group(1)}"
    if re.fullmatch(r"\d{6}", raw):
        exchange = "sh" if raw.startswith(("5", "6", "9")) else "bj" if raw.startswith(("4", "8")) else "sz"
        return f"{exchange}{raw}"
    return None


def _normalize_ohlcv_payload(data: object) -> dict | None:
    if not isinstance(data, dict):
        return None
    fields = ("dates", "open", "high", "low", "close", "volume")
    if any(not isinstance(data.get(field), (list, tuple, np.ndarray)) for field in fields):
        return None
    lengths = {len(data[field]) for field in fields}
    if len(lengths) != 1:
        return None
    count = lengths.pop()
    normalized = dict(data)
    normalized["dates"] = [str(value) for value in data["dates"]]
    try:
        parsed_dates = [datetime.strptime(value, "%Y-%m-%d").date() for value in normalized["dates"]]
    except (TypeError, ValueError):
        return None
    if any(current <= previous for previous, current in zip(parsed_dates, parsed_dates[1:])):
        return None
    try:
        for field in ("open", "high", "low", "close"):
            values = [float(value) for value in data[field]]
            if any(not np.isfinite(value) for value in values):
                return None
            normalized[field] = values
        volumes = [float(value) for value in data["volume"]]
        if any(not np.isfinite(value) or value < 0 for value in volumes):
            return None
        normalized["volume"] = [int(value) for value in volumes]
    except (TypeError, ValueError, OverflowError):
        return None
    for open_value, high_value, low_value, close_value in zip(
        normalized["open"], normalized["high"], normalized["low"], normalized["close"]
    ):
        tolerance = max(
            1e-6,
            max(abs(open_value), abs(high_value), abs(low_value), abs(close_value)) * 1e-7,
        )
        if high_value + tolerance < max(open_value, close_value, low_value):
            return None
        if low_value - tolerance > min(open_value, close_value, high_value):
            return None
    return normalized


# ---------- routes ----------

@app.route("/")
def index():
    if (
        not _has_internal_access()
        and os.environ.get("QI_DEMO_DATA", "0") == "1"
        and not request.args.get("code")
    ):
        return redirect(url_for("index", code="sh600519"))
    return render_template("index.html" if _has_internal_access() else "market.html")


@app.route("/screen")
def screen_page():
    return render_template("screen.html")


@app.route("/pattern")
def pattern_page():
    return render_template("pattern.html")


# ============================================================
#  /api/screen  基本面选股
# ============================================================

def _get_recent_periods(today: datetime | None = None) -> list[tuple[str, str]]:
    """返回 [过去3个年报 + 1个最新已披露季报] 的 (label, end_date) 列表, 旧→新.

    披露规则:
      - 年报: 4月底前 (5月起算最新一份是去年)
      - Q1:   4月底前 (5月起最新一份是当年Q1)
      - 半年: 8月底前 (9月起最新一份是当年中报)
      - Q3:   10月底前 (11月起最新一份是当年Q3)
    """
    today = today or datetime.now()
    y, m = today.year, today.month
    latest_annual_y = (y - 1) if m >= 5 else (y - 2)
    annuals = [(str(yr), f"{yr}1231") for yr in range(latest_annual_y - 2, latest_annual_y + 1)]
    if m >= 11:
        q_label, q_end = f"{y}Q3", f"{y}0930"
    elif m >= 9:
        q_label, q_end = f"{y}Q2", f"{y}0630"
    elif m >= 5:
        q_label, q_end = f"{y}Q1", f"{y}0331"
    else:
        q_label, q_end = f"{y-1}Q3", f"{y-1}0930"
    return annuals + [(q_label, q_end)]


def _qlib_to_ts_code(code: str) -> str:
    return f"{code[2:]}.{code[:2].upper()}"


def _ts_to_qlib_code(ts_code: str) -> str:
    code, exch = ts_code.split(".")
    return f"{exch.lower()}{code}"


def _yoy_growth(now: float | None, before: float | None) -> float | None:
    """同比增长率, before<=0 时返回 None (避免负数为分母时方向反掉)."""
    if now is None or before is None or pd.isna(now) or pd.isna(before):
        return None
    try:
        if before <= 0:
            return None
        return (now - before) / abs(before) * 100.0
    except (TypeError, ValueError):
        return None


@app.route("/api/screen/config")
def screen_config():
    """返回当前 '过去3年1期' 的标签 (供前端动态生成表单)"""
    periods = _get_recent_periods()
    return jsonify({
        "periods": [{"label": l, "end_date": e} for l, e in periods],
        "today": datetime.now().strftime("%Y-%m-%d"),
    })


@app.route("/api/screen")
def screen_query():
    """筛选符合条件的股票.

    查询参数:
      growth_min     扣非净利润同比增速最低值 (%), 默认 20
      growth_periods 启用的报告期 (逗号分隔标签, 如 "2023,2024,2025,2026Q1"), 默认全部启用
      roe_min        ROE 最低值 (%), 默认 8
      roe_period     ROE 取哪一期的值, 默认最新年报 (e.g. "2025")
      q_growth_on    是否启用 "最新一期单季扣非增速" 筛选 (1/0), 默认 0
      q_growth_min   单季扣非增速最低值 (%), 默认 0
      limit          返回多少条, 默认 200
    """
    periods = _get_recent_periods()
    period_map = dict(periods)  # label → end_date
    all_period_labels = [l for l, _ in periods]

    def bounded_number(name, default, minimum, maximum):
        try:
            value = float(request.args.get(name, str(default)))
        except (TypeError, ValueError):
            value = float(default)
        if not np.isfinite(value):
            value = float(default)
        return max(minimum, min(maximum, value))

    growth_min = bounded_number("growth_min", 20, -1000, 10000)
    enabled_growth = (request.args.get("growth_periods") or ",".join(all_period_labels)).split(",")
    enabled_growth = [p.strip() for p in enabled_growth if p.strip() in period_map]
    roe_min = bounded_number("roe_min", 8, -1000, 1000)
    roe_period = request.args.get("roe_period", all_period_labels[-2])  # 默认最新年报
    if roe_period not in period_map:
        roe_period = all_period_labels[-2]
    q_growth_on = (request.args.get("q_growth_on") or "0") == "1"
    q_growth_min = bounded_number("q_growth_min", 0, -1000, 10000)
    try:
        limit = max(1, min(500, int(request.args.get("limit", "200"))))
    except (TypeError, ValueError):
        limit = 200

    # 最新一期 = periods 里最后一个 (那个季报标签, 如 2026Q1), 用于单季扣非同比
    latest_q_label = all_period_labels[-1]

    # 拉所有需要的 end_date 数据 (含同比基期: each period 的去年同期)
    needed_ends = set()
    for label in enabled_growth + [roe_period, latest_q_label]:
        ed = period_map[label]
        needed_ends.add(ed)
        # 同比基期 = 去年同期 (年报: y-1 → y-2; Qx: y-1 Qx)
        py_end = f"{int(ed[:4]) - 1}{ed[4:]}"
        needed_ends.add(py_end)

    if not Path(FINANCIALS_DB).exists():
        return jsonify({
            "error": "财务数据库不存在, 请先运行 docker exec quantinvest python scripts/fetch_financials.py",
            "hits": [],
        }), 503

    placeholders = ",".join("?" * len(needed_ends))
    try:
        with closing(_open_sqlite_readonly(FINANCIALS_DB)) as conn:
            fina_df = pd.read_sql(
                f"SELECT ts_code, end_date, dt_profit_to_holder, roe, q_dtprofit FROM fina_indicators "
                f"WHERE end_date IN ({placeholders})",
                conn,
                params=list(needed_ends),
            )
    except (OSError, sqlite3.Error, pd.errors.DatabaseError) as exc:
        log.warning("financial screen database unavailable: %s", exc)
        return jsonify({
            "ok": False,
            "error": "financial data unavailable",
            "hits": [],
            "total_matched": 0,
        }), 503
    if fina_df.empty:
        return jsonify({"hits": [], "message": "财务表为空"})

    # 转为 dict: ts_code → end_date → {dt_profit, roe}
    by_code: dict[str, dict] = {}
    for r in fina_df.itertuples(index=False):
        # pandas 把 SQL NULL 读成 NaN(float), 不是 None; 统一清成 None,
        # 否则 NaN 会骗过下游的 `is None` 判断, 还会被 jsonify 序列化成非法 JSON 的 NaN.
        by_code.setdefault(r.ts_code, {})[r.end_date] = {
            "dt_profit": None if pd.isna(r.dt_profit_to_holder) else float(r.dt_profit_to_holder),
            "roe": None if pd.isna(r.roe) else float(r.roe),
            "q_dtprofit": None if pd.isna(r.q_dtprofit) else float(r.q_dtprofit),
        }

    # 关联股票名/行业
    try:
        with closing(_open_sqlite_readonly(STOCK_META_DB)) as meta_conn:
            meta = pd.read_sql(
                "SELECT ts_code, name, industry FROM stock_meta WHERE list_status = 'L'",
                meta_conn,
            )
    except (OSError, sqlite3.Error, pd.errors.DatabaseError) as exc:
        log.warning("financial screen metadata unavailable: %s", exc)
        return jsonify({
            "ok": False,
            "error": "stock metadata unavailable",
            "hits": [],
            "total_matched": 0,
        }), 503
    meta_map = {r.ts_code: (r.name, r.industry) for r in meta.itertuples(index=False)}

    # 应用筛选
    hits = []
    for ts_code, periods_data in by_code.items():
        ok = True
        growth_metrics = {}
        for label in enabled_growth:
            ed = period_map[label]
            py = f"{int(ed[:4]) - 1}{ed[4:]}"
            now_v = periods_data.get(ed, {}).get("dt_profit")
            past_v = periods_data.get(py, {}).get("dt_profit")
            g = _yoy_growth(now_v, past_v)
            growth_metrics[label] = g
            if g is None or g < growth_min:
                ok = False
                break
        if not ok:
            continue

        roe_end = period_map[roe_period]
        roe_v = periods_data.get(roe_end, {}).get("roe")
        if roe_v is None or roe_v < roe_min:
            continue

        # 最新一期单季扣非净利润同比 (单季 vs 去年同季单季)
        q_end = period_map[latest_q_label]
        q_py = f"{int(q_end[:4]) - 1}{q_end[4:]}"
        q_now = periods_data.get(q_end, {}).get("q_dtprofit")
        q_past = periods_data.get(q_py, {}).get("q_dtprofit")
        q_growth = _yoy_growth(q_now, q_past)
        if q_growth_on and (q_growth is None or q_growth < q_growth_min):
            continue

        name, industry = meta_map.get(ts_code, ("", ""))
        hits.append({
            "ts_code": ts_code,
            "code": _ts_to_qlib_code(ts_code),
            "name": name,
            "industry": industry,
            "growth": {k: round(v, 2) if v is not None else None for k, v in growth_metrics.items()},
            "q_growth": round(q_growth, 2) if q_growth is not None else None,
            "roe": round(roe_v, 2),
            "roe_period": roe_period,
        })

    hits.sort(key=lambda x: x["roe"], reverse=True)
    return jsonify({
        "hits": hits[:limit],
        "total_matched": len(hits),
        "criteria": {
            "growth_min": growth_min,
            "growth_periods": enabled_growth,
            "roe_min": roe_min,
            "roe_period": roe_period,
            "q_growth_period": latest_q_label,
            "q_growth_on": q_growth_on,
            "q_growth_min": q_growth_min,
        },
        "all_periods": [{"label": l, "end_date": e} for l, e in periods],
    })


# ============================================================
#  /api/pattern  欧奈尔杯柄形态选股 (周线)
# ============================================================

# 日历→周(W-FRI) 的映射, 进程内缓存一次 (全市场共用同一日历, 避免每股重复解析)
_WEEK_CACHE: dict | None = None


def _week_buckets() -> dict:
    global _WEEK_CACHE
    if _WEEK_CACHE is None:
        cal = _read_calendar()
        per = pd.to_datetime(cal).to_period("W-FRI")
        wid, _ = pd.factorize(per, sort=False)        # 日历各日所属周的递增 id
        wend = [str(per[np.where(wid == k)[0][0]].end_time.date())
                for k in range(int(wid.max()) + 1)] if len(cal) else []
        _WEEK_CACHE = {"wid": wid.astype(np.int64), "wend": wend, "n": len(cal), "cal": cal}
    return _WEEK_CACHE


def _daily_ohlc(code: str, days: int = 800) -> dict | None:
    """直接读 bin(前复权) 取最近 days 个交易日的日线. 返回 numpy 数组 dict 或 None."""
    si, close = _read_bin(code, "close")
    if close.size < 120:
        return None
    open_si, open_values = _read_bin(code, "open")
    high_si, high = _read_bin(code, "high")
    low_si, low = _read_bin(code, "low")
    vol_si, vol = _read_bin(code, "volume")
    adj_si, adj = _read_bin(code, "adj")
    if len({si, open_si, high_si, low_si, vol_si}) != 1:
        return None
    n = min(close.size, open_values.size, high.size, low.size, vol.size)
    off = max(0, n - days)
    cal = _week_buckets()["cal"]
    if si + n > len(cal):
        return None
    scale = 1.0
    norm = _adj_normalization(adj, n) if adj_si == si else None
    if norm is not None:
        _, stored_base, latest_base = norm
        scale = stored_base / latest_base
    open_out = open_values[off:n].astype(float) * scale
    close_out = close[off:n].astype(float) * scale
    high_out = high[off:n].astype(float) * scale
    low_out = low[off:n].astype(float) * scale
    high_out, low_out, envelope_repairs = _repair_ohlc_envelope(
        open_out, high_out, low_out, close_out
    )
    return {
        "dates": cal[si + off:si + n],
        "open": open_out,
        "high": high_out,
        "low": low_out,
        "close": close_out,
        "volume": vol[off:n].astype(float),
        "quality": {"ohlc_envelope_repairs": envelope_repairs},
    }


def _weekly_ohlc(code: str, days: int = 900) -> dict | None:
    """直接读 bin(前复权) + numpy reduceat 重采样成周线(周五收盘). 快路径, 无 pandas resample."""
    si, close = _read_bin(code, "close")
    if close.size < 60:
        return None
    open_si, open_values = _read_bin(code, "open")
    high_si, high = _read_bin(code, "high")
    low_si, low = _read_bin(code, "low")
    vol_si, vol = _read_bin(code, "volume")
    adj_si, adj = _read_bin(code, "adj")
    if len({si, open_si, high_si, low_si, vol_si}) != 1:
        return None
    n = min(close.size, open_values.size, high.size, low.size, vol.size)
    off = max(0, n - days)                              # 只取最近 days 个交易日
    s, e = si + off, si + n
    wb = _week_buckets()
    if e > wb["n"]:
        return None
    wid = wb["wid"][s:e]
    if wid.size < 60:
        return None
    scale = 1.0
    norm = _adj_normalization(adj, n) if adj_si == si else None
    if norm is not None:
        _, stored_base, latest_base = norm
        scale = stored_base / latest_base
    open_values = open_values[off:n] * scale
    close = close[off:n] * scale
    high = high[off:n] * scale
    low = low[off:n] * scale
    high, low, envelope_repairs = _repair_ohlc_envelope(open_values, high, low, close)
    vol = vol[off:n]

    starts = np.concatenate([[0], np.nonzero(np.diff(wid))[0] + 1])
    ends = np.append(starts[1:], wid.size)
    if starts.size < 12:
        return None
    return {
        "dates": [wb["wend"][wid[st]] for st in starts],
        "high": np.maximum.reduceat(high, starts).astype(float),
        "low": np.minimum.reduceat(low, starts).astype(float),
        "close": close[ends - 1].astype(float),
        "volume": np.add.reduceat(vol, starts).astype(float),
        "quality": {"ohlc_envelope_repairs": envelope_repairs},
    }


def _detect_cup_handle(w: dict, p: dict) -> dict | None:
    """在周线上检测"当前正在成形"的杯柄形态. 柄部结束于最新一周, 故命中即"当下".

    返回最佳形态的指标 dict, 或 None. 评分以"突破就绪度"为主
    (现价离买点越近 + 柄部缩量越明显 + 杯沿越对称, 分越高).
    """
    H, L, C, V = w["high"], w["low"], w["close"], w["volume"]
    dates = w["dates"]
    n = len(C)
    if n < p["cup_min"] + 4:
        return None
    cur = n - 1
    best = None
    # 柄 = 最近 hl 周 (R 为右杯沿)
    for hl in range(1, p["handle_max"] + 1):
        R = cur - hl
        if R < p["cup_min"]:
            continue
        RH = float(H[R])
        if RH <= 0:
            continue
        handle_hi = float(H[R + 1:].max())
        handle_lo = float(L[R + 1:].min())
        if handle_hi > RH * 1.005:                 # 柄不应创新高(突破右杯沿)
            continue
        handle_depth = (RH - handle_lo) / RH
        if handle_depth > p["handle_depth_max"]:    # 柄回撤要浅
            continue
        pivot = RH * (1 + p["pivot_buffer"])        # 买点 = 右杯沿 + 缓冲
        cclose = float(C[cur])
        dist = (pivot - cclose) / pivot             # >0 在买点下方, <0 已突破
        if dist > p["near_pivot_max"] or dist < -p["above_pivot_max"]:
            continue                                # 离买点太远 / 已冲太高都不算"就绪"
        # 杯 = 右杯沿 R 之前 cup_len 周, 左杯沿 Lidx
        for cup_len in range(p["cup_min"], min(p["cup_max"], R) + 1):
            Lidx = R - cup_len
            if Lidx < 1:
                break
            LH = float(H[Lidx])
            if LH <= 0:
                continue
            rim = max(LH, RH)
            rim_diff = abs(RH - LH) / rim           # 左右杯沿要接近
            if rim_diff > p["rim_tol"]:
                continue
            seg = L[Lidx:R + 1]
            bottom = float(seg.min())
            bottom_pos = Lidx + int(np.argmin(seg))
            depth = (rim - bottom) / rim
            if not (p["cup_depth_min"] <= depth <= p["cup_depth_max"]):
                continue
            rel = (bottom_pos - Lidx) / cup_len      # U型: 底部居中, 非 V 型急跌
            if not (0.2 <= rel <= 0.8):
                continue
            mid = bottom + 0.5 * (rim - bottom)      # 柄应在杯的上半部
            if handle_lo < mid:
                continue
            pw = min(p["prior_bars"], Lidx)          # 前期涨势 >= prior_gain_min
            if pw < 4:
                continue
            prior_low = float(C[Lidx - pw:Lidx].min())
            if prior_low <= 0 or (C[Lidx] - prior_low) / prior_low < p["prior_gain_min"]:
                continue
            cup_vol = float(V[Lidx:R + 1].mean())
            handle_vol = float(V[R + 1:].mean())
            dryup = (cup_vol - handle_vol) / cup_vol if cup_vol > 0 else 0.0
            readiness = 1.0 if dist < 0 else max(0.0, 1 - dist / p["near_pivot_max"])
            shape = 1 - rim_diff / p["rim_tol"]
            score = 100 * (0.55 * readiness + 0.25 * max(0.0, dryup) + 0.20 * shape)
            if best is None or score > best["_score"]:
                best = {
                    "_score": score,
                    "score": round(score, 1),
                    "pivot": round(pivot, 2),
                    "close": round(cclose, 2),
                    "dist_pct": round(dist * 100, 2),
                    "cup_depth_pct": round(depth * 100, 1),
                    "cup_weeks": cup_len,
                    "handle_weeks": hl,
                    "handle_depth_pct": round(handle_depth * 100, 1),
                    "vol_dryup_pct": round(dryup * 100, 1),
                    "left_rim": dates[Lidx],
                    "bottom": dates[bottom_pos],
                    "right_rim": dates[R],
                    "ret": (cclose / float(C[cur - p["rs_lookback"]]) - 1) * 100
                           if cur >= p["rs_lookback"] else None,
                }
    if best:
        best.pop("_score", None)
    return best


@app.route("/api/pattern")
def pattern_query():
    """扫全市场, 找当前正在成形杯柄、价已逼近买点的股票, 按就绪度排序."""
    tf = (request.args.get("tf") or "w").strip().lower()  # 'w' 周线 / 'd' 日线
    if tf not in {"w", "d"}:
        return jsonify({"error": "tf must be w or d", "hits": []}), 400
    # 与周期相关的默认值 (杯/柄长度单位 = 该周期的 bar 数; 前期涨势/RS 回溯也按周期换算)
    if tf == "d":
        tf_def = {"cup_min": "35", "cup_max": "325", "handle_max": "25",
                  "prior_bars": 150, "rs_lookback": 130}
    else:
        tf_def = {"cup_min": "7", "cup_max": "65", "handle_max": "5",
                  "prior_bars": 30, "rs_lookback": 26}
    try:
        p = {
            "cup_min": int(request.args.get("cup_min", tf_def["cup_min"])),
            "cup_max": int(request.args.get("cup_max", tf_def["cup_max"])),
            "handle_max": int(request.args.get("handle_max", tf_def["handle_max"])),
            "cup_depth_min": float(request.args.get("cup_depth_min", "12")) / 100,
            "cup_depth_max": float(request.args.get("cup_depth_max", "50")) / 100,
            "handle_depth_max": float(request.args.get("handle_depth_max", "15")) / 100,
            "near_pivot_max": float(request.args.get("near_pivot_max", "8")) / 100,
            "above_pivot_max": float(request.args.get("above_pivot_max", "5")) / 100,
            "prior_gain_min": float(request.args.get("prior_gain_min", "30")) / 100,
            "rim_tol": 0.08,
            "pivot_buffer": 0.01,
            "prior_bars": tf_def["prior_bars"],
            "rs_lookback": tf_def["rs_lookback"],
        }
        min_amount_wan = float(request.args.get("min_amount", "5000"))
        limit = int(request.args.get("limit", "0"))  # 0 = 全部命中都返回
    except (TypeError, ValueError):
        return jsonify({"error": "筛选参数格式错误", "hits": []}), 400

    finite_values = [
        p["cup_depth_min"], p["cup_depth_max"], p["handle_depth_max"],
        p["near_pivot_max"], p["above_pivot_max"], p["prior_gain_min"],
        min_amount_wan,
    ]
    valid_ranges = (
        3 <= p["cup_min"] <= p["cup_max"] <= 2000
        and 1 <= p["handle_max"] <= 500
        and 0 <= p["cup_depth_min"] <= p["cup_depth_max"] <= 0.95
        and 0 <= p["handle_depth_max"] <= 0.95
        and 0 <= p["near_pivot_max"] <= 1
        and 0 <= p["above_pivot_max"] <= 1
        and 0 <= p["prior_gain_min"] <= 10
        and 0 <= min_amount_wan <= 100_000_000
        and 0 <= limit <= 5000
    )
    if not all(np.isfinite(value) for value in finite_values) or not valid_ranges:
        return jsonify({"error": "筛选参数超出允许范围", "hits": []}), 400

    ex_st = (request.args.get("ex_st", "1") == "1")
    ex_new = (request.args.get("ex_new", "1") == "1")
    ex_board = (request.args.get("ex_board", "1") == "1")  # 北交所/科创板
    min_amount = min_amount_wan * 1e4  # 万元 -> 元

    try:
        with closing(_open_sqlite_readonly(STOCK_META_DB)) as conn:
            meta = pd.read_sql_query(
                "SELECT code, ts_code, name, industry, list_date "
                "FROM stock_meta WHERE list_status='L'",
                conn,
            )
    except FileNotFoundError:
        return jsonify({"error": "股票元数据尚未生成", "hits": []}), 503
    except Exception as exc:
        log.warning("pattern metadata unavailable: %s", exc)
        return jsonify({"error": "股票元数据不可用", "hits": []}), 503
    today = datetime.now()
    one_year_ago = (today - timedelta(days=365)).strftime("%Y-%m-%d")

    hits = []
    scanned = 0
    for r in meta.itertuples(index=False):
        code, name = r.code, (r.name or "")
        if ex_st and "ST" in name.upper():
            continue
        if ex_board and (code.startswith("bj") or code.startswith("sh688")):
            continue
        if ex_new and r.list_date and r.list_date > one_year_ago:
            continue
        w = _daily_ohlc(code) if tf == "d" else _weekly_ohlc(code)
        if w is None:
            continue
        # 流动性: 估算近期日均成交额 (成交额 = close*成交量(手)*100). 周线 sum/5 得日均.
        if tf == "d":
            avg_daily_amount = float((w["close"][-60:] * w["volume"][-60:]).mean()) * 100
        else:
            avg_daily_amount = float((w["close"][-12:] * w["volume"][-12:]).mean()) * 100 / 5.0
        if avg_daily_amount < min_amount:
            continue
        scanned += 1
        det = _detect_cup_handle(w, p)
        if det is None:
            continue
        det.update({"code": code, "ts_code": r.ts_code, "name": name,
                    "industry": r.industry or ""})
        hits.append(det)

    # RS: 近 ~26 周收益在命中股内的百分位 (1-99)
    rets = sorted(h["ret"] for h in hits if h.get("ret") is not None)
    for h in hits:
        v = h.get("ret")
        if v is None or not rets:
            h["rs"] = None
        else:
            rank = sum(1 for x in rets if x <= v) / len(rets)
            h["rs"] = int(round(1 + rank * 98))

    hits.sort(key=lambda x: x["score"], reverse=True)
    return jsonify({
        "hits": hits[:limit] if limit > 0 else hits,
        "total_matched": len(hits),
        "scanned": scanned,
        "tf": tf,
        "today": today.strftime("%Y-%m-%d"),
    })


# ============================================================
#  /api/predict  qlib 下一交易日买入清单
# ============================================================

_predict_job = {"running": False, "status": "", "started": None}


@app.route("/predict")
def predict_page():
    return render_template("predict.html")


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else None
    except Exception:
        return None


def _status_with_request(
    status_path: Path,
    request_path: Path,
    stale_after_seconds: int = 300,
) -> tuple[dict, bool]:
    """Reconcile a persisted running status with its queue request marker."""
    status = _read_json(status_path)
    status = dict(status) if isinstance(status, dict) else {"state": "", "msg": ""}
    pending = request_path.exists()
    if status.get("state") != "running" or pending:
        return status, pending
    updated = None
    try:
        updated = datetime.strptime(str(status.get("updated_at") or ""), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            updated = datetime.fromtimestamp(status_path.stat().st_mtime)
        except OSError:
            pass
    if updated is None or (datetime.now() - updated).total_seconds() >= stale_after_seconds:
        previous = str(status.get("msg") or "").strip()
        status.update({
            "state": "error",
            "msg": "上次任务已中断，状态已自动复位" + (f"（停在：{previous}）" if previous else ""),
            "stale": True,
        })
    return status, pending


def _request_cache_namespace(name: str):
    """Return a request-local cache without retaining data across refreshes."""
    if not has_request_context():
        return None
    caches = getattr(g, "_qi_request_caches", None)
    if caches is None:
        caches = {}
        g._qi_request_caches = caches
    return caches.setdefault(str(name), {})


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        try:
            return float(value) if np.isfinite(float(value)) else None
        except Exception:
            return None
    if pd.isna(value) if not isinstance(value, (str, bytes, bytearray)) else False:
        return None
    return value


_MONEY_OUTFLOW_CACHE = {"mtime": None, "data": {}, "payload": None}


def _code6(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:6] if len(digits) >= 6 else ""


def _money_outflow_payload():
    primary = MONEY_OUTFLOW_JSON
    fallback = Path(__file__).resolve().parent / "data" / "money_outflow_signal.json"
    path = primary if primary.exists() else fallback
    data = _json_safe(_read_json(path) or {})
    rows = data.get("latest_stock_outflow") if isinstance(data, dict) else None
    if isinstance(rows, list) and rows:
        try:
            meta = _meta_for_codes([r.get("code") for r in rows if isinstance(r, dict)])
            for row in rows:
                if isinstance(row, dict):
                    m = meta.get(str(row.get("code") or ""))
                    if m and not row.get("name"):
                        row["name"] = m.get("name") or ""
        except Exception:
            pass
    return data


def _money_outflow_latest_map():
    request_cache = _request_cache_namespace("money_outflow")
    if request_cache is not None and "latest_map" in request_cache:
        return request_cache["latest_map"]
    primary = MONEY_OUTFLOW_JSON
    fallback = Path(__file__).resolve().parent / "data" / "money_outflow_signal.json"
    path = primary if primary.exists() else fallback
    try:
        mtime = path.stat().st_mtime if path.exists() else None
    except Exception:
        mtime = None
    if _MONEY_OUTFLOW_CACHE.get("mtime") == mtime:
        result = _MONEY_OUTFLOW_CACHE.get("data") or {}
        if request_cache is not None:
            request_cache["latest_map"] = result
        return result
    payload = _read_json(path) or {}
    rows = payload.get("latest_stock_flow_all") or payload.get("latest_stock_outflow") or []
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _code6(row.get("code") or row.get("ts_code"))
        if key:
            out[key] = row
    _MONEY_OUTFLOW_CACHE.update({"mtime": mtime, "data": out, "payload": payload})
    if request_cache is not None:
        request_cache["latest_map"] = out
    return out


def _money_outflow_for_code(code):
    key = _code6(code)
    if not key:
        return {"status": "none", "label": "无资金流数据"}
    row = _money_outflow_latest_map().get(key)
    if not row:
        return {"status": "none", "label": "无资金流数据"}
    rank = row.get("outflow_rank_pct")
    status = "high" if isinstance(rank, (int, float)) and rank >= 90 else ("watch" if isinstance(rank, (int, float)) and rank >= 80 else "normal")
    label = "资金流出前10%" if status == "high" else ("资金流出前20%" if status == "watch" else "资金流正常")
    return {
        "status": status,
        "label": label,
        "trade_date": row.get("trade_date"),
        "main_net_yi": row.get("main_net_yi"),
        "outflow_net_yi": row.get("outflow_net_yi"),
        "main_net_15d_yi": row.get("main_net_15d_yi") if row.get("main_net_15d_yi") is not None else row.get("main_net_10d_yi"),
        "outflow_15d_yi": row.get("outflow_15d_yi") if row.get("outflow_15d_yi") is not None else row.get("outflow_10d_yi"),
        "outflow_15d_daily": row.get("outflow_15d_daily") or row.get("outflow_10d_daily") or [],
        "outflow_recent_5d": row.get("outflow_recent_5d") or list(reversed((row.get("outflow_15d_daily") or row.get("outflow_10d_daily") or [])[-5:])),
        "main_net_15d_ratio_pct": row.get("main_net_15d_ratio_pct") if row.get("main_net_15d_ratio_pct") is not None else row.get("main_net_10d_ratio_pct"),
        "n_flow_days_15d": row.get("n_flow_days_15d") if row.get("n_flow_days_15d") is not None else row.get("n_flow_days_10d"),
        "main_net_10d_yi": row.get("main_net_10d_yi"),
        "outflow_10d_yi": row.get("outflow_10d_yi"),
        "outflow_10d_daily": row.get("outflow_10d_daily") or [],
        "main_net_10d_ratio_pct": row.get("main_net_10d_ratio_pct"),
        "n_flow_days_10d": row.get("n_flow_days_10d"),
        "main_net_ratio_pct": row.get("main_net_ratio_pct"),
        "outflow_rank_pct": row.get("outflow_rank_pct"),
    }


@app.route("/api/predict")
def api_predict():
    extra = {
        "compute_here": PREDICT_COMPUTE_HERE,
        "pc_pending": PREDICT_REQUEST.exists(),     # PC 是否有请求在排队/处理
        "pc_status": _read_json(PREDICT_STATUS),    # PC 监听脚本回写的状态
        "job": _predict_job,
    }
    if not PREDICT_JSON.exists():
        msg = "尚无预测结果, 点下方按钮让 PC 跑一次" if not PREDICT_COMPUTE_HERE \
              else "尚无预测结果, 点「更新数据并预测」生成"
        return jsonify({"hits": [], "message": msg, **extra})
    data = _read_json(PREDICT_JSON)
    if data is None:
        return jsonify({"hits": [], "message": "读取预测失败", **extra})
    _attach_unlock_info_to_payload(data)
    data.update(extra)
    return jsonify(data)


@app.route("/api/predict/request", methods=["POST"])
def api_predict_request():
    """网页按钮触发预测. 本机计算模式直接本地跑; 否则写请求文件交 PC 监听脚本执行."""
    retrain = (request.args.get("retrain", "0") == "1")
    update = (request.args.get("update", "0") == "1")   # 先拉 tushare 最新数据再预测
    if PREDICT_COMPUTE_HERE:
        return api_predict_run()
    if PREDICT_REQUEST.exists():
        return jsonify({"ok": False, "message": "已有预测请求在排队/处理中"})
    try:
        PREDICT_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        PREDICT_REQUEST.write_text(json.dumps(
            {"requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "retrain": retrain, "update": update},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"})
    tag = ("(更新数据+" if update else "(") + ("重训+" if retrain else "") + "预测)"
    return jsonify({"ok": True, "message": "已通知 PC: " + tag})


def _run_predict_job(retrain: bool):
    global _predict_job
    _predict_job = {"running": True,
                    "status": ("更新数据 + 重训模型 + 预测中..." if retrain else "更新数据 + 预测中..."),
                    "started": datetime.now().strftime("%H:%M:%S")}
    try:
        from scripts.predict_qlib import update_and_predict
        update_and_predict(retrain=retrain)
        _predict_job = {"running": False, "status": "完成",
                        "started": _predict_job["started"]}
    except Exception as e:
        log.exception("predict job failed")
        _predict_job = {"running": False, "status": f"失败: {e}",
                        "started": _predict_job.get("started")}


@app.route("/api/predict/run", methods=["POST"])
def api_predict_run():
    if not PREDICT_COMPUTE_HERE:
        return jsonify({"ok": False,
                        "message": "本实例不在本机计算; 预测由 PC 端生成, 此页只展示结果"})
    if _predict_job.get("running"):
        return jsonify({"ok": False, "message": "已有预测任务在运行中"})
    retrain = (request.args.get("retrain", "0") == "1")
    threading.Thread(target=_run_predict_job, args=(retrain,), daemon=True).start()
    return jsonify({"ok": True, "message": "已启动: 更新数据并预测" + ("(含重训)" if retrain else "")})


# ============================================================
#  /api/rdagent  RD-Agent 有效因子 + 买入清单 (展示 PC 产出)
# ============================================================

@app.route("/rdagent")
def rdagent_page():
    return render_template("rdagent.html")


@app.route("/tradingagents")
def tradingagents_page():
    return render_template("tradingagents.html")


@app.route("/api/tradingagents/results")
def api_ta_results():
    """TradingAgents 各股分析报告."""
    data = _read_json(TA_RESULTS) or {"results": []}
    data["status"] = _read_json(TA_STATUS) or {}
    data["pending"] = TA_REQUEST.exists()
    return jsonify(data)


@app.route("/api/tradingagents/analyze", methods=["POST"])
def api_ta_analyze():
    """网页按钮触发: 对选中股票跑 TradingAgents 多智能体分析. tickers=逗号分隔代码; date 可选."""
    if TA_REQUEST.exists():
        return jsonify({"ok": False, "message": "已有 TradingAgents 分析在排队/处理中"})
    raw = (request.args.get("tickers", "") or "").strip()
    requested_tickers = [t.strip() for t in raw.split(",") if t.strip()]
    if len(requested_tickers) > 10:
        return jsonify({"ok": False, "message": "最多选择 10 只股票"}), 400
    normalized_tickers = [_normalize_market_code(ticker) for ticker in requested_tickers]
    if any(ticker is None for ticker in normalized_tickers):
        return jsonify({"ok": False, "message": "股票代码格式错误"}), 400
    tickers = list(dict.fromkeys(normalized_tickers))
    if not tickers:
        return jsonify({"ok": False, "message": "没有选中股票"}), 400
    analysis_date = (request.args.get("date", "") or "").strip()
    if analysis_date:
        try:
            date.fromisoformat(analysis_date)
        except ValueError:
            return jsonify({"ok": False, "message": "分析日期格式错误"}), 400
    try:
        TA_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        TA_REQUEST.write_text(json.dumps(
            {"requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "tickers": tickers, "date": analysis_date},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"})
    return jsonify({"ok": True, "message": f"已通知 PC: 分析 {len(tickers)} 只 (多智能体辩论, 每只约几分钟)"})


@app.route("/api/rdagent")
def api_rdagent():
    pending, status, queued_request = _rdagent_queue_state()
    extra = {
        "rd_pending": pending,
        "rd_status": status,
        "rd_request_id": (queued_request or {}).get("request_id", ""),
    }
    data = _read_json(RDAGENT_JSON)
    if data is None:
        return jsonify({"hits": [], "factors": [],
                        "message": "尚无 RD-Agent 结果; 点按钮让 PC 跑一次", **extra})
    _attach_unlock_info_to_payload(data)
    data.update(extra)
    return jsonify(data)


def _queue_rdagent_request(payload):
    """Publish one complete watcher request without overwriting another job."""
    queued = dict(payload)
    queued.setdefault("requested_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    queued.setdefault("request_id", uuid.uuid4().hex)
    RDAGENT_REQUEST.parent.mkdir(parents=True, exist_ok=True)
    temporary = RDAGENT_REQUEST.with_name(
        f".{RDAGENT_REQUEST.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            json.dump(queued, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        # The hard link is an atomic create-if-absent operation. The watcher can
        # never observe the temporary, partially written JSON file.
        os.link(temporary, RDAGENT_REQUEST)
    except FileExistsError:
        return None
    finally:
        temporary.unlink(missing_ok=True)
    return queued


def _rdagent_queue_state():
    """Return pending/status while keeping an older job status out of a new queue."""
    request_exists = RDAGENT_REQUEST.exists()
    queued_request = _read_json(RDAGENT_REQUEST) if request_exists else None
    status = _read_json(RDAGENT_STATUS)
    if request_exists:
        request_id = (queued_request or {}).get("request_id", "")
        status_request_id = (status or {}).get("request_id", "")
        requested_at = (queued_request or {}).get("requested_at", "")
        status_is_older = not (status or {}).get("updated_at") or (
            requested_at and status.get("updated_at", "") < requested_at
        )
        status_is_other_request = bool(
            request_id and status_request_id != request_id
        )
        if (not status or status.get("state") != "running"
                or status_is_older or status_is_other_request):
            status = {
                "state": "queued",
                "msg": "已提交，等待 PC 开始",
                "request_id": request_id,
                "requested_at": requested_at,
            }
    elif status and status.get("state") == "running":
        status = dict(status)
        status.update({
            "state": "error",
            "msg": "PC 处理中状态已失去对应请求；请重新提交",
        })
    return request_exists, status, queued_request


@app.route("/mining")
def mining_page():
    return render_template("mining.html")


@app.route("/api/mine_history")
def api_mine_history():
    """挖矿进展: 各批次有效/全部因子数随时间 + 停滞轮数 + 每轮新增因子."""
    return jsonify(_read_json(MINE_HISTORY) or {"history": [], "n_batches": 0, "stall_rounds": 0, "latest": None})


@app.route("/fund-features")
def fund_features_page():
    template = "fund_features.html" if _has_internal_access() else "member_fund_features.html"
    return render_template(template)


@app.route("/api/fund_features")
def api_fund_features():
    """基本面/行业/事件特征的覆盖率/范围/分布 (灌入qlib给挖矿用的新维度)."""
    data = _read_json(FUND_FEATURES_META) or {"features": [], "n_codes": 0, "n_days": 0}
    if not _has_internal_access():
        allowed = {
            "gm", "dedt_yoy", "roe", "debt", "val_pe_inv", "val_pb_inv",
            "val_ps_inv", "ocf", "or_yoy", "netprofit_yoy", "netprofit_margin",
            "assets_turn", "roe_dt", "ocf_np", "dv_ttm",
        }
        public_fields = {"name", "coverage", "min", "max", "mean", "median", "kind"}
        data = {
            "updated": data.get("updated", ""),
            "n_codes": data.get("n_codes") or data.get("n_dumped") or 0,
            "features": [
                {key: value for key, value in feature.items() if key in public_fields}
                for feature in (data.get("features") or [])
                if feature.get("name") in allowed
            ],
        }
    return jsonify(data)


@app.route("/api/rdagent/batches")
def api_rdagent_batches():
    """列出可选的有效因子批次 (PC 端 export_rdagent.py 写出的索引). 供网页下拉选择."""
    data = _read_json(RDAGENT_BATCHES)
    if not data:
        return jsonify({"batches": [], "default_factors": [], "default_n": 0})
    return jsonify(data)


@app.route("/api/rdagent/request", methods=["POST"])
def api_rdagent_request():
    """网页按钮触发: 更新数据 + 预测. PC 监听脚本执行.
    retrain=1: 全量重训模型; retrain=0 (默认): 复用缓存模型只预测 (快).
    batch=<标签>: 用指定批次的有效因子 (空=默认 effective_factors.json)."""
    retrain = (request.args.get("retrain", "0").strip().lower() not in ("0", "false", "no", ""))
    batch = (request.args.get("batch", "") or "").strip()
    model = (request.args.get("model", "") or "").strip().lower()
    if not _valid_job_label(batch):
        return jsonify({"ok": False, "message": "批次标签包含不允许的字符"}), 400
    if model not in _allowed_models:
        return jsonify({"ok": False, "message": "不支持的模型"}), 400
    try:
        queued = _queue_rdagent_request({"retrain": retrain, "batch": batch, "model": model})
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"}), 503
    if queued is None:
        return jsonify({"ok": False, "message": "已有 RD-Agent 任务在排队/处理中"}), 409
    bmsg = f" [批次 {batch}]" if batch else ""
    if retrain:
        return jsonify({"ok": True, "message": f"已通知 PC: 更新数据 + 全量重训 + 预测{bmsg}",
                        "request_id": queued["request_id"]})
    return jsonify({"ok": True, "message": f"已通知 PC: 更新数据 + 复用模型预测{bmsg} (首次该批次会自动重训一次)",
                    "request_id": queued["request_id"]})


@app.route("/api/rdagent/mine", methods=["POST"])
def api_rdagent_mine():
    """网页按钮触发: RD-Agent 因子挖掘 (fin_factor 演化循环, 几小时, 烧 LLM).
    产出一个新的因子批次, 不改变默认 SOTA. loop_n=挖掘轮数 (默认 5). 需 PC 上 Docker 已启动."""
    try:
        loop_n = int(request.args.get("loop_n", "5"))
    except ValueError:
        loop_n = 5
    loop_n = max(1, min(loop_n, 50))
    fund = request.args.get("fund", "0") in ("1", "true", "True")  # 基本面增强路(prompt含基本面/情绪维度)
    universe = (request.args.get("universe") or "csi300").strip()   # 分池挖矿: 在该池上挖(挖前切conf,挖后恢复csi300)
    if universe not in ("csi300", "csi500", "csi1000"):
        universe = "csi300"
    track = "基本面增强路" if fund else "OHLCV老路"
    try:
        queued = _queue_rdagent_request(
            {"mine": True, "fund": fund, "loop_n": loop_n, "universe": universe}
        )
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"}), 503
    if queued is None:
        return jsonify({"ok": False, "message": "已有 RD-Agent 任务在排队/处理中"}), 409
    upart = "" if universe == "csi300" else f" 池={universe}"
    return jsonify({"ok": True, "message": f"已通知 PC: RD-Agent 因子挖掘 [{track}]{upart} loop_n={loop_n} (很慢, 约几小时; 需 PC 上 Docker 已启动)",
                    "request_id": queued["request_id"]})


MINE_ALL_HISTORY = PREDICT_JSON.parent / "mine_all_history.json"   # 统一挖矿历史(全路线×池)


def _mine_pool_page(route, universe, route_name, univ_name, fund):
    return render_template("mine_pool.html", route=route, universe=universe,
                           route_name=route_name, univ_name=univ_name, fund=fund)


@app.route("/mine-ohlcv-csi1000")
def mine_ohlcv_csi1000():
    """量价·中证1000: 复用真路B模板(挖矿 + 批次因子预测次日买入清单), 股票池/挖矿池均固定csi1000."""
    return render_template("predict_batch.html", pool="csi1000", pool_name="中证1000")


@app.route("/mine-fund-csi500")
def mine_fund_csi500():
    return _mine_pool_page("fund", "csi500", "基本面", "中证500", 1)


@app.route("/mine-fund-csi1000")
def mine_fund_csi1000():
    return _mine_pool_page("fund", "csi1000", "基本面", "中证1000", 1)


@app.route("/api/mine_pool")
def api_mine_pool():
    """某 路线×股票池 组合的挖矿状态 + 该组合的历史留痕(过滤)."""
    route = (request.args.get("route") or "ohlcv").strip()
    universe = (request.args.get("universe") or "csi300").strip()
    runs = (_read_json(MINE_ALL_HISTORY) or {}).get("runs") or []
    runs = [r for r in runs if r.get("route") == route and r.get("universe") == universe]
    return jsonify({"route": route, "universe": universe, "history": runs,
                    "mine_status": _read_json(RDAGENT_STATUS) or {"state": "", "msg": ""},
                    "mine_pending": RDAGENT_REQUEST.exists()})


@app.route("/fund-mining")
def fund_mining_page():
    """基本面增强挖掘 专属页(与/rdagent的OHLCV挖掘/模型实验室分开, 防误操作)."""
    return render_template("fund_mining.html")


@app.route("/api/fund_mining")
def api_fund_mining():
    """基本面挖矿: 状态 + 🧬批次 + 28特征正交增量榜 + 挖到的因子resid增量榜."""
    batches = _read_json(RDAGENT_BATCHES) or {}
    fund_batches = [b for b in (batches.get("batches") or []) if b.get("fund")]
    return jsonify({
        "status": _read_json(RDAGENT_STATUS) or {"state": "", "msg": ""},
        "pending": RDAGENT_REQUEST.exists(),
        "fund_batches": fund_batches,
        "feat_validate": _read_json(FUND_FEAT_VALIDATE) or {},     # 28特征对base正交增量榜
        "resid_screen": _read_json(FUND_RESID_SCREEN) or {},       # 挖到的因子resid增量榜
        "history": (_read_json(FUND_MINE_HISTORY) or {}).get("runs") or [],  # 每次挖掘留痕
    })


def _fund_batch_labels():
    """所有 🧬 基本面批次的标签(供对比页下拉选)."""
    batches = _read_json(RDAGENT_BATCHES) or {}
    return [b.get("label") for b in (batches.get("batches") or []) if b.get("fund") and b.get("label")]


def _name_hits(hits):
    """给买入清单 hits 注入简称(reuse _meta_for_codes)."""
    hits = hits or []
    meta = _meta_for_codes([h.get("code") for h in hits if h.get("code")])
    for h in hits:
        h["name"] = (meta.get(h.get("code")) or {}).get("name", "")
    _attach_unlock_info(hits)
    return hits


@app.route("/fund-predict-compare")
def fund_predict_compare_page():
    """🧬基本面批次 vs 基线 次日买入清单对比 + 留痕 专页."""
    return render_template("fund_predict_compare.html")


@app.route("/api/fund_compare")
def api_fund_compare():
    """最近一次对比(注入简称) + 留痕历史摘要 + 可选🧬批次 + 状态."""
    latest = _read_json(FUND_COMPARE_LATEST) or {}
    if latest:
        latest["baseline_hits"] = _name_hits(latest.get("baseline_hits"))
        latest["fund_hits"] = _name_hits(latest.get("fund_hits"))
    hist = (_read_json(FUND_COMPARE_HISTORY) or {}).get("runs") or []
    # 历史只回摘要(不传全部清单, 减载荷); 点开看详情走 /api/fund_compare/run_detail
    hist_brief = [{"as_of": r.get("as_of"), "batch": r.get("batch"), "baseline_batch": r.get("baseline_batch"),
                   "model": r.get("model"), "universe": r.get("universe") or "csi300", "generated_at": r.get("generated_at"),
                   "n_fund": len(r.get("fund_hits") or []), "n_base": len(r.get("baseline_hits") or [])}
                  for r in hist]
    return jsonify({"latest": latest, "history": hist_brief,
                    "fund_batches": _fund_batch_labels(),
                    "status": _read_json(FUND_COMPARE_STATUS) or {"state": "", "msg": ""},
                    "pending": FUND_COMPARE_REQUEST.exists()})


@app.route("/api/fund_compare/detail")
def api_fund_compare_detail():
    """留痕里某一次对比的完整清单(按 generated_at 定位), 供历史回看展开."""
    gat = (request.args.get("at") or "").strip()
    runs = (_read_json(FUND_COMPARE_HISTORY) or {}).get("runs") or []
    for r in runs:
        if r.get("generated_at") == gat:
            r = dict(r)
            r["baseline_hits"] = _name_hits(r.get("baseline_hits"))
            r["fund_hits"] = _name_hits(r.get("fund_hits"))
            return jsonify(r)
    return jsonify({"error": "未找到该次对比"}), 404


@app.route("/api/fund_compare/run", methods=["POST"])
def api_fund_compare_run():
    """触发一次对比: 同模型同池(csi300/24因子), 基线批次 vs 🧬fund批次, 只因子集不同."""
    if FUND_COMPARE_REQUEST.exists():
        return jsonify({"ok": False, "message": "已有对比任务在处理中"})
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "JSON 对象格式错误"}), 400
    batch = (body.get("batch") or "").strip()
    baseline = (body.get("baseline") or "").strip()   # 空=当前默认SOTA(原24因子)
    model = (body.get("model") or "lgb").strip().lower()
    universe = (body.get("universe") or "csi300").strip() or "csi300"
    if universe not in ("csi300", "csi500", "csi1000"):
        return jsonify({"ok": False, "message": "股票池参数错误"}), 400
    if not batch:
        return jsonify({"ok": False, "message": "请选要对比的 🧬 基本面批次"}), 400
    if not _valid_job_label(batch) or not _valid_job_label(baseline):
        return jsonify({"ok": False, "message": "批次标签包含非法字符"}), 400
    if not model or model not in _allowed_models:
        return jsonify({"ok": False, "message": "模型参数错误"}), 400
    try:
        FUND_COMPARE_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        FUND_COMPARE_REQUEST.write_text(json.dumps(
            {"batch": batch, "baseline": baseline, "model": model, "universe": universe,
             "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"})
    bl = baseline or "默认SOTA(原24因子)"
    uname = {"csi300": "沪深300", "csi500": "中证500", "csi1000": "中证1000"}.get(universe, universe)
    return jsonify({"ok": True, "message": f"已通知 PC: 对比 🧬{batch} vs {bl} @{uname} (model={model}, 重训fund侧约几分~十几分, 需PC在跑)"})


@app.route("/api/rdagent/model_results")
def api_rdagent_model_results():
    """模型实验室: 读取各模型在各批次上的回测结果 (PC 端 run_model.py 写)."""
    data = _read_json(RDAGENT_MODEL_RESULTS)
    if not data:
        return jsonify({"results": []})
    return jsonify(data)


# ===== Alpha158 预测页 (与 24因子页并存, 做预测对比): 用全 Alpha158(158因子)+csi300 出次日买入清单 =====
@app.route("/predict-a158")
def predict_a158_page():
    return render_template("predict_a158.html")


@app.route("/api/predict_a158")
def api_predict_a158():
    """返回 Alpha158 预测结果(买入清单, 名字补全) + 进度 + 是否在跑."""
    res = _read_json(PREDICT_A158_RESULT)
    if res and isinstance(res.get("hits"), list):
        codes = [h.get("code") for h in res["hits"] if h.get("code")]
        meta = _meta_for_codes(codes)
        for h in res["hits"]:
            m = meta.get(h.get("code")) or {}
            h["name"] = m.get("name", "")
            h["industry"] = m.get("industry", "")
        _attach_unlock_info(res["hits"])
    return jsonify({"result": res,
                    "status": _read_json(PREDICT_A158_STATUS) or {"state": "", "msg": ""},
                    "pending": PREDICT_A158_REQUEST.exists()})


@app.route("/api/predict_a158/request", methods=["POST"])
def api_predict_a158_request():
    """网页按钮: 通知PC用全Alpha158跑次日买入清单(可选模型). PC predict_next_day.py RDAGENT_ALPHA158=1."""
    if PREDICT_A158_REQUEST.exists():
        return jsonify({"ok": False, "message": "已有 Alpha158 预测任务在处理中"})
    model = ((request.get_json(silent=True) or {}).get("model") or "xgb").strip().lower()
    if model not in {"lgb", "xgb", "catboost", "ols", "ridge", "lasso", "dlinear", "patchtst", "timesnet", "itransformer"}:
        return jsonify({"ok": False, "message": f"不支持的模型: {model}"})
    try:
        PREDICT_A158_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        PREDICT_A158_REQUEST.write_text(json.dumps(
            {"model": model, "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"})
    slow = "(深度模型训练较慢, 约5-10分钟)" if model in ("dlinear", "patchtst", "timesnet", "itransformer") else "(约3-6分钟)"
    return jsonify({"ok": True, "message": f"已通知 PC: 全 Alpha158 + csi300 跑 {model} 次日买入清单 {slow}"})


@app.route("/api/predict_a158/runall", methods=["POST"])
def api_predict_a158_runall():
    """一键全跑: 在全 Alpha158 上顺序跑所有模型, 每个出买入清单(score文件), 供并排对比. PC 串行."""
    if PREDICT_A158_REQUEST.exists():
        return jsonify({"ok": False, "message": "已有 Alpha158 任务在处理中"})
    try:
        PREDICT_A158_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        PREDICT_A158_REQUEST.write_text(json.dumps(
            {"model": "all", "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"})
    return jsonify({"ok": True, "message": "已通知 PC: 全 Alpha158 顺序跑所有模型(含深度, 较慢约1小时), 完成一个显示一个"})


@app.route("/predict-csi1000")
def predict_csi1000_page():
    return render_template("pool_predict.html", universe="csi1000", uname="中证1000")


@app.route("/predict-csi500")
def predict_csi500_page():
    return render_template("pool_predict.html", universe="csi500", uname="中证500")


@app.route("/api/pool_predict")
def api_pool_predict():
    """某股票池所有模型的次日买入清单, 按 universe_arena 的 IR 降序排. u=csi1000/csi500."""
    u = (request.args.get("u") or "csi1000").strip()
    if u not in ("csi1000", "csi500"):
        u = "csi1000"
    arena = _read_json(PREDICT_JSON.parent / "universe_arena.json") or []
    ir_map = {a["model"]: a.get("ir") for a in arena if a.get("universe") == u and a.get("model")}
    models = []
    for p in sorted(PREDICT_JSON.parent.glob(f"pool_buy_{u}_*.json")):
        d = _read_json(p)
        if not d or not isinstance(d.get("hits"), list):
            continue
        m = d.get("model") or p.stem.replace(f"pool_buy_{u}_", "")
        codes = [h.get("code") for h in d["hits"] if h.get("code")]
        meta = _meta_for_codes(codes)
        for h in d["hits"]:
            h["name"] = (meta.get(h.get("code")) or {}).get("name", "")
        _attach_unlock_info(d["hits"])
        models.append({"model": m, "ir": ir_map.get(m), "as_of": d.get("as_of"),
                       "n_universe": d.get("n_universe"), "hits": d["hits"]})
    models.sort(key=lambda x: (x["ir"] if x["ir"] is not None else -99), reverse=True)
    return jsonify({"universe": u, "models": models,
                    "status": _read_json(POOL_PREDICT_STATUS) or {"state": "", "msg": ""},
                    "train_progress": _read_json(PREDICT_JSON.parent / "train_progress.json") or {},
                    "pending": POOL_PREDICT_REQUEST.exists()})


POOL_MODELS = ["lgb", "xgb", "catboost", "ols", "ridge", "lasso", "dlinear", "timesnet", "patchtst", "itransformer"]


@app.route("/api/pool_predict/runall", methods=["POST"])
def api_pool_predict_runall():
    """跑某池买入清单. model 空/all=所有模型按 IR 降序; 指定单模型=只跑它(省时间). PC 串行 predict_next_day RDAGENT_UNIVERSE=."""
    if POOL_PREDICT_REQUEST.exists():
        return jsonify({"ok": False, "message": "已有分池预测任务在处理中"})
    body = request.get_json(silent=True) or {}
    u = (body.get("universe") or "csi1000").strip()
    if u not in ("csi1000", "csi500"):
        u = "csi1000"
    model = (body.get("model") or "").strip().lower()
    if model and model != "all" and model not in POOL_MODELS:
        return jsonify({"ok": False, "message": f"未知模型 {model}"})
    try:
        POOL_PREDICT_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        POOL_PREDICT_REQUEST.write_text(json.dumps(
            {"universe": u, "model": model, "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"})
    if model and model != "all":
        return jsonify({"ok": True, "message": f"已通知 PC: {u} 只跑 {model} 一个模型出次日清单(约几分~十几分, 比全跑快很多)"})
    return jsonify({"ok": True, "message": f"已通知 PC: {u} 所有模型按 IR 降序跑次日买入清单(含深度模型, 较慢约1小时)"})


@app.route("/predict-batch")
def predict_batch_page():
    """真路B(量价·中证500): 用某历史OHLCV批次因子, 全模型预测次日买入清单 + 与Alpha158对比."""
    return render_template("predict_batch.html", pool="csi500", pool_name="中证500")


@app.route("/timesnet-select")
def timesnet_select_page():
    """🧠 TimesNet 量价批次选股: 用某量价批次精选因子, 只跑最强模型 TimesNet 出次日买入清单(ptnn已真用精选因子)."""
    return render_template("timesnet_select.html")


@app.route("/api/batch_predict")
def api_batch_predict():
    """某OHLCV批次在指定股票池(真路B)全模型的次日买入清单 + 每只票的Alpha158平均排名分位."""
    batch = (request.args.get("batch") or "").strip()
    if not _valid_job_label(batch):
        return jsonify({"error": "invalid batch label"}), 400
    u = (request.args.get("u") or "csi300").strip()
    if u not in ("csi300", "csi500", "csi1000"):
        u = "csi300"
    bdata = _read_json(RDAGENT_BATCHES) or {}
    ohlcv = [b for b in (bdata.get("batches") or []) if not b.get("fund") and b.get("label")]
    labels = [b.get("label") for b in ohlcv]
    if not batch and labels:
        batch = labels[0]
    models = []
    if batch:
        pre = f"batch_buy_{batch}_{u}_"
        for p in sorted(PREDICT_JSON.parent.glob(f"{pre}*.json")):
            d = _read_json(p)
            if not d or not isinstance(d.get("hits"), list):
                continue
            m = p.stem.replace(pre, "")
            codes = [h.get("code") for h in d["hits"] if h.get("code")]
            meta = _meta_for_codes(codes)
            for h in d["hits"]:
                h["name"] = (meta.get(h.get("code")) or {}).get("name", "")
            _attach_unlock_info(d["hits"])
            models.append({"model": m, "as_of": d.get("as_of"), "n_universe": d.get("n_universe"), "hits": d["hits"]})
    # 同池的全Alpha158买入清单(对比'批次挑的票 Alpha158 也挑吗'): 统计各Alpha158模型 top50 里出现次数
    a158_cnt = {}
    if u == "csi300":
        for p in PREDICT_JSON.parent.glob("a158_scores_*.json"):
            d = _read_json(p) or {}
            top = sorted((d.get("scores") or {}).items(), key=lambda kv: kv[1], reverse=True)[:50]
            for code, _ in top:
                a158_cnt[code] = a158_cnt.get(code, 0) + 1
    else:
        for p in PREDICT_JSON.parent.glob(f"pool_buy_{u}_*.json"):
            d = _read_json(p) or {}
            for h in (d.get("hits") or [])[:50]:
                c = h.get("code")
                if c:
                    a158_cnt[c] = a158_cnt.get(c, 0) + 1
    # 已运行历史: 扫所有 batch_buy_*.json, 按(batch,universe)分组(供"一点就看历史"+避免重复跑)
    hist = {}
    for p in PREDICT_JSON.parent.glob("batch_buy_*.json"):
        parts = p.stem.replace("batch_buy_", "").rsplit("_", 2)   # [batch, universe, model](batch可含_)
        if len(parts) != 3 or parts[1] not in ("csi300", "csi500", "csi1000"):
            continue
        b, uu, _m = parts
        key = f"{b}|{uu}"
        if key not in hist:
            hist[key] = {"batch": b, "universe": uu, "n_models": 0, "as_of": (_read_json(p) or {}).get("as_of")}
        hist[key]["n_models"] += 1
    history = sorted(hist.values(), key=lambda x: (x["batch"], x["universe"]), reverse=True)
    return jsonify({"batch": batch, "universe": u, "batches": labels, "models": models,
                    "a158_picks": a158_cnt, "history": history,
                    "status": _read_json(BATCH_PREDICT_STATUS) or {"state": "", "msg": ""},
                    "train_progress": _read_json(PREDICT_JSON.parent / "train_progress.json") or {},
                    "pending": BATCH_PREDICT_REQUEST.exists(),
                    "mine_status": _read_json(RDAGENT_STATUS) or {"state": "", "msg": ""},
                    "mine_pending": RDAGENT_REQUEST.exists()})


@app.route("/api/batch_predict/run", methods=["POST"])
def api_batch_predict_run():
    """触发: 用某OHLCV批次因子, 全模型各跑一遍 csi300 次日买入清单."""
    if BATCH_PREDICT_REQUEST.exists():
        return jsonify({"ok": False, "message": "已有批次预测任务在处理中"})
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "JSON 对象格式错误"}), 400
    batch = (body.get("batch") or "").strip()
    u = (body.get("universe") or "csi300").strip()
    if u not in ("csi300", "csi500", "csi1000"):
        return jsonify({"ok": False, "message": "股票池参数错误"}), 400
    model = (body.get("model") or "").strip().lower()   # 空/all=全模型; 指定=只跑该模型(如timesnet, 省时间)
    if not batch:
        return jsonify({"ok": False, "message": "请选要预测的批次"}), 400
    if not _valid_job_label(batch):
        return jsonify({"ok": False, "message": "批次标签包含非法字符"}), 400
    if model not in _allowed_models:
        return jsonify({"ok": False, "message": "模型参数错误"}), 400
    try:
        BATCH_PREDICT_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        BATCH_PREDICT_REQUEST.write_text(json.dumps(
            {"batch": batch, "universe": u, "model": model, "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"})
    if model and model != "all":
        return jsonify({"ok": True, "message": f"已通知 PC: 用批次 {batch} 只跑 {model} 出 {u} 次日清单(单模型, 较快)"})
    return jsonify({"ok": True, "message": f"已通知 PC: 用批次 {batch} 全模型跑 {u} 次日清单(含深度, 约1小时)"})


@app.route("/api/buylist_history")
def api_buylist_history():
    """次日买入清单留痕: 每次生成都按时间存(批次/池/模型/hits)。
    可选过滤 ?batch=&universe=&model= ; 注入简称。供网页按时间/选项比对分析。"""
    data = _read_json(BUYLIST_HISTORY) or {"runs": []}
    runs = data.get("runs") or []
    fb = (request.args.get("batch") or "").strip()
    fu = (request.args.get("universe") or "").strip()
    fm = (request.args.get("model") or "").strip().lower()
    if fb:
        runs = [r for r in runs if r.get("batch") == fb]
    if fu:
        runs = [r for r in runs if r.get("universe") == fu]
    if fm:
        runs = [r for r in runs if (r.get("model") or "").lower() == fm]
    runs = runs[:200]
    # 注入简称
    codes = {h.get("code") for r in runs for h in (r.get("hits") or [])}
    meta = _meta_for_codes(list(codes))
    for r in runs:
        for h in (r.get("hits") or []):
            h["name"] = (meta.get(h.get("code")) or {}).get("name", "")
        _attach_unlock_info(r.get("hits") or [])
    return jsonify({"runs": runs, "n": len(runs)})


@app.route("/api/buylist_matrix")
def api_buylist_matrix():
    """多批次对比矩阵: 某池+某模型下, 各批次最新清单并排(列=批次, 行=股票, 被几个批次选中=count)。
    供网页画矩阵: 多批次共识的票标绿。参数 universe(默认csi1000)/model(默认timesnet)/topn(默认20)。"""
    u = (request.args.get("universe") or "csi1000").strip()
    m = (request.args.get("model") or "timesnet").strip().lower()
    try:
        topn = max(5, min(80, int(request.args.get("topn") or 20)))
    except (TypeError, ValueError):
        topn = 20
    data = _read_json(BUYLIST_HISTORY) or {"runs": []}
    runs = [r for r in (data.get("runs") or [])
            if r.get("universe") == u and (r.get("model") or "").lower() == m]
    # 每个批次取最新一次(runs已按时间倒序, 先到的即最新)
    latest = {}
    for r in runs:
        b = r.get("batch") or "默认"
        if b not in latest:
            latest[b] = r
    batches = sorted(latest.keys(), reverse=True)
    # 行=股票(各批次top-N并集), 列=批次, 格=rank
    cell = {}      # code -> {batch: rank}
    names = {}
    for b in batches:
        for h in (latest[b].get("hits") or [])[:topn]:
            c = h.get("code")
            if not c:
                continue
            cell.setdefault(c, {})[b] = h.get("rank")
            if h.get("name"):
                names[c] = h["name"]
    # 补简称(留痕里没name的)
    miss = [c for c in cell if c not in names]
    if miss:
        meta = _meta_for_codes(miss)
        for c in miss:
            names[c] = (meta.get(c) or {}).get("name", "")
    stocks = [{"code": c, "name": names.get(c, ""), "ranks": cell[c], "count": len(cell[c])}
              for c in cell]
    stocks.sort(key=lambda s: (-s["count"], min(s["ranks"].values())))
    asof = {b: latest[b].get("as_of") for b in batches}
    gen = {b: latest[b].get("generated_at") for b in batches}
    return jsonify({"universe": u, "model": m, "topn": topn,
                    "batches": batches, "as_of": asof, "generated_at": gen, "stocks": stocks})


@app.route("/api/predict_a158/all")
def api_predict_a158_all():
    """读所有模型的 Alpha158 打分, 各取 top-N, 拼成对比矩阵(共识多的票在前, 多模型选中=高亮)."""
    try:
        topn = max(5, min(int(request.args.get("topn", 30)), 80))
    except Exception:
        topn = 30
    arena = {m.get("model"): m.get("ir") for m in (_read_json(ALPHA158_ARENA_RESULT) or [])}
    models = {}
    asof = None
    for p in PREDICT_JSON.parent.glob("a158_scores_*.json"):
        m = p.stem.replace("a158_scores_", "")
        d = _read_json(p) or {}
        sc = d.get("scores") or {}
        if not sc:
            continue
        asof = d.get("as_of") or asof
        topcodes = sorted(sc.items(), key=lambda kv: kv[1], reverse=True)[:topn]
        models[m] = {code: i + 1 for i, (code, _) in enumerate(topcodes)}  # code -> 该模型内排名
    if not models:
        return jsonify({"models": [], "rows": [], "as_of": None,
                        "pending": PREDICT_A158_REQUEST.exists(),
                        "status": _read_json(PREDICT_A158_STATUS) or {}})
    allcodes = set()
    for ranks in models.values():
        allcodes |= set(ranks.keys())
    meta = _meta_for_codes(list(allcodes))
    rows = []
    for c in allcodes:
        picks = {m: ranks[c] for m, ranks in models.items() if c in ranks}
        mm = meta.get(c) or {}
        rows.append({"code": c, "name": mm.get("name", ""), "industry": mm.get("industry", ""),
                     "picks": picks, "n_picks": len(picks)})
    rows.sort(key=lambda r: (-r["n_picks"], min(r["picks"].values())))
    mlist = sorted(models.keys(), key=lambda m: (arena.get(m) if arena.get(m) is not None else -9), reverse=True)
    return jsonify({"models": mlist, "model_ir": {m: arena.get(m) for m in mlist},
                    "topn": topn, "as_of": asof, "rows": rows,
                    "pending": PREDICT_A158_REQUEST.exists(),
                    "status": _read_json(PREDICT_A158_STATUS) or {}})


# ===== Alpha158 模型擂台 (158因子体系第2页): 各模型在全 Alpha158 上的 IR/超额/回撤排名 =====
@app.route("/alpha158-arena")
def alpha158_arena_page():
    return render_template("alpha158_arena.html")


@app.route("/api/alpha158_arena")
def api_alpha158_arena():
    arr = _read_json(ALPHA158_ARENA_RESULT) or []
    if not isinstance(arr, list):
        arr = []
    return jsonify({"models": arr,
                    "status": _read_json(ALPHA158_ARENA_STATUS) or {"state": "", "msg": ""},
                    "pending": ALPHA158_ARENA_REQUEST.exists()})


@app.route("/api/alpha158_arena/request", methods=["POST"])
def api_alpha158_arena_request():
    """网页按钮: 在全 Alpha158 上回测某模型, 入擂台榜. PC run_model.py RDAGENT_ALPHA158=1."""
    if ALPHA158_ARENA_REQUEST.exists():
        return jsonify({"ok": False, "message": "已有擂台任务在跑"})
    model = ((request.get_json(silent=True) or {}).get("model") or "catboost").strip().lower()
    if model not in {"lgb", "xgb", "catboost", "ols", "ridge", "lasso", "dlinear", "patchtst", "timesnet", "itransformer", "all"}:
        return jsonify({"ok": False, "message": f"不支持的模型: {model}"})
    try:
        ALPHA158_ARENA_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        ALPHA158_ARENA_REQUEST.write_text(json.dumps(
            {"model": model, "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"})
    if model == "all":
        return jsonify({"ok": True, "message": "已通知 PC: 一键全跑所有模型(含深度, 顺序约1小时), 完成一个更新一个"})
    slow = "(深度模型较慢, 约8-15分钟)" if model in ("dlinear", "patchtst", "timesnet", "itransformer") else "(约3-6分钟)"
    return jsonify({"ok": True, "message": f"已通知 PC: 全 Alpha158 回测 {model} {slow}"})


# ===== 股票池对比 (158因子体系第5页): 同模型在 csi300/500/1000/全市场 上谁更好 =====
@app.route("/universe-arena")
def universe_arena_page():
    return render_template("universe_arena.html")


@app.route("/api/universe_arena")
def api_universe_arena():
    data = _read_json(UNIVERSE_ARENA_RESULT)
    if not isinstance(data, list):
        data = []
    return jsonify({"entries": data,
                    "status": _read_json(UNIVERSE_ARENA_STATUS) or {"state": "", "msg": ""},
                    "pending": UNIVERSE_ARENA_REQUEST.exists()})


@app.route("/api/universe_arena/history")
def api_universe_arena_history():
    """股票池擂台历史留痕: 每次计算一条(带测试期+时间), 供跨时点/时段对比. 可按 u/m 过滤."""
    hist = _read_json(PREDICT_JSON.parent / "universe_arena_history.json")
    if not isinstance(hist, list):
        hist = []
    u = (request.args.get("u") or "").strip()
    m = (request.args.get("m") or "").strip()
    if u:
        hist = [h for h in hist if h.get("universe") == u]
    if m:
        hist = [h for h in hist if h.get("model") == m]
    hist = sorted(hist, key=lambda h: h.get("computed_at", ""), reverse=True)
    return jsonify({"history": hist[:1000]})


@app.route("/api/universe_arena/request", methods=["POST"])
def api_universe_arena_request():
    """在某股票池上回测某模型(或 all). PC run_model.py RDAGENT_ALPHA158=1 RDAGENT_UNIVERSE=<u>."""
    if UNIVERSE_ARENA_REQUEST.exists():
        return jsonify({"ok": False, "message": "已有股票池回测任务在跑"})
    body = request.get_json(silent=True) or {}
    universe = (body.get("universe") or "").strip().lower()
    model = (body.get("model") or "xgb").strip().lower()
    if universe not in {"csi300", "csi500", "csi1000", "all", "allunivs"}:
        return jsonify({"ok": False, "message": f"不支持的股票池: {universe}"})
    if model not in {"lgb", "xgb", "catboost", "ols", "ridge", "lasso", "dlinear", "patchtst", "timesnet", "itransformer", "all"}:
        return jsonify({"ok": False, "message": f"不支持的模型: {model}"})
    try:
        UNIVERSE_ARENA_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        UNIVERSE_ARENA_REQUEST.write_text(json.dumps(
            {"universe": universe, "model": model, "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"})
    return jsonify({"ok": True, "message": f"已通知 PC: {universe} 上回测 {model} (全市场较慢/可能很慢)"})


# ===== 批次股票池对比: 选某批次因子, 测所有模型×各股票池的 IR (仿 universe-arena, 但用批次因子非Alpha158) =====
@app.route("/batch-arena")
def batch_arena_page():
    return render_template("batch_arena.html")


@app.route("/api/batch_arena")
def api_batch_arena():
    """批次擂台: batch×universe×model 的IR矩阵。可选 ?batch= 过滤。返回可选批次列表(量价批次)。"""
    data = _read_json(BATCH_ARENA_RESULT)
    if not isinstance(data, list):
        data = []
    fb = (request.args.get("batch") or "").strip()
    # 可选批次 = 量价批次(与真路B同源)
    bdata = _read_json(RDAGENT_BATCHES) or {}
    labels = [b.get("label") for b in (bdata.get("batches") or []) if not b.get("fund") and b.get("label")]
    real0 = labels[0] if labels else "default"  # 默认仍选最新真实批次(数据较全), default(SOTA)只是可选项
    labels = ["default"] + labels  # "default"=SOTA(系统最优15因子, sota_workspace.txt); 置顶可选
    if not fb:
        fb = real0
    entries = [e for e in data if e.get("batch") == fb] if fb else []
    status, pending = _status_with_request(BATCH_ARENA_STATUS, BATCH_ARENA_REQUEST)
    return jsonify({"batch": fb, "batches": labels, "entries": entries,
                    "all_entries": data,  # 跨批次总览: 全部已跑结果一屏对比, 不切换
                    "status": status,
                    "train_progress": _read_json(PREDICT_JSON.parent / "train_progress.json") or {},
                    "pending": pending})


@app.route("/api/batch_arena/run", methods=["POST"])
def api_batch_arena_run():
    """触发: 用某批次因子, 在指定池(或全部池)回测某模型(或全模型). PC run_model RDAGENT_FACTOR_BATCH+RDAGENT_UNIVERSE."""
    if BATCH_ARENA_REQUEST.exists():
        return jsonify({"ok": False, "message": "已有批次擂台回测在跑"})
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "JSON 对象格式错误"}), 400
    batch = (body.get("batch") or "").strip()
    universe = (body.get("universe") or "csi300").strip().lower()
    model = (body.get("model") or "all").strip().lower()
    if not batch:
        return jsonify({"ok": False, "message": "请选批次"}), 400
    if not _valid_job_label(batch):
        return jsonify({"ok": False, "message": "批次标签包含非法字符"}), 400
    if universe not in {"csi300", "csi500", "csi1000", "all", "allunivs"}:
        return jsonify({"ok": False, "message": f"不支持的股票池: {universe}"}), 400
    if model not in {"lgb", "xgb", "catboost", "ols", "ridge", "lasso", "dlinear", "patchtst", "timesnet", "itransformer", "all"}:
        return jsonify({"ok": False, "message": f"不支持的模型: {model}"}), 400
    try:
        BATCH_ARENA_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        BATCH_ARENA_REQUEST.write_text(json.dumps(
            {"batch": batch, "universe": universe, "model": model,
             "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"})
    uw = "全部池(300/500/1000)" if universe == "allunivs" else universe
    mw = "全模型(10个)" if model == "all" else model
    return jsonify({"ok": True, "message": f"已通知 PC: 批次 {batch} 在 {uw} 回测 {mw}(含深度模型, 多组合很慢)"})


@app.route("/api/batch_arena/history")
def api_batch_arena_history():
    """批次擂台历史留痕: 每次回测都存一条(带测试期+时间), 不去重, 供跨时点对比. 可按 batch/u/m 过滤."""
    hist = _read_json(PREDICT_JSON.parent / "batch_arena_history.json")
    if not isinstance(hist, list):
        hist = []
    fb = (request.args.get("batch") or "").strip()
    u = (request.args.get("u") or "").strip()
    m = (request.args.get("m") or "").strip()
    if fb:
        hist = [h for h in hist if h.get("batch") == fb]
    if u:
        hist = [h for h in hist if h.get("universe") == u]
    if m:
        hist = [h for h in hist if h.get("model") == m]
    hist = sorted(hist, key=lambda h: h.get("computed_at", ""), reverse=True)
    return jsonify({"history": hist[:1500]})


# ===== 24 vs 158 预测对比 (158因子体系第3页): 两套次日买入清单并排 + 重合度 =====
@app.route("/predict-compare")
def predict_compare_page():
    return render_template("predict_compare.html")


@app.route("/api/predict_compare")
def api_predict_compare():
    """对比 24因子(buylist_<tag>) 与 158因子(predictions_a158) 的次日买入清单 + 重合度."""
    import re as _re
    r158 = _read_json(PREDICT_A158_RESULT) or {}
    hits158 = r158.get("hits") or []
    set158 = {h.get("code") for h in hits158 if h.get("code")}
    # 24因子: tag 指定或取索引里最新的
    idx = _read_json(PREDICT_JSON.parent / "buylists_index.json") or {}
    bls = idx.get("buylists") or []
    bls_sorted = sorted(bls, key=lambda b: b.get("updated_at", ""), reverse=True)
    tag = (request.args.get("tag") or "").strip()
    if not tag and bls_sorted:
        tag = bls_sorted[0].get("tag", "")
    bl24 = None
    if tag and _re.fullmatch(r"[A-Za-z0-9_]+", tag):
        bl24 = _read_json(PREDICT_JSON.parent / f"buylist_{tag}.json")
    hits24 = (bl24 or {}).get("hits") or []
    set24 = {h.get("code") for h in hits24 if h.get("code")}
    # 158 补名字 + 标记是否在对方清单
    meta = _meta_for_codes([c for c in set158 if c])
    for h in hits158:
        m = meta.get(h.get("code")) or {}
        h["name"] = h.get("name") or m.get("name", "")
        h["industry"] = h.get("industry") or m.get("industry", "")
        h["in_other"] = h.get("code") in set24
    for h in hits24:
        h["in_other"] = h.get("code") in set158
    common = set158 & set24
    union = set158 | set24
    return jsonify({
        "list158": {"model": r158.get("model"), "as_of": r158.get("as_of"),
                    "generated_at": r158.get("generated_at"), "hits": hits158},
        "list24": {"tag": tag, "model": (bl24 or {}).get("model"), "batch": (bl24 or {}).get("batch"),
                   "as_of": (bl24 or {}).get("as_of"), "hits": hits24},
        "tags": [b.get("tag") for b in bls_sorted][:40],
        "overlap": {"n_common": len(common), "n158": len(set158), "n24": len(set24),
                    "jaccard": round(len(common) / len(union), 3) if union else 0,
                    "common_codes": sorted(common)},
    })


@app.route("/api/predict_compare/batch")
def api_predict_compare_batch():
    """按 24因子批次: 返回该批次所有模型(lgb/xgb/...)的买入清单对比网格, 共识(多模型选中)在前.
    不传 batch 用最新批次。同时返回所有批次列表供下拉切换。"""
    try:
        topn = max(5, min(int(request.args.get("topn", 30)), 80))
    except Exception:
        topn = 30
    idx = _read_json(PREDICT_JSON.parent / "buylists_index.json") or {}
    bls = idx.get("buylists") or []
    bls_sorted = sorted(bls, key=lambda x: x.get("updated_at", ""), reverse=True)
    # 去重批次(保最新序)
    batches = list(dict.fromkeys([b.get("batch") for b in bls_sorted if b.get("batch")]))
    batch = (request.args.get("batch") or "").strip() or (batches[0] if batches else "")
    models = {}
    asof = None
    for b in bls_sorted:
        if b.get("batch") != batch:
            continue
        tag, m = b.get("tag"), b.get("model")
        import re as _re
        if not (tag and _re.fullmatch(r"[A-Za-z0-9_]+", tag)):
            continue
        d = _read_json(PREDICT_JSON.parent / f"buylist_{tag}.json")
        if not d:
            continue
        asof = d.get("as_of") or asof
        hits = (d.get("hits") or [])[:topn]
        models[m] = {h.get("code"): h.get("rank") for h in hits if h.get("code")}
    if not models:
        return jsonify({"batch": batch, "batches": batches, "models": [], "rows": [], "as_of": asof})
    allcodes = set()
    for r in models.values():
        allcodes |= set(r.keys())
    meta = _meta_for_codes(list(allcodes))
    rows = []
    for c in allcodes:
        picks = {m: ranks[c] for m, ranks in models.items() if c in ranks}
        mm = meta.get(c) or {}
        rows.append({"code": c, "name": mm.get("name", ""), "industry": mm.get("industry", ""),
                     "picks": picks, "n_picks": len(picks)})
    rows.sort(key=lambda r: (-r["n_picks"], min(r["picks"].values())))
    mlist = sorted(models.keys())
    return jsonify({"batch": batch, "batches": batches, "models": mlist,
                    "as_of": asof, "topn": topn, "rows": rows})


# ===== 集成选股 (158因子体系第4页): 多模型打分按权重合并重排, 出"精选单" =====
@app.route("/ensemble")
def ensemble_page():
    return render_template("ensemble.html")


@app.route("/api/ensemble/models")
def api_ensemble_models():
    """列出有 Alpha158 打分文件的模型(可参与集成) + 它们的擂台 IR."""
    arena = {m.get("model"): m for m in (_read_json(ALPHA158_ARENA_RESULT) or [])}
    avail = []
    for p in PREDICT_JSON.parent.glob("a158_scores_*.json"):
        m = p.stem.replace("a158_scores_", "")
        d = _read_json(p) or {}
        avail.append({"model": m, "as_of": d.get("as_of"), "n": d.get("n"),
                      "ir": (arena.get(m) or {}).get("ir")})
    avail.sort(key=lambda x: (x.get("ir") if x.get("ir") is not None else -9), reverse=True)
    return jsonify({"models": avail})


@app.route("/api/ensemble", methods=["POST"])
def api_ensemble():
    """多模型打分(rank百分位)按权重合并, 重排出 top-N 精选单. 权重: equal/ir/manual."""
    body = request.get_json(silent=True) or {}
    raw_models = body.get("models")
    if not isinstance(raw_models, list) or not 1 <= len(raw_models) <= 10:
        return jsonify({"ok": False, "message": "请选择 1 到 10 个模型"}), 400
    models = list(dict.fromkeys(str(model).strip().lower() for model in raw_models))
    if any(model not in POOL_MODELS for model in models):
        return jsonify({"ok": False, "message": "模型参数错误"}), 400
    scheme = str(body.get("scheme") or "equal").strip().lower()
    weights_in = body.get("weights") or {}
    if scheme not in {"equal", "ir", "manual"}:
        return jsonify({"ok": False, "message": "权重方案错误"}), 400
    if not isinstance(weights_in, dict):
        return jsonify({"ok": False, "message": "模型权重格式错误"}), 400
    try:
        topn = max(1, min(int(body.get("topn") or 50), 100))
    except Exception:
        topn = 50
    arena = {m.get("model"): m for m in (_read_json(ALPHA158_ARENA_RESULT) or [])}
    loaded = {}
    asof = None
    for m in models:
        d = _read_json(PREDICT_JSON.parent / f"a158_scores_{m}.json")
        if d and d.get("scores"):
            loaded[m] = d["scores"]
            asof = d.get("as_of") or asof
    if not loaded:
        return jsonify({"ok": False, "message": "无可用模型打分; 先在 Alpha158预测页 跑这些模型"})
    # 权重
    if scheme == "manual" and weights_in:
        try:
            w = {m: max(0.0, float(weights_in.get(m, 0))) for m in loaded}
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "模型权重必须是数字"}), 400
        if not all(np.isfinite(value) for value in w.values()):
            return jsonify({"ok": False, "message": "模型权重必须是有限数字"}), 400
        if sum(w.values()) <= 0:
            w = {m: 1.0 for m in loaded}
    elif scheme == "ir":
        w = {m: max(0.01, (arena.get(m) or {}).get("ir") or 0.01) for m in loaded}
    else:
        w = {m: 1.0 for m in loaded}
    tw = sum(w.values()) or 1.0
    w = {m: v / tw for m, v in w.items()}
    # 合并(对每只票, 用拥有它的模型加权平均其 rank 百分位)
    allcodes = set()
    for s in loaded.values():
        allcodes |= set(s.keys())
    ens = {}
    for c in allcodes:
        num = den = 0.0
        for m, s in loaded.items():
            if c in s:
                num += w[m] * float(s[c]); den += w[m]
        if den > 0:
            ens[c] = num / den
    ranked = sorted(ens.items(), key=lambda kv: kv[1], reverse=True)[:topn]
    meta = _meta_for_codes([c for c, _ in ranked])
    hits = []
    for i, (c, sc) in enumerate(ranked, 1):
        mm = meta.get(c) or {}
        hits.append({"rank": i, "code": c, "name": mm.get("name", ""), "industry": mm.get("industry", ""),
                     "ens_score": round(sc, 4),
                     "per_model": {m: round(float(loaded[m].get(c, 0)), 3) for m in loaded}})
    _attach_unlock_info(hits)
    return jsonify({"ok": True, "scheme": scheme, "as_of": asof, "n_models": len(loaded),
                    "models": list(loaded.keys()), "weights": {m: round(v, 3) for m, v in w.items()},
                    "hits": hits})


@app.route("/api/rdagent/model_curves")
def api_rdagent_model_curves():
    """回测对比页: 读取各 批次::模型 的回测净值曲线 (PC 端 run_model.py 写)."""
    data = _read_json(RDAGENT_MODEL_CURVES)
    if not data:
        return jsonify({"curves": {}})
    return jsonify(data)


@app.route("/backtest")
def backtest_page():
    return render_template("backtest.html")


@app.route("/api/index_nav")
def api_index_nav():
    """3个基准指数(沪深300/中证500/中证1000)的收盘序列, 供回测对比页选基准叠加.
    直接读 qlib bin(每次数据准备已自动补到最新), 比曲线里存死的 bench 新。"""
    start = (request.args.get("start") or "2025-07-01").strip()
    out = {}
    for code, name in _BENCH.items():
        d = load_ohlcv(code, adjust="qfq")
        ds, cl = d.get("dates") or [], d.get("close") or []
        s = next((i for i, x in enumerate(ds) if x >= start), None)
        if s is not None:
            out[code] = {"name": name, "dates": ds[s:], "close": [round(float(x), 4) for x in cl[s:]]}
    return jsonify({"indices": out})


# ---------- 日内择时: 分时数据 (tushare stk_mins) ----------

@app.route("/intraday")
def intraday_page():
    return render_template("intraday.html")


def _eastmoney_secid(symbol):
    code = "".join(ch for ch in str(symbol or "") if ch.isdigit())[:6]
    if not code:
        return ""
    market = "1" if code.startswith("6") else "0"
    return f"{market}.{code}"


def _eastmoney_history_intraday(symbol, yyyymmdd, scale, session):
    secid = _eastmoney_secid(symbol)
    if not (secid and yyyymmdd and len(yyyymmdd) == 8):
        return None
    ymd = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
    params = {
        "secid": secid, "klt": str(scale), "fqt": "1", "beg": yyyymmdd, "end": yyyymmdd,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }
    r = session.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                    params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    klines = ((r.json().get("data") or {}).get("klines") or [])
    bars = []
    for item in klines:
        parts = str(item).split(",")
        if len(parts) < 7 or not parts[0].startswith(ymd):
            continue
        try:
            bars.append({
                "time": parts[0][11:16],
                "open": float(parts[1]), "close": float(parts[2]),
                "high": float(parts[3]), "low": float(parts[4]),
                "volume": float(parts[5]), "amount": float(parts[6]),
            })
        except Exception:
            continue
    if not bars:
        return None

    pre_close = None
    try:
        dt = datetime.strptime(yyyymmdd, "%Y%m%d")
        daily = session.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                            params={
                                "secid": secid, "klt": "101", "fqt": "1",
                                "beg": (dt - timedelta(days=20)).strftime("%Y%m%d"),
                                "end": yyyymmdd,
                                "fields1": "f1,f2,f3,f4,f5,f6",
                                "fields2": "f51,f52,f53,f54,f55,f56,f57",
                            },
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=15).json()
        prev = []
        for item in ((daily.get("data") or {}).get("klines") or []):
            parts = str(item).split(",")
            if len(parts) >= 3 and parts[0] < ymd:
                prev.append(parts)
        if prev:
            pre_close = float(prev[-1][2])
    except Exception:
        pre_close = None
    if not pre_close:
        pre_close = bars[0]["open"]

    cum_v = 0.0
    cum_a = 0.0
    cum_cv = 0.0
    vwap = []
    for b in bars:
        vol = b["volume"]
        amt = b["amount"]
        cum_v += vol
        cum_a += amt
        cum_cv += b["close"] * vol
        raw = cum_a / cum_v if cum_v > 0 and cum_a > 0 else None
        candidate = raw / 100.0 if raw and raw > b["high"] * 10 else raw
        if not candidate or candidate < b["low"] * 0.8 or candidate > b["high"] * 1.2:
            candidate = cum_cv / cum_v if cum_v > 0 else b["close"]
        vwap.append(round(float(candidate), 3))

    return {
        "source": "eastmoney_history",
        "code": str(symbol or "").lower(),
        "ts_code": _qlib_code_to_ts(symbol),
        "date": ymd,
        "freq": f"{scale}min",
        "times": [b["time"] for b in bars],
        "open": [round(float(b["open"]), 2) for b in bars],
        "high": [round(float(b["high"]), 2) for b in bars],
        "low": [round(float(b["low"]), 2) for b in bars],
        "close": [round(float(b["close"]), 2) for b in bars],
        "vol": [float(b["volume"]) for b in bars],
        "vwap": vwap,
        "pre_close": round(float(pre_close), 3) if pre_close else None,
    }


def _tushare_history_intraday(symbol, yyyymmdd, freq, pro):
    ts_code = _qlib_code_to_ts(symbol)
    if not (ts_code and yyyymmdd and len(yyyymmdd) == 8 and pro is not None):
        return None
    ymd = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
    start = f"{ymd} 09:00:00"
    end = f"{ymd} 15:30:00"
    df = pro.stk_mins(ts_code=ts_code, freq=freq, start_date=start, end_date=end)
    if df is None or df.empty:
        return None
    g = df.copy()
    time_col = "trade_time" if "trade_time" in g.columns else ("datetime" if "datetime" in g.columns else "")
    if not time_col:
        return None
    g[time_col] = pd.to_datetime(g[time_col], errors="coerce")
    g = g[g[time_col].dt.strftime("%Y%m%d") == yyyymmdd].sort_values(time_col)
    if g.empty:
        return None

    pre_close = None
    try:
        dt = datetime.strptime(yyyymmdd, "%Y%m%d")
        daily = pro.daily(ts_code=ts_code, start_date=(dt - timedelta(days=20)).strftime("%Y%m%d"), end_date=yyyymmdd)
        if daily is not None and not daily.empty and {"trade_date", "close"}.issubset(daily.columns):
            d = daily.copy()
            d["trade_date"] = d["trade_date"].astype(str)
            d = d[d["trade_date"] < yyyymmdd].sort_values("trade_date")
            if not d.empty:
                pre_close = float(d.iloc[-1]["close"])
    except Exception:
        pre_close = None
    if not pre_close:
        pre_close = float(g.iloc[0]["open"])

    close = pd.to_numeric(g["close"], errors="coerce").astype(float).tolist()
    vol_col = "vol" if "vol" in g.columns else ("volume" if "volume" in g.columns else "")
    amount_col = "amount" if "amount" in g.columns else ""
    vol = pd.to_numeric(g[vol_col], errors="coerce").fillna(0).astype(float).tolist() if vol_col else [0.0] * len(g)
    amount = pd.to_numeric(g[amount_col], errors="coerce").fillna(0).astype(float).tolist() if amount_col else [0.0] * len(g)
    cum_v = 0.0
    cum_cv = 0.0
    cum_a = 0.0
    vwap = []
    for c, v, a in zip(close, vol, amount):
        cum_v += v
        cum_cv += c * v
        cum_a += a
        raw = cum_a / cum_v if cum_v > 0 and cum_a > 0 else None
        candidate = raw / 10.0 if raw and raw > c * 10 else raw
        if not candidate or candidate < c * 0.8 or candidate > c * 1.2:
            candidate = cum_cv / cum_v if cum_v > 0 else c
        vwap.append(round(float(candidate), 3))

    return {
        "source": "tushare_stk_mins",
        "code": str(symbol or "").lower(),
        "ts_code": ts_code,
        "date": ymd,
        "freq": freq,
        "times": [x.strftime("%H:%M") for x in g[time_col]],
        "open": [round(float(x), 2) for x in pd.to_numeric(g["open"], errors="coerce").astype(float).tolist()],
        "high": [round(float(x), 2) for x in pd.to_numeric(g["high"], errors="coerce").astype(float).tolist()],
        "low": [round(float(x), 2) for x in pd.to_numeric(g["low"], errors="coerce").astype(float).tolist()],
        "close": [round(float(x), 2) for x in close],
        "vol": vol,
        "vwap": vwap,
        "pre_close": round(float(pre_close), 3) if pre_close else None,
    }


@app.route("/api/intraday")
def api_intraday():
    """单股某日分钟分时数据 + VWAP均价 + 昨收, 供入场择时面板画分时图。
    数据走新浪分时接口(免费、无 tushare stk_mins 的 1次/小时 限频)。
    参数: code=sh600183, freq=5min/15min/30min/60min(默认5min), date=YYYYMMDD(默认最新交易日)."""
    import re as _re
    import requests as _rq
    import numpy as _np
    code = (request.args.get("code") or "").strip().lower()
    freq = (request.args.get("freq") or "5min").strip().lower()
    date = (request.args.get("date") or "").strip().replace("-", "")
    scale = {"5min": 5, "15min": 15, "30min": 30, "60min": 60}.get(freq, 5)
    if not code:
        return jsonify({"error": "code required"}), 400
    # 新浪 symbol = sh600183 / sz000001 (与本系统 code 同格式)
    sym = code if code[:2] in ("sh", "sz", "bj") else _qlib_code_to_ts(code)
    try:
        if date:
            try:
                hist = _eastmoney_history_intraday(sym, date, scale, _rq)
            except Exception:
                hist = None
            if hist:
                return jsonify(hist)
            if TUSHARE_TOKEN:
                try:
                    pro = ts.pro_api(TUSHARE_TOKEN)
                    hist = _tushare_history_intraday(sym, date, freq, pro)
                except Exception:
                    hist = None
                if hist:
                    return jsonify(hist)
        url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData"
        r = _rq.get(url, params={"symbol": sym, "scale": scale, "ma": "no", "datalen": 320},
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        m = _re.search(r"=\((\[.*\])\)", r.text, _re.S)
        rows = json.loads(m.group(1)) if m else []
        if not rows:
            return jsonify({"code": code, "date": date, "freq": freq, "times": [], "message": "无分钟数据"})
        # 按交易日分组
        by_day = {}
        for x in rows:
            day = x["day"][:10]
            by_day.setdefault(day, []).append(x)
        days = sorted(by_day.keys())
        ymd = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if date else days[-1]
        if ymd not in by_day:
            if date:
                return jsonify({
                    "code": code, "ts_code": _qlib_code_to_ts(code), "date": ymd, "freq": freq,
                    "times": [],
                    "message": f"{ymd} 没有分时数据；新浪分钟接口通常只保留最近交易日，不能回放较早公告日分时",
                    "available_dates": days[-5:],
                })
            ymd = days[-1]
        # 昨收: 目标日前一交易日的最后一根 close
        prev_days = [d for d in days if d < ymd]
        pre_close = float(by_day[prev_days[-1]][-1]["close"]) if prev_days else None
        bars = by_day[ymd]
        close = _np.array([float(b["close"]) for b in bars])
        vol = _np.array([float(b["volume"]) for b in bars])
        amt = _np.array([float(b.get("amount") or 0) for b in bars])
        cum_v = _np.cumsum(vol)
        cum_a = _np.cumsum(amt)
        # 优先用成交额/成交量算真 VWAP, 没有 amount 时退回 close*vol
        vwap = _np.where(cum_v > 0, _np.where(cum_a > 0, cum_a / cum_v, _np.cumsum(close * vol) / cum_v), close)
        return jsonify({
            "code": code, "ts_code": _qlib_code_to_ts(code), "date": ymd, "freq": freq,
            "times": [b["day"][11:16] for b in bars],
            "open": [round(float(b["open"]), 2) for b in bars],
            "high": [round(float(b["high"]), 2) for b in bars],
            "low": [round(float(b["low"]), 2) for b in bars],
            "close": [round(float(x), 2) for x in close],
            "vol": [float(x) for x in vol],
            "vwap": [round(float(x), 3) for x in vwap],
            "pre_close": pre_close,
        })
    except Exception as e:
        return jsonify({"error": f"取分钟数据失败: {e}"}), 500


def _intraday_today(qlib_code, scale=5):
    """取某股【今日(或最新交易日)】分时 close 序列 + 昨收(新浪getKLineData)。返回(times,close,pre_close)或(None,..)。"""
    import re as _re, requests as _rq
    sym = qlib_code if qlib_code[:2] in ("sh", "sz", "bj") else _ts_to_qlib_code(qlib_code)
    try:
        url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData"
        r = _rq.get(url, params={"symbol": sym, "scale": scale, "ma": "no", "datalen": 300},
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        m = _re.search(r"=\((\[.*\])\)", r.text, _re.S)
        rows = json.loads(m.group(1)) if m else []
        if not rows:
            return None, None, None
        by_day = {}
        for x in rows:
            by_day.setdefault(x["day"][:10], []).append(x)
        days = sorted(by_day)
        ymd = days[-1]
        prev = [d for d in days if d < ymd]
        pre_close = float(by_day[prev[-1]][-1]["close"]) if prev else float(by_day[ymd][0]["open"])
        bars = by_day[ymd]
        return [b["day"][11:16] for b in bars], [float(b["close"]) for b in bars], pre_close
    except Exception:
        return None, None, None


@app.route("/api/holdings_intraday")
def api_holdings_intraday():
    """持仓各股 + 基准 当日分时(归一成距昨收%), 多股叠加供做T看强弱/波段。"""
    pos = _load_positions()
    codes = [str(p.get("code", "")) for p in pos if p.get("code")]
    meta = _meta_for_codes(codes)
    bench = (request.args.get("bench") or "sh000300").strip()
    series, axis_set = [], set()
    for ts in codes:
        q = _ts_to_qlib_code(ts) if "." in ts else ts.lower()
        times, close, pre = _intraday_today(q, scale=5)
        if not times or not pre:
            continue
        pct = [round((c / pre - 1) * 100, 2) for c in close]
        series.append({"code": ts, "name": (meta.get(ts) or {}).get("name", ""),
                       "times": times, "pct": pct})
        axis_set.update(times)
    # 基准分时
    bt, bc, bp = _intraday_today(bench, scale=5)
    bench_s = None
    if bt and bp:
        bench_s = {"name": _BENCH.get(bench, bench), "times": bt,
                   "pct": [round((c / bp - 1) * 100, 2) for c in bc]}
        axis_set.update(bt)
    axis = sorted(axis_set)
    # 对齐到统一时间轴(缺失=null)
    def align(s):
        m = dict(zip(s["times"], s["pct"]))
        return [m.get(t) for t in axis]
    out = [{"code": s["code"], "name": s["name"], "pct": align(s)} for s in series]
    bench_out = {"name": bench_s["name"], "pct": align(bench_s)} if bench_s else None
    return jsonify({"axis": axis, "series": out, "bench": bench_out,
                    "ts": datetime.now().strftime("%H:%M:%S")})


# ===== 经典理论做T信号(分时): BIAS乖离/布林带/ATR轨道/ORB开盘区间突破/VWAP =====
# 数据源 tushare stk_mins(盘中按需, 带60s缓存防限流)。仅对持仓做T(A股T+1: 当天买不能卖, 只能高抛低吸已有持仓)。
_MINS_CACHE = {}  # ts_code|freq -> (epoch, bars, pre_close)


def _em_mins(ts_code, freq="5min"):
    """东财实时分钟K: 支持1分钟, 盘中即有今日bar(新浪scale=1不支持1分钟, tushare盘后才有)。免费直连。
    返回(今日bars升序, pre_close) 或 ([],None)。klines字段: 时间,开,收,高,低,量。"""
    import requests
    klt = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60}.get(freq, 5)
    c = ts_code.upper()
    secid = ("1." + c[:6]) if c.endswith(".SH") else ("0." + c[:6])   # SH=1, SZ/BJ=0
    r = requests.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                     params={"secid": secid, "klt": klt, "fqt": 1, "fields1": "f1",
                             "fields2": "f51,f52,f53,f54,f55,f56", "beg": "0", "end": "20500101", "lmt": 480},
                     timeout=6, headers={"User-Agent": "Mozilla/5.0"})
    kl = (json.loads(r.text).get("data") or {}).get("klines") or []
    if not kl:
        return [], None
    days = {}; order = []
    for k in kl:
        p = k.split(","); d = p[0][:10]
        if d not in days:
            days[d] = []; order.append(d)
        days[d].append(p)
    today = order[-1]
    prev = [d for d in order if d < today]
    pre_close = float(days[prev[-1]][-1][2]) if prev else float(days[today][0][1])
    bars = [{"t": p[0][11:16], "o": float(p[1]), "c": float(p[2]), "h": float(p[3]),
             "l": float(p[4]), "v": float(p[5])} for p in days[today]]
    return bars, pre_close


def _sina_mins(ts_code, freq="5min"):
    """新浪实时分钟K: 盘中即有今日bar(tushare stk_mins盘后才有, 盘中只到昨日)。免费直连无需装包。
    注意: 新浪不支持1分钟(scale=1返回空), 1分钟走东财。返回(今日bars升序, pre_close) 或 ([],None)。"""
    import requests
    scale = {"5min": 5, "15min": 15, "30min": 30, "60min": 60, "1min": 1}.get(freq, 5)
    c = ts_code.lower()
    sym = ("sh" + c[:6]) if c.endswith(".sh") else ("bj" + c[:6]) if c.endswith(".bj") else ("sz" + c[:6])
    r = requests.get("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
                     params={"symbol": sym, "scale": scale, "ma": "no", "datalen": 240}, timeout=6,
                     headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
    data = json.loads(r.text)
    if not data:
        return [], None
    days = {}
    order = []
    for b in data:
        d = b["day"][:10]
        if d not in days:
            days[d] = []; order.append(d)
        days[d].append(b)
    today = order[-1]
    prev = [d for d in order if d < today]
    pre_close = float(days[prev[-1]][-1]["close"]) if prev else float(days[today][0]["open"])
    bars = [{"t": b["day"][11:16], "o": float(b["open"]), "h": float(b["high"]),
             "l": float(b["low"]), "c": float(b["close"]), "v": float(b["volume"])} for b in days[today]]
    return bars, pre_close


def _intraday_mins(ts_code, freq="5min"):
    """最新交易日分时 OHLCV + 昨收。盘中优先新浪(有今日bar), 失败回退 tushare stk_mins。返回(bars, pre_close)升序。"""
    import time as _t
    key = f"{ts_code}|{freq}"
    hit = _MINS_CACHE.get(key)
    if hit and _t.time() - hit[0] < 60:
        return hit[1], hit[2]
    for _src in (_em_mins, _sina_mins):   # 东财(支持1分钟)优先, 新浪次之, 都给今日bar
        try:
            b, pc = _src(ts_code, freq)
            if b:
                _MINS_CACHE[key] = (_t.time(), b, pc)
                return b, pc
        except Exception:
            pass
    try:
        pro = _tushare_api()
        end = datetime.now()
        start = end - timedelta(days=6)
        df = pro.stk_mins(ts_code=ts_code, freq=freq,
                          start_date=start.strftime("%Y-%m-%d 09:00:00"),
                          end_date=end.strftime("%Y-%m-%d 15:05:00"))
        if df is None or not len(df):
            return [], None
        df = df.sort_values("trade_time")
        df["day"] = df["trade_time"].str[:10]
        days = sorted(df["day"].unique())
        today = days[-1]
        prev = [d for d in days if d < today]
        pre_close = (float(df[df["day"] == prev[-1]]["close"].iloc[-1]) if prev
                     else float(df[df["day"] == today]["open"].iloc[0]))
        td = df[df["day"] == today]
        bars = [{"t": str(r["trade_time"])[11:16], "o": float(r["open"]), "h": float(r["high"]),
                 "l": float(r["low"]), "c": float(r["close"]), "v": float(r["vol"])}
                for _, r in td.iterrows()]
        _MINS_CACHE[key] = (_t.time(), bars, pre_close)
        return bars, pre_close
    except Exception as e:
        return [], None


def _rt_quotes(ts_codes):
    """ts.realtime_quote 批量实时盘口(免费, 走新浪东财) -> {ts_code:{price,pre_close,open,high,low,pct,time}}。"""
    out = {}
    cs = [c for c in ts_codes if c][:50]
    if not cs:
        return out
    try:
        df = ts.realtime_quote(ts_code=",".join(cs))
        for _, r in df.iterrows():
            tc = str(r.get("TS_CODE") or r.get("ts_code") or "")
            try:
                price = float(r.get("PRICE") or 0); pre = float(r.get("PRE_CLOSE") or 0)
            except (TypeError, ValueError):
                continue
            if not tc or price <= 0:
                continue
            # 五档挂单失衡(描述当前盘口买卖挂单压力, 非成交流非预测): (买5档量−卖5档量)/(总)
            def _v(k):
                try:
                    return float(r.get(k) or 0)
                except (TypeError, ValueError):
                    return 0.0
            bidv = sum(_v(f"B{i}_V") for i in range(1, 6))
            askv = sum(_v(f"A{i}_V") for i in range(1, 6))
            ob = round((bidv - askv) / (bidv + askv), 2) if (bidv + askv) > 0 else None
            out[tc] = {"price": price, "pre_close": pre, "time": str(r.get("TIME") or ""),
                       "open": float(r.get("OPEN") or 0),
                       "high": float(r.get("HIGH") or price), "low": float(r.get("LOW") or price),
                       "pct": round((price / pre - 1) * 100, 2) if pre else None,
                       "bid_vol": int(bidv), "ask_vol": int(askv), "ob_imb": ob}
    except Exception:
        pass
    return out


def _live_status(price, ind, pre):
    """用实时价 对 最新一根的指标轨道, 算当前低吸/高抛信号(比stk_mins延迟的cur更实时)。"""
    ma = ind["ma"][-1]; vwap = ind["vwap"][-1]
    bu, bd = ind["boll_up"][-1], ind["boll_dn"][-1]; au, ad = ind["atr_up"][-1], ind["atr_dn"][-1]
    orb_h, orb_l = ind["orb_h"], ind["orb_l"]; bth = ind["params"]["bias_th"]
    bias = round((price - ma) / ma * 100, 2) if ma else 0
    rs = []; rs2 = []
    if price <= bd: rs.append("触布林下轨")
    if bias <= -bth: rs.append(f"乖离{bias}%")
    if price <= ad: rs.append("触ATR下轨")
    if price < orb_l: rs.append("破ORB下沿")
    if price >= bu: rs2.append("触布林上轨")
    if bias >= bth: rs2.append(f"乖离+{bias}%")
    if price >= au: rs2.append("触ATR上轨")
    if price > orb_h: rs2.append("破ORB上沿")
    return {"price": round(price, 3), "bias": bias, "vwap": round(vwap, 3),
            "pct": round((price / pre - 1) * 100, 2) if pre else None,
            "vs_vwap": round((price / vwap - 1) * 100, 2) if vwap else None,
            "regime": ind["cur"].get("regime"),
            "signal": ("低吸" if rs else ("高抛" if rs2 else "—")), "why": "·".join(rs or rs2)}


def _classic_indicators(bars, pre_close, N=20, k_boll=2.0, atr_n=14, k_atr=1.5, orb_bars=6, bias_th=1.5):
    """从分时OHLC算 BIAS/布林/ATR轨道/ORB/VWAP + 逐bar低吸高抛信号 + 当前状态。"""
    import numpy as np
    n = len(bars)
    if n < 3:
        return None
    c = np.array([b["c"] for b in bars]); h = np.array([b["h"] for b in bars])
    l = np.array([b["l"] for b in bars]); v = np.array([b["v"] for b in bars])

    def rmean(a, w): return [float(np.mean(a[max(0, i - w + 1):i + 1])) for i in range(n)]
    def rstd(a, w): return [float(np.std(a[max(0, i - w + 1):i + 1])) for i in range(n)]
    ma = rmean(c, N); sd = rstd(c, N)
    boll_up = [round(ma[i] + k_boll * sd[i], 3) for i in range(n)]
    boll_dn = [round(ma[i] - k_boll * sd[i], 3) for i in range(n)]
    bias = [round((c[i] - ma[i]) / ma[i] * 100, 2) if ma[i] else 0 for i in range(n)]
    tr = [float(h[0] - l[0])] + [float(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))) for i in range(1, n)]
    atr = rmean(np.array(tr), atr_n)
    atr_up = [round(ma[i] + k_atr * atr[i], 3) for i in range(n)]
    atr_dn = [round(ma[i] - k_atr * atr[i], 3) for i in range(n)]
    orb_h = round(float(np.max(h[:orb_bars])), 3); orb_l = round(float(np.min(l[:orb_bars])), 3)
    tp = (h + l + c) / 3; cumv = np.cumsum(v); cumtpv = np.cumsum(tp * v)
    vwap = [round(float(cumtpv[i] / cumv[i]) if cumv[i] else float(c[i]), 3) for i in range(n)]
    sig = []
    for i in range(n):
        rs = []
        if c[i] <= boll_dn[i]: rs.append("触布林下轨")
        if bias[i] <= -bias_th: rs.append(f"乖离{bias[i]}%")
        if c[i] <= atr_dn[i]: rs.append("触ATR下轨")
        if i >= orb_bars and c[i] < orb_l: rs.append("破ORB下沿")
        rs2 = []
        if c[i] >= boll_up[i]: rs2.append("触布林上轨")
        if bias[i] >= bias_th: rs2.append(f"乖离+{bias[i]}%")
        if c[i] >= atr_up[i]: rs2.append("触ATR上轨")
        if i >= orb_bars and c[i] > orb_h: rs2.append("破ORB上沿")
        if rs:
            sig.append({"i": i, "t": bars[i]["t"], "type": "低吸", "price": round(float(c[i]), 3), "why": "·".join(rs)})
        elif rs2:
            sig.append({"i": i, "t": bars[i]["t"], "type": "高抛", "price": round(float(c[i]), 3), "why": "·".join(rs2)})
    # 趋势过滤: 震荡(反T有效) vs 趋势下跌(反T失效, 别低吸接飞刀) vs 趋势上涨
    K = min(12, n - 1)
    vwslope = (vwap[-1] - vwap[-K - 1]) / vwap[-K - 1] if (n > K and vwap[-K - 1]) else 0
    day_hi = float(np.max(h)); day_lo = float(np.min(l))
    pos_rng = (float(c[-1]) - day_lo) / (day_hi - day_lo) if day_hi > day_lo else 0.5
    below = sum(1 for i in range(n - K, n) if c[i] < vwap[i]) / K if n > K else 0.5
    if vwslope < -0.002 and pos_rng < 0.38 and float(c[-1]) < vwap[-1] and below > 0.6:
        regime = "趋势下跌"
    elif vwslope > 0.002 and pos_rng > 0.62 and float(c[-1]) > vwap[-1] and below < 0.4:
        regime = "趋势上涨"
    else:
        regime = "震荡"
    last = sig[-1] if (sig and sig[-1]["i"] == n - 1) else None
    cur = {"price": round(float(c[-1]), 3), "bias": bias[-1], "vwap": vwap[-1],
           "regime": regime, "pos_rng": round(pos_rng, 2),
           "pct": round((float(c[-1]) / pre_close - 1) * 100, 2) if pre_close else None,
           "vs_vwap": round((float(c[-1]) / vwap[-1] - 1) * 100, 2) if vwap[-1] else None,
           "signal": (last["type"] if last else "—"), "why": (last["why"] if last else "")}
    return {"times": [b["t"] for b in bars], "close": [round(float(x), 3) for x in c],
            "ma": [round(x, 3) for x in ma], "boll_up": boll_up, "boll_dn": boll_dn,
            "atr_up": atr_up, "atr_dn": atr_dn, "vwap": vwap, "bias": bias,
            "orb_h": orb_h, "orb_l": orb_l, "orb_n": orb_bars, "signals": sig, "cur": cur,
            "params": {"N": N, "k_boll": k_boll, "atr_n": atr_n, "k_atr": k_atr, "orb_bars": orb_bars, "bias_th": bias_th}}


@app.route("/intraday-classic")
def intraday_classic_page():
    """经典理论做T信号: 持仓分时图叠 BIAS/布林/ATR轨道/ORB + 低吸高抛信号 + 监控看板。A股T+1, 仅做T已有持仓。"""
    return render_template("intraday_classic.html")


@app.route("/api/intraday_classic")
def api_intraday_classic():
    code = (request.args.get("code") or "").strip()
    freq = (request.args.get("freq") or "5min").strip()
    if freq not in ("1min", "5min", "15min", "30min"):
        freq = "5min"
    ts_code = _resolve_to_tscode(code) or _code_to_ts(code)   # 支持 代码/拼音首字母/名称
    if not ts_code:
        return jsonify({"ok": False, "message": "代码无法识别"})
    name = (_meta_for_codes([ts_code]).get(ts_code) or {}).get("name", "")
    bars, pre = _intraday_mins(ts_code, freq)
    if not bars:
        return jsonify({"ok": False, "code": ts_code, "name": name,
                        "message": "暂无分时数据(非交易日/盘前, 或tushare分钟权限/限流)"})
    # 实时价更新最末一根(让图尾和信号是live, 不等stk_mins延迟); 不改缓存
    rt = _rt_quotes([ts_code]).get(ts_code)
    if rt and rt["price"] > 0:
        bars = bars[:-1] + [dict(bars[-1], c=rt["price"],
                                 h=max(bars[-1]["h"], rt["price"]), l=min(bars[-1]["l"], rt["price"]))]
        if rt.get("pre_close"):
            pre = rt["pre_close"]
    ind = _classic_indicators(bars, pre)
    if ind is None:
        return jsonify({"ok": False, "message": "分时数据太少, 算不了指标"})
    if rt:   # 盘口五档挂单失衡(描述, 非预测)
        ind["cur"]["ob_imb"] = rt.get("ob_imb")
        ind["cur"]["bid_vol"] = rt.get("bid_vol"); ind["cur"]["ask_vol"] = rt.get("ask_vol")
    return jsonify({"ok": True, "code": ts_code, "name": name, "freq": freq, "pre_close": pre,
                    "live": bool(rt), "ts": datetime.now().strftime("%H:%M:%S"), **ind})


@app.route("/api/intraday_classic/board")
def api_intraday_classic_board():
    """监控看板: 持仓各股当前做T信号状态(触发的置顶)。"""
    freq = (request.args.get("freq") or "5min").strip()
    pos = _load_positions()
    codes = [_code_to_ts(p.get("code", "")) for p in pos]
    codes = [c for c in codes if c]
    rtq = _rt_quotes(codes)   # 一次批量拉所有持仓实时价(快/省限流)
    meta = _meta_for_codes(codes)
    rows = []
    for ts_code in codes:
        bars, pre = _intraday_mins(ts_code, freq)   # 指标轨道用stk_mins(缓存60s, 轨道变化慢)
        if not bars:
            continue
        ind = _classic_indicators(bars, pre)
        if not ind:
            continue
        rt = rtq.get(ts_code)
        cur = _live_status(rt["price"], ind, rt.get("pre_close") or pre) if rt else ind["cur"]   # 信号用实时价
        rows.append({"code": ts_code, "name": (meta.get(ts_code) or {}).get("name", ""),
                     "price": cur["price"], "pct": cur["pct"], "bias": cur["bias"],
                     "vs_vwap": cur["vs_vwap"], "signal": cur["signal"], "why": cur["why"],
                     "ob_imb": (rt.get("ob_imb") if rt else None)})
    order = {"低吸": 0, "高抛": 1, "—": 2}
    rows.sort(key=lambda r: order.get(r["signal"], 3))
    return jsonify({"rows": rows, "ts": datetime.now().strftime("%H:%M:%S"), "n_sig": sum(1 for r in rows if r["signal"] != "—")})


def _surge_t_plan(bars, pre_close, cost=None):
    """生成可解释的冲高卖出、回落买回执行计划。"""
    if len(bars) < 3 or not pre_close:
        return None
    ind = _classic_indicators(bars, pre_close)
    if not ind:
        return None
    closes = np.array([float(b["c"]) for b in bars])
    highs = np.array([float(b["h"]) for b in bars])
    lows = np.array([float(b["l"]) for b in bars])
    volumes = np.array([max(0.0, float(b.get("v") or 0)) for b in bars])
    cur, day_high, day_low = float(closes[-1]), float(highs.max()), float(lows.min())
    high_i = int(highs.argmax())
    high_pct = (day_high / pre_close - 1) * 100
    pullback = (cur / day_high - 1) * 100
    vwap = float(ind["vwap"][-1])
    vs_vwap = (cur / vwap - 1) * 100 if vwap else 0.0
    rng_pos = (cur - day_low) / (day_high - day_low) * 100 if day_high > day_low else 50.0
    recent_n = min(3, len(volumes))
    base_vol = float(np.mean(volumes[:-recent_n])) if len(volumes) > recent_n else float(np.mean(volumes))
    recent_vol = float(np.mean(volumes[-recent_n:]))
    vol_ratio = recent_vol / base_vol if base_vol > 0 else 1.0

    surged = high_pct >= 1.0
    sell_score = int(surged) + int(pullback <= -0.35) + int(cur >= vwap) + int(rng_pos >= 65)
    missed_sell = bool(surged and pullback <= -2.0 and cur < vwap)
    if not surged:
        sell_action = "等待冲高"
    elif missed_sell:
        sell_action = "卖点已过，不追卖"
    elif sell_score >= 3:
        sell_action = "分批卖出T仓"
    elif cur >= day_high * 0.995:
        sell_action = "冲高中，等回落确认"
    else:
        sell_action = "暂缓卖出"

    dynamic_resist = min(day_high, max(float(ind["boll_up"][-1]), float(ind["atr_up"][-1])))
    sell_low = max(day_high * 0.985, min(day_high * 0.997, dynamic_resist))
    sell_high = day_high
    lower_rail = max(float(ind["boll_dn"][-1]), float(ind["atr_dn"][-1]))
    buy_high = min(vwap * 0.998, cur)
    buy_low = min(buy_high, max(lower_rail, day_low))
    buy_discount = (buy_high / sell_low - 1) * 100 if sell_low else 0.0
    stabilizing = bool(len(closes) >= 3 and closes[-1] >= closes[-2] and lows[-1] >= lows[-2])
    buy_ready = bool(cur <= vwap * 0.998 and rng_pos <= 45 and stabilizing
                     and ind["cur"]["regime"] != "趋势下跌")
    buy_action = "若已高抛：可分批买回" if buy_ready else ("若已高抛：等止跌" if cur <= vwap else "等回落到VWAP下方")
    if ind["cur"]["regime"] == "趋势下跌":
        buy_action = "取消买回，防接飞刀"

    if buy_ready:
        phase, now_action = "回落止跌区", "仅在此前已卖出T仓时，分2笔买回"
        next_trigger = f"回到 {buy_high:.3f} 上方并保持止跌"
    elif missed_sell:
        phase, now_action = "冲高已回落", "不在低位补卖；未做高抛则今天放弃这次T"
        next_trigger = f"若此前已卖，等待 {buy_low:.3f}–{buy_high:.3f} 止跌"
    elif sell_score >= 3:
        phase, now_action = "冲高回落确认", "可将计划做T数量分2–3笔卖出"
        next_trigger = f"卖出观察区 {sell_low:.3f}–{sell_high:.3f}"
    elif surged:
        phase, now_action = "冲高中", "先等回落确认，不猜最高点"
        next_trigger = f"高点回落0.35%且仍在VWAP {vwap:.3f} 上方"
    else:
        phase, now_action = "尚未冲高", "不操作"
        next_trigger = f"日内涨幅达到1%，并进入 {sell_low:.3f}–{sell_high:.3f}"

    now_t = bars[-1]["t"]
    if now_t < "09:45":
        timing = "开盘噪声期，先观察"
    elif "11:20" < now_t < "13:00":
        timing = "午间休市"
    elif now_t > "14:45":
        timing = "临近收盘，不新开T仓"
    else:
        timing = "按价格与止跌条件执行"
    return {
        "price": round(cur, 3), "pct": round((cur / pre_close - 1) * 100, 2),
        "cost_ret": round((cur / float(cost) - 1) * 100, 2) if cost else None,
        "day_high": round(day_high, 3), "day_low": round(day_low, 3),
        "high_time": bars[high_i]["t"], "high_pct": round(high_pct, 2),
        "pullback": round(pullback, 2), "range_pos": round(rng_pos, 0),
        "vwap": round(vwap, 3), "vs_vwap": round(vs_vwap, 2),
        "vol_ratio": round(vol_ratio, 2), "regime": ind["cur"]["regime"],
        "surged": surged, "sell_score": sell_score, "sell_action": sell_action,
        "missed_sell": missed_sell, "phase": phase, "now_action": now_action,
        "next_trigger": next_trigger,
        "sell_zone": [round(sell_low, 3), round(sell_high, 3)],
        "buy_action": buy_action, "buy_ready": buy_ready,
        "buy_zone": [round(buy_low, 3), round(buy_high, 3)],
        "buy_discount": round(buy_discount, 2),
        "sell_window": "09:45–10:30；午后再冲高看13:15–14:15",
        "buy_window": "10:30–11:20；或14:00–14:40止跌后",
        "timing": timing,
        "invalid": f"跌破日内低点 {day_low:.3f} 或持续低于VWAP时取消买回",
        "chart": {
            "times": ind["times"],
            "close": ind["close"],
            "vwap": ind["vwap"],
            "high_index": high_i,
        },
    }


@app.route("/surge-t")
def surge_t_page():
    return render_template("surge_t.html")


@app.route("/api/surge_t")
def api_surge_t():
    """只为已有持仓生成实时日内执行计划。"""
    freq = (request.args.get("freq") or "5min").strip()
    if freq not in ("1min", "5min", "15min"):
        freq = "5min"
    positions = _load_positions()
    backtest_path = PREDICT_JSON.parent / "surge_t_backtest.json"
    if not backtest_path.exists():
        backtest_path = Path(__file__).parent / "data" / "surge_t_backtest.json"
    backtest = _read_json(backtest_path) or {}
    folds = backtest.get("folds") or []
    tested = (folds[-1].get("params") if folds else None) or {
        "dev_atr": 1.5, "range_pos": 0.35, "surge_pct": 1.5,
        "reject_pct": 0.6, "max_hold": 5, "stop_pct": 5.0, "stop_atr": 2.0,
    }
    codes = [_code_to_ts(p.get("code", "")) for p in positions]
    codes = [c for c in codes if c]
    meta, rtq = _meta_for_codes(codes), _rt_quotes(codes)
    rows = []
    for p in positions:
        ts_code = _code_to_ts(p.get("code", ""))
        if not ts_code:
            continue
        bars, pre = _intraday_mins(ts_code, freq)
        if not bars:
            continue
        rt = rtq.get(ts_code)
        if rt and rt.get("price", 0) > 0:
            bars = bars[:-1] + [dict(bars[-1], c=rt["price"],
                                     h=max(bars[-1]["h"], rt["price"]),
                                     l=min(bars[-1]["l"], rt["price"]))]
            pre = rt.get("pre_close") or pre
        plan = _surge_t_plan(bars, pre, p.get("cost"))
        if plan:
            # 日线过滤与昨日压力位：使用不复权价格，和盘中实时价保持同一口径。
            qcode = _ts_to_qlib_code(ts_code)
            daily = load_ohlcv(qcode, last_n_days=80, adjust="none")
            dates, dc = daily.get("dates") or [], daily.get("close") or []
            dh, dl = daily.get("high") or [], daily.get("low") or []
            today_iso = datetime.now().strftime("%Y-%m-%d")
            last_done = len(dates) - 2 if dates and dates[-1] >= today_iso else len(dates) - 1
            prev_high = float(dh[last_done]) if last_done >= 0 and len(dh) > last_done else None
            prev_close = float(dc[last_done]) if last_done >= 0 and len(dc) > last_done else None
            hist_end = last_done + 1
            ma20 = float(np.mean(dc[max(0, hist_end - 20):hist_end])) if hist_end > 0 else None
            ma60 = float(np.mean(dc[max(0, hist_end - 60):hist_end])) if hist_end > 0 else None
            daily_up = bool(prev_close and ma20 and ma60 and prev_close >= ma20 and ma20 >= ma60)
            if hist_end >= 2:
                tr = []
                for j in range(max(1, hist_end - 14), hist_end):
                    tr.append(max(float(dh[j]) - float(dl[j]),
                                  abs(float(dh[j]) - float(dc[j - 1])),
                                  abs(float(dl[j]) - float(dc[j - 1]))))
                daily_atr = float(np.mean(tr)) if tr else None
            else:
                daily_atr = None

            orb = [b for b in bars if b["t"] <= "10:00"]
            orb_high = max((float(b["h"]) for b in orb), default=None)
            orb_low = min((float(b["l"]) for b in orb), default=None)
            c = plan["chart"]["close"]
            intraday_tr = [max(float(b["h"]) - float(b["l"]),
                               abs(float(b["h"]) - float(bars[k - 1]["c"])) if k else 0,
                               abs(float(b["l"]) - float(bars[k - 1]["c"])) if k else 0)
                           for k, b in enumerate(bars)]
            intraday_atr = float(np.mean(intraday_tr[-14:])) if intraday_tr else 0
            dev_atr = ((plan["price"] - plan["vwap"]) / intraday_atr
                       if intraday_atr > 0 else 0)
            intraday_stable = bool(len(c) >= 3 and c[-1] >= c[-2] and min(c[-2:]) >= min(c[-3:]))
            day1_buy = bool(daily_up and dev_atr <= -float(tested["dev_atr"])
                            and plan["range_pos"] <= float(tested["range_pos"]) * 100
                            and intraday_stable)

            active_lots = sorted((p.get("lots") or []),
                                 key=lambda x: (x.get("buy_date", ""), x.get("created_at", "")))
            t_lot = active_lots[-1] if active_lots else {}
            buy_date = str(t_lot.get("buy_date") or p.get("date") or "")[:10]
            qty = p.get("qty")
            available_qty = int(p.get("available_qty") or 0)
            t_qty = (int(t_lot.get("remaining_qty") or 0)
                     if buy_date and buy_date < today_iso else 0)
            trading_dates = [x for x in dates if buy_date and buy_date < x <= today_iso]
            hold_days = len(trading_dates)
            cost = float(t_lot.get("cost") or p.get("cost")) if (t_lot.get("cost") or p.get("cost")) else None
            stop_price = (max(cost * (1 - float(tested["stop_pct"]) / 100),
                              cost - float(tested["stop_atr"]) * daily_atr)
                          if cost and daily_atr else None)
            positive_spread = bool(cost and plan["price"] / cost - 1 >= 0.003)
            tested_sell = bool(
                available_qty and positive_spread
                and plan["high_pct"] >= float(tested["surge_pct"])
                and plan["pullback"] <= -float(tested["reject_pct"])
            )
            within_window = bool(1 <= hold_days <= int(tested["max_hold"]))
            tested_stop = bool(within_window and stop_price and plan["price"] <= stop_price)
            if tested_stop:
                plan["strategy_side"] = "风控止损"
                plan["now_action"] = f"现价已触及回测止损线 {stop_price:.3f}，不要继续按做T逻辑等待"
                plan["next_trigger"] = "执行风险控制；是否保留长期底仓需单独决策"
            elif tested_sell and within_window:
                plan["strategy_side"] = "T仓高抛"
                plan["now_action"] = f"最近已解锁T仓 {t_qty} 股；按LIFO分2–3笔高抛"
                plan["next_trigger"] = (f"已满足：涨幅≥{tested['surge_pct']}%、"
                                        f"高点回落≥{tested['reject_pct']}%、正价差≥0.3%")
            elif plan["missed_sell"]:
                plan["strategy_side"] = "卖点已过"
            elif day1_buy:
                plan["strategy_side"] = "第一天低吸"
                plan["now_action"] = "日线趋势合格且分时止跌；可按计划仓位分2笔低吸"
                plan["next_trigger"] = f"守住 {plan['buy_zone'][0]:.3f}，重新站稳VWAP更安全"
            else:
                plan["strategy_side"] = "底仓监控" if hold_days > int(tested["max_hold"]) else "等待"
            plan.update({
                "available_qty": available_qty, "buy_date": buy_date,
                "t_qty": t_qty, "lot_count": len(active_lots),
                "hold_days": hold_days, "max_hold": int(tested["max_hold"]),
                "stop_price": round(stop_price, 3) if stop_price else None,
                "tested_params": tested,
                "prev_high": round(prev_high, 3) if prev_high else None,
                "prev_close": round(prev_close, 3) if prev_close else None,
                "ma20": round(ma20, 3) if ma20 else None,
                "ma60": round(ma60, 3) if ma60 else None,
                "daily_up": daily_up, "day1_buy": day1_buy,
                "daily_atr": round(daily_atr, 3) if daily_atr else None,
                "dev_atr": round(dev_atr, 2),
                "orb_high": round(orb_high, 3) if orb_high else None,
                "orb_low": round(orb_low, 3) if orb_low else None,
            })
            rows.append({"code": ts_code, "name": (meta.get(ts_code) or {}).get("name", ""),
                         "qty": p.get("qty"), "cost": p.get("cost"), **plan})
    side_order = {"风控止损": 0, "T仓高抛": 1, "第一天低吸": 2,
                  "卖点已过": 3, "等待": 4, "底仓监控": 5}
    rows.sort(key=lambda x: (side_order.get(x["strategy_side"], 9), -x["sell_score"], -x["high_pct"]))
    return jsonify({"rows": rows, "count": len(rows), "freq": freq,
                    "tested_params": tested,
                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "message": "" if rows else "暂无可用的持仓分钟数据；请确认已录入持仓且行情源可用。"})


@app.route("/api/surge_t/backtest")
def api_surge_t_backtest():
    path = PREDICT_JSON.parent / "surge_t_backtest.json"
    if not path.exists():
        path = Path(__file__).parent / "data" / "surge_t_backtest.json"
    data = _read_json(path)
    return jsonify(data or {
        "oos_metrics": None,
        "message": "尚未运行滚动样本外回测：python scripts/backtest_surge_t.py",
    })


@app.route("/api/intraday_classic/analog")
def api_intraday_analog():
    """历史相似片段基率(诚实版, 非预测): 该股历史上'跌破20日布林下轨(超卖)'之后5/10/20日的反弹概率+中位收益,
    并按当时是否在【下跌趋势(收盘<MA60)】拆开 —— 直接回答'一路下跌中超卖, 还会不会反弹'。"""
    import numpy as np
    code = (request.args.get("code") or "").strip()
    ts_code = _resolve_to_tscode(code) or _code_to_ts(code)   # 支持 代码/拼音首字母/名称
    if not ts_code:
        return jsonify({"ok": False, "message": "代码无法识别"})
    name = (_meta_for_codes([ts_code]).get(ts_code) or {}).get("name", "")
    qcode = _ts_to_qlib_code(ts_code) if "." in ts_code else ts_code.lower()
    d = load_ohlcv(qcode, last_n_days=900, adjust="qfq")
    closes = d.get("close") or []
    n = len(closes)
    if n < 150:
        return jsonify({"ok": False, "code": ts_code, "name": name, "message": "历史日线不足(新股/数据少)"})
    c = np.array(closes, dtype=float)
    ma20 = np.array([c[max(0, i - 19):i + 1].mean() for i in range(n)])
    sd20 = np.array([c[max(0, i - 19):i + 1].std() for i in range(n)])
    ma60 = np.array([c[max(0, i - 59):i + 1].mean() for i in range(n)])
    hi20 = np.array([c[max(0, i - 19):i + 1].max() for i in range(n)])
    lower = ma20 - 2 * sd20
    oversold = c < lower
    downtrend = c < ma60
    HZ = [5, 10, 20]
    rec = {h: [] for h in HZ}   # (ret, in_downtrend)
    cnt = 0
    for i in range(60, n):
        if not oversold[i]:
            continue
        cnt += 1
        for h in HZ:
            if i + h < n:
                rec[h].append((c[i + h] / c[i] - 1, bool(downtrend[i])))

    def stat(arr):
        if not arr:
            return None
        a = np.array([x[0] for x in arr])
        return {"n": len(a), "pos_pct": round(float((a > 0).mean()) * 100),
                "med": round(float(np.median(a)) * 100, 1), "avg": round(float(a.mean()) * 100, 1),
                "fall_pct": round(float((a < -0.03).mean()) * 100)}   # 继续跌>3%占比
    horizons = []
    for h in HZ:
        allr = rec[h]; downr = [x for x in allr if x[1]]
        horizons.append({"d": h, "all": stat(allr), "down": stat(downr)})
    cur_os = bool(oversold[-1]); cur_down = bool(downtrend[-1])
    cur_dd = round(float((c[-1] / hi20[-1] - 1) * 100), 1)
    return jsonify({"ok": True, "code": ts_code, "name": name, "n_analog": cnt,
                    "current_oversold": cur_os, "current_downtrend": cur_down, "cur_drawdown": cur_dd,
                    "lookback_days": n, "horizons": horizons})


@app.route("/api/rdagent/model_history")
def api_rdagent_model_history():
    """模型实验室: 每次跑的历史记录 (不去重, 供按模型/批次/时间对比)."""
    data = _read_json(PREDICT_JSON.parent / "model_runs_history.json")
    if not data:
        return jsonify({"runs": []})
    return jsonify(data)


@app.route("/api/rdagent/run_all", methods=["POST"])
def api_rdagent_run_all():
    """网页按钮触发: 在指定批次上把所有模型 训练+回测 + 各出买入清单 (供对比). PC 执行, 较慢."""
    batch = (request.args.get("batch", "") or "").strip()
    if not _valid_job_label(batch):
        return jsonify({"ok": False, "message": "批次标签包含非法字符"}), 400
    gen_reports = (request.args.get("gen_reports", "").strip().lower() in ("1", "true", "yes"))
    try:
        queued = _queue_rdagent_request(
            {"run_all": True, "batch": batch, "gen_reports": gen_reports}
        )
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"}), 503
    if queued is None:
        return jsonify({"ok": False, "message": "已有 RD-Agent 任务在排队/处理中"}), 409
    bmsg = f" [批次 {batch}]" if batch else ""
    rmsg = " + 汇总xgb/catboost研报" if gen_reports else ""
    return jsonify({"ok": True, "message": f"已通知 PC: 一键全跑6个模型 训练+回测+出清单{bmsg}{rmsg} (较慢, 约30-60分钟)",
                    "request_id": queued["request_id"]})


@app.route("/api/rdagent/buylists")
def api_rdagent_buylists():
    """模型实验室: 列出已生成的各模型买入清单 (供并排对比)."""
    data = _read_json(PREDICT_JSON.parent / "buylists_index.json")
    if not data:
        return jsonify({"buylists": []})
    return jsonify(data)


@app.route("/api/rdagent/buylist")
def api_rdagent_buylist():
    """读取某个 tag 的买入清单 (tag = <batch>__<model>)."""
    import re as _re
    tag = (request.args.get("tag", "") or "").strip()
    if not tag or not _re.fullmatch(r"[A-Za-z0-9_]+", tag):
        return jsonify({"error": "bad tag"}), 400
    data = _read_json(PREDICT_JSON.parent / f"buylist_{tag}.json")
    if data is None:
        return jsonify({"error": "not found"}), 404
    _attach_unlock_info_to_payload(data)
    return jsonify(data)


@app.route("/api/rdagent/model_eval", methods=["POST"])
def api_rdagent_model_eval():
    """网页按钮触发: 在指定批次上训练某模型 + 回测, 结果回写 model_results.json. PC 执行.
    model=lgb|xgb|catboost|ols|ridge|lasso ; batch=因子批次标签 (空=默认)."""
    model = (request.args.get("model", "lgb") or "lgb").strip().lower()
    allowed = {"lgb", "xgb", "catboost", "ols", "ridge", "lasso"}
    if model not in allowed:
        return jsonify({"ok": False, "message": f"不支持的模型 {model}"})
    batch = (request.args.get("batch", "") or "").strip()
    if not _valid_job_label(batch):
        return jsonify({"ok": False, "message": "批次标签包含非法字符"}), 400
    try:
        queued = _queue_rdagent_request(
            {"model_eval": True, "model": model, "batch": batch}
        )
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"}), 503
    if queued is None:
        return jsonify({"ok": False, "message": "已有 RD-Agent 任务在排队/处理中"}), 409
    bmsg = f" [批次 {batch}]" if batch else ""
    return jsonify({"ok": True, "message": f"已通知 PC: 训练+回测 {model.upper()}{bmsg} (约几分钟)",
                    "request_id": queued["request_id"]})


@app.route("/strategy")
def strategy_page():
    return render_template("strategy.html")


@app.route("/api/strategy/result")
def api_strategy_result():
    """单票·周频策略回测结果 (PC 端 backtest_top1_weekly.py 写). 附 RD-Agent 状态供页面轮询."""
    data = _read_json(STRATEGY_RESULT) or {}
    if isinstance(data, dict):
        cur = data.get("current") or {}
        if isinstance(cur.get("basket"), list):
            _attach_unlock_info(cur["basket"])
        trade = data.get("trade") or {}
        if isinstance(trade.get("items"), list):
            _attach_unlock_info(trade["items"])
    data["rd_pending"] = RDAGENT_REQUEST.exists()
    data["rd_status"] = _read_json(RDAGENT_STATUS)
    return jsonify(data)


@app.route("/api/strategy/request", methods=["POST"])
def api_strategy_request():
    """网页按钮触发: 单票·周频策略回测. model=auto 按本批次 rank_ic 自动选最优."""
    model = (request.args.get("model", "lgb") or "lgb").strip().lower()
    allowed = {"lgb", "xgb", "catboost", "ols", "ridge", "lasso", "auto", "wf"}
    if model not in allowed:
        return jsonify({"ok": False, "message": f"不支持的模型 {model}"})
    batch = (request.args.get("batch", "") or "").strip()
    if not _valid_job_label(batch):
        return jsonify({"ok": False, "message": "批次标签包含非法字符"}), 400
    try:
        hold = max(1, min(int(request.args.get("hold_days", "5")), 60))
        topn = max(1, min(int(request.args.get("topn", "1")), 20))
        cost = max(0.0, min(float(request.args.get("rt_cost", "0.002")), 0.05))
    except ValueError:
        return jsonify({"ok": False, "message": "参数格式错误"})
    try:
        queued = _queue_rdagent_request(
            {"strategy_bt": True, "model": model, "batch": batch,
             "hold_days": hold, "topn": topn, "rt_cost": cost}
        )
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"}), 503
    if queued is None:
        return jsonify({"ok": False, "message": "已有 RD-Agent 任务在排队/处理中"}), 409
    bmsg = f" [批次 {batch}]" if batch else ""
    return jsonify({"ok": True, "message": f"已通知 PC: 单票策略回测 {model.upper()} top{topn}/{hold}日换仓{bmsg} (约几分钟)",
                    "request_id": queued["request_id"]})


@app.route("/advisor")
def advisor_page():
    return render_template("advisor.html")


@app.route("/api/advisor/result")
def api_advisor_result():
    """策略顾问: regime择时 当前推荐篮子+历史战绩 (PC regime_advisor.py 写). 附 RD-Agent 状态供轮询."""
    data = _read_json(REGIME_ADVISOR) or {}
    data["rd_pending"] = RDAGENT_REQUEST.exists()
    data["rd_status"] = _read_json(RDAGENT_STATUS)
    return jsonify(data)


@app.route("/api/advisor/request", methods=["POST"])
def api_advisor_request():
    """网页按钮触发: 刷新策略顾问 (PC 拉最新数据重算当前 regime + 推荐篮子)."""
    try:
        queued = _queue_rdagent_request({"regime_adv": True})
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"}), 503
    if queued is None:
        return jsonify({"ok": False, "message": "已有 RD-Agent 任务在排队/处理中"}), 409
    return jsonify({"ok": True, "message": "已通知 PC: 刷新策略顾问 (拉最新数据+重算, 约1分钟)",
                    "request_id": queued["request_id"]})


@app.route("/advisor-pro")
def advisor_pro_page():
    return render_template("advisor_pro.html")


@app.route("/advisor-pro/backtest")
def advisor_pro_backtest_page():
    return render_template("advisor_pro_backtest.html")


@app.route("/advisor-pro-plus")
def advisor_pro_plus_page():
    return render_template("advisor_pro_plus.html")


def _safe_advisor_pro_backtest_scenario(key: str, label: str, capital_label: str):
    payload = _read_json(Path(ADVISOR_PRO_BACKTEST_FILES[key]))
    if not isinstance(payload, dict):
        return None

    metric_keys = (
        "n", "total_return", "annualized_return", "sharpe", "max_drawdown",
        "annualized_volatility", "calmar", "rolling_252d_sharpe_p10",
        "rolling_252d_return_p10", "annual_returns",
    )

    def safe_metrics(raw):
        if not isinstance(raw, dict):
            return {}
        return {name: raw.get(name) for name in metric_keys if name in raw}

    long_only = ((payload.get("staggered") or {}).get("long_only")) or {}
    double_cost = (
        (((payload.get("double_cost") or {}).get("staggered") or {}).get("long_only"))
        or {}
    )
    period_names = (
        "full", "development_2017_2021", "validation_2022_2024", "recent_2025_plus",
    )
    spec = payload.get("portfolio_spec") or {}
    execution_parameters = payload.get("execution_parameters") or {}
    validation_distribution = (
        ((((payload.get("offset_distribution") or {}).get("long_only") or {}).get(
            "validation_2022_2024"
        )))
        or {}
    )
    execution_quality = payload.get("execution_quality") or {}
    execution_aggregate = None
    if execution_quality.get("available") and isinstance(execution_quality.get("aggregate"), dict):
        raw_execution = execution_quality["aggregate"]
        execution_aggregate = {
            "attempts": raw_execution.get("attempts"),
            "trades": raw_execution.get("trades"),
            "no_fill_count": raw_execution.get("no_fill_count"),
            "no_fill_rate": raw_execution.get("no_fill_rate"),
            "partial_count": raw_execution.get("partial_count"),
            "partial_fill_rate": raw_execution.get("partial_rate"),
            "incomplete_count": raw_execution.get("incomplete_count"),
            "incomplete_fill_rate": raw_execution.get("incomplete_rate"),
            "all_liquidated": bool(execution_quality.get("all_liquidated")),
        }
    return {
        "key": key,
        "label": label,
        "capital_label": capital_label,
        "generated_at": payload.get("generated_at"),
        "portfolio_spec": {
            name: spec.get(name)
            for name in (
                "portfolio_topn", "frequency_days", "frequency_step",
                "max_replacements", "rebalance_mode", "account",
            )
        },
        "execution_parameters": {
            name: execution_parameters.get(name)
            for name in (
                "commission", "max_volume_participation", "impact_cost",
                "risk_degree", "retry_days",
            )
        },
        "offset_count": (payload.get("offsets") or {}).get("count"),
        "periods": {name: safe_metrics(long_only.get(name)) for name in period_names},
        "double_cost_periods": {
            name: safe_metrics(double_cost.get(name)) for name in period_names
        },
        "turnover": {
            "annualized_one_way_median": (payload.get("turnover") or {}).get("median")
        },
        "validation_offset_distribution": {
            name: validation_distribution.get(name)
            for name in ("annualized_return", "sharpe", "max_drawdown")
            if name in validation_distribution
        },
        "execution_quality": execution_aggregate,
    }


@app.route("/api/advisor-pro/backtest/summary")
def api_advisor_pro_backtest_summary():
    definitions = (
        ("standard", "Top8 标准容量", "总资金1亿元"),
        ("stress", "Top8 严格容量", "总资金1亿元"),
        ("top20", "Top20 低换手", "总资金3000万元"),
    )
    scenarios = []
    for key, label, capital_label in definitions:
        scenario = _safe_advisor_pro_backtest_scenario(key, label, capital_label)
        if scenario is not None:
            scenarios.append(scenario)
    if not scenarios:
        return jsonify({"ok": False, "message": "回测汇总文件尚未部署", "scenarios": []}), 404
    return jsonify({
        "ok": True,
        "selection": {
            "tested_configurations": 142,
            "recommended": "Top8 / 15个交易日 / 最多替换2只 / 3组错峰",
            "evaluation_period": "2022-01-01 至 2024-12-31",
            "observation_period": "2025-01-01 起",
            "data_end": "2026-05-18",
        },
        "scenarios": scenarios,
    })


@app.route("/api/advisor-pro/backtest/chart/<name>")
def api_advisor_pro_backtest_chart(name: str):
    filename = {
        "overview": "advisor_pro_backtest_overview.png",
        "capacity": "advisor_pro_capacity_stress.png",
    }.get(name)
    if filename is None:
        return jsonify({"ok": False, "message": "图表不存在"}), 404
    path = Path(ADVISOR_PRO_BACKTEST_CHART_DIR) / filename
    if not path.is_file():
        return jsonify({"ok": False, "message": "图表文件尚未部署"}), 404
    return send_file(path, mimetype="image/png", conditional=True, max_age=0)


def _snapshot_trade_basket(data):
    """把当前Pro篮子按signature去重存 trade_history.json(只在篮子真变时记一条), 供下单页看'到底变没变/哪天换股'。"""
    try:
        items = (data.get("trade") or {}).get("items") or []
        if not items:
            return
        cur = data.get("current") or {}
        as_of = cur.get("as_of") or data.get("updated_at") or ""
        regime = cur.get("regime_label") or cur.get("regime") or ""

        def codes(act):
            return sorted(i.get("code", "") for i in items if i.get("action") == act)
        buy, sell, hold = codes("买入"), codes("卖出"), codes("持有")
        sig = "|".join(["B"] + buy + ["S"] + sell + ["H"] + hold)
        hp = PREDICT_JSON.parent / "trade_history.json"
        with _trade_history_lock:
            hist = _read_json(hp)
            if not isinstance(hist, list):
                hist = []
            if hist and hist[-1].get("sig") == sig:
                return   # 篮子没变, 不重复记
            hist.append({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "as_of": as_of, "regime": regime,
                         "n_buy": len(buy), "n_sell": len(sell), "n_hold": len(hold),
                         "buy": buy, "sell": sell, "hold": hold, "sig": sig,
                         "names": {i.get("code", ""): i.get("name", "") for i in items}})
            hp.parent.mkdir(parents=True, exist_ok=True)
            tmp = hp.with_name(f".{hp.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                tmp.write_text(json.dumps(hist[-100:], ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, hp)
            finally:
                tmp.unlink(missing_ok=True)
    except Exception as exc:
        log.warning("trade basket snapshot failed: %s", exc)


@app.route("/api/advisor-pro/result")
def api_advisor_pro_result():
    """策略顾问Pro: 增强版(regime+正交选股) 当前推荐+战绩 (PC regime_advisor_pro.py 写)."""
    data = _read_json(REGIME_ADVISOR_PRO) or {}
    if isinstance(data, dict):
        cur = data.get("current") or {}
        if isinstance(cur.get("basket"), list):
            _attach_unlock_info(cur["basket"])
        trade = data.get("trade") or {}
        if isinstance(trade.get("items"), list):
            _attach_unlock_info(trade["items"])
    _snapshot_trade_basket(data)   # 篮子变化时留痕
    data["rd_pending"] = RDAGENT_REQUEST.exists()
    data["rd_status"] = _read_json(RDAGENT_STATUS)
    return jsonify(data)


@app.route("/api/advisor-pro-plus/result")
def api_advisor_pro_plus_result():
    """顾问Pro+滚动业绩: Pro篮子叠加滚动业绩公告信号，不改原始Pro结果。"""
    try:
        from scripts.build_advisor_pro_plus import build_report
        data = build_report(Path(STOCK_META_DB).parent, PREDICT_JSON.parent)
        for key in ("enhanced_buy", "enhanced_hold", "event_candidates", "conflicts", "base_buy", "base_hold", "base_sell"):
            if isinstance(data.get(key), list):
                _attach_unlock_info(data[key])
        return jsonify(data)
    except Exception as e:
        log.exception("advisor pro plus failed")
        return jsonify({"message": f"顾问Pro+计算失败: {e}", "summary": {}}), 500


@app.route("/api/trade/history")
def api_trade_history():
    """下单页历史: 历次Pro篮子快照(去重), 带 vs 上次的 新增/移除。"""
    hist = _read_json(PREDICT_JSON.parent / "trade_history.json")
    if not isinstance(hist, list):
        hist = []
    out = []
    for i, h in enumerate(hist):
        prev = hist[i - 1] if i > 0 else None
        now = set(h.get("buy", []) + h.get("hold", []))
        was = set((prev.get("buy", []) + prev.get("hold", [])) if prev else [])
        out.append({**h, "added": sorted(now - was) if prev else [],
                    "removed": sorted(was - now) if prev else []})
    out.reverse()
    return jsonify({"history": out, "n": len(out)})


@app.route("/api/advisor-pro/request", methods=["POST"])
def api_advisor_pro_request():
    """网页按钮触发: 刷新策略顾问Pro (PC 重算增强版 regime + 正交选股篮子)."""
    try:
        queued = _queue_rdagent_request({"regime_adv_pro": True})
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"}), 503
    if queued is None:
        return jsonify({"ok": False, "message": "已有 RD-Agent 任务在排队/处理中"}), 409
    return jsonify({"ok": True, "message": "已通知 PC: 刷新策略顾问Pro (重算增强版, 约1-2分钟)",
                    "request_id": queued["request_id"]})


@app.route("/rolling-earnings")
def rolling_earnings_page():
    return render_template("rolling_earnings.html")


@app.route("/api/rolling_earnings")
def api_rolling_earnings():
    payload = _read_json(PREDICT_JSON.parent / "rolling_earnings.json")
    if payload is None:
        try:
            from scripts.build_rolling_earnings import build_report
            payload = build_report(Path(STOCK_META_DB).parent, PREDICT_JSON.parent)
        except Exception as e:
            log.exception("rolling earnings build failed")
            return jsonify({"items": [], "n": 0, "message": f"滚动业绩调仓计算失败: {e}"}), 500
    data = payload or {"rolling": {"items": []}, "n": 0}
    items = ((data.get("rolling") or {}).get("items") or [])
    _attach_unlock_info(items)
    return jsonify(data)


@app.route("/api/rolling_earnings/rebuild", methods=["POST"])
def api_rolling_earnings_rebuild():
    try:
        from scripts.build_rolling_earnings import build_report
        payload = build_report(Path(STOCK_META_DB).parent, PREDICT_JSON.parent)
        items = ((payload.get("rolling") or {}).get("items") or [])
        _attach_unlock_info(items)
        return jsonify({"ok": True, **payload})
    except Exception as e:
        log.exception("rolling earnings rebuild failed")
        return jsonify({"ok": False, "message": f"滚动业绩调仓重算失败: {e}"}), 500


@app.route("/api/rolling_earnings/backtest")
def api_rolling_earnings_backtest():
    def read_data_json(name: str):
        primary = PREDICT_JSON.parent / name
        if primary.exists():
            return _read_json(primary)
        return _read_json(Path(__file__).parent / "data" / name)

    path = PREDICT_JSON.parent / "rolling_earnings_backtest_top50.json"
    if not path.exists():
        path = Path(__file__).parent / "data" / "rolling_earnings_backtest_top50.json"
    data = _read_json(path)
    if data is None:
        return jsonify({"message": "尚未运行历史回测：python scripts/backtest_rolling_earnings.py --topn 50"}), 404
    backfill = read_data_json("cninfo_earnings_event_backfill_status.json") or {}
    auto = read_data_json("earnings_event_times_auto.json") or {}
    def status_param(name: str):
        value = backfill.get(name)
        return auto.get(name) if value is None else value

    data["event_backfill_status"] = {
        "updated": backfill.get("updated") or "",
        "n_done_tasks": backfill.get("n_done_tasks"),
        "n_items": backfill.get("n_items"),
        "aborted": bool(backfill.get("aborted")),
        "errors_count": len(backfill.get("errors") or []),
        "workers": status_param("workers"),
        "limit": status_param("limit"),
        "sleep": status_param("sleep"),
        "max_403": status_param("max_403"),
    }
    data["event_backfill_auto"] = {
        "last_run": auto.get("last_run") or "",
        "reason": auto.get("reason") or "",
        "added": auto.get("added"),
        "aborted": bool(auto.get("aborted")),
        "workers": auto.get("workers"),
        "limit": auto.get("limit"),
        "sleep": auto.get("sleep"),
        "max_403": auto.get("max_403"),
    }
    return jsonify(data)


EARNINGS_ENTRY_LAG_SPECS = [
    {
        "key": "g20",
        "label": "扣非增速 >20%",
        "min_growth": 20.0,
        "filename": "rolling_earnings_interim_entry_lag_top50.json",
    },
    {
        "key": "g50",
        "label": "扣非增速 >50%",
        "min_growth": 50.0,
        "filename": "rolling_earnings_interim_entry_lag_g50_top50.json",
    },
    {
        "key": "g100",
        "label": "扣非增速 >100%",
        "min_growth": 100.0,
        "filename": "rolling_earnings_interim_entry_lag_g100_top50.json",
    },
]


def _earnings_entry_lag_path(filename: str) -> Path:
    primary = PREDICT_JSON.parent / filename
    if primary.exists():
        return primary
    return Path(__file__).parent / "data" / filename


def _earnings_entry_lag_best_by_horizon(entry_lag_analysis: dict) -> dict:
    best: dict[str, dict] = {}
    for lag, lag_data in (entry_lag_analysis or {}).items():
        summary = (lag_data or {}).get("summary") or {}
        for horizon, stats in summary.items():
            if not isinstance(stats, dict):
                continue
            mean_pct = stats.get("mean_pct")
            try:
                score = float(mean_pct)
            except (TypeError, ValueError):
                continue
            old = best.get(str(horizon))
            if old is None or score > float(old.get("mean_pct")):
                best[str(horizon)] = {
                    "lag": str(lag),
                    "mean_pct": score,
                    "win_rate_pct": stats.get("win_rate_pct"),
                    "n": stats.get("n"),
                }
    return best


def _load_earnings_entry_lag_payload() -> dict:
    variants = []
    updated_values = []
    for spec in EARNINGS_ENTRY_LAG_SPECS:
        path = _earnings_entry_lag_path(spec["filename"])
        data = _read_json(path)
        ok = isinstance(data, dict)
        if ok and data.get("updated"):
            updated_values.append(str(data.get("updated")))
        entry_lag_analysis = (data or {}).get("entry_lag_analysis") if ok else {}
        variants.append({
            "key": spec["key"],
            "label": spec["label"],
            "min_growth": spec["min_growth"],
            "filename": spec["filename"],
            "ok": ok,
            "updated": (data or {}).get("updated") if ok else "",
            "method": (data or {}).get("method") if ok else "",
            "params": (data or {}).get("params") if ok else {},
            "n_source_events": (data or {}).get("n_source_events") if ok else 0,
            "n_codes": (data or {}).get("n_codes") if ok else 0,
            "date_range": (data or {}).get("date_range") if ok else {},
            "entry_lag_analysis": entry_lag_analysis or {},
            "best_by_horizon": _earnings_entry_lag_best_by_horizon(entry_lag_analysis or {}),
            "message": "" if ok else f"缺少结果文件: {spec['filename']}",
        })
    return {
        "ok": any(v["ok"] for v in variants),
        "updated": max(updated_values) if updated_values else "",
        "variants": variants,
    }


def _run_earnings_entry_lag_backtest(min_growth: float, out_path: Path) -> dict:
    script = Path(__file__).parent / "scripts" / "backtest_rolling_earnings.py"
    cmd = [
        sys.executable,
        str(script),
        "--entry-lag-only",
        "--period-suffix", "0630",
        "--min-growth", str(float(min_growth)).rstrip("0").rstrip("."),
        "--entry-lags", "1,2,3,4,5",
        "--topn", "50",
        "--db", str(FINANCIALS_DB),
        "--parquet-dir", str(PARQUET_DIR),
        "--out", str(out_path),
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        cmd,
        cwd=str(Path(__file__).parent),
        capture_output=True,
        text=True,
        timeout=900,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"exit {proc.returncode}")[-2000:])
    return {"returncode": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]}


@app.route("/earnings-entry-lag")
def earnings_entry_lag_page():
    return render_template("earnings_entry_lag.html")


@app.route("/api/earnings_entry_lag")
def api_earnings_entry_lag():
    return jsonify(_load_earnings_entry_lag_payload())


@app.route("/api/earnings_entry_lag/rebuild", methods=["POST"])
def api_earnings_entry_lag_rebuild():
    runs = []
    try:
        for spec in EARNINGS_ENTRY_LAG_SPECS:
            out_path = PREDICT_JSON.parent / spec["filename"]
            runs.append({
                "key": spec["key"],
                "min_growth": spec["min_growth"],
                "filename": spec["filename"],
                "result": _run_earnings_entry_lag_backtest(spec["min_growth"], out_path),
            })
        payload = _load_earnings_entry_lag_payload()
        payload["ok"] = True
        payload["runs"] = runs
        return jsonify(payload)
    except Exception as e:
        log.exception("earnings entry lag rebuild failed")
        return jsonify({"ok": False, "message": f"中报入场日回测重算失败: {e}", "runs": runs}), 500


@app.route("/research")
def research_page():
    """研究台: 因子/策略调查结论 (错峰调仓证伪、小盘PEAD、中证1000等), 诚实记录正负结果。"""
    return render_template("research.html")


@app.route("/api/research")
def api_research():
    data = _read_json(RESEARCH_LOG)
    if data is None:
        return jsonify({"updated": "", "investigations": [],
                        "message": "尚无研究记录; PC 端运行 export_research.py 生成 research_log.json"})
    return jsonify(data)


@app.route("/runup")
def runup_page():
    """业绩预告抢跑(第二sleeve)每日清单: 预增股 正式报告前~6交易日建仓、前1日平。"""
    return render_template("runup.html")


@app.route("/earnings-commentary")
def earnings_commentary_page():
    """中报业绩预告点评：公告事实、单季动能、经营驱动与估值上下文。"""
    return render_template("earnings_commentary.html")


@app.route("/late-disclosure")
def late_disclosure_page():
    """年报晚披露(小/中盘)季节性信号: 年报季买晚披露的小/中盘股持~10交易日。验证 T+10超额+1.69%/IC0.073/四年全正。"""
    return render_template("late_disclosure.html")


@app.route("/api/late_disclosure")
def api_late_disclosure():
    data = _read_json(LATE_DISCLOSURE_JSON)
    if data is None:
        return jsonify({"updated": "", "in_season": False, "items": [],
                        "message": "尚无数据; PC 端运行 export_late_disclosure.py 生成 late_disclosure.json"})
    return jsonify(data)


def _runup_norm_code(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:6]


def _runup_norm_yyyymmdd(value):
    text = str(value or "").strip()[:10].replace("/", "-")
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return ""


def _runup_fmt_date(yyyymmdd):
    text = _runup_norm_yyyymmdd(yyyymmdd)
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 else ""


def _runup_trade_calendar():
    cal = []
    try:
        cal = [_runup_norm_yyyymmdd(x) for x in _read_calendar()]
        cal = [x for x in cal if len(x) == 8]
    except Exception:
        cal = []
    if cal:
        return sorted(set(cal))
    try:
        files = sorted(PARQUET_DIR.glob("*.parquet"))
        return [p.stem for p in files if len(p.stem) == 8 and p.stem.isdigit()]
    except Exception:
        return []


def _runup_next_trade_day(day, cal):
    day = _runup_norm_yyyymmdd(day)
    for d in cal:
        if d > day:
            return d
    try:
        cur = datetime.strptime(day, "%Y%m%d").date() + timedelta(days=1)
        while cur.weekday() >= 5:
            cur += timedelta(days=1)
        return cur.strftime("%Y%m%d")
    except Exception:
        return ""
    return ""


def _runup_effective_ann_date(cninfo_dt, cal):
    if cninfo_dt is None:
        return ""
    day = cninfo_dt.strftime("%Y%m%d")
    if day in cal:
        if cninfo_dt.time() <= datetime.strptime("15:00:00", "%H:%M:%S").time():
            return day
        return _runup_next_trade_day(day, cal)
    for d in cal:
        if d > day:
            return d
    return _runup_next_trade_day(day, cal)


def _runup_load_cninfo_earnings_times():
    data = _read_json(PREDICT_JSON.parent / "cninfo_earnings_announcements.json")
    if data is None:
        data = _read_json(Path(__file__).parent / "data" / "cninfo_earnings_announcements.json")
    out = {}
    for row in (data or {}).get("items") or []:
        code = _runup_norm_code(row.get("code") or row.get("symbol"))
        ann = _runup_norm_yyyymmdd(row.get("ann_date") or row.get("date"))
        if not code or not ann:
            continue
        dt = None
        raw_dt = str(row.get("ann_datetime") or "").strip()
        if raw_dt:
            try:
                dt = datetime.fromisoformat(raw_dt.replace("/", "-"))
            except Exception:
                dt = None
        if dt is not None and dt.time() == datetime.strptime("00:00:00", "%H:%M:%S").time():
            dt = None
        if dt is None:
            continue
        old = out.get((code, ann))
        if old is None or dt < old.get("dt"):
            out[(code, ann)] = {
                "dt": dt,
                "ann_date": ann,
                "ann_datetime": raw_dt,
                "title": row.get("title") or "",
                "url": row.get("url") or "",
                "type": row.get("type") or "",
            }
    return out


def _runup_lookup_cninfo_time(cache, code, ann):
    code = _runup_norm_code(code)
    ann = _runup_norm_yyyymmdd(ann)
    if not code or not ann:
        return None
    if (code, ann) in cache:
        return cache[(code, ann)]
    try:
        base = datetime.strptime(ann, "%Y%m%d")
    except Exception:
        return None
    candidates = []
    for offset in (-2, -1, 1, 2):
        near = (base + timedelta(days=offset)).strftime("%Y%m%d")
        row = cache.get((code, near))
        if row:
            candidates.append((abs(offset), row))
    return sorted(candidates, key=lambda x: x[0])[0][1] if candidates else None


def _attach_cninfo_earnings_time_to_runup(data):
    cache = _runup_load_cninfo_earnings_times()
    if not cache:
        return
    cal = _runup_trade_calendar()

    def enrich(row):
        if not isinstance(row, dict):
            return
        original = row.get("ann_date") or row.get("date")
        ann = _runup_norm_yyyymmdd(original)
        info = _runup_lookup_cninfo_time(cache, row.get("code") or row.get("ts_code") or row.get("symbol"), ann)
        row["raw_ann_date"] = row.get("ann_date") or row.get("date") or ""
        if not info:
            row.setdefault("cninfo_ann_datetime", "")
            row.setdefault("cninfo_effective_ann_date", row.get("ann_date") or row.get("date") or "")
            row.setdefault("cninfo_ann_date_match", "missing")
            return
        eff = _runup_effective_ann_date(info.get("dt"), cal)
        cninfo_ann = info.get("ann_date") or ""
        row["cninfo_ann_datetime"] = info.get("ann_datetime") or ""
        row["cninfo_ann_date"] = _runup_fmt_date(cninfo_ann)
        row["cninfo_effective_ann_date"] = _runup_fmt_date(eff) or row["raw_ann_date"]
        row["cninfo_ann_date_match"] = "same" if cninfo_ann == ann else "nearby"
        row["cninfo_title"] = info.get("title") or ""
        row["cninfo_url"] = info.get("url") or ""

    for key in ("buy", "sell", "watch", "buy_post", "items", "holdings", "repo_focus"):
        for row in data.get(key) or []:
            enrich(row)
    events = data.get("events") or {}
    for key in ("forecast", "express", "report"):
        for row in events.get(key) or []:
            enrich(row)


@app.route("/api/runup")
def api_runup():
    data = _read_json(RUNUP_JSON)
    if data is not None:
        _attach_cninfo_earnings_time_to_runup(data)
        _attach_unlock_info_to_payload(data, keys=("buy", "watch", "buy_post", "holdings", "items"))
        _attach_unlock_info_to_event_groups(data)
        _attach_repo_resonance_to_runup(data)
        data["unlock_focus"] = _build_unlock_focus_items()
    if data is None:
        return jsonify({"updated": "", "buy": [], "sell": [], "watch": [],
                        "unlock_focus": _build_unlock_focus_items(),
                        "message": "尚无抢跑清单; PC 端运行 export_runup.py 生成 runup.json"})
    return jsonify(data)


@app.route("/api/qlib_coverage")
def api_qlib_coverage():
    """行情数据覆盖自检状态 (PC verify_and_backfill_qlib.py 写): 各股bin是否滞后于最新parquet, 滞后则自动补全."""
    return jsonify(_read_json(PREDICT_JSON.parent / "qlib_coverage.json")
                   or {"ok": None, "message": "尚无数据自检; 点『🔄补全数据』或等每日盘后自动跑"})


@app.route("/api/forecast_browse")
def api_forecast_browse():
    """全市场业绩预告浏览(沪深300/中证500/中证1000/全市场分区). PC export_forecast_browse.py 生成. 仅浏览, 非抢跑信号."""
    data = _read_json(PREDICT_JSON.parent / "forecast_browse.json")
    if data is None:
        return jsonify({"updated": "", "counts": {}, "items": [],
                        "unlock_focus": _build_unlock_focus_items(),
                        "message": "尚无预告浏览数据; PC 端运行 export_forecast_browse.py 生成"})
    _attach_cninfo_earnings_time_to_runup(data)
    _attach_unlock_info_to_payload(data, keys=("items",))
    _attach_repo_resonance_to_runup(data)
    data["unlock_focus"] = _build_unlock_focus_items()
    return jsonify(data)


# ---------- 中报业绩预告点评 ----------

_EARNINGS_COMMENTARY_POSITIVE_TYPES = frozenset({"预增", "略增", "扭亏", "续盈", "减亏"})
_EARNINGS_COMMENTARY_PERIOD_SUFFIX = "0630"


def _earnings_commentary_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _earnings_commentary_first_value(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _earnings_commentary_text(value, limit=500):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _earnings_commentary_pinyin_codes(query, limit=200):
    """Return every listed-stock code whose pinyin initials start with query."""
    query = str(query or "").strip().lower()
    if not re.fullmatch(r"[a-z]+", query):
        return set()
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like_query = f"{escaped}%"
    codes = set()
    try:
        with closing(_open_sqlite_readonly(STOCK_META_DB)) as conn:
            rows = conn.execute(
                "SELECT code,ts_code FROM stock_meta "
                "WHERE list_status='L' AND LOWER(pinyin_initials) LIKE ? ESCAPE '\\' "
                "ORDER BY CASE WHEN LOWER(pinyin_initials)=? THEN 0 ELSE 1 END, code LIMIT ?",
                (like_query, query, max(1, min(int(limit), 500))),
            ).fetchall()
        for row in rows:
            code = _runup_norm_code(row[1] or row[0])
            if code:
                codes.add(code)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        log.debug("earnings commentary pinyin search unavailable: %s", exc)
    return codes


def _earnings_commentary_period(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _earnings_commentary_period_label(period):
    period = _earnings_commentary_period(period)
    if len(period) != 8:
        return "半年度"
    if period.endswith(_EARNINGS_COMMENTARY_PERIOD_SUFFIX):
        return f"{period[:4]}年半年度"
    return period


def _earnings_commentary_range(low, high, scale=1.0, digits=2):
    low = _earnings_commentary_number(low)
    high = _earnings_commentary_number(high)
    low = None if low is None else low * scale
    high = None if high is None else high * scale
    if low is not None and high is not None and low > high:
        low, high = high, low
    values = [value for value in (low, high) if value is not None]
    midpoint = (sum(values) / len(values)) if values else None
    width_pct = None
    if low is not None and high is not None and midpoint not in (None, 0):
        width_pct = abs(high - low) / abs(midpoint) * 100
    clean = lambda value: None if value is None else round(value, digits)
    return {
        "min": clean(low),
        "max": clean(high),
        "mid": clean(midpoint),
        "width_pct": None if width_pct is None else round(width_pct, 2),
    }


def _earnings_commentary_decimal(value, digits=2):
    value = _earnings_commentary_number(value)
    if value is None:
        return ""
    text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".")


def _earnings_commentary_money_phrase(value_range):
    low = value_range.get("min")
    high = value_range.get("max")
    values = [abs(value) for value in (low, high) if value is not None]
    use_wan = bool(values) and max(values) < 0.1

    def one(value):
        if value is None:
            return ""
        if use_wan:
            return f"{_earnings_commentary_decimal(value * 10000, 1)}万元"
        digits = 3 if abs(value) < 1 else 2
        return f"{_earnings_commentary_decimal(value, digits)}亿元"

    if low is not None and high is not None:
        if low == high:
            return one(low)
        if use_wan:
            return f"{_earnings_commentary_decimal(low * 10000, 1)}—{_earnings_commentary_decimal(high * 10000, 1)}万元"
        digits = 3 if max(abs(low), abs(high)) < 1 else 2
        return f"{_earnings_commentary_decimal(low, digits)}—{_earnings_commentary_decimal(high, digits)}亿元"
    value = low if low is not None else high
    return f"约{one(value)}" if value is not None else "未披露"


def _earnings_commentary_pct_phrase(value_range):
    def one(value):
        if value is None:
            return ""
        prefix = "+" if value > 0 else ""
        return f"{prefix}{_earnings_commentary_decimal(value, 1)}%"

    low = value_range.get("min")
    high = value_range.get("max")
    if low is not None and high is not None:
        if low == high:
            return one(low)
        return f"{one(low)}—{one(high)}"
    value = low if low is not None else high
    return one(value) if value is not None else "未披露"


def _earnings_commentary_forecast_growth(value_range, current=None, event_type="", use_event_semantics=False):
    current = _earnings_commentary_number(current)
    event_type = str(event_type or "").strip()
    low = _earnings_commentary_number((value_range or {}).get("min"))
    high = _earnings_commentary_number((value_range or {}).get("max"))
    midpoint = _earnings_commentary_number((value_range or {}).get("mid"))
    yoy_crosses_zero = low is not None and high is not None and low < 0 < high
    if use_event_semantics:
        semantic = {
            "扭亏": ("扭亏", "cross_sign"),
            "首亏": ("转亏", "cross_sign"),
            "减亏": ("亏损收窄", "negative_base"),
        }.get(event_type)
        if semantic:
            return {"label": semantic[0], "basis": semantic[1], "comparable": False}
    if current is not None and current < 0:
        if midpoint is not None and midpoint > 0:
            label = f"亏损收窄（原始同比{_earnings_commentary_pct_phrase(value_range)}）"
        elif midpoint is not None and midpoint < 0:
            label = f"亏损扩大（原始同比{_earnings_commentary_pct_phrase(value_range)}）"
        else:
            label = "仍处亏损（基数不可比）"
        return {"label": label, "basis": "negative_current", "comparable": False}
    if yoy_crosses_zero:
        return {
            "label": "同比变动区间跨零（可能增长也可能下降）",
            "basis": "yoy_cross_zero",
            "comparable": False,
        }
    if midpoint is None:
        return {"label": "未披露", "basis": "missing", "comparable": False}
    if abs(midpoint) >= 500:
        return {
            "label": f"低基数高波动（原始同比{_earnings_commentary_pct_phrase(value_range)}）",
            "basis": "low_base_risk",
            "comparable": False,
        }
    return {
        "label": _earnings_commentary_pct_phrase(value_range),
        "basis": "rate_only",
        "comparable": True,
    }


def _earnings_commentary_growth_display(rate, current=None, prior=None):
    """Return a sign-aware label; avoid presenting cross-sign bases as ordinary YoY."""
    rate = _earnings_commentary_number(rate)
    current = _earnings_commentary_number(current)
    prior = _earnings_commentary_number(prior)

    if current is not None and prior is not None:
        if prior < 0 <= current:
            return {"value": None, "raw_value": rate, "label": "扭亏", "basis": "cross_sign"}
        if prior >= 0 > current:
            return {"value": None, "raw_value": rate, "label": "转亏", "basis": "cross_sign"}
        if prior == 0:
            return {"value": None, "raw_value": rate, "label": "低基数/不可比", "basis": "zero_base"}
        if prior < 0 and current < 0:
            label = "亏损收窄" if current > prior else "亏损扩大"
            return {"value": None, "raw_value": rate, "label": label, "basis": "negative_base"}

    if current is not None and current < 0 and prior is None:
        return {
            "value": None,
            "raw_value": None if rate is None else round(rate, 1),
            "label": "单季亏损（基数不可比）",
            "basis": "negative_current_unknown_base",
        }

    if rate is None:
        return {"value": None, "raw_value": None, "label": "未披露", "basis": "missing"}
    prefix = "+" if rate > 0 else ""
    label = f"{prefix}{_earnings_commentary_decimal(rate, 1)}%"
    basis = "comparable" if current is not None and prior is not None and prior > 0 else "rate_only"
    if abs(rate) >= 500:
        label = f"低基数高波动（原始同比{label}）"
        basis = "low_base_risk"
        return {"value": None, "raw_value": round(rate, 1), "label": label, "basis": basis}
    return {"value": round(rate, 1), "raw_value": round(rate, 1), "label": label, "basis": basis}


def _earnings_commentary_cninfo_payload():
    path = PREDICT_JSON.parent / "cninfo_earnings_announcements.json"
    data = _read_json(path)
    if not isinstance(data, dict):
        # Fail closed.  Falling back to a bundled, potentially old file can make
        # an obsolete announcement version look like an exact event timestamp.
        return {"updated": "", "items": [], "_source": "unavailable"}
    payload = dict(data)
    payload["_source"] = str(path)
    return payload


def _earnings_commentary_cninfo_index(data=None):
    data = data if isinstance(data, dict) else _earnings_commentary_cninfo_payload()
    index = defaultdict(list)
    for item in (data or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        code = _runup_norm_code(item.get("code") or item.get("symbol") or item.get("ts_code"))
        ann_date = _runup_norm_yyyymmdd(item.get("ann_date") or item.get("date"))
        if code and ann_date:
            index[(code, ann_date)].append(item)
    return index


def _earnings_commentary_cninfo_relevance(item):
    title = str(item.get("title") or "")
    item_type = str(item.get("type") or "")
    if "业绩预告" not in title and "业绩预告" not in item_type:
        return 0
    score = 6
    if any(token in title for token in ("修正", "更正", "修订", "更新后", "更新版", "补充")):
        score += 2
    if "半年度报告" in title and "业绩预告" not in title:
        score -= 4
    return score


def _earnings_commentary_cninfo_matches_period(item, row):
    title = str(item.get("title") or "")
    period = _earnings_commentary_period(row.get("period"))
    year = period[:4]
    title_years = set(re.findall(r"20\d{2}", title))
    if year and title_years and year not in title_years:
        return False
    if period.endswith(_EARNINGS_COMMENTARY_PERIOD_SUFFIX):
        half_year_tokens = ("半年度", "半年报", "上半年", "中期")
        conflicting_tokens = ("一季度", "第一季度", "三季度", "第三季度")
        if any(token in title for token in conflicting_tokens) and not any(token in title for token in half_year_tokens):
            return False
        if re.search(r"20\d{2}年度业绩预告", title) and not any(token in title for token in half_year_tokens):
            return False
    return True


def _earnings_commentary_next_trade_day(day, calendar):
    day = _runup_norm_yyyymmdd(day)
    for candidate in sorted(set(calendar or [])):
        normalized = _runup_norm_yyyymmdd(candidate)
        if normalized > day:
            return normalized
    return ""


def _earnings_commentary_announcement(row, cninfo_index=None, calendar=None):
    code = _runup_norm_code(row.get("code") or row.get("ts_code"))
    raw_ann = _runup_norm_yyyymmdd(row.get("ann_date") or row.get("raw_ann_date"))
    cninfo_index = cninfo_index if cninfo_index is not None else _earnings_commentary_cninfo_index()

    candidates = []
    if code and raw_ann:
        try:
            base = datetime.strptime(raw_ann, "%Y%m%d")
        except ValueError:
            base = None
        offsets = (0, -1, 1, -2, 2) if base is not None else (0,)
        for offset in offsets:
            day = raw_ann if base is None else (base + timedelta(days=offset)).strftime("%Y%m%d")
            for item in cninfo_index.get((code, day), ()):
                relevance = _earnings_commentary_cninfo_relevance(item)
                if relevance > 0 and _earnings_commentary_cninfo_matches_period(item, row):
                    candidates.append((abs(offset), -relevance, item))
    selected = {}
    if candidates:
        best_offset = min(value[0] for value in candidates)
        same_day = [value for value in candidates if value[0] == best_offset]
        best_relevance = min(value[1] for value in same_day)
        finalists = [value[2] for value in same_day if value[1] == best_relevance]

        # CNINFO announcement IDs are monotonic numeric IDs.  When every finalist
        # has one, it is the safest same-day version order and also lets a later
        # date-only correction keep conservative time semantics.  Older exports
        # without IDs fall back to the latest explicit timestamp; if any finalist
        # has no usable time, choose an unknown-time version rather than infer that
        # an earlier exact announcement was the final disclosure.
        numeric_ids = []
        for item in finalists:
            raw_id = str(item.get("announcement_id") or item.get("id") or "").strip()
            numeric_ids.append(int(raw_id) if raw_id.isdigit() else None)
        if finalists and all(value is not None for value in numeric_ids):
            selected = finalists[max(range(len(finalists)), key=lambda idx: numeric_ids[idx])]
        else:
            def version_datetime(item):
                raw_value = str(item.get("ann_datetime") or "").strip()
                try:
                    value = datetime.fromisoformat(raw_value.replace("/", "-").replace("Z", "+00:00"))
                except ValueError:
                    return None
                if value.time().replace(tzinfo=None) == datetime.strptime("00:00:00", "%H:%M:%S").time():
                    return None
                return value.replace(tzinfo=None)

            unknown_time = [item for item in finalists if version_datetime(item) is None]
            if unknown_time:
                selected = max(
                    unknown_time,
                    key=lambda item: str(item.get("announcement_id") or item.get("id") or ""),
                )
            else:
                selected = max(finalists, key=version_datetime)
    selected_date = _runup_norm_yyyymmdd(selected.get("ann_date") or selected.get("date")) or raw_ann
    raw_datetime = str(selected.get("ann_datetime") or "").strip()
    parsed = None
    if raw_datetime:
        try:
            parsed = datetime.fromisoformat(raw_datetime.replace("/", "-").replace("Z", "+00:00"))
        except ValueError:
            parsed = None

    explicit_precision = str(
        selected.get("time_precision")
        or selected.get("timestamp_precision")
        or selected.get("datetime_precision")
        or ""
    ).strip().lower()
    date_only_flags = {"date_only", "date", "unknown", "placeholder", "none"}
    is_midnight_placeholder = parsed is not None and parsed.time().replace(tzinfo=None) == datetime.strptime(
        "00:00:00", "%H:%M:%S"
    ).time()
    if not selected:
        precision = "missing"
    elif parsed is None or is_midnight_placeholder or explicit_precision in date_only_flags:
        precision = "date_only"
    else:
        precision = "exact"

    match = "missing"
    if selected:
        match = "same" if selected_date == raw_ann else "nearby"
    if match == "nearby":
        precision = "unverified"

    calendar = calendar if calendar is not None else _runup_trade_calendar()
    effective = ""
    rule = ""
    if selected_date:
        if precision == "exact" and parsed is not None:
            cutoff = datetime.strptime("09:15:00", "%H:%M:%S").time()
            naive_time = parsed.time().replace(tzinfo=None)
            if selected_date in calendar and naive_time < cutoff:
                effective = selected_date
                rule = "精确发布时间早于集合竞价，研究口径可从当日使用"
            else:
                effective = _earnings_commentary_next_trade_day(selected_date, calendar)
                rule = "盘中、收盘后或非交易日发布，研究口径从下一交易日使用"
        elif precision in {"date_only", "missing"}:
            effective = _earnings_commentary_next_trade_day(selected_date, calendar)
            rule = "仅有公告日期，具体时点未知，保守从下一交易日使用"
        else:
            rule = "仅相邻日期模糊匹配，保留公告链接供核验，不用于公告时点判断"
        if not effective and precision != "unverified":
            rule += "；交易日历尚未覆盖下一开市日，暂不猜测日期"
    return {
        "id": str(selected.get("announcement_id") or selected.get("id") or ""),
        "title": _earnings_commentary_text(selected.get("title"), 240),
        "url": str(selected.get("url") or "").strip(),
        "raw_date": _runup_fmt_date(raw_ann),
        "source_date": _runup_fmt_date(selected_date),
        "source_datetime": raw_datetime,
        "published_at": raw_datetime if precision == "exact" else "",
        "time_precision": precision,
        "date_match": match,
        "effective_trade_date": _runup_fmt_date(effective),
        "effective_rule": rule,
    }


def _earnings_commentary_source_quality(row):
    source = str(row.get("dedt_src") or "")
    has_structured_range = (
        _earnings_commentary_number(row.get("dedt_lo")) is not None
        and _earnings_commentary_number(row.get("dedt_hi")) is not None
    )
    if has_structured_range and ("精确" in source or "结构化" in source):
        return "structured"
    if _earnings_commentary_number(row.get("q2_dedt")) is not None or source:
        return "estimated"
    return "missing"


def _earnings_commentary_report_codes():
    codes = set()
    try:
        for path in PREDICT_JSON.parent.glob("report_*.json"):
            suffix = path.stem.removeprefix("report_")
            if len(suffix) == 6 and suffix.isdigit():
                codes.add(suffix)
    except Exception as exc:
        log.warning("earnings commentary report index failed: %s", exc)
    return codes


def _earnings_commentary_timestamp_health(source_payload, now=None):
    now = now or datetime.now()
    updated = str((source_payload or {}).get("updated") or "").strip()
    source = str((source_payload or {}).get("_source") or "")
    parsed = None
    if updated:
        try:
            parsed = datetime.fromisoformat(updated.replace("/", "-"))
        except ValueError:
            parsed = None
    if parsed is None:
        return {
            "updated": updated,
            "age_hours": None,
            "stale": True,
            "status": "missing_timestamp",
            "source": source,
        }
    compare_now = now
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    if compare_now.tzinfo is not None:
        compare_now = compare_now.replace(tzinfo=None)
    age_hours = (compare_now - parsed).total_seconds() / 3600
    if age_hours < -1:
        status = "future_timestamp"
        stale = True
    elif age_hours > 36:
        status = "stale"
        stale = True
    else:
        status = "fresh"
        stale = False
    return {
        "updated": updated,
        "age_hours": round(age_hours, 1),
        "stale": stale,
        "status": status,
        "source": source,
    }


def _earnings_commentary_load_rows():
    payload = _read_json(PREDICT_JSON.parent / "forecast_browse.json")
    source = "forecast_browse"
    message = ""
    if isinstance(payload, list):
        payload = {"updated": "", "periods": [], "items": payload}
        message = "全市场预告缓存缺少顶层元数据，已按列表兼容读取；更新时间不可核验。"
    elif not isinstance(payload, dict):
        rolling = _read_json(PREDICT_JSON.parent / "rolling_earnings.json") or {}
        if not isinstance(rolling, dict):
            rolling = {}
        rolling_section = rolling.get("rolling") or {}
        if not isinstance(rolling_section, dict):
            rolling_section = {}
        rolling_items = rolling_section.get("items") or []
        payload = {
            "updated": rolling.get("updated") or "",
            "periods": sorted({str(item.get("period") or "") for item in rolling_items if item.get("period")}),
            "items": rolling_items,
        }
        source = "rolling_earnings_fallback"
        message = "全市场预告缓存暂不可用，当前为滚动业绩候选回退数据，字段可能不完整。"

    cninfo_payload = _earnings_commentary_cninfo_payload()
    # A stale/missing/future-dated timestamp cache may omit a later correction.
    # Keep announcement links/times fail-closed until that cache is healthy.
    cninfo_items = cninfo_payload.get("items")
    cninfo_structure_ok = (
        isinstance(cninfo_items, list)
        and bool(cninfo_items)
        and all(isinstance(item, dict) for item in cninfo_items)
    )
    cninfo_index = (
        _earnings_commentary_cninfo_index(cninfo_payload)
        if cninfo_structure_ok and _earnings_commentary_timestamp_health(cninfo_payload).get("status") == "fresh"
        else defaultdict(list)
    )
    calendar = _runup_trade_calendar()
    report_codes = _earnings_commentary_report_codes()
    latest = {}
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            continue
        period = _earnings_commentary_period(raw.get("period") or raw.get("end_date"))
        if not period.endswith(_EARNINGS_COMMENTARY_PERIOD_SUFFIX):
            continue
        code = _runup_norm_code(raw.get("code") or raw.get("ts_code") or raw.get("symbol"))
        if not code:
            continue
        row = dict(raw)
        row["code"] = code
        row["ts_code"] = _code_to_ts(raw.get("ts_code") or code) or str(raw.get("ts_code") or code)
        row["period"] = period
        ann_key = _runup_norm_yyyymmdd(raw.get("ann_date") or raw.get("date"))
        row["ann_date"] = _runup_fmt_date(ann_key)
        row["type"] = _earnings_commentary_text(raw.get("type") or raw.get("source"), 20)
        row["pos"] = bool(raw.get("pos")) if raw.get("pos") is not None else row["type"] in _EARNINGS_COMMENTARY_POSITIVE_TYPES
        row["_announcement"] = _earnings_commentary_announcement(row, cninfo_index, calendar)
        row["_report_exists"] = code in report_codes
        rank = (ann_key, str(row["_announcement"].get("id") or ""))
        key = (code, period)
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, row)

    universe_rank = {"csi300": 0, "csi500": 1, "csi1000": 2, "other": 3}
    rows = [value[1] for value in latest.values()]
    rows.sort(
        key=lambda row: (
            row.get("ann_date") or "",
            -universe_rank.get(str(row.get("idx") or "other"), 9),
            bool(row.get("pos")),
            row.get("code") or "",
        ),
        reverse=True,
    )
    return payload, rows, source, message, cninfo_payload


def _earnings_commentary_data_health(payload, rows=None, cninfo_payload=None, now=None):
    """Assess both forecast facts and announcement-time metadata conservatively."""
    now = now or datetime.now()
    forecast = _earnings_commentary_timestamp_health(payload, now)
    cninfo = _earnings_commentary_timestamp_health(cninfo_payload, now) if cninfo_payload is not None else None
    row_count = len(rows) if rows is not None else None
    cninfo_items = (cninfo_payload or {}).get("items") if cninfo_payload is not None else None
    cninfo_schema_ok = isinstance(cninfo_items, list) and all(isinstance(item, dict) for item in cninfo_items)
    cninfo_item_count = len(cninfo_items) if isinstance(cninfo_items, list) else None
    if cninfo is not None:
        cninfo["item_count"] = cninfo_item_count
        cninfo["schema_ok"] = cninfo_schema_ok
    reasons = []
    if row_count == 0:
        reasons.append("半年度业绩预告源数据为空")
    if cninfo_payload is not None:
        if not cninfo_schema_ok:
            reasons.append("巨潮公告时点源数据结构异常")
        elif cninfo_item_count == 0:
            reasons.append("巨潮公告时点源数据为空")
    for label, value in (("业绩预告", forecast), ("巨潮公告时点", cninfo)):
        if value is None or not value.get("stale"):
            continue
        if value.get("status") == "future_timestamp":
            reasons.append(f"{label}更新时间异常地晚于当前时间")
        elif value.get("status") == "missing_timestamp":
            reasons.append(f"{label}缺少有效更新时间")
        else:
            reasons.append(f"{label}缓存超过36小时未更新")

    if row_count == 0:
        status = "empty"
    elif any(value and value.get("status") == "future_timestamp" for value in (forecast, cninfo)):
        status = "future_timestamp"
    elif cninfo_payload is not None and (not cninfo_schema_ok or cninfo_item_count == 0):
        status = "degraded"
    elif any(value and value.get("status") == "missing_timestamp" for value in (forecast, cninfo)):
        status = "missing_timestamp"
    elif any(value and value.get("status") == "stale" for value in (forecast, cninfo)):
        status = "stale"
    else:
        status = "fresh"
    return {
        "updated": forecast.get("updated") or "",
        "age_hours": forecast.get("age_hours"),
        "row_count": row_count,
        "stale": bool(reasons),
        "status": status,
        "message": "；".join(reasons),
        "components": {"forecast": forecast, "cninfo": cninfo},
    }


def _earnings_commentary_item_summary(row):
    parent_profit = _earnings_commentary_range(row.get("net_min"), row.get("net_max"), scale=1 / 10000, digits=4)
    parent_yoy = _earnings_commentary_range(row.get("p_chg_min"), row.get("p_chg_max"), digits=1)
    deduct_profit = _earnings_commentary_range(row.get("dedt_lo"), row.get("dedt_hi"), scale=1 / 1e8, digits=4)
    deduct_yoy = _earnings_commentary_number(row.get("dedt_h1_yoy"))
    if deduct_yoy is None:
        deduct_yoy = _earnings_commentary_number(row.get("dedt_yoy"))
    deduct_yoy_range = _earnings_commentary_range(deduct_yoy, deduct_yoy, digits=1)
    parent_growth = _earnings_commentary_forecast_growth(
        parent_yoy,
        parent_profit.get("mid"),
        row.get("type"),
        use_event_semantics=True,
    )
    deduct_growth = _earnings_commentary_forecast_growth(
        deduct_yoy_range,
        deduct_profit.get("mid"),
        row.get("type"),
        use_event_semantics=False,
    )
    if parent_profit.get("min") is not None and parent_profit.get("max") is not None and parent_profit["min"] < 0 < parent_profit["max"]:
        parent_growth = {"label": "利润区间跨盈亏平衡点", "basis": "profit_cross_zero", "comparable": False}
    if deduct_profit.get("min") is not None and deduct_profit.get("max") is not None and deduct_profit["min"] < 0 < deduct_profit["max"]:
        deduct_growth = {"label": "扣非区间跨盈亏平衡点", "basis": "profit_cross_zero", "comparable": False}
    q1_yoy = _earnings_commentary_number(row.get("q1_yoy"))
    q2_yoy = _earnings_commentary_number(row.get("q2_yoy"))
    q1_growth = _earnings_commentary_growth_display(
        q1_yoy,
        row.get("q1_dedt"),
        _earnings_commentary_first_value(row.get("q1_prior_dedt"), row.get("q1_dedt_prior")),
    )
    q2_growth = _earnings_commentary_growth_display(
        q2_yoy,
        row.get("q2_dedt"),
        _earnings_commentary_first_value(row.get("q2_prior_dedt"), row.get("q2_dedt_prior")),
    )
    acceleration = None
    if (
        q1_growth["basis"] == "comparable"
        and q2_growth["basis"] == "comparable"
        and q1_growth["value"] is not None
        and q2_growth["value"] is not None
    ):
        acceleration = q2_growth["value"] > q1_growth["value"]
    announcement = row.get("_announcement") or {}
    source_quality = _earnings_commentary_source_quality(row)
    universe = {
        "csi300": "沪深300",
        "csi500": "中证500",
        "csi1000": "中证1000",
        "other": "其他A股",
    }.get(str(row.get("idx") or "other"), str(row.get("idx") or "其他A股"))
    event_id = ":".join(
        part for part in (
            row.get("code") or "",
            row.get("period") or "",
            _runup_norm_yyyymmdd(row.get("ann_date")),
            str(announcement.get("id") or ""),
        ) if part
    )
    return {
        "event_id": event_id,
        "code": row.get("code") or "",
        "ts_code": row.get("ts_code") or "",
        "name": _earnings_commentary_text(row.get("name"), 80),
        "idx": row.get("idx") or "other",
        "universe": universe,
        "type": row.get("type") or "未分类",
        "positive": bool(row.get("pos")),
        "period": row.get("period") or "",
        "period_label": _earnings_commentary_period_label(row.get("period")),
        "ann_date": row.get("ann_date") or "",
        "announcement_time_precision": announcement.get("time_precision") or "missing",
        "parent_profit_yi": parent_profit,
        "parent_yoy_pct": parent_yoy,
        "parent_growth": parent_growth,
        "parent_growth_label": parent_growth["label"],
        "deduct_profit_yi": deduct_profit,
        "deduct_yoy_pct": None if deduct_yoy is None else round(deduct_yoy, 1),
        "deduct_growth": deduct_growth,
        "deduct_growth_label": deduct_growth["label"],
        "q1_deduct_yoy_pct": q1_growth["value"],
        "q1_deduct_yoy_raw_pct": q1_growth["raw_value"],
        "q1_growth_label": q1_growth["label"],
        "q2_deduct_yoy_pct": q2_growth["value"],
        "q2_deduct_yoy_raw_pct": q2_growth["raw_value"],
        "q2_growth_label": q2_growth["label"],
        "acceleration": acceleration,
        "source_quality": source_quality,
        "report_exists": bool(row.get("_report_exists")),
    }


def _earnings_commentary_report_context(code, event_year):
    report = _read_json(PREDICT_JSON.parent / f"report_{code}.json")
    if not isinstance(report, dict):
        return {
            "available": False,
            "url": f"/report?code={code}",
            "message": "尚未生成该股完整研报，可跳转研报页生成后补充估值与券商预测。",
        }

    overview = report.get("overview") or {}
    market_value = _earnings_commentary_number(overview.get("mv"))
    deduped = []
    seen = set()
    for item in report.get("broker_fc") or []:
        if not isinstance(item, dict):
            continue
        signature = (item.get("org"), item.get("date"), item.get("title"))
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(item)

    values_by_year = defaultdict(list)
    for item in deduped:
        for year, value in (item.get("np") or {}).items():
            number = _earnings_commentary_number(value)
            if number is not None:
                values_by_year[str(year)].append(number)
    consensus = []
    for year in sorted(values_by_year):
        if year.isdigit() and int(year) < event_year:
            continue
        values = values_by_year[year]
        midpoint = float(np.median(values)) if values else None
        pe = market_value / midpoint if market_value is not None and midpoint is not None and midpoint > 0 else None
        consensus.append({
            "year": year,
            "net_profit_yi": None if midpoint is None else round(midpoint, 2),
            "forward_pe": None if pe is None else round(pe, 1),
            "sample_size": len(values),
        })
        if len(consensus) >= 3:
            break

    latest_reports = sorted(deduped, key=lambda item: str(item.get("date") or ""), reverse=True)[:3]
    return {
        "available": True,
        "context_type": "current_snapshot",
        "url": f"/report?code={code}",
        "updated": report.get("updated") or "",
        "as_of_price": report.get("as_of_px") or "",
        "industry": " / ".join(
            str(value) for value in (overview.get("l1"), overview.get("l2"), overview.get("l3")) if value
        ),
        "market_value_yi": None if market_value is None else round(market_value, 2),
        "pe_ttm": _earnings_commentary_number(overview.get("pe")),
        "pb": _earnings_commentary_number(overview.get("pb")),
        "broker_consensus": consensus,
        "latest_broker_reports": [
            {
                "date": str(item.get("date") or ""),
                "org": _earnings_commentary_text(item.get("org"), 80),
                "title": _earnings_commentary_text(item.get("title"), 180),
                "rating": _earnings_commentary_text(item.get("rating"), 40),
            }
            for item in latest_reports
        ],
    }


def _earnings_commentary_compose(row):
    summary = _earnings_commentary_item_summary(row)
    announcement = row.get("_announcement") or {}
    parent_profit = summary["parent_profit_yi"]
    parent_yoy = summary["parent_yoy_pct"]
    parent_growth = summary["parent_growth"]
    deduct_profit = summary["deduct_profit_yi"]
    deduct_yoy_value = summary["deduct_yoy_pct"]
    deduct_yoy = _earnings_commentary_range(deduct_yoy_value, deduct_yoy_value, digits=1)
    deduct_growth = summary["deduct_growth"]
    q2_profit = _earnings_commentary_range(row.get("q2_dedt"), row.get("q2_dedt"), scale=1 / 1e8, digits=4)
    q1_growth = _earnings_commentary_growth_display(
        row.get("q1_yoy"),
        row.get("q1_dedt"),
        _earnings_commentary_first_value(row.get("q1_prior_dedt"), row.get("q1_dedt_prior")),
    )
    q2_growth = _earnings_commentary_growth_display(
        row.get("q2_yoy"),
        row.get("q2_dedt"),
        _earnings_commentary_first_value(row.get("q2_prior_dedt"), row.get("q2_dedt_prior")),
    )
    source_quality = summary["source_quality"]
    source_label = _earnings_commentary_text(row.get("dedt_src"), 80) or "未披露"
    period_label = summary["period_label"]
    name = summary["name"] or summary["code"]
    event_year = int(summary["period"][:4]) if summary["period"][:4].isdigit() else datetime.now().year
    report_context = _earnings_commentary_report_context(summary["code"], event_year)

    facts = []
    if parent_profit.get("mid") is not None:
        facts.append(
            f"预计实现归母净利润{_earnings_commentary_money_phrase(parent_profit)}，"
            f"同比口径应解读为{parent_growth['label']}。"
        )
    else:
        facts.append("当前数据未提供可核验的归母净利润区间。")
    if deduct_profit.get("mid") is not None:
        facts.append(
            f"预计实现扣非归母净利润{_earnings_commentary_money_phrase(deduct_profit)}，"
            f"同比口径应解读为{deduct_growth['label']}；数据口径为“{source_label}”。"
        )
    else:
        facts.append("当前数据未提供可核验的扣非净利润区间，不能用归母净利润替代。")
    facts.append("当前预告数据未包含营业收入区间，本页不补写收入及收入增速。")

    expectation = (
        f"公告类型为“{summary['type']}”，归母业绩口径为{parent_growth['label']}。"
        "当前数据源没有半年度市场一致预期基线，"
        "因此只描述公告方向，不直接下“超预期”或“低于预期”结论；待快报或中报正式披露后复核。"
    )

    if parent_profit.get("mid") is None:
        performance_parts = ["当前数据未披露可核验的归母净利润金额区间。"]
    else:
        performance_parts = [
            f"{period_label}归母净利润中值约"
            f"{_earnings_commentary_money_phrase(_earnings_commentary_range(parent_profit.get('mid'), parent_profit.get('mid'), digits=4))}"
            f"，同比口径为{parent_growth['label']}。"
        ]
    if deduct_profit.get("mid") is not None:
        performance_parts.append(
            f"扣非净利润中值约"
            f"{_earnings_commentary_money_phrase(_earnings_commentary_range(deduct_profit.get('mid'), deduct_profit.get('mid'), digits=4))}"
            f"，扣非同比口径为{deduct_growth['label']}。"
        )
    elif deduct_yoy_value is not None:
        performance_parts.append(
            f"扣非净利润金额区间未披露，当前仅能确认扣非同比口径为"
            f"{deduct_growth['label']}。"
        )
    if parent_profit.get("mid") not in (None, 0) and deduct_profit.get("mid") is not None:
        gap = parent_profit["mid"] - deduct_profit["mid"]
        ratio = abs(gap) / abs(parent_profit["mid"])
        if ratio >= 0.1:
            direction = "高于" if gap > 0 else "低于"
            performance_parts.append(
                f"归母中值{direction}扣非中值约"
                f"{_earnings_commentary_money_phrase(_earnings_commentary_range(abs(gap), abs(gap), digits=4))}，"
                "需结合非经常性损益明细判断利润质量。"
            )

    if q2_profit.get("mid") is not None:
        if source_quality == "structured":
            bridge_intro = "按结构化扣非H1区间中值扣除Q1实际值推算"
        else:
            bridge_intro = "按扣非率等回退口径估算"
        momentum = (
            f"{bridge_intro}，Q2单季扣非净利润约{_earnings_commentary_money_phrase(q2_profit)}；"
            f"Q1扣非同比为{q1_growth['label']}，Q2单季扣非同比为{q2_growth['label']}。"
        )
        if summary["acceleration"] is True:
            momentum += "在可比口径下，单季同比增速较Q1加快。"
        elif summary["acceleration"] is False:
            momentum += "在可比口径下，单季同比增速较Q1放缓。"
        else:
            momentum += "可比数据不足，不判断单季增速方向。"
    else:
        momentum = "当前缺少同口径的H1扣非区间或Q1实际值，不强行推算Q2单季表现。"

    reason = _earnings_commentary_text(row.get("reason"), 520)
    reason_may_be_truncated = len(reason) >= 118
    drivers = (
        f"公司公告原因摘要：{reason}"
        + ("……（源数据可能截断，请核对公告原文）" if reason_may_be_truncated else "")
        if reason else
        "当前结构化数据未带出完整业绩变动原因，经营驱动需回看公告原文。"
    )

    consensus = report_context.get("broker_consensus") or []
    if consensus:
        pieces = []
        for item in consensus:
            text = f"{item['year']}年归母净利润中位数{_earnings_commentary_decimal(item['net_profit_yi'])}亿元"
            if item.get("forward_pe") is not None:
                text += f"、对应前瞻PE约{_earnings_commentary_decimal(item['forward_pe'], 1)}倍"
            text += f"（{item['sample_size']}个去重样本）"
            pieces.append(text)
        valuation = (
            f"截至个股研报更新日{report_context.get('updated') or '未知'}，当前券商年度预测口径显示："
            + "；".join(pieces)
            + "。这是当前快照，可能包含公告后的信息，不能替代本次H1预期差判断或用于公告时点回测。"
        )
    elif report_context.get("available"):
        pe_text = _earnings_commentary_decimal(report_context.get("pe_ttm"), 1)
        valuation = (
            f"已有关联个股研报，当前PE-TTM约{pe_text}倍；但缺少可用的券商预测样本，"
            "本页不外推未来利润。"
            if pe_text else
            "已有关联个股研报，但缺少可用的估值与券商预测样本，本页不外推未来利润。"
        )
    else:
        valuation = report_context.get("message") or "尚无完整个股研报，暂不加入盈利预测与估值判断。"

    risks = []
    if announcement.get("time_precision") != "exact":
        risks.append({
            "level": "high",
            "label": "公告时点",
            "text": "巨潮仅有日期占位或未匹配到精确时间，不能当作盘前精确公告；研究口径保守顺延到下一交易日。",
        })
    risks.append({
        "level": "medium",
        "label": "预期基线",
        "text": "缺少半年度一致预期，公告增长不等于超预期。",
    })
    if parent_profit.get("width_pct") is not None and parent_profit["width_pct"] >= 40:
        risks.append({
            "level": "medium",
            "label": "区间较宽",
            "text": f"归母净利润预告区间宽度约为中值的{_earnings_commentary_decimal(parent_profit['width_pct'], 1)}%，落点不确定性较高。",
        })
    if source_quality != "structured":
        risks.append({
            "level": "medium",
            "label": "扣非估算",
            "text": "扣非H1或Q2使用回退估算口径，只能作为方向参考。",
        })
    if parent_growth.get("basis") not in {"rate_only"} or deduct_growth.get("basis") not in {"rate_only", "missing"}:
        risks.append({
            "level": "medium",
            "label": "H1基数质量",
            "text": "归母或扣非涉及亏损、跨符号、跨零区间或极低基数，正文已改用语义标签，原始同比仅供核验。",
        })
    if not summary["positive"]:
        risks.append({
            "level": "high",
            "label": "业绩方向",
            "text": f"公告类型为“{summary['type']}”，需重点核对下修、亏损或一次性因素。",
        })
    if q1_growth.get("basis") not in {"comparable", "rate_only", "missing"} or q2_growth.get("basis") not in {
        "comparable", "rate_only", "missing"
    }:
        risks.append({
            "level": "medium",
            "label": "基数质量",
            "text": "单季同比涉及负基数、跨符号或极低基数，不按普通百分比线性解读。",
        })
    if report_context.get("available"):
        risks.append({
            "level": "medium",
            "label": "研报时点",
            "text": "关联研报为当前快照，可能包含预告公告后的券商观点和估值，不得用于事件时点回测。",
        })
    if reason_may_be_truncated:
        risks.append({
            "level": "medium",
            "label": "原因摘要",
            "text": "上游变动原因字段可能在约120字处截断，经营驱动必须结合公告原文复核。",
        })

    status = "complete" if parent_profit.get("mid") is not None and reason and deduct_profit.get("mid") is not None else "partial"
    return {
        **summary,
        "headline": f"{name}{period_label}业绩预告点评",
        "status": status,
        "announcement": announcement,
        "announcement_facts": facts,
        "expectation_assessment": expectation,
        "commentary": [
            {"title": "业绩表现与利润质量", "body": "".join(performance_parts)},
            {"title": "Q2单季动能", "body": momentum},
            {"title": "经营驱动", "body": drivers},
            {"title": "盈利预测与估值", "body": valuation},
        ],
        "q1_growth": q1_growth,
        "q2_growth": q2_growth,
        "q2_deduct_profit_yi": q2_profit,
        "deduct_source": source_label,
        "report_context": report_context,
        "risks": risks,
        "method_note": (
            "本页按半年度模板生成，只使用H1预告、Q1实际及同口径可推算的Q2数据；"
            "不沿用年度模板的末季拆分。归母与扣非始终分开，缺失字段不做替代或臆测。"
        ),
        "source_summary": _earnings_commentary_text(row.get("summary"), 200),
        "source_reason": reason,
        "source_reason_may_be_truncated": reason_may_be_truncated,
    }


@app.route("/api/earnings-commentary")
def api_earnings_commentary():
    payload, rows, source, source_message, cninfo_payload = _earnings_commentary_load_rows()
    data_health = _earnings_commentary_data_health(payload, rows, cninfo_payload)
    code_arg = request.args.get("code") or ""
    if code_arg:
        code = _runup_norm_code(code_arg)
        if not code:
            return jsonify({"ok": False, "message": "请输入有效股票代码。"}), 400
        period_arg = _earnings_commentary_period(request.args.get("period"))
        ann_arg = _runup_norm_yyyymmdd(request.args.get("ann_date"))
        matches = [row for row in rows if row.get("code") == code]
        if period_arg:
            matches = [row for row in matches if row.get("period") == period_arg]
        if ann_arg:
            matches = [row for row in matches if _runup_norm_yyyymmdd(row.get("ann_date")) == ann_arg]
        if not matches:
            return jsonify({
                "ok": False,
                "code": code,
                "message": "未找到该股票的半年度业绩预告。",
            }), 404
        selected = matches[0]
        item = _earnings_commentary_compose(selected)
        item["data_health"] = data_health
        if data_health.get("stale"):
            item.setdefault("risks", []).insert(0, {
                "level": "high",
                "label": "数据状态",
                "text": (data_health.get("message") or "数据源状态异常，请刷新后再使用。") + "。",
            })
        return jsonify({
            "ok": True,
            "updated": payload.get("updated") or "",
            "data_health": data_health,
            "source": source,
            "source_message": source_message,
            "item": item,
        })

    summaries = [_earnings_commentary_item_summary(row) for row in rows]
    query = _earnings_commentary_text(request.args.get("q"), 80).lower()
    pinyin_codes = _earnings_commentary_pinyin_codes(query) if query else set()
    universe = str(request.args.get("universe") or "").strip()
    event_type = str(request.args.get("type") or "").strip()
    positive = str(request.args.get("positive") or "").strip().lower()
    acceleration = str(request.args.get("accel") or "").strip().lower()
    source_quality = str(request.args.get("source_quality") or "").strip()
    report_status = str(request.args.get("report_status") or "").strip().lower()

    filtered = summaries
    if query:
        filtered = [
            item for item in filtered
            if query in item["code"].lower()
            or query in item["name"].lower()
            or item["code"] in pinyin_codes
        ]
    if universe:
        filtered = [item for item in filtered if item["idx"] == universe]
    if event_type:
        filtered = [item for item in filtered if item["type"] == event_type]
    if positive in {"1", "true", "yes"}:
        filtered = [item for item in filtered if item["positive"]]
    elif positive in {"0", "false", "no"}:
        filtered = [item for item in filtered if not item["positive"]]
    if acceleration in {"1", "true", "yes"}:
        filtered = [item for item in filtered if item["acceleration"] is True]
    elif acceleration in {"0", "false", "no"}:
        filtered = [item for item in filtered if item["acceleration"] is False]
    elif acceleration == "unknown":
        filtered = [item for item in filtered if item["acceleration"] is None]
    if source_quality:
        filtered = [item for item in filtered if item["source_quality"] == source_quality]
    if report_status == "ready":
        filtered = [item for item in filtered if item["report_exists"]]
    elif report_status == "missing":
        filtered = [item for item in filtered if not item["report_exists"]]

    sort_key = str(request.args.get("sort") or "ann_desc")
    if sort_key == "parent_yoy_desc":
        filtered.sort(
            key=lambda item: (
                bool((item.get("parent_growth") or {}).get("comparable")),
                item["parent_yoy_pct"].get("mid") if item["parent_yoy_pct"].get("mid") is not None else -float("inf"),
            ),
            reverse=True,
        )
    elif sort_key == "q2_yoy_desc":
        filtered.sort(key=lambda item: item["q2_deduct_yoy_pct"] if item["q2_deduct_yoy_pct"] is not None else -float("inf"), reverse=True)
    elif sort_key == "name_asc":
        filtered.sort(key=lambda item: (item["name"], item["code"]))

    try:
        page = max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(10, min(100, int(request.args.get("page_size") or 60)))
    except (TypeError, ValueError):
        page_size = 60
    total = len(filtered)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    start = (page - 1) * page_size
    page_items = filtered[start:start + page_size]

    facets = {
        "universe": {key: sum(1 for item in summaries if item["idx"] == key) for key in ("csi300", "csi500", "csi1000", "other")},
        "types": dict(sorted({item["type"]: sum(1 for other in summaries if other["type"] == item["type"]) for item in summaries}.items())),
        "positive": sum(1 for item in summaries if item["positive"]),
        "acceleration": sum(1 for item in summaries if item["acceleration"] is True),
        "exact_time": sum(1 for item in summaries if item["announcement_time_precision"] == "exact"),
        "date_only": sum(1 for item in summaries if item["announcement_time_precision"] == "date_only"),
        "structured": sum(1 for item in summaries if item["source_quality"] == "structured"),
        "report_ready": sum(1 for item in summaries if item["report_exists"]),
    }
    return jsonify({
        "ok": True,
        "updated": payload.get("updated") or "",
        "data_health": data_health,
        "source": source,
        "source_message": source_message,
        "periods": sorted({item["period"] for item in summaries}, reverse=True),
        "facets": facets,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "total": total,
        "items": page_items,
        "message": source_message if summaries else "尚无半年度业绩预告数据。",
    })


_EARNINGS_ANNOUNCEMENT_KIND_LABELS = {
    "forecast": "业绩预告",
    "express": "业绩快报",
    "report": "业绩报告",
}


_EARNINGS_ANNOUNCEMENT_PERIOD_RE = re.compile(
    r"(20\d{2})\s*年?\s*(第一季度|一季度|一季报|半年度|半年报|上半年|中期|"
    r"第三季度|三季度|三季报|前三季度|1\s*[-—至]\s*9月|年度|年报)"
)


def _earnings_announcement_categories(item):
    title = str((item or {}).get("title") or "")
    raw_type = str((item or {}).get("type") or (item or {}).get("raw_type") or "")
    text = f"{raw_type} {title}"
    categories = []
    if "业绩预告" in text:
        categories.append("forecast")
    if "业绩快报" in text:
        categories.append("express")
    report_tokens = (
        "季度报告", "半年度报告", "年度报告", "一季报", "三季报",
        "半年报", "中期报告", "年报",
    )
    if any(token in text for token in report_tokens):
        categories.append("report")
    return categories


def _earnings_announcement_category(item):
    categories = _earnings_announcement_categories(item)
    return categories[0] if categories else ""


def _earnings_announcement_period_from_marker(year, marker):
    if marker in {"第一季度", "一季度", "一季报"}:
        return f"{year}0331"
    if marker in {"半年度", "半年报", "上半年", "中期"}:
        return f"{year}0630"
    if marker in {"第三季度", "三季度", "三季报", "前三季度", "1-9月", "1—9月", "1至9月"}:
        return f"{year}0930"
    if marker in {"年度", "年报"}:
        return f"{year}1231"
    return ""


def _earnings_announcement_period(item, fallback=""):
    explicit = _earnings_commentary_period(
        (item or {}).get("period")
        or (item or {}).get("end_date")
        or (item or {}).get("report_period")
        or fallback
    )
    if explicit:
        return explicit
    title = str((item or {}).get("title") or "")
    period_matches = list(_EARNINGS_ANNOUNCEMENT_PERIOD_RE.finditer(title))
    if period_matches:
        selected = period_matches[-1]
        year, marker = selected.group(1), selected.group(2)
    else:
        performance_matches = re.findall(r"(20\d{2})\s*年(?:度)?\s*业绩(?:预告|快报)", title)
        years = re.findall(r"20\d{2}", title)
        year = performance_matches[-1] if performance_matches else (years[-1] if years else "")
        marker = "年度" if performance_matches else ""
    if not year:
        return ""
    marked_period = _earnings_announcement_period_from_marker(year, marker)
    if marked_period:
        return marked_period
    if "业绩预告" in title or "业绩快报" in title:
        return f"{year}1231"
    return ""


def _earnings_announcement_period_for_category(item, category):
    explicit = _earnings_commentary_period(
        (item or {}).get("period")
        or (item or {}).get("end_date")
        or (item or {}).get("report_period")
    )
    if explicit:
        return explicit
    title = str((item or {}).get("title") or "")
    category_patterns = {
        "forecast": ("业绩预告",),
        "express": ("业绩快报",),
        "report": ("季度报告", "半年度报告", "年度报告", "一季报", "三季报", "半年报", "中期报告", "年报"),
    }
    positions = [
        (match.start(), match.end())
        for token in category_patterns.get(category, ())
        for match in re.finditer(re.escape(token), title)
    ]
    candidates = []
    for position, token_end in positions:
        preceding = [match for match in _EARNINGS_ANNOUNCEMENT_PERIOD_RE.finditer(title[:token_end])]
        if not preceding:
            continue
        nearest = preceding[-1]
        if max(0, position - nearest.end()) <= 18:
            candidates.append((position, nearest))
    if candidates:
        _, matched = max(candidates, key=lambda value: value[0])
        period = _earnings_announcement_period_from_marker(matched.group(1), matched.group(2))
        if period:
            return period
    return _earnings_announcement_period(item)


def _earnings_announcement_period_label(period):
    period = _earnings_commentary_period(period)
    if len(period) != 8:
        return period or "报告期待核验"
    suffix = period[4:]
    labels = {"0331": "一季度", "0630": "半年度", "0930": "三季度", "1231": "年度"}
    return f"{period[:4]}年{labels.get(suffix, period[4:])}"


def _earnings_announcement_period_arg(value):
    period = _earnings_commentary_period(value)
    if len(period) != 8 or period[4:] not in {"0331", "0630", "0930", "1231"}:
        return ""
    try:
        datetime.strptime(period, "%Y%m%d")
    except ValueError:
        return ""
    return period


def _earnings_announcement_date_arg(value):
    compact = _runup_norm_yyyymmdd(value)
    if len(compact) != 8:
        return ""
    try:
        parsed = datetime.strptime(compact, "%Y%m%d")
    except ValueError:
        return ""
    return parsed.strftime("%Y-%m-%d")


def _earnings_announcement_datetime(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("/", "-").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _earnings_announcement_is_noise(item, category):
    title = str((item or {}).get("title") or "")
    common_noise = (
        "提示性公告", "持续督导", "说明会", "延期披露", "问询函", "监管工作函",
        "回复", "英文", "审计报告", "社会责任", "ESG", "工作规程", "管理制度",
        "审核意见", "确认意见", "预约披露", "披露时间变更", "无法保证", "子公司",
        "附属公司", "重大差错", "责任追究", "募集资金年度报告", "10-K", "10-Q",
        "终止上市", "编制及审计进展", "审计进展", "专项报告", "专项说明",
        "评级机构", "关注公告", "内部控制",
    )
    if any(token in title for token in common_noise):
        return True
    if category == "report" and "摘要" in title:
        return True
    if category in {"forecast", "express"} and "取消" in title:
        return True
    return False


def _earnings_announcement_cninfo_event(item, trusted_time=True, category_override="", period_override=""):
    category = category_override or _earnings_announcement_category(item)
    if not category or _earnings_announcement_is_noise(item, category):
        return None
    code = _runup_norm_code((item or {}).get("code") or (item or {}).get("symbol"))
    ann_date = _runup_fmt_date((item or {}).get("ann_date") or (item or {}).get("date"))
    if not code or not ann_date:
        return None
    raw_datetime = str((item or {}).get("ann_datetime") or "").strip()
    parsed = _earnings_announcement_datetime(raw_datetime)
    if parsed is None:
        precision = "missing"
    elif parsed.time() == datetime.strptime("00:00:00", "%H:%M:%S").time():
        precision = "date_only"
    elif trusted_time:
        precision = "exact"
    else:
        precision = "unverified"
    announcement_id = str((item or {}).get("announcement_id") or (item or {}).get("id") or "").strip()
    title = _earnings_commentary_text((item or {}).get("title"), 300)
    period = period_override or _earnings_announcement_period_for_category(item, category)
    event_id = ":".join((announcement_id, category, period)) if announcement_id else ":".join((code, ann_date, category, period, title))
    return {
        "event_id": event_id,
        "announcement_id": announcement_id,
        "code": code,
        "name": _earnings_commentary_text((item or {}).get("name"), 80),
        "category": category,
        "category_label": _EARNINGS_ANNOUNCEMENT_KIND_LABELS[category],
        "raw_type": _earnings_commentary_text((item or {}).get("type"), 40),
        "ann_date": ann_date,
        "ann_datetime": raw_datetime,
        "time_precision": precision,
        "title": title,
        "url": str((item or {}).get("url") or "").strip(),
        "period": period,
        "period_label": _earnings_announcement_period_label(period),
        "source": "cninfo",
        "growth_value": None,
        "growth_label": "",
        "growth_comparable": False,
        "growth_candidate": False,
        "acceleration": None,
        "window_status": "",
    }


def _earnings_announcement_forecast_growth(row):
    summary = _earnings_commentary_item_summary(row)
    q1_yoy = _earnings_commentary_number(row.get("q1_yoy"))
    q2_yoy = _earnings_commentary_number(row.get("q2_yoy"))
    q2_profit = _earnings_commentary_number(row.get("q2_dedt"))
    q1_profit = _earnings_commentary_number(row.get("q1_dedt"))
    if q1_profit is None and q2_profit is not None:
        h1_low = _earnings_commentary_number(row.get("dedt_lo"))
        h1_high = _earnings_commentary_number(row.get("dedt_hi"))
        h1_values = [value for value in (h1_low, h1_high) if value is not None]
        if h1_values:
            q1_profit = (sum(h1_values) / len(h1_values)) - q2_profit
    q1_prior = _earnings_commentary_first_value(row.get("q1_prior_dedt"), row.get("q1_dedt_prior"))
    q2_prior = _earnings_commentary_first_value(row.get("q2_prior_dedt"), row.get("q2_dedt_prior"))
    low_base_risk = any(value is not None and abs(value) >= 500 for value in (q1_yoy, q2_yoy))
    positive_q2 = q2_profit is not None and q2_profit > 0
    q1_growth = _earnings_commentary_growth_display(
        q1_yoy,
        q1_profit,
        q1_prior,
    )
    q2_growth = _earnings_commentary_growth_display(
        q2_yoy,
        q2_profit,
        q2_prior,
    )
    forecast_type = str(summary.get("type") or row.get("type") or "")
    non_growth_type = any(token in forecast_type for token in ("首亏", "预亏", "续亏", "减亏", "扭亏"))
    comparable_or_pending = (
        q1_growth.get("basis") in {"comparable", "rate_only"}
        and q2_growth.get("basis") in {"comparable", "rate_only"}
        and (q1_profit is None or q1_profit > 0)
        and (q1_prior is None or _earnings_commentary_number(q1_prior) > 0)
        and (q2_prior is None or _earnings_commentary_number(q2_prior) > 0)
    )
    accelerating = (
        q1_yoy is not None
        and q2_yoy is not None
        and q2_yoy > q1_yoy
        and positive_q2
        and comparable_or_pending
        and not low_base_risk
        and not non_growth_type
    )
    if q1_yoy is not None and q2_yoy is not None:
        label = f"Q1扣非同比{q1_growth.get('label') or '待核验'} → Q2{q2_growth.get('label') or '待核验'}"
        if q2_profit is not None:
            label += f"；Q2扣非约{q2_profit / 1e8:.2f}亿元"
    else:
        label = summary.get("deduct_growth_label") or summary.get("parent_growth_label") or "增长指标待补"
    return {
        "growth_value": None if q2_yoy is None else round(q2_yoy, 1),
        "growth_label": label,
        "growth_comparable": q1_growth.get("basis") == "comparable" and q2_growth.get("basis") == "comparable",
        "growth_candidate": accelerating,
        "q1_growth_value": None if q1_yoy is None else round(q1_yoy, 1),
        "q2_profit_yi": None if q2_profit is None else round(q2_profit / 1e8, 4),
        "acceleration": True if accelerating else (False if q1_yoy is not None and q2_yoy is not None else None),
        "forecast_type": forecast_type,
    }


def _earnings_announcement_merge_source(existing, source):
    parts = [part for part in str(existing or "").split("+") if part]
    if source and source not in parts:
        parts.append(source)
    return "+".join(parts)


def _earnings_announcement_build_events_uncached():
    forecast_payload, forecast_rows, forecast_source, forecast_message, cninfo_payload = _earnings_commentary_load_rows()
    forecast_health = _earnings_commentary_timestamp_health(forecast_payload)
    cninfo_health = _earnings_commentary_timestamp_health(cninfo_payload)
    cninfo_items = cninfo_payload.get("items") if isinstance(cninfo_payload, dict) else None
    cninfo_schema_ok = isinstance(cninfo_items, list) and all(isinstance(item, dict) for item in cninfo_items)
    trusted_time = cninfo_schema_ok and bool(cninfo_items) and cninfo_health.get("status") == "fresh"

    events = []
    seen_event_ids = set()
    for raw in cninfo_items or []:
        if not isinstance(raw, dict):
            continue
        for category in _earnings_announcement_categories(raw):
            event = _earnings_announcement_cninfo_event(
                raw,
                trusted_time=trusted_time,
                category_override=category,
                period_override=_earnings_announcement_period_for_category(raw, category),
            )
            if not event or event["event_id"] in seen_event_ids:
                continue
            seen_event_ids.add(event["event_id"])
            events.append(event)

    def event_id_rank(event):
        raw_id = str(event.get("announcement_id") or "")
        return (int(raw_id) if raw_id.isdigit() else -1, raw_id)

    events_by_announcement_id = defaultdict(list)
    events_by_date = defaultdict(list)

    def index_event(event):
        announcement_id = str(event.get("announcement_id") or "").strip()
        if announcement_id:
            events_by_announcement_id[announcement_id].append(event)
        events_by_date[(
            event.get("code") or "",
            event.get("category") or "",
            event.get("ann_date") or "",
        )].append(event)

    for event in events:
        index_event(event)

    for row in forecast_rows:
        code = row.get("code") or ""
        ann_date = row.get("ann_date") or ""
        period = row.get("period") or ""
        growth = _earnings_announcement_forecast_growth(row)
        announcement = row.get("_announcement") or {}
        announcement_id = str(announcement.get("id") or "").strip()
        matches = [
            event for event in events_by_announcement_id.get(announcement_id, ())
            if event.get("code") == code
            and event.get("category") == "forecast"
        ] if announcement_id else []
        if not matches:
            match_dates = {
                value for value in (
                    ann_date,
                    _runup_fmt_date(announcement.get("source_date")),
                ) if value
            }
            matches = [
                event
                for candidate_date in match_dates
                for event in events_by_date.get((code, "forecast", candidate_date), ())
                if not period or not event.get("period") or event.get("period") == period
            ]
        for event in matches:
            if not event.get("period") or announcement_id:
                event["period"] = period
                event["period_label"] = _earnings_announcement_period_label(period)
            if not event.get("name"):
                event["name"] = row.get("name") or ""
        if matches:
            target = max(matches, key=event_id_rank)
            target.update(growth)
            target["source"] = _earnings_announcement_merge_source(target.get("source"), forecast_source)
            target["forecast_raw_date"] = ann_date
            target["date_match"] = announcement.get("date_match") or (
                "same" if target.get("ann_date") == ann_date else "nearby"
            )
            continue

        event_id = announcement_id or ":".join(("forecast", code, period, _runup_norm_yyyymmdd(ann_date)))
        title = announcement.get("title") or f"{_earnings_announcement_period_label(period)}业绩预告"
        event = {
            "event_id": event_id,
            "announcement_id": announcement_id,
            "code": code,
            "name": row.get("name") or "",
            "category": "forecast",
            "category_label": _EARNINGS_ANNOUNCEMENT_KIND_LABELS["forecast"],
            "raw_type": "业绩预告",
            "forecast_type": row.get("type") or "",
            "ann_date": announcement.get("source_date") or ann_date,
            "forecast_raw_date": ann_date,
            "date_match": announcement.get("date_match") or "missing",
            "ann_datetime": announcement.get("source_datetime") or "",
            "time_precision": announcement.get("time_precision") or "missing",
            "title": _earnings_commentary_text(title, 300),
            "url": announcement.get("url") or "",
            "period": period,
            "period_label": _earnings_announcement_period_label(period),
            "source": forecast_source,
            "window_status": "",
            **growth,
        }
        events.append(event)
        index_event(event)

    runup = _read_json(PREDICT_JSON.parent / "runup.json")
    if not isinstance(runup, dict):
        runup = _read_json(APP_ROOT / "data" / "runup.json") or {}
    runup_events = runup.get("events") if isinstance(runup.get("events"), dict) else {}
    for source_key, category in (("express", "express"), ("report", "report")):
        for raw in runup_events.get(source_key) or []:
            if not isinstance(raw, dict):
                continue
            code = _runup_norm_code(raw.get("code") or raw.get("ts_code"))
            ann_date = _runup_fmt_date(raw.get("ann_date") or raw.get("date"))
            period = _earnings_announcement_period(raw)
            if not code or not ann_date:
                continue
            matches = [
                event for event in events_by_date.get((code, category, ann_date), ())
                if not period or not event.get("period") or event.get("period") == period
            ]
            if matches:
                for event in matches:
                    if not event.get("name"):
                        event["name"] = raw.get("name") or ""
                    if not event.get("period") and period:
                        event["period"] = period
                        event["period_label"] = _earnings_announcement_period_label(period)
                    event["source"] = _earnings_announcement_merge_source(event.get("source"), "runup")
                continue
            event = {
                "event_id": ":".join(("runup", category, code, period, _runup_norm_yyyymmdd(ann_date))),
                "announcement_id": "",
                "code": code,
                "name": _earnings_commentary_text(raw.get("name"), 80),
                "category": category,
                "category_label": _EARNINGS_ANNOUNCEMENT_KIND_LABELS[category],
                "raw_type": _EARNINGS_ANNOUNCEMENT_KIND_LABELS[category],
                "ann_date": ann_date,
                "ann_datetime": "",
                "time_precision": "missing",
                "title": f"{_earnings_announcement_period_label(period)}{_EARNINGS_ANNOUNCEMENT_KIND_LABELS[category]}",
                "url": "",
                "period": period,
                "period_label": _earnings_announcement_period_label(period),
                "source": "runup",
                "growth_value": None,
                "growth_label": "",
                "growth_comparable": False,
                "growth_candidate": False,
                "acceleration": None,
                "window_status": "",
            }
            events.append(event)
            index_event(event)

    periods = sorted({event.get("period") for event in events if event.get("period")}, reverse=True)
    forecast_periods = sorted({row.get("period") for row in forecast_rows if row.get("period")}, reverse=True)
    current_period = forecast_periods[0] if forecast_periods else (periods[0] if periods else "")
    cninfo_errors = cninfo_payload.get("errors") if isinstance(cninfo_payload.get("errors"), list) else []
    cninfo_error_messages = [
        str(item.get("error") or "").strip()
        for item in cninfo_errors
        if isinstance(item, dict) and str(item.get("error") or "").strip()
    ]
    failed_query_counts = [
        int(match.group(1))
        for message in cninfo_error_messages
        for match in [re.search(r"(\d+)\s*/\s*\d+\s+failed", message, flags=re.IGNORECASE)]
        if match
    ]
    coverage = {
        "forecast_updated": forecast_payload.get("updated") or "",
        "forecast_max_date": max((row.get("ann_date") or "" for row in forecast_rows), default=""),
        "forecast_status": forecast_health.get("status") or "",
        "forecast_age_hours": forecast_health.get("age_hours"),
        "forecast_stale": bool(forecast_health.get("stale")),
        "forecast_high_growth_ready": bool(forecast_rows) and forecast_health.get("status") == "fresh",
        "cninfo_updated": cninfo_payload.get("updated") or "",
        "cninfo_max_date": max((event.get("ann_date") or "" for event in events if "cninfo" in event.get("source", "")), default=""),
        "cninfo_min_date": min((event.get("ann_date") or "" for event in events if "cninfo" in event.get("source", "") and event.get("ann_date")), default=""),
        "cninfo_item_count": len(cninfo_items or []),
        "cninfo_unique_codes": len({
            _runup_norm_code(item.get("code") or item.get("symbol"))
            for item in cninfo_items or []
            if isinstance(item, dict) and _runup_norm_code(item.get("code") or item.get("symbol"))
        }),
        "runup_updated": runup.get("updated") or "",
        "forecast_source": forecast_source,
        "forecast_message": forecast_message,
        "cninfo_time_trusted": trusted_time,
        "cninfo_error_count": sum(failed_query_counts) if failed_query_counts else len(cninfo_errors),
        "cninfo_error_summary": _earnings_commentary_text("；".join(cninfo_error_messages), 320),
        "cninfo_incomplete": bool(cninfo_errors),
        "cninfo_complete": cninfo_schema_ok and bool(cninfo_items) and not cninfo_errors,
    }
    return events, periods, current_period, coverage


_earnings_announcement_cache_lock = threading.Lock()
_earnings_announcement_cache = {"signature": None, "value": None}


def _earnings_announcement_source_signature():
    paths = (
        PREDICT_JSON.parent / "forecast_browse.json",
        PREDICT_JSON.parent / "rolling_earnings.json",
        PREDICT_JSON.parent / "cninfo_earnings_announcements.json",
        PREDICT_JSON.parent / "runup.json",
        APP_ROOT / "data" / "runup.json",
    )
    stats = []
    for path in paths:
        try:
            stat = path.stat()
            stats.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            stats.append((str(path), -1, -1))
    # Timestamp health changes even when files do not.  A five-minute bucket
    # prevents a once-fresh snapshot from remaining cached past the stale gate.
    return (int(time.time() // 300), tuple(stats))


def _earnings_announcement_clone_result(result):
    events, periods, current_period, coverage = result
    return [dict(event) for event in events], list(periods), current_period, dict(coverage)


def _earnings_announcement_build_events():
    if app.config.get("TESTING"):
        return _earnings_announcement_build_events_uncached()
    signature = _earnings_announcement_source_signature()
    with _earnings_announcement_cache_lock:
        if _earnings_announcement_cache.get("signature") == signature:
            cached = _earnings_announcement_cache.get("value")
            if cached is not None:
                return _earnings_announcement_clone_result(cached)
    built = _earnings_announcement_build_events_uncached()
    with _earnings_announcement_cache_lock:
        _earnings_announcement_cache["signature"] = signature
        _earnings_announcement_cache["value"] = _earnings_announcement_clone_result(built)
    return built


def _earnings_after_close_window(calendar, now=None):
    now = now or datetime.now()
    today = now.strftime("%Y%m%d")
    days = []
    for raw_day in calendar or []:
        normalized = _runup_norm_yyyymmdd(raw_day)
        if not normalized:
            continue
        try:
            datetime.strptime(normalized, "%Y%m%d")
        except ValueError:
            continue
        days.append(normalized)
    days = sorted(set(days))
    calendar_as_of = max((day for day in days if day <= today), default="")
    previous = max((day for day in days if day < today), default="")
    if not previous:
        return {
            "available": False,
            "reason": "calendar_unavailable",
            "previous_trade_date": "",
            "calendar_as_of": _runup_fmt_date(calendar_as_of),
            "start": "",
            "end": now.isoformat(sep=" ", timespec="seconds"),
        }
    gap_days = (now.date() - datetime.strptime(previous, "%Y%m%d").date()).days
    if gap_days > 7:
        return {
            "available": False,
            "reason": "calendar_stale",
            "previous_trade_date": _runup_fmt_date(previous),
            "calendar_as_of": _runup_fmt_date(calendar_as_of),
            "gap_days": gap_days,
            "start": "",
            "end": now.isoformat(sep=" ", timespec="seconds"),
            "label": f"交易日历仅覆盖至 {_runup_fmt_date(calendar_as_of)}，昨收后窗口已暂停",
        }
    start = datetime.strptime(previous + " 15:00:00", "%Y%m%d %H:%M:%S")
    return {
        "available": True,
        "previous_trade_date": _runup_fmt_date(previous),
        "calendar_as_of": _runup_fmt_date(calendar_as_of),
        "gap_days": gap_days,
        "start": start.isoformat(sep=" ", timespec="seconds"),
        "end": now.isoformat(sep=" ", timespec="seconds"),
        "label": f"{_runup_fmt_date(previous)} 15:00 至 {now.strftime('%Y-%m-%d %H:%M')}",
    }


def _earnings_announcement_window_status(event, window):
    if not (window or {}).get("available"):
        return ""
    start = _earnings_announcement_datetime(window.get("start"))
    end = _earnings_announcement_datetime(window.get("end"))
    if start is None or end is None:
        return ""
    published = _earnings_announcement_datetime((event or {}).get("ann_datetime"))
    if (event or {}).get("time_precision") == "exact" and published is not None:
        return "confirmed" if start <= published <= end else ""
    ann_date = _runup_norm_yyyymmdd((event or {}).get("ann_date"))
    previous = _runup_norm_yyyymmdd(window.get("previous_trade_date"))
    today = end.strftime("%Y%m%d")
    if ann_date and previous and previous < ann_date <= today:
        return "date_only_candidate" if (event or {}).get("time_precision") == "date_only" else "missing_time_candidate"
    if ann_date and previous and ann_date == previous:
        return "anchor_date_uncertain"
    return ""


def _earnings_announcement_group_events(events):
    grouped = defaultdict(list)
    for event in events:
        grouped[(event.get("code") or "", event.get("period") or "")].append(event)
    output = []
    category_order = {"forecast": 0, "express": 1, "report": 2}
    window_order = {"": 0, "anchor_date_uncertain": 1, "missing_time_candidate": 2, "date_only_candidate": 3, "confirmed": 4}

    def latest_key(event):
        return (
            event.get("ann_date") or "",
            event.get("ann_datetime") or "",
            str(event.get("announcement_id") or ""),
        )

    for (code, period), rows in grouped.items():
        latest = max(rows, key=latest_key)
        growth_rows = [row for row in rows if row.get("growth_value") is not None]
        best_growth = max(growth_rows, key=lambda row: row.get("growth_value")) if growth_rows else {}
        statuses = [row.get("window_status") or "" for row in rows]
        categories = sorted({row.get("category") for row in rows if row.get("category")}, key=lambda value: category_order.get(value, 9))
        output.append({
            "group_id": f"{code}:{period or 'unknown'}",
            "code": code,
            "name": latest.get("name") or next((row.get("name") for row in rows if row.get("name")), ""),
            "period": period,
            "period_label": _earnings_announcement_period_label(period),
            "categories": categories,
            "category_labels": [_EARNINGS_ANNOUNCEMENT_KIND_LABELS[value] for value in categories],
            "ann_date": latest.get("ann_date") or "",
            "ann_datetime": latest.get("ann_datetime") or "",
            "time_precision": latest.get("time_precision") or "missing",
            "title": latest.get("title") or "",
            "url": latest.get("url") or "",
            "growth_value": best_growth.get("growth_value"),
            "growth_label": best_growth.get("growth_label") or "",
            "growth_comparable": bool(best_growth.get("growth_comparable")),
            "growth_candidate": bool(best_growth.get("growth_candidate")),
            "forecast_type": best_growth.get("forecast_type") or "",
            "acceleration": best_growth.get("acceleration"),
            "window_status": max(statuses, key=lambda value: window_order.get(value, 0), default=""),
            "event_count": len(rows),
        })
    return output


@app.route("/api/earnings-announcements")
def api_earnings_announcements():
    """Read-only earnings disclosure browser; never queues reports or mutates watcher state."""
    events, periods, current_period, coverage = _earnings_announcement_build_events()
    mode = str(request.args.get("preset") or "search").strip().lower()
    if mode not in {"search", "after_close_growth"}:
        return jsonify({"ok": False, "message": "未知公告查询模式。"}), 400

    raw_query = str(request.args.get("q") or "").strip()
    if len(raw_query) > 80 or any(ord(char) < 32 for char in raw_query):
        return jsonify({"ok": False, "message": "股票查询条件无效。"}), 400
    query = raw_query.lower()
    ann_date_raw = str(request.args.get("ann_date") or "").strip()
    ann_date = _earnings_announcement_date_arg(ann_date_raw)
    if ann_date_raw and not ann_date:
        return jsonify({"ok": False, "message": "公告日期格式无效。"}), 400
    period_raw = str(request.args.get("period") or "").strip()
    period = _earnings_announcement_period_arg(period_raw)
    if period_raw and not period:
        return jsonify({"ok": False, "message": "报告期格式无效。"}), 400
    target_period = period or current_period

    kind_raw = str(request.args.get("kind") or "all").strip().lower()
    if kind_raw in {"", "all"}:
        kinds = set(_EARNINGS_ANNOUNCEMENT_KIND_LABELS)
    else:
        requested_kinds = {value.strip() for value in kind_raw.split(",") if value.strip()}
        kinds = requested_kinds & set(_EARNINGS_ANNOUNCEMENT_KIND_LABELS)
        if not kinds or kinds != requested_kinds:
            return jsonify({"ok": False, "message": "公告类型无效。"}), 400
    try:
        min_growth = max(0.0, min(10000.0, float(request.args.get("min_growth") or 20)))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "高增长阈值无效。"}), 400

    window = _earnings_after_close_window(_runup_trade_calendar())
    window["data_as_of"] = coverage.get("forecast_updated") or ""
    for event in events:
        event["window_status"] = _earnings_announcement_window_status(event, window)

    filtered = [event for event in events if event.get("category") in kinds]
    anchor_date_uncertain_excluded = 0
    high_growth_available = bool(
        coverage.get("forecast_high_growth_ready") and window.get("available")
    )
    if mode == "after_close_growth":
        growth_matches = [
            event for event in filtered
            if event.get("category") == "forecast"
            and event.get("period") == target_period
            and event.get("growth_candidate")
            and event.get("growth_value") is not None
            and event.get("growth_value") > min_growth
        ] if high_growth_available else []
        anchor_date_uncertain_excluded = sum(
            1 for event in growth_matches if event.get("window_status") == "anchor_date_uncertain"
        )
        filtered = [
            event for event in growth_matches
            if event.get("window_status") in {"confirmed", "date_only_candidate", "missing_time_candidate"}
        ]
    else:
        if ann_date:
            filtered = [event for event in filtered if event.get("ann_date") == ann_date]
        elif target_period:
            filtered = [event for event in filtered if event.get("period") == target_period]
        if period:
            filtered = [event for event in filtered if event.get("period") == period]

    pinyin_codes = _earnings_commentary_pinyin_codes(query) if query else set()
    if query:
        filtered = [
            event for event in filtered
            if query in str(event.get("code") or "").lower()
            or query in str(event.get("name") or "").lower()
            or event.get("code") in pinyin_codes
        ]

    groups = _earnings_announcement_group_events(filtered)
    if mode == "after_close_growth":
        groups.sort(key=lambda item: (item.get("growth_value") if item.get("growth_value") is not None else -float("inf"), item.get("ann_date") or "", item.get("code") or ""), reverse=True)
    else:
        groups.sort(key=lambda item: (item.get("ann_date") or "", item.get("ann_datetime") or "", item.get("code") or ""), reverse=True)

    try:
        page = max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(10, min(100, int(request.args.get("page_size") or 60)))
    except (TypeError, ValueError):
        page_size = 60
    total = len(groups)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    page_items = groups[(page - 1) * page_size:page * page_size]

    selected_code_raw = str(request.args.get("code") or "").strip()
    selected_code = _runup_norm_code(selected_code_raw)
    if selected_code_raw and not re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", selected_code_raw.upper()):
        return jsonify({"ok": False, "message": "公告链股票代码无效。"}), 400
    chain = []
    chain_period_raw = str(request.args.get("chain_period") or "").strip()
    chain_period = _earnings_announcement_period_arg(chain_period_raw)
    if chain_period_raw and not chain_period:
        return jsonify({"ok": False, "message": "公告链报告期格式无效。"}), 400
    if selected_code:
        selected_events = [event for event in events if event.get("code") == selected_code]
        if not chain_period:
            if any(event.get("period") == target_period for event in selected_events):
                chain_period = target_period
            else:
                chain_period = max((event.get("period") or "" for event in selected_events), default="")
        chain = [event for event in selected_events if not chain_period or event.get("period") == chain_period]
        chain.sort(key=lambda event: (event.get("ann_date") or "", event.get("ann_datetime") or "", event.get("announcement_id") or ""))
    chain_missing_cninfo = any("cninfo" not in str(event.get("source") or "").split("+") for event in chain)
    chain_complete = bool(chain) and bool(coverage.get("cninfo_complete")) and not chain_missing_cninfo
    chain_coverage_note = (
        "巨潮扫描完整，当前公告链中的文档均已匹配巨潮。"
        if chain_complete else
        "当前巨潮扫描或文档匹配不完整；公告链仅代表已采集记录，不代表公司全部历史披露。"
    )

    facets = {
        "forecast": sum(1 for item in groups if "forecast" in item.get("categories", [])),
        "express": sum(1 for item in groups if "express" in item.get("categories", [])),
        "report": sum(1 for item in groups if "report" in item.get("categories", [])),
        "confirmed_after_close": sum(1 for item in groups if item.get("window_status") == "confirmed"),
        "time_pending": sum(1 for item in groups if item.get("window_status") in {"anchor_date_uncertain", "date_only_candidate", "missing_time_candidate"}),
        "anchor_date_uncertain_excluded": anchor_date_uncertain_excluded,
    }
    if mode == "after_close_growth":
        if not coverage.get("forecast_high_growth_ready"):
            response_message = "结构化预告源不是新鲜快照，高增长列表已暂停，避免把旧数据当作昨收后信息。"
        elif not window.get("available"):
            response_message = "交易日历不可用或已陈旧，高增长列表已暂停，避免构造错误的昨收后窗口。"
        else:
            response_message = (
                "昨收后高增长预告候选要求Q2扣非同比高于阈值且高于Q1、Q2扣非为正，并剔除≥500%的低基数波动；"
                "缺少同期金额基数时仍标待核验。锚点交易日仅有日期占位的公告无法确认15:00前后，未计入严格列表。"
            )
    else:
        response_message = "公告日期按原始披露日筛选；按股票查询时默认展示当前报告期公告链。"
    return jsonify({
        "ok": True,
        "preset": mode,
        "current_period": current_period,
        "target_period": target_period,
        "periods": periods,
        "ann_date": ann_date,
        "min_growth": min_growth,
        "high_growth_available": high_growth_available,
        "window": window,
        "coverage": coverage,
        "facets": facets,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "total": total,
        "items": page_items,
        "chain": {
            "code": selected_code,
            "name": next((event.get("name") for event in chain if event.get("name")), ""),
            "period": chain_period or "",
            "period_label": _earnings_announcement_period_label(chain_period),
            "items": chain,
            "complete": chain_complete,
            "coverage_note": chain_coverage_note,
        },
        "message": response_message,
    })


@app.route("/api/forecast_fundamentals")
def api_forecast_fundamentals():
    """个股基本面深度(按需): 近3年【年度】扣非增速/毛利率/ROE + 近12【单季】扣非增速/单季毛利率。tushare fina_indicator+income。"""
    code = (request.args.get("code") or "").strip()
    ts_code = _code_to_ts(code)
    if not ts_code:
        return jsonify({"ok": False, "message": "代码无法识别"})
    name = (_meta_for_codes([ts_code]).get(ts_code) or {}).get("name", "")
    try:
        pro = _tushare_api()
        fi = pro.fina_indicator(ts_code=ts_code, start_date="20210101", end_date="20261231",
                                fields="end_date,profit_dedt,grossprofit_margin,roe")
        inc = pro.income(ts_code=ts_code, start_date="20210101", end_date="20261231",
                         fields="end_date,revenue,oper_cost")
    except Exception as e:
        return jsonify({"ok": False, "message": f"tushare取数失败: {e}"})

    def mp(df, col):
        if df is None or not len(df) or col not in df.columns:
            return {}
        d = df.dropna(subset=["end_date"]).drop_duplicates("end_date", keep="first")
        out = {}
        for _, r in d.iterrows():
            v = r[col]
            out[str(r["end_date"])] = float(v) if (v is not None and v == v) else None
        return out
    dedt = mp(fi, "profit_dedt"); gm = mp(fi, "grossprofit_margin"); roe = mp(fi, "roe")
    rev = mp(inc, "revenue"); cost = mp(inc, "oper_cost")
    ORD = ["0331", "0630", "0930", "1231"]

    def yoy(cur, prev):
        if cur is None or prev is None or prev == 0:
            return None
        return round((cur - prev) / abs(prev) * 100, 1)

    def prevq(p):
        y, md = int(p[:4]), p[4:]
        i = ORD.index(md)
        return f"{y-1}1231" if i == 0 else f"{y}{ORD[i-1]}"

    def single(m, p):   # 单季 = 累计 − 上一季累计(Q1=累计)
        if p[4:] == "0331":
            return m.get(p)
        c, pc = m.get(p), m.get(prevq(p))
        return None if (c is None or pc is None) else c - pc

    annual = []
    for y in (2023, 2024, 2025):
        p, pp = f"{y}1231", f"{y-1}1231"
        annual.append({"year": y, "dedt": dedt.get(p), "dedt_yoy": yoy(dedt.get(p), dedt.get(pp)),
                       "gm": round(gm[p], 1) if gm.get(p) is not None else None,
                       "roe": round(roe[p], 1) if roe.get(p) is not None else None})
    quarterly = []
    for y in (2023, 2024, 2025, 2026):
        for j, md in enumerate(ORD):
            p = f"{y}{md}"
            if p not in dedt and p not in rev:
                continue
            sd = single(dedt, p); sd_ly = single(dedt, f"{y-1}{md}")
            sr = single(rev, p); sc = single(cost, p)
            sgm = round((sr - sc) / sr * 100, 1) if (sr and sc is not None and sr != 0) else None
            quarterly.append({"period": f"{y}Q{j+1}",
                              "dedt_single": None if sd is None else round(sd, 0),
                              "dedt_yoy": yoy(sd, sd_ly), "gm_single": sgm})
    return jsonify({"ok": True, "code": ts_code, "name": name, "annual": annual, "quarterly": quarterly[-12:]})


@app.route("/rsrs")
def rsrs_page():
    """RSRS 指数择时: 沪深300/中证1000 阻力支撑相对强度 多/空仓信号 (独立方向信号, 不并入三腿中性组合)."""
    return render_template("rsrs.html")


@app.route("/api/rsrs")
def api_rsrs():
    data = _read_json(RSRS_JSON)
    if data is None:
        return jsonify({"updated": "", "indices": [],
                        "message": "尚无RSRS信号; PC 端运行 export_rsrs.py 生成 rsrs.json"})
    return jsonify(data)


@app.route("/alphagen")
def alphagen_page():
    """AlphaGen RL自动因子挖掘: 全市场挖公式alpha池, 自动筛 against 饱和base看是否有正交增量."""
    return render_template("alphagen.html")


@app.route("/api/alphagen")
def api_alphagen():
    data = _read_json(ALPHAGEN_RESULT)
    if data is None:
        data = {"alphas": [], "n_pass": 0, "message": "尚无挖掘结果; 点「开始挖掘」或 PC 端运行 alphagen_listener.py"}
    data["pending"] = ALPHAGEN_REQUEST.exists()  # PC 是否有任务在排队/处理
    return jsonify(data)


@app.route("/api/alphagen/run", methods=["POST"])
def api_alphagen_run():
    """网页按钮触发: 写请求文件, PC 上 alphagen_listener.py 监听到后用 GPU 跑挖掘+评估。"""
    if ALPHAGEN_REQUEST.exists():
        return jsonify({"ok": False, "message": "已有挖掘任务在排队/处理中 (GPU 训练约需 30-60 分钟)"})
    body = request.get_json(silent=True) or {}
    req = {"requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "steps": int(body.get("steps", 100000)),
           "instruments": body.get("instruments", "all"),
           "pool_capacity": int(body.get("pool_capacity", 10)),
           "use_llm": bool(body.get("use_llm", False))}
    ALPHAGEN_REQUEST.parent.mkdir(parents=True, exist_ok=True)
    ALPHAGEN_REQUEST.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True, "message": "已提交挖掘任务; PC GPU 训练约 30-60 分钟, 完成后本页自动刷新结果", "request": req})


@app.route("/api/rdagent_screen")
def api_rdagent_screen():
    data = _read_json(RDAGENT_SCREEN)
    if data is None:
        return jsonify({"factors": [], "n_pass": 0, "message": "尚无筛结果; PC 端运行 factor_rdagent_screen.py"})
    return jsonify(data)


@app.route("/rdquant")
def rdquant_page():
    """RD-Agent-Quant: LLM多agent 因子+模型联合优化 (rdagent fin_quant), 展示逐轮 qlib 回测指标."""
    return render_template("rdquant.html")


@app.route("/api/rdquant")
def api_rdquant():
    data = _read_json(RDQUANT_RESULT)
    if data is None:
        data = {"loops": [], "message": "尚无结果; 点「开始优化」或 PC 端 pc_listener.py 处理"}
    data["pending"] = RDQUANT_REQUEST.exists()
    return jsonify(data)


@app.route("/api/rdquant/run", methods=["POST"])
def api_rdquant_run():
    """网页按钮触发: 写请求文件, PC 上 pc_listener.py 监听到后跑 rdagent fin_quant (LLM+docker, 每轮约20-40分钟)。"""
    if RDQUANT_REQUEST.exists():
        return jsonify({"ok": False, "message": "已有 RD-Agent-Quant 任务在排队/处理中"})
    body = request.get_json(silent=True) or {}
    req = {"requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "loop_n": int(body.get("loop_n", 2))}
    RDQUANT_REQUEST.parent.mkdir(parents=True, exist_ok=True)
    RDQUANT_REQUEST.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True, "message": "已提交; RD-Agent-Quant 每轮约 20-40 分钟(LLM+docker回测), 完成后本页自动刷新", "request": req})


@app.route("/stockbond")
def stockbond_page():
    """股债配置: 沪深300+长债ETF 低相关搭配, 降组合回撤。验证(gate_stock_bond_rotation): 股债相关-0.25, 50/50恒定回撤砍半。"""
    return render_template("stockbond.html")


@app.route("/top-risk")
def top_risk_page():
    return render_template("top_risk.html")


@app.route("/huijin-etf-flow")
def huijin_etf_flow_page():
    """汇金位列前十大持有人的ETF：日频总份额/申赎代理与时点回测。"""
    return render_template("huijin_etf_flow.html")


@app.route("/money-outflow")
def money_outflow_page():
    return render_template("money_outflow.html")


@app.route("/api/money_outflow")
def api_money_outflow():
    data = _money_outflow_payload()
    if not data:
        return jsonify({"message": "尚未运行资金流出回测: python scripts/backtest_money_outflow_signal.py", "stock": {}, "market": {}}), 404
    return jsonify(data)


TOPRISK_BROAD_ETFS = {
    "510300.SH": "HS300 ETF",
    "159919.SZ": "HS300 ETF",
    "510050.SH": "SSE50 ETF",
    "510500.SH": "CSI500 ETF",
    "512100.SH": "CSI1000 ETF",
    "159915.SZ": "ChiNext ETF",
    "588000.SH": "STAR50 ETF",
}

TOPRISK_SECTOR_ETFS = {
    "512480.SH": "半导体ETF",
    "159995.SZ": "芯片ETF",
    "512760.SH": "半导体50ETF",
    "515260.SH": "电子ETF",
    "159997.SZ": "电子ETF",
    "512720.SH": "计算机ETF",
    "159998.SZ": "计算机ETF",
    "515880.SH": "通信ETF",
    "515050.SH": "5GETF",
    "159819.SZ": "人工智能ETF",
    "512930.SH": "AI ETF",
    "159770.SZ": "机器人ETF",
    "512880.SH": "证券ETF",
    "512000.SH": "券商ETF",
    "512800.SH": "银行ETF",
    "159841.SZ": "保险ETF",
    "512010.SH": "医药ETF",
    "512170.SH": "医疗ETF",
    "512290.SH": "生物医药ETF",
    "159883.SZ": "医疗器械ETF",
    "516020.SH": "化工ETF",
    "159870.SZ": "化工ETF",
    "516220.SH": "化工龙头ETF",
    "512400.SH": "有色金属ETF",
    "159881.SZ": "有色60ETF",
    "516780.SH": "稀土ETF",
    "515210.SH": "钢铁ETF",
    "159745.SZ": "建材ETF",
    "516750.SH": "建材ETF",
    "515790.SH": "光伏ETF",
    "516160.SH": "新能源ETF",
    "515030.SH": "新能源车ETF",
    "512660.SH": "军工ETF",
    "512670.SH": "国防ETF",
    "512690.SH": "酒ETF",
    "515170.SH": "食品饮料ETF",
    "159928.SZ": "消费ETF",
    "512600.SH": "主要消费ETF",
    "159996.SZ": "家电ETF",
    "159825.SZ": "农业ETF",
    "159865.SZ": "养殖ETF",
    "512980.SH": "传媒ETF",
    "159805.SZ": "传媒ETF",
    "159869.SZ": "游戏ETF",
    "515220.SH": "煤炭ETF",
    "159930.SZ": "能源ETF",
    "159611.SZ": "电力ETF",
    "512200.SH": "房地产ETF",
    "516970.SH": "基建ETF",
}


def _toprisk_cache_dir() -> Path:
    primary = Path(STOCK_META_DB).parent / "etf_flow_cache"
    if primary.exists():
        return primary
    return Path(__file__).resolve().parent / "data" / "etf_flow_cache"


def _toprisk_safe_pct_change(series: pd.Series, periods: int) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").pct_change(periods)


def _toprisk_rolling_percentile(series: pd.Series, lookback: int, min_periods: int | None = None) -> pd.Series:
    min_periods = min_periods or max(5, lookback // 3)

    def pctile(values: np.ndarray) -> float:
        current = values[-1]
        hist = values[np.isfinite(values)]
        if not np.isfinite(current) or len(hist) < min_periods:
            return np.nan
        return float((hist <= current).mean())

    return series.rolling(lookback, min_periods=min_periods).apply(pctile, raw=True)


def _toprisk_add_features(panel: pd.DataFrame, lookback: int = 60,
                          benchmark: pd.DataFrame | None = None) -> pd.DataFrame:
    df = panel.copy().sort_values("trade_date").reset_index(drop=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["share"] = pd.to_numeric(df["share"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["share_chg_5d"] = _toprisk_safe_pct_change(df["share"], 4)
    df["ret_5d"] = _toprisk_safe_pct_change(df["close"], 4)
    df["ret_20d"] = _toprisk_safe_pct_change(df["close"], 19)
    df["flow_pctile"] = _toprisk_rolling_percentile(df["share_chg_5d"], lookback)
    df["ret20_pctile"] = _toprisk_rolling_percentile(df["ret_20d"], lookback)
    df["high_60"] = df["close"].rolling(60, min_periods=min(20, len(df))).max()
    df["near_high"] = df["close"] >= df["high_60"] * 0.98
    if benchmark is not None and not benchmark.empty:
        bm = benchmark.copy()
        bm["trade_date"] = pd.to_datetime(bm["trade_date"])
        bm["benchmark_close"] = pd.to_numeric(bm["close"], errors="coerce")
        bm["benchmark_ret_5d"] = _toprisk_safe_pct_change(bm["benchmark_close"], 4)
        df = df.merge(bm[["trade_date", "benchmark_ret_5d"]], on="trade_date", how="left")
        df["weakness"] = df["ret_5d"] < df["benchmark_ret_5d"]
    else:
        df["benchmark_ret_5d"] = np.nan
        df["weakness"] = df["ret_5d"] < 0
    df["crowding"] = df["flow_pctile"] >= 0.9
    recent_hot = df["ret20_pctile"].rolling(5, min_periods=1).max() >= 0.8
    df["overheat"] = df["near_high"] & recent_hot
    return df


def _toprisk_round(value, ndigits: int = 3):
    try:
        if pd.isna(value):
            return None
        return round(float(value), ndigits)
    except Exception:
        return None


def _toprisk_round_pct(value, scale: float = 100.0):
    try:
        if pd.isna(value):
            return None
        return round(float(value) * scale, 2)
    except Exception:
        return None


def _toprisk_latest_record(code: str, name: str, panel: pd.DataFrame, lookback: int = 60,
                           benchmark: pd.DataFrame | None = None) -> dict:
    if panel is None or panel.empty or "trade_date" not in panel.columns or "close" not in panel.columns:
        return {"code": code, "name": name, "level": "missing", "score": 0}
    featured = _toprisk_add_features(panel, lookback=lookback, benchmark=benchmark).dropna(subset=["close"])
    if featured.empty:
        return {"code": code, "name": name, "level": "missing", "score": 0}
    row = featured.iloc[-1]
    crowding = bool(row.get("crowding", False))
    overheat = bool(row.get("overheat", False))
    weakness = bool(row.get("weakness", False))
    score = (35 if overheat else 0) + (35 if crowding else 0) + (30 if weakness else 0)
    level = "high" if score >= 80 else ("watch" if score >= 35 else "normal")
    return {
        "code": code,
        "name": name,
        "as_of": row["trade_date"].strftime("%Y-%m-%d"),
        "level": level,
        "score": score,
        "close": _toprisk_round(row.get("close")),
        "share_chg_5d_pct": _toprisk_round_pct(row.get("share_chg_5d")),
        "flow_pctile": _toprisk_round_pct(row.get("flow_pctile"), scale=100),
        "ret_5d_pct": _toprisk_round_pct(row.get("ret_5d")),
        "ret_20d_pct": _toprisk_round_pct(row.get("ret_20d")),
        "ret20_pctile": _toprisk_round_pct(row.get("ret20_pctile"), scale=100),
        "benchmark_ret_5d_pct": _toprisk_round_pct(row.get("benchmark_ret_5d")),
        "near_high": bool(row.get("near_high", False)),
        "overheat": overheat,
        "crowding": crowding,
        "weakness": weakness,
    }


def _toprisk_code_key(code: str) -> str:
    return code.replace(".", "_")


def _toprisk_latest_file(prefix: str, code: str) -> Path | None:
    files = sorted(_toprisk_cache_dir().glob(f"{prefix}_{_toprisk_code_key(code)}_*.csv.gz"))
    return files[-1] if files else None


def _toprisk_read_cached(prefix: str, code: str) -> pd.DataFrame:
    path = _toprisk_latest_file(prefix, code)
    if path is None:
        return pd.DataFrame()
    return pd.read_csv(path, dtype={"trade_date": str, "ts_code": str})


def _toprisk_share_col(df: pd.DataFrame) -> str | None:
    return next((c for c in ("fd_share", "fund_share", "share") if c in df.columns), None)


def _toprisk_load_etf_panel(code: str, share_prefix: str, daily_prefix: str) -> pd.DataFrame:
    share = _toprisk_read_cached(share_prefix, code)
    daily = _toprisk_read_cached(daily_prefix, code)
    share_col = _toprisk_share_col(share)
    if share.empty or daily.empty or share_col is None or "close" not in daily.columns:
        return pd.DataFrame()
    panel = share[["trade_date", share_col]].rename(columns={share_col: "share"})
    panel = panel.merge(daily[["trade_date", "close"]], on="trade_date", how="inner")
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], format="%Y%m%d", errors="coerce")
    panel["share"] = pd.to_numeric(panel["share"], errors="coerce")
    panel["close"] = pd.to_numeric(panel["close"], errors="coerce")
    return panel.dropna(subset=["trade_date", "share", "close"]).sort_values("trade_date")


def _toprisk_load_index(code: str) -> pd.DataFrame:
    df = _toprisk_read_cached("index", code)
    if df.empty or "close" not in df.columns:
        return pd.DataFrame()
    out = df[["trade_date", "close"]].copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d", errors="coerce")
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    return out.dropna(subset=["trade_date", "close"]).sort_values("trade_date")


def _toprisk_load_market_panel() -> pd.DataFrame:
    frames = []
    for code in TOPRISK_BROAD_ETFS:
        panel = _toprisk_load_etf_panel(code, "share", "fund_daily")
        if not panel.empty:
            frames.append(panel[["trade_date", "share"]])
    index = _toprisk_load_index("000300.SH")
    if not frames or index.empty:
        return pd.DataFrame()
    share = pd.concat(frames, ignore_index=True).groupby("trade_date", as_index=False)["share"].sum()
    return share.merge(index, on="trade_date", how="inner").sort_values("trade_date")


def _build_top_risk_report():
    benchmark = _toprisk_load_index("000300.SH")
    market = _toprisk_latest_record("000300.SH", "沪深300/宽基ETF", _toprisk_load_market_panel(), benchmark=None)
    money_outflow = _money_outflow_payload()
    if isinstance(market, dict):
        market["money_outflow"] = ((money_outflow.get("market") or {}).get("latest") or {})
        market["money_outflow_summary"] = ((money_outflow.get("market") or {}).get("summary") or {})
    sectors = []
    for code, name in TOPRISK_SECTOR_ETFS.items():
        panel = _toprisk_load_etf_panel(code, "sector_share", "sector_daily")
        if panel.empty:
            sectors.append({"code": code, "name": name, "level": "missing", "score": 0})
        else:
            sectors.append(_toprisk_latest_record(code, name, panel, benchmark=benchmark))
    order = {"high": 0, "watch": 1, "normal": 2, "missing": 3}
    sectors.sort(key=lambda x: (order.get(x.get("level"), 9), -int(x.get("score") or 0), x.get("name", "")))
    return {
        "updated": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "market": market,
        "sectors": sectors,
        "counts": {
            "high": sum(1 for x in sectors if x.get("level") == "high"),
            "watch": sum(1 for x in sectors if x.get("level") == "watch"),
            "normal": sum(1 for x in sectors if x.get("level") == "normal"),
            "missing": sum(1 for x in sectors if x.get("level") == "missing"),
        },
        "method": {
            "overheat": "接近60日高位且20日收益处于历史高分位",
            "crowding": "ETF份额5日增速处于滚动90分位以上",
            "weakness": "近5日表现转弱；板块口径为跑输沪深300",
        },
    }


@app.route("/api/top_risk")
def api_top_risk():
    try:
        return jsonify(_build_top_risk_report())
    except Exception as exc:
        log.exception("top risk report failed")
        return jsonify({"market": {}, "sectors": [], "message": f"见顶风险计算失败: {exc}"}), 500


@app.route("/api/huijin_etf_flow")
def api_huijin_etf_flow():
    """Read-only Huijin-held ETF flow proxy; never labels total ETF flow as Huijin trades."""
    payload = _read_json(HUJIN_ETF_FLOW_JSON)
    if not isinstance(payload, dict) or not isinstance(payload.get("aggregate_series"), list):
        return jsonify({
            "ok": False,
            "message": "汇金ETF资金代理尚未生成，请先运行 backtest_huijin_etf_flow.py。",
        }), 503
    return jsonify(payload)


@app.route("/api/huijin_etf_flow/etf/<code>")
def api_huijin_etf_flow_fund(code: str):
    """Per-ETF daily share history (total fund share, all investors) for the drill-down chart."""
    payload = _read_json(HUJIN_ETF_SERIES_JSON)
    funds = payload.get("funds") if isinstance(payload, dict) else None
    if not isinstance(funds, dict) or not funds:
        return jsonify({
            "ok": False,
            "message": "单只ETF份额历史尚未生成，请先运行 backtest_huijin_etf_flow.py。",
        }), 503
    fund = funds.get(code.strip().upper())
    if not isinstance(fund, dict):
        return jsonify({"ok": False, "message": f"汇金ETF名单中没有 {code}。"}), 404
    return jsonify({
        "ok": True,
        "updated": payload.get("updated"),
        "as_of": payload.get("as_of"),
        **fund,
    })


@app.route("/api/stockbond")
def api_stockbond():
    """股债配置: 沪深300 vs MA20(regime) + 长债ETF实时 + 配比建议 + 回测对比。择时轮动无效(夏0.07), 恒定配置才有效→给恒定配比+再平衡提示。"""
    import urllib.request as _u
    out = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M")}
    # 实时: 沪深300指数 + 国债ETF(腾讯/新浪快照)
    def _q(sina_code):
        try:
            r = _u.urlopen(_u.Request(f"https://qt.gtimg.cn/q={sina_code}",
                           headers={"User-Agent": "Mozilla/5.0"}), timeout=5).read().decode("gbk", "ignore")
            parts = r.split('~')
            return {"name": parts[1], "price": float(parts[3]), "pct": float(parts[32]) if len(parts) > 32 else None}
        except Exception:
            return None
    hs = _q("sh000300"); b10 = _q("sh511260"); b30 = _q("sh511100")
    # regime: 沪深300 vs MA20 (用日线bin)
    si, close = _read_bin("sh000300", "close")
    regime = "—"; above = None
    if close.size >= 20:
        ma20 = float(np.mean(close[-20:])); cur = float(close[-1])
        above = cur > ma20
        regime = "股强(沪深300在MA20上)" if above else "股弱(沪深300在MA20下)"
    out["live"] = {"hs": hs, "b10": b10, "b30": b30, "regime": regime, "above_ma20": above}
    # 配比建议: 恒定为主(择时无效); 可选"股弱时债加重"的温和倾斜
    base = {"stock": 50, "bond": 50}
    tilt = {"stock": 40, "bond": 60} if above is False else ({"stock": 60, "bond": 40} if above else {"stock": 50, "bond": 50})
    out["alloc"] = {"base": base, "tilt": tilt,
                    "note": "核心=恒定50/50(择时轮动已验证无效, 夏0.07)。温和倾斜=股弱时债加到60%(非择时, 仅小幅再平衡)。"}
    # 回测对比(gate_stock_bond_rotation 2020-2026)
    out["backtest"] = {
        "rows": [
            {"name": "全程沪深300", "ann": 4.0, "sharpe": 0.30, "mdd": -45.6},
            {"name": "全程10年国债ETF", "ann": 3.2, "sharpe": 1.25, "mdd": -4.6},
            {"name": "50股/50债恒定", "ann": 4.1, "sharpe": 0.49, "mdd": -20.1},
            {"name": "MA20股债轮动(择时)", "ann": 0.1, "sharpe": 0.07, "mdd": -30.1},
        ],
        "corr": -0.25,
        "note": "2020-2026。股债负相关-0.25。50/50恒定: 年化≥纯股、回撤砍半(-46%→-20%)、夏普0.30→0.49。择时轮动反而最差→别预测、要配置。"}
    return jsonify(out)


@app.route("/portfolio")
def portfolio_page():
    """组合配置: 四腿市场中性book(主300+抢跑+PEAD+回购)权重/各腿夏普/相关性/合并夏普 + RSRS方向overlay。"""
    return render_template("portfolio.html")


@app.route("/api/portfolio")
def api_portfolio():
    data = _read_json(COMBO_JSON)
    if data is None:
        return jsonify({"sleeves": [], "message": "尚无组合数据; PC 端运行 export_combo.py 生成 combo.json"})
    return jsonify(data)


@app.route("/api/combo_history")
def api_combo_history():
    """组合演进历史(4腿→5腿→6腿…), 看进化过程."""
    data = _read_json(COMBO_JSON.parent / "combo_history.json")
    return jsonify({"versions": data if isinstance(data, list) else []})


@app.route("/api/combo_holdings")
def api_combo_holdings():
    """组合总买入清单: 6腿当前持仓×腿权重汇总成可执行个股清单(落地)."""
    data = _read_json(COMBO_JSON.parent / "combo_holdings.json")
    if isinstance(data, dict):
        _attach_unlock_info_to_payload(data, keys=("holdings", "items", "buy"))
    return jsonify(data or {"holdings": [], "message": "尚无组合清单; PC 端运行 export_combo_holdings.py"})


@app.route("/api/combo_holdings_history")
def api_combo_holdings_history():
    """各演进版本的买入清单快照(每版一份, 配合演进历史回看)."""
    data = _read_json(COMBO_JSON.parent / "combo_holdings_history.json")
    return jsonify({"versions": data if isinstance(data, list) else []})


def _pg_code6(value):
    import re
    m = re.search(r"(\d{6})", str(value or ""))
    return m.group(1) if m else str(value or "").strip().upper()


def _pg_rows(payload, keys=("holdings", "items", "buy", "basket")):
    if not isinstance(payload, dict):
        return []
    for key in keys:
        rows = payload.get(key)
        if isinstance(rows, list):
            return [x for x in rows if isinstance(x, dict)]
    return []


def _pg_add_candidate(book, row, source, source_score=10, default_weight=2.0):
    code6 = _pg_code6(row.get("code") or row.get("ts_code") or row.get("symbol"))
    if not code6:
        return
    item = book.setdefault(code6, {
        "code": row.get("code") or code6,
        "code6": code6,
        "name": row.get("name") or row.get("股票简称") or "",
        "sources": [],
        "source_score": 0,
        "base_pct": 0.0,
        "raw": [],
    })
    if row.get("name") and not item.get("name"):
        item["name"] = row.get("name")
    if source not in item["sources"]:
        item["sources"].append(source)
    item["source_score"] += source_score
    try:
        weight = float(row.get("weight_pct") if row.get("weight_pct") is not None else default_weight)
    except Exception:
        weight = default_weight
    item["base_pct"] = max(float(item.get("base_pct") or 0), weight)
    item["raw"].append(row)


def _pg_read_data(data_dir, filename):
    return _read_json(data_dir / filename) or _read_json(Path(__file__).parent / "data" / filename)


def _pg_avoid_codes(data_dir):
    sources = {
        "hot": ("hot_avoid.json", "热榜拥挤"),
        "margin": ("margin_avoid.json", "毛利率恶化"),
        "fraud": ("fraud_avoid.json", "财务异常"),
        "investigation": ("investigation_avoid.json", "立案调查"),
        "lhb": ("lhb_avoid.json", "龙虎榜净卖出"),
        "leverage": ("leverage_avoid.json", "融资透支"),
        "snowball": ("snowball_avoid.json", "雪球风险"),
        "event": ("event_avoid.json", "事件避雷"),
    }
    out = {}
    for key, (filename, label) in sources.items():
        payload = _pg_read_data(data_dir, filename) or {}
        for row in _eventrisk_rows(payload):
            if not isinstance(row, dict):
                continue
            # Exporters keep inactive rows for audit/history.  Only explicit
            # boolean expiry markers suppress a risk flag so legacy payloads
            # that predate these fields retain their original behaviour.
            if row.get("in_window") is False:
                continue
            if key == "investigation" and row.get("in_blacklist") is False:
                continue
            if key == "snowball" and row.get("expired") is True:
                continue
            code6 = _pg_code6(row.get("code") or row.get("ts_code") or row.get("symbol"))
            if code6:
                out.setdefault(code6, []).append({"key": key, "label": label, "row": row})
    return out


def _pg_load_market_risk(data_dir):
    snap = _pg_read_data(data_dir, "top_risk_snapshot.json")
    if isinstance(snap, dict) and isinstance(snap.get("market"), dict):
        market = snap.get("market") or {}
    else:
        try:
            market = (_build_top_risk_report() or {}).get("market") or {}
        except Exception as exc:
            return {"level": "unknown", "score": 0, "message": f"见顶风险不可用: {exc}", "multiplier": 1.0}
    level = market.get("level") or "unknown"
    score = int(market.get("score") or 0)
    multiplier = 0.5 if level == "high" or score >= 80 else (0.75 if level == "watch" or score >= 35 else 1.0)
    return {**market, "level": level, "score": score, "multiplier": multiplier}


def _pg_unlock_days(unlock_info):
    days = []
    if not isinstance(unlock_info, dict):
        return days
    for key in ("transfer", "placement", "other"):
        for row in unlock_info.get(key) or []:
            value = row.get("days_to_unlock")
            if isinstance(value, int):
                days.append((key, value, row))
    return sorted(days, key=lambda x: x[1])


def _build_portfolio_guard_report():
    data_dir = PREDICT_JSON.parent
    book = {}

    combo = _pg_read_data(data_dir, "combo_holdings.json") or _read_json(COMBO_JSON.parent / "combo_holdings.json") or {}
    for row in _pg_rows(combo, keys=("holdings", "items", "buy")):
        _pg_add_candidate(book, row, "组合清单", source_score=40, default_weight=3.0)

    if not book:
        pro = _pg_read_data(data_dir, "regime_advisor_pro.json") or {}
        for row in _pg_rows(pro.get("current") or {}, keys=("basket", "items")):
            _pg_add_candidate(book, row, "顾问Pro", source_score=35, default_weight=3.0)
        plus = _pg_read_data(data_dir, "advisor_pro_plus.json") or {}
        for key, source, score in (("enhanced_buy", "滚动业绩增强", 25), ("event_candidates", "滚动业绩候补", 15)):
            for row in plus.get(key) or []:
                _pg_add_candidate(book, row, source, source_score=score, default_weight=2.0)
        runup = _pg_read_data(data_dir, "runup.json") or {}
        for key, source in (("buy", "业绩抢跑"), ("buy_post", "公告后漂移")):
            for row in runup.get(key) or []:
                _pg_add_candidate(book, row, source, source_score=18, default_weight=1.5)

    rows = list(book.values())
    _attach_unlock_info(rows)
    avoid = _pg_avoid_codes(data_dir)
    market = _pg_load_market_risk(data_dir)

    items = []
    for item in rows:
        code6 = item["code6"]
        reasons = []
        warnings = []
        veto = False
        haircut = float(market.get("multiplier") or 1.0)
        if haircut < 1:
            warnings.append(f"市场风险{market.get('level')}，仓位乘数{haircut:.2f}")

        flags = avoid.get(code6) or []
        flag_keys = {x["key"] for x in flags}
        for flag in flags:
            warnings.append(flag["label"])
        if "investigation" in flag_keys or "fraud" in flag_keys:
            veto = True
            reasons.append("立案/财务异常，一票否决")
        if "hot" in flag_keys:
            haircut *= 0.8
        if "margin" in flag_keys:
            haircut *= 0.75
        if {"hot", "margin"}.issubset(flag_keys):
            haircut *= 0.6
            warnings.append("热榜+毛利恶化双亮")
        if "lhb" in flag_keys or "leverage" in flag_keys:
            haircut *= 0.85

        for kind, days, row in _pg_unlock_days(item.get("unlock_info"))[:2]:
            label = "询转/协转" if kind == "transfer" else ("定增" if kind == "placement" else "其他解禁")
            if days >= 0:
                warnings.append(f"{label}{days}天后解禁")
                if days <= 30:
                    haircut *= 0.35
                    reasons.append(f"{label}临近解禁")
                elif days <= 90:
                    haircut *= 0.7
            elif days >= -30:
                warnings.append(f"{label}已解禁{abs(days)}天")
                haircut *= 0.8

        base_pct = float(item.get("base_pct") or 2.0)
        target_pct = 0.0 if veto else round(max(0.0, base_pct * haircut), 2)
        if veto:
            action = "排除"
        elif target_pct <= 0.05:
            action = "暂缓"
        elif target_pct < base_pct * 0.65:
            action = "暂缓"
        elif target_pct < base_pct * 0.9:
            action = "降权"
        else:
            action = "买入" if any(x in item["sources"] for x in ("顾问Pro", "组合清单", "滚动业绩增强", "业绩抢跑")) else "持有"

        risk_penalty = round((1 - (target_pct / base_pct if base_pct else 0)) * 100, 1)
        items.append({
            "code": item.get("code"),
            "code6": code6,
            "name": item.get("name"),
            "sources": item.get("sources") or [],
            "source_score": int(item.get("source_score") or 0),
            "base_pct": round(base_pct, 2),
            "target_pct": target_pct,
            "haircut": round(haircut, 3),
            "risk_penalty": risk_penalty,
            "action": action,
            "reasons": reasons,
            "warnings": warnings,
            "unlock_info": item.get("unlock_info"),
        })

    action_order = {"买入": 0, "降权": 1, "持有": 2, "暂缓": 3, "排除": 4}
    items.sort(key=lambda x: (action_order.get(x["action"], 9), -x["source_score"], -x["target_pct"], x["code6"]))
    return {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "market": market,
        "summary": {
            "n_total": len(items),
            "n_buy": sum(1 for x in items if x["action"] == "买入"),
            "n_reduce": sum(1 for x in items if x["action"] == "降权"),
            "n_wait": sum(1 for x in items if x["action"] == "暂缓"),
            "n_exclude": sum(1 for x in items if x["action"] == "排除"),
            "gross_target_pct": round(sum(float(x.get("target_pct") or 0) for x in items), 2),
        },
        "items": items,
        "method": "先汇总现有买入清单，再叠加市场风险、询转/定增/其他解禁、避雷清单，输出目标仓位和动作。该页不改变原始清单。",
    }


@app.route("/portfolio-guard")
def portfolio_guard_page():
    return render_template("portfolio_guard.html")


@app.route("/api/portfolio_guard")
def api_portfolio_guard():
    return jsonify(_build_portfolio_guard_report())


@app.route("/chipmap")
def chipmap_page():
    """海力士映射: SK海力士涨>2% → 当日尾盘买A股半导体篮子、持1天 (小卫星信号)。"""
    return render_template("chipmap.html")


@app.route("/tech-external")
def tech_external_page():
    return render_template("tech_external.html")


@app.route("/cross-market")
def cross_market_page():
    return render_template("cross_market.html")


def _read_cross_market_storage():
    local_path = Path(__file__).resolve().parent / "data" / "cross_market_storage.json"
    return _read_json(CROSS_MARKET_STORAGE_JSON) or _read_json(local_path)


@app.route("/api/cross_market")
def api_cross_market():
    if request.args.get("sector", "storage") != "storage":
        return jsonify({"ok": False, "message": "首版仅支持存储"}), 400
    data = _read_cross_market_storage() or {
        "mode": "research", "upside": [], "downside": [], "holdings": [],
        "message": "尚无跨市场映射数据，请在PC端运行采集脚本",
    }
    if (data.get("data_health") or {}).get("status") != "ok":
        data["mode"] = "research"
        data.setdefault("gate", {})["allow_live"] = False
    return jsonify(data)


@app.route("/api/cross_market/chart")
def api_cross_market_chart():
    market = request.args.get("market", "")
    symbol = request.args.get("symbol", "")
    mode = request.args.get("mode", "intraday")
    data = _read_cross_market_storage() or {}
    chart = ((data.get("charts") or {}).get(market) or {}).get(symbol)
    if not chart or chart.get("mode") != mode:
        return jsonify({"ok": False, "message": "图表数据不可用"}), 404
    return jsonify({"ok": True, **chart})


@app.route("/api/chipmap")
def api_chipmap():
    data = _read_json(KOREA_SEMI_JSON)
    if data is None:
        return jsonify({"signal": "", "basket": [], "message": "尚无信号; PC 端运行 export_korea_semi.py"})
    return jsonify(data)


def _pct_from_hynix_payload(hynix: dict | None, korea: dict | None) -> float | None:
    if hynix and isinstance(hynix.get("cur_pct"), (int, float)):
        return float(hynix.get("cur_pct"))
    ret = (korea or {}).get("hynix_ret")
    if isinstance(ret, (int, float)):
        return float(ret) * 100.0
    return None


def _hynix_trend(points: list[dict]) -> str:
    vals = [p.get("pct") for p in points or [] if isinstance(p.get("pct"), (int, float))]
    if len(vals) < 4:
        return "unknown"
    recent = vals[-1] - vals[-4]
    if recent >= 0.7:
        return "rising"
    if recent <= -0.7:
        return "fading"
    return "flat"


def _tech_external_action(hynix_pct: float | None, trend: str) -> tuple[str, str]:
    if hynix_pct is None:
        return "watch", "等待海外锚数据刷新"
    if hynix_pct >= 2.0 and trend != "fading":
        return "trade", "海外锚站上阈值，A股半导体/存储映射可进入尾盘候选"
    if hynix_pct >= 2.0:
        return "watch", "海外锚达标但尾段回落，等A股承接确认"
    if hynix_pct >= 1.0:
        return "watch", "海外锚接近阈值，只观察不追高"
    return "avoid", "海外锚未达阈值，科技映射信号不足"


def _build_tech_external_signal() -> dict:
    korea = _read_json(KOREA_SEMI_JSON) or {}
    hynix = _read_json(HYNIX_INTRADAY_JSON) or {}
    cross = _read_cross_market_storage() or {}
    points = hynix.get("points") or []
    hynix_pct = _pct_from_hynix_payload(hynix, korea)
    trend = _hynix_trend(points)
    state, action = _tech_external_action(hynix_pct, trend)
    return {
        "ok": True,
        "state": state,
        "action": action,
        "updated": str(korea.get("updated") or hynix.get("ts") or ""),
        "anchors": {
            "hynix": {
                "symbol": "000660.KS",
                "name": "SK海力士",
                "date": hynix.get("date") or korea.get("hynix_date"),
                "pct": None if hynix_pct is None else round(hynix_pct, 2),
                "threshold": 2.0,
                "trend": trend,
                "points": points,
                "source_ts": hynix.get("ts") or korea.get("updated"),
            },
            "cross_market": {
                "mode": cross.get("mode") or "research",
                "leaders": cross.get("leaders") or [],
                "health": cross.get("data_health") or {},
            },
        },
        "baskets": {
            "semiconductor": korea.get("basket") or [],
            "storage": (cross.get("upside") or [])[:12],
        },
        "rules": [
            {"state": "trade", "text": "海力士涨幅>=2%且尾段不明显回落：尾盘关注半导体/存储映射篮子"},
            {"state": "watch", "text": "海力士达标但回落，或接近2%：只看A股承接，不追已大涨标的"},
            {"state": "avoid", "text": "海力士未达1%或海外链条走弱：不做科技映射冲动交易"},
        ],
        "risk": [
            "A股已先涨且缩量时，次日高开兑现风险高。",
            "该页是盘中候选信号，不替代仓位和止损规则。",
        ],
        "refresh_kind": "chipmap",
    }


@app.route("/api/tech-external")
def api_tech_external():
    return jsonify(_build_tech_external_signal())


@app.route("/api/chipmap/hynix_intraday")
def api_hynix_intraday():
    """SK海力士(000660.KS)当日分时 -> 距昨收%。主路: PC取Yahoo推JSON(NAS在华常403); 回退: NAS直连。"""
    import urllib.request, datetime as _dt
    kst_today = (_dt.datetime.utcnow() + _dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    pc = _read_json(HYNIX_INTRADAY_JSON)
    # PC推送只在"日期是今天(KST)"时才用; 过期则回退Yahoo实时(NAS可能403, 失败再兜底返回过期PC并标注)
    if pc and pc.get("ok") and pc.get("points") and pc.get("date") == kst_today:
        return jsonify({**pc, "src": "PC推送"})
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/000660.KS?interval=5m&range=5d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        r = json.load(urllib.request.urlopen(req, timeout=15))
        res = r["chart"]["result"][0]
        meta = res["meta"]
        ts = res["timestamp"]; q = res["indicators"]["quote"][0]["close"]
        # 按KST日分组; 最新日=今日分时, 前一交易日末值=昨收(比Yahoo的chartPreviousClose可靠)
        byday = {}
        for t, c in zip(ts, q):
            if c is None:
                continue
            kst = _dt.datetime.utcfromtimestamp(t) + _dt.timedelta(hours=9)
            byday.setdefault(kst.strftime("%Y-%m-%d"), []).append((kst.strftime("%H:%M"), float(c)))
        days = sorted(byday)
        if not days:
            return jsonify({"ok": False, "message": "海力士暂无分时(休市)"})
        today = days[-1]
        prev = [d for d in days if d < today]
        pre = byday[prev[-1]][-1][1] if prev else (meta.get("chartPreviousClose") or byday[today][0][1])
        bars = byday[today]
        pts = [{"t": tm, "pct": round((c / pre - 1) * 100, 2)} for tm, c in bars]
        cp = bars[-1][1]
        return jsonify({"ok": True, "date": today, "pre_close": pre, "cur_price": cp,
                        "cur_pct": round((cp / pre - 1) * 100, 2) if pre else None,
                        "market_state": meta.get("marketState"), "points": pts,
                        "ts": datetime.now().strftime("%H:%M:%S")})
    except Exception as e:
        # Yahoo失败(NAS在华常403): 兜底返回PC推的(即便过期), 标注stale让前端提示
        if pc and pc.get("ok") and pc.get("points"):
            stale = pc.get("date") != kst_today
            return jsonify({**pc, "src": "PC推送(过期)" if stale else "PC推送", "stale": stale,
                            "stale_msg": f"分时数据停在 {pc.get('date')}, PC推送已停(需PC端跑 export_hynix_intraday.py 或点刷新)" if stale else ""})
        return jsonify({"ok": False, "message": f"海力士分时取数失败: {e}"})


@app.route("/api/chipmap/semi_dipt")
def api_semi_dipt():
    """持仓半导体 低开高走做T低吸提示。验证(gate_semi_lowopen): 半导体低开<-0.5%, 当日开→收+0.30%/t2.6(单调)。
    仅对【持有底仓】的半导体: 今日低开>0.5% → 提示低吸做T(加仓→收盘卖等量老股, 当天兑现, 净仓不变)。"""
    semi = (_read_json(PREDICT_JSON.parent / "korea_semi.json") or {}).get("basket") or []
    semi6 = {_c6(b.get("code", "")): b.get("name") for b in semi if b.get("code")}
    held = [p for p in _load_positions() if _c6(p.get("code", "")) in semi6]
    if not held:
        return jsonify({"items": [], "n_held": 0, "message": "你的持仓里没有半导体篮子内的票(低开高走做T仅对持有底仓的半导体有意义)"})
    codes = [_code_to_ts(p.get("code", "")) for p in held]
    rt = _rt_quotes([c for c in codes if c])
    items = []
    for p, ts_code in zip(held, codes):
        q = rt.get(ts_code or "") or {}
        op = q.get("open") or 0; pre = q.get("pre_close") or 0; cur = q.get("price") or 0
        gap = (op / pre - 1) if (op and pre) else None
        cvo = (cur / op - 1) if (op and cur) else None
        c6 = _c6(p.get("code", ""))
        flag = gap is not None and gap < -0.005
        items.append({"code": p.get("code"), "name": semi6.get(c6) or p.get("name") or c6,
                      "open": round(op, 3) if op else None, "pre_close": round(pre, 3) if pre else None,
                      "price": round(cur, 3) if cur else None,
                      "gap": round(gap * 100, 2) if gap is not None else None,
                      "cur_vs_open": round(cvo * 100, 2) if cvo is not None else None,
                      "dipt": flag,
                      "tip": ("低开高走概率高,可低吸做T(加仓→收盘卖等量底仓,当天兑现)" if flag else "未低开>0.5%,无做T提示")})
    items.sort(key=lambda x: (not x["dipt"], x["gap"] if x["gap"] is not None else 99))
    return jsonify({"updated": datetime.now().strftime("%H:%M:%S"), "n_held": len(items),
                    "n_dipt": sum(1 for x in items if x["dipt"]), "items": items,
                    "note": "验证: 半导体低开<-0.5%当日开→收+0.30%/t2.6(2019-26单调)。做T=有底仓者低开加仓、收盘卖等量老股(T+1合规、净仓不变)。regime依赖(震荡市强、单边牛弱), +0.3%/次需纪律。"})


@app.route("/eventstop")
def eventstop_page():
    """事件止损线: 事件腿买入后画 事件日最低价线 + 2ATR止损线, 跌破=市场不认可→3日内强平。"""
    return render_template("eventstop.html")


@app.route("/api/eventstop/holdings")
def api_eventstop_holdings():
    """持仓批量止损信息: 每只持仓(锚=买入日)的 -10%止损/吊灯止盈/现价/状态, 给止损页直接展示。"""
    out = []
    for p in _load_positions():
        try:
            c = p.get("code", "")
            buy_date = str(p.get("date", "") or "")
            r = _eventstop_calc(c, buy_date, p.get("cost"))
            if not isinstance(r, dict) or not r.get("ok"):
                continue
            r.pop("k", None)   # 批量不带K线(省流量)
            r["name"] = r.get("name") or p.get("name") or ""   # 优先stock_meta查的简称
            r["buy_date"] = buy_date or r.get("ev_date")        # 你录入的买入日(没填则取最新交易日)
            r["cost"] = p.get("cost"); r["qty"] = p.get("qty")
            out.append(r)
        except Exception as _e:
            log.warning(f"eventstop holdings skip {p.get('code')}: {_e}")
            continue
    # 排序: 已触发(止损>止盈>警示)置顶
    def _rank(x):
        return (0 if x.get("broke_pct") else (1 if x.get("chand_broke") else (2 if x.get("broke_evlow") else 3)))
    out.sort(key=_rank)
    return jsonify({"items": out, "n": len(out),
                    "n_stop": sum(1 for x in out if x.get("broke_pct")),
                    "n_tp": sum(1 for x in out if not x.get("broke_pct") and x.get("chand_broke"))})


@app.route("/api/eventstop")
def api_eventstop():
    """给 code + 事件日(+买入价) → 日K + -10%止损/吊灯止盈线 + 当前状态。支持 代码/拼音首字母/名称。"""
    raw = (request.args.get("code") or "").strip()
    ev = (request.args.get("event_date") or "").strip()
    return jsonify(_eventstop_calc(raw, ev, request.args.get("buy_price"), request.args.get("chand_mult")))


def _eventstop_calc(raw, ev, buy_price_raw, chand_mult=1.5):
    code = _resolve_to_tscode(raw) or _code_to_ts(raw)   # 支持 代码/拼音首字母/名称
    ev = (ev or "").strip()
    if ev and "-" not in ev and len(ev) >= 8:
        ev = ev[:4] + "-" + ev[4:6] + "-" + ev[6:8]
    if not code:
        return {"ok": False, "message": "代码无法识别"}
    # code 应是 600183.SH; 若解析异常无点则兜底用 _code_to_ts 再转, 仍不行返回
    if "." not in code:
        code = _code_to_ts(code) or ""
    if "." not in code:
        return {"ok": False, "message": "代码无法识别"}
    n6, ex = code.split("."); qcode = ("sh" if ex == "SH" else "sz" if ex == "SZ" else "bj") + n6
    d = _daily_ohlc(qcode, days=160)
    if not d or not len(d["dates"]):
        return {"ok": False, "message": "无日线数据"}
    dates = list(d["dates"]); H = d["high"]; L = d["low"]; C = d["close"]; O = d.get("open")
    n = len(dates)
    tr = [0.0] + [max(H[i]-L[i], abs(H[i]-C[i-1]), abs(L[i]-C[i-1])) for i in range(1, n)]   # tr[i]对齐dates[i]
    # 事件日定位(取 >= ev 的第一个交易日)
    ev_i = next((i for i, x in enumerate(dates) if x >= ev), None) if ev else (n - 1)
    if ev_i is None:
        ev_i = n - 1
    ev_low = float(L[ev_i]); ev_date = dates[ev_i]
    # ATR(14): 用【事件日】那天往前14日的ATR — 止损线建仓即钉死, 不随今日浮动(军规: 买入价-2N是固定红线)
    _w = [t for t in tr[max(1, ev_i-13):ev_i+1] if t > 0]
    atr = float(np.mean(_w)) if _w else (float(np.mean([t for t in tr if t > 0])) if any(tr) else 0)
    try:
        buy = float(buy_price_raw or 0)
    except (TypeError, ValueError):
        buy = 0
    if buy <= 0:
        buy = float(C[ev_i])      # 没填买入价=用事件日收盘
    # A股优化(gate_stoploss_tune验证): 主止损=固定-10%、收盘价触发(躲下影线插针)。
    # 防灾最差锁-10%(不扛到-89%)、被洗少、均收益几乎不降(+2.36% vs 不止损+2.40%)。2-ATR当参考(对高波动股偏宽)。
    pct_stop = round(buy * 0.90, 3)        # 固定-10%主止损
    atr_stop = round(buy - 2 * atr, 3)     # 2-ATR参考线
    last = float(C[-1]); last_low = float(L[-1])
    # 吊灯止盈(gate_chandelier_tune调优: HH-yATR, 收盘触发, 浮盈>5%才启动最优): 收益+2.86%/胜60%/中位+2.35%, 防过山车锁利
    try:
        cm = float(chand_mult or 1.5)
    except (TypeError, ValueError):
        cm = 1.5
    if cm not in (1.0, 1.5, 2.0):
        cm = 1.5
    START_PF = 0.05   # 浮盈>5%才启动吊灯(实测优于2%: 让早期波动跑一跑)
    hh = float(buy); chand_broke = None
    for i in range(ev_i + 1, n):
        if float(C[i]) > hh: hh = float(C[i])   # 转Python float, 防numpy类型混入(jsonify不认numpy.bool_/float32)
        chand_line = hh - cm*atr
        if chand_broke is None and hh > buy*(1+START_PF) and float(C[i]) <= chand_line and float(C[i]) > buy:
            chand_broke = dates[i]
    chand_active = bool(hh > buy*(1+START_PF))   # bool()转Python bool(numpy.bool_不能JSON序列化)
    chand_stop = round(hh - cm*atr, 3) if chand_active else None   # 未激活=不显示吊灯线
    hh = round(hh, 3)
    # 跌破判定(事件日之后, 全用收盘价C — A股下影线长, 盘中触发会被插针洗)
    broke_pct = None; broke_evlow = None; broke_atr = None; broke_pct_i = n - 1
    for i in range(ev_i + 1, n):
        if broke_pct is None and C[i] <= pct_stop:
            broke_pct = dates[i]; broke_pct_i = i
        if broke_evlow is None and C[i] < ev_low:
            broke_evlow = dates[i]
        if broke_atr is None and C[i] <= atr_stop:
            broke_atr = dates[i]
    status = "持有中(未破线)"
    # 强平警报只看 -10%硬止损(实测最优, 防灾不被洗)。事件日最低线太敏感(离买入价仅几个点)→只画不报警。
    if broke_pct:
        deadline = str(dates[min(broke_pct_i + 3, n - 1)])   # 用循环里记的索引, 不再 dates.index() (避免numpy.str_等边界报错)
        status = f"⚠️收盘跌破-10%硬止损({broke_pct}) → {deadline}前强平(3日内), 绝不摊平"
    elif chand_broke:
        status = f"💰收盘跌破吊灯止盈线({chand_broke}) → 已锁利, 建议了结浮盈(从高点{hh}回落1.5ATR)"
    elif broke_evlow:
        status = f"⚠️收盘跌破事件日最低({broke_evlow}) — 仅警示, 离-10%硬止损还有空间, 看基本面决定"
    try:
        _nm = (_meta_for_codes([code]).get(code) or {}).get("name", "")
    except Exception:
        _nm = ""
    return {"ok": True, "code": code, "name": _nm, "ev_date": ev_date, "buy": round(buy, 3),
                    "ev_low": round(ev_low, 3), "atr": round(atr, 3), "atr_stop": atr_stop, "pct_stop": pct_stop,
                    "hh": hh, "chand_stop": chand_stop, "chand_active": chand_active, "chand_broke": chand_broke, "chand_mult": cm,
                    "last": round(last, 3), "last_low": round(last_low, 3),
                    "broke_pct": broke_pct, "broke_evlow": broke_evlow, "broke_atr": broke_atr, "status": status,
                    "k": {
                        "dates": dates,
                        "ohlc": [[float(O[i]) if O is not None else float(C[i]), float(C[i]), float(L[i]), float(H[i])] for i in range(n)],
                        "adjust": "qfq",
                        "source": "qlib",
                        "quality": dict(d.get("quality") or {"ohlc_envelope_repairs": 0}),
                    }}


@app.route("/ipo")
def ipo_page():
    """打新提醒: 今日可申购/即将申购/近期上市 新股 (账户级白捡增厚, 逢新必打)。"""
    return render_template("ipo.html" if _has_internal_access() else "member_ipo.html")


@app.route("/api/ipo")
def api_ipo():
    data = _read_json(IPO_JSON)
    if data is None:
        return jsonify({"today_buy": [], "soon_buy": [], "message": "尚无打新数据; PC 端运行 export_ipo.py 生成 ipo.json"})
    if not _has_internal_access():
        factual_keys = {"updated", "today", "today_buy", "soon_buy", "just_listed"}
        data = {key: value for key, value in data.items() if key in factual_keys}
    return jsonify(data)


@app.route("/repo")
def repo_page():
    """回购事件腿(第四sleeve): 中证1000成分 回购公告后持有60交易日 每日清单。"""
    return render_template("repo.html")


@app.route("/api/repo")
def api_repo():
    data = _read_json(REPO_JSON)
    if data is None:
        return jsonify({"updated": "", "holdings": [], "buy_today": [],
                        "message": "尚无回购清单; PC 端运行 export_repo.py 生成 repo.json"})
    return jsonify(data)


def _event_ts_code(row):
    raw = str(row.get("ts_code") or row.get("code") or "").strip()
    if "." in raw:
        return raw.upper()
    digits = "".join(x for x in raw if x.isdigit())[:6]
    if len(digits) != 6:
        return ""
    return digits + (".SH" if digits[0] in "569" else ".SZ" if digits[0] in "03" else ".BJ")


def _event_date(row):
    raw = str(row.get("ann_date") or row.get("report_date") or row.get("date") or "").strip()
    raw = raw.replace("-", "")[:8]
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


def _repo_signal_date(row):
    for key in ("ann_date", "announcement_date", "report_date", "date", "pub_date", "trade_date"):
        raw = str(row.get(key) or "").strip()
        if not raw:
            continue
        raw = raw.replace("-", "").replace("/", "").replace(".", "")[:8]
        try:
            return datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            continue
    return None


def _repo_signal_rows(repo_payload=None):
    rows = []
    if isinstance(repo_payload, dict):
        for key in ("items", "buy_today", "holdings", "watch", "buy", "signals"):
            for row in repo_payload.get(key) or []:
                if isinstance(row, dict):
                    rows.append((row, row.get("source") or "回购数据"))
        return rows

    repo_path = REPO_JSON if REPO_JSON.exists() else Path(__file__).parent / "data" / "repo.json"
    cancel_path = PREDICT_JSON.parent / "repo_cancel.json"
    if not cancel_path.exists():
        cancel_path = Path(__file__).parent / "data" / "repo_cancel.json"
    repo = _read_json(repo_path) or {}
    cancel = _read_json(cancel_path) or {}
    for row in cancel.get("items") or []:
        if isinstance(row, dict):
            rows.append((row, row.get("source") or "回购注销"))
    for key in ("buy_today", "holdings", "items", "watch", "buy", "signals"):
        for row in repo.get(key) or []:
            if isinstance(row, dict):
                rows.append((row, row.get("source") or "回购腿"))
    return rows


def _build_repo_signal_map(repo_payload=None):
    today = datetime.now().date()
    by_code = {}
    for row, source in _repo_signal_rows(repo_payload):
        code = _event_ts_code(row)
        if not code:
            continue
        ann_date = _repo_signal_date(row)
        age_days = (today - ann_date).days if ann_date else None
        title = str(row.get("title") or row.get("summary") or row.get("reason") or "")
        proc = row.get("proc") or row.get("type") or ("注销型回购" if "注销" in title else "回购")
        item = {
            "hit": True,
            "label": "回购+业绩",
            "code": code,
            "name": row.get("name") or "",
            "ann_date": ann_date.isoformat() if ann_date else "",
            "age_days": age_days,
            "proc": proc,
            "title": title,
            "source": row.get("source") or source,
            "announcement_url": row.get("announcement_url") or row.get("url") or "",
            "level": "focus" if age_days is None or age_days <= 180 else "watch",
        }
        old = by_code.get(code)
        old_date = _repo_signal_date(old or {}) if old else None
        if old is None or (ann_date or date.min) >= (old_date or date.min):
            by_code[code] = item
    return by_code


def _attach_repo_resonance_to_runup(payload, repo_payload=None):
    if not isinstance(payload, dict):
        return payload
    repo_by_code = _build_repo_signal_map(repo_payload)
    focus = {}

    def current_period_tokens():
        today = datetime.now().date()
        year = today.year
        if today.month in (7, 8, 9):
            return {f"{year}0630", f"{year}-06-30", f"{year}H1", "H1", "中报", "半年度"}
        if today.month in (10, 11):
            return {f"{year}0930", f"{year}-09-30", f"{year}Q3", "Q3", "三季报", "第三季度"}
        if today.month in (1, 2, 3, 4):
            return {f"{year - 1}1231", f"{year - 1}-12-31", f"{year - 1}A", "年报", "年度报告",
                    f"{year}0331", f"{year}-03-31", f"{year}Q1", "Q1", "一季报", "第一季度"}
        return {f"{year}0331", f"{year}-03-31", f"{year}Q1", "Q1", "一季报", "第一季度"}

    period_tokens = current_period_tokens()

    def is_current_period(row):
        text = " ".join(str(row.get(key) or "") for key in (
            "period", "end_date", "report_period", "report_date", "title", "type"
        ))
        compact = text.replace("-", "").replace("/", "").replace(".", "")
        if any(token.replace("-", "") in compact or token in text for token in period_tokens):
            return True
        # 没有明确报告期的行保留给抢跑主清单；明确写了过期报告期的行剔除。
        stale_tokens = ("20260331", "2026-03-31", "2026Q1", "Q1", "一季报", "第一季度")
        if datetime.now().month in (7, 8, 9) and any(token in compact or token in text for token in stale_tokens):
            return False
        return not any(row.get(key) for key in ("period", "end_date", "report_period"))

    def is_earnings_row(row):
        if not isinstance(row, dict):
            return False
        typ = str(row.get("type") or "")
        if typ in {"预增", "略增", "扭亏", "续盈", "减亏", "预减", "略减", "首亏", "续亏"}:
            return True
        title = str(row.get("title") or row.get("event") or row.get("source") or "")
        if any(word in title for word in ("业绩预告", "业绩快报", "季度报告", "半年度报告", "年度报告")):
            return True
        earnings_keys = (
            "p_chg_min", "p_chg_max", "yoy", "n_income", "revenue",
            "net_min", "net_max", "dedt_lo", "dedt_hi", "q1_yoy", "q2_yoy",
            "profit_dedt", "grossprofit_margin", "roe",
        )
        return any(row.get(key) not in (None, "") for key in earnings_keys)

    def attach_rows(rows):
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not is_earnings_row(row) or not is_current_period(row):
                continue
            code = _event_ts_code(row)
            sig = repo_by_code.get(code)
            if not sig:
                continue
            row["repo_signal"] = dict(sig)
            row["repo_resonance"] = True
            focus.setdefault(code, {
                "code": row.get("code") or code,
                "name": row.get("name") or sig.get("name") or "",
                "type": row.get("type") or "",
                "ann_date": row.get("ann_date") or row.get("report_date") or row.get("date") or "",
                "period": row.get("period") or row.get("end_date") or "",
                "repo_signal": dict(sig),
                "unlock_info": row.get("unlock_info"),
            })

    for key in ("buy", "watch", "buy_post", "holdings", "items"):
        attach_rows(payload.get(key))
    events = payload.get("events")
    if isinstance(events, dict):
        for rows in events.values():
            attach_rows(rows)
    if focus:
        payload["repo_focus"] = sorted(
            focus.values(),
            key=lambda x: ((x.get("repo_signal") or {}).get("age_days")
                           if isinstance((x.get("repo_signal") or {}).get("age_days"), int) else 999999,
                           x.get("code") or ""),
        )
    else:
        payload["repo_focus"] = []
    return payload


def _build_unlock_focus_items(limit=None):
    focus_cutoff = _eventrisk_add_months(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0), -8)
    rows = []
    for name in (
        "cninfo_transfer.json", "share_transfer.json", "equity_change.json", "agreement_transfer.json",
        "sse_transfer.json", "shse_transfer.json", "sh_transfer.json",
        "sse_equity_change.json", "shse_equity_change.json", "sh_equity_change.json",
        "sse_agreement_transfer.json", "shse_agreement_transfer.json", "sh_agreement_transfer.json",
    ):
        payload = _eventrisk_load_json(name) or {}
        for row in _eventrisk_rows(payload):
            if isinstance(row, dict):
                item = dict(row)
                item["dataset"] = item.get("dataset") or name
                rows.append(item)
    transfer = _eventrisk_sort_transfer(_eventrisk_enrich_transfer(rows))

    unlock_rows = []
    for name in (
        "cninfo_unlock.json", "unlock_calendar.json", "share_unlock.json", "restricted_unlock.json",
        "sse_unlock.json", "shse_unlock.json", "sh_unlock.json",
        "sse_restricted_unlock.json", "shse_restricted_unlock.json", "sh_restricted_unlock.json",
    ):
        payload = _eventrisk_load_json(name) or {}
        for row in _eventrisk_rows(payload):
            if isinstance(row, dict):
                item = dict(row)
                item["dataset"] = item.get("dataset") or name
                unlock_rows.append(item)
    other = _eventrisk_sort_by_unlock(_eventrisk_enrich_unlock(unlock_rows))

    items = []
    seen = set()
    for kind, source_rows in (("协转/询价转让解禁", transfer), ("其他限售解禁", other)):
        for row in source_rows:
            days = row.get("days_to_unlock")
            if not isinstance(days, int):
                continue
            code = "".join(ch for ch in str(row.get("code") or row.get("ts_code") or row.get("symbol") or "") if ch.isdigit())[:6]
            if not code:
                continue
            key = (code, kind, row.get("unlock_date"), row.get("title") or "")
            if key in seen:
                continue
            seen.add(key)
            ann_text = row.get("ann_date") or row.get("date") or row.get("announcement_date") or ""
            ann_dt = _eventrisk_parse_date(ann_text)
            focus_window = bool(ann_dt is not None and ann_dt >= focus_cutoff)
            items.append({
                "code": code,
                "name": row.get("name") or row.get("secName") or row.get("securityName") or row.get("stock_name") or "",
                "kind": kind,
                "unlock_date": row.get("unlock_date") or "",
                "days_to_unlock": days,
                "ann_date": ann_text,
                "title": row.get("title") or row.get("summary") or row.get("event") or "",
                "url": row.get("url") or row.get("announcement_url") or "",
                "dataset": row.get("dataset") or "",
                "status": "expired" if days < 0 else ("high" if days <= 30 else ("watch" if days <= 90 else "future")),
                "focus_window": focus_window,
                "focus_label": "最近8个月重点" if focus_window else "",
            })
    codes = []
    for x in items:
        code = x.get("code")
        if not code:
            continue
        codes.extend([code, f"{code}.SZ", f"{code}.SH", f"{code}.BJ"])
    meta = _meta_for_codes(codes)

    def title_name(title):
        text = str(title or "")
        if "关于" in text:
            text = text.split("关于", 1)[0]
        for suffix in ("股份有限公司", "有限责任公司", "有限公司"):
            text = text.replace(suffix, "")
        return text.strip()[:12]

    for item in items:
        code = item.get("code") or ""
        item["name"] = (
            item.get("name")
            or (meta.get(code) or {}).get("name")
            or (meta.get(f"{code}.SZ") or {}).get("name")
            or (meta.get(f"{code}.SH") or {}).get("name")
            or (meta.get(f"{code}.BJ") or {}).get("name")
            or title_name(item.get("title"))
            or ""
        )
    items.sort(key=lambda x: (
        0 if isinstance(x.get("days_to_unlock"), int) and x["days_to_unlock"] >= 0 else 1,
        abs(x.get("days_to_unlock", 999999)) if isinstance(x.get("days_to_unlock"), int) else 999999,
        x.get("code", ""),
        x.get("unlock_date", ""),
    ))
    return items[:limit] if limit else items


@app.route("/api/event_resonance")
def api_event_resonance():
    """业绩事件 × 同行业回购的当前交叉共振；仅使用当时已披露事件。"""
    runup_path = RUNUP_JSON if RUNUP_JSON.exists() else Path(__file__).parent / "data" / "runup.json"
    repo_path = REPO_JSON if REPO_JSON.exists() else Path(__file__).parent / "data" / "repo.json"
    cancel_path = PREDICT_JSON.parent / "repo_cancel.json"
    if not cancel_path.exists():
        cancel_path = Path(__file__).parent / "data" / "repo_cancel.json"
    runup = _read_json(runup_path) or {}
    repo = _read_json(repo_path) or {}
    cancel = _read_json(cancel_path) or {}

    positive_types = {"预增", "略增", "扭亏", "续盈", "减亏"}
    evidence = []
    forecast_rows = (runup.get("events") or {}).get("forecast") or []
    guidance = {}
    for row in forecast_rows:
        code = _event_ts_code(row)
        period = str(row.get("period") or row.get("end_date") or "")
        lower, upper = row.get("p_chg_min"), row.get("p_chg_max")
        try:
            lower, upper = float(lower), float(upper)
        except (TypeError, ValueError):
            lower, upper = None, None
        if code and period and lower is not None and upper is not None:
            guidance[(code, period)] = {"lower": lower, "upper": upper,
                                        "mid": (lower + upper) / 2}
    for source, rows, base in [
        ("公告后漂移", runup.get("buy_post") or [], 35),
        ("业绩预告", forecast_rows, 30),
        ("业绩快报", (runup.get("events") or {}).get("express") or [], 30),
    ]:
        for row in rows:
            item_source, item_base = source, base
            typ = str(row.get("type") or "")
            yoy = row.get("yoy")
            if source == "业绩预告" and typ not in positive_types:
                continue
            if source == "业绩快报" and yoy is not None:
                try:
                    if float(yoy) <= 0:
                        continue
                except (TypeError, ValueError):
                    pass
            code = _event_ts_code(row)
            if not code:
                continue
            period = str(row.get("period") or row.get("end_date") or "")
            if source == "业绩快报" and yoy is not None:
                guide = guidance.get((code, period))
                try:
                    actual_yoy = float(yoy)
                except (TypeError, ValueError):
                    actual_yoy = None
                if guide is not None and actual_yoy is not None:
                    if actual_yoy < guide["mid"]:
                        continue  # 低于预告中值，没有新增正面信息
                    width = max(1.0, guide["upper"] - guide["lower"])
                    position = (actual_yoy - guide["lower"]) / width
                    item_source = "快报高于预告中值"
                    item_base += min(15, max(0, position - .5) * 20)
            strength = item_base
            growth = row.get("p_chg_min") if row.get("p_chg_min") is not None else yoy
            try:
                strength += min(15, max(0, float(growth)) / 10)
            except (TypeError, ValueError):
                pass
            evidence.append({"code": code, "name": row.get("name", ""), "source": item_source,
                             "event_date": _event_date(row), "strength": round(strength, 1),
                             "type": typ, "growth": growth})
    # 同公司保留信息更强的一条，避免预告/建仓重复计分。
    ev_by_code = {}
    for item in evidence:
        if item["code"] not in ev_by_code or item["strength"] > ev_by_code[item["code"]]["strength"]:
            ev_by_code[item["code"]] = item

    # 巨潮注销公告优先；同代码同时存在时不被后面的结构化回购记录覆盖。
    repo_rows = ([(x, "巨潮资讯") for x in (cancel.get("items") or [])]
                 + [(x, "Tushare/回购腿") for x in
                    ((repo.get("buy_today") or []) + (repo.get("holdings") or []))])
    repo_by_code = {}
    for row, source in repo_rows:
        code = _event_ts_code(row)
        if code and code not in repo_by_code:
            repo_by_code[code] = {"code": code, "name": row.get("name", ""),
                                  "ann_date": _event_date(row),
                                  "proc": row.get("proc") or ("注销型" if row.get("title") else "回购"),
                                  "title": row.get("title", ""),
                                  "announcement_url": row.get("announcement_url", ""),
                                  "announcement_source": row.get("source") or source}
    all_codes = list(ev_by_code) + list(repo_by_code)
    meta = _meta_for_codes(all_codes)
    by_industry = {}
    for item in ev_by_code.values():
        info = meta.get(item["code"]) or {}
        item["name"] = item["name"] or info.get("name", "")
        item["industry"] = info.get("industry", "")
        if item["industry"]:
            by_industry.setdefault(item["industry"], []).append(item)

    today = datetime.now().date()
    pairs = []
    for r in repo_by_code.values():
        info = meta.get(r["code"]) or {}
        industry = info.get("industry", "")
        r["name"] = r["name"] or info.get("name", "")
        peers = [x for x in by_industry.get(industry, []) if x["code"] != r["code"]]
        for e in peers:
            gap = abs((r["ann_date"] - e["event_date"]).days) if r["ann_date"] and e["event_date"] else None
            if gap is not None and gap > 45:
                continue
            repo_age = (today - r["ann_date"]).days if r["ann_date"] else 999
            timing = 15 if gap is not None and gap <= 10 else 10 if gap is not None and gap <= 20 else 5
            recency = 20 if repo_age <= 30 else 12 if repo_age <= 60 else 5
            multi = min(10, max(0, len(peers) - 1) * 5)
            score = min(100, round(e["strength"] + timing + recency + multi, 1))
            pairs.append({
                "industry": industry, "score": score,
                "level": "重点" if score >= 75 else "观察" if score >= 60 else "线索",
                "earnings_code": e["code"], "earnings_name": e["name"],
                "earnings_source": e["source"], "earnings_type": e["type"],
                "earnings_growth": e["growth"],
                "earnings_date": e["event_date"].isoformat() if e["event_date"] else "",
                "repo_code": r["code"], "repo_name": r["name"], "repo_proc": r["proc"],
                "repo_date": r["ann_date"].isoformat() if r["ann_date"] else "",
                "repo_source": r["announcement_source"],
                "repo_title": r["title"], "repo_url": r["announcement_url"],
                "event_gap_days": gap, "peer_count": len(peers),
            })
    pairs.sort(key=lambda x: (-x["score"], x["industry"], x["repo_code"]))
    return jsonify({
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "items": pairs, "n": len(pairs), "n_focus": sum(x["score"] >= 75 for x in pairs),
        "note": "跨事件同行业共振为研究排序，不是独立买入信号；行业字段来自stock_meta，历史有效性需样本外回测。",
    })


@app.route("/api/event_resonance/backtest")
def api_event_resonance_backtest():
    path = PREDICT_JSON.parent / "event_resonance_backtest.json"
    if not path.exists():
        path = Path(__file__).parent / "data" / "event_resonance_backtest.json"
    data = _read_json(path)
    if not data:
        return jsonify({"message": "尚未运行历史回测：python scripts/backtest_event_resonance.py"})
    if request.args.get("detail") != "1":
        data = {k: v for k, v in data.items() if k != "rows"}
    return jsonify(data)


def _ipo_data():
    path = IPO_JSON if IPO_JSON.exists() else Path(__file__).parent / "data" / "ipo.json"
    return _read_json(path) or {}


def _ipo_source_rows() -> tuple[dict, list[dict]]:
    data = _ipo_data()
    if not isinstance(data, dict):
        return {}, []
    rows = []
    for key in ("just_listed", "today_buy", "soon_buy"):
        values = data.get(key)
        if isinstance(values, list):
            rows.extend(item for item in values if isinstance(item, dict))
    return data, rows


def _ipo_date(raw):
    s = str(raw or "").strip().replace("-", "")[:8]
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None


@app.route("/new-stocks")
def new_stocks_page():
    return render_template("new_stocks.html")


@app.route("/api/new_stocks")
def api_new_stocks():
    """上市当天、近N日及即将上市的新股，合并IPO发行字段与stock_meta行业。"""
    include_internal = _has_internal_access()
    try:
        days = max(1, min(30, int(request.args.get("days", 7))))
    except ValueError:
        days = 7
    data, raw = _ipo_source_rows()
    today = datetime.now().date()
    by_code = {}
    for row in raw:
        code = _event_ts_code(row)
        if not code:
            continue
        old = by_code.get(code, {})
        by_code[code] = {**old, **row, "code": code}
    # stock_meta补充最新上市日期，防ipo.json只保留少量近期记录。
    try:
        with closing(_open_sqlite_readonly(STOCK_META_DB)) as conn:
            conn.row_factory = sqlite3.Row
            since = (today - timedelta(days=days + 10)).strftime("%Y-%m-%d")
            rows = conn.execute(
                "SELECT ts_code,name,industry,list_date FROM stock_meta "
                "WHERE list_status='L' AND list_date>=? ORDER BY list_date DESC",
                (since,),
            ).fetchall()
        for r in rows:
            item = by_code.setdefault(r["ts_code"], {"code": r["ts_code"]})
            item.update({k: item.get(k) or r[k] for k in ("name", "industry", "list_date")})
    except (OSError, sqlite3.Error) as exc:
        log.debug("new-stock metadata unavailable: %s", exc)
    meta = _meta_for_codes(list(by_code))
    items = []
    for code, row in by_code.items():
        info = meta.get(code) or {}
        list_raw = row.get("issue_date") or row.get("list_date")
        list_date = _ipo_date(list_raw)
        ipo_date = _ipo_date(row.get("ipo_date"))
        if list_date:
            delta = (today - list_date).days
            group = "today" if delta == 0 else "week" if 0 < delta <= days else "upcoming" if delta < 0 else "older"
        else:
            delta, group = None, "upcoming" if ipo_date and ipo_date >= today else "unknown"
        if group not in {"today", "week", "upcoming"}:
            continue
        c6 = _c6(code)
        report_path = PREDICT_JSON.parent / f"report_{c6}.json"
        if not report_path.exists():
            report_path = Path(__file__).parent / "data" / f"report_{c6}.json"
        items.append({
            "code": code, "c6": c6, "name": row.get("name") or info.get("name", ""),
            "industry": row.get("industry") or info.get("industry", ""),
            "board": row.get("board") or ("科创板" if c6.startswith("688") else
                     "创业板" if c6.startswith("30") else "北交所" if c6.startswith(("4", "8")) else "主板"),
            "list_date": list_date.isoformat() if list_date else "",
            "ipo_date": ipo_date.isoformat() if ipo_date else "",
            "days_since": delta, "group": group,
            "sub_code": row.get("sub_code"),
            "issue_price": row.get("price") or row.get("issue_price"),
            "issue_pe": row.get("pe") or row.get("issue_pe"),
            "amount": row.get("amount") or row.get("issue_amount"),
            "market_amount": row.get("market_amount") or row.get("online_amount"),
            "funds": row.get("funds") or row.get("funds_raised"),
            "ballot": row.get("ballot") or row.get("ballot_rate"),
            "report_ready": report_path.exists() if include_internal else False,
        })
    order = {"today": 0, "week": 1, "upcoming": 2}
    items.sort(key=lambda x: (order[x["group"]], -(x["days_since"] or 0), x["code"]))
    return jsonify({"updated": data.get("updated", ""), "today": today.isoformat(),
                    "days": days, "items": items,
                    "counts": {g: sum(x["group"] == g for x in items)
                               for g in ("today", "week", "upcoming")}})


@app.route("/api/new_stocks/detail")
def api_new_stock_detail():
    code = request.args.get("code") or ""
    c6 = _c6(code)
    if len(c6) != 6 or not c6.isdigit():
        return jsonify({"ok": False, "message": "代码无效"}), 400
    listing = api_new_stocks().get_json()
    item = next((x for x in listing.get("items", []) if x["c6"] == c6), None)
    if item is None:
        _data, source_rows = _ipo_source_rows()
        source = next((row for row in source_rows if _c6(_event_ts_code(row)) == c6), None)
        ts_code = _event_ts_code(source) if source else _resolve_to_tscode(c6)
        info = (_meta_for_codes([ts_code]).get(ts_code) or {}) if ts_code else {}
        item = {
            "code": ts_code or c6,
            "c6": c6,
            "name": (source or {}).get("name") or info.get("name", ""),
            "industry": (source or {}).get("industry") or info.get("industry", ""),
            "sub_code": (source or {}).get("sub_code"),
            "issue_price": (source or {}).get("price") or (source or {}).get("issue_price"),
            "issue_pe": (source or {}).get("pe") or (source or {}).get("issue_pe"),
        }
    report_path = PREDICT_JSON.parent / f"report_{c6}.json"
    if not report_path.exists():
        report_path = Path(__file__).parent / "data" / f"report_{c6}.json"
    report = _read_json(report_path)
    last = None
    ts_code = item.get("code")
    if ts_code and "." in ts_code:
        last = (_rt_quotes([ts_code]).get(ts_code) or {}).get("price")
    issue_price = item.get("issue_price")
    try:
        premium = round((float(last) / float(issue_price) - 1) * 100, 2) if last and issue_price else None
    except (TypeError, ValueError, ZeroDivisionError):
        premium = None
    financials = (report.get("fin_annual") or [])[-4:] if report else []
    summary = None
    if report and _has_internal_access():
        summary = {
            "updated": report.get("updated"), "overview": report.get("overview") or {},
            "conclusion": (report.get("llm") or {}).get("conclusion", ""),
            "landscape": (report.get("llm") or {}).get("landscape", ""),
            "moat": (report.get("llm") or {}).get("moat", ""),
            "risk": (report.get("llm") or {}).get("risk", ""),
            "fin_annual": financials,
            "mainbz": (report.get("mainbz") or [])[:8],
        }
    return jsonify({"ok": True, "item": item, "last": last, "premium_pct": premium,
                    "financials": financials,
                    "report_ready": bool(summary), "report": summary})


@app.route("/avoid")
def avoid_page():
    """避雷页: 毛利率同比恶化最严重的个股(流动性池)。验证过的负面剔除信号——空头侧alpha,落地形态=从多头剔除这些。"""
    return render_template("avoid.html")


@app.route("/api/avoid")
def api_avoid():
    data = _read_json(MARGIN_AVOID_JSON)
    if data is None:
        return jsonify({"as_of": "", "items": [], "n_universe": 0,
                        "message": "尚无避雷清单; PC 端运行 export_fundamentals.py 生成 margin_avoid.json"})
    return jsonify(data)


@app.route("/api/avoid/fraud")
def api_avoid_fraud():
    """财务造假避雷: Beneish-DSRI应收账款透支指数高=可能虚增收入。验证2625样本前向半年-1.05%/t=6.8。负面剔除信号。"""
    data = _read_json(FRAUD_AVOID_JSON)
    if data is None:
        return jsonify({"as_of": "", "items": [], "n_universe": 0,
                        "message": "尚无造假避雷清单; PC 端运行 export_fundamentals.py 生成 fraud_avoid.json"})
    return jsonify(data)


@app.route("/api/avoid/hot")
def api_avoid_hot():
    """热榜避雷: 当前同花顺热榜股=关注度透支, 未来20日跑输市场-3.2%(t=-14)。负面剔除/标红信号。"""
    data = _read_json(HOT_AVOID_JSON)
    if data is None:
        return jsonify({"as_of": "", "items": [], "n": 0,
                        "message": "尚无热榜避雷清单; PC 端运行 export_hot_avoid.py 生成 hot_avoid.json"})
    return jsonify(data)


@app.route("/api/avoid/events")
def api_avoid_events():
    """事件避雷(自动扫描器筛出+精测robust): 高管辞职/换会计师/减持计划/员工持股草案。"""
    data = _read_json(PREDICT_JSON.parent / "event_avoid.json")
    if data is None:
        return jsonify({"cats": {}, "message": "尚无数据; PC运行 export_event_avoid.py"})
    return jsonify(data)


@app.route("/api/avoid/inquiry")
def api_avoid_inquiry():
    """问询函避雷: 收到监管问询函=负面信号, 公告后20日-4.38%/三年全负。比立案温和但高频。"""
    data = _read_json(PREDICT_JSON.parent / "inquiry_letter.json")
    if data is None:
        return jsonify({"as_of": "", "items": [], "n": 0,
                        "message": "尚无问询函清单; PC 端运行 export_inquiry_letter.py"})
    return jsonify(data)


@app.route("/api/avoid/investigation")
def api_avoid_investigation():
    """立案调查避雷: 证监会立案=最强死亡信号, 立案后120日-8.67%/崩盘率53%。一票否决硬黑名单。"""
    data = _read_json(PREDICT_JSON.parent / "investigation_avoid.json")
    if data is None:
        return jsonify({"as_of": "", "items": [], "n": 0,
                        "message": "尚无立案清单; PC 端运行 export_investigation_avoid.py"})
    return jsonify(data)


@app.route("/api/avoid/lhb")
def api_avoid_lhb():
    """龙虎榜净卖避雷: 龙虎榜净卖出股=出货砸盘, T+1后5日-2.19%(三年全负, 强度≈热榜)。验证仅避雷侧真(选股侧IC是盘后穿越)。"""
    data = _read_json(PREDICT_JSON.parent / "lhb_avoid.json")
    if data is None:
        return jsonify({"as_of": "", "items": [], "n": 0,
                        "message": "尚无龙虎榜避雷清单; PC 端运行 export_lhb_avoid.py 生成 lhb_avoid.json"})
    return jsonify(data)


@app.route("/api/avoid/hot/history")
def api_avoid_hot_history():
    """热榜避雷历史: 历次热榜快照(export_hot_avoid 每次追加), 方便查看对比哪些股进出热榜。"""
    hist = _read_json(PREDICT_JSON.parent / "hot_avoid_history.json")
    if not isinstance(hist, list):
        hist = []
    return jsonify({"history": hist[-60:][::-1], "n": len(hist)})


@app.route("/daily")
def daily_page():
    """每日操作台: 一屏聚合 底仓调仓 + 事件腿今日触发(抢跑/回购/海力士/打新) + 持仓踩雷(热榜/毛利/雪球)。"""
    return render_template("daily.html")


@app.route("/readme")
def readme_page():
    """Member data-service guide; internal operators retain the runbook."""
    if _membership_store().has_feature(getattr(g, "current_member", None), "internal_operations"):
        return render_template("readme.html")
    return render_template("member_guide.html")


def _c6(code):
    import re
    m = re.search(r"(\d{6})", str(code).upper())
    return m.group(1) if m else str(code).upper()


@app.route("/api/data_health")
def api_data_health():
    """数据新鲜度监控: 各事件腿/避雷数据JSON的最后更新日期+条数+过期天数, 过期/断供标红。
    防'某腿数据断供了你还在用旧清单下单'(如北向断供那种坑)。"""
    import datetime as _dt
    today = _dt.date.today()
    # (label, Path, 时间戳候选字段, 该数据预期最大新鲜天数: 超过=过期标红)
    SOURCES = [
        ("顾问Pro篮子", REGIME_ADVISOR_PRO, ["updated_at", "updated", "generated_at", "as_of"], 3, "底仓·交易日盘前"),
        ("业绩抢跑(runup)", RUNUP_JSON, ["updated", "as_of", "date"], 3, "事件腿·季节性(空窗期可能空)"),
        ("回购腿(repo)", REPO_JSON, ["updated", "as_of"], 3, "事件腿"),
        ("纳入研究(inclusion)", PREDICT_JSON.parent / "index_inclusion.json", ["updated_at", "updated", "as_of"], 14, "事件腿·季度调整"),
        ("海力士映射(chipmap)", KOREA_SEMI_JSON, ["updated", "as_of", "date"], 2, "事件腿·盘前推"),
        ("组合清单(combo)", PREDICT_JSON.parent / "combo_holdings.json", ["updated"], 3, "组合落地清单"),
        ("毛利恶化避雷", MARGIN_AVOID_JSON, ["as_of", "updated"], 40, "避雷·季报驱动"),
        ("造假避雷(DSRI)", FRAUD_AVOID_JSON, ["as_of", "updated"], 40, "避雷·季报驱动"),
        ("热榜避雷", HOT_AVOID_JSON, ["as_of", "updated"], 3, "避雷·日更"),
        ("雪球合约避雷", PREDICT_JSON.parent / "snowball_avoid.json", ["updated", "as_of"], 3, "避雷·合约源日更"),
        ("打新(ipo)", IPO_JSON, ["updated", "as_of"], 3, "账户级"),
        ("RSRS择时", RSRS_JSON, ["updated", "as_of", "date"], 3, "方向腿"),
        ("下一日预测(沪深300)", RDAGENT_JSON, ["as_of", "generated_at", "updated"], 3, "三合一/挖矿SOTA清单"),
        ("询价转让事件", _eventrisk_source_path("cninfo_transfer.json"), ["updated", "generated_at", "as_of"], 3, "事件资料·巨潮公告"),
        ("定增事件", _eventrisk_source_path("cninfo_placement.json"), ["updated", "generated_at", "as_of"], 3, "事件资料·定增生命周期"),
        ("滚动业绩", _eventrisk_source_path("rolling_earnings.json"), ["updated", "generated_at", "as_of"], 8, "财务资料·周一更新"),
        ("跨市场存储映射", PREDICT_JSON.parent / "cross_market_storage.json", ["generated_at", "updated", "as_of"], 2, "跨市场·09:20快照"),
        ("事件避雷", PREDICT_JSON.parent / "event_avoid.json", ["updated", "as_of", "generated_at"], 3, "避雷·日更"),
        ("问询函避雷", PREDICT_JSON.parent / "inquiry_letter.json", ["updated", "as_of", "generated_at"], 3, "避雷·日更"),
        ("立案调查避雷", PREDICT_JSON.parent / "investigation_avoid.json", ["updated", "as_of", "generated_at"], 3, "避雷·日更"),
        ("龙虎榜净卖出", PREDICT_JSON.parent / "lhb_avoid.json", ["updated", "as_of", "generated_at"], 3, "避雷·日更"),
        ("融资透支避雷", PREDICT_JSON.parent / "leverage_avoid.json", ["updated", "as_of", "generated_at"], 3, "避雷·日更"),
        ("年报晚披露", PREDICT_JSON.parent / "late_disclosure.json", ["updated", "as_of", "generated_at"], 8, "事件腿·周更"),
        ("境外指数纳入", PREDICT_JSON.parent / "foreign_inclusion.json", ["updated", "as_of", "generated_at"], 8, "事件资料·周更"),
        ("注销型回购", PREDICT_JSON.parent / "repo_cancel.json", ["updated", "as_of", "generated_at"], 3, "事件腿·日更"),
        ("承诺不减持", PREDICT_JSON.parent / "commit_nosell.json", ["updated", "as_of", "generated_at"], 3, "事件腿·日更"),
        ("洗大澡反弹", PREDICT_JSON.parent / "bigbath.json", ["updated", "as_of", "generated_at"], 3, "事件腿·日更"),
        ("资产注入定增", PREDICT_JSON.parent / "asset_injection.json", ["updated", "as_of", "generated_at"], 3, "事件资料·日更"),
        ("巨潮业绩公告时间", PREDICT_JSON.parent / "cninfo_earnings_announcements.json", ["updated", "as_of", "generated_at"], 3, "财务资料·日更"),
        ("宽基ETF见顶风险", PREDICT_JSON.parent / "etf_flow_top_signal.json", ["updated", "as_of", "generated_at"], 3, "资金信号·日更"),
        ("行业ETF见顶风险", PREDICT_JSON.parent / "sector_etf_flow_signal.json", ["updated", "as_of", "generated_at"], 3, "资金信号·日更"),
        ("资金流出验证", PREDICT_JSON.parent / "money_outflow_signal.json", ["updated", "as_of", "generated_at"], 3, "资金信号·日更"),
        ("海力士盘中分时", PREDICT_JSON.parent / "hynix_intraday.json", ["date", "updated", "generated_at"], 2, "盘中·分钟级"),
        ("超短线做T", PREDICT_JSON.parent / "intraday_t.json", ["updated", "as_of", "date"], 2, "盘中·分钟级"),
    ]
    def _parse_date(s):
        if not s:
            return None
        s = str(s).replace("/", "-")
        for fmt, ln in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10), ("%Y%m%d", 8)):
            try:
                return _dt.datetime.strptime(s[:ln], fmt).date()
            except Exception:
                continue
        return None
    def _health_count(d):
        if not isinstance(d, dict):
            return 0
        if any(key in d for key in ("today_buy", "soon_buy", "just_listed")):
            return sum(
                len(d.get(key) or [])
                for key in ("today_buy", "soon_buy", "just_listed")
                if isinstance(d.get(key) or [], list)
            )
        if isinstance(d.get("rolling"), dict):
            return len((d.get("rolling") or {}).get("items") or [])
        if isinstance(d.get("details"), list):
            return len(d.get("details") or [])
        if any(isinstance(d.get(key), list) for key in ("leaders", "upside", "downside")):
            # These are duplicate ranked views of the same cross-market universe.
            return len(d.get("leaders") or d.get("downside") or d.get("upside") or [])
        if isinstance(d.get("cats"), dict):
            # Event avoid stores one ``items`` list per category.
            return sum(
                len(bucket.get("items") or [])
                for bucket in d["cats"].values()
                if isinstance(bucket, dict) and isinstance(bucket.get("items") or [], list)
            )
        if isinstance(d.get("candidates"), list) and d.get("candidates"):
            return len(d["candidates"])
        for key in ("n", "n_cand", "count"):
            value = d.get(key)
            if isinstance(value, bool) or value is None:
                continue
            try:
                return max(0, int(float(value)))
            except (TypeError, ValueError):
                continue
        latest = d.get("latest_stock_outflow")
        if isinstance(latest, list):
            return len(latest)
        if isinstance(latest, dict):
            for key in ("list", "items", "rows"):
                if isinstance(latest.get(key), list):
                    return len(latest[key])
        # ``events`` powers both ETF flow studies; ``points`` powers Hynix intraday.
        for key in ("list", "holdings", "items", "rows", "buy", "hits", "events", "points"):
            if isinstance(d.get(key), list) and d.get(key):
                return len(d[key])
        return 0
    items = []
    for label, path, tsfields, max_fresh, note in SOURCES:
        d = _read_json(path) if path is not None else None
        if d is None:
            items.append({"label": label, "status": "missing", "updated": None, "n": 0,
                          "days": None, "note": note, "msg": "文件缺失/未生成"})
            continue
        ts = next((d.get(f) for f in tsfields if isinstance(d, dict) and d.get(f)), None)
        dt = _parse_date(ts)
        n = _health_count(d)
        days = (today - dt).days if dt else None
        if dt is None:
            status = "unknown"
        elif days is not None and days > max_fresh:
            status = "stale"
        else:
            status = "fresh"
        items.append({"label": label, "status": status, "updated": str(dt) if dt else (str(ts)[:19] if ts else None),
                      "n": n, "days": days, "max_fresh": max_fresh, "note": note})
    n_bad = sum(1 for x in items if x["status"] in ("stale", "missing", "unknown"))
    return jsonify({"items": items, "n": len(items), "n_bad": n_bad,
                    "as_of": str(today), "ts": _dt.datetime.now().strftime("%H:%M")})


@app.route("/api/daily_ops")
def api_daily_ops():
    """聚合今日要做的事: 底仓调仓 + 4事件腿触发 + 持仓踩雷交叉。一次返回, 给每日操作台。"""
    import datetime as _dt
    P = PREDICT_JSON.parent
    held6 = {_c6(p.get("code", "")) for p in _load_positions() if p.get("code")}

    # ① 底仓 顾问Pro
    pro = (_read_json(REGIME_ADVISOR_PRO) or {}).get("current", {})
    nr = pro.get("next_rebalance")
    days_to = None
    if nr:
        try:
            days_to = (_dt.date.fromisoformat(nr) - _dt.date.today()).days
        except Exception:
            days_to = None
    core = {"regime": pro.get("regime"), "regime_label": pro.get("regime_label"),
            "next_rebalance": nr, "days_to": days_to, "due": (days_to is not None and days_to <= 0)}

    # RSRS 大盘方向(独立方向腿, ≤10%仓位用): 各指数 持有/空仓 + 是否翻转
    rsrs_d = _read_json(RSRS_JSON) or {}
    rsrs = {"updated": rsrs_d.get("updated"),
            "indices": [{"name": ix.get("name"), "score": ix.get("score"), "state": ix.get("state"),
                         "flip": ix.get("flip")} for ix in (rsrs_d.get("indices") or [])]}

    # ② 事件腿
    runup = _read_json(P / "runup.json") or {}
    repo = _read_json(P / "repo.json") or {}
    ipo = _read_json(IPO_JSON) or {}
    korea = _read_json(P / "korea_semi.json") or {}
    sig = str(korea.get("signal") or "")
    hynix_on = ("买" in sig) or ("🟢" in sig)
    sleeves = {
        "runup": {"buy": runup.get("buy") or [], "sell": runup.get("sell") or []},
        "repo": {"buy": repo.get("buy_today") or [], "sell": repo.get("sell_soon") or []},
        "hynix": {"signal": sig, "on": hynix_on, "ret": korea.get("hynix_ret"), "date": korea.get("hynix_date")},
        "ipo": {"today": ipo.get("today_buy") or [], "soon": ipo.get("soon_buy") or []},
    }

    # ③ 持仓踩雷 交叉
    def _flag(jsonfile, label):
        d = _read_json(P / jsonfile) or {}
        out = []
        for it in (d.get("items") or []):
            c6 = _c6(it.get("code", ""))
            if c6 in held6:
                out.append({"code": it.get("code"), "name": it.get("name"), "label": label,
                            "info": it.get("status") or (f"连续{it.get('days_on')}日上榜" if it.get("days_on") else "")})
        return out
    risk = _flag("hot_avoid.json", "🔥热榜") + _flag("margin_avoid.json", "📉毛利恶化")
    # 造假避雷(DSRI): 只报持仓里 DSRI>1.5 的真嫌疑(应收异常飙升)
    fr = _read_json(P / "fraud_avoid.json") or {}
    for it in (fr.get("items") or []):
        if _c6(it.get("code", "")) in held6 and (it.get("dsri") or 0) > 1.5:
            risk.append({"code": it.get("code"), "name": it.get("name"), "label": "🕵️造假嫌疑",
                         "info": f"DSRI={it.get('dsri')} 应收/营收{it.get('ar_ratio')}%↑(去年{it.get('ar_ratio_prev')}%)"})
    snow = _read_json(P / "snowball_avoid.json") or {}
    for it in (snow.get("items") or []):
        if it.get("held") and it.get("level", 0) >= 2 and not it.get("expired"):
            risk.append({"code": it.get("code"), "name": it.get("name"), "label": "❄️雪球", "info": it.get("status")})

    n_act = (len(sleeves["runup"]["buy"]) + len(sleeves["runup"]["sell"]) + len(sleeves["repo"]["buy"]) +
             len(sleeves["repo"]["sell"]) + len(sleeves["ipo"]["today"]) + len(risk) +
             (1 if hynix_on else 0) + (1 if core["due"] else 0))
    return jsonify({"updated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "today": _dt.date.today().isoformat(),
                    "n_holdings": len(held6), "n_actions": n_act, "core": core, "sleeves": sleeves, "risk": risk, "rsrs": rsrs})


def _ts_to_sina(c):
    c = str(c).strip()
    if c[:2].lower() in ("sh", "sz", "bj"):
        return c.lower()
    if "." in c:
        num, ex = c.split(".")
        return ex.lower() + num
    return c.lower()


@app.route("/api/repo/intraday")
def api_repo_intraday():
    """回购持仓 今日分时(多股叠加, 归一成距昨收%)。NAS可达的新浪源 _intraday_today。codes=逗号分隔可选, 默认取repo.json。"""
    P = PREDICT_JSON.parent
    arg = (request.args.get("codes") or "").strip()
    pairs = []
    if arg:
        pairs = [(c, c) for c in arg.split(",") if c]
    else:
        repo = _read_json(P / "repo.json") or {}
        seen = set()
        for it in ((repo.get("buy_today") or []) + (repo.get("holdings") or [])):
            c = it.get("code")
            if c and c not in seen:
                seen.add(c); pairs.append((c, it.get("name") or c))
            if len(pairs) >= 12:
                break
    series = []; date = None
    for code, name in pairs:
        try:
            times, close, pre = _intraday_today(_ts_to_sina(code), scale=5)
        except Exception:
            times, close, pre = None, None, None
        if times and close and pre and pre > 0:
            series.append({"code": code, "name": name, "times": times,
                           "pct": [round((x / pre - 1) * 100, 2) for x in close],
                           "pre": pre, "cur": close[-1], "cur_pct": round((close[-1] / pre - 1) * 100, 2)})
            date = times[0][:10] if len(times[0]) > 5 else date
    return jsonify({"updated": datetime.now().strftime("%H:%M:%S"), "n": len(series), "series": series})


@app.route("/snowball")
def snowball_page():
    """雪球避雷: 场外雪球距敲入/敲出旗标(踩踏/抛压预警)+持仓交叉。机制真实但无法回测→仅风控旗标非alpha。"""
    return render_template("snowball.html")


@app.route("/api/snowball")
def api_snowball():
    data = _read_json(SNOWBALL_AVOID_JSON)
    if data is None:
        return jsonify({"updated": "", "items": [], "n": 0,
                        "message": "尚无雪球清单; PC 端运行 export_snowball.py 读 W盘Excel 生成 snowball_avoid.json"})
    return jsonify(data)


@app.route("/api/snowball/history")
def api_snowball_history():
    """雪球避雷历史: 历次预警快照(每次刷新追加), 看哪些票进出踩踏/抛压预警区。"""
    hist = _read_json(PREDICT_JSON.parent / "snowball_history.json")
    if not isinstance(hist, list):
        hist = []
    return jsonify({"history": hist[-60:][::-1], "n": len(hist)})


@app.route("/api/fundamentals")
def api_fundamentals():
    """单只个股 近3年报+最新一期 扣非增速/营收增速/毛利率, 加同行业可比(申万三级=精确主营, 带3年+1期; 申万二级=广义板块)。?code=000001"""
    code = (request.args.get("code") or "").strip().upper()
    data = _read_json(FUNDAMENTALS_JSON)
    if data is None:
        return jsonify({"message": "尚无基本面数据; PC 端运行 export_fundamentals.py 生成 fundamentals.json"})
    stocks = data.get("stocks", {})

    def norm(c):
        return c.replace("SH", "").replace("SZ", "").replace(".", "")[:6]
    want = norm(code)
    key = next((k for k in stocks if norm(k) == want), None)
    if key is None:
        return jsonify({"found": False, "code": code, "message": "未找到该股基本面(可能是新股/北交所/缺财报)"})
    me = stocks[key]
    l3 = me.get("l3", me.get("ind", "")); l2 = me.get("l2", "")
    by_l3 = data.get("by_l3", {}); by_l2 = data.get("by_l2", {})

    def peer_full(klist, exclude):
        out = []
        for k in klist:
            if k in exclude:
                continue
            s = stocks.get(k)
            if not s or not s.get("rows"):
                continue
            last = s["rows"][-1]
            out.append({"code": k, "name": s["name"], "rows": s["rows"],
                        "dedt_yoy": last.get("dedt_yoy"), "rev_yoy": last.get("rev_yoy"),
                        "gm": last.get("gm"), "dgm": last.get("dgm")})
        out.sort(key=lambda p: (p["gm"] is None, -(p["gm"] or -999)))
        return out

    l3_members = set(by_l3.get(l3, []))
    l3_peers = peer_full(by_l3.get(l3, []), {key})                       # 精确主营(同三级)
    l2_peers = peer_full(by_l2.get(l2, []), l3_members)                  # 广义板块(同二级, 去掉已在三级里的)

    def med(field):
        vals = [p[field] for p in l3_peers if p.get(field) is not None]
        mv = me["rows"][-1].get(field) if me.get("rows") else None
        if mv is not None:
            vals.append(mv)
        if not vals:
            return None
        vals = sorted(vals); n = len(vals)
        return round((vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2), 1)
    l3_med = {f: med(f) for f in ("dedt_yoy", "rev_yoy", "gm", "dgm")}

    return jsonify({"found": True, "code": key, "name": me["name"],
                    "l1": me.get("l1", ""), "l2": l2, "l3": l3,
                    "rows": me["rows"],
                    "l3_peers": l3_peers, "l2_peers": l2_peers[:60],
                    "l3_median": l3_med, "n_l3": len(l3_peers), "n_l2": len(l2_peers)})


def _c6(code):
    return (code or "").strip().upper().replace("SH", "").replace("SZ", "").replace("BJ", "").replace(".", "")[:6]


@app.route("/report")
def report_page():
    """个股深度研究报告: 输代码→PC按需拉数据+LLM起草定性→可编辑。仿卖方定增报告结构。"""
    return render_template("report.html")


@app.route("/api/report")
def api_report():
    code = request.args.get("code") or ""
    c6 = _c6(code)
    if not c6:
        return jsonify({"found": False, "message": "请输入股票代码"})
    base = _read_json(PREDICT_JSON.parent / f"report_{c6}.json")
    status = _read_json(REPORT_STATUS) or {}
    pending = (status.get("c6") == c6 and status.get("status", "").startswith("⏳"))
    if base is None:
        return jsonify({"found": False, "c6": c6, "pending": pending,
                        "status": status.get("status", "") if pending else "",
                        "message": "尚无该股报告, 点「生成报告」让PC拉数据并用LLM起草(约1-2分钟)"})
    # 合并用户编辑覆盖层
    edit = _member_document("report_edit", None, item_key=c6)
    if edit and isinstance(edit.get("llm"), dict):
        base.setdefault("llm", {}).update({k: v for k, v in edit["llm"].items() if v})
        base["edited"] = edit.get("updated", "")
    base["found"] = True
    base["pending"] = pending
    return jsonify(base)


@app.route("/api/report/list")
def api_report_list():
    """已生成的研报列表, 按生成时间倒序 (供研报页历史快捷入口: 简称点击即看)."""
    d = PREDICT_JSON.parent
    items = []
    try:
        for p in d.glob("report_*.json"):
            parts = p.stem.split("_")  # report_300502 -> ['report','300502']
            if len(parts) != 2 or not (parts[1].isdigit() and len(parts[1]) == 6):
                continue  # 跳过 report_edit_* / report_status / report_request 等
            j = _read_json(p) or {}
            items.append({"c6": parts[1], "code": j.get("code") or parts[1],
                          "name": j.get("name") or "", "updated": j.get("updated") or "",
                          "mtime": p.stat().st_mtime})
    except Exception as e:
        return jsonify({"items": [], "error": str(e)})
    items.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
    for it in items:
        it.pop("mtime", None)
    return jsonify({"items": items})


@app.route("/api/report/growth_after_close", methods=["POST"])
def api_report_growth_after_close():
    """Queue reports for after-close earnings events with accelerating deduct-net-profit growth."""
    try:
        from scripts.build_growth_report_queue import build_queue

        payload = build_queue(
            Path(STOCK_META_DB).parent,
            PREDICT_JSON.parent,
            write_batch=True,
            min_growth=20,
        )
        if not payload.get("n"):
            return jsonify({"ok": True, "n": 0, "items": [], "window": payload.get("window"),
                            "message": "未筛到符合条件的昨收后业绩公告"})
        batch_status = {
            "state": "queued", "msg": "等待PC研报监听器领取任务", "i": 0,
            "n": payload.get("n"), "source": "growth_after_close",
            "job_id": payload.get("job_id"), "requested_at": payload.get("updated"),
        }
        return jsonify({"ok": True, "n": payload.get("n"), "items": payload.get("items") or [],
                        "window": payload.get("window"), "job_id": payload.get("job_id"),
                        "batch_status": batch_status,
                        "message": f"已按沪深300→中证500→中证1000顺序排队 {payload.get('n')} 只个股研报"})
    except Exception as e:
        log.exception("growth after close report queue failed")
        return jsonify({"ok": False, "message": f"生成高增长研报队列失败: {e}"}), 500


@app.route("/api/report/growth_after_close/status")
def api_report_growth_after_close_status():
    payload = _read_json(PREDICT_JSON.parent / "growth_report_queue.json") or {}
    batch_status = _read_json(BATCH_GEN_STATUS) or {}
    job_id = payload.get("job_id")
    if not payload.get("n"):
        # 空队列无任务可领, 回中性状态, 避免页面误显示"已排队 等待监听器"并置灰按钮
        batch_status = {"state": "", "msg": "", "i": 0, "n": 0}
    elif job_id and batch_status.get("job_id") != job_id:
        batch_status = {
            "state": "queued", "msg": "等待PC研报监听器领取任务", "i": 0,
            "n": payload.get("n", 0), "source": "growth_after_close",
            "job_id": job_id, "requested_at": payload.get("updated"),
        }
    payload["batch_status"] = batch_status or {"state": "", "msg": "", "i": 0, "n": 0}
    return jsonify(payload or {"items": [], "n": 0})


@app.route("/api/report/request", methods=["POST"])
def api_report_request():
    code = (request.get_json(silent=True) or {}).get("code", "")
    c6 = _c6(code)
    if not c6:
        return jsonify({"ok": False, "message": "请输入有效代码"})
    try:
        ts_code = _code_to_ts(code) or code
        ipo_path = IPO_JSON if IPO_JSON.exists() else Path(__file__).parent / "data" / "ipo.json"
        ipo = _read_json(ipo_path) or {}
        ipo_item = None
        for item in ((ipo.get("just_listed") or []) + (ipo.get("today_buy") or [])
                     + (ipo.get("soon_buy") or [])):
            item_ts = _code_to_ts(item.get("ts_code") or item.get("code") or "")
            if item_ts == ts_code:
                ipo_item = item
                break
        payload = {
            "code": code, "c6": c6, "ts_code": ts_code,
            "name": str((ipo_item or {}).get("name") or ""),
            "is_ipo": ipo_item is not None,
        }
        REPORT_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        REPORT_REQUEST.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return jsonify({"ok": True, "message": f"已通知PC生成 {c6} 报告(拉数据+LLM起草, 约1-2分钟)"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"请求失败: {e}"})


@app.route("/api/report/save", methods=["POST"])
def api_report_save():
    body = request.get_json(silent=True) or {}
    c6 = _c6(body.get("code", ""))
    llm = body.get("llm", {})
    if not c6 or not isinstance(llm, dict):
        return jsonify({"ok": False, "message": "参数错误"})
    try:
        _member_data_store().put(
            _member_scope_id(),
            "report_edit",
            {"c6": c6, "llm": llm, "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            item_key=c6,
        )
        return jsonify({"ok": True, "message": "已保存编辑"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"保存失败: {e}"})


# ---------- 财务预测 (三表联动 + DCF) ----------
def _forecast_effective(base, edit):
    """默认假设 <- 用户覆盖层; 返回 (effective_assumptions, product_lines, horizon)。"""
    A = dict(base.get("assumptions_default", {}))
    plines = base.get("product_lines")
    horizon = 5
    if edit:
        for k, v in (edit.get("assumptions") or {}).items():
            A[k] = v
        if edit.get("product_lines") is not None:
            plines = edit["product_lines"]
        horizon = int(edit.get("horizon", horizon) or horizon)
    return A, plines, horizon


def _forecast_override(c6):
    paths = [
        FORECAST_EDIT_DIR / "forecast_overrides" / f"{c6}.json",
        Path(__file__).parent / "data" / "forecast_overrides" / f"{c6}.json",
    ]
    for path in paths:
        data = _read_json(path)
        if isinstance(data, dict):
            data["_path"] = str(path)
            return data
    return {}


def _apply_forecast_override(A, plines, override):
    if not isinstance(override, dict):
        return A, plines
    for key, value in (override.get("assumptions") or {}).items():
        A[key] = value
    if override.get("apply_product_lines") and isinstance(override.get("product_lines"), list) and override["product_lines"]:
        plines = override["product_lines"]
    return A, plines


@app.route("/forecast")
def forecast_page():
    """财务预测: 输代码→PC拉三大报表(tushare)→调主营构成+假设→预测三表+DCF。"""
    return render_template("forecast.html")


@app.route("/api/forecast")
def api_forecast():
    c6 = _c6(request.args.get("code") or "")
    if not c6:
        return jsonify({"found": False, "message": "请输入股票代码"})
    base = _read_json(PREDICT_JSON.parent / f"forecast_base_{c6}.json")
    status = _read_json(FORECAST_STATUS) or {}
    pending = (status.get("c6") == c6 and str(status.get("status", "")).startswith("⏳"))
    if base is None:
        return jsonify({"found": False, "c6": c6, "pending": pending,
                        "status": status.get("status", "") if pending else "",
                        "message": "尚无该股基础数据, 点「生成基础数据」让PC拉三大报表(约30-60秒)"})
    if base.get("unsupported"):
        return jsonify({"found": False, "c6": c6, "unsupported": True,
                        "message": base.get("reason", "该股不支持本模型")})
    edit = _member_document("forecast_edit", None, item_key=c6)
    A, plines, horizon = _forecast_effective(base, edit)
    override = _forecast_override(c6)
    A, plines = _apply_forecast_override(A, plines, override)
    out = {k: base[k] for k in ("code", "c6", "name", "base_year", "rev_base", "unit",
                                "price", "hist", "opening", "product_lines", "updated") if k in base}
    out["found"] = True
    out["assumptions"] = A
    out["assumptions_default"] = base.get("assumptions_default", {})
    out["product_lines"] = plines
    out["horizon"] = horizon
    out["edited"] = (edit or {}).get("updated", "")
    out["external_override"] = {k: v for k, v in override.items() if k != "_path"}
    try:
        from scripts.forecast_engine import recompute
        out["result"] = recompute(base, A, plines, horizon)
    except Exception as e:
        out["result"] = None
        out["compute_error"] = str(e)
    return jsonify(out)


@app.route("/api/forecast/recompute", methods=["POST"])
def api_forecast_recompute():
    """快速重算(容器内, 无tushare): 收 假设+产品线 → 返回三表+DCF。"""
    body = request.get_json(silent=True) or {}
    c6 = _c6(body.get("code", ""))
    base = _read_json(PREDICT_JSON.parent / f"forecast_base_{c6}.json")
    if base is None:
        return jsonify({"ok": False, "message": "请先生成基础数据"})
    try:
        from scripts.forecast_engine import recompute
        A = dict(base.get("assumptions_default", {}))
        A.update(body.get("assumptions") or {})
        plines = body.get("product_lines")
        if plines is None:
            plines = base.get("product_lines")
        horizon = int(body.get("horizon", 5) or 5)
        return jsonify({"ok": True, **recompute(base, A, plines, horizon)})
    except Exception as e:
        return jsonify({"ok": False, "message": f"计算失败: {e}"})


@app.route("/api/forecast/request", methods=["POST"])
def api_forecast_request():
    code = (request.get_json(silent=True) or {}).get("code", "")
    c6 = _c6(code)
    if not c6:
        return jsonify({"ok": False, "message": "请输入有效代码"})
    try:
        FORECAST_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        FORECAST_REQUEST.write_text(json.dumps({"code": code, "c6": c6}, ensure_ascii=False), encoding="utf-8")
        return jsonify({"ok": True, "message": f"已通知PC拉取 {c6} 三大报表(约30-60秒)"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"请求失败: {e}"})


@app.route("/api/forecast/save", methods=["POST"])
def api_forecast_save():
    body = request.get_json(silent=True) or {}
    c6 = _c6(body.get("code", ""))
    if not c6:
        return jsonify({"ok": False, "message": "参数错误"})
    try:
        _member_data_store().put(
            _member_scope_id(),
            "forecast_edit",
            {
                "c6": c6,
                "assumptions": body.get("assumptions", {}),
                "product_lines": body.get("product_lines"),
                "horizon": int(body.get("horizon", 5) or 5),
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            item_key=c6,
        )
        return jsonify({"ok": True, "message": "已保存假设"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"保存失败: {e}"})


@app.route("/api/forecast/list")
def api_forecast_list():
    """已生成基础数据的列表, 按时间倒序 (历史快捷入口)。"""
    d = PREDICT_JSON.parent
    items = []
    try:
        for p in d.glob("forecast_base_*.json"):
            c6 = p.stem.replace("forecast_base_", "")
            if not (c6.isdigit() and len(c6) == 6):
                continue
            j = _read_json(p) or {}
            items.append({"c6": c6, "code": j.get("code") or c6, "name": j.get("name") or "",
                          "updated": j.get("updated") or "", "mtime": p.stat().st_mtime})
    except Exception as e:
        return jsonify({"items": [], "error": str(e)})
    items.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
    for it in items:
        it.pop("mtime", None)
    return jsonify({"items": items})


@app.route("/index_inclusion")
def index_inclusion_page():
    data_path = PREDICT_JSON.parent / "index_inclusion.json"
    data = {}
    if data_path.exists():
        try:
            data = json.loads(data_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"Error loading index_inclusion.json: {e}")
    # 历史纳入明细按 ts_code 注入股票简称 (服务端 join stock_meta, 不必重跑PC导出)
    details = data.get("details") or []
    tscodes = sorted({d.get("ts_code") for d in details if d.get("ts_code")})
    if tscodes:
        name_map = {}
        try:
            conn = sqlite3.connect(STOCK_META_DB)
            qs = ",".join("?" * len(tscodes))
            for tc, nm in conn.execute(f"SELECT ts_code, name FROM stock_meta WHERE ts_code IN ({qs})", tscodes):
                name_map[tc] = nm
            conn.close()
        except Exception as e:
            log.warning(f"index_inclusion name join failed: {e}")
        for d in details:
            d["name"] = name_map.get(d.get("ts_code"), "")
    return render_template("index_inclusion.html", data=data)


@app.route("/index_inclusion_pro")
def index_inclusion_pro_page():
    return render_template("index_inclusion_pro.html")


@app.route("/api/index_inclusion_pro")
def api_index_inclusion_pro():
    data_path = PREDICT_JSON.parent / "index_inclusion_pro.json"
    empty = {
        "updated": "",
        "today": "",
        "n_holdings": 0,
        "buy_today": [],
        "sell_today": [],
        "holdings": [],
        "watch": [],
    }
    if not data_path.is_file():
        return jsonify({
            **empty,
            "source_state": "missing",
            "message": "指数纳入 Pro 数据文件尚未生成",
        })
    try:
        payload = json.loads(data_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return jsonify({
            **empty,
            "source_state": "invalid",
            "message": "指数纳入 Pro 数据文件无法解析",
        })
    list_fields = ("buy_today", "sell_today", "holdings", "watch")
    if not isinstance(payload, dict) or any(not isinstance(payload.get(key), list) for key in list_fields):
        return jsonify({
            **empty,
            "source_state": "invalid",
            "message": "指数纳入 Pro 数据结构不完整",
        })
    result = {**empty, **payload}
    result["source_state"] = "ok"
    return jsonify(result)


@app.route("/api/foreign_inclusion")
def api_foreign_inclusion():
    data_path = PREDICT_JSON.parent / "foreign_inclusion.json"
    if data_path.exists():
        return jsonify(json.loads(data_path.read_text(encoding="utf-8")))
    return jsonify({"schedule": [], "candidates": [], "message": "尚无数据; PC运行 export_foreign_inclusion.py"})


@app.route("/asset-injection")
def asset_injection_page():
    """资产注入型定增(36月锁定)=定增全谱系唯一可落地正事件腿。验证 上市后250日+6.3%。"""
    return render_template("asset_injection.html")


@app.route("/api/asset_injection")
def api_asset_injection():
    data_path = PREDICT_JSON.parent / "asset_injection.json"
    if data_path.exists():
        return jsonify(json.loads(data_path.read_text(encoding="utf-8")))
    return jsonify({"items": [], "message": "尚无数据; PC运行 export_asset_injection.py"})


@app.route("/placement-transfer")
def placement_transfer_page():
    return render_template("placement_transfer.html")


@app.route("/transfer-events")
def transfer_events_page():
    return render_template("transfer_events.html")


@app.route("/placement-events")
def placement_events_page():
    return render_template("placement_events.html")


def _pt_rows_from_files(names):
    rows = []
    for name in names:
        payload = _eventrisk_load_json(name) or {}
        for row in _eventrisk_rows(payload):
            if isinstance(row, dict):
                item = dict(row)
                item["dataset"] = item.get("dataset") or name
                rows.append(item)
    return rows


def _pt_overlay_row_key(row):
    announcement_id = str(row.get("announcement_id") or row.get("announcementId") or "").strip()
    if announcement_id:
        return f"announcement:{announcement_id}"
    url = str(row.get("url") or row.get("announcement_url") or "").strip()
    if url:
        return f"url:{url}"
    code = _pt_code(row)
    date = str(row.get("ann_date") or row.get("date") or "")[:10]
    title = str(row.get("title") or row.get("announcementTitle") or "").strip()
    return f"row:{code}|{date}|{title}"


def _pt_transfer_terms_index():
    payload = _eventrisk_load_json("transfer_terms_overlay.json") or {}
    index = {}
    for row in _eventrisk_rows(payload):
        if not isinstance(row, dict) or row.get("status") != "parsed":
            continue
        keys = {_pt_overlay_row_key(row)}
        announcement_id = str(row.get("announcement_id") or row.get("announcementId") or "").strip()
        url = str(row.get("url") or row.get("announcement_url") or "").strip()
        if announcement_id:
            keys.add(f"announcement:{announcement_id}")
        if url:
            keys.add(f"url:{url}")
        for key in keys:
            index[key] = row
    return index


def _pt_missing_term(value):
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return value == 0
    return str(value).strip() in {"", "-", "0", "0.0"}


def _pt_apply_transfer_terms(row, index):
    overlay = index.get(_pt_overlay_row_key(row))
    if not overlay:
        return row
    item = dict(row)
    if _pt_missing_term(item.get("transfer_price")) and not _pt_missing_term(overlay.get("transfer_price")):
        item["transfer_price"] = overlay["transfer_price"]
    if _pt_missing_term(item.get("transfer_ratio")) and not _pt_missing_term(overlay.get("transfer_ratio")):
        item["transfer_ratio"] = overlay["transfer_ratio"]
    item["transfer_terms_source"] = overlay.get("url") or ""
    item["transfer_terms_confidence"] = overlay.get("confidence") or ""
    item["transfer_terms_price_page"] = overlay.get("price_page")
    item["transfer_terms_ratio_page"] = overlay.get("ratio_page")
    item["transfer_terms_parser_version"] = overlay.get("parser_version")
    return item


def _pt_code(row):
    raw = str(row.get("code") or row.get("ts_code") or row.get("symbol") or row.get("secCode") or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[:6] if len(digits) >= 6 else raw


def _pt_date(row, keys):
    dt = _eventrisk_first_date(row, keys)
    return dt.strftime("%Y-%m-%d") if dt is not None else ""


def _pt_days_to(date_text):
    dt = _eventrisk_parse_date(date_text)
    if dt is None:
        return None
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (dt - today).days


def _pt_stage_dates(row, kind=""):
    title = str(row.get("title") or row.get("announcementTitle") or "")
    ann = _pt_date(row, ("ann_date", "annDate", "date", "announcement_date", "announcementDate"))
    is_placement = kind == "定增"
    plan_keys = ("plan_date", "planDate", "preplan_date", "proposal_date", "proposalDate")
    if not is_placement:
        plan_keys += ("ann_date", "annDate", "date", "announcement_date", "announcementDate")
    plan = _pt_date(row, plan_keys)
    board = _pt_date(row, ("board_date", "boardDate"))
    shareholder = _pt_date(row, ("shareholder_date", "shareholderDate", "holders_date"))
    exchange_approval = _pt_date(row, ("exchange_approval_date", "exchangeApprovalDate"))
    approval = _pt_date(row, (
        "approval_date", "approvalDate", "csrc_approval_date", "csrcApprovalDate",
        "reply_date", "registration_date",
    ))
    issue_keys = ("issue_date", "issueDate", "offer_date", "offerDate", "发行日期")
    if not is_placement:
        issue_keys += ("transfer_date", "transferDate", "date", "ann_date", "annDate")
    issue = _pt_date(row, issue_keys)
    if is_placement and not plan and ann and ("预案" in title or "草案" in title):
        plan = ann
    if not board and "董事会" in title:
        board = ann
    if not shareholder and ("股东会" in title or "股东大会" in title):
        shareholder = ann
    if not exchange_approval and "审核通过" in title and any(x in title for x in ("交易所", "上交所", "深交所", "北交所")):
        exchange_approval = ann
    if not approval and any(x in title for x in ("同意注册", "注册批复", "核准批复", "证监会核准")):
        approval = ann
    if not issue and any(x in title for x in ("发行结果", "发行情况报告", "上市公告", "转让结果", "定价")):
        issue = ann
    return plan, board, shareholder, exchange_approval, approval, issue


def _pt_normalize(row, kind):
    plan, board, shareholder, exchange_approval, approval, issue = _pt_stage_dates(row, kind)
    unlock = row.get("unlock_date") or ""
    price = row.get("issue_price") or row.get("transfer_price") or row.get("price") or ""
    return {
        "kind": kind,
        "code": _pt_code(row),
        "project_id": row.get("project_id") or "",
        "batch_id": row.get("batch_id") or "",
        "name": row.get("name") or row.get("secName") or row.get("securityName") or row.get("stock_name") or "",
        "plan_date": plan,
        "board_date": board,
        "shareholder_date": shareholder,
        "exchange_approval_date": exchange_approval,
        "approval_date": approval,
        "issue_date": issue,
        "list_date": _pt_date(row, ("list_date", "listDate", "listing_date", "listingDate")),
        "unlock_date": unlock,
        "unlock_date_latest": row.get("unlock_date_latest") or unlock,
        "unlock_schedule": row.get("unlock_schedule") if isinstance(row.get("unlock_schedule"), list) else [],
        "unlock_estimated": bool(row.get("unlock_estimated")),
        "lock_period": row.get("lock_period") or row.get("lock") or row.get("lockPeriod") or "",
        "lock_periods": row.get("lock_periods") if isinstance(row.get("lock_periods"), list) else [],
        "lock_tranches": row.get("lock_tranches") if isinstance(row.get("lock_tranches"), list) else [],
        "lock_start_date": row.get("lock_start_date") or "",
        "lock_start_basis": row.get("lock_start_basis") or "",
        "lock_term_source": row.get("lock_term_source") or "",
        "lock_term_confidence": row.get("lock_term_confidence") or "",
        "lock_term_evidence": row.get("lock_term_evidence") if isinstance(row.get("lock_term_evidence"), (dict, list)) else {},
        "lock_term_conflict": bool(row.get("lock_term_conflict")),
        "field_conflicts": row.get("field_conflicts") if isinstance(row.get("field_conflicts"), list) else [],
        "unlock_basis": row.get("unlock_basis") or "",
        "unlock_source": row.get("unlock_source") or "",
        "unlock_confidence": row.get("unlock_confidence") or "",
        "regulatory_context": row.get("regulatory_context") if isinstance(row.get("regulatory_context"), dict) else {},
        "days_to_unlock": row.get("days_to_unlock") if isinstance(row.get("days_to_unlock"), int) else _pt_days_to(unlock),
        "issue_price": price,
        "transfer_price": row.get("transfer_price") or price,
        "transfer_ratio": row.get("transfer_ratio") or row.get("ratio") or row.get("share_ratio") or "",
        "current_price": row.get("current_price") or "",
        "market_date": row.get("market_date") or "",
        "avg20": row.get("avg20") or "",
        "discount_floor": row.get("discount_floor") or "",
        "price_gap_pct": row.get("price_gap_pct"),
        "issue_price_gap_pct": row.get("issue_price_gap_pct"),
        "discount_floor_gap_pct": row.get("discount_floor_gap_pct"),
        "placement_support_level": row.get("placement_support_level") or row.get("support_motive") or "",
        "placement_support_label": row.get("placement_support_label") or _placement_support_label(row.get("support_motive")),
        "status": row.get("risk_level") or row.get("placement_support_label") or row.get("support_motive") or "",
        "title": row.get("title") or row.get("announcementTitle") or "",
        "url": row.get("url") or row.get("announcement_url") or "",
        "dataset": row.get("dataset") or "",
        "announcement_id": row.get("announcement_id") or row.get("announcementId") or "",
        "stage_evidence": row.get("stage_evidence") if isinstance(row.get("stage_evidence"), dict) else {},
        "lifecycle_source": row.get("lifecycle_source") or "",
        "lifecycle_status": row.get("lifecycle_status") or "",
        "transfer_terms_source": row.get("transfer_terms_source") or "",
        "transfer_terms_confidence": row.get("transfer_terms_confidence") or "",
        "transfer_terms_price_page": row.get("transfer_terms_price_page"),
        "transfer_terms_ratio_page": row.get("transfer_terms_ratio_page"),
        "transfer_terms_parser_version": row.get("transfer_terms_parser_version"),
    }


def _pt_summary(rows):
    ann_dates = sorted([x.get("plan_date") or x.get("issue_date") or "" for x in rows if x.get("plan_date") or x.get("issue_date")])
    unlock_dates = sorted([x.get("unlock_date") or "" for x in rows if x.get("unlock_date")])
    past = sum(1 for x in rows if isinstance(x.get("days_to_unlock"), int) and x["days_to_unlock"] < 0)
    near = sum(1 for x in rows if isinstance(x.get("days_to_unlock"), int) and 0 <= x["days_to_unlock"] <= 90)
    future = sum(1 for x in rows if isinstance(x.get("days_to_unlock"), int) and x["days_to_unlock"] > 90)
    return {
        "n": len(rows),
        "earliest_ann_date": ann_dates[0] if ann_dates else "",
        "latest_ann_date": ann_dates[-1] if ann_dates else "",
        "earliest_unlock_date": unlock_dates[0] if unlock_dates else "",
        "latest_unlock_date": unlock_dates[-1] if unlock_dates else "",
        "past_unlock": past,
        "near_unlock_90d": near,
        "future_unlock": future,
    }


_PT_TRANSFER_FILES = (
    "cninfo_transfer.json", "share_transfer.json", "equity_change.json", "agreement_transfer.json",
    "sse_transfer.json", "shse_transfer.json", "sh_transfer.json",
    "sse_equity_change.json", "shse_equity_change.json", "sh_equity_change.json",
)


_PT_PLACEMENT_FILES = (
    "cninfo_placement.json", "asset_injection.json", "placement_status.json",
    "sse_placement.json", "shse_placement.json", "sh_placement.json",
)


def _pt_placement_batch_key(row, position):
    code = _pt_code(row)
    project_id = str(row.get("project_id") or "").strip()
    batch_id = str(row.get("batch_id") or "").strip()
    if code and (project_id or batch_id):
        return "identified", code, project_id or "unknown-project", batch_id or "unknown-batch"
    lifecycle_date = _pt_date(row, (
        "list_date", "listDate", "listing_date", "listingDate",
        "issue_date", "issueDate",
    ))
    if code and lifecycle_date:
        return "legacy", code, lifecycle_date
    return "unmatched", code or "unknown", str(position)


def _pt_placement_legacy_key(row):
    code = _pt_code(row)
    lifecycle_date = _pt_date(row, (
        "list_date", "listDate", "listing_date", "listingDate",
        "issue_date", "issueDate",
    ))
    return (code, lifecycle_date) if code and lifecycle_date else None


def _pt_placement_source_priority(row):
    dataset = str(row.get("dataset") or "").lower()
    source = str(row.get("lifecycle_source") or "").lower()
    confidence = str(row.get("lock_term_confidence") or "").lower()
    if confidence == "high" or "official" in source:
        return 400
    if "cninfo_placement" in dataset or "cninfo" in source:
        return 350
    if "asset_injection" in dataset or "eastmoney" in source:
        return 250
    if "placement_status" in dataset:
        return 100
    return 150


def _pt_merge_list_values(current, incoming):
    result = []
    seen = set()
    for value in list(current or []) + list(incoming or []):
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def _pt_merge_placement_rows(rows):
    merged = {}
    legacy_aliases = {}
    ordered = sorted(
        enumerate(rows),
        key=lambda pair: (
            bool(pair[1].get("project_id") or pair[1].get("batch_id")),
            _pt_placement_source_priority(pair[1]),
        ),
        reverse=True,
    )
    for position, row in ordered:
        proposed_key = _pt_placement_batch_key(row, position)
        legacy_key = _pt_placement_legacy_key(row)
        aliases = legacy_aliases.get(legacy_key, []) if legacy_key else []
        if proposed_key[0] == "legacy" and len(aliases) == 1:
            key = aliases[0]
        else:
            key = proposed_key
        current = merged.get(key)
        if current is None:
            current = dict(row)
            current["datasets"] = [str(row.get("dataset") or "")]
            current["_field_priority"] = {
                field: _pt_placement_source_priority(row) for field, value in row.items()
                if value not in (None, "", [], {})
            }
            if isinstance(row.get("stage_evidence"), dict):
                for stage, details in row["stage_evidence"].items():
                    if details:
                        current["_field_priority"][f"stage:{stage}"] = _pt_placement_source_priority(row)
            current["field_conflicts"] = []
            merged[key] = current
            if legacy_key:
                keys = legacy_aliases.setdefault(legacy_key, [])
                if key not in keys:
                    keys.append(key)
            continue
        row_priority = _pt_placement_source_priority(row)
        dataset = str(row.get("dataset") or "")
        if dataset and dataset not in current["datasets"]:
            current["datasets"].append(dataset)
        for field, value in row.items():
            if field in ("dataset", "field_conflicts") or value in (None, "", [], {}):
                continue
            if field == "stage_evidence" and isinstance(value, dict):
                evidence = current.setdefault("stage_evidence", {})
                for stage, details in value.items():
                    if details and (not evidence.get(stage) or row_priority >= current["_field_priority"].get(f"stage:{stage}", 0)):
                        if evidence.get(stage) and evidence[stage] != details:
                            current["field_conflicts"].append({
                                "field": f"stage_evidence.{stage}",
                                "kept": details,
                                "discarded": evidence[stage],
                                "kept_dataset": dataset,
                            })
                        evidence[stage] = details
                        current["_field_priority"][f"stage:{stage}"] = row_priority
                continue
            if field in ("lock_tranches", "lock_periods") and isinstance(value, list):
                current[field] = _pt_merge_list_values(current.get(field), value)
                current["_field_priority"][field] = max(row_priority, current["_field_priority"].get(field, 0))
                continue
            existing = current.get(field)
            if existing in (None, "", [], {}):
                current[field] = value
                current["_field_priority"][field] = row_priority
            elif existing != value:
                if field in ("lock_period", "lock", "lockPeriod"):
                    _a_label, a_months, a_days, _a_field = _placement_lock_term({"lock_period": existing})
                    _b_label, b_months, b_days, _b_field = _placement_lock_term({"lock_period": value})
                    if (a_months, a_days) != (None, None) and (a_months, a_days) == (b_months, b_days):
                        continue
                existing_priority = current["_field_priority"].get(field, 0)
                replace = row_priority > existing_priority
                current["field_conflicts"].append({
                    "field": field,
                    "kept": value if replace else existing,
                    "discarded": existing if replace else value,
                    "kept_dataset": dataset if replace else str(current.get("dataset") or ""),
                })
                if replace:
                    current[field] = value
                    current["_field_priority"][field] = row_priority
    for row in merged.values():
        datasets = [name for name in row.pop("datasets", []) if name]
        row.pop("_field_priority", None)
        row["dataset"] = " + ".join(datasets)
    return list(merged.values())


def _eventrisk_source_path(name: str) -> Path | None:
    shared = PREDICT_JSON.parent / name
    local = Path(__file__).resolve().parent / "data" / name
    candidates = []
    for path in (shared, local):
        if path in candidates or not path.is_file():
            continue
        candidates.append(path)
    try:
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None
    except OSError:
        return None


def _eventrisk_source_snapshot(names) -> dict:
    """Describe the selected source files without substituting request time."""
    found = 0
    valid = 0
    updated_values = []
    source_files = []
    for name in names:
        path = _eventrisk_source_path(name)
        payload = _eventrisk_load_json(name)
        if path is not None:
            found += 1
        if not isinstance(payload, (dict, list)):
            continue
        if path is None:
            # Also supports injected/custom loaders while retaining file-state
            # distinction for the normal on-disk loader.
            found += 1
        valid += 1
        source_files.append(name)
        value = ""
        if isinstance(payload, dict):
            value = str(
                payload.get("updated")
                or payload.get("updated_at")
                or payload.get("generated_at")
                or payload.get("as_of")
                or ""
            ).strip()
        if not value and path is not None:
            try:
                value = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                value = ""
        if value:
            updated_values.append(value)
    state = "ok" if valid else ("invalid" if found else "missing")
    return {
        "source_state": state,
        "updated": max(updated_values) if updated_values else "",
        "source_files": source_files,
    }


def _pt_transfer_source_snapshot() -> dict:
    snapshot = _eventrisk_source_snapshot(_PT_TRANSFER_FILES)
    if snapshot["source_state"] == "ok":
        overlay = _eventrisk_source_snapshot(("transfer_terms_overlay.json",))
        if overlay["source_state"] == "ok" and overlay["updated"] > snapshot["updated"]:
            snapshot["updated"] = overlay["updated"]
    return snapshot


def _pt_placement_source_snapshot() -> dict:
    return _eventrisk_source_snapshot(_PT_PLACEMENT_FILES)


def _pt_placement_source_updated():
    return _pt_placement_source_snapshot()["updated"]


def _pt_build_placement_data():
    placement_raw = _pt_merge_placement_rows(_pt_rows_from_files(_PT_PLACEMENT_FILES))
    placement = [_pt_normalize(row, "定增") for row in _eventrisk_enrich_placement(placement_raw)]
    codes = [row["code"] for row in placement if row.get("code")]
    meta = _meta_for_codes(codes)
    for row in placement:
        row["name"] = row.get("name") or (meta.get(row.get("code")) or {}).get("name", "")
    placement.sort(key=lambda row: (
        row.get("days_to_unlock") is None,
        row.get("days_to_unlock") if row.get("days_to_unlock") is not None else 999999,
        row.get("code", ""),
    ))
    return placement


def _pt_build_data():
    transfer_raw = _pt_rows_from_files(_PT_TRANSFER_FILES)
    transfer_terms = _pt_transfer_terms_index()
    transfer_raw = [_pt_apply_transfer_terms(row, transfer_terms) for row in transfer_raw]
    transfer = [_pt_normalize(r, "询转/协转") for r in _eventrisk_enrich_transfer(transfer_raw)]
    placement = _pt_build_placement_data()
    codes = [x["code"] for x in transfer + placement if x.get("code")]
    meta = _meta_for_codes(codes)
    for row in transfer + placement:
        row["name"] = row.get("name") or (meta.get(row.get("code")) or {}).get("name", "")
    transfer.sort(key=lambda x: (x.get("days_to_unlock") is None, x.get("days_to_unlock") or 999999, x.get("code", "")))
    placement.sort(key=lambda x: (x.get("days_to_unlock") is None, x.get("days_to_unlock") or 999999, x.get("code", "")))
    return transfer, placement


@app.route("/api/placement_transfer")
def api_placement_transfer():
    transfer_source = _pt_transfer_source_snapshot()
    placement_source = _pt_placement_source_snapshot()
    transfer, placement = _pt_build_data()
    source_states = {
        "transfer": transfer_source["source_state"],
        "placement": placement_source["source_state"],
    }
    if all(state == "ok" for state in source_states.values()):
        source_state = "ok"
    elif any(state == "ok" for state in source_states.values()):
        source_state = "partial"
    elif any(state == "invalid" for state in source_states.values()):
        source_state = "invalid"
    else:
        source_state = "missing"
    return jsonify({
        "updated": max(transfer_source["updated"], placement_source["updated"]),
        "source_state": source_state,
        "source_states": source_states,
        "transfer": transfer,
        "placement": placement,
        "counts": {"transfer": len(transfer), "placement": len(placement)},
        "summary": {"transfer": _pt_summary(transfer), "placement": _pt_summary(placement)},
    })


@app.route("/api/transfer_events")
def api_transfer_events():
    source = _pt_transfer_source_snapshot()
    transfer, _placement = _pt_build_data()
    return jsonify({
        "updated": source["updated"],
        "source_state": source["source_state"],
        "items": transfer,
        "count": len(transfer),
        "summary": _pt_summary(transfer),
    })


@app.route("/api/placement_events")
def api_placement_events():
    source = _pt_placement_source_snapshot()
    placement = _pt_build_placement_data()
    return jsonify({
        "updated": source["updated"],
        "source_state": source["source_state"],
        "items": placement,
        "count": len(placement),
        "summary": _pt_summary(placement),
    })


def _eventrisk_code_keys(code):
    raw = str(code or "").strip().upper()
    digits = "".join(ch for ch in raw if ch.isdigit())
    keys = {raw}
    if digits:
        keys.add(digits[:6])
        keys.add(f"{digits[:6]}.SZ")
        keys.add(f"{digits[:6]}.SH")
        keys.add(f"SZ{digits[:6]}")
        keys.add(f"SH{digits[:6]}")
    return {key for key in keys if key}


_EVENTRISK_CODE_FIELDS = (
    "code", "ts_code", "symbol", "stock_code", "secCode", "sec_code",
)


def _eventrisk_row_code_keys(row):
    keys = set()
    if not isinstance(row, dict):
        return keys
    for field in _EVENTRISK_CODE_FIELDS:
        keys |= _eventrisk_code_keys(row.get(field))
    return keys


def _eventrisk_match_code(row, keys):
    return bool(_eventrisk_row_code_keys(row) & keys)


def _eventrisk_rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "rows", "events", "data"):
            if isinstance(payload.get(key), list):
                return payload.get(key)
        # ``event_avoid.json`` groups rows by event category.  Flatten those
        # buckets for consumers that operate on a common event-risk row shape,
        # while retaining the category as useful provenance.
        categories = payload.get("cats")
        if isinstance(categories, dict):
            rows = []
            for category, category_payload in categories.items():
                for row in _eventrisk_rows(category_payload):
                    if isinstance(row, dict):
                        item = dict(row)
                        item.setdefault("category", category)
                        rows.append(item)
            return rows
    return []


def _eventrisk_severity(row):
    value = str(row.get("severity") or row.get("risk") or "").lower()
    if value in ("high", "red", "severe", "danger"):
        return 3
    if value in ("medium", "watch", "yellow"):
        return 2
    if value in ("low", "info", "green"):
        return 1
    text = " ".join(str(row.get(k) or "") for k in ("title", "reason", "summary", "type", "event"))
    high_words = ("立案", "处罚", "调查", "终止", "违约", "亏损", "下修", "问询", "诉讼", "冻结")
    medium_words = ("减持", "定增", "发行", "价格下行", "砍单", "出口限制", "监管", "传闻")
    if any(word in text for word in high_words):
        return 3
    if any(word in text for word in medium_words):
        return 2
    return 1


def _eventrisk_load_json(name):
    request_cache = _request_cache_namespace("event_risk")
    json_cache = request_cache.setdefault("json", {}) if request_cache is not None else None
    if json_cache is not None and name in json_cache:
        return json_cache[name]
    selected = _eventrisk_source_path(name)
    # Keep the historical loader contract: custom/test readers are still
    # invoked with the local candidate even when no file currently exists.
    if selected is None:
        selected = Path(__file__).resolve().parent / "data" / name
    payload = _read_json(selected)
    if json_cache is not None:
        json_cache[name] = payload
    return payload


def _eventrisk_company_index(name):
    request_cache = _request_cache_namespace("event_risk")
    if request_cache is None:
        return None
    indexes = request_cache.setdefault("company_indexes", {})
    if name in indexes:
        return indexes[name]
    index = defaultdict(list)
    for position, row in enumerate(_eventrisk_rows(_eventrisk_load_json(name))):
        if not isinstance(row, dict):
            continue
        for key in _eventrisk_row_code_keys(row):
            index[key].append((position, row))
    indexes[name] = index
    return index


def _eventrisk_pick_company_rows(file_names, keys):
    rows = []
    for name in file_names:
        index = _eventrisk_company_index(name)
        if index is None:
            matches = [
                (position, row)
                for position, row in enumerate(_eventrisk_rows(_eventrisk_load_json(name)))
                if isinstance(row, dict) and _eventrisk_match_code(row, keys)
            ]
        else:
            by_position = {}
            for key in keys:
                for position, row in index.get(key, ()):
                    by_position.setdefault(position, row)
            matches = sorted(by_position.items())
        rows.extend({**row, "dataset": name} for _, row in matches)
    return rows


def _eventrisk_pick_industry_rows(payload, industry):
    industry = str(industry or "").strip()
    if not industry:
        return []
    request_cache = _request_cache_namespace("event_risk")
    industry_cache = request_cache.setdefault("industry_rows", {}) if request_cache is not None else None
    cache_key = (id(payload), industry)
    if industry_cache is not None and cache_key in industry_cache:
        return industry_cache[cache_key]
    rows = []
    source_rows = []
    if isinstance(payload, dict):
        source_rows.extend(payload.get("industry") or [])
        source_rows.extend(payload.get("industries") or [])
    source_rows.extend(_eventrisk_rows(payload))
    for row in source_rows:
        if not isinstance(row, dict):
            continue
        text = " ".join(str(row.get(k) or "") for k in ("industry", "sector", "theme", "title", "summary"))
        if industry in text:
            rows.append(row)
    if industry_cache is not None:
        industry_cache[cache_key] = rows
    return rows


def _eventrisk_status(rows):
    if not rows:
        return "none"
    score = max(_eventrisk_severity(row) for row in rows)
    return "high" if score >= 3 else ("watch" if score == 2 else "low")


def _eventrisk_float(row, keys):
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def _placement_qlib_code(code):
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())[:6]
    if not digits:
        return ""
    if digits.startswith(("6", "9")):
        return f"sh{digits}"
    if digits.startswith(("4", "8")):
        return f"bj{digits}"
    return f"sz{digits}"


def _placement_market_context(code):
    qcode = _placement_qlib_code(code)
    if not qcode:
        return {}
    request_cache = _request_cache_namespace("event_risk")
    contexts = request_cache.setdefault("market_context", {}) if request_cache is not None else None
    if contexts is not None and qcode in contexts:
        return contexts[qcode]
    try:
        d = load_ohlcv(qcode, last_n_days=35, adjust="qfq")
        close = [float(x) for x in (d.get("close") or []) if x is not None and np.isfinite(float(x))]
        dates = d.get("dates") or []
        if not close:
            result = {}
        else:
            last = close[-1]
            ma20 = float(np.mean(close[-20:])) if len(close) >= 20 else None
            result = {
                "current_price": round(last, 4),
                "market_date": dates[-1] if dates else "",
                "avg20": round(ma20, 4) if ma20 else None,
            }
    except Exception:
        result = {}
    if contexts is not None:
        contexts[qcode] = result
    return result


def _placement_support_label(level):
    return {
        "strong": "跌破发行价/九折底价",
        "watch": "接近发行价/九折底价",
        "normal": "高于发行价/九折底价",
        "unknown_price": "缺少现价或定价",
    }.get(str(level or ""), str(level or ""))


def _placement_lock_term(row):
    for field in ("lock_period", "lock", "lockPeriod", "lockin_period", "LOCKIN_PERIOD"):
        value = str(row.get(field) or "").strip()
        if not value:
            continue
        year_match = re.search(r"(\d+(?:\.\d+)?)\s*年", value)
        if year_match:
            return value, int(round(float(year_match.group(1)) * 12)), None, field
        month_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:个?月)", value)
        if month_match:
            return value, int(round(float(month_match.group(1)))), None, field
        day_match = re.search(r"(\d+)\s*天", value)
        if day_match:
            return value, None, int(day_match.group(1)), field
        return value, None, None, field
    months = _eventrisk_float(row, ("lock_months", "lockMonths"))
    if months is not None and months > 0:
        rounded = int(round(months))
        return f"{rounded}个月", rounded, None, "lock_months"
    return "", None, None, ""


def _placement_lock_terms(row):
    """Return distinct, parseable lock terms without inventing a default term."""
    values = row.get("lock_tranches")
    if not isinstance(values, list) or not values:
        values = row.get("lock_periods")
    candidates = values if isinstance(values, list) else []
    if not candidates:
        single = row.get("lock_period") or row.get("lock") or row.get("lockPeriod")
        candidates = [single] if single not in (None, "") else []
    result = []
    seen = set()
    for value in candidates:
        if isinstance(value, dict):
            if value.get("applies_to_new_shares") is False:
                continue
            label = value.get("lock_period") or value.get("period") or value.get("label") or ""
            scope = str(value.get("scope") or value.get("transaction_scope") or "unknown")
            holder = str(value.get("holder") or value.get("subscriber") or "")
        else:
            label = value
            scope = "unknown"
            holder = ""
        label = str(label or "").strip()
        if not label:
            continue
        probe = dict(row, lock_period=label, lock_periods=[])
        term, months, days, _field = _placement_lock_term(probe)
        key = (term, months, days, scope, holder)
        if key in seen:
            continue
        seen.add(key)
        item = {"lock_period": term, "months": months, "days": days, "scope": scope}
        if holder:
            item["holder"] = holder
        if isinstance(value, dict):
            for field in (
                "basis", "page", "url", "start_date", "start_event",
                "tradable_date_official", "confidence", "conditional", "conflicts",
                "lock_start_date", "lock_start_basis", "holder_scope",
                "holder_or_group", "term_text", "evidence",
            ):
                if value.get(field) not in (None, ""):
                    item[field] = value[field]
        result.append(item)
    return result


def _placement_tranche_start(term, row):
    explicit = _eventrisk_parse_date(term.get("start_date") or term.get("lock_start_date"))
    if explicit is not None:
        return explicit, str(term.get("start_event") or term.get("lock_start_basis") or "tranche_start"), "high"

    event = str(term.get("start_event") or term.get("lock_start_basis") or "").lower()
    field_map = {
        "issue_end_date": ("issue_end_date", "issuance_completion_date"),
        "listing_date": ("pdf_listing_date", "list_date"),
        "share_registration_date": ("share_registration_date",),
        "first_batch_end": ("first_batch_end_date",),
    }
    for field in field_map.get(event, ()):
        if field == "list_date" and not str(row.get("list_date_source") or "").strip():
            continue
        value = _eventrisk_parse_date(row.get(field))
        if value is not None:
            return value, event, "high" if field != "list_date" else "medium"

    if event == "issue_end_date":
        issue_source = str(row.get("issue_date_source") or "").lower()
        if any(token in issue_source for token in ("eastmoney", "rpt_seo_detail.issue_date")):
            value = _eventrisk_parse_date(row.get("issue_date"))
            if value is not None:
                return value, "issue_date_proxy_for_issue_end", "medium"

    row_basis = str(row.get("lock_start_basis") or "").lower()
    if event in ("", "not_explicit_in_clause", "not_provided_by_secondary_source") and row_basis in field_map:
        value = _eventrisk_parse_date(row.get("lock_start_date"))
        if value is not None:
            return value, row_basis, "medium"
    return None, event, "pending"


def _placement_regulatory_context(row, terms):
    """Version-aware, additive rule review that never creates a lock term."""
    code = _pt_code(row)
    reference_date = None
    reference_basis = ""
    for field in (
        "rule_applicability_date", "issuance_launch_date", "issue_date", "list_date",
    ):
        reference_date = _eventrisk_parse_date(row.get(field))
        if reference_date is not None:
            reference_basis = field
            break
    reference_text = reference_date.strftime("%Y-%m-%d") if reference_date else ""
    title_parts = [str(row.get("title") or "")]
    evidence = row.get("stage_evidence")
    if isinstance(evidence, dict):
        title_parts.extend(str(item.get("title") or "") for item in evidence.values() if isinstance(item, dict))
    title = " ".join(title_parts)
    category = str(row.get("placement_category") or row.get("transaction_type") or "").lower()
    is_restructuring = any(token in title for token in ("发行股份购买资产", "重大资产重组", "重组上市")) or any(
        token in category for token in ("asset", "restructur", "m&a", "merger")
    )
    is_mixed = is_restructuring and any(token in title for token in ("募集配套资金", "配套融资"))
    is_bse = code.startswith(("4", "8", "92"))

    applicable_rules = []
    if reference_date is None:
        regime = "制度版本待日期证据"
        expected_months = []
        rule_status = "pending_date_evidence"
    elif is_bse:
        regime = "北交所向特定对象发行规则"
        expected_months = [6, 12]
        rule_status = "pending_evidence"
        applicable_rules.append({
            "scope": "base_issuance",
            "name": regime,
            "status": "effective_for_date_review",
            "baseline_months": expected_months,
            "url": "https://www.csrc.gov.cn/csrc/c106256/c1653979/content.shtml",
        })
    elif reference_date < datetime(2020, 2, 14):
        regime = "2020-02-14前非公开发行规则"
        expected_months = [12, 36]
        rule_status = "pending_evidence"
        applicable_rules.append({
            "scope": "base_issuance",
            "name": regime,
            "status": "historical_transition_review",
            "baseline_months": expected_months,
            "url": "https://www.csrc.gov.cn/csrc/c100028/c1000837/content.shtml",
        })
    else:
        regime = "现行向特定对象发行注册规则"
        expected_months = [6, 18]
        rule_status = "pending_evidence"
        applicable_rules.append({
            "scope": "base_issuance",
            "name": regime,
            "status": "effective_for_date_review",
            "effective_from": "2023-02-17",
            "baseline_months": expected_months,
            "url": "https://www.csrc.gov.cn/csrc/c101953/c7121921/content.shtml",
        })

    if reference_date is not None and is_restructuring:
        restructuring_name = "重大资产重组逐笔规则"
        if reference_date >= datetime(2025, 5, 16):
            restructuring_name = "2025-05-16后重大资产重组规则"
        applicable_rules.append({
            "scope": "restructuring",
            "name": restructuring_name,
            "status": "effective_for_date_review",
            "effective_from": "2025-05-16" if reference_date >= datetime(2025, 5, 16) else "",
            "baseline_months": [],
            "url": "https://www.csrc.gov.cn/csrc/c101953/c7558586/content.shtml",
        })
        regime = restructuring_name
        rule_status = "transaction_specific"

    observed_months = sorted({term["months"] for term in terms if term.get("months") is not None})
    if terms and reference_date is None:
        rule_status = "pending_date_evidence"
    elif terms:
        if is_mixed:
            rule_status = "mixed_tranches_announcement_controls"
        elif is_restructuring:
            rule_status = "announcement_controls"
        elif len(observed_months) > 1:
            rule_status = "multiple_terms_announcement_controls"
        elif observed_months and set(observed_months).issubset(set(expected_months)):
            rule_status = "consistent"
        else:
            # Special commitments, holder identity and transaction structure can
            # lawfully produce other terms; flag review instead of declaring an error.
            rule_status = "special_term_review"
    if observed_months and any(month not in expected_months for month in observed_months):
        term_evidence = row.get("lock_term_evidence")
        if isinstance(term_evidence, dict):
            special_url = str(term_evidence.get("url") or "")
        elif isinstance(term_evidence, list):
            special_url = next((str(item.get("url") or "") for item in term_evidence if isinstance(item, dict) and item.get("url")), "")
        else:
            special_url = ""
        applicable_rules.append({
            "scope": "special_or_commitment",
            "name": "行业、股东身份、交易结构或专项承诺",
            "status": "announcement_review",
            "baseline_months": [],
            "url": special_url,
        })
    rule_url = next((item.get("url") for item in applicable_rules if item.get("url")), "")
    tranche_reviews = []
    for term in terms:
        scope = str(term.get("scope") or "unknown").lower()
        months = term.get("months")
        if reference_date is None:
            status = "pending_date_evidence"
        elif scope in ("asset_consideration", "restructuring", "m_and_a", "m&a"):
            status = "announcement_controls"
        elif scope in ("supporting_finance", "refinancing", "cash_subscription"):
            status = "consistent" if months in expected_months else "special_term_review"
        elif is_mixed:
            status = "scope_pending_announcement"
        elif is_restructuring:
            status = "announcement_controls"
        else:
            status = "consistent" if months in expected_months else "special_term_review"
        tranche_reviews.append({
            "lock_period": term.get("lock_period") or "",
            "scope": scope,
            "holder": term.get("holder") or "",
            "status": status,
        })
    return {
        "reference_date": reference_text,
        "reference_date_basis": reference_basis,
        "regime": regime,
        "transaction_type": "mixed" if is_mixed else ("restructuring" if is_restructuring else "refinancing"),
        "expected_months": expected_months,
        "observed_months": observed_months,
        "status": rule_status,
        "rule_url": rule_url,
        "applicable_rules": applicable_rules,
        "tranche_reviews": tranche_reviews,
        "rules_are_validation_only": True,
        "evidence_priority": "official_unlock_date_then_official_terms_then_structured_source",
    }


def _placement_unlock_evidence(row):
    dataset = str(row.get("dataset") or row.get("lifecycle_source") or "placement_source")
    for field in (
        "tradable_date_official", "official_unlock_date",
        "unlock_date", "unlockDate", "lock_expiry_date", "lockExpiryDate",
    ):
        unlock_date = _eventrisk_parse_date(row.get(field))
        if unlock_date is not None:
            return {
                "unlock_date": unlock_date.strftime("%Y-%m-%d"),
                "unlock_estimated": False,
                "unlock_basis": "official_tradable_date" if field in (
                    "tradable_date_official", "official_unlock_date"
                ) else "explicit_unlock_date",
                "unlock_source": f"{dataset}:{field}",
                "unlock_confidence": "high",
            }

    terms = _placement_lock_terms(row)
    lock_labels = list(dict.fromkeys(term["lock_period"] for term in terms))
    lock_period = " / ".join(lock_labels)
    schedule = []
    for term in terms:
        official_date = _eventrisk_parse_date(term.get("tradable_date_official"))
        term_start, term_start_basis, term_start_confidence = _placement_tranche_start(term, row)
        if official_date is not None:
            unlock_date = official_date
            estimated = False
        elif term.get("conditional") or term_start is None:
            continue
        elif term.get("months") is not None:
            unlock_date = _eventrisk_add_months(term_start, term["months"])
            estimated = True
        elif term.get("days") is not None:
            unlock_date = term_start + timedelta(days=term["days"])
            estimated = True
        else:
            continue
        schedule_item = {
            "lock_period": term["lock_period"],
            "unlock_date": unlock_date.strftime("%Y-%m-%d"),
            "scope": term.get("scope") or "unknown",
            "estimated": estimated,
        }
        if term_start is not None:
            schedule_item["lock_start_date"] = term_start.strftime("%Y-%m-%d")
        if term_start_basis:
            schedule_item["start_event"] = term_start_basis
        schedule_item["start_confidence"] = term_start_confidence
        if term.get("holder"):
            schedule_item["holder"] = term["holder"]
        schedule.append(schedule_item)
    if schedule:
        schedule.sort(key=lambda item: (item["unlock_date"], item["lock_period"]))
        term_confidence = str(row.get("lock_term_confidence") or "").lower()
        all_official = all(not item["estimated"] for item in schedule)
        all_explicit_starts = all(
            not item["estimated"] or item.get("start_confidence") == "high" for item in schedule
        )
        confidence = "high" if all_official or (
            term_confidence == "high" and all_explicit_starts
        ) else "medium"
        if len(schedule) > 1:
            basis = "tranche_schedule"
        elif all_official:
            basis = "official_tradable_date"
        else:
            basis = "source_lock_period"
        start_dates = sorted({item.get("lock_start_date") for item in schedule if item.get("lock_start_date")})
        start_events = sorted({item.get("start_event") for item in schedule if item.get("start_event")})
        return {
            "lock_period": lock_period,
            "lock_periods": lock_labels,
            "lock_start_date": start_dates[0] if len(start_dates) == 1 else "",
            "lock_start_basis": start_events[0] if len(start_events) == 1 else ("multiple" if start_events else ""),
            "unlock_date": schedule[0]["unlock_date"],
            "unlock_date_latest": schedule[-1]["unlock_date"],
            "unlock_schedule": schedule,
            "unlock_estimated": not all_official,
            "unlock_basis": basis,
            "unlock_source": f"{dataset}:tranche_evidence",
            "unlock_confidence": confidence,
        }
    return {
        "lock_period": lock_period,
        "lock_periods": lock_labels,
        "lock_start_date": "",
        "lock_start_basis": row.get("lock_start_basis") or "",
        "unlock_schedule": [],
        "unlock_date_latest": "",
        "unlock_date": "",
        "unlock_estimated": False,
        "unlock_basis": "pending_source_evidence",
        "unlock_source": f"{dataset}:missing_explicit_term_or_lock_start",
        "unlock_confidence": "pending",
    }


def _eventrisk_enrich_placement(rows):
    enriched = []
    for row in rows:
        item = dict(row)
        ctx = _placement_market_context(item.get("code") or item.get("ts_code") or item.get("symbol") or item.get("secCode"))
        for k, v in ctx.items():
            item.setdefault(k, v)
        issue_price = _eventrisk_float(item, [
            "issue_price", "issuePrice", "offer_price", "placement_price",
            "发行价", "发行价格",
        ])
        current_price = _eventrisk_float(item, [
            "current_price", "price", "close", "last_price", "最新价",
        ])
        avg20 = _eventrisk_float(item, [
            "avg20", "ma20", "twenty_day_avg", "pricing_base", "20日均价",
        ])
        discount_floor = avg20 * 0.9 if avg20 is not None else None
        if issue_price is not None:
            item["issue_price"] = issue_price
        if current_price is not None:
            item["current_price"] = current_price
        if discount_floor is not None:
            item["discount_floor"] = round(discount_floor, 4)
        reference = issue_price if issue_price is not None else discount_floor
        if current_price is not None and reference:
            gap = (current_price / reference - 1) * 100
            item["price_gap_pct"] = round(gap, 2)
            item["support_motive"] = "strong" if gap < 0 else ("watch" if gap < 5 else "normal")
            item["placement_support_level"] = item["support_motive"]
            item["placement_support_label"] = _placement_support_label(item["support_motive"])
            if issue_price is not None:
                item["issue_price_gap_pct"] = round((current_price / issue_price - 1) * 100, 2)
            if discount_floor is not None:
                item["discount_floor_gap_pct"] = round((current_price / discount_floor - 1) * 100, 2)
        elif issue_price is not None or discount_floor is not None:
            item["support_motive"] = "unknown_price"
            item["placement_support_level"] = "unknown_price"
            item["placement_support_label"] = _placement_support_label("unknown_price")
        item.update(_placement_unlock_evidence(item))
        item["regulatory_context"] = _placement_regulatory_context(
            item, _placement_lock_terms(item)
        )
        unlock_date = _eventrisk_parse_date(item.get("unlock_date"))
        if unlock_date is not None:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            item["days_to_unlock"] = (unlock_date - today).days
        enriched.append(item)
    return enriched


def _eventrisk_parse_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if len(text) >= 8 and text[:8].isdigit():
        text = text[:8]
    else:
        text = text[:10].replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text.replace("-", "") if fmt == "%Y%m%d" else text, fmt)
        except Exception:
            continue
    return None


def _eventrisk_add_months(dt, months):
    month = dt.month - 1 + int(months)
    year = dt.year + month // 12
    month = month % 12 + 1
    days_in_month = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    day = min(dt.day, days_in_month)
    return dt.replace(year=year, month=month, day=day)


def _eventrisk_first_date(row, keys):
    for key in keys:
        dt = _eventrisk_parse_date(row.get(key))
        if dt is not None:
            return dt
    return None


def _eventrisk_enrich_transfer(rows):
    enriched = []
    for row in rows:
        item = dict(row)
        ratio = _eventrisk_float(item, [
            "transfer_ratio", "ratio", "pct", "share_ratio", "percent",
            "转让比例", "权益变动比例",
        ])
        price = _eventrisk_float(item, [
            "transfer_price", "price", "deal_price", "agreement_price",
            "转让价格", "每股价格",
        ])
        if ratio is not None:
            item["transfer_ratio"] = round(ratio, 4)
        if price is not None:
            item["transfer_price"] = round(price, 4)
        base_date = _eventrisk_first_date(item, [
            "unlock_date", "unlockDate", "lock_expiry_date", "lockExpiryDate",
            "date", "trade_date", "tradeDate", "transfer_date", "transferDate",
            "trans_date", "transDate", "change_date", "changeDate",
            "register_date", "registerDate", "registrationDate", "complete_date",
            "completeDate", "completion_date", "completionDate", "over_date",
            "overDate", "deal_date", "dealDate", "sign_date", "signDate",
            "signed_date", "signedDate", "agreement_date", "agreementDate",
            "ann_date", "annDate", "announcement_date", "announcementDate",
            "disclosure_date", "disclosureDate", "publish_date", "publishDate",
            "过户日期", "协议签署日", "协议签署日期", "权益变动日期", "转让日期", "公告日期",
        ])
        if item.get("unlock_date"):
            unlock_date = _eventrisk_parse_date(item.get("unlock_date"))
        elif base_date is not None:
            unlock_date = _eventrisk_add_months(base_date, 6)
            item["lock_months"] = 6
        else:
            unlock_date = None
        if unlock_date is not None:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            item["unlock_date"] = unlock_date.strftime("%Y-%m-%d")
            item["days_to_unlock"] = (unlock_date - today).days
        if ratio is not None and ratio >= 10:
            item["risk_level"] = "high"
        elif ratio is not None and ratio >= 3:
            item["risk_level"] = "watch"
        enriched.append(item)
    return enriched


def _eventrisk_enrich_unlock(rows):
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    enriched = []
    for row in rows:
        item = dict(row)
        unlock_date = None
        for key in ("unlock_date", "date", "list_date", "release_date", "解禁日期", "上市流通日"):
            unlock_date = _eventrisk_parse_date(item.get(key))
            if unlock_date is not None:
                break
        ratio = _eventrisk_float(item, [
            "unlock_ratio", "ratio", "pct", "float_ratio", "circulation_ratio",
            "解禁比例", "占总股本比例", "占流通股比例",
        ])
        if unlock_date is not None:
            item["unlock_date"] = unlock_date.strftime("%Y-%m-%d")
            item["days_to_unlock"] = (unlock_date - today).days
        if ratio is not None:
            item["unlock_ratio"] = round(ratio, 4)
        days = item.get("days_to_unlock")
        if isinstance(days, int) and 0 <= days <= 90:
            if ratio is not None and ratio >= 5:
                item["risk_level"] = "high"
            else:
                item["risk_level"] = "watch"
        enriched.append(item)
    return enriched


def _eventrisk_transfer_status(rows):
    if not rows:
        return "none"
    if any(str(row.get("risk_level") or "").lower() == "high" or _eventrisk_severity(row) >= 3 for row in rows):
        return "high"
    return "watch"


def _eventrisk_unlock_status(rows):
    if not rows:
        return "none"
    near_rows = [
        row for row in rows
        if isinstance(row.get("days_to_unlock"), int) and 0 <= row.get("days_to_unlock") <= 90
    ]
    if not near_rows:
        return "low"
    if any(str(row.get("risk_level") or "").lower() == "high" for row in near_rows):
        return "high"
    return "watch"


def _eventrisk_sort_by_unlock(rows):
    def key(row):
        days = row.get("days_to_unlock")
        if isinstance(days, int):
            return (0 if days >= 0 else 1, abs(days), row.get("unlock_date") or "")
        return (2, 999999, row.get("date") or row.get("ann_date") or "")

    return sorted(rows or [], key=key)


def _eventrisk_transfer_stage(row):
    title = str(row.get("title") or row.get("name") or "")
    if "结果报告" in title or "实施完毕" in title or "转让结果" in title:
        return 0
    if "核查报告" in title and "资格" not in title:
        return 1
    if "定价" in title:
        return 2
    if "计划" in title or "资格" in title or "核查意见" in title:
        return 3
    return 4


def _eventrisk_sort_transfer(rows):
    def key(row):
        stage = _eventrisk_transfer_stage(row)
        days = row.get("days_to_unlock")
        if isinstance(days, int):
            return (0 if days >= 0 else 1, stage, abs(days), row.get("unlock_date") or "")
        return (2, stage, 999999, row.get("date") or row.get("ann_date") or "")

    return sorted(rows or [], key=key)


def _build_event_risk_report(code, industry=""):
    keys = _eventrisk_code_keys(code)
    request_cache = _request_cache_namespace("event_risk")
    reports = request_cache.setdefault("reports", {}) if request_cache is not None else None
    report_key = (_code6(code) or str(code or "").strip().upper(), str(industry or "").strip())
    if reports is not None and report_key in reports:
        return reports[report_key]
    placement_rows = []
    for name in (
        "placement_status.json", "cninfo_placement.json", "asset_injection.json",
        "sse_placement.json", "shse_placement.json", "sh_placement.json",
    ):
        placement_rows.extend(_eventrisk_pick_company_rows([name], keys))
    placement_rows = _eventrisk_enrich_placement(placement_rows)
    placement_rows = _eventrisk_sort_by_unlock(placement_rows)

    transfer_rows = []
    for name in (
        "cninfo_transfer.json", "share_transfer.json", "equity_change.json", "agreement_transfer.json",
        "sse_transfer.json", "shse_transfer.json", "sh_transfer.json",
        "sse_equity_change.json", "shse_equity_change.json", "sh_equity_change.json",
        "sse_agreement_transfer.json", "shse_agreement_transfer.json", "sh_agreement_transfer.json",
    ):
        transfer_rows.extend(_eventrisk_pick_company_rows([name], keys))
    transfer_rows = _eventrisk_enrich_transfer(transfer_rows)
    transfer_rows = _eventrisk_sort_transfer(transfer_rows)

    unlock_rows = []
    for name in (
        "cninfo_unlock.json", "unlock_calendar.json", "share_unlock.json", "restricted_unlock.json",
        "sse_unlock.json", "shse_unlock.json", "sh_unlock.json",
        "sse_restricted_unlock.json", "shse_restricted_unlock.json", "sh_restricted_unlock.json",
    ):
        unlock_rows.extend(_eventrisk_pick_company_rows([name], keys))
    unlock_rows = _eventrisk_enrich_unlock(unlock_rows)
    unlock_rows = _eventrisk_sort_by_unlock(unlock_rows)

    news_payload = _eventrisk_load_json("event_risk_news.json") or {}
    company_rows = []
    if isinstance(news_payload, dict):
        company_rows.extend(
            row for row in (news_payload.get("company") or [])
            if isinstance(row, dict) and _eventrisk_match_code(row, keys)
        )
    company_rows.extend(_eventrisk_pick_company_rows([
        "event_avoid.json",
        "inquiry_letter.json",
        "investigation_avoid.json",
        "late_disclosure.json",
        "repo_cancel.json",
    ], keys))
    industry_rows = _eventrisk_pick_industry_rows(news_payload, industry)

    placement_status = "high" if placement_rows else "none"
    transfer_status = _eventrisk_transfer_status(transfer_rows)
    unlock_status = _eventrisk_unlock_status(unlock_rows)
    company_status = _eventrisk_status(company_rows)
    industry_status = _eventrisk_status(industry_rows)
    high_count = [placement_status, transfer_status, unlock_status, company_status, industry_status].count("high")
    watch_count = [placement_status, transfer_status, unlock_status, company_status, industry_status].count("watch")
    if high_count >= 2 or company_status == "high":
        decision = "排除" if high_count >= 2 else "降级"
    elif placement_status == "high" or unlock_status == "high":
        decision = "降级"
    elif watch_count or transfer_status == "high" or industry_status == "high":
        decision = "观察"
    else:
        decision = "通过"
    result = {
        "code": next((k for k in keys if k.isdigit()), str(code or ""))[:6],
        "industry": industry,
        "placement": {
            "status": placement_status,
            "items": placement_rows[:20],
            "source_hint": "placement_status.json / cninfo_placement.json / sse_placement.json / asset_injection.json",
        },
        "transfer": {
            "status": transfer_status,
            "items": transfer_rows[:20],
            "source_hint": "cninfo_transfer.json / sse_transfer.json / equity_change.json / agreement_transfer.json",
        },
        "unlock": {
            "status": unlock_status,
            "items": unlock_rows[:20],
            "source_hint": "cninfo_unlock.json / sse_unlock.json / unlock_calendar.json / restricted_unlock.json",
        },
        "company_negative": {"status": company_status, "items": company_rows[:20]},
        "industry_negative": {"status": industry_status, "items": industry_rows[:20]},
        "summary": {
            "decision": decision,
            "note": "仅聚合本地缓存和公告/新闻采集结果；传闻必须人工核实后再作为排除依据。",
        },
    }
    if reports is not None:
        reports[report_key] = result
    return result


def _unlock_item_brief(row, source):
    if not isinstance(row, dict):
        return None
    unlock_date = row.get("unlock_date")
    if not unlock_date:
        return None
    out = {
        "type": source,
        "unlock_date": unlock_date,
        "days_to_unlock": row.get("days_to_unlock"),
        "risk_level": row.get("risk_level") or row.get("support_motive") or "",
        "dataset": row.get("dataset") or "",
        "title": row.get("title") or row.get("name") or row.get("summary") or row.get("event") or "",
    }
    for src, dst in (
        ("unlock_ratio", "ratio"),
        ("transfer_ratio", "ratio"),
        ("issue_price", "issue_price"),
        ("current_price", "current_price"),
        ("market_date", "market_date"),
        ("avg20", "avg20"),
        ("discount_floor", "discount_floor"),
        ("transfer_price", "transfer_price"),
        ("price_gap_pct", "price_gap_pct"),
        ("issue_price_gap_pct", "issue_price_gap_pct"),
        ("discount_floor_gap_pct", "discount_floor_gap_pct"),
        ("placement_support_label", "placement_support_label"),
        ("placement_support_level", "placement_support_level"),
    ):
        if row.get(src) is not None:
            out[dst] = row.get(src)
    return out


def _unlock_sort_key(item):
    days = item.get("days_to_unlock")
    if isinstance(days, int):
        return (0 if days >= 0 else 1, abs(days), item.get("unlock_date") or "")
    return (2, 999999, item.get("unlock_date") or "")


def _unlock_transfer_sort_key(item):
    title = str(item.get("title") or "")
    if "结果报告" in title or "实施完毕" in title or "转让结果" in title:
        stage = 0
    elif "核查报告" in title and "资格" not in title:
        stage = 1
    elif "定价" in title:
        stage = 2
    elif "计划" in title or "资格" in title or "核查意见" in title:
        stage = 3
    else:
        stage = 4
    days = item.get("days_to_unlock")
    if isinstance(days, int):
        return (0 if days >= 0 else 1, stage, abs(days), item.get("unlock_date") or "")
    return (2, stage, 999999, item.get("unlock_date") or "")


def _unlock_summary_for_code(code, industry=""):
    request_cache = _request_cache_namespace("event_risk")
    summaries = request_cache.setdefault("summaries", {}) if request_cache is not None else None
    summary_key = _code6(code) or str(code or "").strip().upper()
    if summaries is not None and summary_key in summaries:
        return summaries[summary_key]
    try:
        report = _build_event_risk_report(code, industry=industry)
    except Exception:
        result = {"status": "unknown", "label": "解禁核查失败", "placement": [], "transfer": [], "other": []}
        if summaries is not None:
            summaries[summary_key] = result
        return result

    placement = [
        x for x in (_unlock_item_brief(r, "定增解禁")
                    for r in ((report.get("placement") or {}).get("items") or []))
        if x
    ]
    transfer = [
        x for x in (_unlock_item_brief(r, "协转解禁")
                    for r in ((report.get("transfer") or {}).get("items") or []))
        if x
    ]
    other = [
        x for x in (_unlock_item_brief(r, "其他解禁")
                    for r in ((report.get("unlock") or {}).get("items") or []))
        if x
    ]
    for arr in (placement, other):
        arr.sort(key=_unlock_sort_key)
    transfer.sort(key=_unlock_transfer_sort_key)

    all_items = placement + transfer + other
    status = "none"
    if any(str(x.get("risk_level")).lower() == "high" for x in all_items):
        status = "high"
    elif any(isinstance(x.get("days_to_unlock"), int) and 0 <= x["days_to_unlock"] <= 90 for x in all_items):
        status = "watch"
    elif all_items:
        status = "low"

    def one(arr, name):
        if not arr:
            return ""
        x = arr[0]
        days = x.get("days_to_unlock")
        dtext = "" if not isinstance(days, int) else (f"{days}天" if days >= 0 else f"已过{abs(days)}天")
        ratio = x.get("ratio")
        rtext = "" if ratio is None else f" {ratio}%"
        return f"{name}{x.get('unlock_date')}{('('+dtext+')') if dtext else ''}{rtext}"

    def transfer_one(arr):
        if not arr:
            return ""
        future = [x for x in arr if isinstance(x.get("days_to_unlock"), int) and x["days_to_unlock"] >= 0]
        source = future or arr
        earliest = sorted(source, key=_unlock_sort_key)[0]
        final = sorted(source, key=_unlock_transfer_sort_key)[0]
        picks = []
        for x in (earliest, final):
            if x and x.get("unlock_date") and x.get("unlock_date") not in [p.get("unlock_date") for p in picks]:
                picks.append(x)
        parts = []
        for x in picks[:2]:
            days = x.get("days_to_unlock")
            dtext = "" if not isinstance(days, int) else (f"{days}天" if days >= 0 else f"已过{abs(days)}天")
            tag = "结果" if _unlock_transfer_sort_key(x)[1] <= 1 else "预警"
            parts.append(f"{x.get('unlock_date')}{('('+dtext+')') if dtext else ''}{tag}")
        return "协转" + "/".join(parts)

    labels = [one(placement, "定增"), transfer_one(transfer), one(other, "其他")]
    label = "；".join([x for x in labels if x]) or "无解禁数据"
    result = {
        "status": status,
        "label": label,
        "placement": placement[:5],
        "transfer": transfer[:5],
        "other": other[:5],
    }
    if summaries is not None:
        summaries[summary_key] = result
    return result


def _attach_unlock_info(rows):
    if not isinstance(rows, list):
        return rows
    cache = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = row.get("code") or row.get("ts_code") or row.get("symbol")
        if not code:
            continue
        industry = row.get("industry") or ""
        key = _code6(code) or str(code).strip().upper()
        if key not in cache:
            cache[key] = _unlock_summary_for_code(code, industry=industry)
        row["unlock_info"] = cache[key]
        row["money_outflow"] = _money_outflow_for_code(code)
    return rows


def _attach_unlock_info_to_payload(payload, keys=("hits", "buy", "watch", "holdings", "items")):
    if not isinstance(payload, dict):
        return payload
    for key in keys:
        if isinstance(payload.get(key), list):
            _attach_unlock_info(payload[key])
    return payload


def _attach_unlock_info_to_event_groups(payload, group_key="events"):
    if not isinstance(payload, dict):
        return payload
    groups = payload.get(group_key)
    if not isinstance(groups, dict):
        return payload
    for rows in groups.values():
        if isinstance(rows, list):
            _attach_unlock_info(rows)
    return payload


@app.route("/api/event_risk")
def api_event_risk():
    code = request.args.get("code", "").strip()
    industry = request.args.get("industry", "").strip()
    if not code:
        return jsonify({"ok": False, "message": "missing code"}), 400
    return jsonify(_build_event_risk_report(code, industry))


@app.route("/event-legs")
def event_legs_page():
    """新事件腿汇总: 承诺不减持(60日+3.25%) + 商誉洗大澡(120日+6.15%先抑后扬)。"""
    return render_template("event_legs.html")


@app.route("/api/commit_nosell")
def api_commit_nosell():
    data_path = PREDICT_JSON.parent / "commit_nosell.json"
    if data_path.exists():
        return jsonify(json.loads(data_path.read_text(encoding="utf-8")))
    return jsonify({"items": [], "message": "尚无数据; PC运行 export_commit_nosell.py"})


@app.route("/api/bigbath")
def api_bigbath():
    data_path = PREDICT_JSON.parent / "bigbath.json"
    if data_path.exists():
        return jsonify(json.loads(data_path.read_text(encoding="utf-8")))
    return jsonify({"items": [], "message": "尚无数据; PC运行 export_bigbath.py"})


@app.route("/api/index_inclusion/request", methods=["POST"])
def api_index_inclusion_request():
    """网页按钮: 通知PC重算指数纳入(研究+实盘), 跑 export_index_inclusion(_pro).py 并拷回 csv_tmp."""
    try:
        INCLUSION_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        INCLUSION_REQUEST.write_text(json.dumps(
            {"requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False), encoding="utf-8")
        return jsonify({"ok": True, "message": "已通知PC重算指数纳入(拉成分+算收益, 约2-5分钟), 完成后刷新本页"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"请求失败: {e}"})


@app.route("/api/index_inclusion/status")
def api_index_inclusion_status():
    data = _read_json(INCLUSION_STATUS)
    data = dict(data) if isinstance(data, dict) else {"state": "", "msg": ""}
    research_path = PREDICT_JSON.parent / "index_inclusion.json"
    if "研究表失败" in str(data.get("msg") or "") and research_path.exists():
        try:
            status_mtime = INCLUSION_STATUS.stat().st_mtime
            research_mtime = research_path.stat().st_mtime
        except OSError:
            status_mtime = research_mtime = 0
        if research_mtime > status_mtime:
            data.update({
                "state": "done",
                "msg": "纳入数据已更新（研究表已在后续任务中补齐）",
                "research_state": "done",
                "reconciled": True,
                "updated_at": datetime.fromtimestamp(research_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    return jsonify(data)


@app.route("/api/index_inclusion/predict")
def api_index_inclusion_predict():
    """预计下一期调入名单 (准成分: 市值排名预测, 复现度/置信, 生效后命中率)."""
    data = _read_json(PREDICT_JSON.parent / "inclusion_predict.json")
    return jsonify(data or {"indices": [], "message": "尚无预测; PC 端运行 predict_inclusion.py"})


@app.route("/api/index_inclusion/calendar")
def api_index_inclusion_calendar():
    """纳入评审日历 + 三段提醒(MSCI/富时/中证 各评审的公告日/生效日/当前提醒)."""
    data = _read_json(PREDICT_JSON.parent / "inclusion_calendar.json")
    return jsonify(data or {"reviews": [], "alerts": []})


@app.route("/industry")
def industry_page():
    """行业基本面分析: 按行业 扣非净利增速/营收增速/利润弹性 排序, 选股参考."""
    return render_template("industry.html" if _has_internal_access() else "member_industry.html")


@app.route("/api/industry")
def api_industry():
    data = _read_json(PREDICT_JSON.parent / "industry.json")
    if data is None:
        return jsonify({"rows": [], "message": "尚无行业数据"})
    if not _has_internal_access():
        allowed = {"industry", "n", "dt_yoy", "or_yoy", "spread", "pct_dt_gt_or", "pct_dt_pos"}
        data = {
            "period": data.get("period", ""),
            "updated": data.get("updated", ""),
            "n_industries": data.get("n_industries", 0),
            "min_members": data.get("min_members", 0),
            "rows": [
                {key: value for key, value in row.items() if key in allowed}
                for row in (data.get("rows") or [])
            ],
        }
    return jsonify(data)


@app.route("/quality")
def quality_page():
    """质量成长选股: 扣非增速≥20%&营收≥0&扣非≥营收 (第6腿实盘池, 对冲夏~1.5)."""
    return render_template("quality.html")


@app.route("/api/quality")
def api_quality():
    data = _read_json(PREDICT_JSON.parent / "quality.json")
    return jsonify(data or {"rows": [], "message": "尚无质量股清单; PC 端运行 export_quality.py"})


@app.route("/sell")
def sell_page():
    """卖出提醒: 自选股(+可选成本)按经典卖出规则(趋势破坏/移动止损/硬止损)给信号, 治处置效应."""
    return render_template("sell.html")


@app.route("/intraday_t")
def intraday_t_page():
    """超短线/做T 盘中实时: 持仓∪自选∪主腿 当日位置/VWAP偏离, 高抛低吸提示."""
    return render_template("intraday_t.html")


@app.route("/api/intraday_t")
def api_intraday_t():
    data = _read_json(PREDICT_JSON.parent / "intraday_t.json")
    return jsonify(data or {"rows": [], "message": "尚无盘中数据; 盘中由 intraday_t_loop.py 实时刷新"})


@app.route("/api/sell")
def api_sell():
    data = _read_json(PREDICT_JSON.parent / "sell_alerts.json")
    return jsonify(data or {"alerts": [], "message": "尚无卖出信号; 在自选股加入持仓后 PC 端运行 export_sell_signals.py"})


@app.route("/api/refresh/<kind>", methods=["POST"])
def api_refresh(kind):
    """通用页面刷新: 网页按钮通知PC重跑对应导出脚本(rsrs/ipo/repo/runup)并拷回csv_tmp."""
    if kind not in REFRESH_KINDS:
        return jsonify({"ok": False, "message": f"不支持的刷新类型: {kind}"})
    try:
        REFRESH_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"kind": kind, "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False,
        )
        tmp = REFRESH_REQUEST.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, REFRESH_REQUEST)
        return jsonify({"ok": True, "message": f"已通知PC刷新 {kind}(约1-2分钟), 完成后自动刷新本页"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"请求失败: {e}"})


@app.route("/api/refresh/status")
def api_refresh_status():
    data = _read_json(REFRESH_STATUS)
    return jsonify(data or {"state": "", "msg": "", "kind": ""})


def _thesis_slug(s):
    import re as _re
    return _re.sub(r'[^\w]+', '_', (s or "").strip())[:40] or "theme"


@app.route("/thesis")
def thesis_page():
    """瓶颈链/卖铲子: 大技术建设→架构变化→拆BOM→找窄上游(稀缺)→战略控制力vs估值→等验证信号. LLM走框架+tushare落地."""
    return render_template("thesis.html")


@app.route("/api/thesis")
def api_thesis():
    """返回主题索引 + (可选)某主题的最新分析结果 + 历史记录表."""
    idx = _read_json(PREDICT_JSON.parent / "thesis_index.json") or []
    log = _read_json(PREDICT_JSON.parent / "thesis_log.json") or []
    theme = request.args.get("theme", "")
    result = None
    if theme:
        result = _read_json(PREDICT_JSON.parent / f"thesis_{_thesis_slug(theme)}.json")
    elif idx:
        result = _read_json(PREDICT_JSON.parent / f"thesis_{idx[0].get('slug','')}.json")
    return jsonify({"preset": THESIS_PRESET, "index": idx, "log": log[:200], "result": result})


@app.route("/api/thesis/request", methods=["POST"])
def api_thesis_request():
    """网页按钮: 通知PC对某大技术主题做瓶颈链分析(LLM+tushare落地, 约1-3分钟)."""
    body = request.get_json(silent=True) or {}
    theme = (body.get("theme") or "").strip()
    if not theme or len(theme) > 40:
        return jsonify({"ok": False, "message": "请输入有效主题(≤40字)"})
    try:
        THESIS_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        THESIS_REQUEST.write_text(json.dumps(
            {"theme": theme, "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False), encoding="utf-8")
        return jsonify({"ok": True, "message": f"已通知PC分析「{theme}」(LLM走框架+数据落地, 约1-3分钟), 完成后自动刷新"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"请求失败: {e}"})


@app.route("/api/thesis/status")
def api_thesis_status():
    data = _read_json(THESIS_STATUS)
    return jsonify(data or {"state": "", "msg": "", "theme": ""})


def _advisor_pro_basket_codes():
    """策略顾问Pro篮子 = 买入 + 持有 的股票代码 (去重保序). 卖出腿不含."""
    adv = _read_json(PREDICT_JSON.parent / "regime_advisor_pro.json") or {}
    items = (adv.get("trade") or {}).get("items") or []
    codes = []
    for it in items:
        if it.get("action") in ("买入", "持有") and it.get("code"):
            codes.append(it["code"])
    return list(dict.fromkeys(codes))


@app.route("/api/batch_gen", methods=["POST"])
def api_batch_gen():
    """一键批量生成: 按策略顾问Pro篮子(买入+持有)排队生成研报和/或财务预测."""
    body = request.get_json(silent=True) or {}
    want_report = bool(body.get("report"))
    want_forecast = bool(body.get("forecast"))
    if not (want_report or want_forecast):
        return jsonify({"ok": False, "message": "未指定生成类型(report/forecast)"})
    # 显式 codes(如纳入预测候选) 优先; 否则用策略顾问Pro篮子
    body_codes = body.get("codes")
    if isinstance(body_codes, list) and body_codes:
        codes = list(dict.fromkeys(str(c) for c in body_codes if c))[:60]
    else:
        codes = _advisor_pro_basket_codes()
    if not codes:
        return jsonify({"ok": False, "message": "无股票; 先生成篮子/候选清单"})
    try:
        BATCH_GEN_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        BATCH_GEN_REQUEST.write_text(json.dumps(
            {"codes": codes, "report": want_report, "forecast": want_forecast,
             "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False), encoding="utf-8")
        kinds = "+".join([k for k, v in [("研报", want_report), ("财务预测", want_forecast)] if v])
        return jsonify({"ok": True, "n": len(codes),
                        "message": f"已排队 {len(codes)} 只批量生成{kinds}(串行, 每只约1-2分钟, 总计约{len(codes)}-{2*len(codes)}分钟)"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"请求失败: {e}"})


@app.route("/api/batch_gen/status")
def api_batch_gen_status():
    return jsonify(_read_json(BATCH_GEN_STATUS) or {"state": "", "msg": "", "i": 0, "n": 0})


@app.route("/api/pipeline_status")
def api_pipeline_status():
    """三合一全流程实时进度: ①模型全跑(rdagent_status) ②研报(batch_gen_status) ③行业瓶颈链(thesis_status)。
    给每日操作台轮询显示, 时间长能看到当前在哪一步。"""
    rd = _read_json(RDAGENT_STATUS) or {}
    bg = _read_json(BATCH_GEN_STATUS) or {}
    th = _read_json(THESIS_STATUS) or {}
    rd_run = (rd.get("state") == "running")
    bg_run = (bg.get("state") == "running")
    th_run = (th.get("state") == "running")
    if rd_run:
        stage, label = "model", "① 全跑模型"
    elif bg_run:
        stage, label = "report", "② 个股研报"
    elif th_run:
        stage, label = "thesis", "③ 行业瓶颈链"
    elif any(item.get("state") == "error" for item in (rd, bg, th)):
        stage, label = "error", "空闲（上次任务有失败）"
    else:
        stage, label = "idle", "空闲/已完成"
    return jsonify({
        "stage": stage, "label": label, "active": (rd_run or bg_run or th_run),
        "model": {"state": rd.get("state", ""), "msg": rd.get("msg", "")},
        "report": {"state": bg.get("state", ""), "msg": bg.get("msg", ""), "i": bg.get("i", 0), "n": bg.get("n", 0)},
        "thesis": {"state": th.get("state", ""), "msg": th.get("msg", ""), "theme": th.get("theme", "")},
        "ts": datetime.now().strftime("%H:%M:%S"),
    })


@app.route("/trade")
def trade_page():
    """下单执行页: 从策略顾问Pro的篮子派生 买入/卖出/持有 清单 (复用 regime_advisor_pro.json 的 trade 字段)."""
    return render_template("trade.html")


def _code_to_ts(code: str) -> str | None:
    """qlib(sh600519) / ts_code(600519.SH) / 纯6位 → 600519.SH; 不可识别返回 None."""
    code = (code or "").strip()
    if not code:
        return None
    if "." in code:
        return code.upper()
    if code[:2].lower() in ("sh", "sz", "bj"):
        return _qlib_to_ts_code(code.lower())
    c6 = _c6(code)
    if not c6:
        return None
    suf = "BJ" if c6[0] in ("4", "8") else ("SH" if c6[0] in ("5", "6", "9") else "SZ")
    return f"{c6}.{suf}"


# 进程内缓存「三年一期」基本面, 避免下单页每30秒刷新都重拉 tushare. ts_code -> result
_trade_fund_cache: dict[str, dict] = {}


def _trade_fundamentals(ts_code: str, pro=None) -> dict:
    """拉某只股票「三年一期」基本面: 扣非净利润同比增速 / 销售毛利率 / ROE.
    带进程内缓存; 调批量时传入共享的 pro 句柄避免反复建连."""
    if ts_code in _trade_fund_cache:
        return _trade_fund_cache[ts_code]
    try:
        pro = pro or _tushare_api()
        start = f"{datetime.now().year - 5}0101"
        df = pro.fina_indicator(
            ts_code=ts_code, start_date=start,
            end_date=datetime.now().strftime("%Y%m%d"),
            fields="end_date,ann_date,profit_dedt,grossprofit_margin,roe",
        )
    except Exception as e:
        return {"found": False, "ts_code": ts_code, "message": f"拉取失败: {e}"}  # 不缓存失败, 下次重试
    if df is None or df.empty:
        out = {"found": False, "ts_code": ts_code, "message": "无财务数据(新股/北交所/缺财报)"}
        _trade_fund_cache[ts_code] = out
        return out
    df = df.dropna(subset=["end_date"]).drop_duplicates(subset=["end_date"])
    by_end = {str(r.end_date): r for r in df.itertuples(index=False)}

    def _f(r, attr):
        v = getattr(r, attr, None)
        return None if (v is None or pd.isna(v)) else float(v)

    all_ends = sorted(by_end.keys(), reverse=True)
    if not all_ends:
        return {"found": False, "ts_code": ts_code, "message": "无可用报告期"}
    latest = all_ends[0]
    annuals = [e for e in all_ends if e.endswith("1231")][:3]
    # 三年一期 = 最近3个年报 + 最新一期(若最新本身是年报则自然合并)
    want = sorted(set(annuals + [latest]), reverse=True)
    periods = []
    for e in want:
        r = by_end[e]
        base_end = f"{int(e[:4]) - 1}{e[4:]}"  # 去年同口径报告期
        base = by_end.get(base_end)
        yoy = _yoy_growth(_f(r, "profit_dedt"),
                          _f(base, "profit_dedt") if base is not None else None)
        mm = e[4:]
        plabel = (e[:4] + ("年报" if mm == "1231" else
                           ("一季报" if mm == "0331" else
                            ("中报" if mm == "0630" else
                             ("三季报" if mm == "0930" else "报告")))))
        periods.append({
            "end": e, "label": plabel,
            "dt_profit_yoy": None if yoy is None else round(yoy, 1),
            "gross_margin": (lambda v: None if v is None else round(v, 2))(_f(r, "grossprofit_margin")),
            "roe": (lambda v: None if v is None else round(v, 2))(_f(r, "roe")),
        })
    out = {"found": True, "ts_code": ts_code, "periods": periods}
    _trade_fund_cache[ts_code] = out
    return out


@app.route("/api/trade/fundamentals")
def api_trade_fundamentals():
    """下单清单单只股票「三年一期」基本面: 扣非净利同比 / 毛利率 / ROE.
    入参 code 接受 qlib(sh600519) / ts_code(600519.SH) / 纯6位."""
    ts_code = _code_to_ts(request.args.get("code") or "")
    if not ts_code:
        return jsonify({"found": False, "message": "代码无法识别"})
    return jsonify(_trade_fundamentals(ts_code))


@app.route("/api/trade/fundamentals/batch", methods=["POST"])
def api_trade_fundamentals_batch():
    """批量拉下单清单所有股票的「三年一期」基本面, 一次请求返回 {原始code: 结果}.
    服务端顺序拉取(带缓存+限速), 避免前端几十个并发把 tushare 打到限额."""
    codes = (request.get_json(silent=True) or {}).get("codes", [])
    if not isinstance(codes, list) or not codes:
        return jsonify({"results": {}})
    try:
        pro = _tushare_api()
    except Exception as e:
        return jsonify({"results": {}, "error": str(e)})
    results = {}
    for raw in codes[:200]:
        ts_code = _code_to_ts(raw)
        if not ts_code:
            results[raw] = {"found": False, "message": "代码无法识别"}
            continue
        cached = ts_code in _trade_fund_cache
        results[raw] = _trade_fundamentals(ts_code, pro=pro)
        if not cached:
            time.sleep(0.12)  # 仅未命中缓存时限速
    return jsonify({"results": results})


@app.route("/track")
def track_page():
    """实盘跟踪/OOS验证页: 逐季已实现净超额台账 (复用 regime_advisor_pro.json 的 track.ledger)."""
    return render_template("track.html")


@app.route("/api/health")
def health():
    calendar = _read_calendar()
    calendar_days = len(calendar)
    qlib = _qlib_feature_readiness(calendar)
    stock_db_ready = Path(STOCK_META_DB).exists() and Path(STOCK_META_DB).stat().st_size > 0
    auth_ready = not _auth_enabled() or _secret_is_strong
    ready = (
        calendar_days > 0
        and qlib["features"]
        and qlib["benchmark_close"]
        and stock_db_ready
        and auth_ready
    )
    return jsonify({
        "ok": ready,
        "calendar_days": calendar_days,
        "qlib_features": qlib["features"],
        "benchmark_close": qlib["benchmark_close"],
        "benchmark_code": qlib["benchmark_code"],
        "qlib": qlib,
        "stock_metadata": stock_db_ready,
        "auth_ready": auth_ready,
        # Data-update failure/staleness is diagnostic. It must not make the web
        # or scheduler container look dead and trigger a restart loop.
        "daily_update": _daily_update_health(),
        "weekly_financials": _weekly_financials_health(),
        "time": datetime.now().isoformat(timespec="seconds"),
    }), (200 if ready else 503)


@app.route("/api/search")
def search():
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"hits": []})
    if len(q) > 80 or any(ord(char) < 32 for char in q):
        return jsonify({"error": "invalid search query", "hits": []}), 400
    # 上市首日行情简称带 N，但基础资料和 IPO 文件通常保存正式简称。
    query = q[1:] if q.startswith("n") and len(q) > 1 else q

    # priority: exact code prefix > exact pinyin prefix > name substring
    escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like_q = f"{escaped_query}%"
    name_like = f"%{escaped_query}%"
    rows = []
    try:
        with closing(_open_sqlite_readonly(STOCK_META_DB)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT code, ts_code, name, industry, list_status
                FROM stock_meta
                WHERE list_status = 'L'
                  AND (LOWER(code) LIKE ? ESCAPE '\\'
                       OR LOWER(ts_code) LIKE ? ESCAPE '\\'
                       OR LOWER(pinyin_initials) LIKE ? ESCAPE '\\'
                       OR name LIKE ? ESCAPE '\\')
                ORDER BY
                  CASE WHEN LOWER(code) LIKE ? ESCAPE '\\' OR LOWER(ts_code) LIKE ? ESCAPE '\\' THEN 0
                       WHEN LOWER(pinyin_initials) LIKE ? ESCAPE '\\' THEN 1
                       ELSE 2 END,
                  code
                LIMIT 20
            """, (like_q, like_q, like_q, name_like, like_q, like_q, like_q))
            rows = [dict(row) for row in cur.fetchall()]
    except (OSError, sqlite3.Error) as e:
        log.warning(f"stock_meta search unavailable, using IPO fallback: {e}")

    # stock_meta 默认按周期刷新；用 ipo.json 兜底当天刚上市、尚未入库的股票。
    seen = {str(r.get("ts_code") or "").upper() for r in rows}
    ipo = _ipo_data()
    if not isinstance(ipo, dict):
        ipo = {}
    ipo_rows = []
    for key in ("just_listed", "today_buy", "soon_buy"):
        values = ipo.get(key)
        if isinstance(values, list):
            ipo_rows.extend(item for item in values if isinstance(item, dict))
    for item in ipo_rows:
        ts_code = _code_to_ts(item.get("ts_code") or item.get("code") or "")
        if not ts_code or ts_code in seen:
            continue
        c6 = _c6(ts_code)
        name = str(item.get("name") or "")
        qlib_code = _ts_to_qlib_code(ts_code)
        searchable = (name.lower(), c6.lower(), ts_code.lower(),
                      qlib_code.lower(), str(item.get("sub_code") or "").lower())
        if not any(query in value for value in searchable if value):
            continue
        rows.append({
            "code": qlib_code, "ts_code": ts_code, "name": name,
            "industry": item.get("industry") or "",
            "list_status": "L",
        })
        seen.add(ts_code)
        if len(rows) >= 20:
            break
    if os.environ.get("QI_DEMO_DATA", "0") == "1" and len(rows) < 20:
        for item in _demo_search_hits(query):
            if item["ts_code"] in seen:
                continue
            rows.append(item)
            seen.add(item["ts_code"])
            if len(rows) >= 20:
                break
    return jsonify({"hits": rows})


@app.route("/api/kline")
def kline():
    code = (request.args.get("code") or "").strip()
    # 页面之间常传 Tushare 格式(001234.SZ)，行情文件使用 Qlib 格式(sz001234)。
    if "." in code:
        try:
            code = _ts_to_qlib_code(code.upper())
        except ValueError:
            code = code.lower()
    else:
        code = code.lower()
        if len(code) == 6 and code.isdigit():
            if code.startswith("6"):
                code = "sh" + code
            elif code.startswith(("4", "8")):
                code = "bj" + code
            else:
                code = "sz" + code
    code = _normalize_market_code(code)
    if not code:
        return jsonify({"error": "valid stock code required"}), 400
    days_str = request.args.get("days", "0")  # 0 = 全部历史
    adjust = (request.args.get("adjust") or "qfq").lower()
    refresh_text = (request.args.get("refresh") or "0").strip()
    if refresh_text not in {"0", "1"}:
        return jsonify({"error": "refresh must be 0 or 1"}), 400
    refresh = refresh_text == "1"
    if refresh and not _membership_store().has_feature(
        getattr(g, "current_member", None), "internal_operations"
    ):
        return jsonify({"error": "manual refresh requires internal permission"}), 403
    if adjust not in ("qfq", "hfq", "none", "raw"):
        return jsonify({"error": "adjust must be qfq, hfq, none, or raw"}), 400
    try:
        days = int(days_str)
    except (TypeError, ValueError):
        return jsonify({"error": "days must be an integer"}), 400
    if days < 0 or days > 5000:
        return jsonify({"error": "days must be between 0 and 5000"}), 400

    # 先尝试补到最新数据 (按时段判断是否拉今日 parquet)
    freshness = None
    if refresh:
        try:
            freshness = ensure_freshness_for_stock(code)
        except Exception as e:
            log.exception(f"ensure_freshness failed: {e}")
            freshness = {"status": "error", "message": str(e)}

    try:
        data = load_ohlcv(code, last_n_days=days if days > 0 else None, adjust=adjust)
    except Exception:
        log.exception("local market data failed for %s", code)
        return jsonify({"error": "market data unavailable"}), 503
    if not isinstance(data, dict):
        log.error("local market data returned a non-object payload for %s", code)
        return jsonify({"error": "invalid market data payload"}), 503
    if not data.get("dates"):
        data = _eastmoney_daily_ohlcv(
            code,
            last_n_days=days if days > 0 else None,
            adjust=adjust,
        )
    if not isinstance(data, dict) or not data.get("dates"):
        return jsonify({"error": f"no data for {code}"}), 404
    data = _normalize_ohlcv_payload(data)
    if data is None:
        log.error("market data arrays violate the response contract for %s", code)
        return jsonify({"error": "invalid market data payload"}), 503
    data.setdefault("source", "qlib")
    data.setdefault("adjust", adjust)
    data.setdefault("adjust_requested", adjust)

    # stock display name from meta
    name = ""
    try:
        with closing(_open_sqlite_readonly(STOCK_META_DB)) as conn:
            row = conn.execute("SELECT name FROM stock_meta WHERE code = ?", (code,)).fetchone()
            name = row[0] if row else ""
    except (OSError, sqlite3.Error) as exc:
        log.debug("stock display name unavailable for %s: %s", code, exc)

    data["name"] = str(data.get("name") or name or "")
    return jsonify({"code": code, "freshness": freshness, **data})


# ---------- watchlist / 看板 ----------

def _load_watchlist() -> list[str]:
    data = _member_document("watchlist", [], legacy_path=WATCHLIST_JSON)
    if not isinstance(data, list):
        return []
    codes = [str(c).strip().lower() for c in data if str(c).strip()]
    return [code for code in codes if _valid_watch_code(code)]


def _valid_watch_code(code: str) -> bool:
    return bool(re.fullmatch(r"(?:sh|sz|bj)\d{6}", str(code or "").strip().lower()))


def _save_watchlist(codes: list[str]):
    seen, out = set(), []
    for c in codes:
        c = str(c).strip().lower()
        if c and c not in seen:
            seen.add(c); out.append(c)
    _member_data_store().put(_member_scope_id(), "watchlist", out)
    return out


def _meta_for_codes(codes: list[str]) -> dict[str, dict]:
    """code -> {name, industry}. 同时按 code(qlib格式 sh600183) 和 ts_code(600183.SH) 匹配,
    结果多种 key 都填, 调用方拿 qlib/ts/6位代码都能查到。"""
    if not codes:
        return {}
    def aliases(value):
        raw = str(value or "").strip()
        digits = "".join(ch for ch in raw if ch.isdigit())[:6]
        res = {raw}
        if digits:
            res.add(digits)
            if digits.startswith(("6", "9")):
                res.update({f"sh{digits}", f"{digits}.SH"})
            elif digits.startswith(("4", "8")):
                res.update({f"bj{digits}", f"{digits}.BJ"})
            else:
                res.update({f"sz{digits}", f"{digits}.SZ"})
        return {x for x in res if x}

    query_codes = sorted({a for c in codes for a in aliases(c)})
    out = {}
    try:
        conn = sqlite3.connect(STOCK_META_DB)
        conn.row_factory = sqlite3.Row
        rows = []
        for i in range(0, len(query_codes), 400):
            chunk = query_codes[i:i + 400]
            qs = ",".join("?" * len(chunk))
            cur = conn.execute(
                f"SELECT code, ts_code, name, industry FROM stock_meta WHERE code IN ({qs}) OR ts_code IN ({qs})",
                chunk + chunk)
            rows.extend(cur.fetchall())
        for r in rows:
            info = {"name": r["name"], "industry": r["industry"]}
            keys = set()
            keys.update(aliases(r["code"]))
            if r["ts_code"]:
                keys.update(aliases(r["ts_code"]))
            for key in keys:
                out[key] = info
        for c in codes:
            for key in aliases(c):
                if key in out:
                    out[str(c)] = out[key]
                    break
        conn.close()
    except Exception as e:
        log.warning(f"meta_for_codes failed: {e}")
    missing = [c for c in codes if str(c) not in out]
    if missing:
        try:
            cache = getattr(_meta_for_codes, "_stock_basic_cache", None)
            if cache is None:
                cache = {}
                candidates = [
                    Path(STOCK_META_DB).parent / "event_resonance_cache" / "stock_basic.csv.gz",
                    Path(__file__).resolve().parent / "data" / "event_resonance_cache" / "stock_basic.csv.gz",
                ]
                for p in candidates:
                    if not p.exists():
                        continue
                    df = pd.read_csv(p, dtype=str)
                    for _, r in df.iterrows():
                        ts_code = str(r.get("ts_code") or "").strip()
                        if not ts_code:
                            continue
                        info = {
                            "name": str(r.get("name") or "").strip(),
                            "industry": str(r.get("industry") or "").strip(),
                        }
                        for key in aliases(ts_code):
                            cache[key] = info
                    break
                setattr(_meta_for_codes, "_stock_basic_cache", cache)
            for c in missing:
                for key in aliases(c):
                    if key in cache:
                        out[str(c)] = cache[key]
                        for alias in aliases(c):
                            out.setdefault(alias, cache[key])
                        break
        except Exception as e:
            log.warning(f"meta_for_codes fallback stock_basic failed: {e}")
    return out


def _realtime_quotes(qlib_codes):
    """腾讯行情(qt.gtimg.cn)取实时价(盘中分时, 免费无需referer)。
    返回 {qlib_code: {price, prev, pct}}。失败/停牌返回空或缺项。qlib_code如 sh600183/sz000001。"""
    import requests as _rq
    out = {}
    codes = [c for c in (qlib_codes or []) if c]
    if not codes:
        return out
    try:
        r = _rq.get("https://qt.gtimg.cn/q=" + ",".join(codes),
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        r.encoding = "gbk"
        for line in r.text.split(";"):
            if "v_" not in line or '"' not in line:
                continue
            key = line.split("=")[0].strip().replace("v_", "")
            parts = line.split('"')
            if len(parts) < 2:
                continue
            f = parts[1].split("~")
            if len(f) < 5:
                continue
            try:
                price, prev = float(f[3]), float(f[4])
                if price <= 0:   # 停牌/未开盘=0, 用昨收
                    price = prev
                pct = round((price / prev - 1) * 100, 2) if prev else None
                out[key] = {"price": round(price, 3), "prev": round(prev, 3), "pct": pct}
            except (ValueError, IndexError):
                pass
    except Exception as e:
        log.warning(f"realtime_quotes failed: {e}")
    return out


@app.route("/api/holdings_realtime")
def api_holdings_realtime():
    """持仓 + 基准的实时价(腾讯), 供持仓页盘中更新 现价/收益/市值。返回 {ts_code: {price,pct}} + bench。"""
    pos = [p for p in _load_positions() if p.get("cost")]
    qmap = {}   # qlib_code -> ts_code
    for p in pos:
        ts = str(p.get("code", ""))
        if not ts:
            continue
        q = _ts_to_qlib_code(ts) if "." in ts else ts.lower()
        qmap[q] = ts
    bench = (request.args.get("bench") or "sh000300").strip()
    quotes = _realtime_quotes(list(qmap.keys()) + [bench])
    res = {qmap[q]: info for q, info in quotes.items() if q in qmap}
    return jsonify({"quotes": res, "bench": quotes.get(bench, {}),
                    "ts": datetime.now().strftime("%H:%M:%S")})


def _latest_quote(code: str) -> dict:
    """最新收盘 + 涨跌幅 (前复权)。读最后两根 K 线。"""
    try:
        d = load_ohlcv(code, last_n_days=2, adjust="qfq")
        dates = d.get("dates") or []
        if not dates:
            return {}
        i = len(dates) - 1
        last = d["close"][i]
        prev = d["close"][i - 1] if i > 0 else last
        chg = ((last - prev) / prev * 100) if prev else 0.0
        return {"last": round(last, 2), "chg": round(chg, 2),
                "vol": d["volume"][i], "date": dates[i]}
    except Exception as e:
        log.warning(f"latest_quote {code} failed: {e}")
        return {}


@app.route("/watchlist")
def watchlist_page():
    template = "watchlist.html" if _has_internal_access() else "member_watchlist.html"
    return render_template(template)


@app.route("/api/watchlist")
def api_watchlist():
    codes = _load_watchlist()
    meta = _meta_for_codes(codes)
    items = []
    for c in codes:
        m = meta.get(c, {})
        q = _latest_quote(c)
        items.append({"code": c, "ts_code": _qlib_code_to_ts(c),
                      "name": m.get("name", ""), "industry": m.get("industry", ""), **q})
    return jsonify({"items": items, "count": len(items)})


def _fundamentals_for_codes(codes):
    """给一批code(qlib或ts格式) → 每只 近3年报+最新一期 营收增速/扣非增速/毛利率/ROE。供自选股/组合清单等复用。"""
    fund = _read_json(FUNDAMENTALS_JSON) or {}
    stocks = fund.get("stocks", {}) if isinstance(fund, dict) else {}
    ts_codes = [(_qlib_code_to_ts(c) if not (isinstance(c, str) and "." in c) else c.upper()) for c in codes]
    ends = set()
    for tc in ts_codes:
        st = stocks.get(tc)
        for row in (st.get("rows", []) if isinstance(st, dict) else []):
            if row.get("end"):
                ends.add(row["end"])
    roe_map = {}
    if ends:
        try:
            conn = sqlite3.connect(FINANCIALS_DB)
            qs = ",".join("?" * len(ends))
            df = pd.read_sql(f"SELECT ts_code, end_date, roe FROM fina_indicators WHERE end_date IN ({qs})",
                             conn, params=list(ends))
            conn.close()
            for r in df.itertuples(index=False):
                v = r.roe
                roe_map.setdefault(r.ts_code, {})[str(r.end_date)] = (None if pd.isna(v) else round(float(v), 1))
        except Exception as e:
            log.warning(f"fundamentals ROE query failed: {e}")
    out = {}
    for c, tc in zip(codes, ts_codes):
        st = stocks.get(tc) if isinstance(stocks.get(tc), dict) else None
        rows = []
        for row in (st.get("rows", []) if st else []):
            rows.append({"period": row.get("period", ""), "end": row.get("end", ""),
                         "rev_yoy": row.get("rev_yoy"), "dedt_yoy": row.get("dedt_yoy"),
                         "gm": row.get("gm"), "roe": roe_map.get(tc, {}).get(str(row.get("end", "")), None)})
        out[c] = {"code": c, "ts_code": tc, "name": (st.get("name") if st else "") or "",
                  "ind": (st.get("ind") if st else "") or "", "rows": rows}
    return out, fund.get("as_of", "")


@app.route("/api/fundamentals_for")
def api_fundamentals_for():
    """给 codes(逗号分隔, qlib或ts格式) → 各自财务。组合清单/任意页复用。"""
    raw = (request.args.get("codes", "") or "").strip()
    codes = [c.strip() for c in raw.split(",") if c.strip()][:120]
    if not codes:
        return jsonify({"items": {}, "as_of": ""})
    out, asof = _fundamentals_for_codes(codes)
    return jsonify({"items": out, "as_of": asof, "count": len(out)})


@app.route("/api/watchlist/fundamentals")
def api_watchlist_fundamentals():
    """自选股财务: 每只 近3年报+最新一期 的 营收增速/扣非净利增速/毛利率/ROE。
    营收/扣非/毛利来自 fundamentals.json(PC export_fundamentals写); ROE来自 fina_indicators 表(按end_date join)。"""
    fund = _read_json(FUNDAMENTALS_JSON) or {}
    stocks = fund.get("stocks", {}) if isinstance(fund, dict) else {}
    codes = _load_watchlist()
    ts_codes = [_qlib_code_to_ts(c) for c in codes]
    # 收集所有出现的 end_date, 一次性查 ROE
    ends = set()
    for tc in ts_codes:
        for row in (stocks.get(tc, {}).get("rows", []) if isinstance(stocks.get(tc), dict) else []):
            if row.get("end"):
                ends.add(row["end"])
    roe_map = {}   # ts_code -> end -> roe
    if ends:
        try:
            conn = sqlite3.connect(FINANCIALS_DB)
            qs = ",".join("?" * len(ends))
            df = pd.read_sql(f"SELECT ts_code, end_date, roe FROM fina_indicators WHERE end_date IN ({qs})",
                             conn, params=list(ends))
            conn.close()
            for r in df.itertuples(index=False):
                v = r.roe
                roe_map.setdefault(r.ts_code, {})[str(r.end_date)] = (None if pd.isna(v) else round(float(v), 1))
        except Exception as e:
            log.warning(f"watchlist fundamentals ROE query failed: {e}")
    out = []
    for c, tc in zip(codes, ts_codes):
        st = stocks.get(tc) if isinstance(stocks.get(tc), dict) else None
        rows = []
        for row in (st.get("rows", []) if st else []):
            rows.append({
                "period": row.get("period", ""), "end": row.get("end", ""),
                "rev_yoy": row.get("rev_yoy"), "dedt_yoy": row.get("dedt_yoy"),
                "gm": row.get("gm"), "roe": roe_map.get(tc, {}).get(str(row.get("end", "")), None),
            })
        out.append({"code": c, "ts_code": tc, "name": (st.get("name") if st else "") or "",
                    "ind": (st.get("ind") if st else "") or "", "rows": rows})
    return jsonify({"items": out, "as_of": fund.get("as_of", ""), "count": len(out)})


@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    if request.is_json:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "JSON object required"}), 400
        code = body.get("code", "")
    else:
        code = request.form.get("code", "")
    code = str(code).strip().lower()
    if not _valid_watch_code(code):
        return jsonify({"error": "valid stock code required"}), 400
    def add_code(current):
        codes = [str(c).strip().lower() for c in (current or []) if str(c).strip()]
        if code not in codes and len(codes) < 200:
            codes.append(code)
        return codes

    codes = _member_data_store().update(
        _member_scope_id(), "watchlist", add_code, default=[]
    )
    if code not in codes:
        return jsonify({"ok": False, "error": "watchlist limit reached", "count": len(codes)}), 409
    return jsonify({"ok": True, "count": len(codes), "watching": True})


@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    if request.is_json:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "JSON object required"}), 400
        code = body.get("code", "")
    else:
        code = request.form.get("code", "")
    code = str(code).strip().lower()
    if not _valid_watch_code(code):
        return jsonify({"error": "valid stock code required"}), 400
    codes = _member_data_store().update(
        _member_scope_id(),
        "watchlist",
        lambda current: [str(c).strip().lower() for c in (current or []) if str(c).strip().lower() != code],
        default=[],
    )
    return jsonify({"ok": True, "count": len(codes)})


def _load_positions():
    d = _member_document("positions", [], legacy_path=POSITIONS_JSON)
    return [_recalc_position(p) for p in d] if isinstance(d, list) else []


def _new_lot(cost, qty, buy_date, sleeve=""):
    return {
        "lot_id": uuid.uuid4().hex[:12],
        "cost": round(float(cost), 4),
        "qty": int(qty),
        "remaining_qty": int(qty),
        "buy_date": str(buy_date or "")[:10],
        "sleeve": str(sleeve or ""),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _recalc_position(p):
    """兼容旧单笔结构，并由未卖完批次重算汇总字段。"""
    p = dict(p or {})
    lots = p.get("lots")
    if not isinstance(lots, list):
        lots = []
        try:
            qty = int(float(p.get("qty") or 0))
            cost = float(p.get("cost") or 0)
        except (TypeError, ValueError):
            qty, cost = 0, 0
        if qty > 0 and cost > 0:
            lot = _new_lot(cost, qty, p.get("date", ""), p.get("sleeve", ""))
            lot["lot_id"] = (f"legacy-{p.get('code', '')}-{p.get('date', '')}-{cost}-{qty}"
                             .replace(".", "_").replace(" ", ""))
            lot["legacy"] = True
            lots.append(lot)
    clean = []
    for raw in lots:
        try:
            qty = int(float(raw.get("qty") or 0))
            rem = int(float(raw.get("remaining_qty", qty) or 0))
            cost = float(raw.get("cost") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or rem <= 0 or cost <= 0:
            continue
        clean.append({
            **raw, "lot_id": str(raw.get("lot_id") or uuid.uuid4().hex[:12]),
            "qty": qty, "remaining_qty": min(rem, qty), "cost": round(cost, 4),
            "buy_date": str(raw.get("buy_date") or raw.get("date") or "")[:10],
            "sleeve": str(raw.get("sleeve") or p.get("sleeve") or ""),
        })
    p["lots"] = clean
    if clean:
        total = sum(x["remaining_qty"] for x in clean)
        invested = sum(x["cost"] * x["remaining_qty"] for x in clean)
        p["qty"] = total
        p["cost"] = round(invested / total, 4)
        p["date"] = min((x["buy_date"] for x in clean if x["buy_date"]), default="")
        sleeves = {x["sleeve"] for x in clean if x["sleeve"]}
        p["sleeve"] = sleeves.pop() if len(sleeves) == 1 else (p.get("sleeve") or "mixed")
    today = datetime.now().strftime("%Y-%m-%d")
    old_qty = sum(x["remaining_qty"] for x in clean
                  if x.get("buy_date") and x["buy_date"] < today)
    usage = p.get("lifo_usage") or {}
    used_today = int(usage.get("qty") or 0) if usage.get("date") == today else 0
    p["available_qty"] = max(0, old_qty - used_today)
    p["locked_qty"] = max(0, int(p.get("qty") or 0) - p["available_qty"])
    return p


def _save_positions(pos):
    normalized = [_recalc_position(p) for p in pos]
    _member_data_store().put(_member_scope_id(), "positions", normalized)


def _load_sells_history():
    history = _member_document("sells_history", [], legacy_path=SELLS_HISTORY_JSON)
    return history if isinstance(history, list) else []


def _save_sells_history(history):
    normalized = history if isinstance(history, list) else []
    _member_data_store().put(_member_scope_id(), "sells_history", normalized)


def _save_trade_state(positions, history):
    normalized_positions = [_recalc_position(p) for p in positions]
    normalized_history = history if isinstance(history, list) else []
    _member_data_store().put_many(
        _member_scope_id(),
        {
            ("positions", "default"): normalized_positions,
            ("sells_history", "default"): normalized_history,
        },
    )


@app.route("/api/positions")
def api_positions():
    pos = _load_positions()
    meta = _meta_for_codes([p.get("code", "") for p in pos])
    ts_of = {p.get("code", ""): _code_to_ts(p.get("code", "")) for p in pos}
    rt = _rt_quotes([c for c in ts_of.values() if c])      # 批量实时价 → 当前市值/盈亏
    for p in pos:
        p["name"] = (meta.get(p.get("code", "")) or {}).get("name", "")
        last = (rt.get(ts_of.get(p.get("code", "")) or "") or {}).get("price")
        cost = p.get("cost"); qty = p.get("qty")
        p["last"] = round(last, 3) if last else None
        p["mktval"] = round(last * qty, 2) if (last and qty) else None        # 当前市值=现价×数量
        p["pnl"] = round((last - cost) * qty, 2) if (last and cost and qty) else None
        p["ret_pct"] = round((last / cost - 1) * 100, 2) if (last and cost) else None
    return jsonify({"positions": pos})


_BENCH = {"sh000300": "沪深300", "sh000905": "中证500", "sh000852": "中证1000"}


def _ffill_on_axis(axis, dates, close):
    """把(dates,close)前向填充对齐到有序 axis 上(停牌日取最近收盘). 两者均升序."""
    res = [None] * len(axis)
    ptr = 0
    last = None
    for i, D in enumerate(axis):
        while ptr < len(dates) and dates[ptr] <= D:
            last = close[ptr]
            ptr += 1
        res[i] = last
    return res


def _perf_metrics(line):
    """从'每日累计收益率%'序列算 QuantStats 式绩效: 总收益/年化/波动/夏普/最大回撤/Calmar/胜率.
    line 起点≈0(相对成本), 用 (1+cumret) 还原净值曲线再算日收益."""
    vals = [v for v in line if v is not None]
    if len(vals) < 3:
        return {}
    eq = [1.0 + v / 100.0 for v in vals]
    eq = [x if x > 1e-6 else 1e-6 for x in eq]
    rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    std = var ** 0.5
    growth = eq[-1] / eq[0]
    ann = (growth ** (252.0 / n) - 1.0) if (n > 0 and growth > 0) else None
    ann_vol = std * (252 ** 0.5)
    sharpe = (mean / std * (252 ** 0.5)) if std > 0 else None
    peak, mdd = eq[0], 0.0
    for x in eq:
        peak = max(peak, x)
        mdd = min(mdd, x / peak - 1.0)
    calmar = (ann / abs(mdd)) if (ann is not None and mdd < 0) else None
    winrate = sum(1 for r in rets if r > 0) / n
    return {"total": round((vals[-1]), 2), "ann": round(ann * 100, 2) if ann is not None else None,
            "vol": round(ann_vol * 100, 2), "sharpe": round(sharpe, 2) if sharpe is not None else None,
            "maxdd": round(mdd * 100, 2), "calmar": round(calmar, 2) if calmar is not None else None,
            "winrate": round(winrate * 100, 1), "days": len(vals)}


@app.route("/holdings")
def holdings_page():
    """持仓收益 vs 基准 每日对比(数据来自卖出提醒页输入的持仓: 代码/成本/买入日)."""
    return render_template("holdings.html")


@app.route("/api/holdings_curve")
def api_holdings_curve():
    """每日"持仓平均收益率"vs"同股同买入日投入基准的收益率", 折线对比 + 逐票明细.
    持仓 = positions.json(卖出提醒页录入: code/cost/date)。口径: 等权; 单票收益=close/cost−1;
    基准腿 = 同一只票按同一买入日投入指数(sh000300等)的收益 → 相同权重&时点, 公平比较"我的选股择时 vs 基准"。
    """
    bench = request.args.get("bench", "sh000300")
    if bench not in _BENCH:
        bench = "sh000300"
    all_pos = _load_positions()
    meta = _meta_for_codes([p.get("code", "") for p in all_pos])
    valid = [p for p in all_pos if p.get("cost") and p.get("date")]
    # 缺成本价/买入日的持仓: 始终单列(即使有其他有效持仓也不静默丢弃) —— 否则用户看不到为啥某只没显示
    excluded = []
    for p in all_pos:
        if not (p.get("cost") and p.get("date")):
            miss = []
            if not p.get("cost"):
                miss.append("成本价")
            if not p.get("date"):
                miss.append("买入日")
            excluded.append({"code": p.get("code", ""),
                             "name": (meta.get(p.get("code", "")) or {}).get("name", ""),
                             "missing": miss})
    if not valid:
        if all_pos:
            msg = (f"你有 {len(all_pos)} 笔持仓, 但都缺『成本价/买入日』, 算不了收益。"
                   "去卖出提醒页给每只补上『成本价 + 买入日』就会显示(只填代码=仅监控, 不进本页)。")
        else:
            msg = "还没有持仓 — 去卖出提醒页录入(代码 + 成本价 + 买入日)即可在这里看收益曲线"
        return jsonify({"bench": bench, "bench_name": _BENCH[bench], "dates": [],
                        "portfolio": [], "benchmark": [], "holdings": [],
                        "excluded": excluded, "message": msg})
    bdata = load_ohlcv(bench, adjust="qfq")
    bdates = bdata.get("dates") or []
    bclose = bdata.get("close") or []
    bench_map = dict(zip(bdates, bclose))

    holds, table, skipped = [], [], []
    bmax = bdates[-1] if bdates else ""
    for p in valid:
        code = p["code"]
        buy = str(p.get("date", "")).strip()
        try:
            cost = float(p["cost"])
        except (TypeError, ValueError):
            skipped.append({"code": code, "buy": buy, "why": "成本价不是数字"}); continue
        if cost <= 0:
            skipped.append({"code": code, "buy": buy, "why": "成本价≤0"}); continue
        qcode = _ts_to_qlib_code(code) if "." in code else code.lower()  # 持仓存ts格式(600183.SH), load_ohlcv要qlib格式(sh600183)
        d = load_ohlcv(qcode, adjust="qfq")
        dates, close = d.get("dates") or [], d.get("close") or []
        if not dates:
            skipped.append({"code": code, "buy": buy, "why": "行情库取不到该股数据(代码或行情未更新)"}); continue
        if dates[-1] < buy:
            # 今日/近期新买(买入日晚于最新收盘行情): 不进历史曲线, 但进逐票表(现价用实时, 今晚收盘后自动并入), 不再静默跳过
            try:
                pqty = int(float(p.get("qty") or 0))
            except (TypeError, ValueError):
                pqty = 0
            table.append({"code": code, "name": (meta.get(code) or {}).get("name", ""),
                          "buy": buy, "cost": round(cost, 3), "last": None,
                          "qty": (pqty if pqty > 0 else None), "mktval": None,
                          "ret": None, "idx_ret": None, "alpha": None,
                          "pending": True,
                          "pending_why": f"今日新买(买入日{buy}>最新行情{dates[-1]}); 现价见实时, 收盘后并入曲线"})
            continue
        ient_dates = [x for x in bdates if x >= buy]
        if not ient_dates:
            skipped.append({"code": code, "buy": buy, "why": f"基准 {bench} 只到 {bmax or '无'}, 覆盖不到买入日 {buy}(基准指数待更新)"}); continue
        ient = bench_map.get(ient_dates[0])
        if not ient:
            skipped.append({"code": code, "buy": buy, "why": "基准买入日价格缺失"}); continue
        latest = float(close[-1])
        ret = latest / cost - 1.0
        idx_ret = (float(bclose[-1]) / ient - 1.0) if bclose else None
        try:
            qty = int(float(p.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        mktval = round(latest * qty, 2) if qty > 0 else None
        holds.append({"code": code, "cost": cost, "buy": buy, "dates": dates, "close": close, "ient": ient})
        table.append({"code": code, "name": (meta.get(code) or {}).get("name", ""),
                      "buy": buy, "cost": round(cost, 3), "last": round(latest, 3),
                      "qty": (qty if qty > 0 else None), "mktval": mktval,
                      "ret": round(ret * 100, 2),
                      "idx_ret": round(idx_ret * 100, 2) if idx_ret is not None else None,
                      "alpha": round((ret - idx_ret) * 100, 2) if idx_ret is not None else None})
    if not holds:
        # 没有可画曲线的持仓, 但可能有"今日新买"的pending行 -> 仍把table返回, 让逐票表显示(现价实时)
        pend = [t for t in table if t.get("pending")]
        why = "; ".join(f"{s['code']}: {s['why']}" for s in skipped) or (
            "持仓都是今日新买, 待今晚收盘数据后并入曲线(现价见逐票表实时)" if pend else "未知")
        return jsonify({"bench": bench, "bench_name": _BENCH[bench], "dates": [],
                        "portfolio": [], "benchmark": [], "holdings": table,
                        "skipped": skipped, "excluded": excluded,
                        "message": f"暂无历史收益曲线。{why}"})

    start = min(h["buy"] for h in holds)
    # 主轴用完整交易日历(到最新交易日), 基准/个股都 ffill 上去 —— 防某一腿数据更新滞后导致曲线被截断
    cal = _read_calendar()
    last_data = max((h["dates"][-1] for h in holds), default="")
    axis = [x for x in cal if x >= start and (not last_data or x <= last_data)]
    if not axis:
        axis = [x for x in bdates if x >= start]
    bench_axis = _ffill_on_axis(axis, bdates, bclose)   # 基准收盘 ffill 到主轴(滞后部分持平)
    # 每只票: 对齐到 axis 的 ffill 收盘 + 基准腿收益(基准买入价=买入日的基准收盘 ffill)
    for h in holds:
        caxis = _ffill_on_axis(axis, h["dates"], h["close"])
        bi = next((k for k, a in enumerate(axis) if a >= h["buy"]), None)
        ient = bench_axis[bi] if bi is not None and bench_axis[bi] else h["ient"]
        h["sret"] = [(c / h["cost"] - 1.0) if (c and a >= h["buy"]) else None for a, c in zip(axis, caxis)]
        h["iret"] = [(bench_axis[k] / ient - 1.0) if (a >= h["buy"] and bench_axis[k]) else None
                     for k, a in enumerate(axis)]
    port_line, bench_line = [], []
    for i in range(len(axis)):
        sv = [h["sret"][i] for h in holds if h["sret"][i] is not None]
        iv = [h["iret"][i] for h in holds if h["iret"][i] is not None]
        port_line.append(round(sum(sv) / len(sv) * 100, 2) if sv else None)
        bench_line.append(round(sum(iv) / len(iv) * 100, 2) if iv else None)
    table.sort(key=lambda r: (r["alpha"] if r["alpha"] is not None else -999), reverse=True)
    n_win = sum(1 for r in table if r["alpha"] is not None and r["alpha"] > 0)
    for s in skipped:   # 补简称, 让"未纳入"区显示名字而非光代码
        s["name"] = (meta.get(s.get("code", "")) or {}).get("name", "")
    return jsonify({
        "bench": bench, "bench_name": _BENCH[bench], "dates": axis,
        "portfolio": port_line, "benchmark": bench_line, "holdings": table,
        "skipped": skipped, "excluded": excluded,
        "summary": {"n": len(holds), "n_win": n_win,
                    "total_mktval": (round(sum(r["mktval"] for r in table if r.get("mktval")), 2)
                                     if any(r.get("mktval") for r in table) else None),
                    "port_last": port_line[-1] if port_line else None,
                    "bench_last": bench_line[-1] if bench_line else None,
                    "alpha_last": (round(port_line[-1] - bench_line[-1], 2)
                                   if port_line and port_line[-1] is not None and bench_line[-1] is not None else None),
                    "as_of": axis[-1] if axis else "",
                    "bench_asof": bdates[-1] if bdates else ""},
        "metrics": {"portfolio": _perf_metrics(port_line), "benchmark": _perf_metrics(bench_line)},
    })


def _resolve_to_tscode(raw):
    """代码/拼音首字母/名称 → ts_code(600519.SH). 解析不到返回''."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    up = raw.upper()
    if "." in up:
        return up
    c6 = "".join(ch for ch in up if ch.isdigit())
    if len(c6) == 6:
        return f"{c6}.SH" if c6[0] in ("5", "6", "9") else (f"{c6}.SZ" if c6[0] in ("0", "3") else f"{c6}.BJ")
    try:
        conn = sqlite3.connect(STOCK_META_DB); conn.row_factory = sqlite3.Row
        ql = raw.lower(); lk = f"{ql}%"; nl = f"%{raw}%"
        r = conn.execute("SELECT ts_code FROM stock_meta WHERE list_status='L' AND "
                         "(LOWER(code) LIKE ? OR LOWER(pinyin_initials) LIKE ? OR name LIKE ?) "
                         "ORDER BY CASE WHEN LOWER(pinyin_initials) LIKE ? THEN 0 WHEN name LIKE ? THEN 1 ELSE 2 END LIMIT 1",
                         (lk, lk, nl, lk, nl)).fetchone()
        conn.close()
        return r["ts_code"] if r else ""
    except Exception:
        return ""


@app.route("/api/positions/add", methods=["POST"])
def api_positions_add():
    """追加买入批次: {code, cost, date?, qty?, sleeve?}，同代码不再覆盖旧持仓。"""
    b = request.get_json(silent=True) or {}
    code = _resolve_to_tscode(b.get("code", ""))
    if not code:
        return jsonify({"ok": False, "message": "找不到该股票(可用代码/拼音首字母/名称)"})
    try:
        cost = float(b.get("cost"))
        qty = int(float(b.get("qty")))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "追加买入必须填写有效的成本和数量"})
    if cost <= 0 or qty <= 0:
        return jsonify({"ok": False, "message": "成本和数量必须大于0"})
    date = str(b.get("date", "")).strip() or datetime.now().strftime("%Y-%m-%d")
    sleeve = str(b.get("sleeve", "")).strip() or "manual"
    pos = _load_positions()
    prev = next((p for p in pos if str(p.get("code", "")).upper() == code), None)
    lot = _new_lot(cost, qty, date, sleeve)
    if prev:
        prev.setdefault("lots", []).append(lot)
        _recalc_position(prev)
    else:
        pos.append(_recalc_position({"code": code, "lots": [lot], "sleeve": sleeve}))
    _save_positions(pos)
    current = next(p for p in _load_positions() if str(p.get("code", "")).upper() == code)
    return jsonify({"ok": True, "count": len(pos), "lot": lot, "position": current})


@app.route("/api/positions/remove", methods=["POST"])
def api_positions_remove():
    code = str((request.get_json(silent=True) or {}).get("code", "")).strip().upper()
    pos = [p for p in _load_positions() if str(p.get("code", "")).upper() != code]
    _save_positions(pos)
    return jsonify({"ok": True, "count": len(pos)})


@app.route("/api/positions/lot/remove", methods=["POST"])
def api_position_lot_remove():
    """删除误录批次，不记录盈亏。"""
    b = request.get_json(silent=True) or {}
    code, lot_id = str(b.get("code", "")).upper(), str(b.get("lot_id", ""))
    pos = _load_positions()
    p = next((x for x in pos if str(x.get("code", "")).upper() == code), None)
    if not p:
        return jsonify({"ok": False, "message": "找不到持仓"})
    before = len(p.get("lots") or [])
    p["lots"] = [x for x in (p.get("lots") or []) if str(x.get("lot_id")) != lot_id]
    if len(p["lots"]) == before:
        return jsonify({"ok": False, "message": "找不到该批次"})
    if p["lots"]:
        _recalc_position(p)
    else:
        pos = [x for x in pos if x is not p]
    _save_positions(pos)
    return jsonify({"ok": True})


@app.route("/api/positions/sell", methods=["POST"])
def api_positions_sell():
    """做T口径LIFO：盈亏优先匹配最新批次(可含今日买入)，卖单总量仍受昨日持仓可卖额度约束。"""
    b = request.get_json(silent=True) or {}
    code = _resolve_to_tscode(b.get("code", "")) or str(b.get("code", "")).strip().upper()
    pos = _load_positions()
    p = next((x for x in pos if str(x.get("code", "")).upper() == code), None)
    if not p:
        return jsonify({"ok": False, "message": "持仓里找不到该票"})
    sp = None
    try:
        sp = float(b.get("sell_price")) if str(b.get("sell_price", "")).strip() else None
    except (TypeError, ValueError):
        sp = None
    if not sp or sp <= 0:                         # 没填卖价 → 取实时价
        tsc = _code_to_ts(code)
        sp = (_rt_quotes([tsc]).get(tsc) or {}).get("price") if tsc else None
    sell_date = str(b.get("sell_date", "")).strip() or datetime.now().strftime("%Y-%m-%d")
    all_lots = sorted((p.get("lots") or []),
                      key=lambda x: (x.get("buy_date", ""), x.get("created_at", "")),
                      reverse=True)
    old_qty = sum(x["remaining_qty"] for x in all_lots
                  if x.get("buy_date") and x["buy_date"] < sell_date)
    usage = p.get("lifo_usage") or {}
    already_matched_today = int(usage.get("qty") or 0) if usage.get("date") == sell_date else 0
    available = max(0, old_qty - already_matched_today)
    try:
        qty = int(float(b.get("qty"))) if str(b.get("qty", "")).strip() else available
    except (TypeError, ValueError):
        qty = 0
    if qty <= 0:
        return jsonify({"ok": False, "message": "没有可卖数量；当天买入的批次需下一交易日才能卖"})
    if qty > available:
        return jsonify({"ok": False, "message": f"最多可卖 {available} 股；当天锁定仓位不能卖"})
    remain, allocations, matched_locked = qty, [], 0
    for lot in all_lots:
        take = min(remain, lot["remaining_qty"])
        if take <= 0:
            continue
        lot["remaining_qty"] -= take
        allocations.append({"lot_id": lot["lot_id"], "buy_date": lot["buy_date"],
                            "cost": lot["cost"], "qty": take,
                            "same_day_match": bool(lot.get("buy_date") >= sell_date)})
        if lot.get("buy_date") >= sell_date:
            matched_locked += take
        remain -= take
        if remain == 0:
            break
    cost = sum(x["cost"] * x["qty"] for x in allocations) / qty
    buy = min(x["buy_date"] for x in allocations)
    ret = ((sp / cost - 1) * 100) if (sp and cost) else None
    pnl = round((sp - cost) * qty, 2) if (sp and cost and qty) else None
    held = None
    try:
        held = round(sum((datetime.strptime(sell_date, "%Y-%m-%d")
                          - datetime.strptime(x["buy_date"], "%Y-%m-%d")).days * x["qty"]
                         for x in allocations) / qty, 1)
    except Exception:
        pass
    rec = {"code": code, "name": str(b.get("name", "") or p.get("name", "") or code),
           "cost": cost, "buy_date": buy, "qty": qty, "sell_price": round(sp, 3) if sp else None,
           "sell_date": sell_date, "ret_pct": round(ret, 2) if ret is not None else None,
           "pnl": pnl, "held_days": held, "sleeve": str(p.get("sleeve", "") or b.get("sleeve", "") or ""),
           "allocations": allocations,
           "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")}
    hist = _load_sells_history()
    hist.append(rec)
    p["lots"] = [x for x in p.get("lots", []) if x.get("remaining_qty", 0) > 0]
    if matched_locked:
        p["lifo_usage"] = {"date": sell_date, "qty": already_matched_today + matched_locked}
    if p["lots"]:
        _recalc_position(p)
    else:
        pos = [x for x in pos if x is not p]
    _save_trade_state(pos, hist)
    return jsonify({"ok": True, "record": rec,
                    "remaining_qty": sum(x.get("remaining_qty", 0) for x in p.get("lots", []))})


@app.route("/api/sells/history")
def api_sells_history():
    """卖出历史台账 + 汇总(累计盈亏/胜率/平均收益)。"""
    hist = _load_sells_history()
    n = len(hist)
    wins = sum(1 for r in hist if (r.get("ret_pct") or 0) > 0)
    total_pnl = round(sum(r.get("pnl") or 0 for r in hist), 2)
    rets = [r.get("ret_pct") for r in hist if r.get("ret_pct") is not None]
    return jsonify({"history": hist[::-1], "n": n, "wins": wins,
                    "win_rate": round(wins / n * 100, 0) if n else None,
                    "total_pnl": total_pnl, "avg_ret": round(sum(rets) / len(rets), 2) if rets else None})


@app.route("/api/live_performance")
def api_live_performance():
    """实盘业绩按腿归因: 已平仓(sells_history)+持仓中(positions实时浮盈), 按sleeve聚合实盘收益,
    对比各腿回测预期 → 检验'实盘 vs 回测'是否吻合(策略真伪终极关)。"""
    # 各腿回测预期(来自验证记录, 用于对照实盘是否名副其实)
    BT = {
        "pro": {"name": "顾问Pro(底仓)", "bt": "季频对冲 夏~2"},
        "runup": {"name": "业绩抢跑", "bt": "夏2.05@30bps(中证1000)"},
        "repo": {"name": "回购腿", "bt": "CAR[+1,+60]+2.07%/夏0.95"},
        "chipmap": {"name": "海力士映射", "bt": "超基准+0.28%/夏2.75"},
        "inclusion": {"name": "纳入研究", "bt": "事件腿"},
        "manual": {"name": "手动/其他", "bt": "—"},
    }
    hist = _load_sells_history()
    # 持仓中实时浮盈
    held = []
    for p in _load_positions():
        c = _code_to_ts(p.get("code", "")) if p.get("code") else None
        rt = (_rt_quotes([c]).get(c) if c else None) or {}
        last = rt.get("price")
        cost = p.get("cost")
        ret = ((last / cost - 1) * 100) if (last and cost) else None
        held.append({"sleeve": str(p.get("sleeve", "") or "manual"), "ret_pct": ret, "open": True})
    closed = [{"sleeve": str(r.get("sleeve", "") or "manual"), "ret_pct": r.get("ret_pct"),
               "pnl": r.get("pnl"), "held_days": r.get("held_days"), "open": False} for r in hist]
    rows = []
    for key, meta in BT.items():
        cl = [x for x in closed if x["sleeve"] == key]
        op = [x for x in held if x["sleeve"] == key]
        cl_rets = [x["ret_pct"] for x in cl if x["ret_pct"] is not None]
        op_rets = [x["ret_pct"] for x in op if x["ret_pct"] is not None]
        if not cl and not op:
            continue
        wins = sum(1 for r in cl_rets if r > 0)
        rows.append({
            "sleeve": key, "name": meta["name"], "bt": meta["bt"],
            "n_closed": len(cl), "n_open": len(op),
            "closed_avg_ret": round(sum(cl_rets) / len(cl_rets), 2) if cl_rets else None,
            "closed_win_rate": round(wins / len(cl_rets) * 100, 0) if cl_rets else None,
            "closed_pnl": round(sum(x["pnl"] or 0 for x in cl), 2),
            "open_avg_ret": round(sum(op_rets) / len(op_rets), 2) if op_rets else None,
        })
    # 未归因(sleeve为空/未知)的提示
    untagged = sum(1 for x in closed + held if x["sleeve"] == "manual")
    return jsonify({"rows": rows, "n_closed": len(closed), "n_open": len(held),
                    "untagged": untagged, "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})


@app.route("/api/sells/delete", methods=["POST"])
def api_sells_delete():
    """删一条卖出记录(误录修正), 按 recorded_at 匹配。"""
    ra = str((request.get_json(silent=True) or {}).get("recorded_at", ""))
    hist = _load_sells_history()
    hist = [r for r in hist if r.get("recorded_at") != ra]
    _save_sells_history(hist)
    return jsonify({"ok": True, "n": len(hist)})


@app.route("/api/sells/update", methods=["POST"])
def api_sells_update():
    """改一条卖出记录的 卖出日期/卖出价(按 recorded_at 匹配), 重算 收益%/盈亏/持有天数。"""
    b = request.get_json(silent=True) or {}
    ra = str(b.get("recorded_at", ""))
    hist = _load_sells_history()
    rec = next((r for r in hist if r.get("recorded_at") == ra), None)
    if not rec:
        return jsonify({"ok": False, "message": "找不到该记录"})
    sd = str(b.get("sell_date", "")).strip()
    if sd:
        rec["sell_date"] = sd
    if str(b.get("sell_price", "")).strip():
        try:
            rec["sell_price"] = round(float(b.get("sell_price")), 3)
        except (TypeError, ValueError):
            pass
    cost = rec.get("cost"); sp = rec.get("sell_price"); qty = rec.get("qty"); buy = str(rec.get("buy_date", "") or "")
    rec["ret_pct"] = round((sp / cost - 1) * 100, 2) if (sp and cost) else None
    rec["pnl"] = round((sp - cost) * qty, 2) if (sp and cost and qty) else None
    try:
        rec["held_days"] = (datetime.strptime(rec["sell_date"], "%Y-%m-%d") - datetime.strptime(buy[:10], "%Y-%m-%d")).days if buy else rec.get("held_days")
    except Exception:
        pass
    _save_sells_history(hist)
    return jsonify({"ok": True, "record": rec})


@app.route("/api/positions/bulk_qty", methods=["POST"])
def api_positions_bulk_qty():
    """批次模式禁止覆盖汇总数量；数量只能由买入/卖出流水推导。"""
    return jsonify({"ok": False, "message": "批次持仓模式下不能批量覆盖数量，请追加买入或登记部分卖出"}), 400


# ---------- 因子叠加 K 线 (Phase 4) ----------

@app.route("/api/factor/request", methods=["POST"])
def api_factor_request():
    """网页请求抽取某股某因子的时间序列. PC 监听脚本 extract_factor.py 执行回写."""
    code = _normalize_market_code(request.args.get("code"))
    factor = (request.args.get("factor") or "").strip()
    batch = (request.args.get("batch") or "").strip()
    if not code or not factor:
        return jsonify({"ok": False, "message": "缺少 code 或 factor"}), 400
    if not _valid_factor_name(factor):
        return jsonify({"ok": False, "message": "因子名称格式错误"}), 400
    if not _valid_job_label(batch):
        return jsonify({"ok": False, "message": "批次标签包含非法字符"}), 400
    try:
        FACTOR_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        FACTOR_REQUEST.write_text(json.dumps(
            {"requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "code": code, "factor": factor, "batch": batch}, ensure_ascii=False),
            encoding="utf-8")
    except Exception as e:
        return jsonify({"ok": False, "message": f"写请求失败: {e}"}), 500
    return jsonify({"ok": True, "message": f"已请求抽取 {factor} @ {code}"})


@app.route("/api/factor/values")
def api_factor_values():
    """读取因子值结果 + 进度. 前端轮询."""
    return jsonify({
        "pending": FACTOR_REQUEST.exists(),
        "status": _read_json(FACTOR_STATUS),
        "result": _read_json(FACTOR_VALUES),
    })


# ---------- scheduler ----------


def _write_daily_update_status(state: str, attempt: int, **fields) -> dict:
    """Atomically persist the last daily-update outcome on the shared data volume."""
    now_text = datetime.now().isoformat(timespec="seconds")
    with _daily_update_status_lock:
        current = _read_json(DAILY_UPDATE_STATUS_PATH)
        current = dict(current) if isinstance(current, dict) else {}
        payload = {
            "state": state,
            "attempt": int(attempt),
            "updated_at": now_text,
        }
        if current.get("last_success_at"):
            payload["last_success_at"] = current["last_success_at"]
        payload.update({key: value for key, value in fields.items() if value is not None})
        DAILY_UPDATE_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = DAILY_UPDATE_STATUS_PATH.with_name(
            f".{DAILY_UPDATE_STATUS_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, DAILY_UPDATE_STATUS_PATH)
        finally:
            temporary.unlink(missing_ok=True)
    return payload


def _daily_update_health(now: datetime | None = None) -> dict:
    """Return update diagnostics without changing web/scheduler liveness."""
    status = _read_json(DAILY_UPDATE_STATUS_PATH)
    status = dict(status) if isinstance(status, dict) else {}
    now = now or datetime.now()
    last_success = None
    try:
        if status.get("last_success_at"):
            last_success = datetime.fromisoformat(str(status["last_success_at"]))
    except (TypeError, ValueError):
        last_success = None
    stale = last_success is None or (now - last_success).total_seconds() > DAILY_UPDATE_STALE_HOURS * 3600
    return {
        "state": status.get("state", "unknown"),
        "attempt": status.get("attempt"),
        "updated_at": status.get("updated_at"),
        "last_success_at": status.get("last_success_at"),
        "verified_through": status.get("verified_through"),
        "next_retry_at": status.get("next_retry_at"),
        "stale": stale,
    }


def _record_current_daily_update(through: str, reason: str) -> dict:
    """Record a successful startup freshness check without rebuilding data."""
    checked_at = datetime.now().isoformat(timespec="seconds")
    return _write_daily_update_status(
        "current",
        0,
        finished_at=checked_at,
        last_success_at=checked_at,
        verified_through=through,
        reason=reason,
    )


def run_daily_update(attempt: int = 1):
    log.info("=== daily update triggered by scheduler ===")
    started_at = datetime.now().isoformat(timespec="seconds")
    _write_daily_update_status("running", attempt, started_at=started_at)
    try:
        from scripts.update_daily import main as update_main
        update_main()
        log.info("=== daily update succeeded ===")
        try:
            from scripts.build_stock_meta import main as meta_main
            meta_main(force=False)  # refresh weekly per STOCK_META_REFRESH_DAYS
        except Exception as e:
            log.warning(f"stock_meta refresh skipped: {e}")
        # 热榜避雷: 每日自动刷新(tushare ths_hot), 结果同步到共享目录供页面展示
        try:
            from scripts.export_hot_avoid import main as hot_avoid_main
            hot_avoid_main()
            import shutil
            _data_dir = Path(__file__).parent / "data"
            for _fn in ("hot_avoid.json", "hot_avoid_history.json"):
                _src = _data_dir / _fn
                _dst = PREDICT_JSON.parent / _fn
                if _src.exists():
                    shutil.copy2(str(_src), str(_dst))
            log.info("hot_avoid refreshed and synced to shared dir")
        except Exception as e:
            log.warning(f"hot_avoid refresh skipped: {e}")
        # 数据更新后, 自动预测下一交易日买入清单 (仅当本机负责计算; 方案B由PC算)
        if PREDICT_COMPUTE_HERE:
            try:
                from scripts.predict_qlib import predict_and_save
                predict_and_save()
            except Exception as e:
                log.warning(f"qlib 预测跳过: {e}")
        finished_at = datetime.now().isoformat(timespec="seconds")
        _write_daily_update_status(
            "succeeded",
            attempt,
            started_at=started_at,
            finished_at=finished_at,
            last_success_at=finished_at,
        )
    except Exception as e:
        finished_at = datetime.now().isoformat(timespec="seconds")
        try:
            _write_daily_update_status(
                "failed",
                attempt,
                started_at=started_at,
                finished_at=finished_at,
                error=f"{type(e).__name__}: {e}"[:1000],
            )
        except Exception:
            log.exception("failed to persist daily update failure status")
        log.exception(f"daily update failed: {e}")
        raise


def _schedule_daily_update_retry(
    scheduler,
    attempt: int,
    error: Exception,
    now: datetime | None = None,
) -> datetime | None:
    """Schedule one bounded retry, only while it remains the same local evening."""
    if attempt > DAILY_UPDATE_MAX_RETRIES:
        return None
    now = now or datetime.now()
    delay = DAILY_UPDATE_RETRY_BASE_MINUTES * (2 ** (attempt - 1))
    retry_at = now + timedelta(minutes=delay)
    if retry_at.date() != now.date() or retry_at.hour > DAILY_UPDATE_RETRY_CUTOFF_HOUR:
        return None
    next_attempt = attempt + 1
    _write_daily_update_status(
        "failed",
        attempt,
        error=f"{type(error).__name__}: {error}"[:1000],
        next_retry_at=retry_at.isoformat(timespec="seconds"),
    )
    scheduler.add_job(
        _run_scheduled_daily_update,
        DateTrigger(run_date=retry_at),
        args=[scheduler, next_attempt],
        id="daily_update_retry",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=max(300, delay * 60),
    )
    log.warning(
        "daily update attempt %s failed; retry %s/%s scheduled at %s",
        attempt,
        next_attempt,
        DAILY_UPDATE_MAX_RETRIES + 1,
        retry_at.isoformat(timespec="seconds"),
    )
    return retry_at


def _run_scheduled_daily_update(scheduler, attempt: int = 1):
    try:
        run_daily_update(attempt=attempt)
    except Exception as error:
        retry_at = _schedule_daily_update_retry(scheduler, attempt, error)
        if retry_at is None:
            log.error(
                "daily update attempt %s failed; no further same-evening retry",
                attempt,
            )
        raise


def run_weekly_predict_retrain():
    log.info("=== weekly qlib retrain + predict triggered ===")
    try:
        from scripts.predict_qlib import update_and_predict
        update_and_predict(retrain=True)
        log.info("=== weekly qlib retrain succeeded ===")
    except Exception as e:
        log.exception(f"weekly qlib retrain failed: {e}")
        raise


def run_weekly_financials_update():
    log.info("=== weekly financials update triggered ===")
    started_at = datetime.now().isoformat(timespec="seconds")
    _write_weekly_financials_status("running", started_at=started_at)
    try:
        from scripts.fetch_financials import fetch_all
        fetch_all()
        finished_at = datetime.now().isoformat(timespec="seconds")
        _write_weekly_financials_status(
            "succeeded",
            started_at=started_at,
            finished_at=finished_at,
            last_success_at=finished_at,
        )
        log.info("=== weekly financials update succeeded ===")
    except Exception as e:
        finished_at = datetime.now().isoformat(timespec="seconds")
        try:
            _write_weekly_financials_status(
                "failed",
                started_at=started_at,
                finished_at=finished_at,
                error=f"{type(e).__name__}: {e}"[:1000],
            )
        except Exception:
            log.exception("failed to persist weekly financials failure status")
        log.exception(f"financials update failed: {e}")
        raise


def _write_weekly_financials_status(state: str, **fields) -> dict:
    """Atomically persist weekly financial refresh state and last success."""
    now_text = datetime.now().isoformat(timespec="seconds")
    with _weekly_financials_status_lock:
        current = _read_json(WEEKLY_FINANCIALS_STATUS_PATH)
        current = dict(current) if isinstance(current, dict) else {}
        payload = {"state": state, "updated_at": now_text}
        if current.get("last_success_at"):
            payload["last_success_at"] = current["last_success_at"]
        payload.update({key: value for key, value in fields.items() if value is not None})
        WEEKLY_FINANCIALS_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = WEEKLY_FINANCIALS_STATUS_PATH.with_name(
            f".{WEEKLY_FINANCIALS_STATUS_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, WEEKLY_FINANCIALS_STATUS_PATH)
        finally:
            temporary.unlink(missing_ok=True)
    return payload


def _weekly_financials_due_at(now: datetime) -> datetime:
    monday = now.date() - timedelta(days=now.weekday())
    return datetime.combine(monday, datetime.min.time()).replace(hour=2)


def _weekly_financials_catchup_due(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    due_at = _weekly_financials_due_at(now)
    if now < due_at:
        return False
    status = _read_json(WEEKLY_FINANCIALS_STATUS_PATH)
    status = dict(status) if isinstance(status, dict) else {}
    try:
        last_success = datetime.fromisoformat(str(status.get("last_success_at") or ""))
        if last_success.tzinfo is not None:
            last_success = last_success.replace(tzinfo=None)
    except (TypeError, ValueError):
        last_success = None
    return last_success is None or last_success < due_at


def _weekly_financials_health(now: datetime | None = None) -> dict:
    status = _read_json(WEEKLY_FINANCIALS_STATUS_PATH)
    status = dict(status) if isinstance(status, dict) else {}
    return {
        "state": status.get("state", "unknown"),
        "updated_at": status.get("updated_at"),
        "last_success_at": status.get("last_success_at"),
        "scheduled_for": status.get("scheduled_for"),
        "catchup_due": _weekly_financials_catchup_due(now),
    }


def _schedule_weekly_financials_catchup(scheduler, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    if not _weekly_financials_catchup_due(now):
        return None
    run_at = now + timedelta(seconds=5)
    scheduler.add_job(
        run_weekly_financials_update,
        DateTrigger(run_date=run_at),
        id="weekly_financials_catchup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    _write_weekly_financials_status(
        "scheduled",
        scheduled_for=run_at.isoformat(timespec="seconds"),
        reason="startup_catchup",
    )
    log.warning("weekly financials startup catch-up scheduled at %s", run_at.isoformat(timespec="seconds"))
    return run_at


def init_scheduler():
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    sched.add_job(
        _run_scheduled_daily_update,
        CronTrigger(hour=DAILY_HOUR, minute=DAILY_MINUTE),
        args=[sched, 1],
        id="daily_update",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=6 * 3600,   # 21:00 错过 6 小时内仍补跑 (防调度繁忙/短暂延迟)
    )
    # 每周一凌晨 02:00 跑财务数据更新 (财报披露集中在工作日, 周更细够用)
    sched.add_job(
        run_weekly_financials_update,
        CronTrigger(day_of_week="mon", hour=2, minute=0),
        id="weekly_financials",
        max_instances=1,
        coalesce=True,
    )
    # 每周日凌晨 03:00 重训 qlib 预测模型 (仅当本机负责计算; 方案B由PC算, 默认不挂)
    if PREDICT_COMPUTE_HERE:
        sched.add_job(
            run_weekly_predict_retrain,
            CronTrigger(day_of_week="sun", hour=3, minute=0),
            id="weekly_predict_retrain",
            max_instances=1,
            coalesce=True,
        )
    _schedule_weekly_financials_catchup(sched)
    sched.start()
    log.info(f"scheduler started: daily update at {DAILY_HOUR:02d}:{DAILY_MINUTE:02d}, "
             f"weekly financials Mon 02:00, weekly qlib retrain Sun 03:00 (Asia/Shanghai)")
    return sched


def boot_stock_meta():
    """On container start, build stock_meta.db if missing."""
    try:
        from scripts.build_stock_meta import main as meta_main
        meta_main(force=False)
    except Exception as e:
        log.warning(f"initial stock_meta build skipped: {e}")


def _instrument_metadata_max_end(path: Path | None = None) -> str | None:
    """Return the latest valid end date published in instruments/all.txt."""
    path = path or (QLIB_DATA_PATH / "instruments" / "all.txt")
    try:
        end_dates = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            parts = raw_line.strip().split("\t")
            if len(parts) < 3:
                continue
            try:
                end_dates.append(date.fromisoformat(parts[2]).isoformat())
            except ValueError:
                continue
        return max(end_dates) if end_dates else None
    except OSError:
        return None


def boot_freshness_catchup():
    """启动时检查 bin 数据是否落后于'最近应有数据的交易日', 落后就后台补一次更新。

    防止夜间 21:00 任务因容器重启/停机被跳过 (APScheduler 不补跑错过的任务)。
    判定: 取最近交易日历; 今日若是交易日且已过 16:00 则应有今日数据, 否则到上一交易日。
    """
    def _check():
        try:
            cal = _read_calendar()
            if not cal:
                return
            repaired_through = None
            metadata_max = _instrument_metadata_max_end()
            if metadata_max and metadata_max < cal[-1]:
                log.warning(
                    "startup found a partially published Qlib tail: calendar=%s, instruments=%s; "
                    "attempting validated tail repair",
                    cal[-1], metadata_max,
                )
                try:
                    from scripts.repair_qlib_tail import repair_qlib_tail

                    repair_summary = repair_qlib_tail(
                        QLIB_DATA_PATH,
                        PARQUET_DIR,
                        through=cal[-1],
                    )
                    repaired_through = str(repair_summary["through"])
                    log.info("startup Qlib tail repair succeeded: %s", repair_summary)
                except Exception:
                    # A missing source row/bin requires the complete rebuild below.
                    log.exception("startup Qlib tail repair failed; falling back to full update")
            now = datetime.now()
            pro = _tushare_api()
            start = (now - timedelta(days=12)).strftime("%Y%m%d")
            dfc = pro.trade_cal(exchange="SSE", start_date=start,
                                end_date=now.strftime("%Y%m%d"), is_open="1")
            opens = sorted(str(d) for d in dfc["cal_date"].tolist())
            if not opens:
                return
            if opens[-1] == now.strftime("%Y%m%d") and now.hour < 16:
                opens = opens[:-1]      # 今日还没收盘/未发布, 不计今日
            if not opens:
                return
            should = opens[-1]
            should_iso = f"{should[:4]}-{should[4:6]}-{should[6:]}"
            if repaired_through and repaired_through >= should_iso:
                log.info("startup data recovery is current through %s", repaired_through)
                _record_current_daily_update(repaired_through, "startup_tail_repair")
                return
            # 日历可能被"看K线时按需补数"提前, 不代表全市场; 用一批股票实际 bin 末日期的中位数判断
            sample = []
            try:
                dirs = [d.name for d in (QLIB_DATA_PATH / "features").iterdir() if d.is_dir()]
                for code in dirs[:80]:
                    si, v = _read_bin(code, "close")
                    if v.size and si + v.size - 1 < len(cal):
                        sample.append(cal[si + v.size - 1])
            except Exception:
                pass
            ref = sorted(sample)[len(sample) // 2] if sample else cal[-1]   # 中位数末日期
            if ref < should_iso:
                log.info(f"启动检查: 全市场数据落后 (样本中位 {ref} < 应有 {should_iso}), 后台补全量更新")
                run_daily_update()
            else:
                log.info(f"启动检查: 数据已最新 (样本中位 {ref})")
                _record_current_daily_update(ref, "startup_freshness_check")
        except Exception as e:
            log.warning(f"启动数据新鲜度检查跳过: {e}")
    # 放后台线程, 不阻塞 Flask 启动 (全量更新可能较久)
    threading.Thread(target=_check, daemon=True).start()


if __name__ == "__main__":
    boot_stock_meta()
    init_scheduler()
    boot_freshness_catchup()
    log.info(f"starting Flask on {HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
