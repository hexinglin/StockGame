"""
reset_all_data.py — 全量清空重建（行情快照化架构：历史数据不兼容，整体推倒）

用法:
  python scripts/reset_all_data.py [--config config.yaml] [--yes]

流程:
1. 按依赖序 DROP 业务表：game_trades → game_orders → game_rounds →
   game_days → tick_data_sim → tick_data（agent_status 保留）
2. 重新执行 migrations/init.sql（建表 + game_days 尾部聚合刷新）
3. game_days 昨收从 AutoTrade 库 stock_kline 回补（复用 migrate_db）
4. 昨收防线：回补后仍 last_close 无效的孤儿/异常行删除 + 约束收紧
   （apply_last_close_guardrail，顺序同 migrate_db）
5. 清空 Redis 轮次/心跳缓存（game:* / heartbeat:*，best-effort，连接失败不阻断）

--yes 跳过交互确认（自动化/CI 场景使用）。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2

from app.utils.config import Config
from migrate_db import (_parse_dsn, backfill_day_last_close, run_sql_file,
                        apply_last_close_guardrail)

_DROP_TABLES = [
    "game_trades", "game_orders", "game_rounds",   # 依赖序：先删引用方
    "game_days", "tick_data_sim", "tick_data",
]


def drop_all_tables(dsn: str) -> None:
    """按依赖序 DROP 全部业务表（幂等；agent_status 保留）"""
    params = _parse_dsn(dsn)
    conn = psycopg2.connect(**params)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        for t in _DROP_TABLES:
            cur.execute('DROP TABLE IF EXISTS "%s" CASCADE' % t)
        cur.close()
        print("[清空] 已 DROP 业务表: %s" % ", ".join(_DROP_TABLES))
    finally:
        conn.close()


def clear_redis(config) -> None:
    """清空 Redis 轮次/心跳缓存（best-effort）"""
    if not config.get("redis.enabled", True):
        print("[Redis] 已禁用，跳过清理")
        return
    try:
        import redis
        r = redis.Redis(
            host=config.get("redis.host", "localhost"),
            port=int(config.get("redis.port", 6379)),
            db=int(config.get("redis.db", 0)),
            password=config.get("redis.password", None) or None,
            decode_responses=True,
            socket_connect_timeout=3, socket_timeout=3,
        )
        r.ping()
    except Exception as e:
        print("[Redis] 连接失败，跳过清理: %s" % e)
        return
    n = 0
    try:
        for pattern in ("game:*", "heartbeat:*"):
            for k in r.scan_iter(match=pattern, count=500):
                r.delete(k)
                n += 1
        print("[Redis] 已清理 %d 个 key（game:* / heartbeat:*）" % n)
    except Exception as e:
        print("[Redis] 清理失败（部分已删 %d 个）: %s" % (n, e))


def main():
    parser = argparse.ArgumentParser(description="StockGame 全量清空重建（行情快照化）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    args = parser.parse_args()

    cfg = Config.get_instance(args.config)
    dsn = cfg.get("database.path", "")
    if not dsn:
        print("错误: 未配置 database.path", file=sys.stderr)
        sys.exit(1)

    if not args.yes:
        ans = input("危险操作：将清空全部行情/轮次/委托/成交数据并重建表结构，确认? [y/N] ")
        if ans.strip().lower() != "y":
            print("已取消")
            return

    drop_all_tables(dsn)

    sql_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "migrations", "init.sql")
    run_sql_file(dsn, sql_path)

    auto_dsn = cfg.get("data_source.autotrade_dsn", "")
    backfill_day_last_close(dsn, auto_dsn)
    apply_last_close_guardrail(dsn)

    clear_redis(cfg)
    print("全量清空重建完成！请重启后端服务（ORM 已按新表结构加载）")


if __name__ == "__main__":
    main()
