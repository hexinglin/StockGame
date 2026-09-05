"""
交易日 ORM 模型 — 天维度真实行情（日期管理唯一权威表）

设计说明（快照口径，2026-09 起 QMT 推送为当日快照而非 3s bar）:
- tick_data / tick_data_sim 逐点如实记录当日快照（close=最新价、high/low=
  截至该时刻的当日滚动极值、volume/amount=当日累计量额），游戏运行时读取；
- 本表在快照入库（上传/批量写入/转换/迁移）时同步聚合写入当日终值，数据与
  tick 表对齐（同 (code, trade_date) 对账）：high=max(各快照滚动 high)/low=min/
  close=末条快照 close/volume/amount=末条快照累计值（非逐点求和）/
  tick_count/first_time_key/last_time_key/is_complete 均来自当日快照实况，
  游戏选择与开局读取不再扫描 tick 表；
- open（今开）/last_close（昨收）为当日常量，在本表维护：随生成方携带（hint），
  缺失时 open 以首条 close 兑底、last_close 仅生成侧从 AutoTrade 库 stock_kline
  天维度回补（1d 优先，其次 1m 首根），运行时不依赖；
- 本表为日期选择/管理唯一权威源：页面可开局日、引擎创建轮次判定、开局原始
  数据（昨收/今开/OHLC）均查本表（is_complete 完整日标记），tick 表仅在游戏
  运行时读取。
"""
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Float, Integer,
                        String, UniqueConstraint)
from ..database import Base


class GameDay(Base):
    """天维度真实行情（每 code + trade_date + data_source 一条，与 tick 表对齐
    的日期管理权威表）"""
    __tablename__ = "game_days"
    __table_args__ = (
        UniqueConstraint('code', 'trade_date', 'data_source',
                         name='uq_game_day_code_date_src'),
    )
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(20), nullable=False, index=True)
    trade_date = Column(String(10), nullable=False, index=True)
    data_source = Column(String(10), nullable=False, default="qmt")  # qmt/sim
    # ── 日线真实数据（快照入库时随 tick 落库，与 tick 表对齐）──
    open = Column(Float)                             # 今开（当日常量，随上传 hint 维护）
    high = Column(Float)                             # 当日最高（max 各快照滚动 high）
    low = Column(Float)                              # 当日最低（min 各快照滚动 low）
    close = Column(Float)                            # 当日最新收盘（末条快照 close）
    volume = Column(BigInteger, default=0)           # 当日累计成交量终值（末条快照）
    amount = Column(Float, default=0)                # 当日累计成交额终值（末条快照）
    last_close = Column(Float, default=0)            # 昨收（上一交易日收盘，生成时维护）
    # ── tick 对齐信息（与 tick 表现存快照一致）──
    tick_count = Column(Integer, default=0)          # 该日已入库快照条数
    first_time_key = Column(String(19), default="")  # 首条快照时间
    last_time_key = Column(String(19), default="")   # 末条快照时间
    is_complete = Column(Boolean, default=False)     # 完整交易日（末条快照 >= 15:00:00）
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
