"""购物车：给 testlens 的覆盖率页签提供「有分支没跑到」的素材。"""

from __future__ import annotations

from dataclasses import dataclass, field

FREE_SHIPPING_THRESHOLD = 100.0
SHIPPING_FEE = 8.0


@dataclass
class Cart:
    items: list[tuple[str, float, int]] = field(default_factory=list)

    def add(self, name: str, price: float, quantity: int = 1) -> None:
        if quantity <= 0:
            raise ValueError("数量必须为正数")
        self.items.append((name, price, quantity))

    def total(self) -> float:
        return round(sum(price * quantity for _, price, quantity in self.items), 2)

    def shipping(self) -> float:
        # 满 100 免运费。demo 的测试只覆盖了「收费」这一支，
        # 所以覆盖率页签里 return 0.0 这一行会显示「未覆盖」。
        if self.total() >= FREE_SHIPPING_THRESHOLD:
            return 0.0
        return SHIPPING_FEE

    def payable(self) -> float:
        return round(self.total() + self.shipping(), 2)
