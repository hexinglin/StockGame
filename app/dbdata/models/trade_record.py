"""
QMT 交易记录 ORM 模型 — 成交明细持久化（每日整日替换）+ 采集状态

设计说明:
- trade_records: 某日的全部成交明细（来源 agent=QMT 当日采集 / import=导出导入）；
  同一日期每次采集/导入先清空再整批写入（整日快照语义，天然幂等）；
- trade_fetch_days: 每日采集状态（成功/失败、来源、条数、cmd_id、时间），
  供页面展示与自动采集补采判断（成功后不再自动重发命令）。
"""
from sqlalchemy import (BigInteger, Column, DateTime, Float, Integer,
                        String, Text)
from ..database import Base
from ...utils.timeutil import now_cn


class TradeRecord(Base):
    """某日成交明细（每次采集/导入整日替换）"""
    __tablename__ = "trade_records"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    trade_date = Column(String(10), nullable=False, index=True)  # 'YYYY-MM-DD'
    code = Column(String(20), default="")
    name = Column(String(50), default="")
    direction = Column(String(10), default="")     # buy/sell/unknown
    price = Column(Float, default=0)
    volume = Column(BigInteger, default=0)
    amount = Column(Float, default=0)
    trade_time = Column(String(19), default="")    # 'YYYY-MM-DD HH:MM:SS'
    trade_id = Column(String(50), default="")
    order_id = Column(String(50), default="")
    source = Column(String(10), default="")        # agent/import
    created_at = Column(DateTime, default=now_cn)


class TradeFetchDay(Base):
    """每日采集状态（成功/失败；成功即不再自动重发命令）"""
    __tablename__ = "trade_fetch_days"
    trade_date = Column(String(10), primary_key=True)  # 'YYYY-MM-DD'
    source = Column(String(10), default="")            # agent/import
    status = Column(String(10), default="")            # success/failed
    record_count = Column(Integer, default=0)
    cmd_id = Column(String(50), default="")
    error = Column(Text, default="")
    fetched_at = Column(DateTime)
    updated_at = Column(DateTime, default=now_cn, onupdate=now_cn)
