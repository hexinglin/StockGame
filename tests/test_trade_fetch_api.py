"""
test_trade_fetch_api.py — QMT 交易记录拉取闭环接口测试

闭环：页面下发命令（POST trade_fetch，直写 Redis 单键，TTL 2 分钟）→
Agent 轮询直读（GET command）→ 执行上报（POST trade_records，服务端删
命令）→ 整日替换落库 PostgreSQL（trade_records）+ FIFO 配对分析 →
页面查询（GET trade_fetch）；导入通道（POST trade_records/import）。
"""
import pytest

from app.engine import trade_store
from app.messaging.cache import TRADE_CMD_KEY, TRADE_CMD_TTL_SEC, get_cache

_AGENT = "test_trade_agent"
# 测试用独立日期（与真实/其它用例数据隔离），测试后清理 Redis + DB
_DATE = "2098-12-31"
_DATE2 = "2098-12-30"


@pytest.fixture(scope="module", autouse=True)
def _cleanup_trade_fetch_data(app):
    """模块级清理：命令键与测试日期的落库数据/状态"""
    _clear(app)
    yield
    _clear(app)


def _clear(app):
    with app.app_context():
        trade_store.delete_day(_DATE)
        trade_store.delete_day(_DATE2)
    cache = get_cache()
    if cache.available:
        cache.reset_trade_fetch_commands()


def _record(time_, direction, price, volume, tid):
    return {"time": f"{_DATE} {time_}", "code": "588000.SH",
            "name": "科创50ETF", "direction": direction, "price": price,
            "volume": volume, "amount": round(price * volume, 2),
            "trade_id": tid, "order_id": ""}


class TestTradeFetchFlow:
    def test_full_loop(self, client):
        """下发 → 直读命令 → 上报 → 删命令 → 查询结果与配对分析"""
        cache = get_cache()
        cache.reset_trade_fetch_commands()
        # 1. 页面下发命令（Redis 直写，TTL 2 分钟）
        resp = client.post("/api/v1/agent/trade_fetch", json={"date": _DATE})
        body = resp.get_json()
        assert resp.status_code == 200 and body["code"] == 0
        cmd_id = body["data"]["cmd_id"]
        assert body["data"]["type"] == "fetch_trade_records"
        # 命令键剩余 TTL 应落在 (0, 120] 秒内
        ttl = cache._client.ttl(TRADE_CMD_KEY)
        assert 0 < ttl <= TRADE_CMD_TTL_SEC

        # 2. Agent 轮询直读（上报前重复可读，Agent 侧按 cmd_id 去重执行）
        cmd = client.get(f"/api/v1/agent/command?agent_name={_AGENT}").get_json()["data"]
        assert cmd["cmd_id"] == cmd_id and cmd["date"] == _DATE

        # 页面视角：命令仍在（等待执行），返回有效期供展示
        data = client.get(f"/api/v1/agent/trade_fetch?date={_DATE}").get_json()["data"]
        assert data["command"]["cmd_id"] == cmd_id
        assert data["command_ttl_sec"] == TRADE_CMD_TTL_SEC
        assert data["result"] is None

        # 3. Agent 上报：1 买 + 2 卖（前两笔配对，第三笔卖出昨仓）
        records = [
            _record("09:31:00", "buy", 1.0, 100000, "T1"),
            _record("10:00:00", "sell", 1.1, 100000, "T2"),
            _record("10:30:00", "sell", 1.2, 50000, "T3"),
        ]
        resp = client.post("/api/v1/agent/trade_records", json={
            "agent_name": _AGENT, "cmd_id": cmd_id, "date": _DATE,
            "success": True, "account": "60011302", "records": records})
        assert resp.get_json() == {"code": 0, "message": "ok", "count": 3}

        # 4. 命令已删除（Agent 下次轮询不再返回）
        assert client.get("/api/v1/agent/command").get_json()["data"] is None

        # 5. 页面查询：结果（含配对分析）且携带 cmd_id 供对账
        data = client.get(f"/api/v1/agent/trade_fetch?date={_DATE}").get_json()["data"]
        assert data["command"] is None
        r = data["result"]
        assert r["success"] is True and r["count"] == 3
        assert r["cmd_id"] == cmd_id and r["source"] == "agent"
        s = r["summary"]
        assert s["buy_count"] == 1 and s["sell_count"] == 2
        assert s["matched_count"] == 1 and s["unmatched_count"] == 1
        assert len(r["trades"]) == 3 and len(r["pairs"]) == 1
        # 配对净收益 = (1.1-1.0)*100000 - 双边手续费
        assert r["pairs"][0]["net_profit"] == pytest.approx(9982.15)

    def test_reissue_overwrites_previous(self, client):
        """重新下发直接覆盖旧命令（单命令槽）"""
        first = client.post("/api/v1/agent/trade_fetch",
                            json={"date": _DATE2}).get_json()["data"]["cmd_id"]
        second = client.post("/api/v1/agent/trade_fetch",
                             json={"date": _DATE2}).get_json()["data"]["cmd_id"]
        assert first != second
        cmd = client.get("/api/v1/agent/command").get_json()["data"]
        assert cmd["cmd_id"] == second

    def test_stale_report_keeps_newer_command(self, client):
        """旧命令的迟到上报：不删除期间覆盖的新命令"""
        cache = get_cache()
        cache.reset_trade_fetch_commands()
        old_id = client.post("/api/v1/agent/trade_fetch",
                             json={"date": _DATE}).get_json()["data"]["cmd_id"]
        new_id = client.post("/api/v1/agent/trade_fetch",
                             json={"date": _DATE2}).get_json()["data"]["cmd_id"]
        assert old_id != new_id
        # 旧命令（cmd_id=old_id）迟到上报（数据照存，命中的是旧日期）
        resp = client.post("/api/v1/agent/trade_records", json={
            "agent_name": _AGENT, "cmd_id": old_id, "date": _DATE,
            "success": True, "records": []})
        assert resp.get_json()["code"] == 0
        # 新命令仍可被 Agent 直读
        cmd = client.get("/api/v1/agent/command").get_json()["data"]
        assert cmd["cmd_id"] == new_id
        cache.reset_trade_fetch_commands()   # 清理命令键

    def test_failure_report(self, client):
        """Agent 执行失败：删除命令、失败原因存 Redis 供页面展示"""
        cache = get_cache()
        cache.reset_trade_fetch_commands()
        client.post("/api/v1/agent/trade_fetch", json={"date": _DATE2})
        cmd = client.get("/api/v1/agent/command").get_json()["data"]
        resp = client.post("/api/v1/agent/trade_records", json={
            "agent_name": _AGENT, "cmd_id": cmd["cmd_id"], "date": _DATE2,
            "success": False, "error": "QMT 查询超时"})
        assert resp.get_json()["code"] == 0
        assert client.get("/api/v1/agent/command").get_json()["data"] is None
        data = client.get(f"/api/v1/agent/trade_fetch?date={_DATE2}").get_json()["data"]
        assert data["result"]["success"] is False
        assert "超时" in data["result"]["error"]

    def test_invalid_date(self, client):
        resp = client.post("/api/v1/agent/trade_fetch", json={"date": "20261231"})
        assert resp.status_code == 400
        resp = client.get("/api/v1/agent/trade_fetch?date=bad")
        assert resp.status_code == 400

    def test_past_date_rejected(self, client):
        """历史日期不允许命令采集（不发命令，数据读取自数据库/导入）"""
        resp = client.post("/api/v1/agent/trade_fetch",
                           json={"date": "2001-01-01"})
        assert resp.status_code == 400
        assert "历史日期" in resp.get_json()["message"]

    def test_past_date_failed_not_surfaced(self, client, app):
        """历史日期的旧采集失败状态不对外展示（避免遮蔽导入引导）"""
        d = "2001-01-02"
        with app.app_context():
            trade_store.mark_failed(d, "旧命令失败残影")
        try:
            data = client.get(
                "/api/v1/agent/trade_fetch?date=%s" % d).get_json()["data"]
            assert data["result"] is None
        finally:
            with app.app_context():
                trade_store.delete_day(d)


class TestTradeImport:
    def test_import_tsv_and_replace(self, client):
        """导入通道：Tab 文本解析 → 整日替换入库（来源=import）→ 分析复用"""
        text = "\n".join([
            "成交时间\t证券代码\t证券名称\t买卖\t成交价格\t成交数量\t成交金额\t成交编号",
            f"{_DATE} 09:31:00\t588000\t科创50ETF\t证券买入\t1.000\t100000\t100000.00\tI1",
            f"{_DATE} 10:00:00\t588000\t科创50ETF\t证券卖出\t1.100\t100000\t110000.00\tI2",
            "合计\t\t\t\t\t200000\t210000.00\t",
        ])
        resp = client.post("/api/v1/agent/trade_records/import",
                           json={"date": _DATE, "text": text})
        body = resp.get_json()
        assert body["code"] == 0 and body["count"] == 2
        assert body["data"]["parsed"] == 2 and len(body["data"]["skipped"]) == 1
        # 查询：来源=导入，配对分析直接复用
        r = client.get(f"/api/v1/agent/trade_fetch?date={_DATE}").get_json()["data"]["result"]
        assert r["source"] == "import" and r["count"] == 2
        assert r["summary"]["matched_count"] == 1
        # 整日替换：再导入 1 笔 → 只剩 1 笔（不叠加）
        text2 = "\n".join([
            "成交时间,证券代码,买卖,成交价格,成交数量",
            f"{_DATE} 14:00:00,588000.SH,买,1.05,1000",
        ])
        body2 = client.post("/api/v1/agent/trade_records/import",
                            json={"date": _DATE, "text": text2}).get_json()
        assert body2["count"] == 1
        r2 = client.get(f"/api/v1/agent/trade_fetch?date={_DATE}").get_json()["data"]["result"]
        assert r2["count"] == 1 and r2["trades"][0]["price"] == 1.05

    def test_import_date_mismatch(self, client):
        """导入记录日期与所选不符 → 全部跳过并报错"""
        text = "\n".join([
            "成交时间\t证券代码\t买卖\t成交价格\t成交数量",
            "2098-12-25 09:31:00\t588000\t买入\t1.0\t1000",
        ])
        resp = client.post("/api/v1/agent/trade_records/import",
                           json={"date": _DATE, "text": text})
        assert resp.status_code == 400
        assert "日期与所选不符" in resp.get_json()["message"]

    def test_import_rejects_empty_and_bad_date(self, client):
        resp = client.post("/api/v1/agent/trade_records/import",
                           json={"date": _DATE, "text": "  "})
        assert resp.status_code == 400
        resp = client.post("/api/v1/agent/trade_records/import",
                           json={"date": "bad", "text": "x"})
        assert resp.status_code == 400
