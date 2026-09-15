"""
模块名称: api/analysis_routes.py
说明:    调整页 — 信息辅助查询 API（只读 AutoTrade 库，不动游戏库）
"""
import logging

from flask import Blueprint, jsonify, request

from ..utils.autotrade_db import get_conn
from ..utils.config import Config

logger = logging.getLogger(__name__)

analysis_bp = Blueprint("analysis", __name__, url_prefix="/api/v1/analysis")


def _ok(data=None, message="ok"):
    return jsonify({"code": 0, "message": message, "data": data})


def _err(message, code=400):
    return jsonify({"code": code, "message": message}), code


@analysis_bp.route("/daily_kline", methods=["GET"])
def daily_kline():
    """日K线：最近 N 根（升序返回），截止 end_date 含当日

    数据源：AutoTrade 库 stock_kline（period='1d'）。

    参数:
      code      标的代码（默认 588000.SH）
      end_date  截止交易日 YYYY-MM-DD（缺省取库内最新一根）
      limit     根数，默认 250，上限 1000
    """
    code = request.args.get("code", "") or "588000.SH"
    end_date = request.args.get("end_date", "") or None
    try:
        limit = int(request.args.get("limit", "250"))
    except ValueError:
        limit = 250
    limit = max(1, min(limit, 1000))

    dsn = Config.get_instance().get("data_source.autotrade_dsn", "")
    if not dsn:
        return _err("未配置 data_source.autotrade_dsn（AutoTrade 库连接）", 500)

    sql = ("SELECT time_key, open, high, low, close, volume, last_close "
           "FROM stock_kline WHERE code=%s AND period='1d'")
    params = [code]
    if end_date:
        sql += " AND time_key <= %s::timestamp"
        params.append(end_date)
    sql += " ORDER BY time_key DESC LIMIT %s"
    params.append(limit)

    try:
        conn = get_conn(dsn)
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        logger.exception("daily_kline 查询失败 code=%s end_date=%s", code, end_date)
        return _err("日K查询失败: %s" % e, 500)

    rows.reverse()  # DESC 取最近 N 根 → 反转为升序供前端直接渲染
    bars = [{
        "date": tk.strftime("%Y-%m-%d"),
        "open": float(o), "high": float(h), "low": float(l), "close": float(c),
        "volume": int(float(vol or 0)),
        "last_close": float(lc) if lc else None,
    } for tk, o, h, l, c, vol, lc in rows]
    return _ok({
        "code": code,
        "end_date": bars[-1]["date"] if bars else end_date,
        "count": len(bars),
        "bars": bars,
    })
