"""
模块名称: api/agent_calendar.py
说明:    交易日历通道 — QMT Agent 每日自动上报 + 页面/内部查询

- POST /api/v1/agent/trading_days: Agent 上报 get_trading_dates 结果
  （body: {agent_name, dates: ['20260101', ...]}），幂等 upsert 落库；
- GET  /api/v1/agent/trading_days: 查询 — date=单日判断（含前/后交易
  日）；start/end=区间列表（可只传其一）；无参=日历概览 stats。

原 agent_routes.py 按资源拆分之一；与成交采集命令通道（agent_trade.py）
相互独立，日历走 Agent 主动推送，不占用 Redis 命令槽。
"""
import logging
import re

from flask import Blueprint, jsonify, request

from ..engine import trading_calendar as cal

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

calendar_bp = Blueprint("agent_calendar", __name__, url_prefix="/api/v1/agent")


def _valid_date(date: str) -> str:
    """日期参数校验：空/格式错 → 提示信息；合法返回 ''"""
    if not _DATE_RE.match(date or ""):
        return "date 格式应为 YYYY-MM-DD"
    return ""


@calendar_bp.route("/trading_days", methods=["POST"])
def upload_trading_days():
    """QMT Agent 上报交易日历（每日自动；幂等，只增不改）"""
    data = request.get_json(silent=True) or {}
    dates = data.get("dates") or []
    if not isinstance(dates, list) or not dates:
        return jsonify({"code": 400, "message": "dates 为空或非列表"}), 400
    added = cal.upsert_days(dates, source="qmt")
    logger.info("交易日历上报 agent=%s 收到 %d 个日期，新增 %d",
                data.get("agent_name", ""), len(dates), added)
    return jsonify({"code": 0, "message": "ok", "count": added})


@calendar_bp.route("/trading_days", methods=["GET"])
def query_trading_days():
    """交易日查询 — date=单日判断；start/end=区间列表；无参=概览"""
    date = str(request.args.get("date") or "").strip()
    start = str(request.args.get("start") or "").strip()
    end = str(request.args.get("end") or "").strip()
    for name, v in (("date", date), ("start", start), ("end", end)):
        if v and (msg := _valid_date(v)):
            return jsonify({"code": 400, "message": "%s %s" % (name, msg)}), 400

    if date:
        return jsonify({"code": 0, "data": {
            "date": date,
            "is_trading_day": cal.is_trading_day(date),   # None=日历未覆盖
            "prev": cal.prev_trading_day(date),
            "next": cal.next_trading_day(date),
        }})
    if start or end:
        days = cal.list_days(start, end)
        return jsonify({"code": 0, "data": {"days": days, "count": len(days)}})
    return jsonify({"code": 0, "data": cal.stats()})
