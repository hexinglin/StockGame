"""
数据库迁移脚本 — 自动建库建表（幂等，可重复执行）

用法: python migrate_db.py [--config config.yaml]

流程:
1. 以管理员身份连接现有 PG 实例的 postgres 默认库，检查 stockgame 库是否存在，
   不存在则 CREATE DATABASE stockgame;
2. 连接 stockgame 库执行 migrations/init.sql 建表（幂等）。
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

    print("数据库迁移完成!")


if __name__ == "__main__":
    main()
