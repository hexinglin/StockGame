# ============================================================
# QMT Agent (交易记录) - StockGame 历史成交明细采集代理
# 功能:
#   run_time 定时任务（10s）: 轮询后端命令接口，领取"获取某日历史
#     成交明细"命令 → 调用 QMT 成交查询接口（历史优先，缺省回退当日）→
#     上报结果到后端（服务器收到后删除命令并落库 PostgreSQL，永久保留）；
#     心跳并入同一周期（每 60s 附带一次），启动时立即上报一次心跳
# 说明:
#   命令为 Redis 单键（TTL 2 分钟，到期自动消失），Agent 轮询直读即可；
#   同一 cmd_id 只执行一次（服务端删命令前接口会重复返回同一命令）；
#   上报失败自动在下一个 10s 周期重试（先补报，再领新命令）；
#   查询为账户全量成交明细（DEAL），记录方向统一为 buy/sell；
#   兼容性：部分券商 QMT 未注入历史接口（get_history_trade_detail_data），
#   此时改走当日接口（get_trade_detail_data）全量读取并按成交日期过滤：
#   客户端缓存多日则历史可命中；仅缓存当日时给出明确提示（当日功能不受影响）。
# 部署:
#   1. 修改下方 BACKEND_URL 为 StockGame 后端地址；
#   2. 将本文件追加为 QMT 策略运行（周期任意，主要靠 run_time 驱动）；
#   3. 与行情采集 agent.py 可并行运行（独立策略互不影响）。
# NOTE: QMT built-in functions are provided by QMT runtime.
#       - ContextInfo.run_time()
#       - get_history_trade_detail_data()
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
BACKEND_URL = "http://192.168.1.5:16000"    # StockGame 后端地址（部署后按实际修改）
AGENT_NAME = "qmt_trade"                    # 与行情 Agent（qmt_live）区分
AGENT_ROLE = "交易记录"                      # 角色（心跳上报，监控面板展示）
_account_id = "60011302"                    # 资金账号（与 AutoTrade 实盘共用）
_ACCOUNT_TYPE = "CREDIT"                    # 账号类型：CREDIT=信用 STOCK=普通
POLL_INTERVAL = 10                          # 命令轮询周期（秒）
HEARTBEAT_INTERVAL = 60                     # 心跳周期（秒）
SYNC_TIMEOUT = 15

# 已执行过的命令 id（服务器消除前不重复执行）
_handled_cmds = []
# 上报失败待重试的报告（下个轮询周期先补报）
_pending_report = None
# 轮询计数（每 N 次轮询附带上报一次心跳）
_poll_ticks = 0


def _log(msg):
    t = time.time()
    ms = int((t - int(t)) * 1000)
    print("[%s.%03d] %s" % (time.strftime("%H:%M:%S", time.localtime(t)), ms, msg))


def _http_get(endpoint, timeout=SYNC_TIMEOUT):
    """HTTP GET 通用函数，成功返回 JSON dict，失败返回 None"""
    url = BACKEND_URL + endpoint
    try:
        if requests is None:
            import urllib.request
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _log("HTTP GET %s 失败: %s" % (endpoint, e))
        return None


def _http_post(endpoint, data, timeout=SYNC_TIMEOUT):
    """HTTP POST 通用函数，成功返回 JSON dict，失败返回 None"""
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


# ────────────── 历史成交明细解析 ──────────────

def _is_deal_obj(x):
    """判定是否为 QMT 成交对象（按特征字段探测）"""
    return hasattr(x, "m_strInstrumentID") or hasattr(x, "m_dPrice")


def _iter_history_objects(result):
    """兼容 get_history_trade_detail_data 的多种返回形态

    A. 平铺对象列表  [dealObj, dealObj, ...]
    B. 分组对 (timetag, [obj, ...]) 列表/元组（官方示例形态）
    """
    if not result:
        return []
    objs = []
    # 形态 B：形如 [timetag, [obj...]]（首元素非对象、次元素为列表）
    try:
        if (isinstance(result, (list, tuple)) and len(result) == 2
                and not _is_deal_obj(result[0])
                and isinstance(result[1], (list, tuple))):
            return list(result[1])
    except Exception:
        pass
    for item in result:
        try:
            if _is_deal_obj(item):          # 形态 A
                objs.append(item)
            elif isinstance(item, (list, tuple)) and len(item) >= 2 \
                    and isinstance(item[1], (list, tuple)):
                objs.extend(item[1])        # 形态 B
        except Exception:
            continue
    return objs


def _num(obj, names, dft=0.0, cast=float):
    """按候选字段名依次取数值（float/int），均缺失时取 dft"""
    for n in names:
        try:
            v = getattr(obj, n, None)
            if v is None:
                continue
            return cast(v)
        except (TypeError, ValueError):
            continue
    return dft


def _norm_date(d, fallback):
    """日期归一化：'20240911' → '2024-09-11'；失败回退 fallback"""
    d = str(d or "").strip()
    if len(d) >= 8 and d[:8].isdigit():
        return "%s-%s-%s" % (d[:4], d[4:6], d[6:8])
    return fallback


def _norm_time(obj, date_str):
    """成交时间归一化为 'YYYY-MM-DD HH:MM:SS'

    兼容 m_strTradeTime 的多种形态：'92500' / '102345' / '10:23:45' /
    '20240911 10:23:45' / 'YYYYMMDDHHMMSS'，结合 m_strTradeDate 组装；
    日期优先级：m_strTradeDate 字段 > 时间串自带日期 > 查询日期兜底
    （与 _deal_date 的提取规则保持一致，避免过滤与展示日期不一致）。
    """
    t = str(getattr(obj, "m_strTradeTime", "") or "").strip()
    field_date = _norm_date(getattr(obj, "m_strTradeDate", ""), "")
    d = field_date or date_str
    if not t:
        return "%s 00:00:00" % d
    if ":" in t:
        # 已为完整形态 'YYYY-MM-DD HH:MM:SS'
        if len(t) >= 19 and t[4:5] == "-" and t[13:14] == ":":
            return t
        # 带紧凑日期前缀 'YYYYMMDD HH:MM:SS' → 去前缀取时间
        if len(t) >= 10 and t[:8].isdigit() and t[8] in (" ", "T"):
            dd = field_date or _norm_date(t[:8], date_str)
            return "%s %s" % (dd, t[9:].strip())
        return "%s %s" % (d, t)
    if t.isdigit() and 1 <= len(t) <= 6:         # '92500'→09:25:00 / '102345'
        t6 = t.rjust(6, "0")
        return "%s %s:%s:%s" % (d, t6[:2], t6[2:4], t6[4:6])
    if len(t) >= 14 and t[:14].isdigit():       # 'YYYYMMDDHHMMSS'
        return "%s-%s-%s %s:%s:%s" % (t[:4], t[4:6], t[6:8],
                                      t[8:10], t[10:12], t[12:14])
    return "%s %s" % (d, t)


def _direction(obj):
    """买卖方向：m_nOffsetFlag 48=买 49=卖（兼容 opType 23/24 与中文标记）"""
    raw = getattr(obj, "m_nOffsetFlag", None)
    try:
        raw = int(raw)
    except (TypeError, ValueError):
        raw = None
    if raw in (48, 23):
        return "buy"
    if raw in (49, 24):
        return "sell"
    opt = str(getattr(obj, "m_strOptName", "") or "")
    if "买" in opt:
        return "buy"
    if "卖" in opt:
        return "sell"
    return "unknown"


def _deal_to_record(obj, date_str):
    """QMT 成交对象 → 上报记录（字段缺失按容错兜底）"""
    instrument = str(getattr(obj, "m_strInstrumentID", "") or "")
    exchange = str(getattr(obj, "m_strExchangeID", "") or "")
    price = _num(obj, ["m_dPrice", "m_dTradedPrice"])
    volume = _num(obj, ["m_nVolume", "m_nVolumeTraded"], 0, int)
    if volume <= 0:
        return None
    amount = _num(obj, ["m_dTradeAmount"])
    if amount <= 0:
        amount = price * volume
    return {
        "time": _norm_time(obj, date_str),
        "code": ("%s.%s" % (instrument, exchange)) if exchange else instrument,
        "name": str(getattr(obj, "m_strInstrumentName", "") or ""),
        "direction": _direction(obj),
        "price": float(price),
        "volume": int(volume),
        "amount": round(float(amount), 2),
        "trade_id": str(getattr(obj, "m_strTradeID", "") or ""),
        "order_id": str(getattr(obj, "m_strOrderSysID", "") or ""),
    }


def _deal_date(obj):
    """从成交对象提取真实成交日期 'YYYY-MM-DD'；提取不到返回空串"""
    d = str(getattr(obj, "m_strTradeDate", "") or "").strip()
    if len(d) >= 10 and d[4:5] == "-" and d[7:8] == "-":
        return d[:10]                      # 'YYYY-MM-DD'
    d = _norm_date(d, "")
    if d:
        return d                           # 'YYYYMMDD'
    t = str(getattr(obj, "m_strTradeTime", "") or "").strip()
    if len(t) >= 10 and t[4:5] == "-" and t[7:8] == "-":
        return t[:10]
    if len(t) >= 8 and t[:8].isdigit():
        return _norm_date(t[:8], "")
    return ""


def _records_from_objects(result, date_str):
    """成交对象列表 → 记录列表（解析容错 + 按成交时间排序）"""
    records = []
    for obj in _iter_history_objects(result):
        try:
            rec = _deal_to_record(obj, date_str)
        except Exception as e:
            _log("成交记录解析异常: %s" % e)
            rec = None
        if rec:
            records.append(rec)
    records.sort(key=lambda r: r.get("time") or "")
    return records


def _fetch_trade_records(date_str):
    """查询某日成交明细（全账户，DEAL），返回记录列表

    优先历史接口（get_history_trade_detail_data，按日期区间查询）；
    未注入时（部分券商版本）改走当日接口（get_trade_detail_data）全量读取
    后按成交日期过滤：客户端缓存多日则历史日期可命中；仅缓存当日时给出
    明确提示（消息里附返回记录的实际日期，便于确认环境行为）。
    """
    history_fn = globals().get("get_history_trade_detail_data")
    if callable(history_fn):
        ymd = date_str.replace("-", "")
        result = history_fn(_account_id, _ACCOUNT_TYPE, "DEAL", ymd, ymd)
        return _records_from_objects(result, date_str)

    detail_fn = globals().get("get_trade_detail_data")
    if detail_fn is None:
        raise RuntimeError("该 QMT 环境未注入任何成交查询接口")

    result = detail_fn(_account_id, _ACCOUNT_TYPE, "DEAL")
    today = time.strftime("%Y-%m-%d")
    records = []
    total = 0
    seen_dates = set()
    for obj in _iter_history_objects(result):
        total += 1
        try:
            d = _deal_date(obj)
            if d:
                seen_dates.add(d)
                if d != date_str:
                    continue        # 非所选日期（历史尝试时按日期过滤）
            elif date_str != today:
                continue            # 无日期字段：仅当日查询时保留
            rec = _deal_to_record(obj, date_str)
        except Exception as e:
            _log("成交记录解析异常: %s" % e)
            rec = None
        if rec:
            records.append(rec)
    records.sort(key=lambda r: r.get("time") or "")

    if date_str != today:
        _log("历史尝试 date=%s: 返回 %d 笔（日期: %s），命中 %d 笔"
             % (date_str, total, ",".join(sorted(seen_dates)) or "未知",
                len(records)))
        if not records:
            raise RuntimeError(
                "未获取到 %s 的成交：该 QMT 客户端本地缓存仅含当日成交"
                "（返回记录日期: %s），历史查询需 QMT 支持 "
                "get_history_trade_detail_data 接口"
                % (date_str, ",".join(sorted(seen_dates)) or "未知日期"))
    return records


# ────────────── 命令轮询与上报 ──────────────

def heartbeat(ContextInfo):
    """心跳上报（启动时调用一次 + 并入轮询周期每 60s 一次）"""
    _http_post("/api/v1/agent/heartbeat", {
        "agent_name": AGENT_NAME,
        "role": AGENT_ROLE,
        "timestamp": time.time(),
    })


def _report(payload):
    """上报执行结果；失败返回 False（置 _pending_report 下轮重试）"""
    resp = _http_post("/api/v1/agent/trade_records", payload)
    return bool(resp) and resp.get("code") == 0


def poll_command(ContextInfo):
    """run_time 定时回调 — 每 10s 轮询命令并执行（心跳并入本周期上报）

    顺序：先补报上次失败的报告（服务器消除命令），再领取新命令；
    同一 cmd_id 只执行一次（服务器消除前接口仍会返回同一命令）。
    """
    global _pending_report, _poll_ticks
    _poll_ticks += 1
    # 心跳并入轮询周期：每 HEARTBEAT_INTERVAL/POLL_INTERVAL 次上报一次
    if _poll_ticks % max(1, HEARTBEAT_INTERVAL // POLL_INTERVAL) == 0:
        heartbeat(ContextInfo)

    if _pending_report is not None:
        if _report(_pending_report):
            _log("补报成功 cmd_id=%s" % _pending_report.get("cmd_id", ""))
            _pending_report = None
        return

    resp = _http_get("/api/v1/agent/command?agent_name=" + AGENT_NAME)
    cmd = (resp or {}).get("data")
    if not cmd:
        return
    cmd_id = str(cmd.get("cmd_id") or "")
    date_str = str(cmd.get("date") or "")
    if not cmd_id or not date_str:
        _log("命令字段缺失，忽略: %s" % cmd)
        return
    if cmd_id in _handled_cmds:
        return      # 已执行，等待服务器消除命令

    _log("领取命令 cmd_id=%s type=%s date=%s"
         % (cmd_id, cmd.get("type"), date_str))
    try:
        records = _fetch_trade_records(date_str)
        payload = {
            "agent_name": AGENT_NAME,
            "cmd_id": cmd_id,
            "date": date_str,
            "success": True,
            "account": _account_id,
            "account_type": _ACCOUNT_TYPE,
            "records": records,
        }
        _log("查询完成 date=%s 成交 %d 笔" % (date_str, len(records)))
    except Exception as e:
        payload = {
            "agent_name": AGENT_NAME,
            "cmd_id": cmd_id,
            "date": date_str,
            "success": False,
            "account": _account_id,
            "account_type": _ACCOUNT_TYPE,
            "error": str(e),
            "records": [],
        }
        _log("查询失败 date=%s: %s" % (date_str, e))

    # 记录已处理（无论上报成败，避免重复执行）
    _handled_cmds.append(cmd_id)
    if len(_handled_cmds) > 200:
        del _handled_cmds[0]

    if not _report(payload):
        _log("上报失败，下个周期重试 cmd_id=%s" % cmd_id)
        _pending_report = payload


def init(ContextInfo):
    """QMT 初始化回调：注册命令轮询定时任务"""
    _log("StockGame 交易记录 Agent 初始化完成 account=%s type=%s backend=%s"
         % (_account_id, _ACCOUNT_TYPE, BACKEND_URL))
    try:
        # 单定时器：startTime 设为历史时间 → 立即启动，每 10s 触发 poll_command
        ContextInfo.run_time(
            "poll_command", "%dnSecond" % POLL_INTERVAL, "2000-01-01 00:00:00")
        _log("定时任务已注册：命令轮询 %ds（每 %ds 附带一次心跳上报）"
             % (POLL_INTERVAL, HEARTBEAT_INTERVAL))
    except Exception as e:
        _log("run_time 注册失败: %s" % e)
    # 启动即上报一次心跳：监控面板立即可见
    heartbeat(ContextInfo)


def handlebar(ContextInfo):
    """本策略完全由 run_time 驱动，handlebar 不做处理"""
    pass
