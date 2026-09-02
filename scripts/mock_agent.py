"""
mock_agent.py — 模拟 QMT Agent（自测工具）

用法:
  # 1. 数据拓展模式（默认）：从 AutoTrade 库 1min K 线拓展为 3s 写入 tick_data
  python scripts/mock_agent.py --from-autotrade --code 588000.SH --date 2026-08-14

  # 2. 实时模式：按 3s 间隔 POST 行情 + 每 60s POST 心跳（--sleep 可调速）
  python scripts/mock_agent.py --live --code 588000.SH --date 2026-08-14 --sleep 0.1

  # 3. 随机兜底：纯随机生成完整交易日
  python scripts/mock_agent.py --random --gen-days 3

  # 4. 内置场景：单边上涨/下跌/震荡
  python scripts/mock_agent.py --scenario single_up --code 588000.SH

说明:
  数据拓展算法: 每根 1min bar 拆 20 根 3s bar
    - 首根 open=bar.open, 末根 close=bar.close
    - high/low 落入区间内单根, 中间 bar 在 [low, high] 内平滑插值(线性+小幅随机)
    - volume 随机权重拆分, 总和守恒; last_close 取当日首根
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


def gen_day_from_minutes(minutes: list, last_close: float) -> list:
    """将一日 1min bar 列表拓展为 3s tick 列表"""
    ticks = []
    for i, bar in enumerate(minutes):
        expanded = expand_bar(bar)
        for t in expanded:
            t["last_close"] = last_close
        ticks.extend(expanded)
    return ticks


# ───────────── 随机交易日生成 ─────────────

def gen_random_day(trade_date: str, code: str, start_price: float = None,
                   drift: float = 0.0) -> list:
    """生成一个完整交易日的 1min bar（9:30-11:30 / 13:00-15:00，共 240 根）"""
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
    return gen_day_from_minutes(minutes, last_close)


def _trading_minutes():
    """交易时段分钟序列：9:30-11:30、13:00-15:00"""
    result = []
    for hh in range(9, 12):
        for mm in range(0, 60):
            if hh == 9 and mm < 30:
                continue
            result.append((hh, mm))
    for hh in range(13, 15):
        for mm in range(0, 60):
            result.append((hh, mm))
    result.append((15, 0))
    return result


# ───────────── 场景生成 ─────────────

def gen_scenario_day(trade_date: str, code: str, scenario: str) -> list:
    """内置场景：single_up 单边上涨 / single_down 单边下跌 / volatile 震荡"""
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
    return gen_day_from_minutes(minutes, last_close)


# ───────────── 写入 tick_data ─────────────

def write_ticks(dsn: str, code: str, ticks: list):
    """批量写入 tick_data（幂等 upsert，依赖 uq_tick_code_time 唯一约束）

    不预清空旧数据：逐行 INSERT ... ON CONFLICT DO UPDATE，数据库按
    (code, time_key) 自动去重并保留最新写入值。重复写入同一日时，
    已存在的行被覆盖更新，无残留重复记录，中途失败也不会丢旧数据。
    """
    conn = get_conn(dsn)
    try:
        cur = conn.cursor()
        rows = []
        for t in ticks:
            trade_date = t["time_key"][:10]
            rows.append((
                code, trade_date, t["time_key"], t["open"], t["high"], t["low"],
                t["close"], t["volume"], round(t["close"] * t["volume"], 2),
                t.get("last_close", 0),
            ))
        cur.executemany(
            """INSERT INTO tick_data (code, trade_date, time_key, open, high, low, close,
                                      volume, amount, last_close)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (code, time_key) DO UPDATE SET
                 trade_date=EXCLUDED.trade_date, open=EXCLUDED.open, high=EXCLUDED.high,
                 low=EXCLUDED.low, close=EXCLUDED.close, volume=EXCLUDED.volume,
                 amount=EXCLUDED.amount, last_close=EXCLUDED.last_close""",
            rows,
        )
        conn.commit()
        cur.close()
        log("upsert tick_data: code=%s 共 %d 条（已按 code+time_key 去重，保留最新）"
            % (code, len(rows)))
    finally:
        conn.close()


def verify_ticks(dsn: str, code: str, date: str):
    """入库校验：条数（标准完整日 4820=241×20）/ 时间范围 / OHLC 区间 / last_close"""
    conn = get_conn(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*), min(time_key), max(time_key), min(low), max(high), "
            "min(last_close), max(last_close) FROM tick_data "
            "WHERE code=%s AND trade_date=%s", (code, date))
        cnt, t0, t1, lo, hi, lc0, lc1 = cur.fetchone()
        cur.close()
        if cnt != 4820:
            log("注意: 条数 %d（非标准完整日 4820=241×20，请核对源数据是否完整）" % cnt)
        log("校验: %s %s 共 %d 条, 时间 %s ~ %s, low=%s, high=%s, last_close=%s"
            % (code, date, cnt, t0, t1, lo, hi, lc0))
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
    """实时模式：从 tick_data 读取该日数据，按 3s 间隔 POST 行情 + 60s 心跳"""
    backend = cfg.get("app.backend_url", "http://127.0.0.1:16000")
    dsn = cfg.get("database.path", "")
    conn = get_conn(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT time_key, open, high, low, close, volume, amount, last_close "
            "FROM tick_data WHERE code=%s AND trade_date=%s ORDER BY time_key",
            (code, date),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    if not rows:
        log("错误: %s %s 无数据，请先运行 --from-autotrade 或 --random")
        return

    log("实时模式: 共 %d 条 tick, sleep=%.3fs" % (len(rows), sleep))
    last_hb = 0
    for i, (tk, o, h, l, c, vol, amt, lc) in enumerate(rows):
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
                    "open": o, "high": h, "low": l, "close": c,
                    "volume": vol, "amount": amt, "last_close": lc,
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
    parser.add_argument("--from-autotrade", action="store_true", help="从 AutoTrade 库拓展 1min→3s 并写入")
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
        ticks = gen_day_from_minutes(minutes, last_close)
        log("拓展完成: %d 根 3s tick（= %d × 20）" % (len(ticks), len(minutes)))
        write_ticks(dsn, args.code, ticks)
        verify_ticks(dsn, args.code, date)
        log("写入完成: %s %s 已就绪（tick_data，QMT 数据源）" % (args.code, date))
        return

    if args.random:
        dates = []
        for _ in range(args.gen_days):
            d = (datetime.date(2026, 8, 10) + datetime.timedelta(days=random.randint(0, 10)))
            dates.append(d.strftime("%Y-%m-%d"))
        for d in dates:
            ticks = gen_random_day(d, args.code)
            write_ticks(dsn, args.code, ticks)
            verify_ticks(dsn, args.code, d)
            log("随机生成交易日 %s: %d 条" % (d, len(ticks)))
        return

    if args.scenario:
        date = args.date or DEFAULT_DATE
        ticks = gen_scenario_day(date, args.code, args.scenario)
        write_ticks(dsn, args.code, ticks)
        verify_ticks(dsn, args.code, date)
        log("场景 %s 生成完成: %d 条" % (args.scenario, len(ticks)))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
