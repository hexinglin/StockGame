"""
交易日历 ORM 模型 — A 股交易日持久化（QMT get_trading_dates 拉取）

设计说明:
- trading_days: 全市场交易日历（一行一个交易日）；数据由 QMT Agent 每日
  自动上报（ContextInfo.get_trading_dates 只能在 QMT 客户端内执行），
  服务端幂等 upsert（只增不改，历史交易日永不变化）；
- 用途: 判断某日是否交易日 / 区间交易日列表 / 前后交易日推算；
  成交自动采集（trade_collector）据此决定当日是否下发命令，
  摆脱"周末判断"的粗略口径（无法识别节假日调休）。
"""
from sqlalchemy import Column, DateTime, String

from ..database import Base
from ...utils.timeutil import now_cn


class TradingDay(Base):
    """A 股交易日（只增不改；同一日期重复上报幂等跳过）"""
    __tablename__ = "trading_days"
    trade_date = Column(String(10), primary_key=True)  # 'YYYY-MM-DD'
    source = Column(String(20), default="qmt")         # 数据来源 qmt/import
    synced_at = Column(DateTime, default=now_cn)       # 首次写入时间
