"""
模块名称: engine/trade_analysis.py
说明:    交易记录配对分析 — 手续费计算 + 同日买卖最大收益配对

配对口径（真实记录与游戏轮次共用同一套配对规则，仅手续费来源不同）：
  - 同一标的配满 min(当日买量, 当日卖量)：卖出优先取最高价、买入优先取最低价，
    高价卖与低价买逐量对消（同价按时间升序，结果稳定可复现）；
  - 卖出成本按"最低价买入优先"归属（非 FIFO 时序），使当日配对收益最大；
  - 手续费两套来源：
      · 真实记录（analyze_trades）：按「委托」计一次
        = max(委托合计成交金额 × 万分之 0.85, 5 元)，买卖双边各计（不免 5），
        再平均分摊到该委托的每笔拆分成交；
      · 游戏轮次（analyze_game_trades）：引擎已按模拟费率逐笔计费并落库，
        直接采用记录自带手续费（缺失时按给定费率兜底）；
  - 配对收益 = 卖出部分金额 − 买入部分金额 − 买卖两笔手续费（按配对数量占比分摊）；
  - 剩余未配对的买入（未卖出留仓）与卖出（卖出昨日持仓、无同日买入成本）
    列入"无法匹配记录"，仅展示不参与当日收益计算。
"""
import logging

logger = logging.getLogger(__name__)

FEE_RATE = 0.000085     # 真实记录佣金费率：万分之 0.85（单边）
MIN_FEE = 5.0           # 真实记录最低手续费：5 元（不免 5）
GAME_FEE_RATE = 0.0001  # 游戏模拟费率兜底（记录缺手续费时用；与 config game.fee_rate 默认一致）


def calc_fee(amount, fee_rate: float = FEE_RATE, min_fee: float = MIN_FEE) -> float:
    """单笔手续费 = max(成交金额 × 费率, 最低值)，四舍五入到分

    默认取真实记录口径（万分之 0.85、最低 5 元）；游戏侧传入模拟费率、最低 0。
    """
    try:
        amount = max(0.0, float(amount or 0))
    except (TypeError, ValueError):
        amount = 0.0
    return round(max(amount * fee_rate, min_fee), 2)


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
        "fee_raw": rec.get("fee"),        # 游戏侧自带手续费（真实记录为空）
        "trade_id": str(rec.get("trade_id") or ""),
        "order_id": str(rec.get("order_id") or ""),
        "_idx": idx,
    }


def normalize_records(records) -> list:
    """批量归一化 + 按时间升序（同秒/缺时间按原始顺序稳定排列），返回 trades"""
    trades = []
    for i, r in enumerate(records or []):
        t = _norm_record(r, i)
        if t:
            trades.append(t)
    trades.sort(key=lambda t: (t["time"], t["_idx"]))
    return trades


def _ratio(rec: dict, qty: int) -> float:
    """配对数量占该笔成交的比例（用于金额/手续费分摊）"""
    vol = rec.get("volume") or 0
    return (qty / vol) if vol > 0 else 0.0


def _make_pair(buy: dict, sell: dict, qty: int) -> dict:
    """构造一个配对（金额与手续费按数量占比分摊）"""
    rb, rs = _ratio(buy, qty), _ratio(sell, qty)
    buy_amount = round(buy["amount"] * rb, 2)
    sell_amount = round(sell["amount"] * rs, 2)
    # 费用/净收益分摊保留 6 位精度：碎片配对逐个 round 到分会累计尾差，
    # 委托对聚合展示时再统一舍入到分
    buy_fee = round(buy["fee"] * rb, 6)
    sell_fee = round(sell["fee"] * rs, 6)
    gross = round(sell_amount - buy_amount, 2)
    return {
        "code": sell["code"] or buy["code"],
        "qty": qty,
        "buy_order_id": buy.get("order_id", ""),
        "sell_order_id": sell.get("order_id", ""),
        "buy_time": buy["time"],
        "sell_time": sell["time"],
        "buy_price": buy["price"],
        "sell_price": sell["price"],
        "buy_amount": buy_amount,
        "sell_amount": sell_amount,
        "buy_fee": buy_fee,
        "sell_fee": sell_fee,
        "gross_profit": gross,
        "net_profit": round(gross - buy_fee - sell_fee, 6),
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


def assign_order_fees(trades: list, fee_rate: float = FEE_RATE,
                      min_fee: float = MIN_FEE):
    """真实记录口径手续费：按「委托」计一次（同委托编号的拆分成交先合计金额算一次
    max(合计金额×费率, 最低值)，再平均分摊到该委托的每笔成交）。
    无委托编号的记录各自独立（一笔委托 = 一笔成交）。"""
    groups = {}
    for i, t in enumerate(trades):
        groups.setdefault(t["order_id"] or ("#solo#%d" % i), []).append(t)
    for gts in groups.values():
        per = calc_fee(sum(t["amount"] for t in gts), fee_rate, min_fee) / len(gts)
        for t in gts:
            t["fee"] = per


def assign_recorded_fees(trades: list, fallback_rate: float = GAME_FEE_RATE):
    """游戏轮次口径手续费：直接采用成交记录自带手续费（引擎按模拟费率逐笔计费落库），
    缺失时按 fallback_rate 兜底（不含真实券商的 5 元最低）。"""
    for t in trades:
        raw = t.get("fee_raw")
        try:
            fee = float(raw)
        except (TypeError, ValueError):
            fee = 0.0
        t["fee"] = round(fee, 6) if fee > 0 else round(t["amount"] * fallback_rate, 6)


def pair_trades(trades: list):
    """最大同日收益配对（真实记录与游戏轮次共用）

    按标的分池独立配对：配满 min(买量, 卖量)——卖取最高价、买取最低价逐量对消
    （同价按时间升序，结果稳定）；卖出成本因此按"最低价买入优先"归属（非 FIFO 时序）。
    剩余未配对：卖出（低价端未选中）→ 卖昨仓；买入（高价端未选中）→ 留仓。

    Args: trades 已归一化且已赋 fee 的成交列表
    Returns: (pairs, unmatched)
    """
    pairs, unmatched = [], []
    by_code = {}
    for t in trades:
        by_code.setdefault(t["code"], []).append(t)
    for ts in by_code.values():
        buys = [t for t in ts if t["direction"] == "buy"]
        sells = [t for t in ts if t["direction"] == "sell"]
        for t in ts:
            if t["direction"] not in ("buy", "sell"):
                unmatched.append(_make_unmatched(t, t["volume"], "买卖方向未知"))
        total = min(sum(t["volume"] for t in buys),
                    sum(t["volume"] for t in sells))
        used = {}
        if total > 0:
            bsel = sorted(buys, key=lambda t: (t["price"], t["time"], t["_idx"]))
            ssel = sorted(sells, key=lambda t: (-t["price"], t["time"], t["_idx"]))
            bi = si = 0
            brem, srem = bsel[0]["volume"], ssel[0]["volume"]
            done = 0
            while done < total:
                qty = min(brem, srem)
                if qty <= 0:
                    break
                pairs.append(_make_pair(bsel[bi], ssel[si], qty))
                used[id(bsel[bi])] = used.get(id(bsel[bi]), 0) + qty
                used[id(ssel[si])] = used.get(id(ssel[si]), 0) + qty
                brem -= qty
                srem -= qty
                done += qty
                if brem == 0:
                    bi += 1
                    brem = bsel[bi]["volume"] if bi < len(bsel) else 0
                if srem == 0:
                    si += 1
                    srem = ssel[si]["volume"] if si < len(ssel) else 0
        for t in sells:
            left = t["volume"] - used.get(id(t), 0)
            if left > 0:
                unmatched.append(_make_unmatched(
                    t, left, "无同日对应买入（卖出昨日持仓）"))
        for t in buys:
            left = t["volume"] - used.get(id(t), 0)
            if left > 0:
                unmatched.append(_make_unmatched(
                    t, left, "未有同日卖出配对（留仓）"))
    return pairs, unmatched


def summarize(trades: list, pairs: list, unmatched: list,
              fee_rate: float = FEE_RATE, min_fee: float = MIN_FEE) -> dict:
    """配对结果汇总（笔数/金额/手续费/已配对收益/未匹配数）"""
    buys = [t for t in trades if t["direction"] == "buy"]
    sells = [t for t in trades if t["direction"] == "sell"]
    total_fee = round(sum(t["fee"] for t in trades), 2)
    matched_fee = round(sum(p["buy_fee"] + p["sell_fee"] for p in pairs), 2)
    gross = round(sum(p["gross_profit"] for p in pairs), 2)
    return {
        "count": len(trades),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "total_volume": sum(t["volume"] for t in trades),
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
        "fee_rate": fee_rate,
        "min_fee": min_fee,
    }


def _finalize(trades: list, pairs: list, unmatched: list, summary: dict) -> dict:
    """清理内部排序辅助字段并组装返回值"""
    for t in trades:
        t.pop("_idx", None)
        t.pop("fee_raw", None)
    return {"trades": trades, "pairs": pairs,
            "unmatched": unmatched, "summary": summary}


def analyze_trades(records) -> dict:
    """对某日「真实交易记录」做最大当日收益配对分析

    Args:
        records: [{time, code, direction('buy'/'sell'), price, volume, amount, ...}]

    Returns:
        {trades, pairs, unmatched, summary}：
          trades    归一化后的成交明细（时间升序，fee=委托级费用的每笔均摊值）
          pairs     配对明细（最大收益：高卖配低买）
          unmatched 无法匹配记录（留仓买入 / 卖出昨仓 / 方向未知）
          summary   汇总（笔数/金额/手续费/已配对收益/未匹配数）
    """
    trades = normalize_records(records)
    assign_order_fees(trades)
    pairs, unmatched = pair_trades(trades)
    summary = summarize(trades, pairs, unmatched)
    return _finalize(trades, pairs, unmatched, summary)


def analyze_game_trades(records, fallback_rate: float = GAME_FEE_RATE) -> dict:
    """对「游戏轮次」成交做同一套配对分析（配对规则一致，手续费取记录自带值）

    与 analyze_trades 的差别仅在手续费口径：游戏由引擎按模拟费率逐笔计费并
    落库，故直接采用记录值，而非真实券商的「按委托 min 5 元」模型。
    """
    trades = normalize_records(records)
    assign_recorded_fees(trades, fallback_rate)
    pairs, unmatched = pair_trades(trades)
    summary = summarize(trades, pairs, unmatched, fee_rate=fallback_rate, min_fee=0.0)
    return _finalize(trades, pairs, unmatched, summary)
