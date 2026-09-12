"""
模块名称: api/agent_tick.py
说明:    QMT Agent 行情上传接口 — 当日快照 tick 幂等入库 + game_days 天维度
         同步维护 + Redis 实时快照/最新上传记录。
         原 agent_routes.py 按资源拆分之一。
"""
import logging

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from ..dbdata.database import db
from ..dbdata.models import AgentStatus
from ..messaging.cache import get_cache
from ..utils.timeutil import now_cn, now_str_cn

logger = logging.getLogger(__name__)

tick_bp = Blueprint("agent_tick", __name__, url_prefix="/api/v1/agent")

# SQL 兼容 upsert（PG 9.5+）；今开 open / 昨收 last_close 为当日常量不再入库
# tick 表（快照字段最小化），由引擎 refresh_day 作为 hint 维护到 game_days
# （天维度行情 + 日期管理唯一权威表）
_UPSERT_TICK_SQL = text("""
INSERT INTO tick_data (code, trade_date, time_key, high, low, close, volume, amount, created_at)
VALUES (:code, :trade_date, :time_key, :high, :low, :close, :volume, :amount, :created_at)
ON CONFLICT (code, time_key) DO UPDATE SET
  trade_date = EXCLUDED.trade_date,
  high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,
  volume = EXCLUDED.volume, amount = EXCLUDED.amount,
  created_at = EXCLUDED.created_at
""")


@tick_bp.route("/tick", methods=["POST"])
def upload_tick():
    """当日快照行情上传 — 每个 (code, time_key) 仅一条数据（幂等 upsert）

    body: {agent_name, code, time_key, high, low, close, volume, amount,
           last_close?, open?}；字段为快照语义（close=最新价、high/low=当日
    滚动极值、volume/amount=当日累计量额）；last_close/open 为当日常量，仅作为
    hint 维护到 game_days（tick 表不存该字段）。
    """
    data = request.get_json(silent=True) or {}
    code = data.get("code", "")
    time_key = data.get("time_key", "")
    if not code or not time_key:
        return jsonify({"code": 400, "message": "缺少 code 或 time_key"}), 400

    trade_date = data.get("trade_date") or time_key[:10]
    if not _store_tick(code, trade_date, time_key, data):
        return jsonify({"code": 500, "message": "tick 入库失败（见服务端日志）"}), 500

    # 同步维护天维度真实行情 + Redis 实时快照/最新上传记录
    from ..engine.game_engine import get_engine
    engine = get_engine()
    day_ok = engine.refresh_day(code, trade_date, "qmt",
                                last_close_hint=data.get("last_close", 0),
                                open_hint=data.get("open", 0))
    last_close_val = engine.day_last_close(code, trade_date, "qmt")

    _touch_agent(data.get("agent_name", "unknown"), tick=True)
    _sync_live_cache(engine, data, code, trade_date, time_key, last_close_val)

    if not day_ok:
        # tick 已真正入库；game_days 因昨收缺失暂缓生成（不写脏数据），后续
        # 携带有效 last_close 的 tick 上传会自动补齐（refresh_day 幂等）
        return jsonify({"code": 0, "message": "tick 已入库；game_days 暂缓生成"
                        "（昨收缺失），后续携带有效 last_close 将自动补齐"})
    return jsonify({"code": 0, "message": "ok"})


def _store_tick(code: str, trade_date: str, time_key: str, data: dict) -> bool:
    """tick 幂等 upsert 入库，异常回滚并返回 False"""
    try:
        db.session.execute(_UPSERT_TICK_SQL, {
            "code": code,
            "trade_date": trade_date,
            "time_key": time_key,
            "high": data.get("high", 0),
            "low": data.get("low", 0),
            "close": data.get("close", 0),
            "volume": data.get("volume", 0),
            "amount": data.get("amount", 0),
            "created_at": now_cn(),
        })
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        logger.error("tick 入库失败: %s", e)
        return False


def _sync_live_cache(engine, data: dict, code: str, trade_date: str,
                     time_key: str, last_close_val: float):
    """更新 Redis 实时行情快照（全局 live）+ 最新一条上传记录（25 小时过期）"""
    cache = get_cache()
    cache.save_quote("live", {
        "code": code, "time_key": time_key,
        "close": data.get("close", 0),
        "last_close": last_close_val,
    })
    cache.save_latest_upload({
        "agent_name": data.get("agent_name", "unknown"),
        "code": code,
        "trade_date": trade_date,
        "time_key": time_key,
        "open": data.get("open", 0),
        "high": data.get("high", 0),
        "low": data.get("low", 0),
        "close": data.get("close", 0),
        "volume": data.get("volume", 0),
        "amount": data.get("amount", 0),
        "last_close": last_close_val,
        # 上传时间按北京时间格式化，与行情 time_key 保持一致时区
        "created_at": now_str_cn(),
    })


def _touch_agent(agent_name: str, tick: bool = False):
    """更新 agent 状态（幂等创建）"""
    try:
        status = AgentStatus.query.filter_by(agent_name=agent_name).first()
        if status is None:
            status = AgentStatus(agent_name=agent_name)
            db.session.add(status)
        if tick:
            status.last_tick_at = now_cn()
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning("更新 agent 状态失败: %s", e)
