"""
模块名称: utils/autotrade_db.py
说明:    AutoTrade 库（data_source.autotrade_dsn）只读连接辅助。
         与 scripts/mock_agent.py 的 parse_dsn/get_conn 同一套解析逻辑，
         app 内不能跨项目 import scripts，故此处独立维护一份。
"""
import psycopg2


def parse_dsn(dsn: str) -> dict:
    """解析 postgresql://user:pass@host:port/dbname"""
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
    return {"user": user, "password": password, "host": host,
            "port": int(port), "dbname": dbname}


def get_conn(dsn: str):
    """建立 AutoTrade 库 psycopg2 连接（调用方负责 close）"""
    return psycopg2.connect(**parse_dsn(dsn))
