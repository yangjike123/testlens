"""全部通过的一组：演示勾选运行、参数化用例计数、通过率。"""

from __future__ import annotations

import pytest

from shop.cart import Cart


def test_empty_cart_costs_nothing() -> None:
    assert Cart().total() == 0
    assert Cart().payable() == 8.0  # 空车也要运费（未满免邮门槛）


def test_add_multiplies_price_by_quantity() -> None:
    cart = Cart()
    cart.add("键盘", 30.0, 2)
    assert cart.total() == 60.0
    assert cart.payable() == 68.0


@pytest.mark.parametrize("quantity", [1, 2, 5])
def test_quantity_variants(quantity: int) -> None:
    cart = Cart()
    cart.add("鼠标", 10.0, quantity)
    assert len(cart.items) == 1
    assert cart.total() == 10.0 * quantity


def test_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValueError, match="数量"):
        Cart().add("显示器", 800.0, 0)
