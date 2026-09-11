"""
test_agent_api.py — Agent 接入接口测试（tick 幂等 / 心跳 / 状态）
"""
import time

import pytest

from app.dbdata.database import db
from app.dbdata.models import (TickData, AgentStatus, GameDay)
from app.messaging.cache import get_cache
from app.utils.timeutil import ts_from_cn

# 测试写入使用的 agent 名 / 标的（与真实数据隔离，测试后统一清理）
_API_CODE = "API588000"
_AGENTS = ("test_agent", "hb_test", "role_test", "del_test")


@pytest.fixture(scope="module", autouse=True)
def _cleanup_agent_test_data(app):
    """模块级清理：测试写入的行情/日记录/agent 状态/Redis 心跳/上传记录不留库

    setup 清历史遗留（防影响断言），teardown 清本次用例写入（tick 接口会
    派生 game_days、覆盖 live 行情快照与上传记录，需一并恢复）。
    """
    _cleanup_agent_data(app)
    yield
    _cleanup_agent_data(app)


def _cleanup_agent_data(app):
    with app.app_context():
        TickData.query.filter_by(code=_API_CODE).delete()
        GameDay.query.filter_by(code=_API_CODE).delete()
        for name in _AGENTS:
            AgentStatus.query.filter_by(agent_name=name).delete()
        db.session.commit()
    cache = get_cache()
    for name in _AGENTS:
        cache.delete_heartbeat(name)
        cache.delete_latest_upload(name)   # 按 Agent 分键的上传记录
    # tick 上传会覆盖全局实时行情快照与全局上传记录，测试后清除（真实 agent 下次上报自动重建）
    cache.delete_quote("live")
    cache.delete_latest_upload()


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
        game_days 暂缓生成；后续携带有效 last_close 的上传自动补齐"""
        code, date = "API588000", "2099-03-01"
        with app.app_context():
            TickData.query.filter_by(code=code, trade_date=date).delete()
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
            # game_days 暂缓：零写入
            assert GameDay.query.filter_by(
                code=code, trade_date=date).count() == 0

        # 同一 time_key 携带有效 last_close 重传 → game_days 自动补齐
        payload["last_close"] = 1.2
        resp = client.post("/api/v1/agent/tick", json=payload)
        assert resp.get_json()["code"] == 0
        with app.app_context():
            day = GameDay.query.filter_by(
                code=code, trade_date=date).first()
            assert day is not None
            assert day.last_close == 1.2
            assert day.tick_count == 1


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
            # 库内为北京时间墙钟 → 显式按东八区反解（与进程时区无关）
            assert abs(ts_from_cn(st.last_heartbeat_at) - ts) < 2
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


class TestMultiAgentStatus:
    def test_heartbeat_with_role_enriches_status(self, client):
        """心跳携带 role：状态查询返回角色/心跳年龄/上传标记"""
        resp = client.post("/api/v1/agent/heartbeat", json={
            "agent_name": "role_test", "role": "行情采集",
            "timestamp": time.time()})
        assert resp.get_json()["code"] == 0
        data = client.get("/api/v1/agent/status").get_json()["data"]
        me = [x for x in data if x["agent_name"] == "role_test"]
        assert len(me) == 1
        assert me[0]["role"] == "行情采集"
        assert me[0]["is_alive"] is True
        assert me[0]["age_sec"] is not None and me[0]["age_sec"] < 60
        assert me[0]["has_latest_upload"] is False

    def test_status_offline_first_order(self, client, app):
        """多 Agent 状态：离线优先排序（先暴露问题）"""
        now = time.time()
        for name in ("role_test", "hb_test"):
            client.post("/api/v1/agent/heartbeat",
                        json={"agent_name": name, "timestamp": now})
        with app.app_context():
            st = AgentStatus.query.filter_by(agent_name="hb_test").first()
            st.is_alive = False
            db.session.commit()
        data = client.get("/api/v1/agent/status").get_json()["data"]
        names = [x["agent_name"] for x in data
                 if x["agent_name"] in ("role_test", "hb_test")]
        assert names == ["hb_test", "role_test"]   # 离线在前

    def test_per_agent_latest_upload(self, client):
        """/latest?agent= 返回该 Agent 最近一条；缺省返回全局最近一条"""
        payload = {
            "agent_name": "test_agent", "code": "API588000",
            "trade_date": "2099-02-01", "time_key": "2099-02-01 09:30:00",
            "open": 1.0, "high": 1.01, "low": 0.99, "close": 1.005,
            "volume": 1000, "amount": 1005, "last_close": 1.0,
        }
        assert client.post("/api/v1/agent/tick", json=payload).get_json()["code"] == 0
        me = client.get("/api/v1/agent/latest?agent=test_agent").get_json()["data"]
        assert me and me["agent_name"] == "test_agent"
        assert me["time_key"] == "2099-02-01 09:30:00"
        glob = client.get("/api/v1/agent/latest").get_json()["data"]
        assert glob and glob["agent_name"] == "test_agent"
        # 状态查询附 has_latest_upload 标记（监控面板据此显示入口）
        data = client.get("/api/v1/agent/status").get_json()["data"]
        row = [x for x in data if x["agent_name"] == "test_agent"][0]
        assert row["has_latest_upload"] is True
        # 未上报的 Agent → 无分键记录
        assert client.get("/api/v1/agent/latest?agent=role_test").get_json()["data"] is None


class TestAgentRemoval:
    def test_delete_requires_offline(self, client, app):
        """Agent 移除：在线拒绝；离线可删（连带清理心跳，再删 404）"""
        client.post("/api/v1/agent/heartbeat", json={
            "agent_name": "del_test", "timestamp": time.time()})
        resp = client.delete("/api/v1/agent/status/del_test")
        assert resp.status_code == 400          # 在线拒绝
        with app.app_context():
            AgentStatus.query.filter_by(agent_name="del_test").first().is_alive = False
            db.session.commit()
        resp = client.delete("/api/v1/agent/status/del_test")
        assert resp.get_json()["code"] == 0
        with app.app_context():
            assert AgentStatus.query.filter_by(agent_name="del_test").first() is None
        assert get_cache().get_heartbeat("del_test") == 0
        assert client.delete("/api/v1/agent/status/del_test").status_code == 404
