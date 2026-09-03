"""
日行情 ORM 模型 — 与 tick 表对齐的天维度原始行情（开局原始数据源）

设计说明:
- tick_data / tick_data_sim 仅存逐根 3s 行情，只在游戏运行时读取；
- 本表在 tick 入库（上传/批量写入/迁移）时同步按日聚合写入，数据与 tick 表
  对齐（同 (code, trade_date) 对账）：open/high/low/close/volume/amount/
  tick_count/first_time_key/last_time_key/is_complete 均来自当日 tick 实况，
  游戏选择与开局读取不再扫描 tick 表；
- last_close（昨收）在本表维护：随生成方携带（hint），缺失时仅生成侧从
  AutoTrade 库 stock_kline 天维度回补（1d 优先，其次 1m 首根），运行时不依赖；
- game_days 为游戏选择/管理层（可开局日、轮次计数），is_complete 由本表派生
  同步，开局所需原始数据（昨收/OHLC/对齐信息）一律从本表读取。
"""
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Float, Integer,
                        String, UniqueConstraint)
from ..database import Base


class DayKline(Base):
    """天维度原始行情（每 code + trade_date + data_source 一条，与 tick 表对齐）"""
    __tablename__ = "day_kline"
    __table_args__ = (
        UniqueConstraint('code', 'trade_date', 'data_source',
                         name='uq_day_kline_code_date_src'),
    )
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(20), nullable=False, index=True)
    trade_date = Column(String(10), nullable=False, index=True)
    data_source = Column(String(10), nullable=False, default="qmt")  # qmt/sim
    # ── 日线原始数据（生成时随 tick 落库，与 tick 表对齐）──
    open = Column(Float)                             # 日级今开（首根 tick open）
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)                            # 日级收盘（末根 tick close）
    volume = Column(BigInteger, default=0)
    amount = Column(Float, default=0)
    last_close = Column(Float, default=0)            # 昨收（上一交易日收盘，生成时维护）
    # ── tick 对齐信息（与 tick 表现存数据一致）──
    tick_count = Column(Integer, default=0)          # 该日已入库 3s tick 数
    first_time_key = Column(String(19), default="")  # 首根 tick 时间
    last_time_key = Column(String(19), default="")   # 末根 tick 时间
    is_complete = Column(Boolean, default=False)     # 完整交易日（末根 >= 15:00:00）
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "trade_date": self.trade_date,
            "data_source": self.data_source,
            "tick_count": self.tick_count or 0,
            "first_time_key": self.first_time_key or "",
            "last_time_key": self.last_time_key or "",
            "is_complete": bool(self.is_complete),
            "open": self.open, "high": self.high,
            "low": self.low, "close": self.close,
            "volume": self.volume or 0,
            "amount": self.amount or 0,
            "last_close": self.last_close or 0,
        }
