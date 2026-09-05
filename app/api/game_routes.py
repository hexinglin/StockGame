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
    """可运行交易日列表（按 code 过滤，默认 588000.SH；权威源：game_days）

    参数:
      code       标的代码（缺省返回全部 code 的日期映射）
      allow_sim  1/0 是否允许转换模拟数据（QMT 无该日数据时的兜底）
      source     1/0 返回带来源标记的 [{trade_date, source}]（qmt/sim）
      detail     1/0 附带天维度原始行情（last_close/OHLC/tick_count 等，需 source=1）
    """
    code = request.args.get("code", "") or None
    allow_sim = request.args.get("allow_sim", "0") in ("1", "true", "True")
    with_source = request.args.get("source", "0") in ("1", "true", "True")
    with_detail = request.args.get("detail", "0") in ("1", "true", "True")
    engine = get_engine()
    if code:
        if with_source and with_detail:
            result = engine.date_details(code, allow_sim)
        elif with_source:
            result = engine.date_sources(code, allow_sim)
        else:
            result = engine.available_dates(code, allow_sim)
    else:
        # 全部 code 的日期（game_days 权威：实盘 + 转换模拟并集，与日期选择同源）
        from ..dbdata.models import GameDay
        codes = [c for (c,) in db.session.query(GameDay.code).distinct().all()]
        result = {}
        for c in sorted(codes):
            if with_source and with_detail:
                result[c] = engine.date_details(c, allow_sim)
            elif with_source:
                result[c] = engine.date_sources(c, allow_sim)
            else:
                result[c] = engine.available_dates(c, allow_sim)
    return _ok(result)


@game_bp.route("/days/refresh", methods=["POST"])
def refresh_days():
    """重建/刷新 game_days 天维度行情（与 tick 对齐，日期管理唯一权威表）

    body 可选: {code?, trade_date?}；缺省全量重建（幂等，逐日覆盖）。
    """
    body = request.get_json(silent=True) or {}
    code = body.get("code") or None
    trade_date = body.get("trade_date") or None
    n = get_engine().refresh_all_days(code=code, trade_date=trade_date)
    return _ok({"refreshed": n}, f"交易日记录刷新完成（{n} 日）")


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
    """分时图恢复数据（该日全部快照的轻量字段，按轮次数据源读表）

    快照口径：tick 表存当日累计量额（单调不减），对外转为与上一条快照的
    差分（首条=原值）供前端分钟聚合/均价线累加；high/low 为快照滚动极值、
    close 为最新价，原样透传（今高/今低逐点刷新、价格分钟定型）；open（今开）
    由 game_days 当日常量填充（缺失以首条 close 兑底，与引擎同口径）。
    """
    from ..dbdata.models import TickData, TickDataSim
    r = GameRound.query.get(round_id)
    if not r:
        return _err("轮次不存在", 404)
    m = TickDataSim if (r.data_source or "qmt") == "sim" else TickData
    rows = (db.session.query(m.time_key, m.high,
                             m.low, m.close, m.volume, m.amount)
            .filter(m.code == r.code, m.trade_date == r.trade_date)
            .order_by(m.time_key).all())
    tail = request.args.get("tail", type=int, default=0)
    data = []
    prev_v = prev_a = 0
    for tk, h, l, c, v, a in rows:
        v = v or 0
        a = a or 0
        data.append({"time_key": tk, "high": h, "low": l, "close": c,
                     "volume": max(0, v - prev_v), "amount": max(0, a - prev_a)})
        prev_v, prev_a = v, a
    # 当日常量填充（昨收 + 今开，与引擎 _load_context 同口径）：两值由
    # game_days 天维度真实行情维护（入库时清洗 + stock_kline/首条 close 兑底），
    # 此处按记录值统一回填，保证前端恢复与实时推送一致且恒可解析
    get_engine().normalize_day_constants(
        data, r.code, r.trade_date, r.data_source or "qmt")
    if tail and tail > 0:
        data = data[-tail:]
    return _ok({"trade_date": r.trade_date, "code": r.code,
                "data_source": r.data_source or "qmt", "ticks": data})
