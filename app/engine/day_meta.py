"""
模块名称: engine/day_meta.py
说明:    game_days 天维度行情元数据维护 — 可用交易日查询、昨收确定链、
         快照聚合 upsert、stock_kline 跨库回补。
         从 game_engine.py 拆出（原 _day_map/_date_items/day_last_close/
         _day_row/refresh_day/refresh_all_days/_kline_last_close）。
         注意: 公开函数均要求调用方处于 Flask app context（勿套 _ensure_ctx）。
"""
import logging

from sqlalchemy import text

from ..dbdata.database import db
from ..dbdata.models import GameDay, TickData, TickDataSim
from ..utils.config import Config

logger = logging.getLogger(__name__)


def _valid_price(v) -> bool:
    """有效价格：非空、>0 且非 NaN（NaN 为 truthy 且比较异常，需显式过滤）"""
    return v is not None and v > 0 and v == v


def _parse_dsn(dsn: str) -> dict:
    """解析 postgresql://user:pass@host:port/dbname 连接串"""
    prefix = "postgresql://"
    if dsn.startswith(prefix):
        dsn = dsn[len(prefix):]
    userinfo, _, rest = dsn.partition("@")
    user, _, password = userinfo.partition(":")
    host, _, port_db = rest.partition(":")
    if "/" in port_db:
        port, _, dbname = port_db.partition("/")
    else:
        port, dbname = port_db, "postgres"
    return {"user": user, "password": password, "host": host, "port": port,
            "dbname": dbname}


# 按 tick 表现存快照聚合 upsert 到 game_days（天维度真实行情，与 tick 表
# 对齐的日期管理唯一权威表）的模板（表名白名单拼接）。快照口径：
# volume/amount 为当日累计值，取末条快照（array_agg DESC [1]）而非 sum；
# high/low=max/min 各快照滚动极值；open（今开）为当日常量：hint 有效则写
# hint，否则保留库内旧值（INSERT 时以首条快照 close 兑底）。last_close 维护
# 链见 refresh_day 注释
_GAME_DAY_UPSERT_SQL = """
INSERT INTO game_days (code, trade_date, data_source, open, high, low, close,
                       volume, amount, last_close, tick_count, first_time_key,
                       last_time_key, is_complete)
SELECT :code, :trade_date, :data_source,
       COALESCE(NULLIF(:open_hint, 0), (array_agg(close ORDER BY time_key))[1]),
       max(high), min(low),
       (array_agg(close ORDER BY time_key DESC))[1],
       (array_agg(volume ORDER BY time_key DESC))[1],
       (array_agg(amount ORDER BY time_key DESC))[1],
       :last_close,
       count(*), min(time_key), max(time_key),
       (max(time_key) >= :date_end)
FROM {table}
WHERE code = :code AND trade_date = :trade_date
ON CONFLICT (code, trade_date, data_source) DO UPDATE SET
  open = CASE WHEN :open_hint > 0 THEN :open_hint ELSE game_days.open END,
  high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,
  volume = EXCLUDED.volume, amount = EXCLUDED.amount,
  tick_count = EXCLUDED.tick_count,
  first_time_key = EXCLUDED.first_time_key,
  last_time_key = EXCLUDED.last_time_key,
  is_complete = EXCLUDED.is_complete,
  last_close = CASE WHEN EXCLUDED.last_close > 0 THEN EXCLUDED.last_close
                    ELSE game_days.last_close END,
  updated_at = now()
"""


def tick_model(data_source: str):
    """按数据源返回行情模型：qmt → tick_data(实盘)，sim → tick_data_sim(转换模拟)"""
    return TickDataSim if data_source == "sim" else TickData


# ── 可用交易日（基于 game_days 天维度记录，不扫 tick 表）──

def day_map(code: str) -> dict:
    """标的可开局交易日来源映射（仅完整交易日，权威源：game_days）

    返回 {trade_date: {"qmt": bool, "sim": bool}}；is_complete=true 即该日
    末根 tick >= 15:00:00。日期选择/管理一律以 game_days 为准。
    """
    result = {}
    rows = (db.session.query(GameDay.trade_date, GameDay.data_source)
            .filter(GameDay.code == code, GameDay.is_complete.is_(True))
            .all())
    for trade_date, data_source in rows:
        key = "qmt" if (data_source or "qmt") == "qmt" else "sim"
        result.setdefault(trade_date, {})[key] = True
    return result


def date_items(code: str, allow_sim: bool) -> dict:
    """可用日期 → 实际数据源（qmt 优先）的映射"""
    items = {}
    for date, src in day_map(code).items():
        if src.get("qmt"):
            items[date] = "qmt"
        elif allow_sim and src.get("sim"):
            items[date] = "sim"
    return items


def day_row(code: str, trade_date: str, data_source: str):
    """查询某日 game_days 记录；先按指定数据源精确查，查不到时回退同日
    任意数据源（避免记录缺失时昨收完全不可用）"""
    day = (GameDay.query.filter_by(code=code, trade_date=trade_date,
                                   data_source=data_source).first())
    if day is None:
        day = (GameDay.query.filter_by(code=code, trade_date=trade_date)
               .first())
    return day


def day_last_close(code: str, trade_date: str, data_source: str) -> float:
    """当日昨收：优先 game_days 记录 last_close

    无效（缺失/0/NaN）时以更早完整交易日的 close 兜底（同数据源优先，
    其次另一数据源），语义即上一交易日收盘价；无更早完整日时返回 0
    （前端将展示 "--"）。
    """
    day = day_row(code, trade_date, data_source)
    if day and _valid_price(day.last_close):
        return float(day.last_close)
    other = "sim" if data_source == "qmt" else "qmt"
    for src in (data_source, other):
        prev = (GameDay.query.filter(
                GameDay.code == code,
                GameDay.data_source == src,
                GameDay.is_complete.is_(True),
                GameDay.trade_date < trade_date)
                .order_by(GameDay.trade_date.desc()).first())
        if prev and _valid_price(prev.close):
            return float(prev.close)
    return 0.0


# ── game_days 维护（快照入库后同步刷新）──

def _resolve_last_close(code: str, trade_date: str, data_source: str,
                        hint: float) -> float:
    """昨收确定链：hint > 库内有效旧值 > stock_kline 回补；均无效返回 0"""
    if _valid_price(hint):
        return float(hint)
    old = day_row(code, trade_date, data_source)
    if old is not None and _valid_price(old.last_close):
        return float(old.last_close)
    return kline_last_close(code, trade_date)


def refresh_day(code: str, trade_date: str, data_source: str,
                last_close_hint: float = 0.0, open_hint: float = 0.0) -> bool:
    """快照入库后同步维护 game_days 天维度真实行情（与 tick 表对齐）

    昨收写入前确定链：hint（上传/生成方携带）> 库内有效旧值 > stock_kline
    回补；三者均无效时拒绝写入——last_close 无效的日行情视为异常数据，
    不入库，返回 False（调用方应提示错误；已入库的快照行情不受影响）。
    今开（open）为当日常量随 open_hint 维护，缺失时保留库内旧值（全新日以
    首条快照 close 兑底）。单表 upsert：按 tick 表现存快照聚合当日终值。
    """
    if data_source not in ("qmt", "sim"):
        return False
    table = "tick_data" if data_source == "qmt" else "tick_data_sim"
    lc = _resolve_last_close(code, trade_date, data_source, last_close_hint)
    if not _valid_price(lc):
        logger.warning(
            "刷新 game_days 拒绝: %s %s %s 昨收缺失（last_close hint/库内旧值/"
            "stock_kline 均无效），异常日行情不入库，请携带有效 last_close 后重试",
            code, trade_date, data_source)
        return False
    params = {"code": code, "trade_date": trade_date,
              "data_source": data_source,
              "date_end": f"{trade_date} 15:00:00",
              "last_close": lc,
              "open_hint": _valid_price(open_hint) and float(open_hint) or 0.0}
    try:
        db.session.execute(
            text(_GAME_DAY_UPSERT_SQL.format(table=table)), params)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning("刷新 game_days 失败 code=%s date=%s src=%s: %s",
                       code, trade_date, data_source, e)
        return False
    return True


def refresh_all_days(code: str = None, trade_date: str = None) -> int:
    """全量重建 game_days（迁移/修复用）：遍历 tick 表现存日期逐日刷新"""
    pairs = set()
    for model, src in ((TickData, "qmt"), (TickDataSim, "sim")):
        for c, d in (db.session.query(model.code, model.trade_date)
                     .distinct().all()):
            pairs.add((c, d, src))
    n = 0
    for c, d, src in sorted(pairs):
        if code and c != code:
            continue
        if trade_date and d != trade_date:
            continue
        if refresh_day(c, d, src):
            n += 1
    logger.info("game_days 重建完成: %d 日", n)
    return n


def kline_last_close(code: str, trade_date: str) -> float:
    """从 AutoTrade 库 stock_kline 天维度读取昨收（仅生成时回补）

    优先级：当日 1d 行 last_close > 当日 1m 首根 last_close；查不到/连接
    失败返回 0，不影响主流程。
    """
    dsn = Config.get_instance().get("data_source.autotrade_dsn", "")
    if not dsn:
        return 0.0
    try:
        import psycopg2
        params = _parse_dsn(dsn)
        conn = psycopg2.connect(**params)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT last_close FROM stock_kline "
                "WHERE code=%s AND period='1d' AND last_close > 0 "
                "AND time_key >= %s::date AND time_key < (%s::date + interval '1 day')",
                (code, trade_date, trade_date))
            row = cur.fetchone()
            if not row or not _valid_price(row[0]):
                cur.execute(
                    "SELECT last_close FROM stock_kline "
                    "WHERE code=%s AND period='1m' AND last_close > 0 "
                    "AND time_key >= %s::date AND time_key < (%s::date + interval '1 day') "
                    "ORDER BY time_key LIMIT 1",
                    (code, trade_date, trade_date))
                row = cur.fetchone()
            cur.close()
            return float(row[0]) if row and _valid_price(row[0]) else 0.0
        finally:
            conn.close()
    except Exception as e:
        logger.warning("stock_kline 昨收回补失败 code=%s date=%s: %s",
                       code, trade_date, e)
        return 0.0
