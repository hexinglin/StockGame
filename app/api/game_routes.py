"""
模块名称: api/game_routes.py
说明:    轮次管理 + 游戏操作 API
"""
import logging

from flask import Blueprint, jsonify, request

from ..dbdata.database import db
from ..dbdata.models import GameRound, GameOrder
from ..engine.game_engine import get_engine

logger = logging.getLogger(__name__)

game_bp = Blueprint("game", __name__, url_prefix="/api/v1/game")


def _ok(data=None, message="ok"):
    return jsonify({"code": 0, "message": message, "data": data})


def _err(message, code=400):
    return jsonify({"code": code, "message": message}), code


# ── 交易日 ──

@game_bp.route("/dates", methods=["GET"])
def dates():
    """可用交易日列表（按 code 过滤，默认 588000.SH）

    参数:
      code       标的代码（缺省返回全部 code 的日期映射）
      allow_sim  1/0 是否允许转换模拟数据（QMT 无该日数据时的兜底）
      source     1/0 返回带来源标记的 [{trade_date, source}]（qmt/sim）
    """
    code = request.args.get("code", "") or None
    allow_sim = request.args.get("allow_sim", "0") in ("1", "true", "True")
    with_source = request.args.get("source", "0") in ("1", "true", "True")
    engine = get_engine()
    if code:
        result = (engine.date_sources(code, allow_sim) if with_source
                  else engine.available_dates(code, allow_sim))
    else:
        # 全部 code 的日期（实盘 + 转换模拟的 code 并集）
        from ..dbdata.models import TickData, TickDataSim
        codes = set()
        for model in (TickData, TickDataSim):
            for (c,) in db.session.query(model.code).distinct().all():
                codes.add(c)
        result = {}
        for c in sorted(codes):
            result[c] = (engine.date_sources(c, allow_sim) if with_source
                         else engine.available_dates(c, allow_sim))
    return _ok(result)


# ── 轮次 CRUD ──

@game_bp.route("/rounds", methods=["POST"])
def create_round():
    """创建轮次（body 可选 code/trade_date/allow_sim，缺省随机）"""
    body = request.get_json(silent=True) or {}
    code = body.get("code") or None
    trade_date = body.get("trade_date") or None
    allow_sim = bool(body.get("allow_sim", False))
    engine = get_engine()
    r, msg = engine.create_round(code=code, trade_date=trade_date, allow_sim=allow_sim)
    if not r:
        return _err(msg)
    return _ok({"id": r.id, "code": r.code, "trade_date": r.trade_date,
                "status": r.status, "data_source": r.data_source}, "创建成功")


@game_bp.route("/rounds", methods=["GET"])
def list_rounds():
    """轮次列表（状态/日期/收益/进度）"""
    return _ok(get_engine().list_rounds())


@game_bp.route("/rounds/<int:round_id>", methods=["GET"])
def round_detail(round_id):
    """轮次详情 + 账户 + 最新价"""
    data = get_engine().get_round(round_id)
    if not data:
        return _err("轮次不存在", 404)
    return _ok(data)


@game_bp.route("/rounds/<int:round_id>", methods=["DELETE"])
def delete_round(round_id):
    """删除轮次（仅 ready/paused/finished 可删）"""
    ok, msg = get_engine().delete_round(round_id)
    if not ok:
        return _err(msg)
    return _ok(message="删除成功")


# ── 游戏操作 ──

@game_bp.route("/rounds/<int:round_id>/start", methods=["POST"])
def start_round(round_id):
    ok, msg = get_engine().start_round(round_id)
    if not ok:
        return _err(msg)
    return _ok(message=msg or "开始成功")


@game_bp.route("/rounds/<int:round_id>/pause", methods=["POST"])
def pause_round(round_id):
    ok, msg = get_engine().pause_round(round_id)
    if not ok:
        return _err(msg)
    return _ok(message="已暂停")


@game_bp.route("/rounds/<int:round_id>/resume", methods=["POST"])
def resume_round(round_id):
    ok, msg = get_engine().resume_round(round_id)
    if not ok:
        return _err(msg)
    return _ok(message="已继续")


@game_bp.route("/rounds/<int:round_id>/speed", methods=["POST"])
def set_speed(round_id):
    body = request.get_json(silent=True) or {}
    speed = body.get("speed")
    ok, msg = get_engine().set_speed(round_id, speed)
    if not ok:
        return _err(msg)
    return _ok(message=f"速度已设为 {speed}x")


@game_bp.route("/rounds/<int:round_id>/order", methods=["POST"])
def place_order(round_id):
    """下单 {direction, order_type, price?, shares}，下单即冻结"""
    body = request.get_json(silent=True) or {}
    ok, order, msg = get_engine().place_order(
        round_id,
        direction=body.get("direction", ""),
        order_type=body.get("order_type", "limit"),
        price=body.get("price", 0),
        shares=body.get("shares", 0),
    )
    if not ok:
        return _err(msg)
    return _ok(order, "委托成功")


@game_bp.route("/rounds/<int:round_id>/cancel", methods=["POST"])
def cancel_order(round_id):
    """撤委托单（运行中随时可撤，仅 pending）"""
    body = request.get_json(silent=True) or {}
    order_id = body.get("order_id", "")
    if not order_id:
        return _err("缺少 order_id")
    ok, msg = get_engine().cancel_order(round_id, order_id)
    if not ok:
        return _err(msg)
    return _ok(message="撤单成功")


@game_bp.route("/rounds/<int:round_id>/finish", methods=["POST"])
def finish_round(round_id):
    """提前收盘结算"""
    ok, msg = get_engine().finish_round(round_id)
    if not ok:
        return _err(msg)
    return _ok(message="结算完成")


# ── 查询 ──

@game_bp.route("/rounds/<int:round_id>/orders", methods=["GET"])
def orders(round_id):
    return _ok(get_engine().list_orders(round_id))


@game_bp.route("/rounds/<int:round_id>/trades", methods=["GET"])
def trades(round_id):
    return _ok(get_engine().list_trades(round_id))


@game_bp.route("/rounds/<int:round_id>/account", methods=["GET"])
def account(round_id):
    data = get_engine().get_round(round_id)
    if not data:
        return _err("轮次不存在", 404)
    return _ok(data.get("account"))


@game_bp.route("/rounds/<int:round_id>/ticks", methods=["GET"])
def ticks(round_id):
    """分时图恢复数据（该日全部 tick 的轻量字段，按轮次数据源读表）"""
    from ..dbdata.models import TickData, TickDataSim
    r = GameRound.query.get(round_id)
    if not r:
        return _err("轮次不存在", 404)
    m = TickDataSim if (r.data_source or "qmt") == "sim" else TickData
    rows = (db.session.query(m.time_key, m.open, m.high,
                             m.low, m.close, m.volume,
                             m.amount, m.last_close)
            .filter(m.code == r.code, m.trade_date == r.trade_date)
            .order_by(m.time_key).all())
    tail = request.args.get("tail", type=int, default=0)
    data = [{"time_key": tk, "open": o, "high": h, "low": l, "close": c,
             "volume": v or 0, "amount": a or 0, "last_close": lc or 0}
            for tk, o, h, l, c, v, a, lc in rows]
    if tail and tail > 0:
        data = data[-tail:]
    return _ok({"trade_date": r.trade_date, "code": r.code,
                "data_source": r.data_source or "qmt", "ticks": data})
