"""
游戏轮次 ORM 模型
"""
from datetime import datetime

from sqlalchemy import Column, String, Integer, Float, DateTime
from ..database import Base


class GameRound(Base):
    """游戏轮次 — 一个轮次 = 一个交易日的完整游戏周期"""
    __tablename__ = "game_rounds"
    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(20), default="588000.SH", index=True)
    trade_date = Column(String(10), nullable=False, index=True)
    status = Column(String(20), default="ready")  # ready/running/paused/finished/aborted
    speed = Column(Integer, default=1)            # 1/10/60
    data_source = Column(String(10), default="qmt")  # 行情数据源: qmt(实盘)/sim(转换模拟)
    created_at = Column(DateTime, default=datetime.now)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    initial_cash = Column(Float, default=0)
    base_shares = Column(Integer, default=0)
    initial_assets = Column(Float, default=0)
    final_assets = Column(Float, default=0)
    realized_pnl = Column(Float, default=0)       # 已实现盈亏
    fee_total = Column(Float, default=0)
    last_price = Column(Float, default=0)
    last_time_key = Column(String(19), default="")
