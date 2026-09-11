"""
模块名称: api/agent_routes.py
说明:    QMT Agent 接入接口 — 行情上传（幂等）/ 心跳 / 状态查询；
        交易记录闭环 — 页面下发命令/每日自动采集 → Agent 轮询领取 → 执行上报
        → 落库 PostgreSQL（trade_records 整日替换）+ FIFO 配对分析；
        导入通道 — QMT 客户端导出成交文本解析入库（补齐历史日期）
"""
import logging
import re
import time

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from ..dbdata.database import db
from ..dbdata.models import AgentStatus, TickData
from ..engine import trade_store
from ..engine.trade_analysis import analyze_trades
from ..engine.trade_collector import issue_fetch_command
from ..engine.trade_import import parse_export_text
from ..messaging.cache import TRADE_CMD_TTL_SEC, get_cache
from ..utils.config import Config
from ..utils.timeutil import (fmt_cn, from_ts_cn, now_cn, now_str_cn,
                              ts_from_cn)

logger = logging.getLogger(__name__)

# 时间口径：全部统一为北京时间（utils/timeutil，naive 墙钟），与进程/容器
# 时区解耦——部署容器默认 UTC 时不再出现 8 小时偏差（心跳/上传/轮询时间）。

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

agent_bp = Blueprint("agent", __name__, url_prefix="/api/v1/agent")

# SQL 兼容 upsert（PG 9.5+）；今开 open / 昨收 last_close 为当日常量不再入库
# tick 表（快照字段最小化），由下方 refresh_day 作为 hint 维护到 game_days
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


@agent_bp.route("/tick", methods=["POST"])
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
                "high": data.get("high", 0),
                "low": data.get("low", 0),
                "close": data.get("close", 0),
                "volume": data.get("volume", 0),
                "amount": data.get("amount", 0),
                "created_at": now_cn(),
            },
        )
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error("tick 入库失败: %s", e)
        return jsonify({"code": 500, "message": f"tick 入库失败: {e}"}), 500

    # 同步维护天维度真实行情（昨收/今开 hint 随上传携带，缺失时引擎按链兑底）；
    # game_days 生成与 tick 入库解耦：昨收暂缺仅暂缓 day 侧，tick 入库不阻断
    from ..engine.game_engine import get_engine
    engine = get_engine()
    day_ok = engine.refresh_day(code, trade_date, "qmt",
                                last_close_hint=data.get("last_close", 0),
                                open_hint=data.get("open", 0))

    # 更新 agent_status.last_tick_at（无论 day 是否暂缓，agent 活跃状态照常维护）
    _touch_agent(agent_name, tick=True)

    # 更新 Redis 最新行情快照（全局 live）+ 最新一条上传记录；昨收取 game_days 维护值
    cache = get_cache()
    last_close_val = engine.day_last_close(code, trade_date, "qmt")
    cache.save_quote("live", {
        "code": code, "time_key": time_key,
        "close": data.get("close", 0),
        "last_close": last_close_val,
    })
    # 最新一条上传记录（QMT 在线时点击页面徽标查看），25 小时过期
    cache.save_latest_upload({
        "agent_name": agent_name,
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

    if not day_ok:
        # tick 已真正入库；game_days 因昨收缺失暂缓生成（不写脏数
        # 据），后续携带有效 last_close 的 tick 上传会自动补齐（refresh_day
        # 幂等，按 tick 表现存数据全量对账），亦可用 refresh_all_days 重建
        return jsonify({"code": 0, "message": "tick 已入库；game_days 暂缓生成"
                        "（昨收缺失），后续携带有效 last_close 将自动补齐"})

    return jsonify({"code": 0, "message": "ok"})


@agent_bp.route("/heartbeat", methods=["POST"])
def heartbeat():
    """心跳上报 — 更新 agent_status（含角色）+ Redis 时间戳

    body: {agent_name, timestamp?, role?}
    role 为 Agent 自报角色/职责（如 行情采集/交易记录），随心跳维护，
    供监控面板展示；幂等同步，值一致时不触发推送。
    """
    data = request.get_json(silent=True) or {}
    agent_name = str(data.get("agent_name") or "").strip()
    if not agent_name:
        return jsonify({"code": 400, "message": "缺少 agent_name"}), 400
    role = str(data.get("role") or "").strip()[:50]

    ts = data.get("timestamp") or time.time()
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        ts = time.time()

    cache = get_cache()
    cache.set_heartbeat(agent_name, ts)

    status = AgentStatus.query.filter_by(agent_name=agent_name).first()
    # 首次上线 / 离线后恢复 / 角色变更 → socket 广播（页面实时刷新）
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

    if notify:
        from ..engine.game_engine import get_engine
        get_engine().emit("agent:status", _agent_payload(status))

    return jsonify({"code": 0, "message": "ok"})


def _agent_payload(r) -> dict:
    """AgentStatus → 页面推送/查询用字典（状态字段统一出口；时间为北京时间）"""
    return {
        "agent_name": r.agent_name,
        "role": r.role or "",
        "is_alive": bool(r.is_alive),
        "last_heartbeat_at": fmt_cn(r.last_heartbeat_at),
        "last_tick_at": fmt_cn(r.last_tick_at),
    }


@agent_bp.route("/status", methods=["GET"])
def agent_status():
    """心跳状态查询（多 Agent 监控面板数据源）

    返回按"离线优先"排序的列表；每个 Agent 附角色、在线状态、最近心跳
    （绝对时间 + 距今秒数 age_sec）、最近行情时间、是否有上传记录。
    """
    rows = AgentStatus.query.order_by(AgentStatus.agent_name).all()
    cache = get_cache()
    now = time.time()
    result = []
    for r in rows:
        ts = cache.get_heartbeat(r.agent_name)
        if ts <= 0 and r.last_heartbeat_at:
            # 库内为北京时间墙钟 → 显式按东八区反解时间戳（与进程时区无关）
            ts = ts_from_cn(r.last_heartbeat_at)
        result.append({
            **_agent_payload(r),
            "redis_heartbeat_ts": ts if ts > 0 else 0,
            "age_sec": round(now - ts, 1) if ts > 0 else None,
            "has_latest_upload": cache.has_latest_upload(r.agent_name),
        })
    # 离线优先（便于第一时间发现问题），其次按名称稳定排序
    result.sort(key=lambda x: (x["is_alive"], x["agent_name"]))
    return jsonify({"code": 0, "data": result})


@agent_bp.route("/status/<agent_name>", methods=["DELETE"])
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
    cache = get_cache()
    cache.delete_heartbeat(agent_name)
    cache.clear_alert(agent_name)
    cache.delete_latest_upload(agent_name)
    # 全局"最近一条上传"若属于该 Agent，一并清理（避免面板外查询残留）
    if (cache.load_latest_upload() or {}).get("agent_name") == agent_name:
        cache.delete_latest_upload()
    from ..engine.game_engine import get_engine
    get_engine().emit("agent:removed", {"agent_name": agent_name})
    logger.info("移除 Agent 记录: %s", agent_name)
    return jsonify({"code": 0, "message": "已移除"})


@agent_bp.route("/latest", methods=["GET"])
def latest_upload():
    """查询最近上传记录（Redis，25 小时过期；无则 data 为 None）

    参数: agent（可选）指定 Agent → 该 Agent 最近一条；缺省为全局最近一条。
    """
    agent_name = str(request.args.get("agent") or "").strip() or None
    record = get_cache().load_latest_upload(agent_name)
    return jsonify({"code": 0, "data": record})


# ── 交易记录拉取（页面 ⇄ Redis 命令 ⇄ QMT Agent） ──

@agent_bp.route("/trade_fetch", methods=["POST"])
def create_trade_fetch():
    """页面：下发"获取当日成交明细"命令（仅当日）

    body: {date: 'YYYY-MM-DD'}；命令直写 Redis 单键（TTL 2 分钟，
    到期自动失效），重复下发直接覆盖旧命令，返回新命令
    {cmd_id, type, date, created_at, ts}。
    历史日期（date < 今天）不支持命令采集：QMT 仅能查询当日成交，
    历史数据读取自数据库，补齐请走导入通道 /trade_records/import。
    """
    body = request.get_json(silent=True) or {}
    date = str(body.get("date") or "").strip()
    if not _DATE_RE.match(date):
        return jsonify({"code": 400, "message": "date 格式应为 YYYY-MM-DD"}), 400
    # 历史日期不走命令采集（QMT 仅能查询当日成交）：数据读取自数据库，
    # 补齐历史请走导入通道 /trade_records/import
    if date < now_str_cn()[:10]:
        return jsonify({"code": 400,
                        "message": "历史日期不支持命令采集（QMT 仅能查询当日成交）；"
                                   "数据读取自数据库，可用「导入历史」补齐"}), 400
    cache = get_cache()
    if not cache.available:
        return jsonify({"code": 500, "message": "Redis 不可用，无法下发命令"}), 500
    cmd = issue_fetch_command(date)
    logger.info("下发交易记录拉取命令 date=%s cmd_id=%s", date, cmd["cmd_id"])
    return jsonify({"code": 0, "message": "命令已下发（2 分钟内有效），等待 QMT 执行",
                    "data": cmd})


@agent_bp.route("/trade_fetch", methods=["GET"])
def query_trade_fetch():
    """页面轮询：当前命令（TTL 2 分钟内有效）+ 该日交易记录结果（DB）"""
    date = str(request.args.get("date") or "").strip()
    if not _DATE_RE.match(date):
        return jsonify({"code": 400, "message": "date 格式应为 YYYY-MM-DD"}), 400
    cache = get_cache()
    cmd = cache.load_trade_fetch_cmd()
    if cmd and cmd.get("date") != date:
        cmd = None      # 当前命令非所选日期时不展示进度
    return jsonify({"code": 0, "data": {"command": cmd,
                                         "command_ttl_sec": TRADE_CMD_TTL_SEC,
                                         "result": _build_day_result(date)}})


def _build_day_result(date):
    """组装某日结果 — DB 记录 + 实时 FIFO 分析；无记录时回退失败状态

    success 且 0 条 = 当日确认无成交（采集成功记录），返回空列表结果，
    与"从未采集/采集失败"（失败时附错误原因）区分。
    """
    records = trade_store.load_day(date)
    status = trade_store.get_day_status(date) or {}
    if records or status.get("status") == "success":
        analysis = analyze_trades(records)
        return {
            "date": date, "success": True,
            "source": status.get("source", ""),
            "cmd_id": status.get("cmd_id", ""),
            "fetched_at": status.get("fetched_at") or "",
            "count": analysis["summary"]["count"],
            "trades": analysis["trades"],
            "pairs": analysis["pairs"],
            "unmatched": analysis["unmatched"],
            "summary": analysis["summary"],
        }
    if status.get("status") == "failed":
        # 历史日期的旧失败状态不展示（命令采集仅当日有效；历史靠导入补齐），
        # 避免旧命令失败残影遮蔽「导入历史」引导文案
        if date < now_str_cn()[:10]:
            return None
        return {
            "date": date, "success": False,
            "source": status.get("source", ""),
            "cmd_id": status.get("cmd_id", ""),
            "fetched_at": status.get("fetched_at") or "",
            "error": status.get("error") or "QMT 执行失败",
            "count": 0,
        }
    return None


@agent_bp.route("/command", methods=["GET"])
def poll_command():
    """QMT Agent 10s 轮询：直读 Redis 当前命令（无则 None）

    命令为单命令槽（Redis TTL 2 分钟，到期自动消失）；Agent 侧对同一
    cmd_id 去重执行，结果经 /trade_records 上报后由服务器删除。
    data: {cmd_id, type, date, created_at, ts}；无命令为 None。
    """
    cmd = get_cache().load_trade_fetch_cmd()
    return jsonify({"code": 0, "data": cmd})


@agent_bp.route("/trade_records", methods=["POST"])
def upload_trade_records():
    """QMT Agent 上报某日成交明细 — 删除命令 + 整日替换落库

    body: {agent_name, cmd_id, date, success, error?, account?, records:[...]}
    成功时整日替换写入 DB（source=agent，天然幂等）；失败标记状态并保留
    已有记录（页面展示失败原因）。cmd_id 与当前命令匹配时才删除，防止
    误删上报期间覆盖的新命令。
    """
    data = request.get_json(silent=True) or {}
    date = str(data.get("date") or "").strip()
    cmd_id = str(data.get("cmd_id") or "").strip()
    if not _DATE_RE.match(date):
        return jsonify({"code": 400, "message": "date 格式应为 YYYY-MM-DD"}), 400

    success = bool(data.get("success", True))
    if success:
        count = trade_store.replace_day(date, data.get("records") or [],
                                        source="agent", cmd_id=cmd_id)
        msg = "ok"
    else:
        trade_store.mark_failed(date,
                                str(data.get("error") or "QMT 执行失败"),
                                cmd_id=cmd_id)
        count = 0
        msg = "已记录失败原因"
    get_cache().delete_trade_fetch_cmd(cmd_id)
    logger.info("交易记录上报 date=%s success=%s count=%s", date, success, count)
    return jsonify({"code": 0, "message": msg, "count": count})


@agent_bp.route("/trade_records/import", methods=["POST"])
def import_trade_records():
    """页面导入：QMT 客户端导出的成交文本（补齐历史日期）

    body: {date: 'YYYY-MM-DD', text: '客户端表格复制/CSV 导出的文本'}
    解析为与 Agent 上报同构的记录后，整日替换落库（source=import）。
    返回解析统计（导入笔数、跳过行及原因），供页面提示。
    """
    body = request.get_json(silent=True) or {}
    date = str(body.get("date") or "").strip()
    if not _DATE_RE.match(date):
        return jsonify({"code": 400, "message": "date 格式应为 YYYY-MM-DD"}), 400
    text = str(body.get("text") or "")
    if not text.strip():
        return jsonify({"code": 400, "message": "导入文本为空"}), 400
    try:
        records, meta = parse_export_text(text, date)
    except ValueError as e:
        return jsonify({"code": 400, "message": str(e)}), 400
    if not records:
        msg = "未解析到有效成交记录"
        if meta["skipped"]:
            msg += "（跳过 %d 行，如：%s）" % (
                len(meta["skipped"]), meta["skipped"][0]["reason"])
        return jsonify({"code": 400, "message": msg, "data": meta}), 400
    count = trade_store.replace_day(date, records, source="import")
    logger.info("成交记录导入 date=%s 解析 %d 笔（跳过 %d 行）",
                date, count, len(meta["skipped"]))
    return jsonify({"code": 0,
                    "message": "导入成功：%d 笔（跳过 %d 行）"
                               % (count, len(meta["skipped"])),
                    "count": count, "data": meta})


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
