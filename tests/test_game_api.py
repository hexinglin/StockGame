"""
test_game_api.py — 游戏 API 全链路测试（CRUD / 下单 / 撤单 / 结算 / 并发约束）
"""
import pytest

from app.dbdata.database import db
from app.dbdata.models import GameOrder, GameTrade, GameRound


def create_and_start(client, code, date):
    """创建并启动轮次，返回轮次 id"""
    resp = client.post("/api/v1/game/rounds", json={"code": code, "trade_date": date})
    assert resp.get_json()["code"] == 0, resp.get_json()
    rid = resp.get_json()["data"]["id"]
    resp = client.post(f"/api/v1/game/rounds/{rid}/start", json={})
    assert resp.get_json()["code"] == 0, resp.get_json()
    return rid


class TestRoundCRUD:
    def test_create_list_detail_delete(self, client, test_data):
        rid = create_and_start(client, test_data["code"], test_data["date"])

        # 列表
        resp = client.get("/api/v1/game/rounds")
        rounds = resp.get_json()["data"]
        assert any(x["id"] == rid for x in rounds)

        # 详情
        resp = client.get(f"/api/v1/game/rounds/{rid}")
        detail = resp.get_json()["data"]
        assert detail["status"] == "running"
        assert detail["code"] == test_data["code"]
        assert detail["account"] is not None
        assert detail["account"]["volume"] == 500000

        # 删除（运行中不可删）
        resp = client.delete(f"/api/v1/game/rounds/{rid}")
        assert resp.get_json()["code"] != 0

        # 暂停后可删
        client.post(f"/api/v1/game/rounds/{rid}/pause", json={})
        resp = client.delete(f"/api/v1/game/rounds/{rid}")
        assert resp.get_json()["code"] == 0
        resp = client.get(f"/api/v1/game/rounds/{rid}")
        assert resp.status_code == 404

    def test_create_validation(self, client):
        # 不存在的 code/date
        resp = client.post("/api/v1/game/rounds", json={"code": "NOPE.SH"})
        assert resp.get_json()["code"] != 0

    def test_dates_api(self, client, test_data):
        resp = client.get(f"/api/v1/game/dates?code={test_data['code']}")
        assert resp.get_json()["code"] == 0
        assert test_data["date"] in resp.get_json()["data"]


class TestOrderFlow:
    def test_full_flow(self, client, test_data, app, engine):
        rid = create_and_start(client, test_data["code"], test_data["date"])
        ctx = engine._rounds[rid]

        # 下单参数校验
        resp = client.post(f"/api/v1/game/rounds/{rid}/order",
                           json={"direction": "buy", "order_type": "limit",
                                 "price": 0, "shares": 100})
        assert resp.get_json()["code"] != 0          # 价格<=0
        resp = client.post(f"/api/v1/game/rounds/{rid}/order",
                           json={"direction": "buy", "order_type": "limit",
                                 "price": 1.0, "shares": 150})
        assert resp.get_json()["code"] != 0          # 非 100 整数倍

        # 资金不足拒单（买单超出可用现金）
        resp = client.post(f"/api/v1/game/rounds/{rid}/order",
                           json={"direction": "buy", "order_type": "limit",
                                 "price": 5.0, "shares": 1000000})
        assert resp.get_json()["code"] != 0

        # 持仓不足拒单（卖出超出可卖持仓）
        resp = client.post(f"/api/v1/game/rounds/{rid}/order",
                           json={"direction": "sell", "order_type": "limit",
                                 "price": 1.0, "shares": 600000})
        assert resp.get_json()["code"] != 0

        # 正常限价买单（会成交：tick.low <= 1.05）
        resp = client.post(f"/api/v1/game/rounds/{rid}/order",
                           json={"direction": "buy", "order_type": "limit",
                                 "price": 1.05, "shares": 10000})
        assert resp.get_json()["code"] == 0
        oid1 = resp.get_json()["data"]["order_id"]

        # 市价买单（立即成交）
        resp = client.post(f"/api/v1/game/rounds/{rid}/order",
                           json={"direction": "buy", "order_type": "market", "shares": 10000})
        assert resp.get_json()["code"] == 0
        oid2 = resp.get_json()["data"]["order_id"]

        # 推进引擎直至成交（尾端自动结算）
        while ctx.index < len(ctx.ticks):
            engine._advance(ctx)

        with app.app_context():
            o1 = GameOrder.query.filter_by(order_id=oid1).first()
            o2 = GameOrder.query.filter_by(order_id=oid2).first()
            assert o1.status == "filled"
            assert o2.status == "filled"
            trades = GameTrade.query.filter_by(round_id=rid).count()
            assert trades == 2

        # 委托/成交/账户查询
        resp = client.get(f"/api/v1/game/rounds/{rid}/orders")
        assert len(resp.get_json()["data"]) == 2
        resp = client.get(f"/api/v1/game/rounds/{rid}/trades")
        assert len(resp.get_json()["data"]) == 2
        resp = client.get(f"/api/v1/game/rounds/{rid}/account")
        acct = resp.get_json()["data"]
        assert acct["volume"] == 500000 + 20000
        assert acct["frozen_cash"] == 0

        # 尾端已自动结算（收盘）
        resp = client.get(f"/api/v1/game/rounds/{rid}")
        assert resp.get_json()["data"]["status"] == "finished"
        assert resp.get_json()["data"]["final_assets"] > 0

        # 结束后同一交易日可重建
        rid2 = create_and_start(client, test_data["code"], test_data["date"])
        assert rid2 != rid


class TestCancelOrder:
    def test_cancel_rules(self, client, test_data, engine):
        rid = create_and_start(client, test_data["code"], test_data["date"])
        ctx = engine._rounds[rid]

        # 挂一个不成交的限价单（0.5 远低于市场）
        resp = client.post(f"/api/v1/game/rounds/{rid}/order",
                           json={"direction": "buy", "order_type": "limit",
                                 "price": 0.5, "shares": 10000})
        oid = resp.get_json()["data"]["order_id"]
        # 冻结生效
        frozen_before = ctx.acct.frozen_cash
        assert frozen_before > 0

        # 撤单成功 → 解冻
        resp = client.post(f"/api/v1/game/rounds/{rid}/cancel",
                           json={"order_id": oid})
        assert resp.get_json()["code"] == 0
        assert ctx.acct.frozen_cash == 0

        # 重复撤单失败
        resp = client.post(f"/api/v1/game/rounds/{rid}/cancel",
                           json={"order_id": oid})
        assert resp.get_json()["code"] != 0

        # 已成交订单不可撤
        resp = client.post(f"/api/v1/game/rounds/{rid}/order",
                           json={"direction": "buy", "order_type": "limit",
                                 "price": 1.05, "shares": 10000})
        oid2 = resp.get_json()["data"]["order_id"]
        engine._process_tick(ctx, ctx.ticks[0])
        resp = client.post(f"/api/v1/game/rounds/{rid}/cancel",
                           json={"order_id": oid2})
        assert resp.get_json()["code"] != 0

        # 不存在的订单
        resp = client.post(f"/api/v1/game/rounds/{rid}/cancel",
                           json={"order_id": "NOPE"})
        assert resp.get_json()["code"] != 0

    def test_cancel_not_running(self, client, test_data):
        """ready 状态不可下单/撤单"""
        resp = client.post("/api/v1/game/rounds", json={"code": test_data["code"],
                                                        "trade_date": test_data["date"]})
        rid = resp.get_json()["data"]["id"]
        resp = client.post(f"/api/v1/game/rounds/{rid}/order",
                           json={"direction": "buy", "order_type": "limit",
                                 "price": 1.0, "shares": 100})
        assert resp.get_json()["code"] != 0
        resp = client.post(f"/api/v1/game/rounds/{rid}/cancel",
                           json={"order_id": "X"})
        assert resp.get_json()["code"] != 0
        # 清理
        client.delete(f"/api/v1/game/rounds/{rid}")


class TestControl:
    def test_pause_resume_speed(self, client, test_data):
        rid = create_and_start(client, test_data["code"], test_data["date"])

        resp = client.post(f"/api/v1/game/rounds/{rid}/pause", json={})
        assert resp.get_json()["code"] == 0
        resp = client.get(f"/api/v1/game/rounds/{rid}")
        assert resp.get_json()["data"]["status"] == "paused"

        resp = client.post(f"/api/v1/game/rounds/{rid}/resume", json={})
        assert resp.get_json()["code"] == 0
        resp = client.get(f"/api/v1/game/rounds/{rid}")
        assert resp.get_json()["data"]["status"] == "running"

        resp = client.post(f"/api/v1/game/rounds/{rid}/speed", json={"speed": 10})
        assert resp.get_json()["code"] == 0
        resp = client.post(f"/api/v1/game/rounds/{rid}/speed", json={"speed": 99})
        assert resp.get_json()["code"] != 0
        resp = client.get(f"/api/v1/game/rounds/{rid}")
        assert resp.get_json()["data"]["speed"] == 10

    def test_same_day_running_reject(self, client, test_data):
        rid = create_and_start(client, test_data["code"], test_data["date"])
        resp = client.post("/api/v1/game/rounds", json={"code": test_data["code"],
                                                        "trade_date": test_data["date"]})
        assert resp.get_json()["code"] != 0
        assert "已有未结束轮次" in resp.get_json()["message"]
