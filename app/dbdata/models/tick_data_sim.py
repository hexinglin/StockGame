"""
转换模拟快照行情 ORM 模型 — stockkline 1min → 3s 等间隔快照流
（QMT 无该日数据时的兑底数据源；结构同 tick_data，均为当日快照口径）
"""
from datetime import datetime

from sqlalchemy import Column, String, Float, BigInteger, DateTime, UniqueConstraint
from ..database import Base


class TickDataSim(Base):
    """模拟当日快照点序列（结构同 tick_data，每个时间点仅一条）"""
    __tablename__ = "tick_data_sim"
    __table_args__ = (
        UniqueConstraint('code', 'time_key', name='uq_tick_sim_code_time'),
    )
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(20), nullable=False, index=True)
    trade_date = Column(String(10), nullable=False, index=True)
    time_key = Column(String(19), nullable=False)   # 'YYYY-MM-DD HH:MM:SS'
    high = Column(Float)             # 截至该时刻的当日最高（滚动）
    low = Column(Float)              # 截至该时刻的当日最低（滚动）
    close = Column(Float)            # 最新价
    volume = Column(BigInteger, default=0)     # 当日累计成交量
    amount = Column(Float, default=0)          # 当日累计成交额
    created_at = Column(DateTime, default=datetime.now)
