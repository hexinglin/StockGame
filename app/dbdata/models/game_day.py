"""
交易日 ORM 模型 — 游戏选择/管理层（可开局交易日索引）

设计说明:
- 本表不存行情数据：可开局日（is_complete）由 day_kline（与 tick 表对齐的
  天维度原始行情）生成时派生同步，开局所需原始数据（昨收/OHLC/tick 对齐信息）
  一律从 day_kline 读取；
- round_count 记录该 (code, trade_date, data_source) 已创建的轮次数，
  属游戏侧管理状态，不随行情重刷而丢失；
- tick_data / tick_data_sim 仅存逐根 3s 行情，只在游戏运行时读取。
"""
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Integer,
                        String, UniqueConstraint)
from ..database import Base


class GameDay(Base):
    """可开局交易日（每 code + trade_date + data_source 一条，选择/管理用）"""
    __tablename__ = "game_days"
    __table_args__ = (
        UniqueConstraint('code', 'trade_date', 'data_source',
                         name='uq_game_day_code_date_src'),
    )
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(20), nullable=False, index=True)
    trade_date = Column(String(10), nullable=False, index=True)
    data_source = Column(String(10), nullable=False, default="qmt")  # qmt/sim
    is_complete = Column(Boolean, default=False)     # 可开局（末根 >= 15:00:00，随 day_kline 派生）
    round_count = Column(Integer, default=0)         # 该日已创建轮次数（游戏侧状态）
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "trade_date": self.trade_date,
            "data_source": self.data_source,
            "is_complete": bool(self.is_complete),
            "round_count": self.round_count or 0,
        }
