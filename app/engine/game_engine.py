"""
模块名称: engine/game_engine.py
说明:    游戏核心 — 轮次管理 / 时钟推进 / 3s 数据复核撮合 / 结算
         一个轮次 = 一个交易日的完整游戏周期，账户按轮次独立跟踪
"""
import functools
import json
import logging
import random
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import text

from ..dbdata.database import db
from ..dbdata.models import (GameRound, GameOrder, GameTrade, GameDay, DayKline,
                             TickData, TickDataSim)
from ..messaging.cache import get_cache
from ..utils.config import Config
from .account import MockAccount

logger = logging.getLogger(__name__)

# 轮次状态
ST_READY = "ready"
ST_RUNNING = "running"
ST_PAUSED = "paused"
ST_FINISHED = "finished"
ST_ABORTED = "aborted"
_ACTIVE_STATES = (ST_READY, ST_RUNNING, ST_PAUSED)

# 订单状态
O_PENDING = "pending"
O_FILLED = "filled"
O_CANCELLED = "cancelled"
O_REJECTED = "rejected"

_TICK_SEC = 1.0          # 1x 速度每秒推送一根 tick
_CLOCK_INTERVAL = 0.1    # 时钟推进周期（秒）

# 按 tick 表现存数据聚合写入 day_kline（天维度原始行情，与 tick 表对齐）的
# upsert 模板（表名白名单拼接）；last_close 维护链见 refresh_day 注释
_DAY_KLINE_UPSERT_SQL = """
INSERT INTO day_kline (code, trade_date, data_source, open, high, low, close,
                       volume, amount, last_close, tick_count, first_time_key,
                       last_time_key, is_complete)
SELECT :code, :trade_date, :data_source,
       (array_agg(open ORDER BY time_key))[1], max(high), min(low),
       (array_agg(close ORDER BY time_key DESC))[1],
       COALESCE(sum(volume), 0), COALESCE(sum(amount), 0),
       :last_close,
       count(*), min(time_key), max(time_key),
       (max(time_key) >= :date_end)
FROM {table}
WHERE code = :code AND trade_date = :trade_date
ON CONFLICT (code, trade_date, data_source) DO UPDATE SET
  open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
  close = EXCLUDED.close, volume = EXCLUDED.volume, amount = EXCLUDED.amount,
  tick_count = EXCLUDED.tick_count,
  first_time_key = EXCLUDED.first_time_key,
  last_time_key = EXCLUDED.last_time_key,
  is_complete = EXCLUDED.is_complete,
  last_close = CASE WHEN EXCLUDED.last_close > 0 THEN EXCLUDED.last_close
                    ELSE day_kline.last_close END,
  updated_at = now()
"""

# game_days（游戏选择/管理层）派生同步：is_complete 随 day_kline，round_count 不覆盖
_GAME_DAY_SYNC_SQL = """
INSERT INTO game_days (code, trade_date, data_source, is_complete)
SELECT code, trade_date, data_source, is_complete
FROM day_kline
WHERE code = :code AND trade_date = :trade_date AND data_source = :data_source
ON CONFLICT (code, trade_date, data_source) DO UPDATE SET
  is_complete = EXCLUDED.is_complete,
  updated_at = now()
"""


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
    return {"user": user, "password": password, "host": host, "port": port, "dbname": dbname}


class RoundContext:
    """轮次运行时上下文（内存态）"""

    def __init__(self, round_row: GameRound):
        self.round = round_row
        self.ticks = []          # 预加载的 tick dict 列表
        self.index = 0           # 当前 tick 索引
        self.fraction = 0.0      # 时钟推进余数（支持变速）
        self.acct = None         # MockAccount
        self.pending = {}        # order_id -> GameOrder（pending 订单，按创建时间排序）
        self.lock = threading.RLock()


class GameEngine:
    """游戏引擎单例"""

    def __init__(self):
        self._rounds = {}        # round_id -> RoundContext
        self._lock = threading.RLock()
        self._emitter = None     # socket 推送回调: emit(event, data, room=None)
        self._app = None         # Flask app（供后台线程创建 app context）

    # ── 初始化辅助 ──

    def init_app(self, app):
        """绑定 Flask 应用（APScheduler 线程/测试中需手动创建 app context）"""
        self._app = app

    @contextmanager
    def _ctx(self):
        """确保 Flask app context（调度器后台线程与测试环境无 context，需手动创建）"""
        if self._app is None:
            raise RuntimeError("GameEngine 未绑定 app，请先调用 init_app(app)")
        with self._app.app_context():
            yield

    def _ensure_ctx(fn):
        """装饰器：为方法包裹 Flask app context"""
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            with self._ctx():
                return fn(self, *args, **kwargs)
        return wrapper

    def set_emitter(self, emitter):
        """注入 socket 推送回调（在 main.py 中设置）"""
        self._emitter = emitter

    def emit(self, event, data, room=None):
        if self._emitter:
            try:
                self._emitter(event, data, room)
            except Exception as e:
                logger.warning("socket 推送失败 %s: %s", event, e)

    def _cfg(self):
        cfg = Config.get_instance()
        return {
            "base_shares": int(cfg.get("game.base_shares", 500000)),
            "initial_cash": float(cfg.get("game.initial_cash", 500000)),
            "fee_rate": float(cfg.get("game.fee_rate", 0.0001)),
            "stock_code": cfg.get("game.stock_code", "588000.SH"),
        }

    # ── 可用交易日（基于 game_days 天维度记录，不扫 tick 表）──

    def _tick_model(self, data_source: str):
        """按数据源返回行情模型：qmt → tick_data(实盘)，sim → tick_data_sim(转换模拟)"""
        return TickDataSim if data_source == "sim" else TickData

    @_ensure_ctx
    def _day_map(self, code: str) -> dict:
        """标的可开局交易日来源映射（仅完整交易日）

        返回 {trade_date: {"qmt": bool, "sim": bool}}；数据来自 game_days
        （游戏选择/管理层，is_complete=true 即该日末根 tick >= 15:00:00，
        由 day_kline 在行情入库时派生同步）。
        """
        result = {}
        rows = (db.session.query(GameDay.trade_date, GameDay.data_source)
                .filter(GameDay.code == code, GameDay.is_complete.is_(True))
                .all())
        for trade_date, data_source in rows:
            key = "qmt" if (data_source or "qmt") == "qmt" else "sim"
            result.setdefault(trade_date, {})[key] = True
        return result

    @_ensure_ctx
    def available_dates(self, code: str, allow_sim: bool = False) -> list:
        """可用交易日列表（按 code 过滤，数据完整：最后一根 tick >= 15:00:00）

        allow_sim=True 时并入仅有转换模拟数据完整的日期（QMT 数据仍优先）。
        """
        return sorted(self._date_items(code, allow_sim).keys())

    @_ensure_ctx
    def date_sources(self, code: str, allow_sim: bool = False) -> list:
        """可用交易日 + 数据来源标记（QMT 优先）

        返回 [{trade_date, source}]；同日 qmt 与 sim 并存时 source=qmt
        （游戏数据源优先级：QMT 实盘 > 转换模拟）。
        """
        items = self._date_items(code, allow_sim)
        return [{"trade_date": d, "source": s} for d, s in items.items()]

    @_ensure_ctx
    def _date_items(self, code: str, allow_sim: bool) -> dict:
        """可用日期 → 实际数据源（qmt 优先）的映射（内部，调用方须处于 app context）"""
        items = {}
        for date, src in self._day_map(code).items():
            if src.get("qmt"):
                items[date] = "qmt"
            elif allow_sim and src.get("sim"):
                items[date] = "sim"
        return items

    @_ensure_ctx
    def date_details(self, code: str, allow_sim: bool = False) -> list:
        """可运行交易日 + 天维度原始行情（选择/开局界面用，QMT 优先）

        可开局判定来自 game_days（选择/管理层），行情元数据
        （tick_count/OHLC/last_close 等）从 day_kline 读取（与 tick 表对齐
        的原始数据，入库时维护），不触碰 tick 行情表。
        """
        items = self._date_items(code, allow_sim)
        if not items:
            return []
        klines = (DayKline.query.filter(
                  DayKline.code == code,
                  DayKline.trade_date.in_(list(items)),
                  DayKline.is_complete.is_(True)).all())
        meta = {(d.trade_date, d.data_source): d for d in klines}
        result = []
        for trade_date, src in items.items():
            day = meta.get((trade_date, src)) or meta.get((trade_date, "qmt")) \
                or meta.get((trade_date, "sim"))
            item = day.to_dict() if day else {"trade_date": trade_date,
                                              "data_source": src}
            item["source"] = src
            result.append(item)
        return result

    # ── 轮次管理 ──

    @_ensure_ctx
    def create_round(self, code: str = None, trade_date: str = None,
                     allow_sim: bool = False) -> (GameRound, str):
        """创建轮次

        数据源选择：该日有 QMT 数据 → qmt；无 QMT 数据但 allow_sim 且转换
        模拟数据完整 → sim；否则拒绝。
        约束: 同一 (code, trade_date) 仅允许一个未结束轮次（应用层校验）
        """
        p = self._cfg()
        code = code or p["stock_code"]

        # 可用交易日（allow_sim=False 时仅 QMT 数据日期）
        dates = self._date_items(code, allow_sim)
        if not dates:
            return None, f"标的 {code} 暂无完整交易日数据，请先上传行情"
        if trade_date:
            if trade_date not in dates:
                return None, f"交易日 {trade_date} 数据不完整或不存在（可用: {sorted(dates)}）"
        else:
            trade_date = random.choice(sorted(dates))

        # 同日并发约束（按 code+交易日）
        exist = GameRound.query.filter(
            GameRound.code == code,
            GameRound.trade_date == trade_date,
            GameRound.status.in_(_ACTIVE_STATES),
        ).first()
        if exist:
            return None, f"交易日 {trade_date} 已有未结束轮次 (id={exist.id}, status={exist.status})"

        r = GameRound(
            code=code,
            trade_date=trade_date,
            status=ST_READY,
            speed=1,
            data_source=dates[trade_date],   # qmt / sim
            initial_cash=p["initial_cash"],
            base_shares=p["base_shares"],
            initial_assets=0,
        )
        db.session.add(r)
        db.session.commit()
        # 该日已创建轮次数 +1（game_days 选择/管理层，随创建/删除物理维护）
        self._adjust_round_count(code, trade_date, r.data_source, 1)
        logger.info("创建轮次 id=%s code=%s date=%s source=%s",
                    r.id, code, trade_date, r.data_source)
        return r, ""

    @_ensure_ctx
    def delete_round(self, round_id: int) -> (bool, str):
        """删除轮次（仅 ready/paused/finished 可删）"""
        with self._lock:
            r = GameRound.query.get(round_id)
            if not r:
                return False, "轮次不存在"
            if r.status == ST_RUNNING:
                return False, "运行中的轮次不可删除，请先暂停或结束"
            ctx = self._rounds.pop(round_id, None)

            # 手动级联删除（SQLAlchemy 不自动级联）：委托/成交/轮次行全部物理删除
            GameOrder.query.filter_by(round_id=round_id).delete()
            GameTrade.query.filter_by(round_id=round_id).delete()
            code, trade_date, data_source = r.code, r.trade_date, r.data_source or "qmt"
            db.session.delete(r)
            db.session.commit()

            # 清理 Redis（账户/进度/行情快照）
            cache = get_cache()
            cache.delete_account(round_id)
            cache.delete_progress(round_id)
            cache.delete_quote(str(round_id))

            # 该日已创建轮次数 -1（随删除物理回落，不留计数残留）
            self._adjust_round_count(code, trade_date, data_source, -1)
            return True, ""

    @_ensure_ctx
    def list_rounds(self) -> list:
        """轮次列表（含进度）"""
        rows = GameRound.query.order_by(GameRound.created_at.desc()).all()
        result = []
        for r in rows:
            progress = self._progress_of(r)
            result.append({
                "id": r.id,
                "code": r.code,
                "trade_date": r.trade_date,
                "status": r.status,
                "speed": r.speed,
                "data_source": r.data_source or "qmt",
                "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                "initial_cash": r.initial_cash,
                "base_shares": r.base_shares,
                "initial_assets": round(r.initial_assets or 0, 2),
                "final_assets": round(r.final_assets or 0, 2),
                "realized_pnl": round(r.realized_pnl or 0, 2),
                "fee_total": round(r.fee_total or 0, 2),
                "last_price": r.last_price,
                "last_time_key": r.last_time_key,
                "progress": progress,
            })
        return result

    @_ensure_ctx
    def get_round(self, round_id: int):
        """轮次详情 + 账户 + 最新价"""
        r = GameRound.query.get(round_id)
        if not r:
            return None
        ctx = self._rounds.get(round_id)
        acct = None
        if ctx and ctx.acct:
            acct = ctx.acct.to_dict()
        elif r.status in (ST_RUNNING, ST_PAUSED, ST_FINISHED):
            # finished 轮次同样可从 Redis 快照恢复结算后账户（未删除轮次）；
            # Redis 缺失时以 DB 账户 JSON 兜底
            acct = get_cache().load_account(round_id)
            if not acct:
                acct = self._account_from_json(r.account_json)

        # 累计成交额/量（前端分时图恢复用；重启后按已推进区间重算）
        cum_amount, cum_volume = 0.0, 0
        if ctx and hasattr(ctx, "cum_amount"):
            cum_amount, cum_volume = ctx.cum_amount, ctx.cum_volume
        elif r.last_time_key:
            m = self._tick_model(r.data_source)
            row = (db.session.query(
                    db.func.coalesce(db.func.sum(m.amount), 0),
                    db.func.coalesce(db.func.sum(m.volume), 0))
                   .filter(m.code == r.code,
                           m.trade_date == r.trade_date,
                           m.time_key <= r.last_time_key).first())
            if row:
                cum_amount, cum_volume = row[0] or 0, row[1] or 0
        return {
            "id": r.id,
            "code": r.code,
            "trade_date": r.trade_date,
            "status": r.status,
            "speed": r.speed,
            "data_source": r.data_source or "qmt",
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
            "started_at": r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else None,
            "finished_at": r.finished_at.strftime("%Y-%m-%d %H:%M:%S") if r.finished_at else None,
            "initial_cash": r.initial_cash,
            "base_shares": r.base_shares,
            "initial_assets": round(r.initial_assets or 0, 2),
            "final_assets": round(r.final_assets or 0, 2),
            "realized_pnl": round(r.realized_pnl or 0, 2),
            "fee_total": round(r.fee_total or 0, 2),
            "last_price": r.last_price,
            "last_time_key": r.last_time_key,
            "cum_amount": round(cum_amount, 2),
            "cum_volume": cum_volume,
            "account": acct,
        }

    # ── 启动 / 暂停 / 变速 ──

    def _load_context(self, round_id: int) -> (RoundContext, str):
        """加载轮次运行时上下文（tick/账户/进度/pending），供启动与重启恢复共用

        注意: 调用方必须已处于 Flask app context（start_round/resume_round 均
        由 _ensure_ctx 包裹）；此处不能再套 _ensure_ctx，否则嵌套 context 会
        产生不同的 scoped_session，query.get 返回与调用方不同的 ORM 实例，
        导致 ctx.round 与 DB 状态不同步（时钟据此判断运行状态）。
        """
        r = GameRound.query.get(round_id)
        if not r:
            return None, "轮次不存在"

        ctx = RoundContext(r)
        # 加载该日 tick（按轮次数据源: qmt → tick_data，sim → tick_data_sim）
        m = self._tick_model(r.data_source)
        ticks = (
            db.session.query(m)
            .filter(m.code == r.code, m.trade_date == r.trade_date)
            .order_by(m.time_key)
            .all()
        )
        if not ticks:
            return None, f"交易日 {r.trade_date} 无 {r.code} 行情数据"
        ctx.ticks = [{
            "time_key": t.time_key,
            "open": t.open, "high": t.high, "low": t.low, "close": t.close,
            "volume": t.volume or 0, "amount": t.amount or 0,
        } for t in ticks]

        # 昨收填充：来自 day_kline 天维度原始行情（入库时维护，缺失时生成侧从
        # stock_kline 回补），统一写入每根 tick，保证推送给前端的昨收恒为
        # 有效数值（与 REST ticks 恢复接口同口径，详见 normalize_last_close）
        self.normalize_last_close(ctx.ticks, r.code, r.trade_date, r.data_source)

        # 进度恢复（后端重启场景）：优先 Redis 快照；Redis 缺失/不可用时
        # 以 DB last_time_key（每根 tick 落库）反推已推进位置，保证走势
        # 进度持久化——行情数据本身永久存于 tick 表，进度不丢即可完整恢复
        cache = get_cache()
        saved_index = cache.load_progress(round_id)
        if not (0 < saved_index < len(ctx.ticks)):
            keys = [t["time_key"] for t in ctx.ticks]
            saved_index = (next((i for i, k in enumerate(keys) if k > r.last_time_key),
                                len(keys)) if r.last_time_key else 0)
        ctx.index = saved_index if 0 < saved_index < len(ctx.ticks) else 0
        ctx.fraction = 0.0

        # 账户：优先 Redis 快照，其次 DB 账户 JSON（交易事件随行落库），
        # 均缺失才按初始参数初始化（与委托/成交记录保持一致的兜底链）
        acct_dict = cache.load_account(round_id)
        if not acct_dict:
            acct_dict = self._account_from_json(r.account_json)
        if acct_dict:
            ctx.acct = MockAccount.from_dict(acct_dict)
        else:
            first = ctx.ticks[0]
            ctx.acct = MockAccount(
                base_shares=r.base_shares or None,
                initial_cash=r.initial_cash or None,
                fee_rate=None,
                open_price=first["close"],
            )
            r.initial_assets = ctx.acct.total_assets(first["close"])

        # 恢复 pending 订单（重启场景）
        pending_orders = GameOrder.query.filter_by(
            round_id=round_id, status=O_PENDING).all()
        ctx.pending = {o.order_id: o for o in pending_orders}
        return ctx, ""

    @staticmethod
    def _account_from_json(raw):
        """解析 DB 账户 JSON 快照，缺失/损坏时返回 None"""
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("账户 JSON 快照解析失败，忽略")
            return None

    @staticmethod
    def _attach_account_json(round_row, ctx):
        """账户快照随调用方事务落库（不自行 commit，仅交易事件后低频调用）

        Redis 之外的 DB 兜底：Redis 丢失/重启后仍可恢复与委托/成交
        记录一致的账户状态。round_row 须为当前 session 已 attach 的实例
        （_try_fill 中为 detached 的 ctx.round，需先 add 再 commit）。
        """
        if ctx and ctx.acct and round_row is not None:
            round_row.account_json = json.dumps(ctx.acct.to_dict())

    def _progress_of(self, r: GameRound) -> float:
        """轮次进度：内存 ctx 优先；ctx 缺失（后端重启）时按 DB
        last_time_key 在行情表中反推已推进比例（调用方须处于 app context）"""
        ctx = self._rounds.get(r.id)
        if ctx and ctx.ticks:
            return round(ctx.index / len(ctx.ticks) * 100, 1)
        if not r.last_time_key:
            return 0.0
        m = self._tick_model(r.data_source or "qmt")
        total = (db.session.query(db.func.count())
                 .filter(m.code == r.code, m.trade_date == r.trade_date).scalar() or 0)
        if not total:
            return 0.0
        done = (db.session.query(db.func.count())
                .filter(m.code == r.code, m.trade_date == r.trade_date,
                        m.time_key <= r.last_time_key).scalar() or 0)
        return round(done / total * 100, 1)

    def normalize_last_close(self, ticks: list, code: str, trade_date: str,
                             data_source: str) -> list:
        """昨收填充（调用方须处于 app context，勿套 _ensure_ctx）

        昨收（上一交易日收盘）为日维度常量，由 day_kline 表在行情入库时维护
        （与 tick 表对齐；tick 表已不再存 last_close）。此处读取当日记录值并
        统一写入每根 tick dict，保证引擎 _load_context 与 REST ticks 恢复接口
        同口径；当日无有效值时以前一完整交易日的 close 兜底（绝不用当日价格
        充当基准）。
        """
        base_close = self.day_last_close(code, trade_date, data_source)
        for t in ticks:
            t["last_close"] = base_close
        return ticks

    def day_last_close(self, code: str, trade_date: str, data_source: str) -> float:
        """当日昨收：优先 day_kline 记录 last_close（调用方须处于 app context）

        无效（缺失/0/NaN）时以更早完整交易日的 close 兜底（同数据源优先，
        其次另一数据源），语义即上一交易日收盘价；无更早完整日时返回 0
        （前端将展示 "--"）。
        """
        day = self._day_row(code, trade_date, data_source)
        if day and _valid_price(day.last_close):
            return float(day.last_close)
        other = "sim" if data_source == "qmt" else "qmt"
        for src in (data_source, other):
            prev = (DayKline.query.filter(
                    DayKline.code == code,
                    DayKline.data_source == src,
                    DayKline.is_complete.is_(True),
                    DayKline.trade_date < trade_date)
                    .order_by(DayKline.trade_date.desc()).first())
            if prev and _valid_price(prev.close):
                return float(prev.close)
        return 0.0

    def _day_row(self, code: str, trade_date: str, data_source: str):
        """查询某日 day_kline 记录（调用方须处于 app context）

        先按指定数据源精确查；查不到时回退同日任意数据源（避免记录缺失时
        昨收完全不可用）。
        """
        day = (DayKline.query.filter_by(code=code, trade_date=trade_date,
                                        data_source=data_source).first())
        if day is None:
            day = (DayKline.query.filter_by(code=code, trade_date=trade_date)
                   .first())
        return day

    # ── game_days 天维度记录维护 ──

    def _adjust_round_count(self, code: str, trade_date: str, data_source: str,
                            delta: int) -> None:
        """维护 game_days.round_count（该日已创建轮次数：创建 +1 / 删除 -1，下限 0）

        调用方须处于 app context；game_days 行缺失（理论不发生：轮次只能建于
        game_days 已登记的完整交易日）时仅记录日志，不影响轮次主流程。
        """
        try:
            db.session.execute(
                text("UPDATE game_days "
                     "SET round_count = GREATEST(round_count + :delta, 0), "
                     "updated_at = now() "
                     "WHERE code = :code AND trade_date = :trade_date "
                     "AND data_source = :data_source"),
                {"delta": delta, "code": code, "trade_date": trade_date,
                 "data_source": data_source})
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.warning("维护 game_days.round_count 失败 code=%s date=%s: %s",
                           code, trade_date, e)

    def refresh_day(self, code: str, trade_date: str, data_source: str,
                    last_close_hint: float = 0.0) -> bool:
        """tick 入库后同步维护天维度日行情 day_kline（与 tick 表对齐）

        调用方须处于 app context（行情上传/批量写入后调用，勿套 _ensure_ctx）。
        昨收写入前确定链：hint（上传/生成方携带）> 库内有效旧值 > stock_kline
        天维度回补（_kline_last_close）；三者均无效时拒绝写入——last_close
        无效（空/0）的日行情视为异常数据，不入库、不派生 game_days，返回 False
        （调用方应提示错误；已入库的 tick 行情本身不受影响）。
        写入两步：
        1) day_kline：按 tick 表现存数据聚合（条数/首末时间/OHLC 均对账自
           tick），昨收取确定链结果；
        2) game_days：游戏选择/管理层派生同步（is_complete 随 day_kline）。
        游戏选择与开局数据读取不再扫描 tick 表（tick 表仅游戏运行时使用）。
        """
        if data_source not in ("qmt", "sim"):
            return False
        table = "tick_data" if data_source == "qmt" else "tick_data_sim"
        hint = _valid_price(last_close_hint) and float(last_close_hint) or 0.0
        # 昨收确定链：hint > 库内有效旧值 > stock_kline 天维度（仅生成侧）
        lc = hint
        if not _valid_price(lc):
            old = self._day_row(code, trade_date, data_source)
            if old is not None and _valid_price(old.last_close):
                lc = float(old.last_close)
            else:
                lc = self._kline_last_close(code, trade_date)
        if not _valid_price(lc):
            logger.warning(
                "刷新 day_kline 拒绝: %s %s %s 昨收缺失（last_close hint/库内旧值/"
                "stock_kline 均无效），异常日行情不入库，请携带有效 last_close 后重试",
                code, trade_date, data_source)
            return False
        params = {"code": code, "trade_date": trade_date,
                  "data_source": data_source,
                  "date_end": f"{trade_date} 15:00:00",
                  "last_close": lc}
        try:
            db.session.execute(
                text(_DAY_KLINE_UPSERT_SQL.format(table=table)), params)
            db.session.execute(text(_GAME_DAY_SYNC_SQL), params)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.warning("刷新 day_kline/game_days 失败 code=%s date=%s src=%s: %s",
                           code, trade_date, data_source, e)
            return False
        return True

    def refresh_all_days(self, code: str = None, trade_date: str = None) -> int:
        """全量重建 day_kline/game_days（迁移/修复用，调用方须处于 app context）

        遍历 tick_data / tick_data_sim 现存日期逐日刷新（先写 day_kline 再
        派生 game_days），返回处理日期数。
        """
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
            if self.refresh_day(c, d, src):
                n += 1
        logger.info("day_kline/game_days 重建完成: %d 日", n)
        return n

    def _kline_last_close(self, code: str, trade_date: str) -> float:
        """从 AutoTrade 库 stock_kline 天维度读取昨收（仅生成时回补）

        优先级：当日 1d 行 last_close > 当日 1m 首根 last_close（口径同
        load_minutes_from_autotrade）；查不到/连接失败返回 0，不影响主流程。
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

    @_ensure_ctx
    def start_round(self, round_id: int) -> (bool, str):
        """开始游戏：预加载 tick + 初始化/恢复账户"""
        with self._lock:
            r = GameRound.query.get(round_id)
            if not r:
                return False, "轮次不存在"
            if r.status == ST_RUNNING:
                return False, "轮次已在运行中"
            if r.status == ST_FINISHED:
                return False, "轮次已结束"

            ctx = self._rounds.get(round_id)
            if ctx is None:
                ctx, msg = self._load_context(round_id)
                if ctx is None:
                    return False, msg
                self._rounds[round_id] = ctx

            # 若已到尾端则直接结算（已 finished，不再置 running）
            if ctx.index >= len(ctx.ticks):
                self._settle(round_id, reason="已达尾端", auto=True)
                return True, "轮次已到尾端，自动结算完成"

            r.status = ST_RUNNING
            r.started_at = r.started_at or datetime.now()
            self._attach_account_json(r, ctx)   # 初始/恢复后的账户随行落库
            db.session.commit()
            self._save_snapshot(ctx)
            self.emit("game:status", {"round_id": round_id, "status": ST_RUNNING},
                      room=f"round_{round_id}")
            logger.info("轮次 %s 启动 date=%s ticks=%d index=%d",
                        round_id, r.trade_date, len(ctx.ticks), ctx.index)
            return True, ""

    @_ensure_ctx
    def pause_round(self, round_id: int) -> (bool, str):
        with self._lock:
            r = GameRound.query.get(round_id)
            if not r or r.status != ST_RUNNING:
                return False, "仅运行中的轮次可暂停"
            ctx = self._rounds.get(round_id)
            if ctx:
                self._save_snapshot(ctx)
                ctx.round.status = ST_PAUSED   # 同步内存态（时钟据此判断）
            r.status = ST_PAUSED
            db.session.commit()
            self.emit("game:status", {"round_id": round_id, "status": ST_PAUSED},
                      room=f"round_{round_id}")
            return True, ""

    @_ensure_ctx
    def resume_round(self, round_id: int) -> (bool, str):
        with self._lock:
            r = GameRound.query.get(round_id)
            if not r or r.status != ST_PAUSED:
                return False, "仅暂停的轮次可继续"
            ctx = self._rounds.get(round_id)
            if ctx is None:
                # 后端重启后内存上下文已丢失，从 DB/Redis 恢复
                ctx, msg = self._load_context(round_id)
                if ctx is None:
                    return False, msg
                self._rounds[round_id] = ctx
            # 若已到尾端则直接结算（已 finished，不再置 running）
            if ctx.index >= len(ctx.ticks):
                self._settle(round_id, reason="已达尾端", auto=True)
                return True, "轮次已到尾端，自动结算完成"
            ctx.round.status = ST_RUNNING  # 同步内存态（时钟据此推进）
            r.status = ST_RUNNING
            db.session.commit()
            self._save_snapshot(ctx)
            self.emit("game:status", {"round_id": round_id, "status": ST_RUNNING},
                      room=f"round_{round_id}")
            logger.info("轮次 %s 恢复运行 date=%s ticks=%d index=%d",
                        round_id, r.trade_date, len(ctx.ticks), ctx.index)
            return True, ""

    @_ensure_ctx
    def set_speed(self, round_id: int, speed: int) -> (bool, str):
        if speed not in (1, 10, 60):
            return False, "speed 仅支持 1/10/60"
        with self._lock:
            r = GameRound.query.get(round_id)
            if not r:
                return False, "轮次不存在"
            ctx = self._rounds.get(round_id)
            if ctx:
                ctx.round.speed = speed       # 同步内存态（时钟据此推进）
            r.speed = speed
            db.session.commit()
            self.emit("game:status", {"round_id": round_id, "speed": speed},
                      room=f"round_{round_id}")
            return True, ""

    # ── 下单 / 撤单 ──

    @_ensure_ctx
    def place_order(self, round_id: int, direction: str, order_type: str,
                    price: float, shares: int) -> (bool, dict, str):
        """下单（下单即冻结；市价单按最新价预估冻结）

        返回 (ok, order_dict, message)
        """
        if direction not in ("buy", "sell"):
            return False, None, "direction 仅支持 buy/sell"
        if order_type not in ("limit", "market"):
            return False, None, "order_type 仅支持 limit/market"
        if shares <= 0 or shares % 100 != 0:
            return False, None, "数量必须为 100 的整数倍"
        if order_type == "limit" and (not price or price <= 0):
            return False, None, "限价单价格必须大于 0"

        with self._lock:
            r = GameRound.query.get(round_id)
            if not r:
                return False, None, "轮次不存在"
            if r.status not in (ST_RUNNING, ST_PAUSED):
                return False, None, "轮次未在运行中（需先开始游戏）"
            ctx = self._rounds.get(round_id)
            if ctx is None or ctx.acct is None:
                return False, None, "轮次上下文未初始化，请重新开始"

            acct = ctx.acct
            # 市价单用最新价预估
            if order_type == "market":
                if not acct.last_price or acct.last_price <= 0:
                    return False, None, "暂无最新价，无法下市价单"
                price = acct.last_price

            # 下单即冻结
            if direction == "buy":
                if not acct.freeze_buy(price, shares):
                    return False, None, "可用资金不足（含手续费）"
            else:
                if not acct.freeze_sell(shares):
                    return False, None, "可卖持仓不足"

            order_id = "R%d_%s" % (round_id, uuid.uuid4().hex[:12].upper())
            order = GameOrder(
                order_id=order_id,
                round_id=round_id,
                code=r.code,
                direction=direction,
                order_type=order_type,
                price=price,
                shares=shares,
                status=O_PENDING,
                frozen_amount=acct.frozen_amount(price, shares) if direction == "buy" else 0,
            )
            db.session.add(order)
            self._attach_account_json(r, ctx)   # 冻结后的账户落库（DB 兜底）
            db.session.commit()

            ctx.pending[order_id] = order
            self._save_snapshot(ctx)
            self.emit("game:order_update", self._order_to_dict(order),
                      room=f"round_{round_id}")
            return True, self._order_to_dict(order), ""

    @_ensure_ctx
    def cancel_order(self, round_id: int, order_id: str) -> (bool, str):
        """撤委托单（运行中随时可撤，仅 pending 状态）"""
        with self._lock:
            r = GameRound.query.get(round_id)
            if not r:
                return False, "轮次不存在"
            if r.status not in (ST_RUNNING, ST_PAUSED):
                return False, "仅运行中的轮次可撤单"
            ctx = self._rounds.get(round_id)
            order = GameOrder.query.filter_by(round_id=round_id, order_id=order_id).first()
            if not order:
                return False, "委托不存在"
            if order.status != O_PENDING:
                return False, f"仅 pending 状态可撤（当前: {order.status}）"

            # 解冻
            if ctx and ctx.acct:
                if order.direction == "buy":
                    ctx.acct.unfreeze_buy(order.price, order.shares)
                else:
                    ctx.acct.unfreeze_sell(order.shares)

            order.status = O_CANCELLED
            self._attach_account_json(r, ctx)   # 解冻后的账户落库（DB 兜底）
            db.session.commit()
            if ctx:
                ctx.pending.pop(order_id, None)
                self._save_snapshot(ctx)
            self.emit("game:order_update", self._order_to_dict(order),
                      room=f"round_{round_id}")
            return True, ""

    @_ensure_ctx
    def list_orders(self, round_id: int) -> list:
        rows = (GameOrder.query.filter_by(round_id=round_id)
                .order_by(GameOrder.created_at.desc()).all())
        return [self._order_to_dict(o) for o in rows]

    @_ensure_ctx
    def list_trades(self, round_id: int) -> list:
        rows = (GameTrade.query.filter_by(round_id=round_id)
                .order_by(GameTrade.id.desc()).all())
        return [{
            "id": t.id,
            "order_id": t.order_id,
            "code": t.code,
            "direction": t.direction,
            "price": t.price,
            "shares": t.shares,
            "fee": t.fee,
            "trade_time": t.trade_time,
        } for t in rows]

    # ── 时钟推进 ──

    def tick_all(self):
        """全局时钟任务（APScheduler interval=0.1s）：推进所有 running 轮次"""
        with self._lock:
            running = [ctx for ctx in self._rounds.values()
                       if ctx.round.status == ST_RUNNING]
        for ctx in running:
            try:
                self._advance(ctx)
            except Exception as e:
                logger.exception("轮次 %s 推进异常: %s", ctx.round.id, e)

    @_ensure_ctx
    def _advance(self, ctx: RoundContext):
        """按速度推进轮次时钟"""
        speed = ctx.round.speed or 1
        with ctx.lock:
            # 每 0.1s 推进 speed×0.1/3 个 tick
            ctx.fraction += speed * _CLOCK_INTERVAL / _TICK_SEC
            steps = int(ctx.fraction)
            if steps <= 0:
                return
            ctx.fraction -= steps
            for _ in range(steps):
                if ctx.round.status != ST_RUNNING or ctx.index >= len(ctx.ticks):
                    break
                self._process_tick(ctx, ctx.ticks[ctx.index])
                ctx.index += 1
                if ctx.index % 10 == 0:
                    self._save_snapshot(ctx)
            self._save_snapshot(ctx)

    @_ensure_ctx
    def _process_tick(self, ctx: RoundContext, tick: dict):
        """处理一根 3s tick：更新行情 + 撮合"""
        r = ctx.round
        acct = ctx.acct
        acct.last_price = tick["close"]
        r.last_price = tick["close"]
        r.last_time_key = tick["time_key"]

        # 累计成交额/量（前端均价线用）
        if not hasattr(ctx, "cum_amount"):
            ctx.cum_amount = 0.0
            ctx.cum_volume = 0.0
        ctx.cum_amount += tick.get("amount") or 0
        ctx.cum_volume += tick.get("volume") or 0

        # 推送最新行情（分时图数据源）
        self.emit("game:quote", {
            "round_id": r.id,
            "code": r.code,
            "time_key": tick["time_key"],
            "open": tick["open"],
            "high": tick["high"],
            "low": tick["low"],
            "close": tick["close"],
            "volume": tick.get("volume") or 0,
            "amount": tick.get("amount") or 0,
            "last_close": tick.get("last_close") or 0,
            "cum_amount": round(ctx.cum_amount, 2),
            "cum_volume": ctx.cum_volume,
            "progress": round(ctx.index / len(ctx.ticks) * 100, 1) if ctx.ticks else 0,
        }, room=f"round_{r.id}")

        # 撮合所有 pending 订单（按委托时间排序）
        filled_any = False
        for order_id, order in list(ctx.pending.items()):
            if order.status != O_PENDING:
                ctx.pending.pop(order_id, None)
                continue
            if self._try_fill(ctx, order, tick):
                filled_any = True
                ctx.pending.pop(order_id, None)

        # 本 tick 无成交 → 持久化轮次状态（last_price/last_time_key 等）
        if not filled_any:
            db.session.add(ctx.round)
            db.session.commit()

        # 最后一根 tick → 自动结算
        if ctx.index >= len(ctx.ticks) - 1:
            self._settle(ctx.round.id, reason="收盘", auto=True)

    @_ensure_ctx
    def _try_fill(self, ctx: RoundContext, order: GameOrder, tick: dict) -> bool:
        """尝试撮合一笔委托，成交返回 True；未触及返回 False"""
        acct = ctx.acct
        direction, otype, price, shares = order.direction, order.order_type, order.price, order.shares
        filled = False
        fill_price = price

        if otype == "limit":
            if direction == "buy" and tick["low"] <= price:
                filled = True
            elif direction == "sell" and tick["high"] >= price:
                filled = True
            else:
                return False
        else:  # market
            fill_price = tick["close"]
            filled = True

        if not filled:
            return False

        # 成交校验（市价单成交价可能高于冻结预估）
        if direction == "buy":
            amount = fill_price * shares + acct.fee_for(fill_price * shares)
            if order.frozen_amount < amount - 0.001:
                # 本单冻结额不足 → 拒单并解冻（冻结转回可用现金）
                acct.unfreeze_buy(order.price, shares)
                order.status = O_REJECTED
                order.reject_reason = f"市价成交价 {fill_price} 超出冻结额"
                # 订单/轮次为跨 context 的 detached 实例，需重新 attach 才能持久化
                self._attach_account_json(ctx.round, ctx)
                db.session.add(order)
                db.session.add(ctx.round)
                db.session.commit()
                self.emit("game:order_update", self._order_to_dict(order),
                          room=f"round_{ctx.round.id}")
                return True
            fee = acct.fill_buy(fill_price, shares, frozen_amount=order.frozen_amount)
        else:
            fee = acct.fill_sell(fill_price, shares)

        # 记录成交
        trade = GameTrade(
            round_id=ctx.round.id,
            order_id=order.order_id,
            code=order.code,
            direction=direction,
            price=fill_price,
            shares=shares,
            fee=fee,
            trade_time=tick["time_key"],
        )
        db.session.add(trade)
        order.status = O_FILLED
        order.filled_shares = shares
        order.filled_price = fill_price
        order.fee = fee
        order.filled_at = datetime.now()

        # 累计已实现盈亏与手续费
        if direction == "buy":
            # 买入：仅手续费为已实现亏损（本金转为持仓，不产生盈亏）
            ctx.round.realized_pnl = (ctx.round.realized_pnl or 0) - fee
        else:
            # 卖出：(卖出价 - 持仓成本) × 数量 - 手续费
            ctx.round.realized_pnl = (ctx.round.realized_pnl or 0) + (fill_price - acct.avg_price) * shares - fee
        ctx.round.fee_total = (ctx.round.fee_total or 0) + fee
        # 订单/轮次为跨 context 的 detached 实例，需重新 attach 才能持久化
        self._attach_account_json(ctx.round, ctx)
        db.session.add(order)
        db.session.add(ctx.round)
        db.session.commit()

        self.emit("game:order_update", self._order_to_dict(order),
                  room=f"round_{ctx.round.id}")
        self.emit("game:trade", {
            "order_id": order.order_id, "code": order.code, "direction": direction,
            "price": fill_price, "shares": shares, "fee": fee,
            "trade_time": tick["time_key"],
            "realized_pnl": round(ctx.round.realized_pnl or 0, 2),
            "fee_total": round(ctx.round.fee_total or 0, 2),
        }, room=f"round_{ctx.round.id}")
        acct_d = acct.to_dict()
        acct_d["realized_pnl"] = round(ctx.round.realized_pnl or 0, 2)
        acct_d["fee_total"] = round(ctx.round.fee_total or 0, 2)
        self.emit("game:account", acct_d, room=f"round_{ctx.round.id}")
        return True

    # ── 结算 ──

    @_ensure_ctx
    def finish_round(self, round_id: int) -> (bool, str):
        """提前收盘结算"""
        with self._lock:
            r = GameRound.query.get(round_id)
            if not r:
                return False, "轮次不存在"
            if r.status not in (ST_RUNNING, ST_PAUSED):
                return False, "仅运行中的轮次可提前结束"
            return self._settle(round_id, reason="提前结束", auto=False), ""

    def _settle(self, round_id: int, reason: str, auto: bool = True) -> bool:
        """结算：final_assets = 现金 + 持仓×最后价；未成交委托作废

        注意: 调用方（finish_round/_process_tick/start_round/resume_round）均
        已处于 app context，此处不套 _ensure_ctx 以保证与调用方同一 session，
        query.get 能命中 identity map 返回与 ctx.round 同一实例。
        """
        with self._lock:
            r = GameRound.query.get(round_id)
            if not r:
                return False
            ctx = self._rounds.get(round_id)
            acct = ctx.acct if ctx and ctx.acct else None
            last_price = r.last_price or 0
            if acct:
                final = acct.total_assets(last_price)
                # 解冻未成交委托
                pending = GameOrder.query.filter_by(
                    round_id=round_id, status=O_PENDING).all()
                for o in pending:
                    if acct:
                        if o.direction == "buy":
                            acct.unfreeze_buy(o.price, o.shares)
                        else:
                            acct.unfreeze_sell(o.shares)
                    o.status = O_CANCELLED
                final = acct.total_assets(last_price)
            else:
                final = 0
            r.final_assets = round(final, 2)
            r.status = ST_FINISHED
            r.finished_at = datetime.now()
            if ctx:
                # 同步内存态（时钟据此停止推进）
                ctx.round.status = ST_FINISHED
                ctx.round.final_assets = round(final, 2)
                # 解冻后的账户快照落 Redis（重启后恢复一致）
                self._save_snapshot(ctx)
            self._attach_account_json(r, ctx)   # 结算后账户落库（DB 兜底）
            db.session.commit()
            self.emit("game:status", {"round_id": round_id, "status": ST_FINISHED,
                                      "reason": reason, "final_assets": final},
                      room=f"round_{round_id}")
            logger.info("轮次 %s 结算完成 reason=%s final=%.2f", round_id, reason, final)
            return True

    # ── 快照 ──

    def _save_snapshot(self, ctx: RoundContext):
        """保存账户/进度到 Redis"""
        cache = get_cache()
        if ctx.acct:
            d = ctx.acct.to_dict()
            d["last_price"] = ctx.acct.last_price
            cache.save_account(ctx.round.id, d)
        cache.save_progress(ctx.round.id, ctx.index)
        if ctx.round.last_price:
            cache.save_quote(str(ctx.round.id), {
                "code": ctx.round.code,
                "trade_date": ctx.round.trade_date,
                "time_key": ctx.round.last_time_key,
                "close": ctx.round.last_price,
                "last_close": ctx.ticks[0]["last_close"] if ctx.ticks else 0,
            })

    @staticmethod
    def _order_to_dict(o: GameOrder) -> dict:
        return {
            "order_id": o.order_id,
            "round_id": o.round_id,
            "code": o.code,
            "direction": o.direction,
            "order_type": o.order_type,
            "price": o.price,
            "shares": o.shares,
            "status": o.status,
            "filled_shares": o.filled_shares,
            "filled_price": o.filled_price,
            "fee": o.fee,
            "created_at": o.created_at.strftime("%Y-%m-%d %H:%M:%S") if o.created_at else "",
            "reject_reason": o.reject_reason,
        }


# 全局单例
_engine = None


def get_engine() -> GameEngine:
    global _engine
    if _engine is None:
        _engine = GameEngine()
    return _engine


def register_game_clock(scheduler):
    """注册全局时钟任务（幂等）"""
    engine = get_engine()
    scheduler.add_job(
        id="game_clock",
        func=engine.tick_all,
        trigger="interval",
        seconds=_CLOCK_INTERVAL,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("游戏时钟任务已注册 (interval=%ss)", _CLOCK_INTERVAL)
