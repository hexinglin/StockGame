#!/usr/bin/env bash
# =============================================================================
# StockGame 部署脚本
#
# 用法:
#   ./deploy.sh up           # 构建并启动（启动前自动执行数据库迁移）
#   ./deploy.sh down         # 停止
#   ./deploy.sh restart      # 重启
#   ./deploy.sh logs         # 查看日志
#   ./deploy.sh ps           # 查看状态
# =============================================================================
set -euo pipefail

ACTION="${1:-up}"
ACTION="$(echo "${ACTION}" | tr '[:upper:]' '[:lower:]')"

# ---- 兼容 docker compose 与 docker-compose ----
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
else
    DC="docker-compose"
fi

mkdir -p logs

case "${ACTION}" in
    up)
        echo "==> 执行数据库迁移 (migrate_db.py)"
        python migrate_db.py || echo "[警告] 数据库迁移失败，请检查现有 PG 实例连接"
        echo "==> 构建并启动 StockGame 服务"
        ${DC} up -d --build
        echo "服务地址: http://<服务器IP>:16000"
        ;;
    down|stop)
        ${DC} down
        ;;
    restart)
        ${DC} restart
        ;;
    logs)
        ${DC} logs -f --tail=200
        ;;
    ps|status)
        ${DC} ps
        ;;
    *)
        echo "用法: $0 [up|down|restart|logs|ps]" >&2
        exit 1
        ;;
esac
