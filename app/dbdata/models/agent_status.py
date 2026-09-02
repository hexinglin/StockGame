"""
QMT 心跳状态 ORM 模型
"""
from datetime import datetime

from sqlalchemy import Column, String, Integer, Boolean, DateTime
from ..database import Base


class AgentStatus(Base):
    """QMT Agent 心跳状态"""
    __tablename__ = "agent_status"
    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_name = Column(String(50), unique=True, nullable=False, index=True)
    last_heartbeat_at = Column(DateTime)
    last_tick_at = Column(DateTime)
    is_alive = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
