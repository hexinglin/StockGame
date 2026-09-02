"""
模块名称: engine/game_engine.py
说明:    游戏核心 — 轮次管理 / 时钟推进 / 3s 数据复核撮合 / 结算
         一个轮次 = 一个交易日的完整游戏周期，账户按轮次独立跟踪
"""
import functools
import logging
import random
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

from ..dbdata.database import db
from ..dbdata.models import GameRound, GameOrder, GameTrade, TickData, TickDataSim
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

_TICK_SEC = 3.0          # 每根 tick 对应 3s 行情
_CLOCK_INTERVAL = 0.1    # 时钟推进周期（秒）


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

    # ── 可用交易日 ──

    def _tick_model(self, data_source: str):
        """按数据源返回行情模型：qmt → tick_data(实盘)，sim → tick_data_sim(转换模拟)"""
        return TickDataSim if data_source == "sim" else TickData

    @_ensure_ctx
    def _day_map(self, code: str) -> dict:
        """标的可用交易日来源映射（仅完整交易日）

        返回 {trade_date: {"qmt": bool, "sim": bool}}；完整性判断：
        该日最后一根行情时间 >= 15:00:00。
        """
        result = {}
        for model in (TickData, TickDataSim):
            rows = (
                db.session.query(model.trade_date, db.func.max(model.time_key))
                .filter(model.code == code)
                .group_by(model.trade_date)
                .all()
            )
            for trade_date, max_time in rows:
                if max_time and max_time >= f"{trade_date} 15:00:00":
                    key = "qmt" if model is TickData else "sim"
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

            # 手动级联删除（SQLAlchemy 不自动级联）
            GameOrder.query.filter_by(round_id=round_id).delete()
            GameTrade.query.filter_by(round_id=round_id).delete()
            db.session.delete(r)
            db.session.commit()

            # 清理 Redis
            cache = get_cache()
            cache.delete_account(round_id)
            cache.delete_progress(round_id)
            cache.delete_quote(str(round_id))
            return True, ""

    @_ensure_ctx
    def list_rounds(self) -> list:
        """轮次列表（含进度）"""
        rows = GameRound.query.order_by(GameRound.created_at.desc()).all()
        result = []
        for r in rows:
            ctx = self._rounds.get(r.id)
            progress = 0.0
            if ctx and ctx.ticks:
                progress = round(ctx.index / len(ctx.ticks) * 100, 1)
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
            # finished 轮次同样可从 Redis 快照恢复结算后账户（未删除轮次）
            acct = get_cache().load_account(round_id)

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
            "last_close": t.last_close or 0,
        } for t in ticks]

        # 进度恢复（后端重启场景）
        cache = get_cache()
        saved_index = cache.load_progress(round_id)
        ctx.index = saved_index if 0 < saved_index < len(ctx.ticks) else 0
        ctx.fraction = 0.0

        # 账户：优先恢复 Redis 快照，否则初始化
        acct_dict = cache.load_account(round_id)
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

        # 累计收益与手续费
        amount = fill_price * shares
        if direction == "buy":
            ctx.round.realized_pnl = (ctx.round.realized_pnl or 0) - (amount + fee)
        else:
            ctx.round.realized_pnl = (ctx.round.realized_pnl or 0) + (amount - fee)
        ctx.round.fee_total = (ctx.round.fee_total or 0) + fee
        # 订单/轮次为跨 context 的 detached 实例，需重新 attach 才能持久化
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
