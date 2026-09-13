"""
模块名称: api/agent_monitor.py
说明:    QMT Agent 心跳与监控 — 心跳上报 / 状态列表 / 移除注册 / 最近上传查询。
         原 agent_routes.py 按资源拆分之一。
         时间口径：全部统一为北京时间（utils/timeutil，naive 墙钟），与进程/
         容器时区解耦——部署容器默认 UTC 时不再出现 8 小时偏差。
"""
import logging
import time

from flask import Blueprint, jsonify, request

from ..dbdata.database import db
from ..dbdata.models import AgentStatus
from ..messaging.cache import get_cache
from ..utils.timeutil import fmt_cn, from_ts_cn, ts_from_cn

logger = logging.getLogger(__name__)

monitor_bp = Blueprint("agent_monitor", __name__, url_prefix="/api/v1/agent")


@monitor_bp.route("/heartbeat", methods=["POST"])
def heartbeat():
    """心跳上报 — 更新 agent_status（含角色）+ Redis 时间戳

    body: {agent_name, timestamp?, role?}
    role 为 Agent 自报角色/职责（如 行情信息维护/维护·工具查询），随心跳维护，
    供监控面板展示；幂等同步，值一致时不触发推送。
    """
    data = request.get_json(silent=True) or {}
    agent_name = str(data.get("agent_name") or "").strip()
    if not agent_name:
        return jsonify({"code": 400, "message": "缺少 agent_name"}), 400
    role = str(data.get("role") or "").strip()[:50]
    ts = _parse_ts(data.get("timestamp"))

    get_cache().set_heartbeat(agent_name, ts)
    status, notify = _sync_agent_status(agent_name, role, ts)
    if notify:
        from ..engine.game_engine import get_engine
        get_engine().emit("agent:status", agent_payload(status))
    return jsonify({"code": 0, "message": "ok"})


def _parse_ts(raw) -> float:
    """时间戳参数解析：缺失/非法回退当前时间"""
    ts = raw or time.time()
    try:
        return float(ts)
    except (TypeError, ValueError):
        return time.time()


def _sync_agent_status(agent_name: str, role: str, ts: float):
    """同步 agent_status 行；返回 (status, 是否需要 socket 广播)

    首次上线 / 离线后恢复 / 角色变更 → 广播（页面实时刷新）。
    """
    status = AgentStatus.query.filter_by(agent_name=agent_name).first()
    first_seen = status is None
    role_changed = bool(role) and role != ((status.role or "") if status else "")
    notify = first_seen or (not first_seen and not status.is_alive) or role_changed
    if first_seen:
        status = AgentStatus(agent_name=agent_name)
        db.session.add(status)
    if role:
        status.role = role
    status.last_heartbeat_at = from_ts_cn(ts)
    status.is_alive = True
    db.session.commit()
    return status, notify


def agent_payload(r) -> dict:
    """AgentStatus → 页面推送/查询用字典（状态字段统一出口；时间为北京时间）"""
    return {
        "agent_name": r.agent_name,
        "role": r.role or "",
        "is_alive": bool(r.is_alive),
        "last_heartbeat_at": fmt_cn(r.last_heartbeat_at),
        "last_tick_at": fmt_cn(r.last_tick_at),
    }


@monitor_bp.route("/status", methods=["GET"])
def agent_status():
    """心跳状态查询（多 Agent 监控面板数据源）

    返回按"离线优先"排序的列表；每个 Agent 附角色、在线状态、最近心跳
    （绝对时间 + 距今秒数 age_sec）、最近行情时间、是否有上传记录。
    """
    rows = AgentStatus.query.order_by(AgentStatus.agent_name).all()
    cache = get_cache()
    now = time.time()
    result = [_status_item(r, cache, now) for r in rows]
    # 离线优先（便于第一时间发现问题），其次按名称稳定排序
    result.sort(key=lambda x: (x["is_alive"], x["agent_name"]))
    return jsonify({"code": 0, "data": result})


def _status_item(r, cache, now: float) -> dict:
    """单个 Agent 的监控面板条目"""
    ts = _heartbeat_ts(r, cache)
    return {
        **agent_payload(r),
        "redis_heartbeat_ts": ts if ts > 0 else 0,
        "age_sec": round(now - ts, 1) if ts > 0 else None,
        "has_latest_upload": cache.has_latest_upload(r.agent_name),
    }


def _heartbeat_ts(r, cache) -> float:
    """最后心跳时间戳：Redis 优先；库内为北京时间墙钟 → 显式按东八区反解
    时间戳（与进程时区无关）"""
    ts = cache.get_heartbeat(r.agent_name)
    if ts <= 0 and r.last_heartbeat_at:
        ts = ts_from_cn(r.last_heartbeat_at)
    return ts


@monitor_bp.route("/status/<agent_name>", methods=["DELETE"])
def delete_agent(agent_name):
    """移除 Agent 注册记录（仅离线可删，防止误删在跑脚本）

    同时清理该 Agent 的 Redis 心跳、告警防抖标记与最近上传记录；
    若脚本仍在运行，下一次心跳会自动重新注册。
    """
    status = AgentStatus.query.filter_by(agent_name=agent_name).first()
    if not status:
        return jsonify({"code": 404, "message": "Agent 不存在"}), 404
    if status.is_alive:
        return jsonify({"code": 400,
                        "message": "Agent 在线，无法移除（请先停止对应脚本）"}), 400
    db.session.delete(status)
    db.session.commit()
    _cleanup_agent_cache(agent_name)
    from ..engine.game_engine import get_engine
    get_engine().emit("agent:removed", {"agent_name": agent_name})
    logger.info("移除 Agent 记录: %s", agent_name)
    return jsonify({"code": 0, "message": "已移除"})


def _cleanup_agent_cache(agent_name: str):
    """清理该 Agent 的 Redis 心跳/告警/上传记录；全局"最近一条上传"若属于
    该 Agent 一并清理（避免面板外查询残留）"""
    cache = get_cache()
    cache.delete_heartbeat(agent_name)
    cache.clear_alert(agent_name)
    cache.delete_latest_upload(agent_name)
    if (cache.load_latest_upload() or {}).get("agent_name") == agent_name:
        cache.delete_latest_upload()


@monitor_bp.route("/latest", methods=["GET"])
def latest_upload():
    """查询最近上传记录（Redis，25 小时过期；无则 data 为 None）

    参数: agent（可选）指定 Agent → 该 Agent 最近一条；缺省为全局最近一条。
    """
    agent_name = str(request.args.get("agent") or "").strip() or None
    record = get_cache().load_latest_upload(agent_name)
    return jsonify({"code": 0, "data": record})
