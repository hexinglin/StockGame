"""
模块名称: engine/trade_collector.py
说明:    成交记录每日自动采集 — 收盘后自动下发"获取当日成交"命令

机制:
- 交易日（周一~周五）15:05 ~ 18:00（东八区）为采集/补采窗口；
- 每 5 分钟检查一次：当日采集状态非 success 且命令队列为空 → 下发当日命令
  （QMT agent 领取后走既有上报链路，数据落库 trade_records）；
- 成功后（trade_fetch_days.status=success）当日晚不再下发；
- 失败（agent 离线/执行报错）自动进入下一轮补采，直至窗口结束。
"""
import logging
import time
import uuid
from datetime import datetime

from ..messaging.cache import get_cache
from ..utils.timeutil import CN_TZ, now_str_cn

logger = logging.getLogger(__name__)

COLLECT_AGENT = "qmt_trade"      # 交易记录 Agent 名称（qmt_agent/trade_records_agent.py）
WINDOW_START = (15, 5)           # 补采窗口开始（收盘后，时:分）
WINDOW_END = (18, 0)             # 补采窗口结束
CHECK_INTERVAL_SEC = 300         # 检查周期（秒）

_collector_started = False


def issue_fetch_command(date, agent_name=COLLECT_AGENT):
    """下发"获取某日成交"命令（与页面手动触发同一条链路：Redis 单键 TTL 2 分钟）"""
    cmd = {
        "cmd_id": uuid.uuid4().hex[:16],
        "type": "fetch_trade_records",
        "date": date,
        "agent_name": agent_name,
        "created_at": now_str_cn(),
        "ts": time.time(),
    }
    get_cache().save_trade_fetch_cmd(cmd)
    return cmd


def check_and_collect(app):
    """周期检查任务（APScheduler 调用）：窗口内为"当日未成功采集"补发命令"""
    now = datetime.now(CN_TZ)
    if now.weekday() >= 5:
        return                       # 周末不下发
    hm = (now.hour, now.minute)
    if not (WINDOW_START <= hm <= WINDOW_END):
        return

    with app.app_context():
        from . import trade_store
        today = now.strftime("%Y-%m-%d")
        status = trade_store.get_day_status(today)
        if status and status.get("status") == "success":
            return                   # 当日已成功采集，不再下发
        cmd = get_cache().load_trade_fetch_cmd()
        if cmd:
            return                   # 队列中已有待执行命令（含手动触发），下轮再试
        issued = issue_fetch_command(today)
        logger.info("每日自动采集：下发 %s 命令 cmd_id=%s", today, issued["cmd_id"])


def register_trade_collector(scheduler, app):
    """注册自动采集任务（幂等，防止重复注册）"""
    global _collector_started
    if _collector_started:
        return
    scheduler.add_job(
        id="trade_collector",
        func=check_and_collect,
        trigger="interval",
        seconds=CHECK_INTERVAL_SEC,
        args=[app],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _collector_started = True
    logger.info("成交每日自动采集任务已注册 (窗口 %02d:%02d~%02d:%02d, interval=%ss)",
                WINDOW_START[0], WINDOW_START[1],
                WINDOW_END[0], WINDOW_END[1], CHECK_INTERVAL_SEC)
