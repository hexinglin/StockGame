"""
test_agent_api.py — Agent 接入接口测试（tick 幂等 / 心跳 / 状态）
"""
import time

import pytest

from app.dbdata.database import db
from app.dbdata.models import (TickData, AgentStatus, DayKline, GameDay)
from app.messaging.cache import get_cache

# 测试写入使用的 agent 名 / 标的（与真实数据隔离，测试后统一清理）
_API_CODE = "API588000"
_AGENTS = ("test_agent", "hb_test")


@pytest.fixture(scope="module", autouse=True)
def _cleanup_agent_test_data(app):
    """模块级清理：测试写入的行情/日记录/agent 状态/Redis 心跳不留库

    setup 清历史遗留（防影响断言），teardown 清本次用例写入（tick 接口会
    派生 day_kline/game_days、覆盖 live 行情快照，需一并恢复）。
    """
    with app.app_context():
        TickData.query.filter_by(code=_API_CODE).delete()
        DayKline.query.filter_by(code=_API_CODE).delete()
        GameDay.query.filter_by(code=_API_CODE).delete()
        for name in _AGENTS:
            AgentStatus.query.filter_by(agent_name=name).delete()
        db.session.commit()
    yield
    with app.app_context():
        TickData.query.filter_by(code=_API_CODE).delete()
        DayKline.query.filter_by(code=_API_CODE).delete()
        GameDay.query.filter_by(code=_API_CODE).delete()
        for name in _AGENTS:
            AgentStatus.query.filter_by(agent_name=name).delete()
        db.session.commit()
        for name in _AGENTS:
            get_cache().delete_heartbeat(name)
        # tick 上传会覆盖全局实时行情快照，测试后清除（真实 agent 下次上报自动重建）
        get_cache().delete_quote("live")


class TestTickAPI:
    def test_upload_tick_no_time_filter(self, client, app):
        """无交易时间过滤：任意时间点均接受"""
        with app.app_context():
            TickData.query.filter_by(code="API588000").delete()
            db.session.commit()
        for tk in ["2099-02-01 09:15:00",   # 盘前
                   "2099-02-01 11:35:00",   # 午休
                   "2099-02-01 15:05:00",   # 盘后
                   "2099-02-01 09:30:00"]:  # 盘中
            resp = client.post("/api/v1/agent/tick", json={
                "agent_name": "test_agent", "code": "API588000",
                "trade_date": tk[:10], "time_key": tk,
                "open": 1.0, "high": 1.01, "low": 0.99, "close": 1.005,
                "volume": 1000, "amount": 1005, "last_close": 1.0,
            })
            assert resp.status_code == 200
            assert resp.get_json()["code"] == 0
        with app.app_context():
            cnt = TickData.query.filter_by(code="API588000").count()
            assert cnt == 4

    def test_tick_idempotent_same_time_key(self, client, app):
        """同 (code, time_key) 重复上报：行数不变，值更新"""
        with app.app_context():
            TickData.query.filter_by(code="API588000").delete()
            db.session.commit()
        payload = {
            "agent_name": "test_agent", "code": "API588000",
            "trade_date": "2099-02-01", "time_key": "2099-02-01 10:00:00",
            "open": 1.0, "high": 1.01, "low": 0.99, "close": 1.005,
            "volume": 1000, "amount": 1005, "last_close": 1.0,
        }
        assert client.post("/api/v1/agent/tick", json=payload).get_json()["code"] == 0
        payload["close"] = 1.02
        payload["volume"] = 2000
        assert client.post("/api/v1/agent/tick", json=payload).get_json()["code"] == 0
        with app.app_context():
            rows = TickData.query.filter_by(code="API588000").all()
            assert len(rows) == 1
            assert rows[0].close == 1.02
            assert rows[0].volume == 2000

    def test_tick_missing_fields(self, client):
        resp = client.post("/api/v1/agent/tick", json={"code": "X"})
        assert resp.status_code == 400

    def test_upload_tick_missing_last_close_day_deferred(self, client, app):
        """昨收缺失（hint 无效且无库内旧值/外部回补）→ tick 照常真正入库；
        day_kline/game_days 暂缓生成；后续携带有效 last_close 的上传自动补齐"""
        code, date = "API588000", "2099-03-01"
        with app.app_context():
            TickData.query.filter_by(code=code, trade_date=date).delete()
            DayKline.query.filter_by(code=code, trade_date=date).delete()
            GameDay.query.filter_by(code=code, trade_date=date).delete()
            db.session.commit()
        payload = {
            "agent_name": "test_agent", "code": code, "trade_date": date,
            "time_key": f"{date} 10:00:00",
            "open": 1.0, "high": 1.01, "low": 0.99, "close": 1.0,
            "volume": 1000, "amount": 1000,   # 不带 last_close
        }
        resp = client.post("/api/v1/agent/tick", json=payload)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["code"] == 0
        assert "昨收" in body["message"]
        with app.app_context():
            # tick 已真正入库（上传主链路不受 day 暂缓影响）
            assert TickData.query.filter_by(
                code=code, trade_date=date).count() == 1
            # day_kline/game_days 暂缓：零写入
            assert DayKline.query.filter_by(
                code=code, trade_date=date).count() == 0
            assert GameDay.query.filter_by(
                code=code, trade_date=date).count() == 0

        # 同一 time_key 携带有效 last_close 重传 → day_kline/game_days 自动补齐
        payload["last_close"] = 1.2
        resp = client.post("/api/v1/agent/tick", json=payload)
        assert resp.get_json()["code"] == 0
        with app.app_context():
            day = DayKline.query.filter_by(
                code=code, trade_date=date).first()
            assert day is not None
            assert day.last_close == 1.2
            assert day.tick_count == 1
            assert GameDay.query.filter_by(
                code=code, trade_date=date).count() == 1


class TestHeartbeatAPI:
    def test_heartbeat_updates_db_and_redis(self, client, app):
        from app.messaging.cache import get_cache
        ts = time.time()
        resp = client.post("/api/v1/agent/heartbeat", json={
            "agent_name": "hb_test", "timestamp": ts})
        assert resp.status_code == 200
        with app.app_context():
            st = AgentStatus.query.filter_by(agent_name="hb_test").first()
            assert st is not None
            assert st.is_alive is True
            assert abs(st.last_heartbeat_at.timestamp() - ts) < 2
        assert abs(get_cache().get_heartbeat("hb_test") - ts) < 2

    def test_heartbeat_missing_name(self, client):
        resp = client.post("/api/v1/agent/heartbeat", json={})
        assert resp.status_code == 400

    def test_status_query(self, client):
        resp = client.get("/api/v1/agent/status")
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        names = [x["agent_name"] for x in data]
        assert "hb_test" in names
