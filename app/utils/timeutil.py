"""
模块名称: utils/timeutil.py
说明:    统一时间口径 — 东八区（北京时间）墙钟时间

行情/交易/心跳/账户等全部对外展示与入库时间统一为北京时间（naive 墙钟，
可直接写入 TIMESTAMP 列），与进程/容器时区解耦：即使部署在默认 UTC 的
容器中，时间仍为北京时间，不再出现 8 小时偏差。

使用约定:
  - 入库/展示: now_cn() / from_ts_cn() / fmt_cn()
  - 由库值反解时间戳（心跳超时判断等）: ts_from_cn()
  - Agent 上报的 unix 时间戳本身与时区无关，用 from_ts_cn 转为北京时间
"""
from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))     # 东八区（北京时间）


def now_cn() -> datetime:
    """当前北京时间（naive 墙钟，可直接入库）"""
    return datetime.now(CN_TZ).replace(tzinfo=None)


def now_str_cn() -> str:
    """当前北京时间字符串 'YYYY-MM-DD HH:MM:SS'"""
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def from_ts_cn(ts) -> datetime:
    """unix 时间戳 → 北京时间（naive 墙钟，可直接入库）"""
    return datetime.fromtimestamp(float(ts), CN_TZ).replace(tzinfo=None)


def ts_from_cn(dt) -> float:
    """北京时间（naive 墙钟，库内值由此反解）→ unix 时间戳，与进程时区无关"""
    return dt.replace(tzinfo=CN_TZ).timestamp()


def fmt_cn(dt) -> str:
    """北京时间 → 'YYYY-MM-DD HH:MM:SS'；空值返回 None"""
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None
