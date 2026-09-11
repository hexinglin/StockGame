"""
test_timeutil.py — 时间口径测试（统一东八区，与进程/容器时区无关）

覆盖: now_cn 恒为 UTC+8 墙钟、unix 时间戳与北京时间互转（东八区显式解释）、
格式化输出，防止「容器默认 UTC 导致时间偏 8 小时」问题回归。
"""
import time
from datetime import datetime, timezone

from app.utils.timeutil import fmt_cn, from_ts_cn, now_cn, now_str_cn, ts_from_cn


class TestTimeUtil:
    def test_now_cn_is_utc_plus_8(self):
        """now_cn 与 UTC 当前时间恒差 8 小时（naive 墙钟，与进程时区无关）"""
        utc = datetime.now(timezone.utc).replace(tzinfo=None)
        diff = (now_cn() - utc).total_seconds()
        assert abs(diff - 8 * 3600) < 5

    def test_ts_roundtrip(self):
        """unix 时间戳 → 北京时间 → 反解回同一时间戳（显式东八区）"""
        ts = time.time()
        assert abs(ts_from_cn(from_ts_cn(ts)) - ts) < 0.001
        assert from_ts_cn(0) == datetime(1970, 1, 1, 8, 0, 0)   # epoch 0 = 北京 08:00

    def test_fmt_cn(self):
        assert fmt_cn(datetime(2026, 9, 11, 5, 6, 50)) == "2026-09-11 05:06:50"
        assert fmt_cn(None) is None

    def test_now_str_format(self):
        s = now_str_cn()
        assert len(s) == 19 and s[4] == "-" and s[10] == " " and s[13] == ":"
