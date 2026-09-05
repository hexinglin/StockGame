# -*- coding: utf-8 -*-
"""rebuild_qmt_3s.py — 历史分钟级 qmt tick 重灌为 3s 等间隔快照流

背景: StockGame tick_data(qmt) 中 2026-01-07 ~ 2026-02-06 的历史数据为分钟级
直灌（241 根整分/日，秒位恒 00），与引擎"每根 tick = 3s 快照"的播放模型不
匹配，导致游戏内行情时间只有分钟跳动、无秒级变化。本脚本以 AutoTrade 库
stock_kline(1m) 为源，按 mock_agent 同款算法（gen_snapshots_from_minutes，
与 sim 源转换 convert_kline_to_3s.py 完全一致）展开为 4820 条快照流重灌。

特性:
- 幂等可重跑：write_ticks 逐行 INSERT ... ON CONFLICT (code, time_key)
  DO UPDATE，3s 展开含每分钟整分首根，天然覆盖旧分钟行、无残留；
- 昨收防线兼容：昨收优先取 game_days(qmt) 已有有效值（防漂移），无则用
  AutoTrade 1m 携带值；均无效的日（如部分日 02-06）game_days
  拒绝写入，与后端防线口径一致；
- 重灌后自动同步 game_days（tick_count/is_complete 对账）。

用法: python scripts/rebuild_qmt_3s.py [--code 588000.SH]
                                       [--start 2026-01-01] [--end 2026-02-28]
"""
import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.utils.config import Config
from mock_agent import (get_conn, log, load_minutes_from_autotrade,
                        gen_snapshots_from_minutes, write_ticks, verify_ticks)

DEFAULT_CODE = "588000.SH"
DEFAULT_START = "2026-01-01"
DEFAULT_END = "2026-02-28"


def target_days(dsn: str, code: str, start: str, end: str) -> list:
    """tick_data 在 [start, end] 区间内现存交易日（按日升序）"""
    conn = get_conn(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT trade_date FROM tick_data "
            "WHERE code=%s AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
            (code, start, end))
        days = [r[0] for r in cur.fetchall()]
        cur.close()
        return days
    finally:
        conn.close()


def existing_last_close(dsn: str, code: str, date: str) -> float:
    """game_days(qmt) 现有有效昨收（无则 0.0）"""
    conn = get_conn(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT last_close FROM game_days WHERE code=%s AND trade_date=%s "
            "AND data_source='qmt' AND last_close > 0 AND last_close < 'Infinity' "
            "LIMIT 1", (code, date))
        row = cur.fetchone()
        cur.close()
        return float(row[0]) if row else 0.0
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="历史分钟级 qmt tick 重灌为 3s 粒度（幂等，可重复执行）")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--code", default=DEFAULT_CODE,
                        help="股票代码，默认 %s" % DEFAULT_CODE)
    parser.add_argument("--start", default=DEFAULT_START,
                        help="起始交易日 YYYY-MM-DD，默认 %s" % DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END,
                        help="结束交易日 YYYY-MM-DD，默认 %s" % DEFAULT_END)
    args = parser.parse_args()

    for name in ("start", "end"):
        d = getattr(args, name)
        try:
            datetime.datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            log("错误: --%s 格式应为 YYYY-MM-DD" % name)
            sys.exit(1)

    cfg = Config.get_instance(args.config)
    auto_dsn = cfg.get("data_source.autotrade_dsn", "")
    dsn = cfg.get("database.path", "")
    if not auto_dsn:
        log("错误: 未配置 data_source.autotrade_dsn")
        sys.exit(1)

    days = target_days(dsn, args.code, args.start, args.end)
    if not days:
        log("无目标日: tick_data 中 %s 在 %s ~ %s 无数据" % (args.code, args.start, args.end))
        return
    log("待重灌 %d 日: %s" % (len(days), ", ".join(days)))

    total_ok = total_skip = 0
    for date in days:
        # 昨收 hint：库内已有有效值优先（防漂移），无则用 AutoTrade 1m 携带值
        lc = existing_last_close(dsn, args.code, date)
        minutes, auto_lc = load_minutes_from_autotrade(auto_dsn, args.code, date)
        if not minutes:
            log("跳过 %s: AutoTrade stock_kline 无 1m 源数据" % date)
            total_skip += 1
            continue
        if not lc:
            lc = auto_lc
        if not (lc and lc == lc and lc > 0):
            log("警告 %s: 昨收无效（库内/AutoTrade 均缺失），重灌后 game_days 将拒绝写入"
                % date)
        ticks = gen_snapshots_from_minutes(minutes, lc)
        log("==> %s 重灌: 1m %d 根 -> 快照 %d 条 (last_close=%s)"
            % (date, len(minutes), len(ticks), lc))
        write_ticks(dsn, args.code, ticks,
                    open_hint=minutes[0]["open"] if minutes else 0)
        # upsert + sync day_kline/game_days
        verify_ticks(dsn, args.code, date)
        total_ok += 1
    log("重灌完成: 成功 %d 日, 跳过 %d 日" % (total_ok, total_skip))


if __name__ == "__main__":
    main()
