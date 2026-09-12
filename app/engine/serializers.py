"""
模块名称: engine/serializers.py
说明:    ORM → API/推送字典的统一出口（轮次/委托/成交/账户/行情）。
         REST 响应与 socket 推送共用此处组装，保证字段口径一致；
         原先散落在 game_engine 各方法内的 20+ 行 dict 字面量全部收拢于此。
"""
from ..utils.timeutil import fmt_cn


def _fmt(dt) -> str:
    """datetime → 'YYYY-MM-DD HH:MM:SS'（None → ''）"""
    return fmt_cn(dt) if dt else ""


def round_brief(r, progress: float) -> dict:
    """轮次列表项（list_rounds 用）"""
    return {
        "id": r.id,
        "code": r.code,
        "trade_date": r.trade_date,
        "status": r.status,
        "speed": r.speed,
        "data_source": r.data_source or "qmt",
        "created_at": _fmt(r.created_at),
        "initial_cash": r.initial_cash,
        "base_shares": r.base_shares,
        "initial_assets": round(r.initial_assets or 0, 2),
        "final_assets": round(r.final_assets or 0, 2),
        "realized_pnl": round(r.realized_pnl or 0, 2),
        "fee_total": round(r.fee_total or 0, 2),
        "last_price": r.last_price,
        "last_time_key": r.last_time_key,
        "progress": progress,
    }


def round_detail(r, acct: dict, cum_amount: float, cum_volume: int) -> dict:
    """轮次详情（get_round 用）；acct 为补充过轮次级盈亏的账户字典或 None"""
    return {
        "id": r.id,
        "code": r.code,
        "trade_date": r.trade_date,
        "status": r.status,
        "speed": r.speed,
        "data_source": r.data_source or "qmt",
        "created_at": _fmt(r.created_at),
        "started_at": _fmt(r.started_at) or None,
        "finished_at": _fmt(r.finished_at) or None,
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


def with_round_totals(acct: dict, r) -> dict:
    """账户字典补充轮次级已实现盈亏/手续费（与 game:account 推送同口径），
    使 REST /account 与 socket 推送字段一致，前端统一读 realized_pnl/fee_total"""
    if acct is not None:
        acct["realized_pnl"] = round(r.realized_pnl or 0, 2)
        acct["fee_total"] = round(r.fee_total or 0, 2)
    return acct


def order_to_dict(o) -> dict:
    """委托记录 → 推送/列表字典"""
    return {
        "order_id": o.order_id,
        "round_id": o.round_id,
        "code": o.code,
        "direction": o.direction,
        "order_type": o.order_type,
        "price": o.price,
        "shares": o.shares,
        # 网格行主格号（网格一键下单关联；普通下单为 None）
        "grid_idx": o.grid_idx,
        "status": o.status,
        "filled_shares": o.filled_shares,
        "filled_price": o.filled_price,
        "fee": o.fee,
        "created_at": _fmt(o.created_at),
        "reject_reason": o.reject_reason,
    }


def trade_to_dict(t) -> dict:
    """成交记录 → 列表字典"""
    return {
        "id": t.id,
        "order_id": t.order_id,
        "code": t.code,
        "direction": t.direction,
        "price": t.price,
        "shares": t.shares,
        "fee": t.fee,
        "trade_time": t.trade_time,
    }


def quote_payload(r, tick: dict, cum_amount: float, cum_volume: int,
                  progress: float) -> dict:
    """game:quote 推送载荷（分时图数据源）"""
    return {
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
        "cum_amount": round(cum_amount, 2),
        "cum_volume": cum_volume,
        "progress": progress,
    }


def trade_payload(order, fill_price: float, fee: float, tick_time_key: str,
                  realized_pnl: float, fee_total: float) -> dict:
    """game:trade 推送载荷"""
    return {
        "order_id": order.order_id, "code": order.code,
        "direction": order.direction,
        "price": fill_price, "shares": order.shares, "fee": fee,
        "trade_time": tick_time_key,
        "realized_pnl": round(realized_pnl, 2),
        "fee_total": round(fee_total, 2),
    }


def grid_row_to_dict(x: dict) -> dict:
    """网格梯度行 → 前端「网格表」渲染字典"""
    return {
        "idx": x["idx"],
        "direction": x["direction"],
        "buy_idx": x["buy_idx"],
        "sell_idx": x["sell_idx"],
        "interval": x["interval"],
        "buy_price": x["buy_price"],
        "sell_price": x["sell_price"],
        "buy_fill_price": x.get("buy_fill_price"),
        "sell_fill_price": x.get("sell_fill_price"),
        "shares": x["shares"],
        "status": x["status"],
        "buy_hit": x["buy_hit"],
        "sell_hit": x["sell_hit"],
        # 该行出场腿的未成交委托 id（一键下单置灰用；无挂单为 None）
        "pending_order_id": x.get("pending_order_id"),
    }
