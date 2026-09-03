"""
test_sim_data.py — 转换模拟数据源测试（QMT 优先 / 模拟兜底 / 开关控制）

场景:
  test_data fixture: TEST_CODE/TEST_DATE 已有 QMT 数据（tick_data）
  sim_data fixture : 追加 TickDataSim ——
    - 同 TEST_DATE（与 QMT 同日，验证 QMT 优先）
    - SIM_DATE（QMT 无数据的纯模拟日，验证开关兜底）
"""
import pytest

from app.dbdata.database import db
from app.dbdata.models import (TickDataSim, GameRound, GameOrder, GameTrade,
                                GameDay, DayKline)
from app.messaging.cache import get_cache
from app.engine.game_engine import get_engine

SIM_DATE = "2099-01-06"   # 仅转换模拟数据的交易日


def _make_sim_ticks(code, date, last_close=1.10):
    """构造一个完整小交易日的模拟 3s 数据（3 根 1min bar × 20 = 60 条）"""
    from mock_agent import gen_day_from_minutes
    bars = [
        {"time_key": f"{date} 09:30:00", "open": 1.10, "high": 1.11,
         "low": 1.09, "close": 1.105, "volume": 120000},
        {"time_key": f"{date} 10:00:00", "open": 1.105, "high": 1.13,
         "low": 1.10, "close": 1.12, "volume": 180000},
        {"time_key": f"{date} 15:00:00", "open": 1.12, "high": 1.16,
         "low": 1.11, "close": 1.15, "volume": 240000},
    ]
    ticks = gen_day_from_minutes(bars, last_close=last_close)
    for t in ticks:
        db.session.add(TickDataSim(
            code=code, trade_date=date, time_key=t["time_key"],
            open=t["open"], high=t["high"], low=t["low"], close=t["close"],
            volume=t["volume"], amount=round(t["close"] * t["volume"], 2),
        ))
    db.session.commit()
    # 同步写入 day_kline（与 tick 对齐）并派生 game_days：sim 日可见读 game_days，
    # 开局原始数据（昨收）读 day_kline
    get_engine().refresh_day(code, date, "sim", last_close_hint=last_close)
    return len(ticks)


def _cleanup_sim(app, code):
    """清理模拟数据与该 code 轮次（含 game_days 计数回落）"""
    with app.app_context():
        rounds = GameRound.query.filter(GameRound.code == code).all()
        for r in rounds:
            GameOrder.query.filter_by(round_id=r.id).delete()
            GameTrade.query.filter_by(round_id=r.id).delete()
            get_cache().delete_account(r.id)
            get_cache().delete_progress(r.id)
            get_cache().delete_quote(str(r.id))
            db.session.delete(r)
        TickDataSim.query.filter_by(code=code).delete()
        DayKline.query.filter_by(code=code).delete()
        GameDay.query.filter_by(code=code).delete()
        db.session.commit()
        engine = get_engine()
        # 轮次行随批量清理物理删除后，game_days 计数同步回落（口径同 delete_round）
        for r in rounds:
            engine._adjust_round_count(r.code, r.trade_date, r.data_source or "qmt", -1)
        engine._rounds.clear()


@pytest.fixture()
def sim_data(app, test_data):
    """在 test_data（QMT 日）基础上追加转换模拟数据（同 code）"""
    code = test_data["code"]
    with app.app_context():
        # 同日副本（验证 QMT 优先） + 纯模拟日
        _make_sim_ticks(code, test_data["date"], last_close=1.00)
        n = _make_sim_ticks(code, SIM_DATE, last_close=1.10)
        assert n == 60
    yield {"code": code, "qmt_date": test_data["date"], "sim_date": SIM_DATE}
    _cleanup_sim(app, code)


class TestSimDateSource:
    def test_dates_exclude_sim_by_default(self, sim_data, engine, test_data):
        """开关关闭：仅返回 QMT 数据日期，纯模拟日不可见"""
        dates = engine.available_dates(test_data["code"])
        assert test_data["date"] in dates
        assert SIM_DATE not in dates

    def test_dates_include_sim_when_allowed(self, sim_data, engine, test_data):
        """开关开启：纯模拟日可见，且 QMT 优先标记"""
        dates = engine.available_dates(test_data["code"], allow_sim=True)
        assert SIM_DATE in dates

        src = {x["trade_date"]: x["source"]
               for x in engine.date_sources(test_data["code"], allow_sim=True)}
        assert src[test_data["date"]] == "qmt"   # 同日双数据 → QMT 优先
        assert src[SIM_DATE] == "sim"

    def test_create_sim_requires_allow_sim(self, sim_data, engine, test_data):
        """开关关闭时纯模拟日拒绝；开启后成功且 data_source=sim，可运行撮合"""
        code = test_data["code"]
        # 开关关闭 → 拒绝
        r, msg = engine.create_round(code=code, trade_date=SIM_DATE)
        assert r is None and "数据不完整或不存在" in msg

        # 开关开启 → 创建成功，数据源 = sim
        r, msg = engine.create_round(code=code, trade_date=SIM_DATE, allow_sim=True)
        assert r is not None, msg
        assert r.data_source == "sim"

        ok, msg = engine.start_round(r.id)
        assert ok, msg
        ctx = engine._rounds[r.id]
        assert len(ctx.ticks) == 60
        assert ctx.ticks[0]["last_close"] == pytest.approx(1.10)

        # 模拟数据轮次同样可撮合（限价买 1.15：tick.low <= 1.15 必然成交）
        ok, order, msg = engine.place_order(r.id, "buy", "limit", 1.15, 10000)
        assert ok, msg
        engine._process_tick(ctx, ctx.ticks[0])
        with engine._ctx():
            fresh = GameOrder.query.filter_by(order_id=order["order_id"]).first()
        assert fresh.status == "filled"

        engine.delete_round(r.id)

    def test_qmt_priority_same_day(self, sim_data, engine, test_data):
        """同日 QMT 与模拟并存 → data_source=qmt"""
        r, msg = engine.create_round(code=test_data["code"],
                                     trade_date=test_data["date"], allow_sim=True)
        assert r is not None, msg
        assert r.data_source == "qmt"
        engine.delete_round(r.id)


class TestSimApi:
    def test_dates_api_with_source(self, sim_data, client, test_data):
        """dates 接口：source=1 返回来源标记；allow_sim 过滤"""
        resp = client.get(f"/api/v1/game/dates?code={test_data['code']}&source=1")
        data = resp.get_json()["data"]
        dates = [x["trade_date"] for x in data]
        assert test_data["date"] in dates
        assert SIM_DATE not in dates                      # 默认关闭

        resp = client.get(f"/api/v1/game/dates?code={test_data['code']}"
                          f"&source=1&allow_sim=1")
        data = resp.get_json()["data"]
        m = {x["trade_date"]: x["source"] for x in data}
        assert m[test_data["date"]] == "qmt"
        assert m[SIM_DATE] == "sim"

    def test_create_round_sim_api(self, sim_data, client, test_data):
        """创建轮次接口：allow_sim 关闭拒绝 / 开启成功；ticks 从模拟表读取"""
        code = test_data["code"]
        # 默认（关闭）→ 拒绝
        resp = client.post("/api/v1/game/rounds",
                           json={"code": code, "trade_date": SIM_DATE})
        assert resp.get_json()["code"] != 0

        # 开启 → 成功，data_source=sim
        resp = client.post("/api/v1/game/rounds",
                           json={"code": code, "trade_date": SIM_DATE,
                                 "allow_sim": True})
        assert resp.get_json()["code"] == 0, resp.get_json()
        rid = resp.get_json()["data"]["id"]
        assert resp.get_json()["data"]["data_source"] == "sim"

        resp = client.get(f"/api/v1/game/rounds/{rid}")
        assert resp.get_json()["data"]["data_source"] == "sim"

        # 分时图恢复数据从 tick_data_sim 读取
        resp = client.get(f"/api/v1/game/rounds/{rid}/ticks?tail=10")
        body = resp.get_json()["data"]
        assert body["data_source"] == "sim"
        assert len(body["ticks"]) == 10
        client.delete(f"/api/v1/game/rounds/{rid}")

    def test_rounds_list_shows_source(self, sim_data, client, test_data):
        """轮次列表返回 data_source 字段"""
        resp = client.post("/api/v1/game/rounds",
                           json={"code": test_data["code"],
                                 "trade_date": SIM_DATE, "allow_sim": True})
        rid = resp.get_json()["data"]["id"]
        resp = client.get("/api/v1/game/rounds")
        item = next(x for x in resp.get_json()["data"] if x["id"] == rid)
        assert item["data_source"] == "sim"
        client.delete(f"/api/v1/game/rounds/{rid}")
