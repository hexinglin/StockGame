"""
QMT 心跳状态 ORM 模型
"""
from sqlalchemy import Column, String, Integer, Boolean, DateTime
from ..database import Base
from ...utils.timeutil import now_cn


class AgentStatus(Base):
    """QMT Agent 心跳状态"""
    __tablename__ = "agent_status"
    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_name = Column(String(50), unique=True, nullable=False, index=True)
    role = Column(String(50), default="")     # 角色/职责（心跳自报：行情信息维护/维护·工具查询…）
    last_heartbeat_at = Column(DateTime)
    last_tick_at = Column(DateTime)
    is_alive = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=now_cn, onupdate=now_cn)
