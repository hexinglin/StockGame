"""
模块名称: engine/grid_math.py
说明:    网格表纯函数 — 网格行生成 / 成交触发状态推导（参考 AutoTrade 网格数学）

    网格模型（每行 = 一个买点 + 一个卖点）:
      - 买点线: buy(idx) = idx × grid_spacing + init_value      （一元一次直线，idx 整数）
      - 卖点线: sell(idx) = buy(idx) + sell_gap_ratio × grid_spacing
          sell_gap_ratio 默认 2（"2x为卖点"），即卖点 = 买点再往上涨 2 个间隔处。
          等价 buy(idx + sell_gap_ratio)，保证"低位买、涨 2 格卖"的配对语义。
      - 锚点: 以昨收（last_close）为基准，定位其最近网格索引，向上下各展开 grid_up / grid_down 行。
      - 持仓合理化: 当前总持仓总量均分到每个网格行（向下取整到 100 股整数倍）。

    状态推导（成交触发）: 遍历该轮次成交记录，用 hit_tolerance 容差判定某笔成交是否命中某行
      的买点/卖点；买、卖均命中 → done（已完成），单个命中 → buy/sell（部分完成），否则 pending。
    本模块为纯函数，不读写 DB/Redis，调用方（game_engine.get_grid）负责取数。
"""

# 网格默认参数（config.yaml game.grid 可覆盖）
DEFAULT_GRID_PARAMS = {
    "grid_spacing": 0.005,      # 网格间隔（绝对价格）
    "init_value": 0.003,        # 买点线初始值（x=0 时买点价）
    "offset": 0.001,            # off 值（卖点额外偏移，默认不叠加，预留扩展）
    "sell_gap_ratio": 2,        # 卖点 = 买点 + sell_gap_ratio × 间隔（"2x为卖点"）
    "grid_up": 8,               # 锚点上方网格行数
    "grid_down": 8,             # 锚点下方网格行数
    "hit_tolerance": 0.001,     # 成交触发命中容差（绝对价格 ±）
}

_GRID_EPS = 1e-9   # 消除二进制浮点误差


def normalize_params(raw: dict) -> dict:
    """合并配置到默认网格参数（过滤无效键，缺失/异常值用默认）"""
    p = dict(DEFAULT_GRID_PARAMS)
    if not raw:
        return p
    for k in p:
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            p[k] = float(v) if k != "grid_up" and k != "grid_down" and k != "sell_gap_ratio" else int(v)
    return p


def grid_level_of(price: float, init_value: float, grid_spacing: float) -> int:
    """价格最近的网格索引（四舍五入取整）"""
    if grid_spacing <= 0:
        return 0
    return int(round((price - init_value) / grid_spacing - _GRID_EPS))


def buy_grid_price(idx: int, init_value: float, grid_spacing: float,
                   hit_tolerance: float = 0.0) -> float:
    """买点价格（保留 3 位小数）"""
    return round(idx * grid_spacing + init_value, 3)


def sell_grid_price(idx: int, init_value: float, grid_spacing: float,
                    sell_gap_ratio: float = 2, hit_tolerance: float = 0.0) -> float:
    """卖点价格 = 买点 + sell_gap_ratio × 间隔（保留 3 位小数）"""
    return round(buy_grid_price(idx, init_value, grid_spacing) + sell_gap_ratio * grid_spacing, 3)


def is_hit(price: float, grid_price: float, hit_tolerance: float) -> bool:
    """价格是否命中网格：|price - grid_price| <= tolerance"""
    return abs(price - grid_price) <= hit_tolerance + _GRID_EPS


def build_grid_rows(anchor_price: float, total_shares: int, params: dict) -> list:
    """生成网格行列表（围绕锚点向上下展开，持仓均分到各格）

    Args:
        anchor_price: 锚点价（通常昨收 last_close），用于定位中心网格索引
        total_shares: 当前总持仓量（均分到每格）
        params: 归一化后的网格参数（normalize_params 输出）

    Returns:
        list[dict]: 每行 {idx, buy_price, sell_price, shares, status, buy_hit, sell_hit}
            按买点价由低到高排序（idx 升序）
    """
    p = params
    spacing = float(p["grid_spacing"])
    init_value = float(p["init_value"])
    ratio = int(p["sell_gap_ratio"])
    up = int(p["grid_up"])
    down = int(p["grid_down"])

    if spacing <= 0:
        return []

    base = grid_level_of(anchor_price, init_value, spacing)
    rows = []
    for idx in range(base - down, base + up + 1):
        rows.append({
            "idx": idx,
            "buy_price": buy_grid_price(idx, init_value, spacing),
            "sell_price": sell_grid_price(idx, init_value, spacing, ratio),
            "shares": 0,
            "status": "pending",
            "buy_hit": False,
            "sell_hit": False,
        })

    # 持仓合理化：总持仓均分到每格（向下取整到 100 股整数倍，至少保留 100 股）
    n = len(rows)
    if n and total_shares > 0:
        per = int((int(total_shares) // n) // 100) * 100
        per = max(per, 100)
        for r in rows:
            r["shares"] = per

    return rows


def mark_grid_status(rows: list, trades: list, params: dict) -> list:
    """根据成交记录推导每行网格状态（买/卖命中容差判定）

    Args:
        rows: build_grid_rows 输出
        trades: 该轮次成交记录 [{direction, price}, ...]
        params: 归一化后的网格参数（取 hit_tolerance）

    Returns:
        rows（就地更新 status/buy_hit/sell_hit）
    """
    tol = float(params.get("hit_tolerance", DEFAULT_GRID_PARAMS["hit_tolerance"]))

    for t in trades or []:
        price = t.get("price")
        if not price or price <= 0:
            continue
        direction = t.get("direction")
        if direction == "buy":
            for r in rows:
                if is_hit(price, r["buy_price"], tol):
                    r["buy_hit"] = True
                    break
        elif direction == "sell":
            for r in rows:
                if is_hit(price, r["sell_price"], tol):
                    r["sell_hit"] = True
                    break

    for r in rows:
        if r["buy_hit"] and r["sell_hit"]:
            r["status"] = "done"
        elif r["buy_hit"]:
            r["status"] = "buy"      # 已买入持仓，待卖出
        elif r["sell_hit"]:
            r["status"] = "sell"     # 已卖出，待回补
        else:
            r["status"] = "pending"  # 未触发
    return rows
