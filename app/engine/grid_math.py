"""
模块名称: engine/grid_math.py
说明:    网格表纯函数 — 网格行生成 / 成交触发状态推导（参考 AutoTrade 网格数学）

    网格模型（每行 = 一个买点 + 一个卖点）:
      - 买点线: buy(idx) = idx × grid_spacing + init_value      （一元一次直线，idx 为买格号）
      - 卖点线: sell(sell_idx) = sell_idx × grid_spacing + init_value + offset   （sell_idx 为卖格号）
      - 间隔（interval，下拉 1-6，默认 2）决定买卖格号的偏移：off_grid = interval - 1。
          买入方向行: 主格号=买格号，卖格号 = 买格号 + off_grid
          卖出方向行: 主格号=卖格号，买格号 = 卖格号 - off_grid
      - 方向（锚点分侧）: 主格号 < 锚点格号 → 买入（先买后卖）；> 锚点格号 → 卖出（先卖后买）。
      - 调整间隔时: 买入行重算其卖出侧（卖格号+卖点）；卖出行重算其买入侧（买格号+买点）。
      - 锚点: 以昨收（last_close）为基准，定位其最近网格索引，向上下各展开 grid_up / grid_down 行。
      - 持仓合理化: 当前总持仓总量均分到每个网格行（向下取整到 100 股整数倍）。

    状态推导（成交触发）: 遍历该轮次成交记录，用 hit_tolerance 容差判定某笔成交是否命中某行
      的买点/卖点；买、卖均命中 → done（已完成），单个命中 → buy/sell（部分完成），否则 pending。
    本模块为纯函数，不读写 DB/Redis，调用方（game_engine.get_grid）负责取数。

    梯度推导（建梯度 / 消梯度，参考 AutoTrade T0 网格匹配思想）:
      - 初始化不再全量建梯子（build_grid_rows 停用保留）；梯度仅由成交产生，
        derive_gradient_rows 按成交时序逐笔重放（买卖各一个价格匹配函数，卖侧含偏移值）。
      - 消梯度（先判）: 成交价命中某未消完梯度的对侧网格线（买成交→该行买点线、卖成交→
        该行卖点线含偏移值；容差判定）即消该梯度；数量不足只消部分（行保留剩余数量继续
        等待），取价格最近者，一笔最多消一个。
      - 建梯度: 消无可消、或消费后有多的部分，按成交价就近网格线新建（买→买入向行、
        卖→卖出向行）；同方向同格号已有未消完行 → 数量合并。
      - 消完的行保留并标记 done（已完成），不做删除。
      - 行内记录两侧真实成交价（buy_fill_price / sell_fill_price，多次成交取加权均价；
        未成交侧为 None），供前端与网格价（买点/卖点）对照展示。
"""

# 网格默认参数（config.yaml game.grid 可覆盖）
DEFAULT_GRID_PARAMS = {
    "grid_spacing": 0.005,      # 网格间隔（绝对价格）
    "init_value": 0.003,        # 买点线初始值（x=0 时买点价）
    "offset": 0.001,            # off 值（卖点线偏移）
    "interval": 2,              # 间隔（下拉 1-6）：买卖格号偏移 = interval - 1
    "grid_up": 8,               # 锚点上方网格行数
    "grid_down": 8,             # 锚点下方网格行数
    "hit_tolerance": 0.001,     # 成交触发命中容差（绝对价格 ±）
}

_GRID_EPS = 1e-9   # 消除二进制浮点误差


def normalize_params(raw: dict) -> dict:
    """合并配置到默认网格参数（过滤无效键，缺失/异常值用默认；interval 收敛 1-6）"""
    p = dict(DEFAULT_GRID_PARAMS)
    if not raw:
        return p
    for k in p:
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            p[k] = float(v) if k not in ("grid_up", "grid_down", "interval") else int(v)
    # 间隔合法域与前端下拉一致：1-6（买卖格号偏移 = interval - 1）
    p["interval"] = max(1, min(int(p["interval"]), 6))
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


def sell_grid_price(sell_idx: int, init_value: float, grid_spacing: float,
                    offset: float = 0.0) -> float:
    """卖点价格（卖格号 sell_idx）：sell(sell_idx) = sell_idx × grid_spacing + init_value + offset（保留 3 位小数）"""
    return round(sell_idx * grid_spacing + init_value + offset, 3)


def is_hit(price: float, grid_price: float, hit_tolerance: float) -> bool:
    """价格是否命中网格：|price - grid_price| <= tolerance"""
    return abs(price - grid_price) <= hit_tolerance + _GRID_EPS


def build_grid_rows(anchor_price: float, total_shares: int, params: dict,
                    interval_map: dict = None) -> list:
    """生成网格行列表（围绕锚点向上下展开，持仓均分到各格）

    Args:
        anchor_price: 锚点价（通常昨收 last_close），用于定位中心网格索引
        total_shares: 当前总持仓量（均分到每格）
        params: 归一化后的网格参数（normalize_params 输出）
        interval_map: 可选，每行自定义间隔 {主格号idx(int): interval}，
            命中的行用自定义间隔，否则用 params.interval 默认。用于持久化行级间隔。

    Returns:
        list[dict]: 每行 {idx, direction, buy_idx, sell_idx, interval, buy_price,
            sell_price, shares, status, buy_hit, sell_hit}，按 idx 升序
    """
    p = params
    spacing = float(p["grid_spacing"])
    init_value = float(p["init_value"])
    offset = float(p["offset"])
    default_interval = int(p.get("interval", 2))
    imap = {}
    if interval_map:
        for k, v in interval_map.items():
            try:
                imap[int(k)] = int(v)
            except (TypeError, ValueError):
                continue
    up = int(p["grid_up"])
    down = int(p["grid_down"])

    if spacing <= 0:
        return []

    base = grid_level_of(anchor_price, init_value, spacing)
    rows = []
    for idx in range(base - down, base + up + 1):
        interval = max(imap.get(idx, default_interval), 1)
        off_grid = max(interval - 1, 0)      # 间隔 → 买卖格号偏移（interval 最小 1 → 偏移 0）
        # 锚点分侧方向：低价侧（< 锚点格号）→ 买入；高价侧（>= 锚点格号）→ 卖出
        if idx < base:
            direction = "buy"
            buy_idx = idx
            sell_idx = idx + off_grid
        else:
            direction = "sell"
            sell_idx = idx
            buy_idx = idx - off_grid
        rows.append({
            "idx": idx,                     # 主格号
            "direction": direction,         # 方向：buy(先买后卖) / sell(先卖后买)
            "buy_idx": buy_idx,             # 买入格号
            "sell_idx": sell_idx,           # 卖出格号
            "interval": interval,           # 该行实际生效间隔（可逐行覆盖）
            "buy_price": buy_grid_price(buy_idx, init_value, spacing),
            "sell_price": sell_grid_price(sell_idx, init_value, spacing, offset),
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


# ── 梯度推导（建梯度 / 消梯度）──

def buy_grid_index_of(price: float, init_value: float, grid_spacing: float,
                      hit_tolerance: float = DEFAULT_GRID_PARAMS["hit_tolerance"]):
    """买入网格匹配函数（参考 AutoTrade T0）: 返回 (买格号, 买点价)；价格在死区返回 (None, None)

    价格取最近的买点线（四舍五入）；命中判定 |price - 买点价| <= hit_tolerance。
    """
    if grid_spacing <= 0:
        return None, None
    idx = grid_level_of(price, init_value, grid_spacing)
    line = buy_grid_price(idx, init_value, grid_spacing)
    if not is_hit(price, line, hit_tolerance):
        return None, None
    return idx, line


def sell_grid_index_of(price: float, init_value: float, grid_spacing: float,
                       offset: float,
                       hit_tolerance: float = DEFAULT_GRID_PARAMS["hit_tolerance"]):
    """卖出网格匹配函数（带偏移值 offset，参考 AutoTrade T0）: 返回 (卖格号, 卖点价)；死区返回 (None, None)

    价格取最近的卖点线（四舍五入，卖点线含 offset）；命中判定 |price - 卖点价| <= hit_tolerance。
    """
    if grid_spacing <= 0:
        return None, None
    idx = int(round((price - init_value - offset) / grid_spacing - _GRID_EPS))
    line = sell_grid_price(idx, init_value, grid_spacing, offset)
    if not is_hit(price, line, hit_tolerance):
        return None, None
    return idx, line


def derive_gradient_rows(trades: list, params: dict, interval_map: dict = None) -> list:
    """由成交记录推导梯度行（建梯度 / 消梯度）— 初始化不再全量建梯子

    按成交时序（旧→新）逐笔重放，每笔先消、消无可消或数量有多的再建：
      1. 消: 成交价命中某未消完梯度的对侧网格线（买成交→该行买点线、卖成交→该行卖点线含
         偏移值）即消该梯度；数量不足只消部分（行保留剩余数量继续等待），取最近者，
         一笔最多消一个。
      2. 建: 按成交价就近网格线新建（买成交→买入向行、卖成交→卖出向行）；同方向同格号
         已有未消完行 → 数量合并；消完的行保留并标记 done。

    Args:
        trades: 该轮次成交记录 [{direction, price, shares}, ...]（list_trades 输出，新→旧）
        params: 归一化后的网格参数（normalize_params 输出）
        interval_map: 可选行级间隔 {主格号idx(int): interval}

    Returns:
        list[dict]: 梯度行（字段同 build_grid_rows 输出 + buy_fill_price/sell_fill_price
                    两侧真实成交价加权均价，未成交侧为 None），按主格号升序
    """
    p = params
    spacing = float(p["grid_spacing"])
    init_value = float(p["init_value"])
    offset = float(p["offset"])
    default_interval = int(p.get("interval", 2))
    tol = float(p.get("hit_tolerance", DEFAULT_GRID_PARAMS["hit_tolerance"]))
    imap = {}
    if interval_map:
        for k, v in interval_map.items():
            try:
                imap[int(k)] = int(v)
            except (TypeError, ValueError):
                continue
    if spacing <= 0:
        return []

    rows = []

    def _interval_of(idx):
        return max(imap.get(idx, default_interval), 1)

    def _add_fill(row, side, price, shares):
        """累计某侧真实成交（两侧成交价取加权均价用，side: buy/sell）"""
        row["_fill_amt_" + side] += float(price) * int(shares)
        row["_fill_sh_" + side] += int(shares)

    def _new_row(idx, direction, shares, price):
        interval = _interval_of(idx)
        off_grid = max(interval - 1, 0)
        if direction == "sell":
            sell_idx, buy_idx = idx, idx - off_grid
        else:
            buy_idx, sell_idx = idx, idx + off_grid
        row = {
            "idx": idx,
            "direction": direction,
            "buy_idx": buy_idx,
            "sell_idx": sell_idx,
            "interval": interval,
            "buy_price": buy_grid_price(buy_idx, init_value, spacing),
            "sell_price": sell_grid_price(sell_idx, init_value, spacing, offset),
            "shares": int(shares),
            "status": "sell" if direction == "sell" else "buy",
            "buy_hit": direction == "buy",
            "sell_hit": direction == "sell",
            # 两侧真实成交价（加权均价，未成交为 None；推导结束后统一填充）
            "buy_fill_price": None,
            "sell_fill_price": None,
            "_fill_amt_buy": 0.0, "_fill_sh_buy": 0,
            "_fill_amt_sell": 0.0, "_fill_sh_sell": 0,
        }
        rows.append(row)
        _add_fill(row, direction, price, shares)
        return row

    def _open_row(direction, main_idx):
        """同方向同主格号未消完行（建梯度数量合并用）"""
        for r in rows:
            if r["direction"] == direction and r["idx"] == main_idx and r["status"] != "done":
                return r
        return None

    def _build(direction, price, shares):
        """建梯度：就近网格线；同方向同格号未消完 → 数量合并"""
        if direction == "sell":
            idx = int(round((price - init_value - offset) / spacing - _GRID_EPS))
        else:
            idx = grid_level_of(price, init_value, spacing)
        row = _open_row(direction, idx)
        if row is not None:
            row["shares"] += int(shares)
            _add_fill(row, direction, price, shares)
            return row
        return _new_row(idx, direction, shares, price)

    def _consume(row, shares, price, side):
        """消梯度（部分/全部），返回未消完的剩余数量；side = 成交方向（累计真实成交价）"""
        x = min(int(shares), int(row["shares"]))
        row["shares"] -= x
        _add_fill(row, side, price, x)
        if row["shares"] <= 0:
            row["buy_hit"] = row["sell_hit"] = True
            row["status"] = "done"
        return int(shares) - x

    for t in reversed(trades or []):
        price = t.get("price")
        shares = int(t.get("shares") or 0)
        direction = t.get("direction")
        if not price or price <= 0 or shares <= 0:
            continue
        leftover = shares
        if direction == "sell":
            idx, _ = sell_grid_index_of(price, init_value, spacing, offset, tol)
            if idx is not None:
                cand = [r for r in rows if r["direction"] == "buy"
                        and r["status"] != "done" and r["sell_idx"] == idx]
                if cand:
                    row = min(cand, key=lambda r: abs(price - r["sell_price"]))
                    leftover = _consume(row, shares, price, "sell")
        elif direction == "buy":
            idx, _ = buy_grid_index_of(price, init_value, spacing, tol)
            if idx is not None:
                cand = [r for r in rows if r["direction"] == "sell"
                        and r["status"] != "done" and r["buy_idx"] == idx]
                if cand:
                    row = min(cand, key=lambda r: abs(price - r["buy_price"]))
                    leftover = _consume(row, shares, price, "buy")
        if leftover > 0:
            _build(direction, price, leftover)

    # 填充两侧真实成交价（加权均价，未成交侧为 None），并清理累计字段
    for r in rows:
        for side in ("buy", "sell"):
            sh = r.pop("_fill_sh_" + side)
            amt = r.pop("_fill_amt_" + side)
            if sh > 0:
                r[side + "_fill_price"] = round(amt / sh, 4)

    rows.sort(key=lambda r: r["idx"])
    return rows


def apply_idx_override(rows: list, overrides: dict, params: dict) -> list:
    """应用行级格号覆盖（人工微调网格行位置）

    按行「原格号」查覆盖的新格号并重算该行主格号/买卖格号与买卖点价。
    行原格号（写入 rows[*]["key_idx"]）是由成交流水推导出的稳定标识，也是
    覆盖表的键——连续微调时沿用同一键覆盖，不会因显示格号变化而丢失关联。

    成交价（buy_fill_price / sell_fill_price）不动：那是已发生成交的事实，
    与行位置调整无关；间隔（interval，决定 off_grid）也不动，故整行平移时
    买卖格号同步移动、买卖点价同步平移。

    纯函数，不读写 DB/Redis（调用方负责取覆盖表与持久化）。
    """
    spacing = float(params["grid_spacing"])
    init_value = float(params["init_value"])
    offset = float(params["offset"])
    for x in rows:
        x["key_idx"] = x["idx"]         # 稳定标识：所有行级覆盖以此为键
    for x in rows:
        try:
            new_idx = int(overrides.get(str(x["key_idx"])))
        except (TypeError, ValueError):
            continue                     # 无覆盖 / 值非法 → 保持原格号
        if new_idx == x["key_idx"] or new_idx < 0:
            continue
        off_grid = max(int(x.get("interval") or 1) - 1, 0)
        if x["direction"] == "sell":
            buy_idx, sell_idx = new_idx - off_grid, new_idx
        else:
            buy_idx, sell_idx = new_idx, new_idx + off_grid
        x["idx"] = new_idx
        x["buy_idx"], x["sell_idx"] = buy_idx, sell_idx
        x["buy_price"] = buy_grid_price(buy_idx, init_value, spacing)
        x["sell_price"] = sell_grid_price(sell_idx, init_value, spacing, offset)
    return rows
