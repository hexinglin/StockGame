"""
游戏成交记录 ORM 模型
"""
from sqlalchemy import Column, String, Integer, Float
from ..database import Base


class GameTrade(Base):
    """游戏成交记录"""
    __tablename__ = "game_trades"
    id = Column(Integer, primary_key=True, autoincrement=True)
    round_id = Column(Integer, nullable=False, index=True)
    order_id = Column(String(50), index=True)
    code = Column(String(20))
    direction = Column(String(10))
    price = Column(Float, default=0)
    shares = Column(Integer, default=0)
    fee = Column(Float, default=0)
    trade_time = Column(String(19), default="")
