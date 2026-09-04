# ============================================================
# QMT Agent - StockGame 行情采集代理
# 功能:
#   handlebar: 盘中每 3s 触发一次，采集最新 K 线行情 HTTP 上传
#   run_time 心跳任务: 每 60s POST 一次心跳，与行情上传解耦
# 说明:
#   非当日行情数据直接过滤，不参与上报；
#   同一时间点重复上报由后端按 (code, time_key) 幂等去重。
# NOTE: QMT built-in functions are provided by QMT runtime.
#       - timetag_to_datetime()
#       - get_bar_timetag()
#       - ContextInfo.run_time()
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
BACKEND_URL = "http://192.168.1.5:16000"   # StockGame 后端地址（部署后按实际修改）
AGENT_NAME = "qmt_live"
HEARTBEAT_INTERVAL = 60                   # 心跳周期（秒）
SYNC_TIMEOUT = 10

_last_sent_time = None   # 上次成功上报的 time_key（防重复触发日志噪音）


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


def _get_pre_close(ContextInfo):
    """昨收（上一交易日收盘）：tick 行情不含 pre_close 字段，改从日线序列推导

    - 最后一根日线为当日（盘中 1d 动态生成）→ 取其 pre_close（恒为昨收）
    - 最后一根为上一交易日（当日 1d 未生成）→ 取其 close（即昨收）
    取不到有效值返回 0（后端 day_kline 将暂缓生成，tick 入库不受影响）。
    """
    try:
        result = ContextInfo.get_market_data_ex(
            ['close', 'pre_close'], stock_code=[_stock_code],
            count=2, period='1d')
        df = None
        for d in (result or {}).values():
            if d is not None and len(d) > 0:
                df = d
                break
        if df is None:
            return 0.0

        def _f(name):
            try:
                v = float(df.iloc[-1][name])
            except Exception:
                v = float('nan')
            return v if v == v and v > 0 else 0.0

        # 末根日线日期 == 今天（盘中）→ pre_close；否则（盘前/首笔）→ close
        last_date = timetag_to_datetime(int(df.index[-1]), '%Y-%m-%d')
        if last_date == time.strftime("%Y-%m-%d", time.localtime()):
            return _f('pre_close') or _f('close')
        return _f('close') or _f('pre_close')
    except Exception as e:
        _log("获取昨收异常: %s" % e)
        return 0.0


def _get_market_data(ContextInfo):
    """通过 ContextInfo 获取当前 K 线行情"""
    try:
        result = ContextInfo.get_market_data_ex(
            ['close', 'open', 'high', 'low', 'volume', 'amount', 'pre_close'],
            stock_code=[_stock_code],
            count=1,
            period='tick',
        )

        bar = None
        for df in result.values():
            if df is not None and len(df) > 0:
                bar = df.iloc[-1]
                break

        if bar is None:
            _log("无法获取 K 线数据")
            return None

        def _v(k, dft=0):
            try:
                return float(bar[k]) if isinstance(bar, dict) else float(bar.loc[k])
            except Exception:
                return dft

        close_val = _v('close')
        # 昨收：tick 行情无 pre_close 字段，由日线序列推导（见 _get_pre_close）
        last_close = _get_pre_close(ContextInfo)

        # 过滤非当日数据：bar 日期不是今天（如盘前/跨日残留的旧行情）直接丢弃
        bar_timetag = ContextInfo.get_bar_timetag(ContextInfo.barpos)
        trade_date = timetag_to_datetime(bar_timetag, '%Y-%m-%d')
        today = time.strftime("%Y-%m-%d", time.localtime())
        if trade_date != today:
            #_log("数据非当日已过滤: bar=%s today=%s" % (trade_date, today))
            return None

        return {
            "agent_name": AGENT_NAME,
            "code": _stock_code,
            "time_key": timetag_to_datetime(bar_timetag, '%Y-%m-%d %H:%M:%S'),
            "trade_date": trade_date,
            "period": ContextInfo.period,
            "open": _v("open"),
            "high": _v("high"),
            "low": _v("low"),
            "close": close_val,
            "last_close": last_close,
            "volume": _v("volume"),
            "amount": _v("amount", 0),
        }
    except Exception as e:
        _log("获取行情异常: %s" % e)
    return None


def heartbeat(ContextInfo):
    """run_time 定时回调 — 每 60s 上报一次心跳，与行情上传解耦"""
    _http_post("/api/v1/agent/heartbeat", {
        "agent_name": AGENT_NAME,
        "timestamp": time.time(),
    })


def init(ContextInfo):
    """QMT 初始化回调"""
    _log("StockGame Agent 初始化完成 code=%s period=%s backend=%s" % (
        _stock_code, ContextInfo.period, BACKEND_URL))
    # 注册定时心跳任务（run_time 机制替代独立线程）：
    # startTime 设为历史时间使定时器立即启动，之后每 60s 触发一次 heartbeat
    ContextInfo.run_time(
        "heartbeat", "%dnSecond" % HEARTBEAT_INTERVAL, "2000-01-01 00:00:00")


def handlebar(ContextInfo):
    """QMT handlebar 回调 — 每 3s 触发一次，上传最新行情（无交易时间过滤）"""
    try:
        data = _get_market_data(ContextInfo)
        if not data:
            return
        _logdata(data)
        _http_post("/api/v1/agent/tick", data)
    except Exception as e:
        _log("handlebar 异常: %s" % e)
