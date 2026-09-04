# ============================================================
# QMT Agent - StockGame 行情采集代理
# 功能:
#   subscribe_quote 事件驱动: 订阅标的每笔分笔推送即回调上传，
#     替代原 handlebar 3s 轮询（无行情不触发，不空转）
#   run_time 心跳任务: 每 60s POST 一次心跳，与行情上传解耦
# 说明:
#   仅上传当日行情（非当日推送直接过滤）；
#   同一秒内多次推送只上报一次（秒级节流），重复上报由后端按
#   (code, time_key) 幂等去重。
# NOTE: QMT built-in functions are provided by QMT runtime.
#       - timetag_to_datetime()
#       - ContextInfo.run_time()
#       - ContextInfo.subscribe_quote()
# ============================================================
import sys
import os

_bin_path = os.path.dirname(sys.executable)
if _bin_path not in sys.path:
    sys.path.insert(0, _bin_path)
_std_lib = r"D:\python-3.6.8-embed-amd64\Lib"
if os.path.exists(_std_lib) and _std_lib not in sys.path:
    sys.path.insert(1, _std_lib)
_site_pkg = r"D:\python-3.6.8-embed-amd64\Lib\site-packages"
if os.path.exists(_site_pkg) and _site_pkg not in sys.path:
    sys.path.append(_site_pkg)

import json
import time

try:
    import requests
except ImportError:
    requests = None

# ---- 配置 ----
_stock_code = "588000.SH"
_QUOTE_PERIOD = "tick"                  # 订阅周期：tick=分笔（快照变化即推送）
BACKEND_URL = "http://192.168.1.5:16000"   # StockGame 后端地址（部署后按实际修改）
AGENT_NAME = "qmt_live"
HEARTBEAT_INTERVAL = 60                   # 心跳周期（秒）
SYNC_TIMEOUT = 10

_last_sent_time = None   # 上次已上报的 time_key（秒级节流，防同秒重复推送）


def _log(msg):
    t = time.time()
    ms = int((t - int(t)) * 1000)
    print("[%s.%03d] %s" % (time.strftime("%H:%M:%S", time.localtime(t)), ms, msg))

def _logdata(data):
    t = time.time()
    ms = int((t - int(t)) * 1000)
    time_key = data["time_key"]
    print(data["time_key"], '信息', data, time.strftime("%H:%M:%S", time.localtime(t)))

def _http_post(endpoint, data, timeout=SYNC_TIMEOUT):
    """HTTP POST 通用函数，返回解析后的 JSON dict，失败返回 None"""
    url = BACKEND_URL + endpoint
    try:
        if requests is None:
            import urllib.request
            req = urllib.request.Request(
                url, data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        r = requests.post(url, json=data, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _log("HTTP POST %s 失败: %s" % (endpoint, e))
        return None

def _quote_time_key(bar):
    """从订阅推送行情提取秒级 time_key（'%Y-%m-%d %H:%M:%S'）

    优先毫秒时间戳 time/timetag（QMT 内置 timetag_to_datetime，10 位秒级
    戳自动补足毫秒），缺失时退回解析 stime（'20231107110321.000'）。
    """
    ms = bar.get("time")
    if ms is None:
        ms = bar.get("timetag")
    if ms is not None:
        try:
            ms = int(ms)
            if 0 < ms < 100000000000:     # 10 位秒级时间戳 → 毫秒
                ms *= 1000
            return timetag_to_datetime(ms, '%Y-%m-%d %H:%M:%S')
        except Exception:
            pass
    stime = str(bar.get("stime") or "")
    if len(stime) >= 14 and stime[:14].isdigit():
        try:
            return time.strftime('%Y-%m-%d %H:%M:%S',
                                 time.strptime(stime[:14], '%Y%m%d%H%M%S'))
        except Exception:
            return None
    return None


def _on_quote(ContextInfo, data):
    """subscribe_quote 回调 — 订阅标的分笔推送即组装上传

    data 结构: {code: {字段: 值}}（订阅时指定 result_type='dict'）；
    兼容个别版本忽略 result_type 返回 DataFrame/Series 的情况。
    分笔推送字段为 quoter 快照: time(毫秒)/stime/lastPrice/lastClose/
    open/high/low/volume/amount（昨收字段部分版本命名 preClose）。
    """
    try:
        if not isinstance(data, dict):
            return
        bar = data.get(_stock_code)
        if bar is None:
            return
        if hasattr(bar, "iloc"):        # DataFrame → 末行 Series
            bar = bar.iloc[-1]
        if hasattr(bar, "to_dict"):     # Series → dict
            bar = bar.to_dict()

        time_key = _quote_time_key(bar)
        if not time_key:
            return
        # 过滤非当日数据：跨日残留/盘前旧推送直接丢弃
        if time_key[:10] != time.strftime("%Y-%m-%d", time.localtime()):
            #_log("数据非当日已过滤: %s" % time_key)
            return
        # 秒级节流：同一秒内多次推送（同 time_key）只上报一次
        global _last_sent_time
        if time_key == _last_sent_time:
            return
        _last_sent_time = time_key

        def _v(*keys, dft=0.0):
            """按候选字段名取数值，NaN/缺字段视为缺失取 dft"""
            for k in keys:
                try:
                    v = float(bar.get(k))
                except (TypeError, ValueError):
                    continue
                if v == v:
                    return v
            return dft

        close_val = _v("lastPrice", "close")
        if close_val <= 0:
            return
        # 昨收：推送自带 lastClose/preClose 优先，缺失时按日线序列推导兜底
        last_close = _v("lastClose", "preClose")

        payload = {
            "agent_name": AGENT_NAME,
            "code": _stock_code,
            "time_key": time_key,
            "trade_date": time_key[:10],
            "period": _QUOTE_PERIOD,
            "open": _v("open"),
            "high": _v("high"),
            "low": _v("low"),
            "close": close_val,
            "last_close": last_close,
            "volume": _v("volume"),
            "amount": _v("amount"),
        }
        _logdata(payload)
        _http_post("/api/v1/agent/tick", payload)
    except Exception as e:
        _log("订阅回调异常: %s" % e)


def heartbeat(ContextInfo):
    """run_time 定时回调 — 每 60s 上报一次心跳，与行情上传解耦"""
    _http_post("/api/v1/agent/heartbeat", {
        "agent_name": AGENT_NAME,
        "timestamp": time.time(),
    })


def init(ContextInfo):
    """QMT 初始化回调：注册心跳定时任务 + 订阅行情（事件驱动，替代 handlebar）"""
    _log("StockGame Agent 初始化完成 code=%s period=%s backend=%s" % (
        _stock_code, _QUOTE_PERIOD, BACKEND_URL))
    # 注册定时心跳任务（run_time 机制替代独立线程）：
    # startTime 设为历史时间使定时器立即启动，之后每 60s 触发一次 heartbeat
    ContextInfo.run_time(
        "heartbeat", "%dnSecond" % HEARTBEAT_INTERVAL, "2000-01-01 00:00:00")
    # 订阅分笔行情：订阅标的有新分笔（快照变化）即回调 _on_quote，
    # 替代原 handlebar 主图 3s 轮询；回调闭包携带 ContextInfo 供昨收兜底
    sub_id = ContextInfo.subscribe_quote(
        _stock_code, period=_QUOTE_PERIOD, result_type="dict",
        callback=lambda data: _on_quote(ContextInfo, data))
    _log("行情订阅完成 sub_id=%s（>0 成功；-1 失败请检查订阅权限/数量限制）"
         % sub_id)
