"""
转换模拟行情 ORM 模型 — stockkline 1min → 3s 拓展（QMT 无该日数据时的兜底数据源）
"""
from datetime import datetime

from sqlalchemy import Column, String, Float, BigInteger, DateTime, UniqueConstraint
from ..database import Base


class TickDataSim(Base):
    """转换模拟 3s 行情（结构同 tick_data，每个时间点仅一条）"""
    __tablename__ = "tick_data_sim"
    __table_args__ = (
        UniqueConstraint('code', 'time_key', name='uq_tick_sim_code_time'),
    )
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(20), nullable=False, index=True)
    trade_date = Column(String(10), nullable=False, index=True)
    time_key = Column(String(19), nullable=False)   # 'YYYY-MM-DD HH:MM:SS'
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(BigInteger, default=0)
    amount = Column(Float, default=0)
    # 昨收不在此维护：天维度原始行情（含 last_close）统一存放 day_kline 表
    # （与 tick 表对齐），game_days 为游戏选择/管理层，开局数据从 day_kline 读取
    created_at = Column(DateTime, default=datetime.now)
