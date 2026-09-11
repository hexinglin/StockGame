"""
模块名称: engine/trade_store.py
说明:    成交明细持久化存储（PostgreSQL，每日整日替换语义）

职责:
- replace_day(date, records, source, cmd_id): 整日替换写入 —— 先清空该日全部
  记录再整批插入（同一天重复采集/重复导入天然幂等），并更新采集状态；
- load_day(date): 读取某日成交记录（key 与 agent 上报结构一致，供 analysis 复用）；
- get_day_status(date): 读取采集状态（成功/失败、来源、条数、时间）；
- mark_failed(date, error, cmd_id): 标记采集失败（已入库的记录保持不变）；
- delete_day(date): 物理删除某日记录与状态（测试/维护用）。
"""
from ..dbdata.database import db
from ..dbdata.models import TradeRecord, TradeFetchDay
from ..utils.timeutil import now_cn, fmt_cn


def _f(v):
    """安全转 float（导入文本可能带千分位/空值）"""
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _i(v):
    """安全转 int"""
    try:
        return int(float(str(v).replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def _upsert_status(date, source="", status="", record_count=0,
                   cmd_id="", error=""):
    """更新（或新建）某日采集状态"""
    row = db.session.get(TradeFetchDay, date)
    if row is None:
        row = TradeFetchDay(trade_date=date)
        db.session.add(row)
    row.source = source or row.source
    row.status = status or row.status
    row.record_count = record_count
    row.cmd_id = cmd_id or row.cmd_id
    row.error = str(error or "")[:500]
    row.fetched_at = now_cn()
    return row


def replace_day(date, records, source, cmd_id=""):
    """整日替换写入：清空该日记录后整批插入，并置采集状态为 success"""
    db.session.query(TradeRecord).filter(
        TradeRecord.trade_date == date).delete()
    rows = []
    for r in records:
        rows.append(TradeRecord(
            trade_date=date,
            code=str(r.get("code", "") or "")[:20],
            name=str(r.get("name", "") or "")[:50],
            direction=str(r.get("direction", "") or "")[:10],
            price=_f(r.get("price", 0)),
            volume=_i(r.get("volume", 0)),
            amount=_f(r.get("amount", 0)),
            trade_time=str(r.get("time", "") or "")[:19],
            trade_id=str(r.get("trade_id", "") or "")[:50],
            order_id=str(r.get("order_id", "") or "")[:50],
            source=source,
        ))
    if rows:
        db.session.add_all(rows)
    _upsert_status(date, source=source, status="success",
                   record_count=len(rows), cmd_id=cmd_id, error="")
    db.session.commit()
    return len(rows)


def mark_failed(date, error, cmd_id=""):
    """标记采集失败（已入库的记录与来源保持不变）

    注意：source 是随记录走的（agent/import），失败仅记录错误状态，
    不得覆盖已导入数据的来源标签。
    """
    _upsert_status(date, source="", status="failed",
                   record_count=0, cmd_id=cmd_id, error=error)
    db.session.commit()


def load_day(date):
    """读取某日成交记录（按成交时间升序；key 与上报结构一致）"""
    rows = (TradeRecord.query
            .filter(TradeRecord.trade_date == date)
            .order_by(TradeRecord.trade_time.asc(), TradeRecord.id.asc())
            .all())
    return [{
        "time": r.trade_time, "code": r.code, "name": r.name,
        "direction": r.direction, "price": r.price, "volume": r.volume,
        "amount": r.amount, "trade_id": r.trade_id, "order_id": r.order_id,
    } for r in rows]


def get_day_status(date):
    """采集状态（不存在返回 None）"""
    row = db.session.get(TradeFetchDay, date)
    if row is None:
        return None
    return {
        "date": row.trade_date,
        "source": row.source or "",
        "status": row.status or "",
        "record_count": row.record_count or 0,
        "cmd_id": row.cmd_id or "",
        "error": row.error or "",
        "fetched_at": fmt_cn(row.fetched_at),
    }


def delete_day(date):
    """物理删除某日记录与状态（测试/维护用）"""
    n = db.session.query(TradeRecord).filter(
        TradeRecord.trade_date == date).delete()
    db.session.query(TradeFetchDay).filter(
        TradeFetchDay.trade_date == date).delete()
    db.session.commit()
    return n
