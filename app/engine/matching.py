"""
模块名称: engine/matching.py
说明:    撮合纯逻辑 — 交易时段判定 + 成交判定。
         从 game_engine.py 拆出；不触碰 DB/推送，可脱离 app context 单测。
"""

# ── 交易时段划分（盘前集合竞价 / 盘中连续 / 收盘集合竞价 / 盘后）──
# A 股交易日结构：09:15-09:25 开盘集合竞价（09:25 出竞价价），09:25-09:30
# 竞价等待段，09:30-11:30/13:00-14:57 连续竞价，14:57-15:00 收盘集合竞价
# （只挂不撤），15:00 收盘。盘后（>15:00，如固定价交易）不进入游戏。
SESSION_PRE_FILL = "09:25:00"        # 开盘集合竞价撮合时点（出竞价价）
SESSION_CLOSING_START = "14:57:00"   # 收盘集合竞价开始（此后只挂不撤）
SESSION_DAY_END = "15:00:00"         # 交易日收盘（最后一根 tick）


def time_hms(time_key: str) -> str:
    """取 time_key 的 HH:MM:SS 部分（格式异常时原样返回，供字符串比较）"""
    return time_key[11:19] if len(time_key) >= 19 else time_key


def session_of(time_key: str) -> str:
    """按 time_key 判定当前所属交易时段: pre / intraday / closing / post

    - pre      盘前集合竞价（< 09:30:00，含 09:25 出竞价价）
    - intraday 盘中连续竞价（09:30:00 ~ 14:56:59）
    - closing  收盘集合竞价（14:57:00 ~ 15:00:00，只挂不撤）
    - post     盘后（> 15:00:00，应被上层过滤，正常情况下不会到达）
    """
    hms = time_hms(time_key)
    if hms < "09:30:00":
        return "pre"
    if hms >= SESSION_CLOSING_START:
        return "closing" if hms <= SESSION_DAY_END else "post"
    return "intraday"


def is_auction_point(time_key: str) -> bool:
    """当前 tick 是否为（开盘/收盘）集合竞价撮合点

    返回 True 表示该 tick 时刻已到竞价撮合时点（盘前 09:25 后、收盘 15:00），
    应以其最新价作为竞价价集中撮合；False 表示竞价等待期（只挂不撮）。
    """
    hms = time_hms(time_key)
    if hms < "09:30:00":
        return hms >= SESSION_PRE_FILL
    if hms >= SESSION_CLOSING_START:
        return hms >= SESSION_DAY_END
    return False  # 盘中连续竞价，逐 tick 以最新价触及即成交（非竞价口径）


def decide_fill(direction: str, order_type: str, price: float,
                tick_close: float, auction_price: float = None):
    """成交判定（纯函数）：返回 (filled, fill_price)

    连续竞价（auction_price=None）：快照口径下 high/low 是截至该时刻的
    当日滚动极值，不代表本时刻可成交的价格区间，因此限价单以最新价
    （快照 close）触发：买单价 <= 限价、卖单价 >= 限价即成交（成交价=限价），
    市价单按最新价成交。

    集合竞价（auction_price 传入竞价价）：限价单以竞价价触发——买单限价 >=
    竞价价成交、卖单限价 <= 竞价价成交，成交价=竞价价（集合竞价以竞价价
    撮合，成交价恒等于竞价价，而非限价）；市价单同样按竞价价成交。
    """
    if order_type != "limit":
        # 市价单：按当前口径价（竞价价或最新价）立即成交
        return True, (auction_price if auction_price is not None else tick_close)

    if auction_price is not None:    # 集合竞价：限价以竞价价为基准
        if direction == "buy" and price >= auction_price:
            return True, auction_price
        if direction == "sell" and price <= auction_price:
            return True, auction_price
    else:                            # 连续竞价：限价以最新价为基准
        if direction == "buy" and tick_close <= price:
            return True, price
        if direction == "sell" and tick_close >= price:
            return True, price
    return False, price
