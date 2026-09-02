"""
游戏委托 ORM 模型
"""
from datetime import datetime

from sqlalchemy import Column, String, Integer, Float, DateTime, Text
from ..database import Base


class GameOrder(Base):
    """游戏委托（状态机 pending/filled/cancelled/rejected，整单成交）"""
    __tablename__ = "game_orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(50), unique=True, nullable=False, index=True)
    round_id = Column(Integer, nullable=False, index=True)
    code = Column(String(20))
    direction = Column(String(10))     # buy/sell
    order_type = Column(String(10), default="limit")  # limit/market
    price = Column(Float, default=0)
    shares = Column(Integer, default=0)
    frozen_amount = Column(Float, default=0)   # 下单冻结金额（买单，含手续费）
    status = Column(String(20), default="pending")
    filled_shares = Column(Integer, default=0)
    filled_price = Column(Float, default=0)
    fee = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.now)
    filled_at = Column(DateTime)
    reject_reason = Column(Text, default="")
