"""
模块名称: engine/game_engine.py
说明:    游戏核心编排 — 轮次生命周期 / 时钟推进 / 撮合调度 / 结算。
         一个轮次 = 一个交易日的完整游戏周期，账户按轮次独立跟踪。

         纯逻辑与数据访问已拆分至：
         - matching.py     交易时段判定 + 成交判定（纯函数）
         - day_meta.py     game_days 天维度行情维护 / 昨收确定链
         - serializers.py  ORM → API/推送字典统一出口
         本模块只保留轮次状态机与上下文编排（加载/恢复/推进/落库）。
"""
import functools
import json
import logging
import random
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime

from ..dbdata.database import db
from ..dbdata.models import (GameRound, GameOrder, GameTrade, GameDay)
from ..messaging.cache import get_cache
from ..utils.config import Config
from ..utils.timeutil import now_cn
from .account import MockAccount
from .trade_analysis import analyze_game_trades
from . import day_meta, matching, serializers
from .grid_math import normalize_params, derive_gradient_rows

logger = logging.getLogger(__name__)

# 时段常量向后兼容再导出（game_routes 引用 _SESSION_DAY_END）
_SESSION_DAY_END = matching.SESSION_DAY_END
_session_of = matching.session_of

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


def _parse_game_time(time_key: str):
    """行情 time_key（游戏时间 'YYYY-MM-DD HH:MM:SS'）→ datetime

    委托/成交时间一律记游戏时间（行情时间轴），绝不使用真实时间——行情为
    历史交易日时订单同样按历史日期落库，与前端分时图时间轴保持一致；
    time_key 缺失/格式异常时返回 None（调用方自行兜底）。
    """
    if not time_key:
        return None
    try:
        return datetime.strptime(time_key, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        logger.warning("解析游戏时间失败: %r，订单时间记 None", time_key)
        return None


def _valid_price(v) -> bool:
    """有效价格：非空、>0 且非 NaN"""
    return day_meta._valid_price(v)


class RoundContext:
    """轮次运行时上下文（内存态）"""

    def __init__(self, round_row: GameRound):
        self.round = round_row
        self.ticks = []          # 预加载的 tick dict 列表
        self.index = 0           # 当前 tick 索引
        self.fraction = 0.0      # 时钟推进余数（支持变速）
        self.acct = None         # MockAccount
        self.pending = {}        # order_id -> GameOrder（pending 订单）
        self.lock = threading.RLock()
        self.cum_amount = 0.0    # 当日累计成交额（差分累加）
        self.cum_volume = 0      # 当日累计成交量


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

    # ── 配置 ──

    def _cfg(self):
        cfg = Config.get_instance()
        return {
            "base_shares": int(cfg.get("game.base_shares", 500000)),
            "initial_cash": float(cfg.get("game.initial_cash", 500000)),
            "fee_rate": float(cfg.get("game.fee_rate", 0.0001)),
            "stock_code": cfg.get("game.stock_code", "588000.SH"),
            # 网格表参数（Redis 覆盖优先，config.yaml game.grid 默认透底）
            "grid": self._grid_params(),
        }

    def _grid_params(self) -> dict:
        """网格参数：Redis 覆盖值优先，否则 config.yaml game.grid 默认值兜底"""
        cfg = Config.get_instance()
        override = get_cache().load_config("grid")
        raw = override if override else cfg.get("game.grid", None)
        return normalize_params(raw)

    def save_grid_params(self, params: dict) -> dict:
        """保存网格参数覆盖（写 Redis），返回归一化后的参数"""
        norm = normalize_params(params)
        get_cache().save_config("grid", norm)
        return norm

    @_ensure_ctx
    def save_grid_interval(self, round_id: int, idx: int, interval: int) -> (bool, str, dict):
        """保存某轮次某行的行级间隔，返回 (ok, msg, 间隔映射)

        间隔是人工微调「未成交出场腿」位置的手段：它只重算非主格号那侧（买入方向
        行重算卖出侧、卖出方向行重算买入侧），已成交的进场腿格号恒不动，故无需额外
        保护。以下两种行则整行不可调整，与前端置灰一致：
          - 该行已有未成交委托：委托价挂在当前网格线上，改动会使其脱节
          - 该行已完成（双腿均已成交）：历史既成事实
        """
        grid = self.get_grid(round_id)
        if grid is None:
            return False, "轮次不存在", {}
        row = next((x for x in grid["rows"] if x["idx"] == int(idx)), None)
        if row is None:
            return False, f"网格行 {idx} 不存在", {}
        if row.get("pending_order_id"):
            return False, "该行已有未成交委托，请先撤单再调整", {}
        if row.get("status") == "done":
            return False, "该行已完成（买卖均已成交），不可调整", {}
        ns = f"grid_int:{round_id}"
        imap = dict(get_cache().load_config(ns) or {})
        # 间隔合法域与前端下拉一致：1-6
        imap[str(int(idx))] = min(max(int(interval), 1), 6)
        get_cache().save_config(ns, imap)
        return True, "", imap

    # ── 可用交易日（委托 day_meta，保持原引擎接口）──

    def _tick_model(self, data_source: str):
        """按数据源返回行情模型（day_meta.tick_model 兼容出口）"""
        return day_meta.tick_model(data_source)

    @_ensure_ctx
    def available_dates(self, code: str, allow_sim: bool = False) -> list:
        """可用交易日列表（数据完整：最后一根 tick >= 15:00:00）"""
        return sorted(day_meta.date_items(code, allow_sim).keys())

    @_ensure_ctx
    def date_sources(self, code: str, allow_sim: bool = False) -> list:
        """可用交易日 + 数据来源标记（QMT 优先）: [{trade_date, source}]"""
        items = day_meta.date_items(code, allow_sim)
        return [{"trade_date": d, "source": s} for d, s in items.items()]

    @_ensure_ctx
    def date_details(self, code: str, allow_sim: bool = False) -> list:
        """可运行交易日 + 天维度原始行情（选择/开局界面用，QMT 优先）

        可开局判定与行情元数据（tick_count/OHLC/last_close 等）均来自
        game_days（权威源），不触碰 tick 行情表。
        """
        items = day_meta.date_items(code, allow_sim)
        if not items:
            return []
        klines = (GameDay.query.filter(
                  GameDay.code == code,
                  GameDay.trade_date.in_(list(items)),
                  GameDay.is_complete.is_(True)).all())
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

    def day_last_close(self, code: str, trade_date: str, data_source: str) -> float:
        """当日昨收（day_meta 委托；调用方须处于 app context）"""
        return day_meta.day_last_close(code, trade_date, data_source)

    def refresh_day(self, code: str, trade_date: str, data_source: str,
                    last_close_hint: float = 0.0, open_hint: float = 0.0) -> bool:
        """快照入库后同步维护 game_days（day_meta 委托；调用方须处于 app context）"""
        return day_meta.refresh_day(code, trade_date, data_source,
                                    last_close_hint, open_hint)

    def refresh_all_days(self, code: str = None, trade_date: str = None) -> int:
        """全量重建 game_days（day_meta 委托；调用方须处于 app context）"""
        return day_meta.refresh_all_days(code, trade_date)

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

        dates = day_meta.date_items(code, allow_sim)
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
            self._rounds.pop(round_id, None)

            # 手动级联删除（SQLAlchemy 不自动级联）：委托/成交/轮次行全部物理删除
            GameOrder.query.filter_by(round_id=round_id).delete()
            GameTrade.query.filter_by(round_id=round_id).delete()
            db.session.delete(r)
            db.session.commit()
            self._cleanup_round_cache(round_id)
            return True, ""

    @staticmethod
    def _cleanup_round_cache(round_id: int):
        """清理轮次相关 Redis（账户/进度/行情快照）"""
        cache = get_cache()
        cache.delete_account(round_id)
        cache.delete_progress(round_id)
        cache.delete_quote(str(round_id))

    @_ensure_ctx
    def list_rounds(self) -> list:
        """轮次列表（含进度）"""
        rows = GameRound.query.order_by(GameRound.created_at.desc()).all()
        return [serializers.round_brief(r, self._progress_of(r)) for r in rows]

    @_ensure_ctx
    def get_round(self, round_id: int):
        """轮次详情 + 账户 + 最新价"""
        r = GameRound.query.get(round_id)
        if not r:
            return None
        ctx = self._rounds.get(round_id)
        acct = self._round_account(r, ctx)
        acct = serializers.with_round_totals(acct, r)
        cum_amount, cum_volume = self._cum_totals(r, ctx)
        return serializers.round_detail(r, acct, cum_amount, cum_volume)

    def _round_account(self, r, ctx):
        """轮次账户字典：内存 ctx 优先，其次 Redis 快照，最后 DB JSON 兜底

        finished 轮次同样可从 Redis 快照恢复结算后账户（未删除轮次）。
        """
        if ctx and ctx.acct:
            return ctx.acct.to_dict()
        if r.status not in (ST_RUNNING, ST_PAUSED, ST_FINISHED):
            return None
        acct = get_cache().load_account(r.id)
        return acct if acct else self._account_from_json(r.account_json)

    def _cum_totals(self, r, ctx):
        """累计成交额/量（前端分时图恢复用；重启后按已推进区间重算）

        快照口径：当日累计量额 = <= last_time_key 的末条快照值
        （volume/amount 在 tick 表为当日累计值，不可再逐点求和）。
        """
        if ctx:
            return ctx.cum_amount, ctx.cum_volume
        if not r.last_time_key:
            return 0.0, 0
        m = self._tick_model(r.data_source)
        row = (db.session.query(m.amount, m.volume)
               .filter(m.code == r.code,
                       m.trade_date == r.trade_date,
                       m.time_key <= r.last_time_key)
               .order_by(m.time_key.desc()).first())
        return (row[0] or 0, row[1] or 0) if row else (0.0, 0)

    @_ensure_ctx
    def get_grid(self, round_id: int) -> dict:
        """网格表数据：梯度行 + 状态（由成交记录推导：建梯度/消梯度），供前端「网格表」tab 渲染

        初始化不再全量建梯子：梯度仅随成交产生——每笔成交先消（命中未消完
        梯度的对侧网格线）、消无可消或数量有多的再建（按成交价就近网格线
        新建，同方向同格号数量合并）。每行附带两侧真实成交价供前端对照。
        锚点价仍用于页面展示：昨收 > 最新成交价 > 引擎首根快照 close。
        """
        r = GameRound.query.get(round_id)
        if not r:
            return None

        params = self._cfg()["grid"]
        ctx = self._rounds.get(round_id)
        acct = self._grid_account(r, ctx)
        anchor = self._grid_anchor(r, ctx, acct)
        volume = int(acct.volume) if acct else int(r.base_shares or 0)
        interval_map = get_cache().load_config(f"grid_int:{round_id}") or {}
        rows = derive_gradient_rows(self.list_trades(round_id), params, interval_map)
        # 行 ↔ 未成交委托关联（一键下单记 grid_idx）：已挂单的行前端按钮置灰，
        # 委托撤单/成交/拒单后脱离 pending，行自动恢复可下单。
        # 匹配键 (grid_idx, 出场方向)：同格号可同时存在买卖两行，方向区分归属。
        pending_map = {}
        for o in GameOrder.query.filter_by(round_id=round_id,
                                           status=O_PENDING).all():
            if o.grid_idx is not None:
                pending_map[(o.grid_idx, o.direction)] = o.order_id
        for x in rows:
            # 出场腿方向与行方向相反：买入方向行（已购）→ 卖出；卖出方向行（已售）→ 买入
            out_dir = "sell" if x["direction"] == "buy" else "buy"
            x["pending_order_id"] = pending_map.get((x["idx"], out_dir))

        return {
            "round_id": round_id,
            "anchor_price": anchor,
            "last_price": r.last_price,
            "total_shares": volume,
            "params": params,
            "rows": [serializers.grid_row_to_dict(x) for x in rows],
        }

    def _grid_account(self, r, ctx):
        """网格推导用账户（内存 ctx 或 Redis 快照重建；仅取持仓量）"""
        if ctx and ctx.acct:
            return ctx.acct
        if r.status in (ST_RUNNING, ST_PAUSED, ST_FINISHED):
            acct_dict = get_cache().load_account(r.id)
            if acct_dict:
                return MockAccount.from_dict(acct_dict)
        return None

    def _grid_anchor(self, r, ctx, acct) -> float:
        """网格锚点价（昨收优先，其次最新成交价/首根快照/账户最新价）"""
        anchor = self.day_last_close(r.code, r.trade_date, r.data_source or "qmt")
        if not _valid_price(anchor):
            anchor = r.last_price or 0
        if not _valid_price(anchor) and ctx and ctx.ticks:
            anchor = float(ctx.ticks[ctx.index]["close"] or 0)
        if not _valid_price(anchor) and acct and _valid_price(acct.last_price):
            anchor = float(acct.last_price)
        return round(float(anchor), 3)

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
        ticks, msg = self._load_ticks(r)
        if not ticks:
            return None, msg
        ctx.ticks = ticks
        self._restore_progress(ctx)
        self._restore_cum(ctx)
        self._restore_account(ctx)
        self._restore_pending(ctx)
        return ctx, ""

    @staticmethod
    def _restore_cum(ctx: RoundContext):
        """当日累计量额恢复：对已推进区间的差分序列求和（与前端 REST 恢复
        rebuildMinuteSeries 的差分累加、以及末条快照累计值三者同口径）

        上下文重载（后端重启 / 暂停后继续 / 从非零进度开局）时若从 0 起算，
        实时推送的累计量额会小于当日真实值——表现为进场后数字突然回落。
        """
        upto = ctx.ticks[:ctx.index]
        ctx.cum_amount = sum(t.get("amount") or 0 for t in upto)
        ctx.cum_volume = sum(t.get("volume") or 0 for t in upto)

    def _load_ticks(self, r) -> (list, str):
        """加载该日 tick 并转为播放序列（差分口径），失败返回 ([], 原因)

        过滤盘后数据（> 15:00:00，如固定价交易尾巴）：游戏只播盘前集合
        竞价 / 盘中连续 / 收盘集合竞价（末根为 15:00:00 收盘价）。
        DB 存当日累计量额（单调不减），逐点预转为与上一快照的差分（首条=
        原值），供 _process_tick 差分累加回当日累计（cum）；high/low 为
        快照滚动极值、close 为最新价，原样保留。
        """
        m = self._tick_model(r.data_source)
        day_end = f"{r.trade_date} {_SESSION_DAY_END}"
        ticks = (
            db.session.query(m)
            .filter(m.code == r.code, m.trade_date == r.trade_date,
                    m.time_key <= day_end)
            .order_by(m.time_key)
            .all()
        )
        if not ticks:
            return [], f"交易日 {r.trade_date} 无 {r.code} 行情数据"
        # 当日常量（昨收 + 今开）统一写入每根 tick，保证推送给前端的昨收/
        # 今开恒为有效数值（与 REST ticks 恢复接口同口径）
        return self.normalize_day_constants(
            self._diff_ticks(ticks), r.code, r.trade_date, r.data_source), ""

    @staticmethod
    def _diff_ticks(ticks) -> list:
        """快照 ORM 列表 → 差分播放序列（volume/amount 转为与上一快照之差）"""
        result = []
        prev_v = prev_a = 0
        for t in ticks:
            v = t.volume or 0
            a = t.amount or 0
            result.append({
                "time_key": t.time_key,
                "high": t.high, "low": t.low, "close": t.close,
                "volume": max(0, v - prev_v),
                "amount": max(0, a - prev_a),
            })
            prev_v, prev_a = v, a
        return result

    def _restore_progress(self, ctx: RoundContext):
        """进度恢复：优先 Redis 快照；缺失/不可用时以 DB last_time_key
        反推已推进位置——行情永久存于 tick 表，进度不丢即可完整恢复"""
        r = ctx.round
        saved_index = get_cache().load_progress(r.id)
        if not (0 < saved_index < len(ctx.ticks)):
            keys = [t["time_key"] for t in ctx.ticks]
            saved_index = (next((i for i, k in enumerate(keys)
                                 if k > r.last_time_key), len(keys))
                           if r.last_time_key else 0)
        ctx.index = saved_index if 0 < saved_index < len(ctx.ticks) else 0
        ctx.fraction = 0.0

    def _restore_account(self, ctx: RoundContext):
        """账户恢复：Redis 快照 > DB 账户 JSON（交易事件随行落库），均缺失才
        按初始参数初始化（与委托/成交记录保持一致的兜底链）"""
        r = ctx.round
        acct_dict = get_cache().load_account(r.id)
        if not acct_dict:
            acct_dict = self._account_from_json(r.account_json)
        if acct_dict:
            ctx.acct = MockAccount.from_dict(acct_dict)
            return
        first = ctx.ticks[0]
        ctx.acct = MockAccount(
            base_shares=r.base_shares or None,
            initial_cash=r.initial_cash or None,
            fee_rate=None,
            open_price=first["close"],
        )
        r.initial_assets = ctx.acct.total_assets(first["close"])

    @staticmethod
    def _restore_pending(ctx: RoundContext):
        """恢复 pending 订单（重启场景）"""
        pending_orders = GameOrder.query.filter_by(
            round_id=ctx.round.id, status=O_PENDING).all()
        ctx.pending = {o.order_id: o for o in pending_orders}

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
        记录一致的账户状态。round_row 须为当前 session 已 attach 的实例。
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
        total = self._count_ticks(m, r)
        if not total:
            return 0.0
        done = self._count_ticks(m, r, upto=r.last_time_key)
        return round(done / total * 100, 1)

    @staticmethod
    def _count_ticks(m, r, upto: str = None) -> int:
        """统计轮次当日 tick 总数（upto 给定时统计 <= upto 的条数）"""
        q = (db.session.query(db.func.count())
             .filter(m.code == r.code, m.trade_date == r.trade_date))
        if upto:
            q = q.filter(m.time_key <= upto)
        return q.scalar() or 0

    def normalize_day_constants(self, ticks: list, code: str, trade_date: str,
                                data_source: str) -> list:
        """当日常量填充（调用方须处于 app context，勿套 _ensure_ctx）

        昨收（上一交易日收盘）与今开（当日开盘）为日维度常量，由 game_days
        表在行情入库时维护。此处读取当日记录值并统一写入每根 tick dict，
        保证引擎 _load_context 与 REST ticks 恢复接口同口径；当日昨收无
        有效值时以前一完整交易日的 close 兑底（绝不用当日价格充当基准），
        今开缺失时以首条快照 close 兑底。
        """
        base_close = self.day_last_close(code, trade_date, data_source)
        day = day_meta.day_row(code, trade_date, data_source)
        day_open = 0.0
        if day is not None and _valid_price(day.open):
            day_open = float(day.open)
        if not _valid_price(day_open) and ticks:
            day_open = float(ticks[0]["close"] or 0)
        for t in ticks:
            t["last_close"] = base_close
            t["open"] = day_open
        return ticks

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
            return self._activate_round(round_id, r, "启动")

    @_ensure_ctx
    def resume_round(self, round_id: int) -> (bool, str):
        """继续游戏（暂停 → 运行；后端重启后内存上下文从 DB/Redis 恢复）"""
        with self._lock:
            r = GameRound.query.get(round_id)
            if not r or r.status != ST_PAUSED:
                return False, "仅暂停的轮次可继续"
            return self._activate_round(round_id, r, "恢复运行")

    def _activate_round(self, round_id: int, r, action: str) -> (bool, str):
        """置轮次为运行态（start/resume 共用：加载 ctx → 尾端即结算 → 置 running）"""
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

        # r 与 ctx.round 跨请求时是不同 ORM 实例（各自 session 的 identity
        # map）：r 写库，ctx.round 同步内存态（时钟 tick_all 据此推进）
        # r 与 ctx.round 跨请求时是不同 ORM 实例（各自 session 的 identity
        # map）：r 写库，ctx.round 同步内存态（时钟 tick_all 据此推进）
        ctx.round.status = ST_RUNNING
        r.status = ST_RUNNING
        r.started_at = r.started_at or now_cn()
        self._attach_account_json(r, ctx)   # 初始/恢复后的账户随行落库
        db.session.commit()
        self._save_snapshot(ctx)
        self.emit("game:status", {"round_id": round_id, "status": ST_RUNNING},
                  room=f"round_{round_id}")
        logger.info("轮次 %s %s date=%s ticks=%d index=%d",
                    round_id, action, r.trade_date, len(ctx.ticks), ctx.index)
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
    def set_speed(self, round_id: int, speed: int) -> (bool, str):
        if speed not in (1, 5, 10, 60):
            return False, "speed 仅支持 1/5/10/60"
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
                    price: float, shares: int,
                    grid_idx: int = None) -> (bool, dict, str):
        """下单（下单即冻结；市价单按最新价预估冻结），返回 (ok, order_dict, message)

        grid_idx: 网格一键下单时携带的网格行主格号（委托与网格行关联，
        供 get_grid 推导行级 pending_order_id / 按钮置灰）；普通下单为 None。
        """
        ok, msg = self._validate_order(direction, order_type, price, shares)
        if not ok:
            return False, None, msg

        with self._lock:
            r, ctx, err = self._orderable_context(round_id)
            if err:
                return False, None, err

            acct = ctx.acct
            # 市价单用最新价预估
            if order_type == "market":
                if not acct.last_price or acct.last_price <= 0:
                    return False, None, "暂无最新价，无法下市价单"
                price = acct.last_price

            # 下单即冻结
            ok, msg = self._freeze_for(acct, direction, price, shares)
            if not ok:
                return False, None, msg

            order = self._new_order(ctx, r, direction, order_type, price, shares,
                                    grid_idx=grid_idx)
            db.session.add(order)
            self._attach_account_json(r, ctx)   # 冻结后的账户落库（DB 兜底）
            db.session.commit()

            ctx.pending[order.order_id] = order
            self._save_snapshot(ctx)
            self.emit("game:order_update", serializers.order_to_dict(order),
                      room=f"round_{round_id}")
            return True, serializers.order_to_dict(order), ""

    @staticmethod
    def _validate_order(direction: str, order_type: str,
                        price: float, shares: int) -> (bool, str):
        """下单参数校验"""
        if direction not in ("buy", "sell"):
            return False, "direction 仅支持 buy/sell"
        if order_type not in ("limit", "market"):
            return False, "order_type 仅支持 limit/market"
        if shares <= 0 or shares % 100 != 0:
            return False, "数量必须为 100 股（0.01 万股）的整数倍"
        if order_type == "limit" and (not price or price <= 0):
            return False, "限价单价格必须大于 0"
        return True, ""

    def _orderable_context(self, round_id: int):
        """取可下单的轮次与上下文（须运行/暂停且账户已初始化）"""
        r = GameRound.query.get(round_id)
        if not r:
            return None, None, "轮次不存在"
        if r.status not in (ST_RUNNING, ST_PAUSED):
            return None, None, "轮次未在运行中（需先开始游戏）"
        ctx = self._rounds.get(round_id)
        if ctx is None or ctx.acct is None:
            return None, None, "轮次上下文未初始化，请重新开始"
        return r, ctx, None

    @staticmethod
    def _freeze_for(acct, direction: str, price: float, shares: int) -> (bool, str):
        """下单冻结资金/持仓（买冻结含手续费金额，卖冻结可卖持仓）"""
        if direction == "buy":
            if not acct.freeze_buy(price, shares):
                return False, "可用资金不足（含手续费）"
        else:
            if not acct.freeze_sell(shares):
                return False, "可卖持仓不足"
        return True, ""

    @staticmethod
    def _new_order(ctx: RoundContext, r, direction: str, order_type: str,
                   price: float, shares: int, grid_idx: int = None) -> GameOrder:
        """构造委托单；委托时间记游戏时间（最后已播出快照的 time_key，
        尚未开播时以首根快照时间兑底），与行情时间轴一致而非真实时间"""
        order_id = "R%d_%s" % (r.id, uuid.uuid4().hex[:12].upper())
        game_now = ctx.round.last_time_key or ctx.ticks[0]["time_key"]
        # 买单冻结额随单落库（卖单为 0）
        frozen = ctx.acct.frozen_amount(price, shares) if direction == "buy" else 0
        return GameOrder(
            order_id=order_id,
            round_id=r.id,
            code=r.code,
            direction=direction,
            order_type=order_type,
            price=price,
            shares=shares,
            grid_idx=grid_idx,
            status=O_PENDING,
            created_at=_parse_game_time(game_now),
            frozen_amount=frozen,
        )

    @_ensure_ctx
    def place_grid_order(self, round_id: int, idx: int) -> (bool, dict, str):
        """网格行一键下单：按行出场腿自动构造限价委托（委托记 grid_idx 关联行）

        仅出场腿待成交的行（已购/已售）可下：买入方向行（已购）挂卖点价卖出、
        卖出方向行（已售）挂买点价买回，方向/价格/数量全部由网格行推导。
        该行已有未成交委托时拒绝（前端按钮置灰）；撤单（未成交）后恢复可下。
        """
        grid = self.get_grid(round_id)
        if not grid:
            return False, None, "轮次不存在"
        row = next((x for x in grid["rows"] if x["idx"] == int(idx)), None)
        if row is None:
            return False, None, f"网格行 {idx} 不存在"
        if row["status"] not in ("buy", "sell"):
            return False, None, "仅已购/已售（出场腿待成交）的行可一键下单"
        if row.get("pending_order_id"):
            return False, None, "该行已有未成交委托，请先撤单"
        # 出场腿方向与行方向相反：买入方向行（已购）→ 卖出；卖出方向行（已售）→ 买入
        direction = "sell" if row["direction"] == "buy" else "buy"
        price = row["sell_price"] if direction == "sell" else row["buy_price"]
        shares = int(row["shares"] or 0) // 100 * 100
        if shares <= 0:
            return False, None, "该行分摊持仓不足 100 股，无法委托"
        return self.place_order(round_id, direction=direction, order_type="limit",
                                price=price, shares=shares, grid_idx=int(idx))

    @_ensure_ctx
    def cancel_order(self, round_id: int, order_id: str) -> (bool, str):
        """撤委托单（运行中随时可撤，仅 pending 状态；收盘集合竞价只挂不撤）"""
        with self._lock:
            r = GameRound.query.get(round_id)
            if not r:
                return False, "轮次不存在"
            if r.status not in (ST_RUNNING, ST_PAUSED):
                return False, "仅运行中的轮次可撤单"
            ctx = self._rounds.get(round_id)
            order = GameOrder.query.filter_by(round_id=round_id,
                                              order_id=order_id).first()
            if not order:
                return False, "委托不存在"
            if order.status != O_PENDING:
                return False, f"仅 pending 状态可撤（当前: {order.status}）"
            # 收盘集合竞价时段（14:57-15:00）只挂不撤（与真实收盘集合竞价
            # 规则一致）：防止盘中挂单在收盘竞价被撤走、影响收盘竞价撮合
            if r.last_time_key and _session_of(r.last_time_key) == "closing":
                return False, "收盘集合竞价时段（14:57-15:00）不可撤单"

            # 解冻
            if ctx and ctx.acct:
                self._unfreeze_order(ctx.acct, order)

            order.status = O_CANCELLED
            self._attach_account_json(r, ctx)   # 解冻后的账户落库（DB 兜底）
            db.session.commit()
            if ctx:
                ctx.pending.pop(order_id, None)
                self._save_snapshot(ctx)
            self.emit("game:order_update", serializers.order_to_dict(order),
                      room=f"round_{round_id}")
            return True, ""

    @staticmethod
    def _unfreeze_order(acct, order):
        """按委托方向解冻（撤单/结算作废共用）"""
        if order.direction == "buy":
            acct.unfreeze_buy(order.price, order.shares)
        else:
            acct.unfreeze_sell(order.shares)

    @_ensure_ctx
    def list_orders(self, round_id: int) -> list:
        rows = (GameOrder.query.filter_by(round_id=round_id)
                .order_by(GameOrder.created_at.desc()).all())
        return [serializers.order_to_dict(o) for o in rows]

    @_ensure_ctx
    def list_trades(self, round_id: int) -> list:
        rows = (GameTrade.query.filter_by(round_id=round_id)
                .order_by(GameTrade.id.desc()).all())
        return [serializers.trade_to_dict(t) for t in rows]

    @_ensure_ctx
    def analyze_round(self, round_id: int) -> dict:
        """轮次成交的「同日最大收益配对」收益分析（与真实交易记录同一套配对规则）

        配对规则完全复用真实记录分析（卖取最高价、买取最低价逐量对消，配满
        min(买量, 卖量)，余量列入无法匹配）；差别仅在手续费口径——游戏由引擎按
        模拟费率逐笔计费并落库，故直接取记录值，而非真实券商的「按委托 min 5 元」。

        返回配对结果外附轮次口径对照（引擎已实现盈亏/手续费合计/期初资产）与
        每日收益率，便于玩家比对「最大收益配对」与「持仓成本法」两种口径差异。
        """
        r = GameRound.query.get(round_id)
        if not r:
            return None
        records = [{
            "time": t["trade_time"], "code": t["code"], "direction": t["direction"],
            "price": t["price"], "volume": t["shares"],
            "amount": round((t["price"] or 0) * (t["shares"] or 0), 2),
            "fee": t["fee"], "order_id": t["order_id"], "trade_id": str(t["id"]),
        } for t in self.list_trades(round_id)]
        out = analyze_game_trades(records)
        init_assets = float(r.initial_assets or 0)
        net = out["summary"]["net_profit"]
        out["round"] = {
            "round_id": round_id,
            "trade_date": r.trade_date,
            "code": r.code,
            "status": r.status,
            "initial_assets": round(init_assets, 2),
            "realized_pnl": round(r.realized_pnl or 0, 2),   # 引擎口径（持仓成本法）
            "fee_total": round(r.fee_total or 0, 2),
            # 每日收益率：配对净收益 ÷ 期初资产（期初缺失时不计算）
            "return_rate": round(net / init_assets * 100, 4) if init_assets > 0 else None,
            "last_time_key": r.last_time_key or "",
        }
        return out

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
        """按速度推进轮次时钟：每 0.1s 周期推进 speed×0.1 根 tick"""
        speed = ctx.round.speed or 1
        with ctx.lock:
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
            # 周期末统一存一次快照（最新进度/账户/行情）
            self._save_snapshot(ctx)

    @_ensure_ctx
    def _process_tick(self, ctx: RoundContext, tick: dict):
        """处理一个快照点：更新行情 → 撮合 → 持久化 → 尾端结算"""
        r = ctx.round
        self._apply_tick_state(ctx, tick)
        self.emit("game:quote",
                  serializers.quote_payload(r, tick, ctx.cum_amount,
                                            ctx.cum_volume,
                                            self._tick_progress(ctx)),
                  room=f"round_{r.id}")

        filled_any = self._match_pending(ctx, tick)

        self._persist_tick(ctx, filled_any)
        if ctx.index >= len(ctx.ticks) - 1:
            self._settle(ctx.round.id, reason="收盘", auto=True)

    def _apply_tick_state(self, ctx: RoundContext, tick: dict):
        """推进最新价与当日累计量额（前端均价线用）"""
        ctx.acct.last_price = tick["close"]
        ctx.round.last_price = tick["close"]
        ctx.round.last_time_key = tick["time_key"]
        ctx.cum_amount += tick.get("amount") or 0
        ctx.cum_volume += tick.get("volume") or 0

    @staticmethod
    def _tick_progress(ctx: RoundContext) -> float:
        """当前 tick 推进进度（%）"""
        return round(ctx.index / len(ctx.ticks) * 100, 1) if ctx.ticks else 0

    def _match_pending(self, ctx: RoundContext, tick: dict) -> bool:
        """撮合所有 pending 订单（按交易时段分流），返回是否有订单被处理

        - 盘中连续竞价（intraday）：每根 tick 以最新价触及即成交（默认口径）
        - 开盘/收盘集合竞价点（auction）：以竞价价（=该 tick 最新价）集中撮合
        - 竞价等待期（盘前 09:25 前、收盘 14:57-15:00 未到 15:00）：只挂不撮
        """
        session = _session_of(tick["time_key"])
        if session == "intraday":
            return self._sweep_pending(ctx, tick, auction_price=None)
        if matching.is_auction_point(tick["time_key"]):
            return self._sweep_pending(ctx, tick, auction_price=tick["close"])
        return False    # 竞价等待期——只挂不撮，跳过

    def _sweep_pending(self, ctx: RoundContext, tick: dict,
                       auction_price: float = None) -> bool:
        """遍历 pending 订单逐笔尝试撮合，清理已终结订单，返回是否有成交/拒单"""
        filled_any = False
        for order_id, order in list(ctx.pending.items()):
            if order.status != O_PENDING:
                ctx.pending.pop(order_id, None)
                continue
            if self._try_fill(ctx, order, tick, auction_price=auction_price):
                filled_any = True
                ctx.pending.pop(order_id, None)
        return filled_any

    def _persist_tick(self, ctx: RoundContext, filled_any: bool):
        """无成交 tick 的周期性持久化轮次行情（last_price/last_time_key）

        高速档每 tick 都 commit 远程库开销极大（x60≈60 次/秒），改为每 10
        tick 一次；但收盘 tick 必须落库，确保随后的 _settle 读到最新
        last_price 计算期末资产。成交/暂停/结算路径均各自 commit，且 Redis
        快照每周期存进度，崩溃恢复优先用快照，DB 行情滞后几 tick 可接受。
        """
        is_last = ctx.index >= len(ctx.ticks) - 1
        if not filled_any and (ctx.index % 10 == 0 or is_last):
            db.session.add(ctx.round)
            db.session.commit()

    @_ensure_ctx
    def _try_fill(self, ctx: RoundContext, order: GameOrder, tick: dict,
                  auction_price: float = None) -> bool:
        """尝试撮合一笔委托，订单被处理（成交或拒单）返回 True

        成交判定委托 matching.decide_fill（连续竞价以最新价触及即成交、
        集合竞价以竞价价撮合，详见其文档）。
        """
        acct = ctx.acct
        if order.status != O_PENDING:
            return False

        filled, fill_price = matching.decide_fill(
            order.direction, order.order_type, order.price,
            tick["close"], auction_price)
        if not filled:
            return False

        # 成交校验（市价单成交价可能高于冻结预估）→ 冻结不足则拒单
        if order.direction == "buy":
            amount = fill_price * order.shares + acct.fee_for(fill_price * order.shares)
            if order.frozen_amount < amount - 0.001:
                self._reject_order(ctx, order, fill_price)
                return True
            fee = acct.fill_buy(fill_price, order.shares,
                                frozen_amount=order.frozen_amount)
        else:
            fee = acct.fill_sell(fill_price, order.shares)

        self._record_fill(ctx, order, fill_price, fee, tick)
        self._emit_fill(ctx, order, fill_price, fee, tick)
        return True

    def _reject_order(self, ctx: RoundContext, order: GameOrder,
                      fill_price: float):
        """拒单：冻结不足，解冻转回可用现金并落库推送"""
        acct = ctx.acct
        acct.unfreeze_buy(order.price, order.shares)
        order.status = O_REJECTED
        order.reject_reason = f"市价成交价 {fill_price} 超出冻结额"
        # 订单/轮次为跨 context 的 detached 实例，需重新 attach 才能持久化
        self._attach_account_json(ctx.round, ctx)
        db.session.add(order)
        db.session.add(ctx.round)
        db.session.commit()
        self.emit("game:order_update", serializers.order_to_dict(order),
                  room=f"round_{ctx.round.id}")

    def _record_fill(self, ctx: RoundContext, order: GameOrder,
                     fill_price: float, fee: float, tick: dict):
        """成交落库：成交记录 + 委托回填 + 轮次级盈亏/手续费累计"""
        acct = ctx.acct
        r = ctx.round
        trade = GameTrade(
            round_id=r.id,
            order_id=order.order_id,
            code=order.code,
            direction=order.direction,
            price=fill_price,
            shares=order.shares,
            fee=fee,
            trade_time=tick["time_key"],
        )
        db.session.add(trade)
        order.status = O_FILLED
        order.filled_shares = order.shares
        order.filled_price = fill_price
        order.fee = fee
        # 成交时间记游戏时间（当前撮合快照的 time_key），与 trade_time 同源
        order.filled_at = _parse_game_time(tick["time_key"])

        # 累计已实现盈亏与手续费：买入仅手续费为已实现亏损（本金转持仓）；
        # 卖出为 (卖出价 - 持仓成本) × 数量 - 手续费
        if order.direction == "buy":
            r.realized_pnl = (r.realized_pnl or 0) - fee
        else:
            r.realized_pnl = ((r.realized_pnl or 0)
                              + (fill_price - acct.avg_price) * order.shares - fee)
        r.fee_total = (r.fee_total or 0) + fee
        # 订单/轮次为跨 context 的 detached 实例，需重新 attach 才能持久化
        self._attach_account_json(r, ctx)
        db.session.add(order)
        db.session.add(r)
        db.session.commit()

    def _emit_fill(self, ctx: RoundContext, order: GameOrder,
                   fill_price: float, fee: float, tick: dict):
        """成交推送：委托更新 + 成交流水 + 账户（补充轮次级盈亏/手续费）"""
        r = ctx.round
        room = f"round_{r.id}"
        self.emit("game:order_update", serializers.order_to_dict(order), room=room)
        self.emit("game:trade",
                  serializers.trade_payload(order, fill_price, fee,
                                            tick["time_key"],
                                            r.realized_pnl or 0,
                                            r.fee_total or 0),
                  room=room)
        acct_d = ctx.acct.to_dict()
        acct_d["realized_pnl"] = round(r.realized_pnl or 0, 2)
        acct_d["fee_total"] = round(r.fee_total or 0, 2)
        self.emit("game:account", acct_d, room=room)

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

        注意: 调用方（finish_round/_process_tick/_activate_round）均
        已处于 app context，此处不套 _ensure_ctx 以保证与调用方同一 session，
        query.get 能命中 identity map 返回与 ctx.round 同一实例。
        """
        with self._lock:
            r = GameRound.query.get(round_id)
            if not r:
                return False
            ctx = self._rounds.get(round_id)
            acct = ctx.acct if ctx and ctx.acct else None
            final = self._cancel_pending_orders(r, acct)
            r.final_assets = round(final, 2)
            r.status = ST_FINISHED
            r.finished_at = now_cn()
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

    def _cancel_pending_orders(self, r, acct) -> float:
        """作废全部未成交委托并解冻，返回解冻后期末总资产（无账户返回 0）"""
        if not acct:
            return 0.0
        for o in GameOrder.query.filter_by(round_id=r.id,
                                           status=O_PENDING).all():
            self._unfreeze_order(acct, o)
            o.status = O_CANCELLED
        return acct.total_assets(r.last_price or 0)

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

    def _order_to_dict(self, o: GameOrder) -> dict:
        """委托序列化（serializers 委托，保留引擎侧兼容出口）"""
        return serializers.order_to_dict(o)


# 全局单例
_engine = None
_engine_lock = threading.Lock()


def get_engine() -> GameEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
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
