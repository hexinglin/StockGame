"""
test_trade_import.py — 成交导出文本解析器单元测试

覆盖：Tab/CSV 分隔、表头识别（高优先/兜底列名）、方向映射、时间格式
（完整/仅时间/五位数字）、代码补后缀（市场列）、合计行与脏行跳过、
日期不符跳过、缺表头/缺必需列报错。
"""
import pytest

from app.engine.trade_import import parse_export_text

_DATE = "2026-09-10"


class TestParseExportText:
    def test_tab_full_columns(self):
        """Tab 全列：方向/代码/编号/金额全映射"""
        text = "\n".join([
            "成交时间\t证券代码\t证券名称\t买卖\t成交价格\t成交数量\t成交金额\t成交编号\t委托编号",
            f"{_DATE} 09:31:00\t588000.SH\t科创50ETF\t证券买入\t1.641\t50000\t82050.00\tN1\tO1",
            f"{_DATE} 14:52:44\t588000\t科创50ETF\t融券卖出\t1.637\t800\t1309.60\tN2\tO2",
        ])
        records, meta = parse_export_text(text, _DATE)
        assert meta["parsed"] == 2 and not meta["skipped"]
        r0, r1 = records
        assert r0["direction"] == "buy" and r0["code"] == "588000.SH"
        assert r0["price"] == 1.641 and r0["volume"] == 50000
        assert r0["amount"] == 82050.0 and r0["trade_id"] == "N1"
        assert r1["direction"] == "sell" and r1["order_id"] == "O2"
        assert r1["time"] == f"{_DATE} 14:52:44"

    def test_csv_and_time_only(self):
        """CSV 分隔 + 仅时间列（结合所选日期）+ 金额缺失按价×量补"""
        text = "\n".join([
            "成交时间,证券代码,买卖,成交价格,成交数量",
            "09:31:15,588000,买入,1.05,10000",
            "093200,588000,卖出,1.06,2000",
        ])
        records, meta = parse_export_text(text, _DATE)
        assert meta["parsed"] == 2
        assert records[0]["time"] == f"{_DATE} 09:31:15"
        assert records[1]["time"] == f"{_DATE} 09:32:00"
        assert records[0]["amount"] == 10500.0

    def test_market_column_fills_suffix(self):
        """无后缀代码 + 市场列 → 自动补 .SH/.SZ"""
        text = "\n".join([
            "成交时间\t证券代码\t交易市场\t买卖\t成交价格\t成交数量",
            f"{_DATE} 09:31:00\t588000\t上海\t买入\t1.0\t1000",
            f"{_DATE} 09:32:00\t159915\t深圳\t卖出\t2.0\t1000",
        ])
        records, _ = parse_export_text(text, _DATE)
        assert records[0]["code"] == "588000.SH"
        assert records[1]["code"] == "159915.SZ"

    def test_direction_variants(self):
        """方向映射：中文买卖/数字代码/英文"""
        text = "\n".join([
            "成交时间\t证券代码\t买卖\t成交价格\t成交数量",
            f"{_DATE} 09:31:00\t588000\t48\t1.0\t1000",
            f"{_DATE} 09:32:00\t588000\t49\t1.0\t1000",
            f"{_DATE} 09:33:00\t588000\t担保品卖出\t1.0\t1000",
            f"{_DATE} 09:34:00\t588000\t融资买入\t1.0\t1000",
        ])
        records, _ = parse_export_text(text, _DATE)
        assert [r["direction"] for r in records] == \
            ["buy", "sell", "sell", "buy"]

    def test_skip_total_and_bad_rows(self):
        """合计行与脏行跳过并记录原因"""
        text = "\n".join([
            "成交时间\t证券代码\t买卖\t成交价格\t成交数量",
            f"{_DATE} 09:31:00\t588000\t买入\t1.0\t1000",
            "合计\t\t\t\t5000",
            f"{_DATE} 10:00:00\t588000\t买入\t1.0\t",
        ])
        records, meta = parse_export_text(text, _DATE)
        assert meta["parsed"] == 1
        assert len(meta["skipped"]) == 2

    def test_date_mismatch_skipped(self):
        """记录自带日期与所选不符 → 跳过（防跨日误导入）"""
        text = "\n".join([
            "成交时间\t证券代码\t买卖\t成交价格\t成交数量",
            "2026-09-09 09:31:00\t588000\t买入\t1.0\t1000",
        ])
        records, meta = parse_export_text(text, _DATE)
        assert not records and len(meta["skipped"]) == 1
        assert "日期与所选不符" in meta["skipped"][0]["reason"]

    def test_no_header_raises(self):
        with pytest.raises(ValueError):
            parse_export_text("abc\ndef", _DATE)

    def test_missing_required_columns_raises(self):
        text = "\n".join([
            "成交时间\t证券名称\t买卖",
            f"{_DATE} 09:31:00\t科创50ETF\t买入",
        ])
        with pytest.raises(ValueError):
            parse_export_text(text, _DATE)

    def test_empty_text(self):
        records, meta = parse_export_text("   \n  ", _DATE)
        assert records == [] and meta["parsed"] == 0
