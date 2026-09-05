"""
当日快照行情 ORM 模型 — 游戏数据源

快照语义（QMT 推送为当日某时刻的股票快照，非 3s 增量 bar）:
- close = 最新价；high/low = 截至该时刻的当日滚动最高/最低
- volume/amount = 截至该时刻的当日累计成交量/额（单调不减）
- 今开 open / 昨收 last_close 为当日常量：不在此表逐点维护，统一存放
本表仅存逐点行情，只在游戏运行时读取，页面日期管理不触碰本表
（天维度行情 + 日期选择的唯一权威表是 game_days，见 game_day.py）
"""
from datetime import datetime

from sqlalchemy import Column, String, Float, BigInteger, DateTime, UniqueConstraint
from ..database import Base


class TickData(Base):
    """当日快照点序列（每个时间点仅一条）"""
    __tablename__ = "tick_data"
    __table_args__ = (
        UniqueConstraint('code', 'time_key', name='uq_tick_code_time'),
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
