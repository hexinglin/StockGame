"""
数据库迁移脚本 — 自动建库建表（幂等，可重复执行）

用法: python migrate_db.py [--config config.yaml]

流程:
1. 以管理员身份连接现有 PG 实例的 postgres 默认库，检查 stockgame 库是否存在，
   不存在则 CREATE DATABASE stockgame;
2. 连接 stockgame 库执行 migrations/init.sql 建表 + 存量刷新（幂等，无清理类操作）。
3. game_days 昨收回补：完整交易日但 last_close 缺失/无效时，从 AutoTrade 库
   stock_kline 天维度读取（1d 行 last_close 优先，其次当日 1m 首根）并更新
   （仅生成侧一次回补，tick 表已不再存 last_close）。
4. 昨收防线 apply_last_close_guardrail：回补后仍 last_close 无效的孤儿/异常行
   物理删除，SET NOT NULL + 收紧 CHECK 约束（先回补救能救的，再删救不了的）。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2
from psycopg2 import sql as pg_sql

from app.utils.config import Config


def _parse_dsn(dsn: str):
    """解析 postgresql://user:pass@host:port/dbname 形式的连接串"""
    prefix = "postgresql://"
    if dsn.startswith(prefix):
        dsn = dsn[len(prefix):]
    userinfo, _, rest = dsn.partition("@")
    user, _, password = userinfo.partition(":")
    host, _, port_db = rest.partition(":")
    if "/" in port_db:
        port, _, dbname = port_db.partition("/")
    else:
        port, dbname = port_db, "postgres"
    return {"user": user, "password": password, "host": host, "port": port, "dbname": dbname}


def ensure_database(admin_dsn: str, dbname: str):
    """检查数据库是否存在，不存在则创建"""
    params = _parse_dsn(admin_dsn)
    params["dbname"] = "postgres"
    conn = psycopg2.connect(**params)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        exists = cur.fetchone()
        if exists:
            print(f"[检查] 数据库 {dbname} 已存在，跳过创建")
        else:
            cur.execute(pg_sql.SQL("CREATE DATABASE {}").format(pg_sql.Identifier(dbname)))
            print(f"[创建] 数据库 {dbname} 创建成功")
        cur.close()
    finally:
        conn.close()


def run_sql_file(dsn: str, sql_path: str):
    """在目标库执行 SQL 脚本（幂等）"""
    params = _parse_dsn(dsn)
    conn = psycopg2.connect(**params)
    conn.autocommit = True
    try:
        with open(sql_path, encoding="utf-8") as f:
            sql_text = f.read()
        cur = conn.cursor()
        cur.execute(sql_text)
        cur.close()
        print(f"[执行] {sql_path} 执行完成")
    finally:
        conn.close()


def backfill_day_last_close(dsn: str, auto_dsn: str) -> int:
    """game_days 昨收回补（幂等）：完整交易日但 last_close 缺失/无效的记录

    从 AutoTrade 库 stock_kline 天维度读取昨收（口径与引擎 _kline_last_close
    一致：当日 1d 行 last_close 优先，其次当日 1m 首根），仅更新缺失行，
    覆盖该 (code, trade_date) 下的 qmt/sim 双数据源记录。返回更新条数。
    """
    if not auto_dsn:
        print("[回补] 未配置 data_source.autotrade_dsn，跳过昨收回补")
        return 0
    conn = psycopg2.connect(**_parse_dsn(dsn))
    auto = psycopg2.connect(**_parse_dsn(auto_dsn))
    n = 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT code, trade_date FROM game_days "
            "WHERE is_complete = true AND (last_close IS NULL OR last_close <= 0) "
            "GROUP BY code, trade_date")
        pairs = cur.fetchall()
        auto_cur = auto.cursor()
        for code, trade_date in pairs:
            # 1d 当日行 last_close 优先，其次 1m 当日首根
            auto_cur.execute(
                "SELECT last_close FROM stock_kline "
                "WHERE code=%s AND period='1d' AND last_close > 0 "
                "AND time_key >= %s::date AND time_key < (%s::date + interval '1 day')",
                (code, trade_date, trade_date))
            row = auto_cur.fetchone()
            if not row or not row[0] or row[0] != row[0]:
                auto_cur.execute(
                    "SELECT last_close FROM stock_kline "
                    "WHERE code=%s AND period='1m' AND last_close > 0 "
                    "AND time_key >= %s::date AND time_key < (%s::date + interval '1 day') "
                    "ORDER BY time_key LIMIT 1",
                    (code, trade_date, trade_date))
                row = auto_cur.fetchone()
            if not row or not row[0] or row[0] != row[0]:
                continue
            cur.execute(
                "UPDATE game_days SET last_close=%s, updated_at=now() "
                "WHERE code=%s AND trade_date=%s "
                "AND (last_close IS NULL OR last_close <= 0)",
                (float(row[0]), code, trade_date))
            n += cur.rowcount
        auto_cur.close()
        conn.commit()
        cur.close()
    finally:
        auto.close()
        conn.close()
    if n:
        print(f"[回补] game_days 昨收从 stock_kline 回补 {n} 条（{len(pairs)} 日）")
    else:
        print(f"[回补] 无需回补（检查 {len(pairs)} 日：stock_kline 均无可用昨收或记录已有效）")
    return n


def apply_last_close_guardrail(dsn: str) -> int:
    """昨收防线（幂等）：删除 last_close 无效的孤儿/异常行 + 约束收紧

    必须在 backfill_day_last_close 之后执行：能回补修复的行先修，救不了的
    残留（无对应 stock_kline 昨收）在此物理删除。tick 快照数据不受影响，
    需要时以有效昨收 refresh_all_days 重建。收紧 last_close NOT NULL + CHECK
    （必有效，无 NULL 语义）。返回删除行数。
    """
    conn = psycopg2.connect(**_parse_dsn(dsn))
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM game_days WHERE last_close IS NULL "
            "OR NOT (last_close > 0 AND last_close < 'Infinity')")
        n = cur.rowcount
        cur.execute("ALTER TABLE game_days "
                    "ALTER COLUMN last_close SET NOT NULL")
        cur.execute("ALTER TABLE game_days DROP CONSTRAINT IF EXISTS "
                    "ck_game_days_last_close_valid")
        cur.execute("ALTER TABLE game_days ADD CONSTRAINT "
                    "ck_game_days_last_close_valid "
                    "CHECK (last_close > 0 AND last_close < 'Infinity')")
        cur.close()
    finally:
        conn.close()
    if n:
        print(f"[防线] 清理 {n} 行 last_close 无效的孤儿/异常行，"
              "CHECK/NOT NULL 约束已收紧")
    else:
        print("[防线] 无需清理（game_days 全部行 last_close 有效），"
              "CHECK/NOT NULL 约束已收紧")
    return n


def main():
    parser = argparse.ArgumentParser(description="StockGame 数据库迁移")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args()

    cfg = Config.get_instance(args.config)
    dsn = cfg.get("database.path", "")
    if not dsn:
        print("错误: 未配置 database.path", file=sys.stderr)
        sys.exit(1)

    # 管理员连接串：替换库名为 postgres
    admin_dsn = dsn.rsplit("/", 1)[0] + "/postgres"
    dbname = dsn.rsplit("/", 1)[-1]

    print(f"==> 目标实例: {admin_dsn}")
    print(f"==> 目标数据库: {dbname}")

    ensure_database(admin_dsn, dbname)

    sql_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations", "init.sql")
    run_sql_file(dsn, sql_path)

    auto_dsn = cfg.get("data_source.autotrade_dsn", "")
    backfill_day_last_close(dsn, auto_dsn)
    apply_last_close_guardrail(dsn)

    print("数据库迁移完成!")


if __name__ == "__main__":
    main()
