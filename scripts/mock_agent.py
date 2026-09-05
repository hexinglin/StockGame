"""
mock_agent.py — 模拟 QMT Agent（自测工具）

用法:
  # 1. 数据拓展模式（默认）：从 AutoTrade 库 1min K 线拓展为快照流写入 tick_data
  python scripts/mock_agent.py --from-autotrade --code 588000.SH --date 2026-08-14

  # 2. 实时模式：按 3s 间隔 POST 行情 + 每 60s POST 心跳（--sleep 可调速）
  python scripts/mock_agent.py --live --code 588000.SH --date 2026-08-14 --sleep 0.1

  # 3. 随机兜底：纯随机生成完整交易日
  python scripts/mock_agent.py --random --gen-days 3

  # 4. 内置场景：单边上涨/下跌/震荡
  python scripts/mock_agent.py --scenario single_up --code 588000.SH

说明:
  行情快照化（2026-09 起）：QMT 真实推送为"当日某时刻的股票快照"，模拟侧同
  口径生成快照流写入 tick_data/tick_data_sim：
    - 每根 1min bar 拆 20 个 3s 快照点（:00~:57，恰覆盖前端一个展示分钟）
    - 价格路径/量能拆分复用 expand_bar（20 点量能之和=bar.volume，守恒）
    - bar.high/bar.low 嵌入序列最近点，滚动极值随点演进（新高/新低出现在
      嵌入点所在时刻）
    - volume/amount 输出为"增量前缀累计"（快照口径：当日累计，单调不减），
      游戏消费端差分还原分钟量能；high/low 为滚动极值、close 为最新价
  今开 open / 昨收 last_close 为当日常量不入 tick 表：随生成值作为 hint
  写入 game_days（与 tick 对齐的天维度真实行情，日期管理唯一权威表）。
"""
import argparse
import datetime
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
import requests

from app.utils.config import Config


# ================= 默认参数区（改这里后直接运行即可） =================
DEFAULT_CODE = "588000.SH"     # 股票代码
DEFAULT_DATE = "2026-08-14"    # 交易日 YYYY-MM-DD（--from-autotrade/--scenario 默认）
# ====================================================================


# ───────────── 工具 ─────────────

def parse_dsn(dsn: str) -> dict:
    """解析 postgresql://user:pass@host:port/dbname"""
    prefix = "postgresql://"
    if dsn.startswith(prefix):
        dsn = dsn[len(prefix):]
    userinfo, _, rest = dsn.partition("@")
    user, _, password = userinfo.partition(":")
    host, _, port_db = rest.partition(":")
    if "/" in port_db:
        port, _, dbname = port_db.partition("/")
    else:
        port, dbname = port_db, "postgres"
    return {"user": user, "password": password, "host": host, "port": port, "dbname": dbname}


def get_conn(dsn: str):
    return psycopg2.connect(**parse_dsn(dsn))


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ───────────── 1min → 3s 数据拓展 ─────────────

def expand_bar(bar: dict) -> list:
    """将一根 1min bar 拓展为 20 根 3s bar（时间戳自 bar 起始每 3s 一根）

    bar: {time_key(str), open, high, low, close, volume}
    返回: [{time_key, open, high, low, close, volume}]
    """
    n = 20
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    volume = bar["volume"]
    base_dt = datetime.datetime.strptime(bar["time_key"], "%Y-%m-%d %H:%M:%S")

    # 20 根随机权重（总和守恒）
    weights = [random.uniform(0.5, 1.5) for _ in range(n)]
    total_w = sum(weights)
    vols = [int(round(volume * w / total_w)) for w in weights]
    # 修正取整误差，保证总和守恒
    diff = volume - sum(vols)
    if diff:
        vols[-1] += diff
    for i in range(n):
        vols[i] = max(0, vols[i])

    # 构造 20 根 OHLC：首根 open=o、末根 close=c，high/low 落入区间内单根
    prices = []
    if n == 1:
        prices = [c]
    else:
        # 线性插值 + 小幅随机，生成 20 个收盘价，起点 o 终点 c，波动限制在 [l, h] 内
        for i in range(n):
            ratio = i / (n - 1)
            base = o + (c - o) * ratio
            jitter = random.uniform(-1, 1) * (h - l) * 0.25 * math.sin(math.pi * ratio)
            prices.append(base + jitter)
        prices[0] = o
        prices[-1] = c
        # 夹取到 [l, h]
        prices = [max(l, min(h, p)) for p in prices]
        prices[0] = o
        prices[-1] = c

    bars = []
    for i in range(n):
        dt = base_dt + datetime.timedelta(seconds=3 * i)
        p_open = prices[i - 1] if i > 0 else o
        p_close = prices[i]
        p_high = max(p_open, p_close)
        p_low = min(p_open, p_close)
        bars.append({
            "time_key": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "open": round(p_open, 3),
            "high": round(p_high, 3),
            "low": round(p_low, 3),
            "close": round(p_close, 3),
            "volume": vols[i],
        })
    return bars


def gen_snapshots_from_minutes(minutes: list, last_close: float) -> list:
    """1m bar 列表 → 当日快照流（快照口径，2026-09 起替代 3s 增量 bar）

    每根 1min bar（hh:mm:00）展开 20 个 3s 价格点（hh:mm:00~:57，恰覆盖前端
    一个展示分钟，分钟边界与 1m bar 无缝对齐）：价格路径/量能拆分复用
    expand_bar（20 点增量之和 = bar.volume，量能守恒），并把 bar.high/bar.low
    嵌入序列最近点以保证日极值还原且出现在对应分钟。随后全局滚动计算每点
    high = max(此前所有价格)/low = min(此前所有价格)（随点逐跳变化，新高/新低
    出现在嵌入点所在时刻，支撑前端今高/今低同步刷新）、volume/amount = 增量
    前缀累计（amount_i = close_i × vol_i）。输出点序列的分钟语义自然成立：
    第 20 点（:57）close = bar.close（前端分钟定型价 = 该分钟 1m 收盘）、
    该分钟 20 点量能之和 = bar.volume（分钟量柱 = 源分钟量能）。

    返回: [{time_key, high, low, close, volume(当日累计), amount(当日累计),
            last_close}]
    """
    points = []
    # 1) 逐根 bar 展开 3s 价格点；源分钟 high/low 必须在本分钟序列中真实出现
    #    （嵌入最近价格点），否则滚动极值无法在对应分钟还原。expand_bar 已将
    #    价格夹取到 [low, high]，仅当序列未触及极值时才需嵌入；首/末点固定为
    #    bar.open/bar.close（分钟开盘与分钟定型价不受嵌入影响）
    for bar in minutes:
        expanded = expand_bar(bar)
        prices = [b["close"] for b in expanded]
        vols = [b["volume"] for b in expanded]
        if max(prices) < bar["high"]:
            idx = min(range(len(prices)), key=lambda i: abs(prices[i] - bar["high"]))
            prices[idx] = bar["high"]
        if min(prices) > bar["low"]:
            idx = min(range(len(prices)), key=lambda i: abs(prices[i] - bar["low"]))
            prices[idx] = bar["low"]
        prices[0] = bar["open"]
        prices[-1] = bar["close"]
        for i, b in enumerate(expanded):
            points.append({"time_key": b["time_key"], "close": prices[i],
                           "volume": vols[i]})

    # 2) 全局滚动：滚动极值 high/low + 前缀累计 volume/amount（快照语义）
    snapshots = []
    day_high = day_low = None
    cum_v = 0
    cum_a = 0.0
    for p in points:
        if day_high is None:
            day_high = day_low = p["close"]
        else:
            day_high = max(day_high, p["close"])
            day_low = min(day_low, p["close"])
        cum_v += p["volume"]
        cum_a += p["volume"] * p["close"]
        snapshots.append({
            "time_key": p["time_key"],
            "high": round(day_high, 3), "low": round(day_low, 3),
            "close": round(p["close"], 3),
            "volume": cum_v, "amount": round(cum_a, 2),
            "last_close": last_close,
        })
    return snapshots


# ───────────── 随机交易日生成 ─────────────

def gen_random_day(trade_date: str, code: str, start_price: float = None,
                   drift: float = 0.0) -> tuple:
    """随机生成一个完整交易日的快照流（含 open hint）

    内部先生成 1min bar（9:31-11:30 / 13:01-15:00，共 240 根）再拓展为快照流
    （gen_snapshots_from_minutes）；返回 (ticks, open_hint)，open_hint 取首根
    bar open（= 起始价，随机/场景场景下即昨收）。
    """
    price = start_price if start_price else random.uniform(0.8, 1.5)
    last_close = price
    minutes = []
    for hh, mm in _trading_minutes():
        # 场景漂移 + 随机波动
        pct = random.gauss(drift, 0.0015)
        new_price = max(0.1, price * (1 + pct))
        o = price
        c = new_price
        h = max(o, c) * (1 + random.uniform(0, 0.0008))
        l = min(o, c) * (1 - random.uniform(0, 0.0008))
        vol = random.randint(100000, 3000000)
        minutes.append({
            "time_key": f"{trade_date} {hh:02d}:{mm:02d}:00",
            "open": round(o, 3), "high": round(h, 3),
            "low": round(l, 3), "close": round(c, 3),
            "volume": vol,
        })
        price = new_price
    return gen_snapshots_from_minutes(minutes, last_close), minutes[0]["open"]


def _trading_minutes():
    """交易时段分钟序列（真实 1m bar 口径）：9:31-11:30、13:01-15:00，共 240 根

    注意排除午休（11:31-11:59 无行情），首根从 9:31 起（9:30 为连续竞价首分钟，
    与 QMT/同花顺等数据源 1m 序列一致）。
    """
    result = []
    for hh, mm in _minute_range((9, 31), (11, 30)):
        result.append((hh, mm))
    for hh, mm in _minute_range((13, 1), (15, 0)):
        result.append((hh, mm))
    return result


def _minute_range(start: tuple, end: tuple):
    """闭区间分钟序列 (h1,m1) ~ (h2,m2)，含两端"""
    t = start
    while t <= end:
        yield t
        t = (t[0] + 1, 0) if t[1] == 59 else (t[0], t[1] + 1)


# ───────────── 场景生成 ─────────────

def gen_scenario_day(trade_date: str, code: str, scenario: str) -> tuple:
    """内置场景快照流：single_up 单边上涨 / single_down 单边下跌 / volatile 震荡

    返回 (ticks, open_hint)，open_hint 取首根 bar open（= 起始价 1.0 = 昨收）。
    """
    price = 1.0
    last_close = price
    drift = {"single_up": 0.002, "single_down": -0.002, "volatile": 0.0}[scenario]
    minutes = []
    for hh, mm in _trading_minutes():
        pct = drift + random.gauss(0, 0.0008) if scenario != "volatile" \
            else random.gauss(0, 0.003)
        new_price = max(0.1, price * (1 + pct))
        o, c = price, new_price
        h = max(o, c) * (1 + random.uniform(0, 0.0005))
        l = min(o, c) * (1 - random.uniform(0, 0.0005))
        minutes.append({
            "time_key": f"{trade_date} {hh:02d}:{mm:02d}:00",
            "open": round(o, 3), "high": round(h, 3),
            "low": round(l, 3), "close": round(c, 3),
            "volume": random.randint(100000, 2000000),
        })
        price = new_price
    return gen_snapshots_from_minutes(minutes, last_close), minutes[0]["open"]


# ───────────── 写入 tick_data ─────────────

# game_days（天维度真实行情 + 轮次计数）聚合 upsert 模板
# （__TABLE__ 为 tick_data/tick_data_sim 白名单；命名参数 %(name)s 风格）
# 快照口径：volume/amount 取末条快照累计值（array_agg DESC [1]），不可 sum；
# high/low = max/min 各快照滚动极值；open（今开）为当日常量：hint 有效则写
# hint，否则保留库内旧值（INSERT 时以首条快照 close 兑底）；round_count 为
# 游戏侧轮次计数，随轮次创建/删除单独维护，此处不覆盖
_SYNC_GAME_DAY_SQL = """
INSERT INTO game_days (code, trade_date, data_source, open, high, low, close,
                       volume, amount, last_close, tick_count, first_time_key,
                       last_time_key, is_complete)
SELECT %(code)s, %(trade_date)s, %(data_source)s,
       COALESCE(NULLIF(%(open_hint)s, 0), (array_agg(close ORDER BY time_key))[1]),
       max(high), min(low),
       (array_agg(close ORDER BY time_key DESC))[1],
       (array_agg(volume ORDER BY time_key DESC))[1],
       (array_agg(amount ORDER BY time_key DESC))[1],
       %(last_close)s,
       count(*), min(time_key), max(time_key),
       (max(time_key) >= %(date_end)s)
FROM __TABLE__
WHERE code = %(code)s AND trade_date = %(trade_date)s
ON CONFLICT (code, trade_date, data_source) DO UPDATE SET
  open = CASE WHEN %(open_hint)s > 0 THEN %(open_hint)s ELSE game_days.open END,
  high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,
  volume = EXCLUDED.volume, amount = EXCLUDED.amount,
  tick_count = EXCLUDED.tick_count,
  first_time_key = EXCLUDED.first_time_key,
  last_time_key = EXCLUDED.last_time_key,
  is_complete = EXCLUDED.is_complete,
  last_close = CASE WHEN EXCLUDED.last_close > 0 THEN EXCLUDED.last_close
                    ELSE game_days.last_close END,
  updated_at = now()
"""


def sync_game_day(dsn: str, code: str, trade_date: str, table: str,
                  last_close: float = 0.0, open_hint: float = 0.0):
    """写入/刷新 game_days 天维度真实行情（与后端 refresh_day 同口径）

    按 (code, trade_date) 从指定 tick 表聚合统计该日行情（快照口径：条数/首末
    时间/极值/末条累计量额与 tick 表对齐；open 为当日常量随 open_hint 维护；
    round_count 为游戏侧轮次计数不覆盖）；昨收确定链：hint（生成侧从
    stock_kline 读取/内置）> 库内已有有效值；两者均无效时拒绝写入——
    last_close 无效（空/0/NaN）的日行情视为异常数据，不入库（提示错误，请
    携带有效 last_close 后重试）。表名仅允许 tick_data / tick_data_sim（qmt/sim）。
    """
    if table not in ("tick_data", "tick_data_sim"):
        log("错误: sync_game_day 表名仅支持 tick_data/tick_data_sim: %s" % table)
        return
    data_source = "qmt" if table == "tick_data" else "sim"
    conn = get_conn(dsn)
    try:
        cur = conn.cursor()
        lc = last_close if (last_close and last_close == last_close
                            and last_close > 0) else 0.0
        if not lc:
            # hint 无效：回读库内已有有效昨收（upsert 分支会保留旧值，
            # 但全新行需显式回读才能获得有效值）
            cur.execute(
                "SELECT last_close FROM game_days "
                "WHERE code=%s AND trade_date=%s AND data_source=%s "
                "AND last_close > 0 AND last_close < 'Infinity' LIMIT 1",
                (code, trade_date, data_source))
            row = cur.fetchone()
            if row:
                lc = float(row[0])
        if not (lc and lc == lc and lc > 0):
            log("错误: game_days 拒绝写入 %s %s %s：昨收缺失（hint/库内旧值均无效），"
                "last_close 为空/0 属异常数据不入库，请携带有效 last_close 后重试"
                % (code, trade_date, data_source))
            return
        oh = open_hint if (open_hint and open_hint == open_hint
                           and open_hint > 0) else 0.0
        params = {"code": code, "trade_date": trade_date,
                  "data_source": data_source, "last_close": lc,
                  "date_end": trade_date + " 15:00:00", "open_hint": oh}
        cur.execute(_SYNC_GAME_DAY_SQL.replace("__TABLE__", table), params)
        conn.commit()
        cur.close()
        log("刷新 game_days: code=%s date=%s source=%s last_close=%s open=%s"
            % (code, trade_date, data_source, lc, oh))
    except Exception as e:
        log("刷新 game_days 失败: %s（请先执行 python migrate_db.py 建表）" % e)
    finally:
        conn.close()


def write_ticks(dsn: str, code: str, ticks: list, table: str = "tick_data",
                open_hint: float = 0.0):
    """批量写入当日快照行情（幂等 upsert，依赖 uq_tick_code_time 唯一约束）

    快照口径：tick dict 的 volume/amount 为当日累计值（单调不减）、high/low 为
    滚动极值、close 为最新价；逐行 INSERT ... ON CONFLICT (code, time_key)
    DO UPDATE，数据库自动去重并保留最新写入值。重复写入同一日时，已存在的行
    被覆盖更新，无残留重复记录，中途失败也不会丢旧数据。写入完成后同步刷新
    game_days 天维度真实行情（昨收随生成值维护，今开取 open_hint）。
    """
    conn = get_conn(dsn)
    try:
        cur = conn.cursor()
        rows = []
        for t in ticks:
            rows.append((
                code, t["time_key"][:10], t["time_key"],
                t["high"], t["low"], t["close"],
                t["volume"], t["amount"],
            ))
        cur.executemany(
            """INSERT INTO %s (code, trade_date, time_key, high, low, close,
                               volume, amount)
               VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)
               ON CONFLICT (code, time_key) DO UPDATE SET
                 trade_date=EXCLUDED.trade_date, high=EXCLUDED.high,
                 low=EXCLUDED.low, close=EXCLUDED.close,
                 volume=EXCLUDED.volume, amount=EXCLUDED.amount""" % table,
            rows,
        )
        conn.commit()
        cur.close()
        log("upsert %s: code=%s 共 %d 条快照（已按 code+time_key 去重，保留最新）"
            % (table, code, len(rows)))
        # 同步维护天维度真实行情（昨收 hint 取生成时的 last_close，今开取生成侧）
        if ticks:
            hint = next((float(t["last_close"]) for t in ticks
                         if t.get("last_close")), 0.0)
            sync_game_day(dsn, code, ticks[0]["time_key"][:10], table, hint,
                          open_hint=open_hint)
    finally:
        conn.close()


def verify_ticks(dsn: str, code: str, date: str):
    """入库校验：条数 / 时间范围 / 量额单调 / game_days 对齐（快照口径）

    完整日参考 240~242 根 1min × 20 = 4800~4840 条（真实 QMT 与模拟生成
    口径略有差异，仅提示，不视为错误）；快照口径对账：game_days.volume/amount
    应等于 tick_data 末条快照的当日累计值（输出末条累计供核对）；序列差分无
    负值即量额单调不减（无回退自洽）。
    """
    conn = get_conn(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*), min(time_key), max(time_key), min(low), max(high) "
            "FROM tick_data WHERE code=%s AND trade_date=%s", (code, date))
        cnt, t0, t1, lo, hi = cur.fetchone()
        cur.execute(
            "SELECT last_close, is_complete, tick_count FROM game_days "
            "WHERE code=%s AND trade_date=%s AND data_source='qmt'", (code, date))
        day = cur.fetchone()
        # 末条快照当日累计值（game_days 对账基准）；序列差分负值计数（单调性）
        cur.execute(
            "SELECT (array_agg(volume ORDER BY time_key DESC))[1],"
            "       (array_agg(amount ORDER BY time_key DESC))[1] "
            "FROM tick_data WHERE code=%s AND trade_date=%s", (code, date))
        last_v, last_a = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM ("
            "  SELECT volume - lag(volume, 1, 0) OVER (ORDER BY time_key) AS dv "
            "  FROM tick_data WHERE code=%s AND trade_date=%s) s WHERE dv < 0",
            (code, date))
        neg_cnt = cur.fetchone()[0]
        cur.close()
        if cnt not in (240 * 20, 241 * 20, 242 * 20):
            log("注意: 条数 %d（完整日参考值 4800~4840 = 240~242 根 1min×20，"
                "请核对源数据是否完整）" % cnt)
        if day:
            lc, complete, day_cnt = day
            align = "对齐" if day_cnt == cnt else "不对齐"
            log("校验: %s %s 共 %d 条, 时间 %s ~ %s, low=%s, high=%s, "
                "日记录昨收=%s, tick_count=%d(%s), 完整日=%s, "
                "末条累计 volume=%s amount=%s, 量额单调=%s"
                % (code, date, cnt, t0, t1, lo, hi, lc, day_cnt, align, complete,
                   last_v, last_a, "是" if neg_cnt == 0 else "否(%d 处回退)" % neg_cnt))
        else:
            log("警告: %s %s 无 game_days 日记录，请检查 sync_game_day 是否执行"
                % (code, date))
        return cnt
    finally:
        conn.close()


# ───────────── 从 AutoTrade 库读取 1min 数据 ─────────────

def load_minutes_from_autotrade(autotrade_dsn: str, code: str, date: str) -> list:
    """从 AutoTrade 库 stock_kline 读取指定日 1min K 线"""
    conn = get_conn(autotrade_dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT time_key, open, high, low, close, volume, last_close
               FROM stock_kline
               WHERE code=%s AND period='1m'
                 AND time_key >= %s::timestamp AND time_key < (%s::timestamp + interval '1 day')
               ORDER BY time_key""",
            (code, date, date),
        )
        rows = cur.fetchall()
        cur.close()
        minutes = []
        last_close = 0
        for tk, o, h, l, c, vol, lc in rows:
            minutes.append({
                "time_key": tk.strftime("%Y-%m-%d %H:%M:%S"),
                "open": float(o), "high": float(h), "low": float(l),
                "close": float(c), "volume": int(float(vol or 0)),
            })
            if last_close == 0 and lc:
                last_close = float(lc)
        return minutes, last_close
    finally:
        conn.close()


# ───────────── 实时模拟（POST） ─────────────

def run_live(cfg, code: str, date: str, sleep: float):
    """实时模式：从 tick_data 读取该日快照，按 3s 间隔 POST 行情 + 60s 心跳

    今开 open / 昨收 last_close 为当日常量，从 game_days 天维度真实行情读出
    （tick 表已不再存），随每根 POST 上报；volume/amount 为当日累计值原样上报
    （与后端存储口径一致，后端自行差分消费）。
    """
    backend = cfg.get("app.backend_url", "http://127.0.0.1:16000")
    dsn = cfg.get("database.path", "")
    conn = get_conn(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT open, last_close FROM game_days "
            "WHERE code=%s AND trade_date=%s AND data_source='qmt'",
            (code, date))
        day = cur.fetchone()
        day_open = float(day[0]) if day and day[0] else 0.0
        day_lc = float(day[1]) if day and day[1] else 0.0
        cur.execute(
            "SELECT time_key, high, low, close, volume, amount "
            "FROM tick_data WHERE code=%s AND trade_date=%s ORDER BY time_key",
            (code, date),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    if not rows:
        log("错误: %s %s 无数据，请先运行 --from-autotrade 或 --random"
            % (code, date))
        return

    log("实时模式: 共 %d 条快照, sleep=%.3fs, open=%s, last_close=%s"
        % (len(rows), sleep, day_open, day_lc))
    last_hb = 0
    for i, (tk, h, l, c, vol, amt) in enumerate(rows):
        now = time.time()
        if now - last_hb >= 60:
            try:
                requests.post(f"{backend}/api/v1/agent/heartbeat",
                              json={"agent_name": "mock_agent", "timestamp": now},
                              timeout=5)
                log("心跳上报 OK")
            except Exception as e:
                log("心跳失败: %s" % e)
            last_hb = now
        try:
            resp = requests.post(
                f"{backend}/api/v1/agent/tick",
                json={
                    "agent_name": "mock_agent",
                    "code": code,
                    "trade_date": date,
                    "time_key": tk,
                    "open": day_open, "high": h, "low": l, "close": c,
                    "volume": vol, "amount": amt, "last_close": day_lc,
                },
                timeout=5,
            )
            if i % 20 == 0:
                log("tick %d/%d %s close=%s -> %s" % (i + 1, len(rows), tk, c, resp.status_code))
        except Exception as e:
            log("tick 失败: %s" % e)
        time.sleep(sleep)


# ───────────── 主入口 ─────────────

def main():
    parser = argparse.ArgumentParser(description="StockGame 模拟 QMT Agent")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--from-autotrade", action="store_true", help="从 AutoTrade 库拓展 1min→快照流并写入")
    parser.add_argument("--live", action="store_true", help="实时模式（POST 行情+心跳）")
    parser.add_argument("--random", action="store_true", help="随机生成完整交易日")
    parser.add_argument("--scenario", choices=["single_up", "single_down", "volatile"],
                        help="内置场景")
    parser.add_argument("--code", default=DEFAULT_CODE,
                        help="股票代码，默认 %s（改脚本 DEFAULT_CODE）" % DEFAULT_CODE)
    parser.add_argument("--date", default=None,
                        help="交易日 YYYY-MM-DD（默认 %s，改脚本 DEFAULT_DATE；"
                             "--live 模式不传则取今天）" % DEFAULT_DATE)
    parser.add_argument("--gen-days", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=0.05, help="实时模式 tick 间隔")
    args = parser.parse_args()

    cfg = Config.get_instance(args.config)
    dsn = cfg.get("database.path", "")

    if args.live:
        run_live(cfg, args.code, args.date or datetime.date.today().strftime("%Y-%m-%d"), args.sleep)
        return

    if args.from_autotrade:
        date = args.date or DEFAULT_DATE
        auto_dsn = cfg.get("data_source.autotrade_dsn", "")
        if not auto_dsn:
            log("错误: 未配置 data_source.autotrade_dsn")
            sys.exit(1)
        log("从 AutoTrade 库读取 %s %s 1min 数据..." % (args.code, date))
        minutes, last_close = load_minutes_from_autotrade(auto_dsn, args.code, date)
        if not minutes:
            log("错误: AutoTrade 库无 %s %s 的 1m 数据" % (args.code, date))
            sys.exit(1)
        log("读取到 %d 根 1min bar, last_close=%s，开始拓展..." % (len(minutes), last_close))
        ticks = gen_snapshots_from_minutes(minutes, last_close)
        log("拓展完成: %d 条快照（= %d × 20）" % (len(ticks), len(minutes)))
        write_ticks(dsn, args.code, ticks,
                    open_hint=minutes[0]["open"] if minutes else 0)
        verify_ticks(dsn, args.code, date)
        log("写入完成: %s %s 已就绪（tick_data，QMT 数据源）" % (args.code, date))
        return

    if args.random:
        dates = []
        for _ in range(args.gen_days):
            d = (datetime.date(2026, 8, 10) + datetime.timedelta(days=random.randint(0, 10)))
            dates.append(d.strftime("%Y-%m-%d"))
        for d in dates:
            ticks, open_hint = gen_random_day(d, args.code)
            write_ticks(dsn, args.code, ticks, open_hint=open_hint)
            verify_ticks(dsn, args.code, d)
            log("随机生成交易日 %s: %d 条" % (d, len(ticks)))
        return

    if args.scenario:
        date = args.date or DEFAULT_DATE
        ticks, open_hint = gen_scenario_day(date, args.code, args.scenario)
        write_ticks(dsn, args.code, ticks, open_hint=open_hint)
        verify_ticks(dsn, args.code, date)
        log("场景 %s 生成完成: %d 条" % (args.scenario, len(ticks)))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
