#!/usr/bin/env bash
# =============================================================================
# StockGame 部署脚本
#
# 用法:
#   ./deploy.sh up           # 构建并启动（启动前在容器内自动执行数据库迁移）
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
        echo "==> 构建镜像"
        ${DC} build
        # 迁移在容器内执行（config.dev.yaml → host.docker.internal 直连宿主机
        # 现有 PG，作用于复用中的 dev 库；init.sql 幂等无破坏性操作，可重复执行）。
        # 不依赖宿主机安装 Python；失败即中止（set -e），不再带警告继续。
        echo "==> 执行数据库迁移 (migrate_db.py, 容器内)"
        ${DC} run --rm stockgame python migrate_db.py --config config.dev.yaml
        echo "==> 启动 StockGame 服务"
        ${DC} up -d
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
