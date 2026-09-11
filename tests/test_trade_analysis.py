"""
test_trade_analysis.py — 交易记录配对分析测试（手续费口径 + FIFO 配对）

手续费口径：单笔 max(成交金额 × 万分之0.85, 5 元)，买卖双边（不免 5）。
配对口径：同一标的按成交时间升序 FIFO，卖出与最早未配完的买入配对；
剩余买入（留仓）/卖出（卖出昨仓）列入无法匹配。
"""
import pytest

from app.engine.trade_analysis import analyze_trades, calc_fee


def _rec(time, direction, price, volume, code="588000.SH", amount=None, tid=""):
    return {"time": time, "code": code, "direction": direction,
            "price": price, "volume": volume, "trade_id": tid,
            "amount": amount if amount is not None else round(price * volume, 2)}


D = "2026-09-11"


class TestFee:
    def test_min_fee_floor(self):
        """小额成交：费率不足 5 元 → 按 5 元（不免 5）"""
        assert calc_fee(10000) == 5.0
        assert calc_fee(50000) == 5.0

    def test_rate_applied(self):
        """大额成交：按万分之 0.85 计"""
        assert calc_fee(1000000) == 85.0
        assert calc_fee(1100000) == 93.5

    def test_invalid_amount(self):
        assert calc_fee(None) == 5.0
        assert calc_fee(-100) == 5.0


class TestMatching:
    def test_basic_pair(self):
        """单买单调全额配对：毛收益 = 价差 × 数量；净收益扣双边手续费"""
        r = analyze_trades([
            _rec(f"{D} 09:31:00", "buy", 1.0, 100000),
            _rec(f"{D} 10:31:00", "sell", 1.1, 100000),
        ])
        assert len(r["pairs"]) == 1
        p = r["pairs"][0]
        assert p["qty"] == 100000
        assert p["gross_profit"] == 10000.0
        assert p["buy_fee"] == 8.5 and p["sell_fee"] == 9.35
        assert p["net_profit"] == pytest.approx(9982.15)
        s = r["summary"]
        assert s["net_profit"] == pytest.approx(9982.15)
        assert s["total_fee"] == pytest.approx(17.85)
        assert s["matched_count"] == 1 and s["unmatched_count"] == 0

    def test_fifo_split(self):
        """一笔卖出覆盖两笔买入：FIFO 先配最早买入，金额按占比分摊"""
        r = analyze_trades([
            _rec(f"{D} 09:31:00", "buy", 1.0, 60000),
            _rec(f"{D} 09:40:00", "buy", 1.05, 60000),
            _rec(f"{D} 10:00:00", "sell", 1.1, 100000),
        ])
        pairs = r["pairs"]
        assert len(pairs) == 2
        assert pairs[0]["qty"] == 60000 and pairs[0]["buy_price"] == 1.0
        assert pairs[1]["qty"] == 40000 and pairs[1]["buy_price"] == 1.05
        # 剩余 2 万股买入未配对（留仓）
        assert r["summary"]["unmatched_count"] == 1
        u = r["unmatched"][0]
        assert u["direction"] == "buy" and u["unmatched_volume"] == 20000
        assert "留仓" in u["reason"]

    def test_partial_sell_remainder(self):
        """卖单大于买单：部分配对，剩余卖单列入无法匹配"""
        r = analyze_trades([
            _rec(f"{D} 09:31:00", "buy", 1.0, 50000),
            _rec(f"{D} 10:00:00", "sell", 1.1, 80000),
        ])
        assert len(r["pairs"]) == 1 and r["pairs"][0]["qty"] == 50000
        assert r["summary"]["unmatched_count"] == 1
        u = r["unmatched"][0]
        assert u["direction"] == "sell" and u["unmatched_volume"] == 30000
        assert "卖出昨日持仓" in u["reason"]

    def test_sell_without_same_day_buy(self):
        """仅卖出（卖出昨日持仓）→ 无法匹配，净收益 0"""
        r = analyze_trades([_rec(f"{D} 09:31:00", "sell", 1.02, 100000)])
        assert r["pairs"] == []
        assert r["summary"]["net_profit"] == 0
        assert r["summary"]["unmatched_count"] == 1

    def test_cross_code_not_matched(self):
        """不同标的之间不配对"""
        r = analyze_trades([
            _rec(f"{D} 09:31:00", "buy", 1.0, 100000, code="588000.SH"),
            _rec(f"{D} 10:00:00", "sell", 1.1, 100000, code="510300.SH"),
        ])
        assert r["pairs"] == []
        assert r["summary"]["unmatched_count"] == 2

    def test_input_order_ignored(self):
        """乱序输入按成交时间排序后仍可配对"""
        r = analyze_trades([
            _rec(f"{D} 10:00:00", "sell", 1.1, 100000),
            _rec(f"{D} 09:31:00", "buy", 1.0, 100000),
        ])
        assert len(r["pairs"]) == 1

    def test_fee_proration_sums_to_total(self):
        """拆分配对：各配对手续费分摊之和 = 该笔成交手续费"""
        r = analyze_trades([
            _rec(f"{D} 09:31:00", "buy", 1.0, 100000),
            _rec(f"{D} 10:00:00", "sell", 1.1, 60000),
            _rec(f"{D} 10:30:00", "sell", 1.1, 40000),
        ])
        buy_fee_sum = round(sum(p["buy_fee"] for p in r["pairs"]), 2)
        total_buy_fee = round(sum(t["fee"] for t in r["trades"]
                                 if t["direction"] == "buy"), 2)
        assert buy_fee_sum == total_buy_fee

    def test_empty_records(self):
        r = analyze_trades([])
        s = r["summary"]
        assert s["count"] == 0 and s["net_profit"] == 0 and s["total_fee"] == 0

    def test_unknown_direction(self):
        """方向未知的成交列入无法匹配，不参与收益"""
        r = analyze_trades([{
            "time": f"{D} 09:31:00", "code": "588000.SH", "direction": "",
            "price": 1.0, "volume": 100000, "amount": 100000.0}])
        assert r["summary"]["matched_count"] == 0
        assert r["summary"]["unmatched_count"] == 1
        assert "方向未知" in r["unmatched"][0]["reason"]
