# -*- coding: utf-8 -*-
"""一次性修复：tick_data(QMT 源) 的 volume 单位由「手」校正为「股」

背景: QMT 真实源上传的 volume 单位为「手」（1手=100股），而模拟源
(tick_data_sim) 的 volume 为「股」。游戏消费端均价线 = amount/volume 统一按
「股」计算，QMT 源因此被放大 100 倍抬高 Y 轴。

本脚本按「日粒度 ratio = sum(amount)/sum(volume)」判断单位：
  ratio ≈ close*100  -> 手（需 ×100 校正）
  ratio ≈ close      -> 股（正常，不改）

用法:
  python scripts/fix_qmt_volume_unit.py          # dry-run：仅打印将影响的日期与量级
  python scripts/fix_qmt_volume_unit.py --apply  # 实际写入并同步 game_days
"""
import argparse
import sys

import psycopg2

DSN = "postgresql://autotrade:autotrade_pg_dev_2026@192.168.1.9:30004/stockgame"
CODE = "588000.SH"


def connect(dsn):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


def main(apply: bool):
    conn = connect(DSN)
    cur = conn.cursor()

    # 1) 按日期聚合判断 volume 单位
    cur.execute("""
        SELECT trade_date, count(*),
               max(close)::float AS mc,
               sum(volume)::float AS vol,
               sum(amount)::float AS amt
        FROM tick_data WHERE code=%s GROUP BY trade_date ORDER BY trade_date
    """, (CODE,))
    rows = cur.fetchall()

    fix_dates = []       # 需校验为「手」的日期
    pending = []         # (date, n, mc, vol, amt)
    for d, n, mc, vol, amt in rows:
        ratio = amt / vol if vol else 0
        is_hand = abs(ratio - mc * 100) < 8        # 手：amount/volume ≈ close*100
        is_share = abs(ratio - mc) < 8             # 股：正常
        if is_hand and not is_share:
            fix_dates.append(d)
            pending.append((d, n, mc, vol, amt))
        elif not is_share:
            print("  [跳过?] %s 无法判定单位 ratio=%.2f close_max=%.3f" % (d, ratio, mc))

    if not pending:
        print("未发现「手」单位数据，无需校正。")
        cur.close(); conn.close(); return

    print("将校正 %d 天（volume ×100）：" % len(pending))
    for d, n, mc, vol, amt in pending:
        print("  %s  条数=%d  close_max=%.3f  volume=%.0f  amount=%.0f"
              % (d, n, mc, vol, amt))

    if not apply:
        print("\n[dry-run] 未写入。确认无误后加 --apply 执行。")
        cur.close(); conn.close(); return

    # 2) 执行校正：tick_data.volume ×100
    for d, *_ in pending:
        cur.execute(
            "UPDATE tick_data SET volume = volume * 100 "
            "WHERE code=%s AND trade_date=%s", (CODE, d))
        print("  已更新 tick_data  %s" % d)

    # 3) 同步 game_days（qmt 源）volume（其值聚合自 tick_data 末条累计值）
    for d, *_ in pending:
        cur.execute(
            "UPDATE game_days SET volume = volume * 100 "
            "WHERE code=%s AND trade_date=%s AND data_source='qmt'", (CODE, d))
        print("  已更新 game_days  %s (qmt)" % d)

    # 4) 验证：ratio 应回到 ≈ close
    print("\n=== 修复后校验 ===")
    cur.execute("""
        SELECT trade_date, max(close)::float, sum(volume)::float, sum(amount)::float
        FROM tick_data WHERE code=%s GROUP BY trade_date ORDER BY trade_date
    """, (CODE,))
    for d, mc, vol, amt in cur.fetchall():
        ratio = amt / vol if vol else 0
        unit = "股(正常)" if abs(ratio - mc) < 8 else ("手(仍异常)" if abs(ratio - mc * 100) < 8 else "?")
        print("  %s  ratio=%.2f  -> %s" % (d, ratio, unit))

    cur.close(); conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际写入（默认 dry-run）")
    args = parser.parse_args()
    main(args.apply)
