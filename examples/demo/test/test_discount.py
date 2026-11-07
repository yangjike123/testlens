"""折扣的正常路径都有测试；非法参数的 raise 故意没测（用来看未覆盖行）。"""

from __future__ import annotations

from shop.discount import apply_discount


def test_apply_discount_rounds_to_cents() -> None:
    assert apply_discount(199.99, 15) == 169.99


def test_full_discount_is_free() -> None:
    assert apply_discount(120.0, 100) == 0.0
