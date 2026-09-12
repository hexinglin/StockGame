"""
模块名称: api/agent_trade.py
说明:    QMT 交易记录闭环 — 页面下发命令/每日自动采集 → Agent 轮询领取 →
         执行上报 → 落库 PostgreSQL（trade_records 整日替换）+ FIFO 配对分析；
         导入通道 — QMT 客户端导出成交文本解析入库（补齐历史日期）。
         原 agent_routes.py 按资源拆分之一。
"""
import logging
import re

from flask import Blueprint, jsonify, request

from ..engine import trade_store
from ..engine.trade_analysis import analyze_trades
from ..engine.trade_collector import issue_fetch_command
from ..engine.trade_import import parse_export_text
from ..messaging.cache import TRADE_CMD_TTL_SEC, get_cache
from ..utils.timeutil import now_str_cn

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

trade_bp = Blueprint("agent_trade", __name__, url_prefix="/api/v1/agent")


def _valid_date(date: str) -> str:
    """日期参数校验：空/格式错 → 提示信息；合法返回 ''"""
    if not _DATE_RE.match(date or ""):
        return "date 格式应为 YYYY-MM-DD"
    return ""


def _today() -> str:
    return now_str_cn()[:10]


# ── 命令下发 / 进度查询（页面侧） ──

@trade_bp.route("/trade_fetch", methods=["POST"])
def create_trade_fetch():
    """页面：下发"获取当日成交明细"命令（仅当日）

    body: {date: 'YYYY-MM-DD'}；命令直写 Redis 单键（TTL 2 分钟，到期自动
    失效），重复下发直接覆盖旧命令，返回新命令 {cmd_id, type, date, ...}。
    历史日期（date < 今天）不支持命令采集：QMT 仅能查询当日成交，历史数据
    读取自数据库，补齐请走导入通道 /trade_records/import。
    """
    body = request.get_json(silent=True) or {}
    date = str(body.get("date") or "").strip()
    if msg := _valid_date(date):
        return jsonify({"code": 400, "message": msg}), 400
    if date < _today():
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


@trade_bp.route("/trade_fetch", methods=["GET"])
def query_trade_fetch():
    """页面轮询：当前命令（TTL 2 分钟内有效）+ 该日交易记录结果（DB）"""
    date = str(request.args.get("date") or "").strip()
    if msg := _valid_date(date):
        return jsonify({"code": 400, "message": msg}), 400
    cmd = get_cache().load_trade_fetch_cmd()
    if cmd and cmd.get("date") != date:
        cmd = None      # 当前命令非所选日期时不展示进度
    return jsonify({"code": 0, "data": {"command": cmd,
                                         "command_ttl_sec": TRADE_CMD_TTL_SEC,
                                         "result": build_day_result(date)}})


def build_day_result(date):
    """组装某日结果 — DB 记录 + 实时 FIFO 分析；无记录时回退失败状态

    success 且 0 条 = 当日确认无成交（采集成功记录），返回空列表结果，
    与"从未采集/采集失败"（失败时附错误原因）区分。
    """
    records = trade_store.load_day(date)
    status = trade_store.get_day_status(date) or {}
    if records or status.get("status") == "success":
        return _success_result(date, records, status)
    if status.get("status") == "failed":
        # 历史日期的旧失败状态不展示（命令采集仅当日有效；历史靠导入补齐），
        # 避免旧命令失败残影遮蔽「导入历史」引导文案
        return None if date < _today() else _failed_result(date, status)
    return None


def _success_result(date, records, status) -> dict:
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


def _failed_result(date, status) -> dict:
    return {
        "date": date, "success": False,
        "source": status.get("source", ""),
        "cmd_id": status.get("cmd_id", ""),
        "fetched_at": status.get("fetched_at") or "",
        "error": status.get("error") or "QMT 执行失败",
        "count": 0,
    }


# ── Agent 侧轮询 / 上报 ──

@trade_bp.route("/command", methods=["GET"])
def poll_command():
    """QMT Agent 10s 轮询：直读 Redis 当前命令（无则 None）

    命令为单命令槽（Redis TTL 2 分钟，到期自动消失）；Agent 侧对同一
    cmd_id 去重执行，结果经 /trade_records 上报后由服务器删除。
    """
    cmd = get_cache().load_trade_fetch_cmd()
    return jsonify({"code": 0, "data": cmd})


@trade_bp.route("/trade_records", methods=["POST"])
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
    if msg := _valid_date(date):
        return jsonify({"code": 400, "message": msg}), 400

    if bool(data.get("success", True)):
        count = trade_store.replace_day(date, data.get("records") or [],
                                        source="agent", cmd_id=cmd_id)
        msg = "ok"
    else:
        trade_store.mark_failed(date,
                                str(data.get("error") or "QMT 执行失败"),
                                cmd_id=cmd_id)
        count, msg = 0, "已记录失败原因"
    get_cache().delete_trade_fetch_cmd(cmd_id)
    logger.info("交易记录上报 date=%s success=%s count=%s", date, bool(data.get("success", True)), count)
    return jsonify({"code": 0, "message": msg, "count": count})


# ── 导入通道（补齐历史日期） ──

@trade_bp.route("/trade_records/import", methods=["POST"])
def import_trade_records():
    """页面导入：QMT 客户端导出的成交文本（补齐历史日期）

    body: {date: 'YYYY-MM-DD', text: '客户端表格复制/CSV 导出的文本'}
    解析为与 Agent 上报同构的记录后，整日替换落库（source=import）。
    返回解析统计（导入笔数、跳过行及原因），供页面提示。
    """
    body = request.get_json(silent=True) or {}
    date = str(body.get("date") or "").strip()
    if msg := _valid_date(date):
        return jsonify({"code": 400, "message": msg}), 400
    text = str(body.get("text") or "")
    if not text.strip():
        return jsonify({"code": 400, "message": "导入文本为空"}), 400
    try:
        records, meta = parse_export_text(text, date)
    except ValueError as e:
        return jsonify({"code": 400, "message": str(e)}), 400
    if not records:
        return _empty_import_reply(meta)
    count = trade_store.replace_day(date, records, source="import")
    logger.info("成交记录导入 date=%s 解析 %d 笔（跳过 %d 行）",
                date, count, len(meta["skipped"]))
    return jsonify({"code": 0,
                    "message": "导入成功：%d 笔（跳过 %d 行）"
                               % (count, len(meta["skipped"])),
                    "count": count, "data": meta})


def _empty_import_reply(meta) -> tuple:
    """无有效记录的导入响应（附首条跳过原因供页面提示）"""
    msg = "未解析到有效成交记录"
    if meta["skipped"]:
        msg += "（跳过 %d 行，如：%s）" % (
            len(meta["skipped"]), meta["skipped"][0]["reason"])
    return jsonify({"code": 400, "message": msg, "data": meta}), 400
