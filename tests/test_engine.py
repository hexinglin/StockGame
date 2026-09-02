"""
test_engine.py — 游戏核心单元测试（账户/冻结/撮合/结算/并发/删除/code隔离）
"""
import pytest

from app.engine.account import MockAccount
from app.dbdata.database import db
from app.dbdata.models import GameRound, GameOrder


# ═══════════ 账户单元测试 ═══════════

class TestAccount:
    def test_fee(self):
        acct = MockAccount(open_price=1.0)
        assert acct.fee_for(10000) == 1.0          # 万1
        assert acct.fee_for(500000) == 50.0
        assert acct.fee_for(100) == 0.01

    def test_initial(self):
        acct = MockAccount(open_price=1.0)
        assert acct.available_cash == 500000
        assert acct.volume == 500000
        assert acct.available_volume() == 500000

    def test_freeze_buy_insufficient(self):
        acct = MockAccount(open_price=1.0)
        ok = acct.freeze_buy(2.0, 300000)          # 60 万 > 50 万
        assert ok is False
        assert acct.frozen_cash == 0

    def test_freeze_buy_ok(self):
        acct = MockAccount(open_price=1.0)
        ok = acct.freeze_buy(1.0, 100000)          # 10 万 + 10 元手续费
        assert ok is True
        assert acct.frozen_cash == 100000 + 10

    def test_freeze_sell_insufficient(self):
        acct = MockAccount(open_price=1.0)
        assert acct.freeze_sell(600000) is False   # 超出底仓
        assert acct.frozen_volume == 0

    def test_freeze_sell_ok(self):
        acct = MockAccount(open_price=1.0)
        assert acct.freeze_sell(100000) is True
        assert acct.frozen_volume == 100000
        assert acct.available_volume() == 400000

    def test_unfreeze(self):
        acct = MockAccount(open_price=1.0)
        acct.freeze_buy(1.0, 100000)
        acct.unfreeze_buy(1.0, 100000)
        assert acct.frozen_cash == 0
        acct.freeze_sell(100000)
        acct.unfreeze_sell(100000)
        assert acct.frozen_volume == 0

    def test_fill_buy_weighted_avg(self):
        acct = MockAccount(open_price=1.0)
        acct.freeze_buy(1.0, 100000)
        fee = acct.fill_buy(1.0, 100000, frozen_amount=acct.frozen_amount(1.0, 100000))
        assert acct.volume == 600000
        assert fee == 10.0
        assert acct.avg_price == pytest.approx(1.0)
        assert acct.available_cash == pytest.approx(500000 - 100000 - 10)
        assert acct.frozen_cash == 0

    def test_fill_sell(self):
        acct = MockAccount(open_price=1.0)
        acct.freeze_sell(100000)
        fee = acct.fill_sell(1.5, 100000)
        assert fee == 15.0
        assert acct.volume == 400000
        assert acct.frozen_volume == 0
        assert acct.available_cash == pytest.approx(500000 + 150000 - 15)

    def test_fill_buy_frozen_return(self):
        """成交价低于冻结预估时，多冻结部分回流可用资金"""
        acct = MockAccount(open_price=1.0)
        acct.freeze_buy(1.0, 100000)               # 冻结 100010
        acct.fill_buy(0.9, 100000, frozen_amount=100010)  # 实际 90000+9
        assert acct.available_cash == pytest.approx(500000 - 90000 - 9)
        assert acct.frozen_cash == pytest.approx(0)


# ═══════════ 引擎集成测试（真实 DB + 手动驱动） ═══════════

@pytest.fixture()
def running_round(app, test_data, engine):
    """创建并启动一个轮次，返回 (round_id, ctx)"""
    r, msg = engine.create_round(code=test_data["code"], trade_date=test_data["date"])
    assert r is not None, msg
    ok, msg = engine.start_round(r.id)
    assert ok, msg
    ctx = engine._rounds[r.id]
    yield r, ctx
    # 清理
    engine.delete_round(r.id)


class TestMatching:
    @staticmethod
    def _fetch_order(app, order_id):
        """按 order_id 查询最新订单状态（ORM 对象）"""
        with app.app_context():
            return GameOrder.query.filter_by(order_id=order_id).first()

    def test_limit_buy_fill(self, running_round, engine, app):
        r, ctx = running_round
        ok, order, msg = engine.place_order(r.id, "buy", "limit", 1.05, 10000)
        assert ok, msg
        ticks = [t for t in ctx.ticks if t["low"] <= 1.05]
        assert ticks
        engine._process_tick(ctx, ticks[0])
        fresh = self._fetch_order(app, order["order_id"])
        assert fresh.status == "filled"
        assert fresh.filled_price == pytest.approx(1.05)
        assert fresh.filled_shares == 10000

    def test_limit_sell_fill(self, running_round, engine, app):
        r, ctx = running_round
        ok, order, msg = engine.place_order(r.id, "sell", "limit", 0.99, 10000)
        assert ok, msg
        ticks = [t for t in ctx.ticks if t["high"] >= 0.99]
        assert ticks
        engine._process_tick(ctx, ticks[0])
        fresh = self._fetch_order(app, order["order_id"])
        assert fresh.status == "filled"
        assert fresh.filled_price == pytest.approx(0.99)

    def test_limit_not_touch_pending(self, running_round, engine, app):
        r, ctx = running_round
        ok, order, msg = engine.place_order(r.id, "buy", "limit", 0.5, 10000)
        assert ok, msg
        for t in ctx.ticks:
            engine._process_tick(ctx, t)
        fresh = self._fetch_order(app, order["order_id"])
        assert fresh.status == "pending"

    def test_market_fill_by_close(self, running_round, engine, app):
        r, ctx = running_round
        ok, order, msg = engine.place_order(r.id, "buy", "market", 0, 10000)
        assert ok, msg
        first = ctx.ticks[0]
        engine._process_tick(ctx, first)
        fresh = self._fetch_order(app, order["order_id"])
        assert fresh.status == "filled"
        assert fresh.filled_price == pytest.approx(first["close"])

    def test_market_reject_when_price_over_frozen(self, running_round, engine, app):
        r, ctx = running_round
        ctx.acct.last_price = 1.0
        ok, order, msg = engine.place_order(r.id, "buy", "market", 0, 100000)
        assert ok, msg
        frozen_before = ctx.acct.frozen_cash
        # 极端跳涨 tick（close=2.0）→ 成交额超冻结 → 拒单并解冻
        big_tick = {"time_key": "2099-01-05 09:30:03", "open": 1.0, "high": 2.0,
                    "low": 1.0, "close": 2.0, "volume": 100, "amount": 200,
                    "last_close": 1.0}
        engine._process_tick(ctx, big_tick)
        fresh = self._fetch_order(app, order["order_id"])
        assert fresh.status == "rejected"
        assert ctx.acct.frozen_cash == pytest.approx(
            frozen_before - ctx.acct.frozen_amount(1.0, 100000))
        assert ctx.acct.frozen_cash == pytest.approx(0)

    def test_full_order_fill(self, running_round, engine, app):
        """整单成交：一次成交全部数量"""
        r, ctx = running_round
        ok, order, msg = engine.place_order(r.id, "buy", "limit", 1.05, 10000)
        assert ok, msg
        engine._process_tick(ctx, ctx.ticks[0])
        fresh = self._fetch_order(app, order["order_id"])
        assert fresh.filled_shares == 10000
        assert fresh.status == "filled"


class TestSettlement:
    def test_settle_pnl(self, running_round, engine, app):
        r, ctx = running_round
        ok, o1, _ = engine.place_order(r.id, "buy", "limit", 1.0, 10000)
        engine._process_tick(ctx, ctx.ticks[0])
        with app.app_context():
            o1 = GameOrder.query.filter_by(order_id=o1["order_id"]).first()
        assert o1.status == "filled"

        ok, o2, _ = engine.place_order(r.id, "sell", "limit", 1.05, 10000)
        ticks = [t for t in ctx.ticks if t["high"] >= 1.05]
        engine._process_tick(ctx, ticks[0])
        with app.app_context():
            o2 = GameOrder.query.filter_by(order_id=o2["order_id"]).first()
        assert o2.status == "filled"

        ok, msg = engine.finish_round(r.id)
        assert ok, msg
        detail = engine.get_round(r.id)
        assert detail["status"] == "finished"
        # 已实现盈亏 = 卖(10500-1.05) - 买(10000+1.0) = 500 - 2.05
        assert detail["realized_pnl"] == pytest.approx(500 - 2.05, abs=0.01)
        assert detail["fee_total"] == pytest.approx(2.05, abs=0.01)
        assert detail["final_assets"] > 0

    def test_auto_settle_at_last_tick(self, running_round, engine, app):
        r, ctx = running_round
        while ctx.index < len(ctx.ticks):
            engine._advance(ctx)
        assert engine.get_round(r.id)["status"] == "finished"

    def test_finish_cancels_pending(self, running_round, engine, app):
        r, ctx = running_round
        ok, order, msg = engine.place_order(r.id, "buy", "limit", 0.5, 10000)
        assert ok, msg
        engine.finish_round(r.id)
        with app.app_context():
            fresh = GameOrder.query.filter_by(order_id=order["order_id"]).first()
        assert fresh.status == "cancelled"


class TestConcurrency:
    def test_same_day_round_rejected(self, test_data, engine):
        r1, _ = engine.create_round(code=test_data["code"], trade_date=test_data["date"])
        assert r1 is not None
        engine.start_round(r1.id)
        r2, msg = engine.create_round(code=test_data["code"], trade_date=test_data["date"])
        assert r2 is None and "已有未结束轮次" in msg
        engine.delete_round(r1.id)

    def test_rebuild_after_finished(self, test_data, engine):
        r1, _ = engine.create_round(code=test_data["code"], trade_date=test_data["date"])
        engine.start_round(r1.id)
        engine.finish_round(r1.id)
        r2, msg = engine.create_round(code=test_data["code"], trade_date=test_data["date"])
        assert r2 is not None, msg
        engine.delete_round(r2.id)

    def test_code_isolation(self, test_data, engine):
        """不同 code 同交易日互不冲突（无数据 code 被数据完整性校验拒绝）"""
        r1, _ = engine.create_round(code=test_data["code"], trade_date=test_data["date"])
        engine.start_round(r1.id)
        r2, msg = engine.create_round(code="OTHER588000", trade_date=test_data["date"])
        assert r2 is None
        assert "暂无完整交易日数据" in msg
        engine.delete_round(r1.id)


class TestDelete:
    def test_delete_cleans_redis(self, running_round, engine):
        r, ctx = running_round
        from app.messaging.cache import get_cache
        cache = get_cache()
        engine.pause_round(r.id)
        ok, msg = engine.delete_round(r.id)
        assert ok, msg
        assert cache.load_account(r.id) is None
        assert cache.load_progress(r.id) == 0

    def test_delete_running_rejected(self, running_round, engine):
        r, ctx = running_round
        ok, msg = engine.delete_round(r.id)
        assert not ok and "运行中" in msg
