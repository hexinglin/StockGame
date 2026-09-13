"""
模块名称: engine/trading_calendar.py
说明:    A 股交易日历存储与查询（trading_days 表）

数据来源: QMT Agent 每日自动上报 ContextInfo.get_trading_dates() 的结果
（该接口只能在 QMT 客户端内执行，服务端无法直连）；upsert 幂等，只增
不改 —— 历史交易日永不变化，重复上报安全。

能力:
- upsert_days: 批量写入交易日（'20260101' 紧凑 / '2026-01-01' 均接受）；
- is_trading_day: 判断某日是否交易日；日历未覆盖该区间时返回 None
  （调用方按需回退，如 trade_collector 回退周末判断）；
- list_days / prev_trading_day / next_trading_day / stats。
"""
import logging
import re

from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..dbdata.database import db
from ..dbdata.models import TradingDay

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})$")


def _norm(v):
    """'20260101' / '2026-01-01' → '2026-01-01'；非法返回 ''"""
    s = str(v or "").strip()
    m = _DATE_RE.match(s)
    if not m:
        return ""
    return "%s-%s-%s" % m.groups()


def upsert_days(dates, source="qmt"):
    """批量写入交易日（幂等：已存在的日期跳过，不更新），返回新增数"""
    rows = [{"trade_date": d, "source": source}
            for d in sorted({d for d in (_norm(x) for x in dates) if d})]
    if not rows:
        return 0
    # PostgreSQL 批量 upsert（无冲突跳过）—— 单条一条 INSERT 完成，无查询竞态
    stmt = pg_insert(TradingDay).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["trade_date"])
    res = db.session.execute(stmt)
    db.session.commit()
    added = res.rowcount if res.rowcount and res.rowcount > 0 else 0
    return added


def _coverage():
    """日历覆盖范围 → (最小日期, 最大日期) 或 (None, None)"""
    row = db.session.query(db.func.min(TradingDay.trade_date),
                           db.func.max(TradingDay.trade_date)).one()
    return row[0], row[1]


def is_trading_day(date):
    """是否交易日 → True/False；日历为空或该日期在覆盖范围之外 → None"""
    d = _norm(date)
    if not d:
        return None
    lo, hi = _coverage()
    if lo is None or d < lo or d > hi:
        return None
    return db.session.query(db.session.query(TradingDay)
                            .filter(TradingDay.trade_date == d).exists()).scalar()


def list_days(start=None, end=None):
    """区间交易日列表（升序；start/end 可只传其一）"""
    q = db.session.query(TradingDay.trade_date)
    s, e = _norm(start), _norm(end)
    if s:
        q = q.filter(TradingDay.trade_date >= s)
    if e:
        q = q.filter(TradingDay.trade_date <= e)
    return [r[0] for r in q.order_by(TradingDay.trade_date.asc()).all()]


def prev_trading_day(date):
    """严格早于 date 的最近交易日；无 → ''"""
    d = _norm(date)
    if not d:
        return ""
    row = (db.session.query(TradingDay.trade_date)
           .filter(TradingDay.trade_date < d)
           .order_by(TradingDay.trade_date.desc()).first())
    return row[0] if row else ""


def next_trading_day(date):
    """严格晚于 date 的最近交易日；无 → ''"""
    d = _norm(date)
    if not d:
        return ""
    row = (db.session.query(TradingDay.trade_date)
           .filter(TradingDay.trade_date > d)
           .order_by(TradingDay.trade_date.asc()).first())
    return row[0] if row else ""


def stats():
    """日历概览：总量 / 覆盖范围 / 最近写入时间"""
    row = (db.session.query(db.func.count(TradingDay.trade_date),
                            db.func.min(TradingDay.trade_date),
                            db.func.max(TradingDay.trade_date),
                            db.func.max(TradingDay.synced_at)).one())
    return {"count": row[0] or 0, "min": row[1] or "", "max": row[2] or "",
            "synced_at": row[3].strftime("%Y-%m-%d %H:%M:%S") if row[3] else ""}


def delete_range(start, end):
    """删除区间内交易日（测试/维护用）"""
    s, e = _norm(start), _norm(end)
    if not s or not e:
        return 0
    n = db.session.query(TradingDay).filter(
        TradingDay.trade_date >= s, TradingDay.trade_date <= e).delete()
    db.session.commit()
    return n
