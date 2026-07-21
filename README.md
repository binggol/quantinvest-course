# quantinvest（教学版）

A 股量化投资工具 · Flask + ECharts + tushare + qlib · Docker 一键部署

> 本仓库为《AI量化实战21讲》配套教学代码，来自讲师自用的生产系统。
> 仅供课程学习使用，请勿转售（详见 LICENSE）。
> 系统仅为技术教学演示，所有策略与输出不构成任何投资建议。

## 功能一览

- **行情**：股票代码/名称/拼音首字母搜索，ECharts K线 + MA + 前/后复权切换
- **基本面选股** `/screen`：扣非净利润增速、ROE、单季增速多期筛选
- **机器学习预测** `/predict`：LightGBM + qlib 因子，每日自动预测，每周自动重训
- **模型擂台**：lgb / xgb / catboost / PatchTST / DLinear 同台对比
- **回测与净值**：模型净值曲线叠加基准；事件研究回测（业绩/回购/纳入指数等）
- **策略引擎** `/advisor-pro`：regime 切换的多策略组合
- **风控避雷**：热榜避雷、问询函、资金流监控
- **运维台** `/daily`：数据健康检查、任务状态
- **自动化**：每晚 21:00 自动增量更新数据，APScheduler 独立容器，失败自动重试

## 快速开始（Windows / Mac / Linux 通用）

只需要一台装了 Docker 的电脑：

1. 安装 Docker Desktop（Windows/Mac）或 Docker Engine（Linux），并启动
2. 注册 tushare 拿到 token：https://tushare.pro → 个人中心 → 接口 TOKEN
3. 配置环境变量：

```bash
cp .env.example .env
# 编辑 .env，填入 TUSHARE_TOKEN；
# 生成 SECRET_KEY：python -c "import secrets; print(secrets.token_urlsafe(48))"
```

4. 启动（首次构建约 10-20 分钟，qlib 需要编译）：

```bash
docker compose up -d --build
```

5. 浏览器打开 http://127.0.0.1:5055

首次启动后系统会在每天 21:00 自动下载更新数据；也可以手动触发首次全量下载
（tushare 有限速，全市场历史数据首次拉取需要较长时间，属正常现象）。

## 硬件要求

- 内存 ≥ 4GB（推荐 8GB），磁盘 ≥ 20GB
- 群晖 NAS（支持 Container Manager）可作为 7×24 运行环境，课程第 15 课专门讲解

## 目录结构

```
app.py               # Flask 主应用（路由入口）
scripts/             # 数据管道、选股、回测、预测等核心脚本
templates/ static/   # Web 前端
docs/                # 专题文档（回测双引擎对账等）
research/            # 实验记录与结论索引（含证伪案例）
pc-agent/            # PC 端 RD-Agent 协同脚本（第 19 课选学内容）
data/                # 运行时生成（数据库与结果 JSON，已 gitignore）
qlib_data/           # 运行时生成（行情数据，已 gitignore）
```

## 常见问题

- **首次 build 很慢**：qlib 需要源码编译，属正常；之后启动秒级
- **页面提示无数据**：等 21:00 自动更新，或先手动触发一次数据下载
- **tushare 拉取失败**：检查 token 是否填入 `.env`、账号是否有 `daily` 接口权限
- **端口被占用**：改 `.env` 里的 `QI_PUBLISH_PORT`

## 会员体系（教学用默认关闭）

`.env` 中 `QI_AUTH_ENABLED=0` 时免登录直接体验全部功能；
设为 `1` 可启用三角色（member/operator/admin）与三套餐（basic/data_pro/enterprise）
的会员门控，管理员在 `/admin/members` 后台管理账号。

## 风险声明

本系统为编程与量化技术教学项目。所有数据、策略、模型输出仅用于教学演示，
不构成任何投资建议；据此操作，风险自负。
