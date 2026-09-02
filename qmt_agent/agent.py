# ============================================================
# QMT Agent - StockGame 行情采集代理
# 功能:
#   handlebar: 盘中每 3s 触发一次，采集最新 K 线行情 HTTP 上传
#   独立心跳线程: 每 60s POST 一次心跳，与行情上传解耦
# 说明:
#   不做交易时间过滤——每个 handlebar 触发均上传；
#   同一时间点重复上报由后端按 (code, time_key) 幂等去重。
# NOTE: QMT built-in functions are provided by QMT runtime.
#       - timetag_to_datetime()
#       - get_bar_timetag()
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
import threading
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


def _get_market_data(ContextInfo):
    """通过 ContextInfo 获取当前 K 线行情"""
    try:
        result = ContextInfo.get_market_data_ex(
            ['close', 'open', 'high', 'low', 'volume', 'amount', 'pre_close'],
            stock_code=[_stock_code],
            count=1,
            period=ContextInfo.period,
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
        # 昨收：优先 pre_close，兜底 last_close
        last_close = _v('pre_close', _v('last_close'))

        return {
            "agent_name": AGENT_NAME,
            "code": _stock_code,
            "time_key": timetag_to_datetime(
                ContextInfo.get_bar_timetag(ContextInfo.barpos), '%Y-%m-%d %H:%M:%S'),
            "trade_date": timetag_to_datetime(
                ContextInfo.get_bar_timetag(ContextInfo.barpos), '%Y-%m-%d'),
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


def _heartbeat_loop():
    """独立心跳线程：每 60s 上报一次，与行情上传解耦"""
    _log("心跳线程启动 (interval=%ds)" % HEARTBEAT_INTERVAL)
    while True:
        try:
            _http_post("/api/v1/agent/heartbeat", {
                "agent_name": AGENT_NAME,
                "timestamp": time.time(),
            })
        except Exception as e:
            _log("心跳上报异常: %s" % e)
        time.sleep(HEARTBEAT_INTERVAL)


def init(ContextInfo):
    """QMT 初始化回调"""
    _log("StockGame Agent 初始化完成 code=%s period=%s backend=%s" % (
        _stock_code, ContextInfo.period, BACKEND_URL))
    # 启动独立心跳线程（daemon，QMT 退出自动终止）
    t = threading.Thread(target=_heartbeat_loop, daemon=True)
    t.start()


def handlebar(ContextInfo):
    """QMT handlebar 回调 — 每 3s 触发一次，上传最新行情（无交易时间过滤）"""
    try:
        data = _get_market_data(ContextInfo)
        if not data:
            return
        time_key = data["time_key"]
        _log("上传行情 %s close=%.3f volume=%.0f" % (
            time_key, data["close"], data["volume"]))
        _http_post("/api/v1/agent/tick", data)
    except Exception as e:
        _log("handlebar 异常: %s" % e)
