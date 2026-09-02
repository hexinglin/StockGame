"""
test_agent_api.py — Agent 接入接口测试（tick 幂等 / 心跳 / 状态）
"""
import time

from app.dbdata.database import db
from app.dbdata.models import TickData, AgentStatus


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
