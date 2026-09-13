# -*- coding: utf-8 -*-
"""
import_trade_xlsx.py — 券商「历史成交查询」导出 xlsx 导入 trade_records

与页面「📥 导入历史」同一链路: parse_export_workbook 解析 → group_by_date
按记录自带交易日分组 → trade_store.replace_day 逐日整日替换（source=import，
幂等可重复执行）。适合首次批量补齐历史 / 无页面环境运维使用。

用法:
  python scripts/import_trade_xlsx.py                       # 导入 DEFAULT_FILE
  python scripts/import_trade_xlsx.py --file 路径.xlsx      # 指定导出文件
  python scripts/import_trade_xlsx.py --dry-run             # 仅解析不入库

说明:
  - 文件格式: 前 4 行营业部/账号等元信息 + 表头（日期/成交时间/交易类别/证券
    代码/证券名称/成交价格/成交数量/证券余额/成交金额/委托编号/成交编号/股东代码）
    + 数据行；一次可跨多个交易日；
  - 日期取「日期」列，方向与市场取「交易类别」（融资买入→buy，
    融资卖出还款→sell，上海/深圳前缀补 .SH/.SZ）；
  - 重复导入同一文件安全（整日覆盖），当日 Agent 采集的数据也会被覆盖为导入数据。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("STOCKGAME_CONFIG", "config.yaml")

from flask import Flask

from app.dbdata.database import db
from app.utils.config import Config
from app.engine import trade_store
from app.engine.trade_import import (group_by_date,
                                     parse_export_workbook)

DEFAULT_FILE = r"F:\stock\2026-09-13-两融-历史成交查询.xlsx"


def _make_app() -> Flask:
    """真实库最小 app（同 init_db 口径，无调度器）"""
    app = Flask(__name__)
    cfg = Config.get_instance()
    app.config["SQLALCHEMY_DATABASE_URI"] = cfg.get("database.path")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    from app.dbdata import models  # noqa: F401
    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="导入券商历史成交 xlsx")
    ap.add_argument("--file", default=DEFAULT_FILE, help="导出文件路径")
    ap.add_argument("--dry-run", action="store_true", help="仅解析不入库")
    args = ap.parse_args()

    records, meta = parse_export_workbook(args.file)
    print("解析 %d 笔（表头第 %d 行，跳过 %d 行）"
          % (meta["parsed"], meta["header_line"], len(meta["skipped"])))
    for s in meta["skipped"][:10]:
        print("  跳过 行%s: %s | %s" % (s["line"], s["reason"], s["text"]))
    if not records:
        print("无有效记录，结束")
        return 1

    days = group_by_date(records)
    print("覆盖 %d 个交易日: %s ~ %s"
          % (len(days), min(days), max(days)))
    for d in sorted(days):
        buys = sum(1 for r in days[d] if r["direction"] == "buy")
        print("  %s: %d 笔（买 %d / 卖 %d）"
              % (d, len(days[d]), buys, len(days[d]) - buys))
    if args.dry_run:
        print("dry-run：未入库")
        return 0

    app = _make_app()
    with app.app_context():
        total = 0
        for d in sorted(days):
            n = trade_store.replace_day(d, days[d], source="import")
            total += n
            # 回读校验
            back = trade_store.load_day(d)
            assert len(back) == n, "回读笔数不符 %s" % d
        print("入库完成：共 %d 笔，回读校验通过" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
