"""
convert_kline_to_3s.py — 从 AutoTrade 库 stock_kline(1min) 转换为 3s 等间隔快照流

转换结果写入 StockGame 库 tick_data_sim 表（模拟数据源，游戏在 QMT 无该日
数据且页面允许模拟时使用）。每次只能转换一个交易日。

用法（两种等价，任选其一）:
  1) 直接运行（推荐）: 改脚本下方 DEFAULT_CODE / DEFAULT_DATE 后
     python scripts/convert_kline_to_3s.py
  2) 命令行覆盖默认值:
     python scripts/convert_kline_to_3s.py --code 588000.SH --date 2026-08-14

说明:
  每根 1min bar 按 mock_agent 同一算法（gen_snapshots_from_minutes）拆为
  20 个 3s 快照点（价格路径插值、极值嵌入、volume 总和守恒、当日累计量额）；
  重复执行同一日会先清空该日旧数据再重新生成（幂等）。
"""
import argparse
import datetime
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app.utils.config import Config
from mock_agent import (get_conn, log, load_minutes_from_autotrade,
                        gen_snapshots_from_minutes, sync_game_day)


# ================= 默认参数区（改这里后直接运行即可） =================
DEFAULT_CODE = "588000.SH"     # 股票代码
DEFAULT_DATE = "2026-08-14"    # 交易日 YYYY-MM-DD（一次只能转换一日）
# ====================================================================


def upsert_sim_ticks(dsn: str, code: str, date: str, ticks: list,
                     open_hint: float = 0.0):
    """写入 tick_data_sim（幂等 upsert，依赖 uq_tick_sim_code_time 唯一约束）

    快照口径：tick dict 的 volume/amount 为当日累计值、high/low 为滚动极值、
    close 为最新价；逐行 INSERT ... ON CONFLICT DO UPDATE，数据库按
    (code, time_key) 自动去重并保留最新写入值。重复转换同一日时，已存在的行
    被覆盖更新，无残留重复记录，中途失败也不会丢旧数据。写入完成后同步写入
    game_days（与 tick 对齐）天维度行情（昨收随生成值维护，今开取 open_hint）。
    """
    conn = get_conn(dsn)
    try:
        cur = conn.cursor()
        rows = []
        for t in ticks:
            rows.append((
                code, date, t["time_key"],
                t["high"], t["low"], t["close"],
                t["volume"], t["amount"],
            ))
        cur.executemany(
            """INSERT INTO tick_data_sim (code, trade_date, time_key, high,
                                          low, close, volume, amount)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (code, time_key) DO UPDATE SET
                 trade_date=EXCLUDED.trade_date,
                 high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
                 volume=EXCLUDED.volume, amount=EXCLUDED.amount""",
            rows,
        )
        conn.commit()
        cur.close()
        log("upsert tick_data_sim: code=%s date=%s 共 %d 条快照（已按 code+time_key 去重，保留最新）"
            % (code, date, len(rows)))
        # 同步写入 game_days（与 tick 对齐）天维度行情（昨收 hint 取生成时的 last_close）
        if ticks:
            hint = next((float(t["last_close"]) for t in ticks
                         if t.get("last_close")), 0.0)
            sync_game_day(dsn, code, date, "tick_data_sim", hint,
                          open_hint=open_hint)
    finally:
        conn.close()


def verify(dsn: str, code: str, date: str):
    """入库校验：条数 / 时间范围 / OHLC 区间 / game_days 对齐记录（快照口径）

    标准完整日 = 241 根 1min × 20 点 = 4820 条（仅提示，不视为错误）；
    game_days.volume/amount 应等于 tick_data_sim 末条快照的当日累计值。
    """
    conn = get_conn(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*), min(time_key), max(time_key), min(low), max(high) "
            "FROM tick_data_sim WHERE code=%s AND trade_date=%s", (code, date))
        cnt, t0, t1, lo, hi = cur.fetchone()
        cur.execute(
            "SELECT last_close, is_complete, tick_count FROM game_days "
            "WHERE code=%s AND trade_date=%s AND data_source='sim'", (code, date))
        day = cur.fetchone()
        cur.execute(
            "SELECT (array_agg(volume ORDER BY time_key DESC))[1],"
            "       (array_agg(amount ORDER BY time_key DESC))[1] "
            "FROM tick_data_sim WHERE code=%s AND trade_date=%s", (code, date))
        last_v, last_a = cur.fetchone()
        cur.close()
        if cnt != 241 * 20:
            log("注意: 条数 %d（标准完整日 241×20=4820，请核对源数据是否完整）" % cnt)
        if day:
            lc, complete, dkcnt = day
            aligned = "对齐" if dkcnt == cnt else "不对齐(%s vs %s)" % (dkcnt, cnt)
            log("校验: %s %s 共 %d 条, 时间 %s ~ %s, low=%s, high=%s, "
                "game_days 昨收=%s, 完整日=%s, tick 对账=%s, "
                "末条累计 volume=%s amount=%s"
                % (code, date, cnt, t0, t1, lo, hi, lc, complete, aligned,
                   last_v, last_a))
        else:
            log("警告: %s %s 无 game_days 日记录，请检查 sync_game_day 是否执行"
                % (code, date))
        return cnt
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="stockkline 1min → 3s 转换（一次仅一个交易日）")
    parser.add_argument("--config", default=None,
                        help="配置文件路径（默认 $STOCKGAME_CONFIG 或项目根 config.yaml）")
    parser.add_argument("--code", default=DEFAULT_CODE,
                        help="股票代码，默认 %s（改脚本 DEFAULT_CODE）" % DEFAULT_CODE)
    parser.add_argument("--date", default=DEFAULT_DATE,
                        help="交易日 YYYY-MM-DD，默认 %s（改脚本 DEFAULT_DATE，"
                             "一次只能转换一日）" % DEFAULT_DATE)
    args = parser.parse_args()

    # 每次只能转换一日：严格校验日期格式
    try:
        datetime.datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        log("错误: --date 格式应为 YYYY-MM-DD，且一次只能转换一个交易日")
        sys.exit(1)

    cfg = Config.get_instance(
        args.config or os.environ.get("STOCKGAME_CONFIG")
        or os.path.join(PROJECT_ROOT, "config.yaml"))
    auto_dsn = cfg.get("data_source.autotrade_dsn", "")
    dsn = cfg.get("database.path", "")
    if not auto_dsn:
        log("错误: 未配置 data_source.autotrade_dsn")
        sys.exit(1)

    log("从 AutoTrade 库读取 %s %s 1min K 线..." % (args.code, args.date))
    minutes, last_close = load_minutes_from_autotrade(auto_dsn, args.code, args.date)
    if not minutes:
        log("错误: AutoTrade 库 stock_kline 无 %s %s 的 1m 数据"
            % (args.code, args.date))
        sys.exit(1)
    log("读取到 %d 根 1min bar, last_close=%s，开始拓展..." % (len(minutes), last_close))

    ticks = gen_snapshots_from_minutes(minutes, last_close)
    log("拓展完成: %d 条快照（= %d × 20）" % (len(ticks), len(minutes)))

    # 纯 upsert 写入：DB 按 (code, time_key) 自动去重保留最新，无需先清空
    upsert_sim_ticks(dsn, args.code, args.date, ticks,
                     open_hint=minutes[0]["open"] if minutes else 0)
    verify(dsn, args.code, args.date)
    log("转换完成: %s %s 已就绪（tick_data_sim），页面开启模拟开关后可用于游戏"
        % (args.code, args.date))


if __name__ == "__main__":
    main()
