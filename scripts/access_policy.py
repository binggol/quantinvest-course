from __future__ import annotations

from dataclasses import dataclass


PUBLIC_PATHS = {
    "/login",
    "/logout",
    "/membership-expired",
    "/service-info",
    "/terms",
    "/privacy",
    "/risk-disclosure",
    "/api/health",
    "/favicon.ico",
}


@dataclass(frozen=True)
class NavLink:
    path: str
    label: str
    feature: str


NAV_GROUPS = (
    (
        "数据服务",
        (
            NavLink("/member", "会员首页", "member_workspace"),
            NavLink("/", "K线与行情", "market_data"),
            NavLink("/watchlist", "自选资料", "market_data"),
            NavLink("/industry", "行业数据", "market_data"),
            NavLink("/ipo", "新股日历", "market_data"),
            NavLink("/new-stocks", "上市资料", "market_data"),
        ),
    ),
    (
        "高级数据",
        (
            NavLink("/screen", "自定义筛选", "advanced_data"),
            NavLink("/fund-features", "基本面字段", "advanced_data"),
            NavLink("/transfer-events", "询价转让事件", "advanced_data"),
            NavLink("/placement-events", "定增事件", "advanced_data"),
            NavLink("/placement-transfer", "事件合并数据", "advanced_data"),
        ),
    ),
    (
        "内部模型",
        (
            NavLink("/daily", "数据操作台", "internal_operations"),
            NavLink("/rdagent", "RD-Agent", "internal_operations"),
            NavLink("/mining", "挖矿进展", "internal_operations"),
            NavLink("/fund-mining", "基本面挖矿", "internal_operations"),
            NavLink("/alphagen", "AlphaGen", "internal_operations"),
            NavLink("/rdquant", "RD-Quant", "internal_operations"),
            NavLink("/predict", "预测任务", "internal_operations"),
            NavLink("/predict-a158", "Alpha158预测", "internal_operations"),
            NavLink("/predict-batch", "批次预测", "internal_operations"),
            NavLink("/alpha158-arena", "模型擂台", "internal_operations"),
            NavLink("/universe-arena", "股票池对比", "internal_operations"),
            NavLink("/batch-arena", "批次对比", "internal_operations"),
            NavLink("/fund-predict-compare", "因子批次对比", "internal_operations"),
            NavLink("/predict-compare", "预测对比", "internal_operations"),
            NavLink("/ensemble", "集成模型", "internal_operations"),
            NavLink("/timesnet-select", "TimesNet", "internal_operations"),
        ),
    ),
    (
        "内部研究",
        (
            NavLink("/backtest", "模型回测", "internal_operations"),
            NavLink("/research", "研究记录", "internal_operations"),
            NavLink("/strategy", "策略回测", "internal_operations"),
            NavLink("/advisor", "策略顾问", "internal_operations"),
            NavLink("/advisor-pro", "顾问Pro", "internal_operations"),
            NavLink("/advisor-pro/backtest", "Pro参数回测", "internal_operations"),
            NavLink("/advisor-pro-plus", "顾问Pro+", "internal_operations"),
            NavLink("/huijin-etf-flow", "国家队ETF", "internal_operations"),
            NavLink("/rolling-earnings", "滚动业绩", "internal_operations"),
            NavLink("/earnings-entry-lag", "中报入场研究", "internal_operations"),
            NavLink("/earnings-commentary", "业绩预告点评", "internal_operations"),
            NavLink("/runup", "业绩事件", "internal_operations"),
            NavLink("/repo", "回购事件", "internal_operations"),
            NavLink("/avoid", "风险模型", "internal_operations"),
            NavLink("/snowball", "雪球风险", "internal_operations"),
            NavLink("/chipmap", "跨市场映射", "internal_operations"),
            NavLink("/tech-external", "科技外部信号", "internal_operations"),
            NavLink("/cross-market", "跨市场研究", "internal_operations"),
            NavLink("/intraday", "日内研究", "internal_operations"),
            NavLink("/intraday-classic", "经典日内信号", "internal_operations"),
            NavLink("/quality", "质量模型", "internal_operations"),
            NavLink("/index_inclusion", "指数纳入研究", "internal_operations"),
            NavLink("/index_inclusion_pro", "指数纳入Pro", "internal_operations"),
            NavLink("/late-disclosure", "披露事件", "internal_operations"),
            NavLink("/asset-injection", "资产注入事件", "internal_operations"),
            NavLink("/event-legs", "事件策略", "internal_operations"),
            NavLink("/report", "内部个股报告", "internal_operations"),
            NavLink("/forecast", "内部盈利预测", "internal_operations"),
            NavLink("/thesis", "内部主题研究", "internal_operations"),
            NavLink("/tradingagents", "TradingAgents", "internal_operations"),
        ),
    ),
    (
        "内部持仓",
        (
            NavLink("/trade", "执行清单", "internal_operations"),
            NavLink("/sell", "卖出记录", "internal_operations"),
            NavLink("/holdings", "内部持仓", "internal_operations"),
            NavLink("/eventstop", "内部止损模型", "internal_operations"),
            NavLink("/rsrs", "内部择时", "internal_operations"),
            NavLink("/track", "实盘验证", "internal_operations"),
            NavLink("/portfolio", "内部组合", "internal_operations"),
            NavLink("/portfolio-guard", "内部风控", "internal_operations"),
            NavLink("/stockbond", "内部股债模型", "internal_operations"),
            NavLink("/top-risk", "内部见顶模型", "internal_operations"),
            NavLink("/money-outflow", "内部资金信号", "internal_operations"),
            NavLink("/intraday_t", "内部做T", "internal_operations"),
            NavLink("/surge-t", "内部冲高策略", "internal_operations"),
        ),
    ),
    (
        "账户",
        (
            NavLink("/account", "账户与套餐", "member_workspace"),
            NavLink("/admin", "管理后台", "internal_operations"),
            NavLink("/admin/members", "会员管理", "manage_members"),
        ),
    ),
)


PAGE_FEATURES = {
    link.path: link.feature
    for _, links in NAV_GROUPS
    for link in links
}
PAGE_FEATURES["/readme"] = "member_workspace"


# Only these APIs are part of the external data product. Any unlisted API is
# internal by default, which prevents new model/action endpoints from being
# exposed accidentally.
MEMBER_API_FEATURES = {
    "/api/search": "market_data",
    "/api/kline": "market_data",
    "/api/watchlist": "market_data",
    "/api/watchlist/add": "market_data",
    "/api/watchlist/remove": "market_data",
    "/api/fundamentals_for": "market_data",
    "/api/watchlist/fundamentals": "market_data",
    "/api/industry": "market_data",
    "/api/ipo": "market_data",
    "/api/new_stocks": "market_data",
    "/api/new_stocks/detail": "market_data",
    "/api/screen/config": "advanced_data",
    "/api/screen": "advanced_data",
    "/api/fund_features": "advanced_data",
    "/api/index_nav": "advanced_data",
    "/api/transfer_events": "advanced_data",
    "/api/placement_events": "advanced_data",
    "/api/placement_transfer": "advanced_data",
}


def required_feature(path: str, method: str = "GET") -> str | None:
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return None
    if path in PAGE_FEATURES:
        return PAGE_FEATURES[path]
    if path.startswith("/admin/"):
        return "manage_members"
    if path.startswith("/api/"):
        return MEMBER_API_FEATURES.get(path, "internal_operations")
    return "internal_operations"


def csrf_required(path: str, method: str) -> bool:
    method_name = str(method or "GET").upper()
    if method_name not in {"GET", "HEAD", "OPTIONS"}:
        return True
    if method_name != "GET" or not path.startswith("/api/"):
        return False
    feature = required_feature(path, method_name)
    if feature != "internal_operations":
        return False
    action_segments = {
        "analyze",
        "mine",
        "model_eval",
        "request",
        "run",
        "run_all",
        "runall",
    }
    return any(segment in action_segments for segment in path.strip("/").split("/"))


def navigation_for(member: dict | None, has_feature) -> list[dict]:
    groups: list[dict] = []
    for label, links in NAV_GROUPS:
        allowed = [
            {"path": link.path, "label": link.label}
            for link in links
            if has_feature(member, link.feature)
        ]
        if allowed:
            groups.append({"label": label, "links": allowed})
    return groups

