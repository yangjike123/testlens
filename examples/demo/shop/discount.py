"""折扣计算：参数校验那条 raise 在 demo 里故意没有测试覆盖。"""

from __future__ import annotations


def apply_discount(amount: float, percent: int) -> float:
    if not 0 <= percent <= 100:
        raise ValueError("折扣必须在 0-100 之间")
    return round(amount * (100 - percent) / 100, 2)
