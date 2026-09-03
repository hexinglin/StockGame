"""
pytest 公共夹具 — 测试应用 / 客户端 / 测试行情数据
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from app.main import create_app
from app.dbdata.database import db
from app.dbdata.models import (TickData, TickDataSim, GameRound, GameOrder,
                                GameTrade, GameDay, DayKline)
from app.messaging.cache import get_cache
from app.engine.game_engine import get_engine

TEST_CODE = "TEST588000"
TEST_DATE = "2099-01-05"


@pytest.fixture(scope="session")
def app():
    """测试应用（不启用调度器，时钟手动驱动）"""
    os.environ["STOCKGAME_CONFIG"] = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
    application = create_app(enable_scheduler=False)
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def test_data(app):
    """生成一个完整小交易日的 3s 行情（3 根 1min bar × 20 = 60 条 tick）"""
    from mock_agent import gen_day_from_minutes

    with app.app_context():
        # 清理遗留数据
        _cleanup(app)
        bars = [
            {"time_key": f"{TEST_DATE} 09:30:00", "open": 1.00, "high": 1.01,
             "low": 0.99, "close": 1.005, "volume": 100000},
            {"time_key": f"{TEST_DATE} 10:00:00", "open": 1.005, "high": 1.02,
             "low": 1.00, "close": 1.015, "volume": 150000},
            {"time_key": f"{TEST_DATE} 15:00:00", "open": 1.015, "high": 1.05,
             "low": 1.01, "close": 1.05, "volume": 200000},
        ]
        ticks = gen_day_from_minutes(bars, last_close=1.00)
        assert len(ticks) == 60
        for t in ticks:
            db.session.add(TickData(
                code=TEST_CODE, trade_date=TEST_DATE, time_key=t["time_key"],
                open=t["open"], high=t["high"], low=t["low"], close=t["close"],
                volume=t["volume"], amount=round(t["close"] * t["volume"], 2),
            ))
        db.session.commit()
        # 同步写入 day_kline（与 tick 对齐）并派生 game_days：开局/昨收读 day_kline
        get_engine().refresh_day(TEST_CODE, TEST_DATE, "qmt",
                                 last_close_hint=1.00)

    yield {"code": TEST_CODE, "date": TEST_DATE, "last_close": 1.00}

    with app.app_context():
        _cleanup(app)


def _cleanup(app):
    """清理测试数据（轮次级联 + tick + Redis + game_days 计数回落）"""
    rounds = GameRound.query.filter(GameRound.code == TEST_CODE).all()
    for r in rounds:
        GameOrder.query.filter_by(round_id=r.id).delete()
        GameTrade.query.filter_by(round_id=r.id).delete()
        get_cache().delete_account(r.id)
        get_cache().delete_progress(r.id)
        get_cache().delete_quote(str(r.id))
        db.session.delete(r)
    TickData.query.filter_by(code=TEST_CODE).delete()
    TickDataSim.query.filter_by(code=TEST_CODE).delete()
    DayKline.query.filter_by(code=TEST_CODE).delete()
    GameDay.query.filter_by(code=TEST_CODE).delete()
    db.session.commit()
    # 轮次行随批量清理物理删除后，game_days 计数同步回落（口径同 delete_round）
    engine = get_engine()
    for r in rounds:
        engine._adjust_round_count(r.code, r.trade_date, r.data_source or "qmt", -1)
    # 重置引擎内存态
    engine._rounds.clear()


@pytest.fixture()
def engine():
    """游戏引擎单例（测试中手动驱动）"""
    return get_engine()
