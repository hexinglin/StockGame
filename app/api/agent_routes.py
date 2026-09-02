"""
模块名称: api/agent_routes.py
说明:    QMT Agent 接入接口 — 行情上传（幂等）/ 心跳 / 状态查询
"""
import logging
import time
from datetime import datetime

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from ..dbdata.database import db
from ..dbdata.models import AgentStatus, TickData
from ..messaging.cache import get_cache
from ..utils.config import Config

logger = logging.getLogger(__name__)

agent_bp = Blueprint("agent", __name__, url_prefix="/api/v1/agent")

# SQL 兼容 upsert（PG 9.5+）
_UPSERT_TICK_SQL = text("""
INSERT INTO tick_data (code, trade_date, time_key, open, high, low, close, volume, amount, last_close, created_at)
VALUES (:code, :trade_date, :time_key, :open, :high, :low, :close, :volume, :amount, :last_close, :created_at)
ON CONFLICT (code, time_key) DO UPDATE SET
  trade_date = EXCLUDED.trade_date,
  open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,
  volume = EXCLUDED.volume, amount = EXCLUDED.amount, last_close = EXCLUDED.last_close,
  created_at = EXCLUDED.created_at
""")


@agent_bp.route("/tick", methods=["POST"])
def upload_tick():
    """原始行情上传 — 每个 (code, time_key) 仅一条数据（幂等 upsert）

    body: {agent_name, code, time_key, open, high, low, close, volume, amount, last_close}
    """
    data = request.get_json(silent=True) or {}
    code = data.get("code", "")
    time_key = data.get("time_key", "")
    agent_name = data.get("agent_name", "unknown")

    if not code or not time_key:
        return jsonify({"code": 400, "message": "缺少 code 或 time_key"}), 400

    try:
        trade_date = data.get("trade_date") or time_key[:10]
        db.session.execute(
            _UPSERT_TICK_SQL,
            {
                "code": code,
                "trade_date": trade_date,
                "time_key": time_key,
                "open": data.get("open", 0),
                "high": data.get("high", 0),
                "low": data.get("low", 0),
                "close": data.get("close", 0),
                "volume": data.get("volume", 0),
                "amount": data.get("amount", 0),
                "last_close": data.get("last_close", 0),
                "created_at": datetime.now(),
            },
        )
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error("tick 入库失败: %s", e)
        return jsonify({"code": 500, "message": f"tick 入库失败: {e}"}), 500

    # 更新 agent_status.last_tick_at
    _touch_agent(agent_name, tick=True)

    # 更新 Redis 最新行情快照（全局 live）
    cache = get_cache()
    cache.save_quote("live", {
        "code": code, "time_key": time_key,
        "close": data.get("close", 0), "last_close": data.get("last_close", 0),
    })

    return jsonify({"code": 0, "message": "ok"})


@agent_bp.route("/heartbeat", methods=["POST"])
def heartbeat():
    """心跳上报 — 更新 agent_status + Redis 时间戳

    body: {agent_name, timestamp?}
    """
    data = request.get_json(silent=True) or {}
    agent_name = data.get("agent_name", "")
    if not agent_name:
        return jsonify({"code": 400, "message": "缺少 agent_name"}), 400

    ts = data.get("timestamp") or time.time()
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        ts = time.time()

    cache = get_cache()
    cache.set_heartbeat(agent_name, ts)

    status = AgentStatus.query.filter_by(agent_name=agent_name).first()
    if status is None:
        status = AgentStatus(agent_name=agent_name)
        db.session.add(status)
    status.last_heartbeat_at = datetime.fromtimestamp(ts)
    status.is_alive = True
    db.session.commit()

    return jsonify({"code": 0, "message": "ok"})


@agent_bp.route("/status", methods=["GET"])
def agent_status():
    """心跳状态查询（供页面展示）"""
    rows = AgentStatus.query.order_by(AgentStatus.agent_name).all()
    cache = get_cache()
    result = []
    for r in rows:
        result.append({
            "agent_name": r.agent_name,
            "last_heartbeat_at": r.last_heartbeat_at.strftime("%Y-%m-%d %H:%M:%S") if r.last_heartbeat_at else None,
            "last_tick_at": r.last_tick_at.strftime("%Y-%m-%d %H:%M:%S") if r.last_tick_at else None,
            "is_alive": r.is_alive,
            "redis_heartbeat_ts": cache.get_heartbeat(r.agent_name),
        })
    return jsonify({"code": 0, "data": result})


def _touch_agent(agent_name: str, tick: bool = False):
    """更新 agent 状态（幂等创建）"""
    try:
        status = AgentStatus.query.filter_by(agent_name=agent_name).first()
        if status is None:
            status = AgentStatus(agent_name=agent_name)
            db.session.add(status)
        if tick:
            status.last_tick_at = datetime.now()
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning("更新 agent 状态失败: %s", e)
