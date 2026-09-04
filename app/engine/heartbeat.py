"""
模块名称: engine/heartbeat.py
说明:    心跳维护 — 按配置周期检查（默认 15s），离线则飞书告警（防抖 30 分钟），
         恢复发送恢复通知；状态变化经 socket 广播，主页面实时展示 agent 在线状态
"""
import logging
import time
from datetime import datetime

from ..utils.config import Config
from ..utils import feishu
from ..messaging.cache import get_cache

logger = logging.getLogger(__name__)

# 防止重复注册
_checker_started = False


def _fmt(dt):
    """datetime → 展示字符串，空值返回 None"""
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _notify_status(agent_name, is_alive, last_heartbeat_at, last_tick_at):
    """socket 广播 agent 状态变化（主页面实时刷新 Agent 徽标）"""
    try:
        from ..engine.game_engine import get_engine
        get_engine().emit("agent:status", {
            "agent_name": agent_name,
            "is_alive": is_alive,
            "last_heartbeat_at": last_heartbeat_at,
            "last_tick_at": last_tick_at,
        })
    except Exception as e:
        logger.warning("agent 状态推送失败 %s: %s", agent_name, e)


def check_heartbeats(app):
    """心跳检查任务（APScheduler 按 heartbeat.check_interval_sec 周期调用）

    1. 读取 Redis 心跳时间，now - last > timeout_sec 视为离线
    2. 离线 → 飞书卡片告警（防抖：Redis 有 alert 标记则跳过）
    3. Redis 不可用则查 agent_status 表兜底
    4. 恢复在线 → 发送恢复通知并清防抖 key
    5. 状态变化（离线/恢复）→ socket 广播 agent:status（页面实时展示）
    """
    cfg = Config.get_instance()
    timeout_sec = cfg.get("heartbeat.timeout_sec", 180)
    alert_cooldown = cfg.get("heartbeat.alert_cooldown_sec", 1800)
    cache = get_cache()

    with app.app_context():
        from ..dbdata.models import AgentStatus
        from ..dbdata.database import db

        try:
            agents = AgentStatus.query.all()
        except Exception as e:
            logger.warning("心跳检查: 查询 agent_status 失败: %s", e)
            return

        now = time.time()
        for agent in agents:
            name = agent.agent_name
            # 优先 Redis（实时），Redis 不可用或没有值则用数据库
            last_ts = cache.get_heartbeat(name)
            last_str = None
            if last_ts <= 0:
                if agent.last_heartbeat_at:
                    last_ts = agent.last_heartbeat_at.timestamp()
                    last_str = agent.last_heartbeat_at.strftime("%Y-%m-%d %H:%M:%S")
            else:
                last_str = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M:%S")

            offline = last_ts <= 0 or (now - last_ts) > timeout_sec

            if offline:
                if not cache.has_alert(name):
                    logger.warning("心跳离线: %s last=%s", name, last_str or "无记录")
                    resp = feishu.send_heartbeat_alert(
                        name, last_str or "无记录",
                        reason=f"超过 {timeout_sec}s 未收到心跳")
                    logger.info("飞书告警发送结果: %s", resp)
                    cache.set_alert(name, ttl=alert_cooldown)
                # 同步数据库状态
                if agent.is_alive:
                    agent.is_alive = False
                    db.session.commit()
                    _notify_status(name, False, last_str,
                                   _fmt(agent.last_tick_at))
            else:
                if cache.has_alert(name):
                    logger.info("心跳恢复: %s", name)
                    feishu.send_heartbeat_recover(name)
                    cache.clear_alert(name)
                if not agent.is_alive:
                    agent.is_alive = True
                    db.session.commit()
                    _notify_status(name, True, last_str,
                                   _fmt(agent.last_tick_at))


def register_heartbeat_checker(scheduler, app):
    """注册心跳检查任务（幂等，防止重复注册）"""
    global _checker_started
    if _checker_started:
        return
    cfg = Config.get_instance()
    interval = cfg.get("heartbeat.check_interval_sec", 300)
    scheduler.add_job(
        id="heartbeat_checker",
        func=check_heartbeats,
        trigger="interval",
        seconds=interval,
        args=[app],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _checker_started = True
    logger.info("心跳检查任务已注册 (interval=%ss)", interval)
