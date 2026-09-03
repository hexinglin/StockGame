"""
模块名称: engine/account.py
说明:    模拟账户 — 按轮次独立跟踪，下单即冻结、撤单解冻、成交冻结转实扣
"""
import logging
import math

logger = logging.getLogger(__name__)

# 游戏参数默认值（config.yaml 可覆盖）
DEFAULT_PARAMS = {
    "base_shares": 500000,      # 底仓 50w 股
    "initial_cash": 500000,     # 初始现金
    "fee_rate": 0.0001,         # 单面万1，无最低 5 元，无印花税
}


class MockAccount:
    """模拟账户

    字段:
        available_cash: 可用现金
        frozen_cash:    挂单冻结现金
        volume:         持仓总量（底仓 + 买入 - 卖出）
        frozen_volume:  挂单冻结持仓
        avg_price:      持仓加权均价
    """

    def __init__(self, base_shares: int = None, initial_cash: float = None,
                 fee_rate: float = None, open_price: float = 0):
        params = dict(DEFAULT_PARAMS)
        params.update({k: v for k, v in {
            "base_shares": base_shares,
            "initial_cash": initial_cash,
            "fee_rate": fee_rate,
        }.items() if v is not None})

        self.base_shares = int(params["base_shares"])
        self.initial_cash = float(params["initial_cash"])
        self.fee_rate = float(params["fee_rate"])

        self.available_cash = self.initial_cash
        self.frozen_cash = 0.0
        self.volume = self.base_shares
        self.frozen_volume = 0
        self.today_bought = 0        # T+1：当日买入不可卖
        self.avg_price = open_price or 0
        self.last_price = open_price

    # ── 计算辅助 ──

    def fee_for(self, amount: float) -> float:
        """手续费 = 金额 × 万1（无最低 5 元、无印花税）"""
        return round(amount * self.fee_rate, 2)

    def available_volume(self) -> int:
        """可卖持仓 = 持仓 - 冻结 - 今日买入（T+1）"""
        return max(0, self.volume - self.frozen_volume - self.today_bought)

    def frozen_amount(self, price: float, shares: int) -> float:
        """买入委托冻结金额 = 金额 + 手续费"""
        amount = price * shares
        return amount + self.fee_for(amount)

    def position_value(self, price: float = None) -> float:
        """持仓市值（按最新价）"""
        p = price if price is not None else self.last_price
        return self.volume * p

    def total_assets(self, price: float = None) -> float:
        """总资产 = 现金 + 持仓市值（含冻结）"""
        return self.available_cash + self.frozen_cash + self.position_value(price)

    def float_pnl(self, price: float = None) -> float:
        """浮动盈亏 = (最新价 - 均价) × 持仓（不含底仓成本差）"""
        p = price if price is not None else self.last_price
        if self.avg_price <= 0:
            return 0.0
        return (p - self.avg_price) * self.volume

    # ── 冻结 / 解冻 ──

    def freeze_buy(self, price: float, shares: int) -> bool:
        """买单下单冻结：可用现金 >= 金额+手续费 才允许，冻结之"""
        amount = self.frozen_amount(price, shares)
        if self.available_cash - self.frozen_cash < amount:
            return False
        self.frozen_cash += amount
        return True

    def freeze_sell(self, shares: int) -> bool:
        """卖单下单冻结：可卖持仓 >= 数量 才允许，冻结之"""
        if self.available_volume() < shares:
            return False
        self.frozen_volume += shares
        return True

    def unfreeze_buy(self, price: float, shares: int):
        """买单解冻（撤单/拒单）"""
        self.frozen_cash = max(0.0, self.frozen_cash - self.frozen_amount(price, shares))

    def unfreeze_sell(self, shares: int):
        """卖单解冻（撤单/拒单）"""
        self.frozen_volume = max(0, self.frozen_volume - shares)

    # ── 成交 ──

    def fill_buy(self, price: float, shares: int, frozen_amount: float = None) -> float:
        """买入成交：从冻结转实扣，更新持仓与均价

        Args:
            price: 成交价
            shares: 成交数量
            frozen_amount: 该单下单时的冻结额（含手续费），仅扣本单冻结额；
                           为 None 时按本次成交金额扣减（兼容无冻结场景）

        返回实际手续费
        """
        fee = self.fee_for(price * shares)
        amount = price * shares + fee
        # 冻结转实扣：只扣本单冻结额，多冻结部分自然回流为可用资金
        if frozen_amount is not None:
            self.frozen_cash = max(0.0, self.frozen_cash - frozen_amount)
        else:
            self.frozen_cash = max(0.0, self.frozen_cash - amount)
        self.available_cash -= amount

        # 加权均价更新
        total_cost = self.avg_price * self.volume + price * shares
        self.volume += shares
        self.today_bought += shares   # T+1：今日买入不可卖
        if self.volume > 0:
            self.avg_price = total_cost / self.volume
        return fee

    def fill_sell(self, price: float, shares: int) -> float:
        """卖出成交：冻结持仓转实扣，加现金"""
        fee = self.fee_for(price * shares)
        self.frozen_volume = max(0, self.frozen_volume - shares)
        self.volume -= shares
        self.available_cash += price * shares - fee
        return fee

    # ── 序列化 ──

    def to_dict(self) -> dict:
        return {
            "base_shares": self.base_shares,
            "initial_cash": self.initial_cash,
            "fee_rate": self.fee_rate,
            "available_cash": round(self.available_cash, 2),
            "frozen_cash": round(self.frozen_cash, 2),
            "volume": self.volume,
            "frozen_volume": self.frozen_volume,
            "today_bought": self.today_bought,
            "avg_price": round(self.avg_price, 4) if self.avg_price else 0,
            "last_price": self.last_price,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MockAccount":
        acct = cls(
            base_shares=d.get("base_shares"),
            initial_cash=d.get("initial_cash"),
            fee_rate=d.get("fee_rate"),
            open_price=d.get("avg_price", 0),
        )
        acct.available_cash = float(d.get("available_cash", acct.initial_cash))
        acct.frozen_cash = float(d.get("frozen_cash", 0))
        acct.volume = int(d.get("volume", acct.base_shares))
        acct.frozen_volume = int(d.get("frozen_volume", 0))
        acct.today_bought = int(d.get("today_bought", 0))
        acct.avg_price = float(d.get("avg_price", 0))
        acct.last_price = float(d.get("last_price", 0))
        return acct
