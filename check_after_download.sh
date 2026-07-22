#!/bin/bash
set -e
cd "$(dirname "$0")"

LOG_FILE="check_after_download.log"
> "$LOG_FILE"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "开始监控 scheduler，等待财务数据下载完成..."
TIMEOUT=3600
ELAPSED=0
while ! docker logs quantinvest-scheduler --tail 30 2>/dev/null | grep -q "weekly financials update succeeded"; do
  sleep 60
  ELAPSED=$((ELAPSED + 60))
  if [ $ELAPSED -ge $TIMEOUT ]; then
    log "等待超时（${TIMEOUT}秒），未能确认下载完成"
    exit 1
  fi
  if [ $((ELAPSED % 300)) -eq 0 ]; then
    PROGRESS=$(docker logs quantinvest-scheduler --tail 5 2>/dev/null | grep -oE '\[[0-9]+/[0-9]+\]' | tail -1)
    log "仍在下载中... 最近进度: ${PROGRESS:-未知}"
  fi
done

log "财务数据下载完成，开始检查各页面..."

BASE="http://127.0.0.1:5055"

check_page() {
  local url=$1
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" "$url" || echo "000")
  local size
  size=$(curl -s "$url" | wc -c || echo "0")
  log "$url -> HTTP $code, 内容大小 ${size} bytes"
}

check_api() {
  local url=$1
  local desc=$2
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" "$url" || echo "000")
  local body
  body=$(curl -s "$url" || echo "")
  local len=${#body}
  log "$desc ($url) -> HTTP $code, 返回体长度 $len"
  if [ "$code" = "200" ] && [ "$len" -gt 50 ]; then
    log "  数据示例: $(echo "$body" | head -c 200)"
  fi
}

log "=== 健康检查 ==="
curl -s "$BASE/api/health" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

log "=== 主要页面 ==="
check_page "$BASE/"
check_page "$BASE/screen"
check_page "$BASE/pattern"
check_page "$BASE/predict"
check_page "$BASE/backtest"
check_page "$BASE/intraday"
check_page "$BASE/strategy"
check_page "$BASE/advisor"
check_page "$BASE/advisor-pro"
check_page "$BASE/daily"
check_page "$BASE/ensemble"
check_page "$BASE/predict-compare"
check_page "$BASE/alpha158-arena"
check_page "$BASE/mining"

log "=== 关键 API ==="
check_api "$BASE/api/search?q=000001" "股票搜索"
check_api "$BASE/api/kline?code=000001.SZ&days=30" "K线数据"
check_api "$BASE/api/screen/config" "选股配置"
check_api "$BASE/api/screen" "选股结果"
check_api "$BASE/api/predict" "预测结果"
check_api "$BASE/api/advisor-pro/result" "策略结果"
check_api "$BASE/api/daily/status" "运维状态"

log "检查完成"
