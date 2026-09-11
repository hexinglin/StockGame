"""
模块名称: engine/trade_analysis.py
说明:    QMT 实盘交易记录分析 — 手续费计算 + 同日买卖 FIFO 配对收益

配对口径（用于核对"当日实际收益"）：
  - 同一标的按成交时间升序做 FIFO 配对：每笔卖出依次与最早未配完的买入配对；
  - 单笔手续费 = max(成交金额 × 万分之 0.85, 5 元)，买卖双边各计（不免 5）；
  - 配对收益 = 卖出部分金额 − 买入部分金额 − 买卖两笔手续费（按配对数量占比分摊）；
  - 剩余未配对的买入（未卖出留仓）与卖出（卖出昨日持仓、无同日买入成本）
    列入"无法匹配记录"，仅展示不参与当日收益计算。
"""
import logging

logger = logging.getLogger(__name__)

FEE_RATE = 0.000085     # 佣金费率：万分之 0.85（单边）
MIN_FEE = 5.0           # 最低手续费：5 元（不免 5）


def calc_fee(amount) -> float:
    """单笔手续费 = max(成交金额 × FEE_RATE, MIN_FEE)，四舍五入到分"""
    try:
        amount = max(0.0, float(amount or 0))
    except (TypeError, ValueError):
        amount = 0.0
    return round(max(amount * FEE_RATE, MIN_FEE), 2)


def _norm_record(rec: dict, idx: int) -> dict:
    """归一化一条成交记录（容错：金额缺失按 价 × 量 兜底）"""
    if not isinstance(rec, dict):
        return None
    try:
        price = float(rec.get("price") or 0)
        volume = int(rec.get("volume") or 0)
    except (TypeError, ValueError):
        return None
    if volume <= 0:
        return None
    try:
        amount = float(rec.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount <= 0:
        amount = price * volume
    direction = str(rec.get("direction") or "").lower()
    return {
        "time": str(rec.get("time") or ""),
        "code": str(rec.get("code") or ""),
        "name": str(rec.get("name") or ""),
        "direction": direction,
        "price": price,
        "volume": volume,
        "amount": round(amount, 2),
        "trade_id": str(rec.get("trade_id") or ""),
        "order_id": str(rec.get("order_id") or ""),
        "_idx": idx,
    }


def _ratio(rec: dict, qty: int) -> float:
    """配对数量占该笔成交的比例（用于金额/手续费分摊）"""
    vol = rec.get("volume") or 0
    return (qty / vol) if vol > 0 else 0.0


def _make_pair(buy: dict, sell: dict, qty: int) -> dict:
    """构造一个配对（金额与手续费按数量占比分摊）"""
    rb, rs = _ratio(buy, qty), _ratio(sell, qty)
    buy_amount = round(buy["amount"] * rb, 2)
    sell_amount = round(sell["amount"] * rs, 2)
    buy_fee = round(buy["fee"] * rb, 2)
    sell_fee = round(sell["fee"] * rs, 2)
    gross = round(sell_amount - buy_amount, 2)
    return {
        "code": sell["code"] or buy["code"],
        "qty": qty,
        "buy_time": buy["time"],
        "sell_time": sell["time"],
        "buy_price": buy["price"],
        "sell_price": sell["price"],
        "buy_amount": buy_amount,
        "sell_amount": sell_amount,
        "buy_fee": buy_fee,
        "sell_fee": sell_fee,
        "gross_profit": gross,
        "net_profit": round(gross - buy_fee - sell_fee, 2),
    }


def _make_unmatched(rec: dict, qty: int, reason: str) -> dict:
    """构造一条无法匹配记录（保留原始成交信息 + 未匹配数量/原因）"""
    return {
        "time": rec["time"],
        "code": rec["code"],
        "name": rec["name"],
        "direction": rec["direction"],
        "price": rec["price"],
        "volume": rec["volume"],
        "amount": rec["amount"],
        "fee": rec["fee"],
        "trade_id": rec["trade_id"],
        "order_id": rec["order_id"],
        "unmatched_volume": qty,
        "reason": reason,
    }


def analyze_trades(records) -> dict:
    """对某日交易记录做 FIFO 配对分析

    Args:
        records: [{time, code, direction('buy'/'sell'), price, volume, amount, ...}]

    Returns:
        {trades, pairs, unmatched, summary}：
          trades    归一化后的成交明细（时间升序，含单笔手续费 fee）
          pairs     配对明细（FIFO）
          unmatched 无法匹配记录（留仓买入 / 卖出昨仓 / 方向未知）
          summary   汇总（笔数/金额/手续费/已配对收益/未匹配数）
    """
    trades = []
    for i, r in enumerate(records or []):
        t = _norm_record(r, i)
        if t:
            trades.append(t)
    # 时间升序；同秒/缺时间按原始顺序稳定排列（FIFO 配对基准）
    trades.sort(key=lambda t: (t["time"], t["_idx"]))
    for t in trades:
        t["fee"] = calc_fee(t["amount"])

    pairs, unmatched = [], []
    open_buys = {}      # code -> [{'rec': buy, 'remain': 未配对数量}]
    for t in trades:
        lots = open_buys.setdefault(t["code"], [])
        if t["direction"] == "buy":
            lots.append({"rec": t, "remain": t["volume"]})
            continue
        if t["direction"] != "sell":
            unmatched.append(_make_unmatched(t, t["volume"], "买卖方向未知"))
            continue
        remain = t["volume"]
        for lot in lots:
            if remain <= 0:
                break
            qty = min(lot["remain"], remain)
            if qty <= 0:
                continue
            pairs.append(_make_pair(lot["rec"], t, qty))
            lot["remain"] -= qty
            remain -= qty
        if remain > 0:
            unmatched.append(_make_unmatched(
                t, remain, "无同日对应买入（卖出昨日持仓）"))
    for lots in open_buys.values():
        for lot in lots:
            if lot["remain"] > 0:
                unmatched.append(_make_unmatched(
                    lot["rec"], lot["remain"], "未有同日卖出配对（留仓）"))

    buys = [t for t in trades if t["direction"] == "buy"]
    sells = [t for t in trades if t["direction"] == "sell"]
    total_fee = round(sum(t["fee"] for t in trades), 2)
    matched_fee = round(sum(p["buy_fee"] + p["sell_fee"] for p in pairs), 2)
    gross = round(sum(p["gross_profit"] for p in pairs), 2)
    summary = {
        "count": len(trades),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "buy_volume": sum(t["volume"] for t in buys),
        "sell_volume": sum(t["volume"] for t in sells),
        "buy_amount": round(sum(t["amount"] for t in buys), 2),
        "sell_amount": round(sum(t["amount"] for t in sells), 2),
        "buy_fee": round(sum(t["fee"] for t in buys), 2),
        "sell_fee": round(sum(t["fee"] for t in sells), 2),
        "total_fee": total_fee,
        "matched_count": len(pairs),
        "matched_volume": sum(p["qty"] for p in pairs),
        "matched_fee": matched_fee,
        "unmatched_fee": round(total_fee - matched_fee, 2),
        "gross_profit": gross,
        "net_profit": round(gross - matched_fee, 2),   # 当日实际收益（已配对）
        "unmatched_count": len(unmatched),
        "fee_rate": FEE_RATE,
        "min_fee": MIN_FEE,
    }
    # 清理内部排序辅助字段
    for t in trades:
        t.pop("_idx", None)
    return {"trades": trades, "pairs": pairs,
            "unmatched": unmatched, "summary": summary}
